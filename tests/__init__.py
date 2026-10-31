import os
import sys
from unittest.mock import MagicMock

# No pause after the homing in the tests (the real one is 1 s); the tests of the pause itself patch the constant
os.environ.setdefault("SCAN_Z_HOME_SETTLE_S", "0")
# No buzzer in the tests (no GPIO on a PC; tests/test_buzzer.py uses a fake device)
os.environ.setdefault("BUZZER_GPIO_PIN", "off")
# One photo pair per face capture (the burst has its own tests in tests/test_face_scan.py)
os.environ.setdefault("SCAN_FACE_BURST_SHOTS", "1")

# Mock hardware, camera, and cloud dependencies for test runs without hardware
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "picamera2.encoders", "picamera2.outputs",
    "libcamera", "RPi", "RPi.GPIO", "serial",
    "pydantic", "fastapi", "fastapi.responses",
    "cloud", "cloud.cloudflare_r2", "cloud.runpod", "cloud.job",
    "system", "system.file_control",
    "hardware.motor", "hardware.endstop", "hardware.lights", "hardware.tof",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# The R2 key helpers are pure functions: give the mock fixed ones (the key is the path relative to the job's input/
# folder, "rig/0/f_1000.jpg", so the scanner tests read the same keys as before the per-job prefix)
_r2 = sys.modules["cloud.cloudflare_r2"]
if isinstance(_r2, MagicMock):
    _r2.new_job_id = lambda: "20261030-101500-abcdef"
    _r2.input_key = lambda job_id, relative_path: relative_path
    _r2.input_prefix = lambda job_id: ""
    _r2.job_prefix = lambda job_id: "rig/"
    _r2.delete_stale_jobs = MagicMock(return_value=[])
    # `import cloud.cloudflare_r2 as r2` reads the attribute of the (mocked) `cloud` package, not sys.modules
    if isinstance(sys.modules["cloud"], MagicMock):
        sys.modules["cloud"].cloudflare_r2 = _r2

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
