"""
A simulated rig for development on a PC without the hardware (MOCK_HARDWARE=1, see main_dev.py).

`ScannerMock` offers the members of `hardware.scanner.Scanner` that the routers use (`router/scanner.py`,
`router/test.py`), with the same states, the same status data and the same errors, but nothing touches a motor, a
camera, the cloud or the disk (apart from the placeholder photos of POST /test/camera/snap). Motion is simulated: the
carriage has a height (mm) that moves with the Z commands and drives the two endstops; the ToF sensor reads a short
distance while an "object" is in front of it and a long one above it.

It is deliberately simple: it follows the real scanner's API, not its timing or its failure modes.
"""
import contextlib
import base64
import io
import logging
import random
import threading
import time
from pathlib import Path
from typing import Optional, Any, Dict

from hardware.lights import LightLockedError
from hardware.motor import TURNTABLE_STEP_TYPE, Z_STEP_TYPE, angle_to_steps, distance_to_steps
# The real states and phases: the routers compare against them, so the mock must not have its own
from hardware.scanner import (
    ScannerState, ScanPhase, ScanMode, ScannerModeError, ScannerError, ScannerBusyError, ScannerStoppingError,
    ScannerUploadError,
    ArduinoUnavailableError, ARDUINO_UNAVAILABLE_MESSAGE,
    Z_TRAVEL_MAX_MM, Z_START_DEFAULT_MM, INPUT_IMAGES_DIR, UPLOAD_DRAIN_TIMEOUT_S,
)

log = logging.getLogger(__name__)

__all__ = ["ScannerMock", "ScannerState", "ScanPhase", "ScannerUploadError"]

# A 64x48 grey JPEG, used when Pillow is not installed
_FALLBACK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1x"
    "eXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAAR"
    "CAAwAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEG"
    "E1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
    "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEB"
    "AQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
    "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0"
    "tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCpRRRWxiFFFFABRRRQAUUUUAFFFFABRRRQ"
    "AUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB//Z"
)

# Simulated geometry: the "object" is as tall as this, and the ToF sees its surface or the backdrop
_OBJECT_HEIGHT_MM = 220.0
_TOF_OBJECT_MM = 300.0
_TOF_BACKDROP_MM = 520.0
_TOF_THRESHOLD_MM = 350.0   # same meaning as TOF_BACKGROUND_DISTANCE_MM in hardware/scanner.py
_SIM_STEP_S = 0.05          # how long one simulated shot or short motion takes


