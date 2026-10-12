import unittest
from unittest.mock import MagicMock, patch
import os
import sys

os.environ["MOCK_HARDWARE"] = "1"

# Mock hardware and third-party modules not present in sandbox
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "RPi", "RPi.GPIO", "gpiozero",
    "serial", "serial.tools", "serial.tools.list_ports",
    "board", "busio", "adafruit_vl53l0x",
    "fastapi", "fastapi.responses", "pydantic",
    "cloud", "cloud.runpod", "cloud.cloudflare_r2",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Configure minimal FastAPI and Pydantic mocks
mock_fastapi = sys.modules["fastapi"]
class MockHTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)

mock_fastapi.HTTPException = MockHTTPException

def mock_decorator(*args, **kwargs):
    def deco(fn):
        return fn
    return deco

mock_router = MagicMock()
mock_router.get = mock_decorator
mock_router.post = mock_decorator
mock_router.put = mock_decorator
mock_router.delete = mock_decorator
mock_fastapi.APIRouter = lambda *args, **kwargs: mock_router

class MockBaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

sys.modules["pydantic"].BaseModel = MockBaseModel
sys.modules["pydantic"].Field = lambda *args, **kwargs: kwargs.get("default", None)

def mock_query(default=None, **kwargs):
    return default
mock_fastapi.Query = mock_query

class MockStreamingResponse:
    def __init__(self, generator, media_type=None):
        self.generator = generator
        self.media_type = media_type

sys.modules["fastapi.responses"].StreamingResponse = MockStreamingResponse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import router.scanner
import router.test


class TestFastAPIMJPEGStreamRoutes(unittest.TestCase):
    def setUp(self):
        self.scanner = router.scanner.scanner
        self.scanner.dual_cameras = None
        self.scanner.is_streaming = False

    def tearDown(self):
        self.scanner.dual_cameras = None
        self.scanner.is_streaming = False

    def test_mjpeg_stream_not_streaming(self):
        self.scanner.dual_cameras = None
        self.scanner.is_streaming = False
        # Should raise 400 when cameras are not streaming
        with self.assertRaises(MockHTTPException) as ctx:
            router.test.test_stream_mjpeg()
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Cameras are not streaming", ctx.exception.detail)

        with self.assertRaises(MockHTTPException) as ctx2:
            router.scanner.scanner_stream_mjpeg()
        self.assertEqual(ctx2.exception.status_code, 400)

    def test_mjpeg_stream_streaming_generator(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.wait_jpeg.side_effect = [
            (1, b"\xff\xd8\xff\xe0fakejpeg1"),
            (1, b"\xff\xd8\xff\xe0fakejpeg1"),  # duplicate id, won't yield
            (2, b"\xff\xd8\xff\xe0fakejpeg2"),
        ]
        self.scanner.dual_cameras = mock_dual_cam
        self.scanner.is_streaming = True

        # Test /test/stream/mjpeg
        resp = router.test.test_stream_mjpeg(camera=2)
        self.assertEqual(resp.media_type, "multipart/x-mixed-replace; boundary=FRAME")
        self.assertEqual(mock_dual_cam.active_camera, 2)

        # Consume 2 frames from generator
        gen = resp.generator
        f1 = next(gen)
        self.assertIn(b"--FRAME", f1)
        self.assertIn(b"fakejpeg1", f1)

        f2 = next(gen)
        self.assertIn(b"--FRAME", f2)
        self.assertIn(b"fakejpeg2", f2)

    def test_switch_active_camera(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.active_camera = 1
        self.scanner.dual_cameras = mock_dual_cam

        # Switch via query parameter in /test/stream/active
        res = router.test.test_stream_switch_active(camera_id=2)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 2)

        # Switch via mock request object
        req = MagicMock(camera_id=1)
        res = router.test.test_stream_switch_active(req=req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 1)

        # Switch via /scanner/stream/active
        res = router.scanner.scanner_stream_switch_active(camera_id=2)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 2)

    def test_set_stream_quality(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.quality = 95
        self.scanner.dual_cameras = mock_dual_cam

        # Update via /test/stream/quality
        res = router.test.test_stream_set_quality(quality=80)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.quality, 80)

        # Update via mock request object
        req = MagicMock(quality=90)
        res = router.test.test_stream_set_quality(req=req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.quality, 90)

        # Update via /scanner/stream/quality
        res = router.scanner.scanner_stream_set_quality(quality=75)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.quality, 75)

    def test_stream_sync_status(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.get_sync_status.return_value = {
            "ready": True,
            "cam1_seq": 42,
            "cam2_seq": 42,
            "active_camera": 1,
            "offset_ms": 0.05,
            "stream_fps": 10.0,
            "last_jpeg_kb": 85.0,
        }
        self.scanner.dual_cameras = mock_dual_cam

        status1 = router.test.test_stream_sync_status()
        self.assertTrue(status1["ready"])
        self.assertEqual(status1["cam1_seq"], 42)
        self.assertEqual(status1["offset_ms"], 0.05)

        status2 = router.scanner.scanner_stream_sync_status()
        self.assertEqual(status2["cam2_seq"], 42)


if __name__ == "__main__":
    unittest.main()
