import logging
from pathlib import Path
from typing import Optional, Union
import threading
import time
from queue import Queue
import queue

from hardware.camera.config import DEFAULT_PHOTO_RESOLUTION, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_QUALITY, \
    DEFAULT_VIDEO_RESOLUTION
import os
from .base_dual_camera import BaseDualCamera
from .dual_camera import DualCamera
from .dual_camera_xvs import DualCameraXVS
from .dual_camera_rpi_sync import DualCameraRPISync
from .endstop import Endstop, TopEndstopTriggered, BottomEndstopTriggered
from .motor import Motor, TURNTABLE_END_DELAY_DEFAULT, TURNTABLE_STEP_TYPE, Z_STEP_TYPE
from .lights import DimmableLight
from .tof import ToFSensor
import cloud.cloudflare_r2 as r2
from system import file_control as fc
import statistics

log = logging.getLogger(__name__)

TOF_BACKGROUND_DISTANCE_MM = 535
# An object is detected only when the reading is closer than the backdrop by more than this
# margin, so ToF noise on the empty backdrop does not count as an object. Calibrate on the rig.
TOF_CLEARANCE_MARGIN_MM = 25
# Minimum pause after each turntable move before the next photo, so the object stops vibrating.
TURNTABLE_SETTLE_S = 0.2
# How often the Z-axis watchdog samples the limit switch in the direction of travel while the carriage moves.
ENDSTOP_POLL_S = 0.005

class ScannerError(Exception):
    pass

class ScannerState:
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"

    # States from which a new prepare_scan() cycle is allowed
    PREPARE_ALLOWED = (IDLE, COMPLETED, CANCELLED, ERROR)

    # Terminal states that cleanup() must not overwrite back to IDLE
    TERMINAL = (CANCELLED, ERROR, COMPLETED)

T8_THREADED_ROD_STEP = 8 # mm