def _placeholder_jpeg(label: str) -> bytes:
    """A small JPEG with a moving label, so a browser tab shows that the stream is alive."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return _FALLBACK_JPEG
    image = Image.new("RGB", (640, 480), (40, 60, 80))
    ImageDraw.Draw(image).text((20, 20), label, fill=(240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=60)
    return buffer.getvalue()


class _FakeEndstop:
    def __init__(self):
        self.is_active = False
        self.when_pressed = None

    def close(self):
        pass


class _FakeMotor:
    """Accepts every command; remembers whether the coils are energized."""

    def __init__(self, step_type: str):
        self.step_type = step_type
        self.is_enabled = False
        self.bridge = None

    def enable(self):
        self.is_enabled = True

    def disable(self, settle_delay: float = 0.0, interrupt=None, **kwargs):
        self.is_enabled = False

    def stop(self, repeat: int = 1):
        pass

    def angle_to_steps_conversion(self, angle, step_type: Optional[str] = None) -> int:
        return angle_to_steps(angle, step_type or self.step_type)

    def distance_to_step_conversion(self, distance: float = 100.0, step_type: Optional[str] = None) -> int:
        return distance_to_steps(distance, step_type or self.step_type)

    def rotate(self, clockwise: bool = True, steps: int = 0, delay: float = 0.001, **kwargs) -> bool:
        time.sleep(min(_SIM_STEP_S, max(0.0, steps * delay)))
        return True

    def rotate_angle(self, clockwise: bool = True, angle=18, delay: float = 0.001, **kwargs) -> bool:
        return self.rotate(clockwise=clockwise, steps=self.angle_to_steps_conversion(angle), delay=delay)

    def close(self):
        self.is_enabled = False


class _FakeLights:
    def __init__(self):
        self.brightness = 1.0
        self.locked = False
        self._is_on = False

    @property
    def is_on(self) -> bool:
        return self._is_on

    is_active = is_on

    def on(self):
        if self.brightness <= 0.0:
            self.brightness = 1.0
        self._is_on = True

    def off(self, force: bool = True):
        if self.locked and not force:
            raise LightLockedError("Lights are locked (scan in progress).")
        self._is_on = False
        self.locked = False

    def toggle(self):
        if self.locked:
            raise LightLockedError("Brightness is locked (scan in progress).")
        self.off() if self._is_on else self.on()

    def set_brightness(self, value: float):
        if self.locked:
            raise LightLockedError("Brightness is locked (scan in progress).")
        self.brightness = max(0.0, min(1.0, float(value)))

    def lock_brightness(self):
        self.locked = True

    def unlock_brightness(self):
        self.locked = False

    def close(self):
        self._is_on = False


class _FakeToF:
    available = True

    def __init__(self, scanner: "ScannerMock"):
        self._scanner = scanner

    def read_distance_mm(self) -> Optional[float]:
        base = _TOF_OBJECT_MM if self._scanner.z_mm < _OBJECT_HEIGHT_MM else _TOF_BACKDROP_MM
        return round(base + random.uniform(-3.0, 3.0), 1)

    def probe_object(self, threshold: float = _TOF_THRESHOLD_MM, samples: int = 2) -> Optional[bool]:
        distance = self.read_distance_mm()
        return distance is not None and distance < threshold

    def close(self):
        pass


class _FakeCameras:
    """Stands for the dual camera driver: a preview that serves placeholder JPEGs and photos that are placeholders."""
    supports_preview_stream = True
    supports_lock_auto = True
    supported_photo_sizes = None

    def __init__(self):
        self._active = 0
        self._spectators = [0, 0]
        self._quality = 80
        self._frame = 0
        self.last_capture_info = None
        self.autofocus_positions = [512, 512]

    # preview
    @property
    def active(self) -> int:
        return self._active

    @property
    def active_camera(self) -> int:
        return self._active + 1

    @active_camera.setter
    def active_camera(self, camera_id: int):
        self._active = 0 if camera_id == 1 else 1

    @property
    def stream_quality(self) -> int:
        return self._quality

    @stream_quality.setter
    def stream_quality(self, value: int):
        self._quality = max(1, min(100, int(value)))

    def add_spectator(self, cam_idx: int):
        self._spectators[cam_idx] += 1

    def remove_spectator(self, cam_idx: int):
        self._spectators[cam_idx] = max(0, self._spectators[cam_idx] - 1)

    def wait_jpeg(self, cam_idx: int, last_id: int = -1, timeout: float = 1.0):
        time.sleep(0.1)  # ~10 fps
        self._frame += 1
        return self._frame, _placeholder_jpeg(f"MOCK camera {cam_idx + 1}  frame {self._frame}")

    def get_sync_status(self) -> dict:
        controls = {"exposure_time": 10000, "analogue_gain": 1.5, "colour_gains": [1.8, 1.6], "sensor_timestamp": 0}
        return {
            "ready": True, "cam1_ready": True, "cam2_ready": True,
            "cam1_seq": self._frame, "cam2_seq": self._frame, "active_camera": self.active_camera,
            "offset_ms": 0.03, "drops": [0, 0], "stream_fps": 10.0, "last_jpeg_kb": 12.0,
            "quality": 95, "stream_quality": self._quality, "spectators": list(self._spectators),
            "cam1_controls": dict(controls), "cam2_controls": dict(controls),
        }

    # scan
    def prepare_scan(self, *, lock_auto: bool = False, copy_colour_gains: bool = False, **kwargs):
        return [{"camera_id": i, "ready": True, "locked": lock_auto, "focus": 512} for i in (0, 1)]

    def capture_photo(self, output_prefix="dual_photo", output_dir=".", meshroom_rig=True, **kwargs):
        paths = []
        for cam in (1, 2):
            path = Path(output_dir) / (f"{cam - 1}/{output_prefix}.jpg" if meshroom_rig else f"{output_prefix}_cam{cam}.jpg")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_placeholder_jpeg(f"MOCK photo cam{cam} {output_prefix}"))
            paths.append(str(path))
        self.last_capture_info = {"paths": paths, "cameras": [{}, {}]}
        return paths

    def get_latest_lores_frames(self, timeout=1.0, fresh=False, strict=False):
        return None, None

    def stop_stream(self):
        pass

    def close(self):
        pass


class ScannerMock:
    """Drop-in stand-in for `Scanner` in development (see the module docstring)."""

    def __init__(self, xvs=False, camera_mode=None, **kwargs):
        self.camera_mode = (camera_mode or "rpi_sync").lower()
        self.xvs = False
        self.state = ScannerState.IDLE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._stopping = False
        self._manual_session = False

        # simulated hardware
        self.z_mm = 0.0
        self.turntable_motor = _FakeMotor(TURNTABLE_STEP_TYPE)
        self.z_axis_motor = _FakeMotor(Z_STEP_TYPE)
        self.up_endstop = _FakeEndstop()
        self.down_endstop = _FakeEndstop()
        self.lights = _FakeLights()
        self.tof = _FakeToF(self)
        self.dual_cameras: Optional[_FakeCameras] = None
        self._update_endstops()

        self._motion_mutex = threading.Lock()
        self._motion_owner: Optional[int] = None
        self._motion_depth = 0

        # status data read by GET /scanner/status
        self.stream_urls = None
        self.total_photos = 0
        self.images_dir = INPUT_IMAGES_DIR
        self.mode: Optional[str] = None
        self.z_position_mm: Optional[float] = None
        self._face_upload_r2 = True
        self._reset_status()

    # ── status ────────────────────────────────────────────────────────────────
    def _reset_status(self):
        self.phase = ScanPhase.IDLE
        self.phase_message = None
        self.level = 0
        self.photos_captured = 0
        self.photos_uploaded = 0
        self.missing = []
        self.error = None
        self.tof_warnings = []
        self.total_photos = 0
        self.face_result = None

    def _set_phase(self, phase: str, message: Optional[str] = None, error: Optional[str] = None):
        self.phase = phase
        self.phase_message = message
        self.error = error

    def require_motors(self) -> None:
        """Like `Scanner.require_motors`; set `arduino_available = False` to simulate a missing Arduino."""
        if not getattr(self, "arduino_available", True):
            raise ArduinoUnavailableError(ARDUINO_UNAVAILABLE_MESSAGE)

    def require_tof(self) -> None:
        """Like `Scanner.require_tof`; the simulated sensor is always there."""

    def upload_progress(self) -> dict:
        """Same keys as `Scanner.upload_progress`: the simulated uploads succeed at once while a run is RUNNING."""
        running = self.state == ScannerState.RUNNING
        return {"seconds_since_last_upload": 0.0 if running else None, "upload_drain_timeout_s": UPLOAD_DRAIN_TIMEOUT_S}

    @property
    def is_streaming(self) -> bool:
        return self.dual_cameras is not None

    @property
    def scan_owns_rig(self) -> bool:
        return self.state in (ScannerState.PREPARING, ScannerState.PREPARED, ScannerState.RUNNING)

    # ── motion ────────────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def motion_guard(self):
        """Non-blocking and re-entrant per thread, like the real one: a second thread is refused."""
        me = threading.get_ident()
        with self._motion_mutex:
            if self._motion_owner not in (None, me):
                raise ScannerBusyError("Another motion is in progress.")
            self._motion_owner = me
            self._motion_depth += 1
        try:
            yield
        finally:
            with self._motion_mutex:
                self._motion_depth -= 1
                if self._motion_depth == 0:
                    self._motion_owner = None

    def _update_endstops(self):
        self.down_endstop.is_active = self.z_mm <= 0.0
        self.up_endstop.is_active = self.z_mm >= Z_TRAVEL_MAX_MM

    def _move_z(self, delta_mm: float) -> bool:
        with self.motion_guard():
            self.z_axis_motor.distance_to_step_conversion(abs(delta_mm))   # same whole-step rule as the real motor
            time.sleep(_SIM_STEP_S)
            self.z_mm = max(0.0, min(float(Z_TRAVEL_MAX_MM), self.z_mm + delta_mm))
            self._update_endstops()
        return True

    def move_z_up_distance(self, distance: float = 100.0, **kwargs) -> bool:
        return self._move_z(+distance)

    def move_z_down_distance(self, distance: float = 100.0, **kwargs) -> bool:
        return self._move_z(-distance)

    def home_z_axis(self, **kwargs):
        with self.motion_guard():
            time.sleep(_SIM_STEP_S)
            self.z_mm = 0.0
            self._update_endstops()

    def move_z_to_top(self, **kwargs):
        with self.motion_guard():
            time.sleep(_SIM_STEP_S)
            self.z_mm = float(Z_TRAVEL_MAX_MM)
            self._update_endstops()

    # ── prepare ───────────────────────────────────────────────────────────────
    def setup_and_stream(self, enable_stream: bool = True, camera_mode: Optional[str] = None,
                         preflight: bool = True, scan_mode: Optional[str] = ScanMode.OBJECT, check_cloud: bool = True,
                         **kwargs):
        if preflight:
            self.require_motors()
        with self._lock:
            if self._stopping:
                error = ScannerError("Cannot prepare. An emergency stop is still in progress.")
                error.conflict = True
                raise error
            if self.state not in ScannerState.PREPARE_ALLOWED:
                raise RuntimeError(f"Cannot prepare. Scanner is currently {self.state}")
            self._cancel_event.clear()
            self._reset_status()
            self._manual_session = not preflight
            self.mode = scan_mode if preflight else None
            self.z_position_mm = None
            self.state = ScannerState.PREPARING
            self._set_phase(ScanPhase.PREPARING, "Simulated cameras starting")
        self.lights.on()
        self.dual_cameras = _FakeCameras()
        self.dual_cameras.prepare_scan(keep_running=True, **{
            k: v for k, v in kwargs.items() if k not in ("master_id", "slave_id", "rotation")})
        self.stream_urls = self.generate_livestream() if enable_stream else None
        return self.stream_urls

    def generate_livestream(self):
        return ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]

    def finish_preparation(self, z_start_mm: float = Z_START_DEFAULT_MM, upload_r2: bool = True, **camera_kwargs):
        """Simulated homing, climb to `z_start_mm` and camera calibration (about 2 s); a cancel ends it."""
        self._face_upload_r2 = bool(upload_r2)
        try:
            self.z_axis_motor.distance_to_step_conversion(z_start_mm)
            self.home_z_axis()
            if self._cancel_event.wait(0.5):
                return
            if z_start_mm > 0:
                self.move_z_up_distance(z_start_mm)
            if self._cancel_event.wait(1.0):
                return
            self.lights.lock_brightness()
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.PREPARED
                    self.z_position_mm = self.z_mm
                    self._set_phase(ScanPhase.PREPARED, "Simulated rig ready")
        except Exception as e:
            log.error("[ScannerMock] preparation failed: %s", e)
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.ERROR
                    self._set_phase(ScanPhase.ERROR, error=str(e))

    # ── scan ──────────────────────────────────────────────────────────────────
    def scan(self, angle: float = 18.0, z_move_mm: float = 100.0, finish_state: str = ScannerState.IDLE, **kwargs):
        """Simulated scan: one ring per level until the carriage is above the object, two photos per shot."""
        with self._lock:
            if self.state != ScannerState.PREPARED:
                raise RuntimeError("System must be prepared before scanning.")
            if self.mode == ScanMode.FACE:
                raise ScannerModeError("The scanner is prepared for a face scan.")
            steps = self.turntable_motor.angle_to_steps_conversion(angle)
            self.z_axis_motor.distance_to_step_conversion(z_move_mm)
            self._cancel_event.clear()
            self.state = ScannerState.RUNNING
            self._reset_status()
            self._set_phase(ScanPhase.SCANNING)
        shots = max(1, angle_to_steps(360, TURNTABLE_STEP_TYPE) // steps)
        self.level = 1
        while not self._cancel_event.is_set():
            self.phase_message = f"Ring {self.level}: capturing"
            for _ in range(shots):
                if self._cancel_event.wait(_SIM_STEP_S):
                    break
                self.photos_captured += 2
                self.photos_uploaded = self.photos_captured
                self.total_photos = self.photos_captured
            if self._cancel_event.is_set() or self.up_endstop.is_active or not self.tof.probe_object():
                break
            self.move_z_up_distance(z_move_mm)
            self.level += 1
        if self._cancel_event.is_set():
            return
        self._set_phase(ScanPhase.VERIFYING, "Simulated upload verification")
        time.sleep(_SIM_STEP_S)
        with self._lock:
            if self.state == ScannerState.CANCELLED:
                return
            self.state = finish_state
            self._set_phase(ScanPhase.DONE)
        self._teardown()

    _FACE_SCAN_REFUSED_STATES = (
        ScannerState.RUNNING, ScannerState.PREPARING, ScannerState.PROCESSING, ScannerState.DOWNLOADING,
    )

    def _face_detection_problem(self) -> Optional[str]:
        """The simulated rig frames a simulated face: nothing to check."""
        return None

    def begin_face_scan(self, strategy: str = "sweep_best_score", search_range_mm: float = 150.0,
                        step_mm: float = 8.0, manual_z: Optional[float] = None, **_ignored) -> None:
        """Same checks and state change as `Scanner.begin_face_scan` (needs a face prepare first)."""
        self.require_motors()
        with self._lock:
            if self._stopping:
                raise ScannerStoppingError("Cannot start face scan. An emergency stop is still in progress.")
            if self.state in self._FACE_SCAN_REFUSED_STATES:
                raise ScannerBusyError(f"Cannot start face scan. Scanner is currently {self.state}")
            if self.state != ScannerState.PREPARED or self.mode != ScanMode.FACE:
                raise ScannerModeError(
                    f"Cannot start face scan: the scanner is {self.state} (mode {self.mode}). "
                    "Call POST /scanner/face/prepare first."
                )
            if strategy not in ("sweep_best_score", "direct_stop", "none"):
                raise ValueError(f"Unknown strategy {strategy!r}")
            if manual_z is not None:
                if manual_z < 200.0 or manual_z > Z_TRAVEL_MAX_MM:
                    raise ValueError(f"manual_z must be between 200.0 mm and {Z_TRAVEL_MAX_MM} mm (got {manual_z})")
            elif strategy != "none" and self.z_mm + search_range_mm > Z_TRAVEL_MAX_MM:
                raise ValueError(
                    f"The search ends above the Z travel: carriage at {self.z_mm:g} mm + search_range_mm "
                    f"{search_range_mm:g} > {Z_TRAVEL_MAX_MM} mm"
                )
            for value in (step_mm, search_range_mm, manual_z):
                if value is not None:
                    try:
                        self.z_axis_motor.distance_to_step_conversion(value)
                    except Exception as e:
                        raise ValueError(str(e))
            self._cancel_event.clear()
            self._manual_session = False
            self.state = ScannerState.RUNNING
            self._reset_status()
            self._set_phase(ScanPhase.SCANNING, "Face scan: positioning the carriage")

    def face_scan(
        self,
        strategy: str = "sweep_best_score",
        search_range_mm: float = 150.0,
        step_mm: float = 8.0,
        manual_z: Optional[float] = None,
        settle_s: float = 0.150,
        finish_state: str = ScannerState.COMPLETED,
        analyzer: Optional[Any] = None,
        begun: bool = False,
    ) -> dict:
        """Simulated face capture from the prepared height: sweeps or moves directly, and captures mock photos."""
        if not begun:
            self.begin_face_scan(strategy=strategy, search_range_mm=search_range_mm, step_mm=step_mm,
                                 manual_z=manual_z)
        # The real scanner moves Z at constant speed; the same keywords reach the simulated moves
        z_move = {"acceleration": False}
        upload_r2 = self._face_upload_r2

        images_dir = str(self.images_dir)
        paths = []
        target_z = self.z_mm
        best_score = None
        face_detected = None

        def go_to(z: float):
            if z > self.z_mm:
                self.move_z_up_distance(z - self.z_mm, **z_move)
            elif z < self.z_mm:
                overshoot = 3.0
                self.move_z_down_distance(self.z_mm - z + overshoot, **z_move)
                self.move_z_up_distance(overshoot, **z_move)

        try:
            with self.motion_guard():
                self.turntable_motor.disable()
                if self.dual_cameras is None:
                    raise ScannerError("The cameras are not running; call POST /scanner/face/prepare again.")

                if manual_z is not None:
                    go_to(float(manual_z))
                elif strategy != "none":
                    simulated_apex_z = 240.0
                    best_score = -1.0
                    best_z = self.z_mm
                    max_z = self.z_mm + search_range_mm
                    while not self.up_endstop.is_active:
                        if self._cancel_event.is_set():
                            break
                        dist = abs(self.z_mm - simulated_apex_z)
                        sim_score = max(0.1, 1.85 - (dist / 40.0) * 1.5)
                        if analyzer is not None:
                            frame_l, frame_r = self.dual_cameras.get_latest_lores_frames(fresh=True, strict=True)
                            sim_score = analyzer.evaluate_stereo_pair(frame_l, frame_r).get("total_score", 0.0)
                        if sim_score > best_score:
                            best_score, best_z = sim_score, self.z_mm
                        if strategy == "direct_stop" and dist <= 4.0:
                            best_score, best_z = sim_score, self.z_mm
                            break
                        if self.z_mm + step_mm > max_z or self.up_endstop.is_active:
                            break
                        self.move_z_up_distance(step_mm, **z_move)
                    face_detected = best_score >= 0.3
                    go_to(best_z if face_detected else max_z - search_range_mm)
                target_z = self.z_mm

                stem = f"f_{int(round(target_z))}"
                paths = self.dual_cameras.capture_photo(output_prefix=stem, output_dir=images_dir, meshroom_rig=True)
                self.photos_captured = len(paths)
                self.total_photos = len(paths)
                if upload_r2:
                    self.photos_uploaded = self.photos_captured

        except Exception as e:
            if not self._cancel_event.is_set():
                self._set_phase(ScanPhase.ERROR, error=str(e))
                with self._lock:
                    self.state = ScannerState.ERROR
                raise
        finally:
            self._teardown()

        warning = None if face_detected is not False else "No face was found in the search range (simulated)."
        result = {
            "optimal_z_mm": float(target_z),
            "best_score": float(best_score) if face_detected and best_score is not None else None,
            "face_detected": face_detected,
            "warning": warning,
            "photos": list(paths),
            "r2_uploaded": bool(upload_r2),
        }
        if self._cancel_event.is_set():
            self._set_phase(ScanPhase.CANCELLED)
            return {"status": "cancelled", "message": "Face scan was cancelled", **result, "r2_uploaded": False}

        self.face_result = dict(result)
        self._set_phase(ScanPhase.DONE)
        with self._lock:
            if self.state == ScannerState.RUNNING:
                self.state = finish_state
        return {"status": "success", "message": "Face capture completed successfully", **result}

    # ── stop / cleanup ────────────────────────────────────────────────────────
    def _teardown(self):
        self.lights.off()
        self.turntable_motor.disable()
        self.z_axis_motor.disable()
        self._close_cameras()

    def _close_cameras(self):
        if self.dual_cameras is not None:
            self.dual_cameras.close()
        self.dual_cameras = None
        self.stream_urls = None

    def stop_stream(self):
        self._close_cameras()
        with self._lock:
            if self._manual_session and self.state in (ScannerState.PREPARING, ScannerState.PREPARED):
                self._manual_session = False
                self.state = ScannerState.IDLE
                self._set_phase(ScanPhase.IDLE)
        if self.state == ScannerState.IDLE:
            self.lights.off()

    def emergency_stop(self):
        with self._lock:
            self._stopping = True
            self._cancel_event.set()
            self.state = ScannerState.CANCELLED
            self._set_phase(ScanPhase.CANCELLED, "Cancelled")
        try:
            self._teardown()
        finally:
            with self._lock:
                self._stopping = False
                self.state = ScannerState.CANCELLED

    def cleanup(self):
        self._teardown()
        with self._lock:
            if not self._stopping and self.state not in ScannerState.TERMINAL:
                self.state = ScannerState.IDLE
                self._set_phase(ScanPhase.IDLE)

    def close_hardware(self):
        self._teardown()
