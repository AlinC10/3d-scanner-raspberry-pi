import math
import time
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

# The Pi-only libraries are imported defensively: a missing library (or Blinka refusing to run on a
# non-Pi platform) must disable the sensor, not make `import hardware.scanner` fail.
try:
    import board
    import busio
    import adafruit_vl53l1x
    _IMPORT_ERROR: Optional[Exception] = None
except Exception as _e:  # ImportError, or a platform error raised by Blinka
    board = busio = adafruit_vl53l1x = None
    _IMPORT_ERROR = _e


class ToFSensor:
    def __init__(self, timing_budget_ms: int = 200, distance_mode: int = 1,
                 roi_size: tuple[int, int] = (4, 4), roi_center: int = 199):
        """
        :param timing_budget_ms: One of 15 (short mode only), 20, 33, 50, 100, 200, 500.
        :param distance_mode: 1 = short (up to ~136 cm), 2 = long (up to ~360 cm).
        :param roi_size: (x, y) SPAD window, 4..16. Smaller means a narrower cone and less signal.
        :param roi_center: SPAD index of the window centre (199 = optical centre of the array).
        """
        self.available = False
        self._timing_budget_ms = timing_budget_ms
        # Ranging is a multi-step I2C sequence (flush, poll, read, clear), so concurrent
        # callers (scan loop and /test/tof) must not interleave.
        self._lock = threading.Lock()
        self.i2c = None
        self.sensor = None
        if _IMPORT_ERROR is not None:
            log.warning(f"VL53L1X ToF sensor unavailable, driver libraries missing: {_IMPORT_ERROR}")
            return
        try:
            # Initialize I2C bus and sensor
            self.i2c = busio.I2C(board.SCL, board.SDA)
            self.sensor = adafruit_vl53l1x.VL53L1X(self.i2c)

            # Order matters: the distance mode selects the valid timing budget table, and
            # setting roi_xy resets the ROI centre, so roi_center must come last.
            self.sensor.distance_mode = distance_mode
            self.sensor.timing_budget = timing_budget_ms
            self.sensor.roi_xy = roi_size
            self.sensor.roi_center = roi_center
            self.sensor.start_ranging()
            self.available = True

            log.info(
                "VL53L1X ToF Sensor initialized: distance_mode=%s, timing_budget=%sms, roi_xy=%s, roi_center=%s",
                self.sensor.distance_mode, self.sensor.timing_budget, self.sensor.roi_xy, self.sensor.roi_center,
            )

            import atexit
            atexit.register(self.close)
        except Exception as e:
            log.warning(f"Failed to initialize VL53L1X ToF sensor: {e}")
            self.available = False
            # The bus may already be open (constructor or configuration failed): release it.
            self.sensor = None
            if self.i2c is not None:
                try:
                    self.i2c.deinit()
                except Exception as deinit_error:
                    log.warning(f"Error releasing I2C bus after failed init: {deinit_error}")
                finally:
                    self.i2c = None

    def read_distance_mm(self) -> Optional[float]:
        """
        Returns the distance in mm of a fresh measurement (taken after this call started
        waiting, so it blocks for up to one timing budget), or None when there is no valid
        reading: sensor not available (returns immediately), invalid range status (no target,
        signal too weak, out of range), timeout, or any driver/I2C error.
        None means "unknown", never "no object": the empty backdrop is itself a valid reading.
        """
        if not self.available:
            return None

        with self._lock:
            # close() may have run while this call waited for the lock
            if not self.available or self.sensor is None:
                return None
            try:
                # Discard the sample that is already waiting, it may predate the last move
                self.sensor.clear_interrupt()

                deadline = time.monotonic() + (2 * self._timing_budget_ms + 100) / 1000.0
                while not self.sensor.data_ready:
                    if time.monotonic() > deadline:
                        log.warning("ToF read timed out waiting for a measurement.")
                        return None
                    time.sleep(0.005)

                dist_cm = self.sensor.distance
                self.sensor.clear_interrupt()

                if dist_cm is None:
                    return None
                dist_mm = float(dist_cm) * 10.0
                if not math.isfinite(dist_mm) or dist_mm <= 0:
                    return None
                return dist_mm
            except Exception as e:
                log.error(f"ToF reading error: {e}")
                return None

    def get_distance_mm(self) -> float:
        """
        Legacy form of read_distance_mm(): returns float('inf') when there is no valid reading.
        New code should use read_distance_mm() or probe_object(), which keep "invalid" apart
        from "far away".
        """
        distance = self.read_distance_mm()
        return float('inf') if distance is None else distance

    def probe_object(self, threshold: float, samples: int = 2) -> Optional[bool]:
        """
        Decides whether an object is in the cone, from up to `samples` consecutive fresh
        readings (each one via read_distance_mm). Rule, evaluated reading by reading:
          - invalid reading (None)            -> stop, return None (unknown, no decision);
          - valid reading >= threshold        -> stop, return False (backdrop seen);
          - valid reading < threshold         -> take the next one;
          - `samples` valid readings all < threshold -> return True (object present).
        So a False result is always backed by a valid far reading, and an invalid reading
        before the decision makes the whole probe None, even if an earlier reading was close.
        """
        for _ in range(max(1, samples)):
            distance = self.read_distance_mm()
            if distance is None:
                return None
            if distance >= threshold:
                return False
        return True

    def is_object_detected(self, threshold: float) -> bool:
        """
        True only when probe_object() is True: two consecutive fresh valid readings below the
        threshold, which filters noise and random dust reflections. An unknown outcome (invalid
        readings) is False here; callers that must tell it apart from "no object" use probe_object().
        """
        return self.probe_object(threshold) is True

    def close(self) -> None:
        """
        Stops ranging and releases the I2C bus back to the OS.
        """
        with self._lock:
            self.available = False

            if getattr(self, "sensor", None) is not None:
                try:
                    self.sensor.stop_ranging()
                except Exception as e:
                    log.warning(f"Error stopping ToF ranging: {e}")
                finally:
                    self.sensor = None

            if getattr(self, "i2c", None) is not None:
                try:
                    self.i2c.deinit()
                    log.info("VL53L1X ToF sensor I2C bus deinitialized.")
                except Exception as e:
                    log.warning(f"Error releasing I2C bus: {e}")
                finally:
                    self.i2c = None
