import os
import sys
from unittest.mock import MagicMock

# No pause after the homing in the tests (the real one is 1 s); the tests of the pause itself patch the constant
os.environ.setdefault("SCAN_Z_HOME_SETTLE_S", "0")

# Mock hardware, camera, and cloud dependencies for test runs without hardware
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "picamera2.encoders", "picamera2.outputs",
    "libcamera", "RPi", "RPi.GPIO", "serial",
    "pydantic", "fastapi", "fastapi.responses",
    "cloud", "cloud.cloudflare_r2", "cloud.runpod",
    "system", "system.file_control",
    "hardware.motor", "hardware.endstop", "hardware.lights", "hardware.tof",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Configure serial mock so subpackages like serial.tools.list_ports resolve
mock_serial = sys.modules["serial"]
mock_serial.__path__ = []
sys.modules["serial.tools"] = MagicMock()
sys.modules["serial.tools.list_ports"] = MagicMock()

# Ensure Endstop exceptions inherit from Exception
class TopEndstopTriggered(Exception): pass
class BottomEndstopTriggered(Exception): pass
sys.modules["hardware.endstop"].TopEndstopTriggered = TopEndstopTriggered
sys.modules["hardware.endstop"].BottomEndstopTriggered = BottomEndstopTriggered

# Ensure Picamera2 class exists on picamera2 mock
mock_p2 = sys.modules["picamera2"]
if not hasattr(mock_p2, "Picamera2") or isinstance(mock_p2.Picamera2, MagicMock):
    class MockPicam2:
        def __init__(self, camera_id=0):
            self.camera_id = camera_id
            self.camera_controls = {"SyncMode": 1, "SyncFrames": 1}
            self.started = True
        def create_video_configuration(self, **kwargs): return kwargs
        def configure(self, config): pass
        def start(self): self.started = True
        def stop(self): self.started = False
        def close(self): self.started = False
        def capture_request(self, wait=None):
            req = MagicMock()
            req.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1000000000}
            return req
    mock_p2.Picamera2 = MockPicam2
