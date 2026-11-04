# VCM driver IC (DW9714-compatible) on Raspberry Pi CSI camera I2C bus
import os
from typing import Optional

VCM_I2C_BUS      = 10          # /dev/i2c-10  (CSI camera bus on RPi 5)
VCM_I2C_ADDR     = 0x0C        # Default address for Arducam B0272 VCM
VCM_MIN_POS      = 0           # Far / infinity
VCM_MAX_POS      = 1023        # Near / macro
VCM_STEP_SMALL   = 10
VCM_STEP_LARGE   = 50
VCM_MOVE_DELAY_S = 0.06        # ~60 ms settling time per step

# Calibrated optimal focus positions: (cam1, cam2)
# Configurable directly in Python (modify the tuples below) or via environment variables.
# Objects: camera lenses focused at turntable center distance (~35-40 cm).
OPTIMAL_FOCUS_OBJECT: tuple[int, int] = (100, 100)

# Faces: subject seated with chin around turntable center (~35-45 cm).
OPTIMAL_FOCUS_FACE: tuple[int, int] = (120, 120)


def get_optimal_focus(scan_mode: Optional[str] = None) -> tuple[int, int]:
    """
    Returns the optimal focus pair (cam1, cam2) for the given scan mode ('object' or 'face').
    Respects FOCUS_OPTIMAL_OBJECT and FOCUS_OPTIMAL_FACE environment variable overrides.
    """
    mode = str(scan_mode).strip().lower() if scan_mode else ""
    if mode in ("face", "scanmode.face"):
        env_val = os.environ.get("FOCUS_OPTIMAL_FACE")
        if env_val:
            try:
                parts = [int(p.strip()) for p in env_val.replace("x", ",").split(",") if p.strip()]
                if len(parts) == 1:
                    return (parts[0], parts[0])
                if len(parts) >= 2:
                    return (parts[0], parts[1])
            except Exception:
                pass
        return OPTIMAL_FOCUS_FACE

    env_val = os.environ.get("FOCUS_OPTIMAL_OBJECT")
    if env_val:
        try:
            parts = [int(p.strip()) for p in env_val.replace("x", ",").split(",") if p.strip()]
            if len(parts) == 1:
                return (parts[0], parts[0])
            if len(parts) >= 2:
                return (parts[0], parts[1])
        except Exception:
            pass
    return OPTIMAL_FOCUS_OBJECT
