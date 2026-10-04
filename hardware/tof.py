import time
import logging
import board
import busio
import adafruit_vl53l0x

log = logging.getLogger(__name__)

class ToFSensor:
    def __init__(self, timing_budget: int = 100000):
        self.available = False
        try:
            # Initialize I2C bus and sensor
            self.i2c = busio.I2C(board.SCL, board.SDA)
            self.sensor = adafruit_vl53l0x.VL53L0X(self.i2c)
            
            # Increase timing budget for high accuracy (default is 33000 us)
            self.sensor.measurement_timing_budget = timing_budget
            self.available = True
            log.info(f"VL53L0X ToF Sensor initialized with {timing_budget/1000}ms budget.")
            
            import atexit
            atexit.register(self.close)
        except Exception as e:
            log.warning(f"Failed to initialize VL53L0X ToF sensor: {e}")

    def get_distance_mm(self) -> float:
        """
        Returns distance in mm. 
        Returns float('inf') if out of range (>8000), hardware error, or disconnected.
        """
        if not self.available:
            return float('inf')
            
        try:
            dist = self.sensor.range
            if dist > 8000:
                return float('inf')
            return float(dist)
        except Exception as e:
            log.error(f"ToF reading error: {e}")
            return float('inf')
            
    def is_object_detected(self, threshold: float) -> bool:
        """
        Checks if an object is present, using a rapid double-read debounce 
        to filter out floating point noise or random dust reflections.
        """
        if self.get_distance_mm() < threshold:
            time.sleep(0.01)  # Debounce
            if self.get_distance_mm() < threshold:
                return True
        return False

    def close(self) -> None:
        """
        Releases the I2C bus back to the OS.
        """
        if hasattr(self, "i2c") and self.i2c is not None:
            try:
                self.i2c.deinit()
                log.info("VL53L0X ToF sensor I2C bus deinitialized.")
            except Exception as e:
                log.warning(f"Error releasing I2C bus: {e}")

