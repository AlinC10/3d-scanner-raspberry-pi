import sys
from unittest.mock import MagicMock

# Mock external hardware libraries if we are not on the Pi
class MockHardware:
    @classmethod
    def apply(cls):
        sys.modules['picamera2'] = MagicMock()
        sys.modules['picamera2.encoders'] = MagicMock()
        sys.modules['picamera2.outputs'] = MagicMock()
        sys.modules['libcamera'] = MagicMock()
        sys.modules['libcamera.controls'] = MagicMock()
        sys.modules['smbus2'] = MagicMock()
        sys.modules['gpiozero'] = MagicMock()
        sys.modules['rpi_hardware_pwm'] = MagicMock()
        
        # Serial requires explicit submodule mocks
        sys.modules['serial'] = MagicMock()
        sys.modules['serial.tools'] = MagicMock()
        sys.modules['serial.tools.list_ports'] = MagicMock()
        
        sys.modules['VL53L0X'] = MagicMock()
