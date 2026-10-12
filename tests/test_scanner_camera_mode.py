import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Ensure mocks for platform hardware modules
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "RPi", "RPi.GPIO", "serial",
    "pydantic", "fastapi", "fastapi.responses",
    "cloud", "cloud.cloudflare_r2", "cloud.runpod",
    "system", "system.file_control",
    "hardware.motor", "hardware.endstop", "hardware.lights", "hardware.tof",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Setup Endstop classes
mock_endstop = sys.modules["hardware.endstop"]
class MockTopEndstopTriggered(Exception): pass
class MockBottomEndstopTriggered(Exception): pass
mock_endstop.TopEndstopTriggered = MockTopEndstopTriggered
mock_endstop.BottomEndstopTriggered = MockBottomEndstopTriggered

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware.scanner import Scanner, ScannerState


class TestScannerCameraMode(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner()

    def test_default_camera_mode(self):
        """Verifies default camera_mode is rpi_sync when none specified."""
        self.assertEqual(self.scanner.camera_mode, "rpi_sync")
        self.assertFalse(self.scanner.xvs)

    def test_init_camera_mode_explicit(self):
        """Verifies explicit camera_mode configurations."""
        s_sync = Scanner(camera_mode="rpi_sync")
        self.assertEqual(s_sync.camera_mode, "rpi_sync")
        self.assertFalse(s_sync.xvs)

        s_xvs = Scanner(xvs=True)
        self.assertEqual(s_xvs.camera_mode, "xvs")
        self.assertTrue(s_xvs.xvs)

        s_staggered = Scanner(camera_mode="staggered")
        self.assertEqual(s_staggered.camera_mode, "staggered")
        self.assertFalse(s_staggered.xvs)

    def test_setup_and_stream_selects_rpi_sync(self):
        """Verifies setup_and_stream instantiates DualCameraRPISync by default or parameter."""
        mock_sync_instance = MagicMock()
        mock_sync_instance.is_streaming = True
        mock_sync_instance.start_stream.return_value = ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"]

        with patch("hardware.scanner.DualCameraRPISync", return_value=mock_sync_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="rpi_sync")
            self.assertEqual(urls, ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"])
            self.assertIs(self.scanner.dual_cameras, mock_sync_instance)
            mock_cls.assert_called_once()
            mock_sync_instance.prepare_scan.assert_called_once()

    def test_setup_and_stream_selects_xvs(self):
        """Verifies setup_and_stream instantiates DualCameraXVS when camera_mode='xvs'."""
        mock_xvs_instance = MagicMock()
        mock_xvs_instance.is_streaming = True
        mock_xvs_instance.start_stream.return_value = ["rtsp://localhost:8554/cam1", "rtsp://localhost:8554/cam2"]

        with patch("hardware.scanner.DualCameraXVS", return_value=mock_xvs_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="xvs")
            self.assertEqual(urls, ["rtsp://localhost:8554/cam1", "rtsp://localhost:8554/cam2"])
            self.assertIs(self.scanner.dual_cameras, mock_xvs_instance)
            mock_cls.assert_called_once()
            mock_xvs_instance.prepare_scan.assert_called_once()

    def test_setup_and_stream_selects_staggered(self):
        """Verifies setup_and_stream instantiates DualCamera when camera_mode='staggered'."""
        mock_dual_instance = MagicMock()
        mock_dual_instance.is_streaming = True
        mock_dual_instance.start_stream.return_value = ["rtsp://localhost:8554/cam1", "rtsp://localhost:8554/cam2"]

        with patch("hardware.scanner.DualCamera", return_value=mock_dual_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="staggered")
            self.assertEqual(urls, ["rtsp://localhost:8554/cam1", "rtsp://localhost:8554/cam2"])
            self.assertIs(self.scanner.dual_cameras, mock_dual_instance)
            mock_cls.assert_called_once()
            mock_dual_instance.prepare_scan.assert_called_once()

    def test_setup_and_stream_fallback_on_unknown(self):
        """Verifies setup_and_stream falls back to DualCameraRPISync on unknown mode."""
        mock_sync_instance = MagicMock()
        mock_sync_instance.is_streaming = True
        mock_sync_instance.start_stream.return_value = ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"]

        with patch("hardware.scanner.DualCameraRPISync", return_value=mock_sync_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="unsupported_mode")
            self.assertEqual(urls, ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"])
            self.assertIs(self.scanner.dual_cameras, mock_sync_instance)
            mock_cls.assert_called_once()

    def test_finish_preparation_cleans_camera_mode_kwargs(self):
        """Verifies finish_preparation removes camera_mode and mode from kwargs."""
        mock_dual = MagicMock()
        self.scanner.dual_cameras = mock_dual
        self.scanner.state = ScannerState.PREPARING

        # Mock lights so finish_preparation doesn't block on stabilization
        self.scanner.lights.brightness = 100
        self.scanner.lights.is_on = True
        self.scanner.wait_for_light_stable = MagicMock(return_value=True)
        self.scanner.home_z_axis = MagicMock()
        self.scanner.move_z_up_distance = MagicMock()

        self.scanner.finish_preparation(camera_mode="rpi_sync", mode="rpi_sync", camera_id1=0, camera_id2=1)

        mock_dual.prepare_scan.assert_called_once()
        called_kwargs = mock_dual.prepare_scan.call_args[1]
        self.assertNotIn("camera_mode", called_kwargs)
        self.assertNotIn("mode", called_kwargs)
        self.assertNotIn("camera_id1", called_kwargs)
        self.assertNotIn("camera_id2", called_kwargs)

    def test_cleanup_tears_down_dual_cameras(self):
        """Verifies cleanup cleanly stops stream and closes camera instance."""
        mock_cam = MagicMock()
        self.scanner.dual_cameras = mock_cam
        self.scanner.cleanup()
        mock_cam.stop_stream.assert_called_once()
        mock_cam.close.assert_called_once()
        self.assertIsNone(self.scanner.dual_cameras)


if __name__ == "__main__":
    unittest.main()
