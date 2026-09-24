import logging
import os
from pathlib import Path
from typing import Optional
import threading
import time
from queue import Queue
import queue

from camera.config import DEFAULT_PHOTO_RESOLUTION, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_QUALITY, \
    DEFAULT_VIDEO_RESOLUTION
from dual_camera import DualCamera
from dual_camera_xvs import DualCameraXVS
from endstop import Endstop, TopEndstopTriggered, BottomEndstopTriggered
from motor import Motor
from relay import Relay
import cloud.cloudflare_r2 as r2
from system import file_control as fc

log = logging.getLogger(__name__)

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
    def __init__(self, xvs: bool = True):
        self.turntable_motor = Motor(dir_pin=24, step_pin=23, mode_pins=(25, 8, 7), en_pin=18, flt_pin=4)
        self.z_axis_motor = Motor(dir_pin=19, step_pin=26, mode_pins=(13, 6, 5), en_pin=21, flt_pin=12)

        self.up_endstop = Endstop(pin=2, pull_up=True, active_state=False, bounce_time=0.02)
        self.down_endstop = Endstop(pin=3, pull_up=True, active_state=False, bounce_time=0.02)

        # Wire up safety interrupts: Any endstop hit immediately stops the Z-axis motor mid-loop
        self.up_endstop.when_pressed = self.z_axis_motor.stop
        self.down_endstop.when_pressed = self.z_axis_motor.stop

        self.lights = Relay(pin=11, active_high=True, initial_value=False)

        self.dual_cameras: Optional[DualCamera | DualCameraXVS] = None

        self.xvs = xvs

        self.state = ScannerState.IDLE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

        self._level = 0
        self.total_photos = 0

        # used for uploading the images when they are captured
        self.upload_queue = Queue()
        self._upload_thread: Optional[threading.Thread] = None
        self._last_upload_time = time.time()
        self._upload_failed = False


    @property
    def is_streaming(self):
        if isinstance(self.dual_cameras, DualCamera):
            return getattr(self.dual_cameras.cam1.picam2, "recording", False)
        elif isinstance(self.dual_cameras, DualCameraXVS):
            return getattr(self.dual_cameras.master.picam2, "recording", False)
        else:
            return False

    @property
    def level(self):
        return self._level

    def _upload_worker(self):
        """Background thread worker that uploads photos one by one."""
        while True:
            file_path = self.upload_queue.get()

            if file_path is None:
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
                    if self._cancel_event.is_set():
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
                            # Wait using _cancel_event for instant abort on cancel
                            if self._cancel_event.wait(timeout=delay):
                                break # User cancelled mid-sleep
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
            self._upload_thread = threading.Thread(target=self._upload_worker, daemon=True)
            self._upload_thread.start()

    def stop_upload_worker(self, wait_for_completion: bool = True, stall_timeout: float = 45.0):
        """Signals the worker to stop after remaining uploads finish, with a stalled-progress watchdog."""
        if self._upload_thread and self._upload_thread.is_alive():
            # Send the sentinel 'None' to signal the loop to terminate
            self.upload_queue.put(None)

            if wait_for_completion:
                # Poll the queue until all tasks are marked done
                while self.upload_queue.unfinished_tasks > 0:
                    if self._cancel_event.is_set():
                        log.warning("[UploadWorker] Scan cancelled, abandoning remaining queue.")
                        break

                    if time.time() - self._last_upload_time > stall_timeout:
                        log.error("[UploadWorker] Uploads stalled for >%ds (network offline). Aborting.", stall_timeout)
                        with self._lock:
                            if self.state not in ScannerState._TERMINAL:
                                self.state = ScannerState.ERROR
                        break
                        
                    time.sleep(1.0)
                
                # We give a short grace timeout for the thread to exit cleanly
                self._upload_thread.join(timeout=5.0)

            self._upload_thread = None

    def move_z_up(self, steps: int = 20, delay: float = 0.001, step_type: str = "Full"):
        """Move Z-axis UP. Blocked if the top endstop is already active."""
        if self.up_endstop.is_active:
            raise TopEndstopTriggered("Cannot move UP: Top limit switch is already reached!")

        self.z_axis_motor.rotate(clockwise=True, steps=steps, delay=delay, step_type=step_type)

    def move_z_down(self, steps: int = 20, delay: float = 0.001, step_type: str = "Full"):
        """Move Z-axis DOWN. Blocked if the bottom endstop is already active."""
        if self.down_endstop.is_active:
            raise BottomEndstopTriggered("Cannot move DOWN: Bottom (home) limit switch is already reached!")

        self.z_axis_motor.rotate(clockwise=False, steps=steps, delay=delay, step_type=step_type)
        
    def move_z_up_angle(self, angle: float, delay: float = 0.001, step_type: str = "Full"):
        if self.up_endstop.is_active:
            raise TopEndstopTriggered("Cannot move UP: Top limit switch is already reached!")
            
        self.z_axis_motor.rotate_angle(clockwise=True, angle=angle, delay=delay, step_type=step_type)

    def move_z_down_angle(self, angle: float, delay: float = 0.001, step_type: str = "Full"):
        if self.down_endstop.is_active:
            raise BottomEndstopTriggered("Cannot move DOWN: Bottom (home) limit switch is already reached!")
            
        self.z_axis_motor.rotate_angle(clockwise=False, angle=angle, delay=delay, step_type=step_type)

    def home_z_axis(self):
        """The z-axis motor will rotate until it will reach the bottom (home) endstop."""
        self.z_axis_motor.enable()

        try:
            while True:
                self.move_z_down(steps=20, delay=0.0005)
        except BottomEndstopTriggered:
            pass
        except Exception as e:
            raise ScannerError(f"Motor Error in home_z_axis: {str(e)}")
        finally:
            self.z_axis_motor.stop()
            self.z_axis_motor.disable()


    def prepare_scan(self, enable_stream: bool = True, bitrate: int = 2_000_000, **kwargs):
        """
        Prepare mode: Homing, Lights On, Cameras initialized and focused (Live Stream Ready).
         Function prepare_scan parameters
            # photo_resolution: tuple[int, int] | None = None,
            # preview_resolution: tuple[int, int] | None = None,
            # quality: int | None = None,
            # focus: int | str | None = None,
            # rotation: int | None = None,
            # show_preview: bool = False,
            # settle_time: float = 2.0,
            # exposure_time: int | None = None,
            # analogue_gain: float | None = None,
            # colour_gains: tuple[float, float] | None = None,
            # awb_mode: str | None = "auto",
            # brightness: float | None = None,
            # contrast: float | None = None,
            # saturation: float | None = None,
            # sharpness: float | None = None,
            # autofocus_step: int = AF_STEP,
            # autofocus_roi: tuple = AF_ROI,
            # keep_running: bool = False
        """
        # Lock only for state validation and transition
        with self._lock:
            if self.state not in ScannerState.PREPARE_ALLOWED:
                raise RuntimeError(f"Cannot prepare. Scanner is currently {self.state}")
            self.state = ScannerState.PREPARING
            self._cancel_event.clear()

        # Lock released — safe for emergency_stop() to intervene from another thread
        try:
            self.lights.on()

            # home the motor
            self.home_z_axis()

            # Clean up the cloud bucket before capturing new assets
            try:
                r2.delete_all_files_from_bucket()
            except Exception as e:
                raise ScannerError(f"Cloud sanitization failed: {e}")

            # retrieve cameras arguments
            master_id = kwargs.pop("master_id", kwargs.pop("camera_id_1", 0))
            slave_id = kwargs.pop("slave_id", kwargs.pop("camera_id_2", 1))

            quality = kwargs.get("quality", DEFAULT_QUALITY)
            photo_resolution = kwargs.get("size", DEFAULT_PHOTO_RESOLUTION)
            video_resolution = kwargs.get("video_size", DEFAULT_VIDEO_RESOLUTION)
            preview_resolution = kwargs.get("preview_size", DEFAULT_PREVIEW_RESOLUTION)
            rotation = kwargs.get("rotation", 0)

            camera_kwargs = {
                "quality": quality,
                "size": photo_resolution,
                "video_size": video_resolution,
                "preview_size": preview_resolution,
                "rotation": rotation
            }

            if self.xvs:
                self.dual_cameras = DualCameraXVS(
                    master_id=master_id,
                    slave_id=slave_id,
                    **camera_kwargs
                )
            else:
                self.dual_cameras = DualCamera(
                    camera_id_1=master_id,
                    camera_id_2=slave_id,
                    **camera_kwargs
                )

            # keep_running=True is required so the livestream can work!
            kwargs["keep_running"] = True

            # prepare cameras for scanning
            self.dual_cameras.prepare_scan(**kwargs)

            url = None
            if enable_stream:
                # return URL to the streams
                url = self.generate_livestream(bitrate)

            self.state = ScannerState.PREPARED
            self._level = 0
            return url

        except Exception as e:
            self.cleanup()
            raise ScannerError(f"Error in prepare_scan: {str(e)}")

    def generate_livestream(self, bitrate: int = 2_000_000):
        """"""
        if self.state not in ("prepared", "scanning") or self.dual_cameras is None:
            return

        if not self.is_streaming:
            try:
                # return URL to the streams
                return self.dual_cameras.start_stream(bitrate=bitrate)
            except Exception as e:
                self.cleanup()
                raise ScannerError(f"generate_livestream error: {str(e)}")

    def scan(
        self, 
        angle: float = 18.0, 
        step_type: str = "Full",
        delay_turntable: float = 0.0005,
        z_move_mm: float = 100.0,
        z_step_type: str = "Full",
        delay_z_motor: float = 0.0005
    ):
        """
        Capture mode: Loop rotating turntable and moving z-axis while streaming.
        Stops when the top endstop is triggered, cancellation is requested,
        or the requested logic completes.
        """
        # Lock only for state validation, transition, and variable setup
        with self._lock:
            if self.state != ScannerState.PREPARED:
                raise RuntimeError("System must be prepared before scanning.")

            # Reset cancellation flag and photo counter for a fresh scan
            self._cancel_event.clear()
            self.total_photos = 0

            steps = self.turntable_motor.angle_to_steps_conversion(angle, step_type)

            if not isinstance(steps, int):
                raise ValueError(f"""Angle should be a multiple of 1.8 * step_type to obtain an integer number for 
                steps. Current angles and number of steps: {angle} degrees -> {steps} steps""")

            self.state = ScannerState.RUNNING

        # Lock released — physical execution begins without blocking emergency_stop()
        self.turntable_motor.enable()
        self.z_axis_motor.enable()
        
        images_dir = str(Path(__file__).resolve().parent.parent / "input_images")

        fc.create_clean_dir(images_dir)

        self.start_upload_worker()

        # move z-axis to the next floor
        degrees_to_rotate = (z_move_mm / T8_THREADED_ROD_STEP) * 360.0
        try:
            self._level = 1
            
            # Until the top endstop triggers or cancellation is requested
            while not self.up_endstop.is_active:
                # Check for cancellation before starting a new Z-level slice
                if self._cancel_event.is_set():
                    log.warning("[Scanner] Scan aborted by user.")
                    break

                total_shots = max(1, round(360.0 / angle))
                
                for shot in range(total_shots):
                    # Check for cancellation before each photo
                    if self._cancel_event.is_set():
                        break

                    # 1. Capture 2 photos using DualCamera / DualCameraXVS (Master & Slave)
                    if self.dual_cameras:
                        photo_paths = self.dual_cameras.capture_photo(
                            output_prefix=f"lvl_{self._level}_shot_{shot}",
                            output_dir=images_dir,
                            meshroom_rig=True
                        )

                        # Track total physical images for the RAM heuristic
                        self.total_photos += len(photo_paths)

                    # 2. Enqueue each photo path for background upload!
                    for path in photo_paths:
                        self.upload_queue.put(path)

                    # Check for cancellation before rotating
                    if self._cancel_event.is_set():
                        break

                    # 3. Rotate turntable to next position
                    self.turntable_motor.rotate(
                        clockwise=True,
                        steps=steps,
                        delay=delay_turntable,
                        step_type=step_type,
                        verbose=False
                    )
                    time.sleep(0.2)  # Let object settle so the next photo isn't blurry

                # Exit outer loop if cancelled during the inner loop
                if self._cancel_event.is_set():
                    break

                # If endstop was triggered during the slice, exit loop
                if self.up_endstop.is_active:
                    break

                try:
                    self.move_z_up_angle(
                        angle=degrees_to_rotate,
                        delay=delay_z_motor,
                        step_type=z_step_type
                    )
                except TopEndstopTriggered:
                    # Reached the ceiling, break out of the scan loop
                    break
                
                self._level += 1

        except Exception as e:
            raise ScannerError(f"Error during scan: {str(e)}")
        finally:
            self.cleanup()
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
        self.lights.off()
        
        # Shut down cameras if running
        if self.dual_cameras:
            self.dual_cameras.stop_stream()
            self.dual_cameras.close()
            self.dual_cameras = None

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
        self.lights.off()

        self._level = 0

        if self.dual_cameras:
            self.dual_cameras.stop_stream()
            self.dual_cameras.close()
            self.dual_cameras = None
