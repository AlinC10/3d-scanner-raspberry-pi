import os
import time
import logging
import json
import threading
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
    # Do not kill the whole process (FastAPI) from a library import; the
    # Focuser raises FocuserUnavailable when it is actually instantiated.
    smbus2 = None



logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class FocuserUnavailable(RuntimeError):
    """The VCM bus cannot be opened or smbus2 is missing."""


class FocuserWriteError(FocuserUnavailable):
    """An I2C write to the VCM failed (after one retry); the position was not committed."""


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

    Two pieces of state are tracked separately:
      * ``position``  - the last committed target (persisted to the state file).
      * ``hw_valid``  - whether the VCM is known to physically be at ``position``.
        It is False after process start and after the camera stops (the camera
        I2C bus is power-gated), so the first write after each camera start is
        always sent to the hardware even if ``position`` equals the target.
    """

    def __init__(self, bus: int = VCM_I2C_BUS, addr: int = VCM_I2C_ADDR):
        self._bus_num = bus
        self._addr = addr
        self._lock = threading.RLock()

        # Get the directory where focuser.py is located
        current_dir = Path(__file__).resolve().parent
        self._state_file = current_dir / "config" / f"focus_state_bus{bus}.json"

        self._position = self._load_state()
        self._hw_valid = False
        self._bus = None
        self._initialised = False
        self._open()
        # NOTE: VCM init write is deferred until first use because the
        # camera I2C bus is power-gated — the VCM is only reachable
        # while picamera2 is actively streaming.

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def _open(self):
        if smbus2 is None:
            raise FocuserUnavailable(
                "smbus2 not found. Install with: sudo apt install python3-smbus2"
            )
        try:
            self._bus = smbus2.SMBus(self._bus_num)
            log.info("Opened I2C bus %d, VCM addr=0x%02X", self._bus_num, self._addr)
        except (OSError, FileNotFoundError) as exc:
            raise FocuserUnavailable(
                f"Cannot open I2C bus {self._bus_num}: {exc}. "
                "Ensure dtoverlay=imx477,vcm is in /boot/firmware/config.txt "
                "and you have rebooted the Pi."
            ) from exc

    def _ensure_init(self):
        """Power-on sequence: write 0x00 to register 0x02 (once per power cycle)."""
        if self._initialised or self._bus is None:
            return
        try:
            self._bus.write_byte_data(self._addr, 0x02, 0x00)
            time.sleep(0.01)
            self._initialised = True
            log.info("VCM initialised on i2c-%d", self._bus_num)
        except OSError as exc:
            log.debug("VCM init write deferred (camera not streaming yet): %s", exc)

    def invalidate(self):
        """
        Mark the VCM as unpowered/unknown (call whenever the camera stops).
        The next position write is forced to the hardware and re-runs the init.
        """
        with self._lock:
            self._hw_valid = False
            self._initialised = False

    mark_unpowered = invalidate

    @property
    def hw_valid(self) -> bool:
        return self._hw_valid

    def close(self):
        """Close the I2C bus. Idempotent."""
        with self._lock:
            bus, self._bus = self._bus, None
            self._hw_valid = False
            self._initialised = False
        if bus is not None:
            try:
                bus.close()
            except Exception as exc:
                log.debug("Error closing I2C bus %d: %s", self._bus_num, exc)

    # ── Low-level I2C write ───────────────────────────────────────────────────
    def _write_raw(self, value: int) -> bool:
        """Send a 10-bit DAC value to the VCM driver. Returns True on success."""
        if self._bus is None:
            log.error("I2C write error: bus %d is closed", self._bus_num)
            return False
        value = max(VCM_MIN_POS, min(VCM_MAX_POS, value))
        high = (value >> 4) & 0x3F
        low = (value & 0x0F) << 4
        try:
            self._bus.write_i2c_block_data(self._addr, high, [low])
            return True
        except OSError as exc:
            log.error("I2C write error: %s", exc)
            return False

    def _write_checked(self, value: int) -> bool:
        """_write_raw with a single retry (re-running the VCM init in between)."""
        if self._write_raw(value):
            return True
        time.sleep(0.01)
        self._initialised = False
        self._ensure_init()
        return self._write_raw(value)

    # ── State persistence ─────────────────────────────────────────────────────
    def _load_state(self) -> int:
        try:
            if self._state_file.exists():
                with open(self._state_file, 'r') as f:
                    data = json.load(f)
                    return max(VCM_MIN_POS, min(VCM_MAX_POS, int(data.get("position", 0))))
        except Exception as e:
            log.debug("Failed to load focus state: %s", e)
        return 0

    def _save_state(self):
        try:
            tmp = self._state_file.with_suffix(".json.tmp")
            with open(tmp, 'w') as f:
                json.dump({"position": self._position}, f)
            os.replace(tmp, self._state_file)
        except Exception as e:
            log.debug("Failed to save focus state: %s", e)

    def restore_state(self) -> bool:
        """
        Make sure the hardware is at the last known position (used after power-on).
        Forces a physical write when the hardware state is unknown. Returns False
        (and logs) when the write fails.
        """
        with self._lock:
            if self._hw_valid:
                return True
            target = self._position
            try:
                self.set_position(target, settle=False)
            except FocuserUnavailable as exc:
                log.error("Could not restore VCM on i2c-%d to position %d: %s", self._bus_num, target, exc)
                return False
            log.info("Restored VCM on i2c-%d to position %d", self._bus_num, target)
            return True

    # ── Public API ────────────────────────────────────────────────────────────
    @property
    def position(self) -> int:
        return self._position

    def set_position(self, pos: int, settle: bool = True):
        """
        Move VCM to absolute position [0 … 1023] with a smooth ramp to
        prevent mechanical shock (snapping) when making large jumps.

        The first call after a camera start (``hw_valid`` False) is always
        written to the hardware, ramping from 0 (the VCM power-on position).
        The position and the state file are committed only after the final
        write succeeded; on failure FocuserWriteError is raised and the next
        call is forced to the hardware again.
        """
        with self._lock:
            self._ensure_init()
            target = max(VCM_MIN_POS, min(VCM_MAX_POS, pos))

            # Smooth ramp: max jump of 20 units per step to prevent hard stops
            step_size = 20
            delay_per_step = 0.002  # 2ms per microstep

            if not self._hw_valid or self._position != target:
                start = self._position if self._hw_valid else VCM_MIN_POS
                direction = 1 if target > start else -1
                ok = True
                for p in range(start, target, direction * step_size):
                    if not self._write_checked(p):
                        ok = False
                        break
                    time.sleep(delay_per_step)

                # Write final target position
                if ok:
                    ok = self._write_checked(target)
                if not ok:
                    self._hw_valid = False
                    raise FocuserWriteError(
                        f"I2C write to VCM (i2c-{self._bus_num}, addr=0x{self._addr:02X}) failed "
                        f"while moving to position {target}"
                    )
                changed = self._position != target
                self._position = target
                self._hw_valid = True
                if changed:  # a forced re-write of the cached value needs no new state file
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
