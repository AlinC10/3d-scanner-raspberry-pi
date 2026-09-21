import os
from pathlib import Path
from typing import Optional
import threading
import time
from queue import Queue

from camera.config import DEFAULT_PHOTO_RESOLUTION, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_QUALITY, \
    DEFAULT_VIDEO_RESOLUTION
from dual_camera import DualCamera
from dual_camera_xvs import DualCameraXVS
from endstop import Endstop, TopEndstopTriggered, BottomEndstopTriggered
from motor import Motor
from relay import Relay
import cloud.cloudflare_r2 as r2


class ScannerError(Exception):
    pass

class ScannerState:
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"

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

        self._level = 0

        self.upload_queue = Queue()
        self._upload_thread: Optional[threading.Thread] = None


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
        with self._lock:
            if self.state != ScannerState.IDLE:
                raise RuntimeError(f"Cannot prepare. Scanner is currently {self.state}")
            self.state = ScannerState.PREPARING

            self.lights.on()

            try:
                self.lights.on()

                # home the motor
                self.home_z_axis()

                # retrieve cameras arguments
                master_id = kwargs.pop("master_id", kwargs.get("camera_id_1", 0))
                slave_id = kwargs.pop("slave_id", kwargs.get("camera_id_2", 1))

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

                if enable_stream:
                    self.generate_livestream(bitrate)

                self.state = ScannerState.PREPARED
                self._level = 0

            except Exception as e:
                self.cleanup()
                raise ScannerError(f"Error in prepare_scan: {str(e)}")

    def generate_livestream(self, bitrate: int = 2_000_000):
        """"""
        if self.state not in ("prepared", "scanning") or self.dual_cameras is None:
            return

        if not self.is_streaming:
            try:
                self.dual_cameras.start_stream(bitrate=bitrate)
            except Exception as e:
                self.cleanup()
                raise ScannerError(f"generate_livestream error: {str(e)}")

    def scan(
        self, 
        angle: float = 18.0, 
        step_type: str = "Full", 
        z_move_mm: float = 10.0, 
        z_step_type: str = "Full"
    ):
        """
        Capture mode: Loop rotating turntable and moving z-axis while streaming.
        Stops when the top endstop is triggered, or the requested logic completes.
        """
        with self._lock:
            if self.state != ScannerState.PREPARED:
                raise RuntimeError("System must be prepared before scanning.")

            self.state = ScannerState.RUNNING
            self.turntable_motor.enable()
            self.z_axis_motor.enable()
            
            library_dir = str(Path(__file__).resolve().parent.parent.joinpath("library"))
            os.makedirs(library_dir, exist_ok=True)

            try:
                self._level = 1
                
                # Until the top endstop triggers
                while not self.up_endstop.is_active:
                    total_shots = max(1, round(360.0 / angle))
                    
                    for shot in range(total_shots):
                        # 1. Capture 2 photos using DualCamera / DualCameraXVS (Master & Slave)
                        if self.dual_cameras:
                            self.dual_cameras.capture_photo(
                                output_prefix=f"lvl_{self._level}_shot_{shot}",
                                output_dir=library_dir,
                                meshroom_rig=self.xvs
                            )

                        # 2. Rotate turntable to next position
                        self.turntable_motor.rotate_angle(
                            clockwise=True,
                            angle=angle,
                            delay=0.002,
                            step_type=step_type,
                            verbose=False
                        )
                        time.sleep(0.5)  # Let object settle so the next photo isn't blurry

                    # If endstop was triggered during the slice, exit loop
                    if self.up_endstop.is_active:
                        break
                        
                    # 3. Move Z-axis to the next floor
                    degrees_to_rotate = (z_move_mm / T8_THREADED_ROD_STEP) * 360.0
                    try:
                        self.move_z_up_angle(
                            angle=degrees_to_rotate,
                            delay=0.001,
                            step_type=z_step_type
                        )
                    except TopEndstopTriggered:
                        # Reached the ceiling, gracefully break out of the scan loop
                        break
                    
                    self._level += 1

            except Exception as e:
                raise ScannerError(f"Error during scan: {str(e)}")
            finally:
                self.cleanup()

    def cleanup(self):
        """Forces the system safely back to an idle state."""
        self.state = ScannerState.IDLE
        self.turntable_motor.stop(release_torque=True)
        self.z_axis_motor.stop(release_torque=True)
        self.lights.off()

        self._level = 0

        if self.dual_cameras:
            self.dual_cameras.stop_stream()
            self.dual_cameras.close()
            self.dual_cameras = None