class Scanner:
    def __init__(self, xvs: Optional[bool] = None, camera_mode: Optional[str] = None):
        self.tof = ToFSensor()
        self.turntable_motor = Motor(motor_id=0, dir_pin=7, step_pin=6, en_pin=8, step_type=TURNTABLE_STEP_TYPE)
        self.z_axis_motor = Motor(motor_id=1, dir_pin=4, step_pin=5, en_pin=3, step_type=Z_STEP_TYPE)

        self.up_endstop = Endstop(pin=22, pull_up=True, bounce_time=0.02)
        self.down_endstop = Endstop(pin=4, pull_up=True, bounce_time=0.02)

        # Wire up safety interrupts: Any endstop hit immediately aborts in-flight Z-axis steps
        self.up_endstop.when_pressed = self._on_top_endstop_hit
        self.down_endstop.when_pressed = self._on_bottom_endstop_hit

        self.lights = DimmableLight(relay_pin=11, pwm_channel=0, frequency=10000)

        self.dual_cameras: Optional[BaseDualCamera] = None

        if camera_mode is not None:
            self.camera_mode = camera_mode.lower()
        elif xvs is True:
            self.camera_mode = "xvs"
        elif xvs is False:
            self.camera_mode = os.environ.get("CAMERA_MODE", "rpi_sync").lower()
        else:
            self.camera_mode = os.environ.get("CAMERA_MODE", "rpi_sync").lower()

        self.xvs = (self.camera_mode == "xvs")

        self.state = ScannerState.IDLE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        # Set only while a Z move is in flight. The endstop callbacks send a *universal* STOP byte, so they must
        # not fire it while the Z axis is idle (it would abort the turntable or cut every motor).
        self._z_moving = threading.Event()
        # The universal STOP must be sent at most once per Z move: a second one would reach an idle Arduino and
        # cut the other motor as well.
        self._z_stop_sent = False
        self._z_stop_lock = threading.Lock()

        self._level = 0
        self.total_photos = 0
        self.stream_urls = None

        # used for uploading the images when they are captured
        self.upload_queue = Queue()
        self._upload_thread: Optional[threading.Thread] = None
        self._last_upload_time = time.time()
        self._upload_failed = False


    @property
    def is_streaming(self):
        if self.dual_cameras:
            return self.dual_cameras.is_streaming
        return False

    @property
    def level(self):
        return self._level

    def _on_top_endstop_hit(self) -> None:
        """Invoked when the physical upper ceiling limit switch is pressed."""
        if not self._z_moving.is_set():
            log.warning("[Scanner] TOP limit switch pressed while the Z-axis is not moving; ignored.")
            return
        log.warning("[Scanner] TOP limit switch triggered mid-motion!")
        self._stop_z_once()

    def _on_bottom_endstop_hit(self) -> None:
        """Invoked when the physical bottom home limit switch is pressed."""
        if not self._z_moving.is_set():
            log.warning("[Scanner] BOTTOM limit switch pressed while the Z-axis is not moving; ignored.")
            return
        log.info("[Scanner] BOTTOM limit switch triggered.")
        self._stop_z_once()

    def _begin_z_motion(self) -> None:
        """Marks a Z move as in flight (the endstop callbacks only act while this is set)."""
        with self._z_stop_lock:
            self._z_stop_sent = False
        self._z_moving.set()

    def _end_z_motion(self, watcher) -> None:
        if watcher is not None:
            stop_event, thread = watcher
            stop_event.set()
            thread.join(timeout=0.5)
        with self._z_stop_lock:
            self._z_moving.clear()

    def _stop_z_once(self) -> None:
        """
        Sends the (universal) STOP at most once per Z move, however many sources notice the endstop, and never after
        the move has ended: the check and the send share a lock with `_end_z_motion`, so a late callback cannot
        reach an idle Arduino (which would cut the torque of every motor).
        """
        with self._z_stop_lock:
            if self._z_stop_sent or not self._z_moving.is_set():
                return
            self._z_stop_sent = True
            self.z_axis_motor.stop()

    def _start_endstop_watch(self, endstop, label: str):
        """
        Level-based watchdog for the limit switch in the direction of travel. The gpiozero callback reacts to the
        press *edge* only; this also catches a switch that became active between the pre-check and the first step,
        or an edge that was missed. Returns (stop_event, thread) for `_end_z_motion`.
        """
        stop_event = threading.Event()

        def watch():
            while not stop_event.wait(ENDSTOP_POLL_S):
                if endstop.is_active:
                    log.warning("[Scanner] %s limit switch active during Z travel (watchdog); stopping.", label)
                    self._stop_z_once()
                    return

        thread = threading.Thread(target=watch, name=f"z-endstop-watch-{label.lower()}", daemon=True)
        thread.start()
        return stop_event, thread

    def _upload_worker(self, stop_event: threading.Event):
        """Background thread worker that uploads photos one by one."""
        while not stop_event.is_set():
            try:
                # Use a timeout to periodically check stop_event even if queue is empty
                file_path = self.upload_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if file_path is None or stop_event.is_set():
                self.upload_queue.task_done()
                break

            try:
                # determine the relative path from the root input_images folder
                # to preserve camera directory structure (e.g. "0/lvl_1_shot_0.jpg")
                path_obj = Path(file_path)
                images_dir = Path(__file__).resolve().parent.parent / "input_images"
                rel_path = path_obj.relative_to(images_dir)
                
                # Prefix with "rig/" so the downstream download code
                # (download_every_img_from_bucket with rig_mode=True) retrieves it correctly
                cloud_key = f"rig/{rel_path}"

                log.info("[UploadWorker] Uploading %s to R2 as %s ...", file_path, cloud_key)
                
                max_retries = 3
                delays = [3, 7, 15]

                for attempt in range(max_retries):
                    if self._cancel_event.is_set() or stop_event.is_set():
                        break

                    try:
                        success = r2.upload_file(file_path, object_name=cloud_key)
                        if success:
                            self._last_upload_time = time.time()
                            break
                        else:
                            raise RuntimeError("r2.upload_file returned False")
                    except Exception as e:
                        if attempt < max_retries - 1:
                            delay = delays[attempt]
                            log.warning("[UploadWorker] Network hiccup (%s). Retrying %s in %ds...", e, file_path, delay)
                            # Wait using stop_event for instant abort on cancel
                            if stop_event.wait(timeout=delay):
                                break # Thread killed mid-sleep
                        else:
                            log.error("[UploadWorker] Failed to upload %s after %d attempts: %s", file_path, max_retries, e)
                            self._upload_failed = True

            except Exception as e:
                log.error("[UploadWorker] Unexpected error processing %s: %s", file_path, e)
            finally:
                self.upload_queue.task_done()

    def start_upload_worker(self):
        """Starts the background worker thread."""
        if self._upload_thread is None or not self._upload_thread.is_alive():
            # Clear any stale items from previous runs
            while not self.upload_queue.empty():
                try: 
                    self.upload_queue.get_nowait()
                    self.upload_queue.task_done()
                except queue.Empty: 
                    break

            self._last_upload_time = time.time()
            self._upload_failed = False
            self._worker_stop_event = threading.Event()
            self._upload_thread = threading.Thread(target=self._upload_worker, args=(self._worker_stop_event,), daemon=True)
            self._upload_thread.start()

    def stop_upload_worker(self, wait_for_completion: bool = True, stall_timeout: float = 45.0):
        """Signals the worker to stop after remaining uploads finish, with a stalled-progress watchdog."""
        if self._upload_thread and self._upload_thread.is_alive():
            # Send the sentinel 'None' to signal the loop to terminate gracefully
            self.upload_queue.put(None)
            
            if not wait_for_completion:
                # Instantly abort the specific thread, bypassing the queue
                if hasattr(self, '_worker_stop_event'):
                    self._worker_stop_event.set()

            if wait_for_completion:
                # Poll the queue until all tasks are marked done
                while self.upload_queue.unfinished_tasks > 0:
                    if self._cancel_event.is_set():
                        log.warning("[UploadWorker] Scan cancelled, abandoning remaining queue.")
                        if hasattr(self, '_worker_stop_event'):
                            self._worker_stop_event.set()
                        break

                    if time.time() - self._last_upload_time > stall_timeout:
                        log.error("[UploadWorker] Uploads stalled for >%ds (network offline). Aborting.", stall_timeout)
                        with self._lock:
                            if self.state not in ScannerState.TERMINAL:
                                self.state = ScannerState.ERROR
                        if hasattr(self, '_worker_stop_event'):
                            self._worker_stop_event.set()
                        break
                        
                    time.sleep(1.0)
                
                # We give a short grace timeout for the thread to exit cleanly
                self._upload_thread.join(timeout=5.0)

            self._upload_thread = None

    def move_z_up(self, steps: int = 20, delay: float = 0.002, step_type: Optional[str] = None, acceleration: bool = True, start_delay: Optional[float] = 0.004, ramp_percent: float = 0.2, decel_percent: Optional[float] = None, end_delay: Optional[float] = None) -> bool:
        """Move Z-axis UP. Blocked if the top endstop is already active or hit mid-flight."""
        # Flag first, then check the endstop: a press between the two would otherwise be ignored by the callback.
        self._begin_z_motion()
        watcher = None
        try:
            if self.up_endstop.is_active:
                raise TopEndstopTriggered("Cannot move UP: Top limit switch is already reached!")

            watcher = self._start_endstop_watch(self.up_endstop, "TOP")
            success = self.z_axis_motor.rotate(
                clockwise=True,
                steps=steps,
                delay=delay,
                step_type=step_type,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                decel_percent=decel_percent,
                end_delay=end_delay
            )
        finally:
            self._end_z_motion(watcher)
        if not success and self.up_endstop.is_active:
            raise TopEndstopTriggered("Top limit switch hit during upward travel!")
        return success

    def move_z_down(self, steps: int = 20, delay: float = 0.002, step_type: Optional[str] = None, acceleration: bool = True, start_delay: Optional[float] = 0.004, ramp_percent: float = 0.2, decel_percent: Optional[float] = None, end_delay: Optional[float] = None) -> bool:
        """Move Z-axis DOWN. Blocked if the bottom endstop is already active or hit mid-flight."""
        # Flag first, then check the endstop: a press between the two would otherwise be ignored by the callback.
        self._begin_z_motion()
        watcher = None
        try:
            if self.down_endstop.is_active:
                raise BottomEndstopTriggered("Cannot move DOWN: Bottom (home) limit switch is already reached!")

            watcher = self._start_endstop_watch(self.down_endstop, "BOTTOM")
            success = self.z_axis_motor.rotate(
                clockwise=False,
                steps=steps,
                delay=delay,
                step_type=step_type,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                decel_percent=decel_percent,
                end_delay=end_delay
            )
        finally:
            self._end_z_motion(watcher)
        if not success and self.down_endstop.is_active:
            raise BottomEndstopTriggered("Bottom limit switch hit during downward travel!")
        return success
        
    def move_z_up_angle(self, angle: float = 18.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.angle_to_steps_conversion(angle=angle, step_type=step_type)
        return self.move_z_up(steps=steps, step_type=step_type, **kwargs)

    def move_z_down_angle(self, angle: float = 18.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.angle_to_steps_conversion(angle=angle, step_type=step_type)
        return self.move_z_down(steps=steps, step_type=step_type, **kwargs)

    def move_z_up_distance(self, distance: float = 100.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.distance_to_step_conversion(distance=distance, step_type=step_type)
        return self.move_z_up(steps=steps, step_type=step_type, **kwargs)

    def move_z_down_distance(self, distance: float = 100.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.distance_to_step_conversion(distance=distance, step_type=step_type)
        return self.move_z_down(steps=steps, step_type=step_type, **kwargs)

    def home_z_axis(self, delay: float = 0.0015, start_delay: float = 0.004, ramp_percent: float = 0.1):
        """
        The z-axis motor will rotate until it reaches the bottom (home) endstop.

        :raises ScannerError: if the bottom endstop was not reached (move aborted, serial error, or the
            50000-step travel limit ran out). Motor errors are propagated, never swallowed.
        """
        homed = False
        self.z_axis_motor.enable()
        try:
            self.move_z_down(
                steps=50000,
                delay=delay,
                acceleration=True,
                start_delay=start_delay,
                ramp_percent=ramp_percent
            )
        except BottomEndstopTriggered:
            homed = True
        finally:
            self.z_axis_motor.disable() # Automatically settles for 0.5s then cuts power

        # The move can also end on the switch without raising (it was released again, or the callback raced the check)
        if homed or self.down_endstop.is_active:
            log.info("[Scanner] Carriage successfully homed to bottom endstop.")
            return
        raise ScannerError("Homing failed: the bottom endstop was not reached.")

    def move_z_to_top(self, delay: float = 0.0015, start_delay: float = 0.004, ramp_percent: float = 0.1):
        """
        The z-axis motor will rotate UP until it reaches the top endstop.

        :raises ScannerError: if the top endstop was not reached (move aborted, serial error, or the
            50000-step travel limit ran out). Motor errors are propagated, never swallowed.
        """
        reached = False
        self.z_axis_motor.enable()
        try:
            self.move_z_up(
                steps=50000,
                delay=delay,
                acceleration=True,
                start_delay=start_delay,
                ramp_percent=ramp_percent
            )
        except TopEndstopTriggered:
            reached = True
        finally:
            self.z_axis_motor.disable() # Automatically settles for 0.5s then cuts power

        if reached or self.up_endstop.is_active:
            log.info("[Scanner] Carriage successfully reached top endstop.")
            return
        raise ScannerError("Move to top failed: the top endstop was not reached.")

    def wait_for_light_stable(
        self,
        samples: int = 4,
        tolerance: float = 0.03,
        timeout_s: float = 60.0,
        stable_time_s: float = 10.0,
    ) -> bool:
        """
        Monitors ExposureTime * AnalogueGain metadata to confirm LED thermal stabilization.
        """
        if not self.dual_cameras:
            return False

        cams = [cam for cam in self.dual_cameras.cameras if getattr(cam, "picam2", None)]

        if not cams or not getattr(cams[0].picam2, "started", False):
            return False

        log.info("[Light] Verifying optical light stabilization...")
        try:
            for cam in cams:
                for key in ("AeEnable", "AwbEnable", "ExposureTime", "AnalogueGain", "ColourGains"):
                    cam._pending_controls.pop(key, None)
                cam.picam2.set_controls({"AeEnable": True, "AwbEnable": True})
        except Exception as e:
            log.warning("[Light] Could not enable AE for light stabilization: %s", e)
            return False

        if self._cancel_event.wait(timeout=0.4):
            return False

        def measure() -> float:
            vals = []
            for _ in range(samples):
                if self._cancel_event.is_set():
                    return 0.0
                try:
                    md = cams[0].picam2.capture_metadata()
                    exp = md.get("ExposureTime")
                    gain = md.get("AnalogueGain")
                    if exp and gain and exp > 0 and gain > 0:
                        vals.append(float(exp) * float(gain))
                except Exception:
                    pass
                if self._cancel_event.wait(timeout=0.06):
                    return 0.0
            return statistics.median(vals) if vals else 0.0

        start = time.monotonic()
        anchor_val = measure()
        anchor_time = time.monotonic()

        while time.monotonic() - start < timeout_s:
            if self._cancel_event.wait(timeout=1.0):
                return False

            if not self.lights.is_on:
                log.warning("[Light] Illumination was turned off or lost during stabilization.")
                return False

            cur_val = measure()
            if anchor_val <= 0 or cur_val <= 0:
                anchor_val = cur_val
                anchor_time = time.monotonic()
                continue

            drift = abs(cur_val - anchor_val) / anchor_val
            log.info("[Light] Thermal Drift: %.2f%% relative to anchor", drift * 100)

            if drift >= tolerance:
                anchor_val = cur_val
                anchor_time = time.monotonic()
            else:
                if time.monotonic() - anchor_time >= stable_time_s:
                    log.info("[Light] Illumination stabilized.")
                    return True

        log.warning("[Light] Thermal stabilization window timed out.")
        return False

    def setup_and_stream(
        self,
        enable_stream: bool = True,
        bitrate: int = 4_000_000,
        camera_mode: Optional[str] = None,
        **kwargs
    ) -> list[str] | None:
        """
        Phase 1: Initializes cameras, gets initial stream image, and starts livestream.
        Returns the stream_urls immediately so the frontend can connect.
        """
        # Lock only for state validation and transition
        with self._lock:
            if self.state not in ScannerState.PREPARE_ALLOWED:
                raise RuntimeError(f"Cannot prepare. Scanner is currently {self.state}")
            self.state = ScannerState.PREPARING
            self._cancel_event.clear()
            self.stream_urls = None

        try:
            self.lights.on()

            # Determine camera driver mode
            selected_mode = (
                camera_mode
                or kwargs.pop("camera_mode", None)
                or kwargs.pop("mode", None)
                or self.camera_mode
            )
            if isinstance(selected_mode, str):
                selected_mode = selected_mode.lower()

            if selected_mode in ("rpi_sync", "sync", "dual_camera_rpi_sync"):
                CameraClass = DualCameraRPISync
            elif selected_mode in ("xvs", "dual_camera_xvs"):
                CameraClass = DualCameraXVS
            elif selected_mode in ("staggered", "dual_camera", "legacy"):
                CameraClass = DualCamera
            else:
                log.warning("[Scanner] Unrecognized camera mode '%s', falling back to DualCameraRPISync", selected_mode)
                CameraClass = DualCameraRPISync

            # retrieve cameras arguments
            cam1_id = kwargs.pop("camera_id1", kwargs.pop("camera_id_1", kwargs.pop("master_id", 0)))
            cam2_id = kwargs.pop("camera_id2", kwargs.pop("camera_id_2", kwargs.pop("slave_id", 1)))

            quality = kwargs.get("quality", DEFAULT_QUALITY)
            photo_resolution = kwargs.get("photo_resolution", DEFAULT_PHOTO_RESOLUTION)
            video_resolution = kwargs.pop("video_resolution", DEFAULT_VIDEO_RESOLUTION)
            preview_resolution = kwargs.get("preview_resolution", DEFAULT_PREVIEW_RESOLUTION)

            camera_kwargs = {
                "quality": quality,
                "size": photo_resolution,
                "video_size": video_resolution,
                "preview_size": preview_resolution,
            }

            self.dual_cameras = CameraClass(
                camera_id1=cam1_id,
                camera_id2=cam2_id,
                **camera_kwargs
            )

            # keep_running=True is required so the livestream can work!
            kwargs["keep_running"] = True

            stream_camera_settings = {
                "camera_mode": selected_mode,
                "driver": getattr(CameraClass, "__name__", None) or type(CameraClass).__name__,
                "camera_id1": cam1_id,
                "camera_id2": cam2_id,
                "photo_resolution": photo_resolution,
                "preview_resolution": preview_resolution,
                "video_resolution": video_resolution,
                "photo_quality": quality,
                "stream_quality": getattr(self.dual_cameras, "stream_quality", 80),
                "enable_stream": enable_stream,
                "bitrate": bitrate,
                **{k: v for k, v in kwargs.items() if k not in ("photo_resolution", "preview_resolution", "quality")},
            }
            log.info("[Scanner] Initializing camera stream & scan configuration: %s", stream_camera_settings)

            # prepare cameras for scanning (initial setup facing whatever direction)
            prepare_results = self.dual_cameras.prepare_scan(**kwargs)
            log.info("[Scanner] Applied camera settings from prepare_scan: %s", prepare_results)

            if enable_stream:
                self.stream_urls = self.generate_livestream(bitrate)

            return self.stream_urls

        except Exception as e:
            self.cleanup()
            raise ScannerError(f"Error in setup_and_stream: {str(e)}")

    def finish_preparation(self, **camera_kwargs):
        """
        Phase 2 & 3: Homes carriage, cleans R2, recalibrates cameras in front of the object.
        Designed to run as a background task.
        """
        try:
            # Pop off initialization arguments that would crash ArducamIMX477
            camera_kwargs.pop("camera_id1", None)
            camera_kwargs.pop("camera_id2", None)
            camera_kwargs.pop("camera_id_1", None)
            camera_kwargs.pop("camera_id_2", None)
            camera_kwargs.pop("master_id", None)
            camera_kwargs.pop("slave_id", None)
            camera_kwargs.pop("video_resolution", None)
            camera_kwargs.pop("camera_mode", None)
            camera_kwargs.pop("mode", None)

            # home the motor (a cancel during the move makes the homing report "not reached": not an error)
            try:
                self.home_z_axis()
            except ScannerError:
                if self._cancel_event.is_set():
                    return
                raise
            if self._cancel_event.is_set(): return

            if not self.move_z_up_distance(distance=30.0):
                if self._cancel_event.is_set():
                    return
                raise ScannerError("The Z-axis move to the scan start position (+30 mm) was aborted.")
            # The lead screw holds the carriage: do not leave the Z driver energized (1.8 A) through the light
            # stabilization and the wait for /scanner/start (the firmware energizes it by itself for every move).
            self.z_axis_motor.disable(interrupt=self._cancel_event)
            if self._cancel_event.is_set(): return

            # Clean up the cloud bucket before capturing new assets
            try:
                r2.delete_all_files_from_bucket()
            except Exception as e:
                raise ScannerError(f"Cloud sanitization failed: {e}")

            if self._cancel_event.is_set(): return

            # Reject scan if brightness is 0
            if self.lights.brightness <= 0:
                raise ScannerError("Brightness is 0%; cannot start a photogrammetry scan.")
            
            self.lights.lock_brightness()

            try:
                if not self.lights.is_on:
                    self.lights.on()

                if not self.lights.is_on:
                    raise ScannerError("Failed to energize LED illumination.")

                stable = self.wait_for_light_stable(timeout_s=90.0, stable_time_s=10.0, tolerance=0.03)
                
                if self._cancel_event.is_set():
                    self.lights.unlock_brightness()
                    return
                    
                if not stable:
                    raise ScannerError("Light thermal stabilization timed out.")

                if not self.lights.is_on:
                    raise ScannerError("Lights were turned off before camera lock.")

                # Recalibrate AE/AWB and focus in front of the illuminated object
                camera_kwargs["keep_running"] = True
                log.info("[Scanner] In-position camera recalibration settings (home + 30mm): %s", camera_kwargs)
                recalib_results = self.dual_cameras.prepare_scan(**camera_kwargs)
                log.info("[Scanner] Applied recalibration settings from prepare_scan: %s", recalib_results)
                
            except Exception:
                self.lights.unlock_brightness()
                self.lights.off()
                raise
            
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.PREPARED
                    self._level = 0
                    log.info("[Scanner] Preparation fully completed!")

        except Exception as e:
            log.error(f"Background preparation failed: {e}")
            self.cleanup()
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.ERROR

    def generate_livestream(self, bitrate: int = 4_000_000):
        """Starts or retrieves livestream URLs."""
        if self.state not in (ScannerState.PREPARING, ScannerState.PREPARED) or self.dual_cameras is None:
            return None

        if not self.is_streaming:
            try:
                self.stream_urls = self.dual_cameras.start_stream(bitrate=bitrate)
                return self.stream_urls
            except Exception as e:
                self.cleanup()
                raise ScannerError(f"generate_livestream error: {str(e)}")
        else:
            if not self.stream_urls:
                try:
                    self.stream_urls = self.dual_cameras.start_stream(bitrate=bitrate)
                except Exception:
                    pass
            return self.stream_urls

    def stop_stream(self):
        """Stops the live stream and releases the camera devices."""
        if self.dual_cameras:
            try:
                self.dual_cameras.stop_stream()
                self.dual_cameras.close()
            except Exception as e:
                log.warning("[Scanner] Error stopping stream: %s", e)
            finally:
                self.dual_cameras = None
                self.stream_urls = None

    def _capture_turntable_slice(
        self,
        images_dir: str,
        angle: float,
        steps: int,
        delay_turntable: float,
        turntable_acceleration: bool = True,
        turntable_start_delay: Optional[float] = None,
        turntable_ramp_percent: float = 0.2,
        turntable_decel_percent: Optional[float] = None,
        level_label: Optional[Union[int, str]] = None,
        check_tof: bool = True,
        turntable_end_delay: Optional[float] = TURNTABLE_END_DELAY_DEFAULT,
    ) -> bool:
        """
        Captures a 360-degree slice of photos by rotating the turntable in increments of `angle`.
        Returns True if an object was detected by the ToF sensor (or if check_tof is False).
        """
        if level_label is None:
            level_label = self._level

        total_shots = max(1, round(360.0 / angle))
        object_detected_this_level = False

        for shot in range(total_shots):
            if self._cancel_event.is_set():
                break

            # emergency_stop() closes the cameras and sets self.dual_cameras = None from another thread
            cameras = self.dual_cameras
            if cameras is None:
                if self._cancel_event.is_set():
                    break
                raise ScannerError("The camera system is not available.")

            # 1. Capture 2 photos using DualCamera / DualCameraXVS (Master & Slave)
            photo_paths = cameras.capture_photo(
                output_prefix=f"lvl_{level_label}_shot_{shot}",
                output_dir=images_dir,
                meshroom_rig=True
            )

            # Track total physical images for the RAM heuristic
            self.total_photos += len(photo_paths)

            # 2. Enqueue each photo path for background upload!
            for path in photo_paths:
                self.upload_queue.put(path)

            if self._cancel_event.is_set():
                break

            # 3. Rotate turntable to next position
            rotated = self.turntable_motor.rotate(
                clockwise=True,
                steps=steps,
                delay=delay_turntable,
                acceleration=turntable_acceleration,
                start_delay=turntable_start_delay,
                ramp_percent=turntable_ramp_percent,
                decel_percent=turntable_decel_percent,
                end_delay=turntable_end_delay,
                verbose=False
            )
            if not rotated:
                if self._cancel_event.is_set():
                    break
                # An aborted rotation means every following photo would be taken at a wrong angle
                raise ScannerError(
                    f"Turntable rotation aborted at level {level_label}, shot {shot}; scan positions are no longer valid."
                )

            # --- Settle Time & ToF Object Detection ---
            settle_start = time.monotonic()
            if check_tof and not object_detected_this_level:
                if self.tof.is_object_detected(threshold=TOF_BACKGROUND_DISTANCE_MM - TOF_CLEARANCE_MARGIN_MM):
                    object_detected_this_level = True
                    log.debug("Object detected by ToF sensor.")

            remaining_settle = TURNTABLE_SETTLE_S - (time.monotonic() - settle_start)
            if remaining_settle > 0:
                time.sleep(remaining_settle)

        return object_detected_this_level

    def scan(
        self, 
        angle: float = 18.0, 
        delay_turntable: float = 0.0010,
        z_move_mm: float = 100.0,
        delay_z_motor: float = 0.0010,
        turntable_acceleration: bool = True,
        turntable_start_delay: Optional[float] = None,
        turntable_ramp_percent: float = 0.2,
        turntable_decel_percent: Optional[float] = None,
        z_acceleration: bool = False,
        z_start_delay: Optional[float] = None,
        z_ramp_percent: float = 0.2,
        z_decel_percent: Optional[float] = None,
        turntable_end_delay: Optional[float] = TURNTABLE_END_DELAY_DEFAULT,
        z_end_delay: Optional[float] = None,
        finish_state: str = ScannerState.IDLE
    ):
        """
        Capture mode: Loop rotating turntable and moving z-axis while streaming.
        Stops when the top endstop is triggered, cancellation is requested,
        or the requested logic completes.

        :param angle: The turntable rotation angle per step in degrees.
        :param delay_turntable: Cruise step delay in seconds between pulses for turntable.
        :param z_move_mm: Vertical distance to elevate the Z-axis carriage between slices.
        :param delay_z_motor: Cruise step delay in seconds between pulses for Z-axis motor.
        :param turntable_acceleration: Whether to use velocity ramping on the turntable.
        :param turntable_start_delay: Starting delay in seconds for turntable ramp.
        :param turntable_ramp_percent: Fraction (0.0 - 1.0) of steps used for turntable acceleration.
        :param turntable_decel_percent: Optional separate fraction (0.0 - 1.0) for turntable deceleration.
        :param z_acceleration: Whether to use velocity ramping on the Z-axis.
        :param z_start_delay: Starting delay in seconds for Z-axis ramp.
        :param z_ramp_percent: Fraction (0.0 - 1.0) of steps used for Z-axis acceleration.
        :param z_decel_percent: Optional separate fraction (0.0 - 1.0) for Z-axis deceleration.
        :param turntable_end_delay: Step delay in seconds at the end of the turntable deceleration, so every
            stop ends slowly (clamped to [END_DELAY_MIN, END_DELAY_MAX]). None disables the slow tail.
        :param z_end_delay: Same as turntable_end_delay for the Z-axis (None = off, the default).
        :param finish_state: State to leave the scanner in after a scan that completed normally. cleanup() resets it
            to IDLE, which would briefly allow a new /prepare before the cloud pipeline starts; the orchestrator
            passes PROCESSING to close that gap. Failed and cancelled scans keep their own terminal state.
        """
        # Lock only for state validation, transition, and variable setup
        with self._lock:
            if self.state != ScannerState.PREPARED:
                raise RuntimeError("System must be prepared before scanning.")
            if self.dual_cameras is None:
                raise ScannerError("Dual Camera System is not initialized. Run prepare_scan() before scan().")

            # Validate the mechanical parameters BEFORE touching any state or hardware: a bad angle or z_move_mm
            # must leave the scanner PREPARED, not half-started (energized motors, running upload worker).
            steps = self.turntable_motor.angle_to_steps_conversion(angle)
            z_steps = self.z_axis_motor.distance_to_step_conversion(distance=z_move_mm)

            # Reset cancellation flag and photo counter for a fresh scan
            self._cancel_event.clear()
            self.total_photos = 0

            self.state = ScannerState.RUNNING

        # The slow-stop tail is part of the deceleration ramp: without acceleration there is nothing to apply it to
        if not turntable_acceleration:
            turntable_end_delay = None
        if not z_acceleration:
            z_end_delay = None
        scan_ok = False

        # Lock released - physical execution begins without blocking emergency_stop()
        images_dir = str(Path(__file__).resolve().parent.parent / "input_images")

        try:
            # Everything that touches the hardware lives inside the try, so cleanup() always runs
            self.turntable_motor.enable()
            self.z_axis_motor.disable() # Z-axis is idle while taking photos on the first layer

            fc.create_clean_dir(images_dir)

            self.start_upload_worker()

            self._level = 1
            
            # Until the top endstop triggers or cancellation is requested
            while not self.up_endstop.is_active:
                # Check for cancellation before starting a new Z-level slice
                if self._cancel_event.is_set():
                    log.warning("[Scanner] Scan aborted by user.")
                    break

                object_detected_this_level = self._capture_turntable_slice(
                    images_dir=images_dir,
                    angle=angle,
                    steps=steps,
                    delay_turntable=delay_turntable,
                    turntable_acceleration=turntable_acceleration,
                    turntable_start_delay=turntable_start_delay,
                    turntable_ramp_percent=turntable_ramp_percent,
                    turntable_decel_percent=turntable_decel_percent,
                    level_label=self._level,
                    check_tof=True,
                    turntable_end_delay=turntable_end_delay,
                )

                # Exit outer loop if cancelled during the inner loop
                if self._cancel_event.is_set():
                    break

                # --- End of Level Evaluation ---
                if not object_detected_this_level:
                    log.info(f"ToF Sensor cleared the object at level {self._level}. Terminating scan early.")
                    self.turntable_motor.disable()
                    break

                # If endstop was triggered during the slice, exit loop
                if self.up_endstop.is_active:
                    self.turntable_motor.disable()
                    break

                try:
                    # The settle pauses are interruptible and the cancel flag is re-checked before every step:
                    # a cancel that lands during a pause must not still lift the carriage.
                    self.turntable_motor.disable(interrupt=self._cancel_event)  # Holding torque settles for 0.5s then cuts power
                    if self._cancel_event.is_set():
                        break
                    self.z_axis_motor.enable()      # Energize Z-axis
                    if self._cancel_event.is_set():
                        break
                    z_moved = self.move_z_up(
                        steps=z_steps,
                        delay=delay_z_motor,
                        acceleration=z_acceleration,
                        start_delay=z_start_delay,
                        ramp_percent=z_ramp_percent,
                        decel_percent=z_decel_percent,
                        end_delay=z_end_delay
                    )
                    self.z_axis_motor.disable(interrupt=self._cancel_event)     # Done climbing, settles for 0.5s then cuts power
                    if self._cancel_event.is_set():
                        break
                    if not z_moved:
                        raise ScannerError("The Z-axis climb was aborted before reaching the next level.")
                    if self.up_endstop.is_active:
                        # The climb ended exactly on the top switch: rotate() reported success, but the carriage is at
                        # the ceiling. Take the final ring instead of silently leaving the loop without it.
                        raise TopEndstopTriggered("Top limit switch reached at the end of the climb.")
                    self.turntable_motor.enable()   # Re-enable turntable for next slice
                except TopEndstopTriggered:
                    log.info("[Scanner] Carriage reached the TOP ceiling during climb. Performing final top-level photo ring...")
                    self.z_axis_motor.disable()
                    self.turntable_motor.enable()
                    self._level += 1
                    self._capture_turntable_slice(
                        images_dir=images_dir,
                        angle=angle,
                        steps=steps,
                        delay_turntable=delay_turntable,
                        turntable_acceleration=turntable_acceleration,
                        turntable_start_delay=turntable_start_delay,
                        turntable_ramp_percent=turntable_ramp_percent,
                        turntable_decel_percent=turntable_decel_percent,
                        level_label=self._level,
                        check_tof=False,
                        turntable_end_delay=turntable_end_delay,
                    )
                    self.turntable_motor.disable()
                    break
                
                self._level += 1

            scan_ok = True

        except Exception as e:
            if self._cancel_event.is_set():
                # The cancel tore the hardware down (cameras closed, motors stopped) under the scan thread: the
                # resulting error is a consequence, not a failure.
                log.info("[Scanner] Scan stopped by cancellation (%s).", e)
            else:
                raise ScannerError(f"Error during scan: {str(e)}")
        finally:
            self.cleanup()
            if scan_ok and finish_state != ScannerState.IDLE and not self._cancel_event.is_set():
                with self._lock:
                    if self.state == ScannerState.IDLE:  # cleanup() just reset RUNNING -> IDLE
                        self.state = finish_state
            # Tell the worker that no more images are coming
            # Don't wait for uploads if the user cancelled
            wait = not self._cancel_event.is_set()
            self.stop_upload_worker(wait_for_completion=wait)

    def emergency_stop(self):
        """Instantly stops all hardware and aborts ongoing processes."""
        log.warning("[Scanner] EMERGENCY STOP TRIGGERED!")
        self._cancel_event.set()
        
        # Instantly interrupt any running motor loops
        self.turntable_motor.stop(release_torque=True)
        self.z_axis_motor.stop(release_torque=True)
        
        # Cut lights
        self.lights.unlock_brightness()
        self.lights.off()
        
        # Shut down cameras if running
        if self.dual_cameras:
            self.dual_cameras.stop_stream()
            self.dual_cameras.close()
            self.dual_cameras = None
            self.stream_urls = None

        # Lock only for updating the state securely
        with self._lock:
            self.state = ScannerState.CANCELLED

    def cleanup(self):
        """
        Safely powers down all hardware (motors, lights, cameras).
        
        Does NOT overwrite terminal states (CANCELLED, ERROR, COMPLETED) back to IDLE,
        so the orchestrator and API can correctly observe the final outcome.
        """
        # Only reset to IDLE if we're not already in a terminal state
        if self.state not in ScannerState.TERMINAL:
            self.state = ScannerState.IDLE

        self.turntable_motor.stop(release_torque=True)
        self.z_axis_motor.stop(release_torque=True)
        self.lights.unlock_brightness()
        self.lights.off()

        self._level = 0
        self.stream_urls = None

        if self.dual_cameras:
            self.dual_cameras.stop_stream()
            self.dual_cameras.close()
            self.dual_cameras = None

    def close_hardware(self) -> None:
        """
        Explicitly closes all hardware components, releasing GPIO pins and I2C bus.
        """
        log.info("[Scanner] Releasing all GPIO and hardware resources...")
        components = (
            ("turntable_motor", getattr(self, "turntable_motor", None)),
            ("z_axis_motor", getattr(self, "z_axis_motor", None)),
            ("up_endstop", getattr(self, "up_endstop", None)),
            ("down_endstop", getattr(self, "down_endstop", None)),
            ("lights", getattr(self, "lights", None)),
            ("tof", getattr(self, "tof", None)),
        )
        for name, component in components:
            if component and hasattr(component, "close"):
                try:
                    component.close()
                except Exception as e:
                    log.warning(f"[Scanner] Error closing {name}: {e}")

        # Ensure the ArduinoBridge is explicitly closed (sends 0xFF and releases serial port)
        if getattr(self, "turntable_motor", None) is not None:
            bridge = getattr(self.turntable_motor, "bridge", None)
            if bridge and hasattr(bridge, "close"):
                try:
                    bridge.close()
                except Exception as e:
                    log.warning(f"[Scanner] Error closing ArduinoBridge: {e}")

