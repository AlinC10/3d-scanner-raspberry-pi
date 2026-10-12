import unittest
from unittest.mock import MagicMock, patch
import os
import sys
import tempfile
import shutil

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
from hardware.dual_camera_rpi_sync import DualCameraRPISync


class TestE2EScannerRPISync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.scanner = Scanner(camera_mode="rpi_sync")

    def tearDown(self):
        self.scanner.cleanup()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_e2e_setup_and_capture_meshroom_rig(self):
        """
        End-to-end verification of Scanner with DualCameraRPISync:
        1. Initialize scanner with rpi_sync mode
        2. setup_and_stream()
        3. capture_photo() into Meshroom rig structure (0/, 1/)
        4. Validate return paths, directory structure, and clean teardown
        """
        urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="rpi_sync")
        self.assertIsNotNone(urls)
        self.assertEqual(len(urls), 2)
        self.assertIn("camera=1", urls[0])
        self.assertIn("camera=2", urls[1])
        self.assertIsInstance(self.scanner.dual_cameras, DualCameraRPISync)

        # Trigger synchronized capture into tmp_dir
        photo_paths = self.scanner.dual_cameras.capture_photo(
            output_prefix="e2e_shot_0",
            output_dir=self.tmp_dir,
            meshroom_rig=True,
        )

        self.assertEqual(len(photo_paths), 2)
        cam1_path, cam2_path = photo_paths

        # Verify Meshroom stereoscopic folder separation
        self.assertTrue(cam1_path.endswith("0/e2e_shot_0.jpg"))
        self.assertTrue(cam2_path.endswith("1/e2e_shot_0.jpg"))
        self.assertTrue(os.path.exists(cam1_path))
        self.assertTrue(os.path.exists(cam2_path))

        # Cleanup releases resources
        self.scanner.cleanup()
        self.assertIsNone(self.scanner.dual_cameras)
        self.assertIsNone(self.scanner.stream_urls)


if __name__ == "__main__":
    unittest.main()
