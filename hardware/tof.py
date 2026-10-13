import time
import logging
import threading
import board
import busio
import adafruit_vl53l1x

log = logging.getLogger(__name__)

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

    def get_distance_mm(self) -> float:
        """
        Returns distance in mm of a fresh measurement (taken after this call started
        waiting, so it blocks for up to one timing budget).
        Returns float('inf') if there is no valid target (invalid range status),
        on timeout, hardware error, or if the sensor is disconnected.
        """
        if not self.available:
            return float('inf')

        with self._lock:
            try:
                # Discard the sample that is already waiting, it may predate the last move
                self.sensor.clear_interrupt()

                deadline = time.monotonic() + (2 * self._timing_budget_ms + 100) / 1000.0
                while not self.sensor.data_ready:
                    if time.monotonic() > deadline:
                        log.warning("ToF read timed out waiting for a measurement.")
                        return float('inf')
                    time.sleep(0.005)

                dist_cm = self.sensor.distance
                self.sensor.clear_interrupt()

                if dist_cm is None:
                    return float('inf')
                return float(dist_cm * 10)
            except Exception as e:
                log.error(f"ToF reading error: {e}")
                return float('inf')

    def is_object_detected(self, threshold: float) -> bool:
        """
        Checks if an object is present, requiring two consecutive fresh readings
        below the threshold to filter out noise or random dust reflections.
        """
        return (self.get_distance_mm() < threshold
                and self.get_distance_mm() < threshold)

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
