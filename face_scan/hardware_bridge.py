"""
Hardware Bridge & Rig Interface for Face Scanning
=================================================
Connects face scanning routines with the Raspberry Pi scanner hardware
or provides desktop simulation mocks when hardware is unavailable.
"""

import os
import sys
import logging
from pathlib import Path
from typing import Optional, Any

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger("3DScanner.HardwareBridge")

# Check environment for mock hardware flag
MOCK_ENV = os.environ.get("MOCK_HARDWARE") == "1"

try:
    if MOCK_ENV:
        raise ImportError("MOCK_HARDWARE is set to 1")

    from hardware.motor import Motor, Z_STEP_TYPE
    from hardware.endstop import Endstop, TopEndstopTriggered
    from hardware.dual_camera_rpi_sync import DualCameraRPISync
    from hardware.rig_limits import Z_TRAVEL_MAX_MM
    HARDWARE_AVAILABLE = True
except (ImportError, ModuleNotFoundError) as err:
    log.info("Hardware modules not available (%s); mock hardware will be used.", err)
    HARDWARE_AVAILABLE = False
    Motor = None  # type: ignore
    Endstop = None  # type: ignore
    DualCameraRPISync = None  # type: ignore
    Z_STEP_TYPE = "1/4"
    Z_TRAVEL_MAX_MM = 475.0

    class TopEndstopTriggered(Exception):
        """Mock exception for top limit switch trigger."""
        pass


class MockMotor:
    """Mock Z-axis motor for desktop simulation."""
    def __init__(self, motor_id: int = 1, step_type: str = "1/4", **kwargs):
        self.motor_id = motor_id
        self.step_type = step_type
        self.current_z_mm = 0.0
        self.is_enabled = False
        log.info("[MOCK] Motor Z initialized (ID %d).", motor_id)

    def enable(self):
        self.is_enabled = True
        log.info("[MOCK] Motor Z energized.")

    def disable(self, settle_delay: float = 0.5, **kwargs):
        self.is_enabled = False
        log.info("[MOCK] Motor Z de-energized.")

    def distance_to_step_conversion(self, distance: float, step_type: Optional[str] = None) -> int:
        return int(distance * 100)  # 100 steps/mm on T8x8 with 1/4 microstepping

    def rotate(self, clockwise: bool = True, steps: int = 0, delay: float = 0.001, **kwargs) -> bool:
        dist_mm = steps / 100.0
        if clockwise:
            self.current_z_mm += dist_mm
            log.info("[MOCK] Motor Z climbed +%.2f mm -> Height: %.2f mm", dist_mm, self.current_z_mm)
        else:
            self.current_z_mm = max(0.0, self.current_z_mm - dist_mm)
            log.info("[MOCK] Motor Z descended -%.2f mm -> Height: %.2f mm", dist_mm, self.current_z_mm)
        return True

    def stop(self, **kwargs):
        log.info("[MOCK] Motor Z stop.")


class MockCameras:
    """Mock dual camera driver returning real test frames or synthetic previews."""
    def __init__(self, test_image_path: Optional[str] = None):
        self.test_img = None
        if test_image_path:
            p = Path(test_image_path)
            if not p.is_file():
                p_alt = Path(__file__).resolve().parent / test_image_path
                if p_alt.is_file():
                    p = p_alt
            if p.is_file():
                try:
                    import cv2
                    self.test_img = cv2.imread(str(p))
                except ImportError:
                    pass

        if self.test_img is None:
            import numpy as np
            self.test_img = np.zeros((720, 960, 3), dtype=np.uint8)

    def prepare_scan(self, **kwargs):
        log.info("[MOCK] Dual Cameras stream prepared.")

    def get_preview_frame(self, camera_index: int, sim_vertical_shift: float = 0.0):
        import cv2
        import numpy as np
        preview = cv2.resize(self.test_img, (960, 720))
        shift_y = int(sim_vertical_shift * 5.0)
        if shift_y != 0:
            M = np.float32([[1, 0, 0], [0, 1, shift_y]])
            preview = cv2.warpAffine(preview, M, (960, 720), borderMode=cv2.BORDER_REFLECT)
        return preview

    def capture_photo(self, save_path_0: str, save_path_1: str):
        import cv2
        Path(save_path_0).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path_1).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(save_path_0, self.test_img)
        cv2.imwrite(save_path_1, self.test_img)
        log.info("[MOCK] Stereo pair saved: %s and %s", save_path_0, save_path_1)

    def close(self):
        log.info("[MOCK] Cameras closed cleanly.")


class MockEndstop:
    """Mock limit switch."""
    def __init__(self, pin: int = 4, label: str = "ENDSTOP", **kwargs):
        self.pin = pin
        self.label = label
        self.is_active = False

    def simulate_trigger(self, active: bool = True):
        self.is_active = active


__all__ = [
    "HARDWARE_AVAILABLE",
    "MockMotor",
    "MockCameras",
    "MockEndstop",
    "Motor",
    "Endstop",
    "TopEndstopTriggered",
    "DualCameraRPISync",
    "Z_STEP_TYPE",
    "Z_TRAVEL_MAX_MM",
]
