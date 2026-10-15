import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Ensure piexif and cv2 can be imported on systems without them installed
if "piexif" not in sys.modules:
    try:
        import piexif
    except ImportError:
        sys.modules["piexif"] = MagicMock()

if "cv2" not in sys.modules:
    try:
        import cv2
    except ImportError:
        sys.modules["cv2"] = MagicMock()

if "smbus2" not in sys.modules:
    try:
        import smbus2
    except ImportError:
        sys.modules["smbus2"] = MagicMock()

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.dual_camera_rpi_sync import (
    DualCameraRPISync,
    clamp_framerate,
    SYNC_SERVER,
    SYNC_CLIENT,
)
from hardware.base_dual_camera import BaseDualCamera



def _write_fake_jpeg(path, img=None, params=None):
    """cv2 is a MagicMock under tests/__init__: give imwrite the one side effect capture_photo now verifies (a file with bytes)."""
    with open(path, "wb") as f:
        f.write(b"\xff\xd8fake-jpeg\xff\xd9")
    return True


class TestDualCameraRPISync(unittest.TestCase):
    def setUp(self):
        patcher = patch("hardware.dual_camera_rpi_sync.cv2.imwrite", side_effect=_write_fake_jpeg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clamp_framerate(self):
        # 12 MP mode (4056x3040)
        self.assertEqual(clamp_framerate((4056, 3040), 30), 10)
        self.assertEqual(clamp_framerate((4056, 3040), 10), 10)
        self.assertEqual(clamp_framerate((4056, 3040), 5), 5)
        self.assertEqual(clamp_framerate((4056, 3040), 0), 1)

        # 2x2 binned mode (2028x1520)
        self.assertEqual(clamp_framerate((2028, 1520), 50), 40)
        self.assertEqual(clamp_framerate((2028, 1520), 30), 30)
        self.assertEqual(clamp_framerate((2028, 1520), 10), 10)

    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_initialization_and_inheritance(self, mock_arducam):
        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(
            camera_id1=0,
            camera_id2=1,
            size=(4056, 3040),
            fps=15,  # will be clamped to 10
            quality=90,
            auto_start=False,
        )

        self.assertIsInstance(cam, BaseDualCamera)
        self.assertEqual(cam.fps, 10)
        self.assertEqual(cam.cam1, mock_cam1)
        self.assertEqual(cam.cam2, mock_cam2)
        self.assertEqual(len(cam.cameras), 2)
        self.assertEqual(cam.active_camera, 1)
        self.assertEqual(cam.active, 0)
        self.assertFalse(cam.is_streaming)
        self.assertEqual(cam.stream_quality, 80)
        self.assertEqual(cam.buffer_count, 3)

        # Test active camera toggles
        cam.active_camera = 2
        self.assertEqual(cam.active_camera, 2)
        self.assertEqual(cam.active, 1)

        cam.active = 0
        self.assertEqual(cam.active_camera, 1)
        self.assertEqual(cam.active, 0)

        with self.assertRaises(ValueError):
            cam.active_camera = 3

        with self.assertRaises(ValueError):
            cam.active = 2

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_abstract_contracts(self, mock_arducam):
        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False)

        # Test start_stream returns expected routes
        urls = cam.start_stream()
        self.assertEqual(len(urls), 2)
        self.assertIn("?camera=1", urls[0])
        self.assertIn("?camera=2", urls[1])

        # Test capture_photo returns paths
        paths = cam.capture_photo(output_prefix="test_photo", output_dir="/tmp/test_rig", meshroom_rig=True)
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith("0/test_photo.jpg"))
        self.assertTrue(paths[1].endswith("1/test_photo.jpg"))

        # Test record_video raises NotImplementedError
        with self.assertRaises(NotImplementedError):
            cam.record_video()

        # Test sync status structure
        status = cam.get_sync_status()
        self.assertIn("ready", status)
        self.assertIn("cam1_seq", status)
        self.assertIn("cam2_seq", status)
        self.assertIn("offset_ms", status)
        self.assertIn("stream_fps", status)

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.PICAMERA2_AVAILABLE", True)
    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_pipeline_and_grab_worker(self, mock_arducam):
        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam1._pending_controls = {}
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_cam2._pending_controls = {}
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False)

        # Mock requests
        req1 = MagicMock()
        req1.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1_000_000_000}
        req1.make_array.return_value = MagicMock()

        mock_cam1.picam2.capture_request.return_value = req1
        mock_cam2.picam2.capture_request.return_value = req1

        cam._start_pipeline()
        self.assertTrue(cam.is_streaming)

        # Let the workers run briefly
        import time
        time.sleep(0.05)

        # Verify req.release() was called
        self.assertTrue(req1.release.called)

        # Verify cam1 and cam2 started in correct order (cam2 then cam1)
        mock_cam2.picam2.start.assert_called_once()
        mock_cam1.picam2.start.assert_called_once()

        cam.close()
        self.assertFalse(cam.is_streaming)

    @patch("hardware.dual_camera_rpi_sync.PICAMERA2_AVAILABLE", True)
    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_capture_photo_tier1_strict_sync(self, mock_arducam):
        import tempfile

        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam1._pending_controls = {}
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_cam2._pending_controls = {}
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False)

        # Mock frame generation where sequences hit target_seq
        mock_yuv = MagicMock()
        mock_yuv.shape = (150, 100)

        def make_req(seq, ts):
            r = MagicMock()
            r.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": ts}
            r.make_array.return_value = mock_yuv
            return r

        # Provide sequence numbers advancing: 0, 1, 2...
        seq_counter1 = [0]
        seq_counter2 = [0]

        def req_side_effect1():
            s = seq_counter1[0]
            seq_counter1[0] += 1
            return make_req(s, 1_000_000_000 + s * 100_000_000)

        def req_side_effect2():
            s = seq_counter2[0]
            seq_counter2[0] += 1
            return make_req(s, 1_000_000_000 + s * 100_000_000)

        mock_cam1.picam2.capture_request.side_effect = req_side_effect1
        mock_cam2.picam2.capture_request.side_effect = req_side_effect2

        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = cam.capture_photo(
                output_prefix="sync_test",
                output_dir=tmp_dir,
                meshroom_rig=True,
                sync_timeout=0.5,
            )

            self.assertEqual(len(paths), 2)
            self.assertTrue(os.path.exists(paths[0]))
            self.assertTrue(os.path.exists(paths[1]))

            # Verify _process_captured_image was called for both cameras with the output path
            mock_cam1._process_captured_image.assert_called_once()
            mock_cam2._process_captured_image.assert_called_once()
            self.assertEqual(mock_cam1._process_captured_image.call_args[0][0], paths[0])
            self.assertEqual(mock_cam2._process_captured_image.call_args[0][0], paths[1])

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.PICAMERA2_AVAILABLE", True)
    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_capture_photo_tier2_fallback(self, mock_arducam):
        import tempfile

        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam1._pending_controls = {}
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_cam2._pending_controls = {}
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False)

        mock_yuv = MagicMock()
        mock_yuv.shape = (150, 100)

        # Cam1 advances normally, Cam2 stalls or lags so Tier 1 times out
        req1 = MagicMock()
        req1.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1_000_000_000}
        req1.make_array.return_value = mock_yuv

        req2 = MagicMock()
        req2.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1_000_000_000}
        req2.make_array.return_value = mock_yuv

        mock_cam1.picam2.capture_request.return_value = req1
        mock_cam2.picam2.capture_request.return_value = req2

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Short timeout to trigger Tier 2 fallback immediately
            paths = cam.capture_photo(
                output_prefix="fallback_test",
                output_dir=tmp_dir,
                meshroom_rig=True,
                sync_timeout=0.05,
            )

            self.assertEqual(len(paths), 2)
            self.assertTrue(os.path.exists(paths[0]))
            self.assertTrue(os.path.exists(paths[1]))

            # Guaranteed delivery: both photos delivered and processed
            mock_cam1._process_captured_image.assert_called_once()
            mock_cam2._process_captured_image.assert_called_once()

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.PICAMERA2_AVAILABLE", True)
    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_capture_photo_raw_dng(self, mock_arducam):
        import tempfile

        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam1._pending_controls = {}
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_cam2._pending_controls = {}
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False)

        mock_yuv = MagicMock()
        mock_yuv.shape = (150, 100)

        req1 = MagicMock()
        req1.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1_000_000_000}
        req1.make_array.return_value = mock_yuv

        req2 = MagicMock()
        req2.get_metadata.return_value = {"SyncReady": True, "SensorTimestamp": 1_000_000_000}
        req2.make_array.return_value = mock_yuv

        mock_cam1.picam2.capture_request.return_value = req1
        mock_cam2.picam2.capture_request.return_value = req2

        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = cam.capture_photo(
                output_prefix="raw_test",
                output_dir=tmp_dir,
                meshroom_rig=True,
                raw=True,
                sync_timeout=0.05,
            )

            self.assertEqual(len(paths), 2)
            # Verify req.acquire() was called during grab loop
            self.assertTrue(req1.acquire.called or req2.acquire.called)
            # Verify save_dng was called
            self.assertTrue(req1.save_dng.called or req2.save_dng.called)

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_spectator_tracking_and_stream_quality(self, mock_arducam):
        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(auto_start=False, stream_quality=85)
        self.assertEqual(cam.stream_quality, 85)
        self.assertEqual(cam.buffer_count, 3)

        # Test stream_quality property
        cam.stream_quality = 75
        self.assertEqual(cam.stream_quality, 75)
        # Quality clamping
        cam.stream_quality = 150
        self.assertEqual(cam.stream_quality, 100)
        cam.stream_quality = -10
        self.assertEqual(cam.stream_quality, 1)

        # Test spectator tracking
        self.assertFalse(cam.has_spectators())
        self.assertFalse(cam.has_spectators(0))
        self.assertFalse(cam.has_spectators(1))

        cam.add_spectator(0)
        self.assertTrue(cam.has_spectators())
        self.assertTrue(cam.has_spectators(0))
        self.assertFalse(cam.has_spectators(1))

        cam.add_spectator(1)
        self.assertTrue(cam.has_spectators(1))

        cam.remove_spectator(0)
        self.assertFalse(cam.has_spectators(0))
        self.assertTrue(cam.has_spectators(1))

        cam.remove_spectator(1)
        self.assertFalse(cam.has_spectators())

        # Test wait_jpeg signature flexibility
        # Set dummy jpeg bytes in both slots
        with cam._jpeg_cond:
            cam._jpegs[0] = b"cam0_jpeg"
            cam._jpeg_ids[0] = 5
            cam._jpegs[1] = b"cam1_jpeg"
            cam._jpeg_ids[1] = 8
            cam._jpeg = b"cam0_jpeg"
            cam._jpeg_id = 5

        # 1. Legacy call: wait_jpeg(last_id)
        fid, data = cam.wait_jpeg(last_id=4)
        self.assertEqual(fid, 5)
        self.assertEqual(data, b"cam0_jpeg")

        # 2. Per-camera call: wait_jpeg(camera_idx, last_id)
        fid1, data1 = cam.wait_jpeg(1, last_id=7)
        self.assertEqual(fid1, 8)
        self.assertEqual(data1, b"cam1_jpeg")

        # 3. Keyword camera_idx call
        fid0, data0 = cam.wait_jpeg(camera_idx=0, last_id=4)
        self.assertEqual(fid0, 5)
        self.assertEqual(data0, b"cam0_jpeg")

        cam.close()

    @patch("hardware.dual_camera_rpi_sync.ArducamIMX477")
    def test_modulo_offset_calculation(self, mock_arducam):
        mock_cam1 = MagicMock()
        mock_cam1.camera_id = 0
        mock_cam2 = MagicMock()
        mock_cam2.camera_id = 1
        mock_arducam.side_effect = [mock_cam1, mock_cam2]

        cam = DualCameraRPISync(fps=10, auto_start=False)
        # 10 fps -> period_ns = 100,000,000 ns (100 ms)

        # Simulate camera 1 at ts = 1,000,000,000 ns
        # Simulate camera 2 at ts = 1,100,028,000 ns (raw difference = 100.028 ms, which is 1 frame + 28 us)
        with cam._cond:
            cam._ts_hist[0][100] = 1_000_000_000
            cam._ts_hist[1][100] = 1_100_028_000

        status = cam.get_sync_status()
        # Modulo arithmetic should yield +0.028 ms instead of 100.028 ms
        self.assertIsNotNone(status["offset_ms"])
        self.assertAlmostEqual(status["offset_ms"], 0.028, places=3)
        self.assertIn("stream_quality", status)
        self.assertIn("spectators", status)

        cam.close()


if __name__ == "__main__":
    unittest.main()


