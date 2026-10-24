import unittest
from unittest.mock import MagicMock, patch
import os
import sys

os.environ["MOCK_HARDWARE"] = "1"

# Mock hardware and third-party modules not present in sandbox
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "RPi", "RPi.GPIO", "gpiozero",
    "serial", "serial.tools", "serial.tools.list_ports",
    "board", "busio", "adafruit_vl53l1x",
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

    def tearDown(self):
        self.scanner.dual_cameras = None

    def test_mjpeg_stream_not_streaming(self):
        self.scanner.dual_cameras = None
        # Should raise 400 when cameras are not streaming
        with self.assertRaises(MockHTTPException) as ctx:
            router.scanner.scanner_stream_mjpeg()
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

        # Test /test/stream/mjpeg
        resp = router.scanner.scanner_stream_mjpeg(camera=2)
        self.assertEqual(resp.media_type, "multipart/x-mixed-replace; boundary=FRAME")
        self.assertEqual(mock_dual_cam.active_camera, 2)

        # Consume 2 frames from generator
        gen = resp.generator
        f1 = next(gen)
        self.assertIn(b"--FRAME", f1)
        self.assertIn(b"fakejpeg1", f1)
        mock_dual_cam.add_spectator.assert_called_with(1)

        f2 = next(gen)
        self.assertIn(b"--FRAME", f2)
        self.assertIn(b"fakejpeg2", f2)

        gen.close()
        mock_dual_cam.remove_spectator.assert_called_with(1)

    def test_switch_active_camera(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.active_camera = 1
        self.scanner.dual_cameras = mock_dual_cam

        # Switch via query parameter in /test/stream/active
        res = router.scanner.scanner_stream_switch_active(camera_id=2)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 2)

        # Switch via mock request object
        req = MagicMock(camera_id=1)
        res = router.scanner.scanner_stream_switch_active(req=req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 1)

        # Switch via /scanner/stream/active
        res = router.scanner.scanner_stream_switch_active(camera_id=2)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.active_camera, 2)

    def test_set_stream_quality(self):
        mock_dual_cam = MagicMock()
        mock_dual_cam.stream_quality = 95
        self.scanner.dual_cameras = mock_dual_cam

        # Update via /test/stream/quality
        res = router.scanner.scanner_stream_set_quality(quality=80)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.stream_quality, 80)

        # Update via mock request object
        req = MagicMock(quality=90)
        res = router.scanner.scanner_stream_set_quality(req=req)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.stream_quality, 90)

        # Update via /scanner/stream/quality
        res = router.scanner.scanner_stream_set_quality(quality=75)
        self.assertEqual(res["status"], "success")
        self.assertEqual(mock_dual_cam.stream_quality, 75)

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

        status1 = router.scanner.scanner_stream_sync_status()
        self.assertTrue(status1["ready"])
        self.assertEqual(status1["cam1_seq"], 42)
        self.assertEqual(status1["offset_ms"], 0.05)

        status2 = router.scanner.scanner_stream_sync_status()
        self.assertEqual(status2["cam2_seq"], 42)


class TestRigOwnershipRoutes(unittest.TestCase):
    """While /scanner/prepare or a scan owns the rig, the manual light and snap endpoints are refused (409).
    (Scanner.scan_owns_rig itself is tested in tests/test_rig_ownership.py.)"""

    def setUp(self):
        self.fake = MagicMock()
        self.fake.state = "idle"
        self.fake.scan_owns_rig = False
        self.fake.dual_cameras = None
        patcher = patch.object(router.test, "scanner", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_lights_are_refused_while_a_scan_owns_the_rig(self):
        self.fake.scan_owns_rig = True
        for state in ("preparing", "prepared", "running"):
            self.fake.state = state
            with self.assertRaises(MockHTTPException) as off_ctx:
                router.test.test_lights_disable()
            self.assertEqual(off_ctx.exception.status_code, 409, state)
            with self.assertRaises(MockHTTPException) as on_ctx:
                router.test.test_lights_enable(req=MockBaseModel(brightness=0.5))
            self.assertEqual(on_ctx.exception.status_code, 409, state)
        self.fake.lights.off.assert_not_called()
        self.fake.lights.on.assert_not_called()

    def test_lights_work_when_the_scan_does_not_own_the_rig(self):
        self.assertEqual(router.test.test_lights_disable()["state"], "disabled")
        self.fake.lights.off.assert_called_once_with(force=False)
        self.assertEqual(router.test.test_lights_enable(req=MockBaseModel(brightness=0.5))["state"], "enabled")
        self.fake.lights.set_brightness.assert_called_once_with(0.5)

    def test_a_locked_light_is_a_409_not_a_500(self):
        class Locked(Exception):   # hardware.lights is a mock in this process: use a real exception class
            pass

        self.fake.lights.off.side_effect = Locked("locked")
        with patch.object(router.test, "LightLockedError", Locked):
            with self.assertRaises(MockHTTPException) as ctx:
                router.test.test_lights_disable()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_snap_is_refused_while_a_scan_owns_the_cameras(self):
        self.fake.scan_owns_rig = True
        self.fake.dual_cameras = MagicMock()
        with self.assertRaises(MockHTTPException) as ctx:
            router.test.test_camera_snap()
        self.assertEqual(ctx.exception.status_code, 409)
        self.fake.dual_cameras.capture_photo.assert_not_called()

    def test_snap_releases_the_cameras_it_started(self):
        cams = MagicMock()
        cams.capture_photo.return_value = ["a.jpg", "b.jpg"]

        def start(**kwargs):
            self.fake.dual_cameras = cams

        self.fake.setup_and_stream.side_effect = start
        result = router.test.test_camera_snap()
        self.assertEqual(result["count"], 2)
        self.fake.setup_and_stream.assert_called_once()
        self.fake.stop_stream.assert_called_once()

    def test_snap_goes_to_the_test_snaps_folder_with_a_recognisable_name(self):
        cams = MagicMock()
        cams.capture_photo.return_value = ["a.jpg", "b.jpg"]
        self.fake.dual_cameras = cams
        router.test.test_camera_snap()
        kwargs = cams.capture_photo.call_args.kwargs
        self.assertEqual(os.path.basename(kwargs["output_dir"]), "test_snaps")
        self.assertTrue(kwargs["output_prefix"].startswith("snap_"))
        self.assertIs(kwargs["meshroom_rig"], False)

    def test_snap_releases_the_cameras_even_when_the_capture_fails(self):
        cams = MagicMock()
        cams.capture_photo.side_effect = RuntimeError("no frame")

        def start(**kwargs):
            self.fake.dual_cameras = cams

        self.fake.setup_and_stream.side_effect = start
        with self.assertRaises(MockHTTPException) as ctx:
            router.test.test_camera_snap()
        self.assertEqual(ctx.exception.status_code, 500)
        self.fake.stop_stream.assert_called_once()

    def test_snap_keeps_cameras_that_were_already_running(self):
        self.fake.dual_cameras = MagicMock()
        self.fake.dual_cameras.capture_photo.return_value = ["a.jpg", "b.jpg"]
        router.test.test_camera_snap()
        self.fake.stop_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
