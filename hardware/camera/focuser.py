import sys
import time
import logging
import json
from pathlib import Path
from .config import (
    VCM_I2C_ADDR,
    VCM_I2C_BUS,
    VCM_MAX_POS,
    VCM_MIN_POS,
    VCM_MOVE_DELAY_S,
)

try:
    import smbus2
except ImportError:
    sys.exit(
        "[ERROR] smbus2 not found. Install with:\n" "  sudo apt install python3-smbus2"
    )



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# VCM / Focus motor driver
# ─────────────────────────────────────────────────────────────────────────────
class Focuser:
    """
    Controls the Voice Coil Motor (VCM) on the Arducam IMX477 B0272
    via I2C using smbus2.

    The DW9714-compatible driver expects a 10-bit DAC value split into
    two bytes:  high_byte = bits[9:4]   low_byte = bits[3:0] << 4
    Init: write 0x00 to register 0x02 to power-on the driver.
    """

    def __init__(self, bus: int = VCM_I2C_BUS, addr: int = VCM_I2C_ADDR):
        self._bus_num = bus
        self._addr = addr
        
        # Get the directory where focuser.py is located
        current_dir = Path(__file__).resolve().parent
        self._state_file = current_dir / "config" / f"focus_state_bus{bus}.json"
    
        self._position = self._load_state()
        self._bus = None
        self._initialised = False
        self._open()
        # NOTE: VCM init write is deferred until first use because the
        # camera I2C bus is power-gated — the VCM is only reachable
        # while picamera2 is actively streaming.

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def _open(self):
        try:
            self._bus = smbus2.SMBus(self._bus_num)
            log.info("Opened I2C bus %d, VCM addr=0x%02X", self._bus_num, self._addr)
        except (OSError, FileNotFoundError) as exc:
            sys.exit(
                f"[ERROR] Cannot open I2C bus {self._bus_num}: {exc}\n"
                "  Ensure dtoverlay=imx477,vcm is in /boot/firmware/config.txt\n"
                "  and you have rebooted the Pi."
            )

    def _ensure_init(self):
        """Power-on sequence: write 0x00 to register 0x02 (once)."""
        if self._initialised:
            return
        try:
            self._bus.write_byte_data(self._addr, 0x02, 0x00)
            time.sleep(0.01)
            self._initialised = True
            log.info("VCM initialised on i2c-%d", self._bus_num)
        except OSError as exc:
            log.debug("VCM init write deferred (camera not streaming yet): %s", exc)

    def close(self):
        if self._bus:
            self._bus.close()
            self._bus = None

    # ── Low-level I2C write ───────────────────────────────────────────────────
    def _write_raw(self, value: int):
        """Send a 10-bit DAC value to the VCM driver."""
        value = max(VCM_MIN_POS, min(VCM_MAX_POS, value))
        high = (value >> 4) & 0x3F
        low = (value & 0x0F) << 4
        try:
            self._bus.write_i2c_block_data(self._addr, high, [low])
        except OSError as exc:
            log.error("I2C write error: %s", exc)

    # ── State persistence ─────────────────────────────────────────────────────
    def _load_state(self) -> int:
        try:
            if self._state_file.exists():
                with open(self._state_file, 'r') as f:
                    data = json.load(f)
                    return int(data.get("position", 0))
        except Exception as e:
            log.debug("Failed to load focus state: %s", e)
        return 0

    def _save_state(self):
        try:
            with open(self._state_file, 'w') as f:
                json.dump({"position": self._position}, f)
        except Exception as e:
            log.debug("Failed to save focus state: %s", e)

    def restore_state(self):
        """Force the hardware to move to the last known position (used after power-on)."""
        if self._position != 0:
            target = self._position
            self._position = 0  # Force physical write
            self.set_position(target, settle=False)
            log.info("Restored VCM on i2c-%d to position %d", self._bus_num, target)

    # ── Public API ────────────────────────────────────────────────────────────
    @property
    def position(self) -> int:
        return self._position

    def set_position(self, pos: int, settle: bool = True):
        """
        Move VCM to absolute position [0 … 1023] with a smooth ramp to
        prevent mechanical shock (snapping) when making large jumps.
        """
        self._ensure_init()
        target = max(VCM_MIN_POS, min(VCM_MAX_POS, pos))

        # Smooth ramp: max jump of 20 units per step to prevent hard stops
        step_size = 20
        delay_per_step = 0.002  # 2ms per microstep

        if self._position != target:
            direction = 1 if target > self._position else -1
            for p in range(self._position, target, direction * step_size):
                self._write_raw(p)
                time.sleep(delay_per_step)

            # Write final target position
            self._write_raw(target)
            self._position = target
            self._save_state()

        if settle:
            time.sleep(VCM_MOVE_DELAY_S)
        log.debug("VCM -> position %d", target)

    def step(self, delta: int):
        """Move VCM by a relative step (positive=near, negative=far)."""
        self.set_position(self._position + delta)

    def reset(self):
        """Move to infinity (far) — position 0."""
        self.set_position(VCM_MIN_POS)
        log.info("VCM reset to far/infinity (pos=0)")
