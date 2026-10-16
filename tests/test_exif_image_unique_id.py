import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# tests/__init__.py replaces piexif with a MagicMock; this test needs the real library to read the EXIF back.
_mock_piexif = sys.modules.pop("piexif", None)
try:
    real_piexif = importlib.import_module("piexif")
    from PIL import Image
    REAL_PIEXIF = True
except ImportError:
    real_piexif = None
    REAL_PIEXIF = False
finally:
    if _mock_piexif is not None:
        sys.modules["piexif"] = _mock_piexif

from hardware.camera import camera as camera_module
from hardware.camera.camera import ArducamIMX477


@unittest.skipUnless(REAL_PIEXIF, "piexif/Pillow not installed")
class ImageUniqueIdTest(unittest.TestCase):
    """_process_captured_image writes ImageUniqueID = cam<camera_id>_<file stem> next to the body serial number."""

    def _process(self, camera_id: int, name: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            Image.new("RGB", (16, 16), "white").save(path, "JPEG")
            fake_self = SimpleNamespace(camera_id=camera_id, FULL_RES=ArducamIMX477.FULL_RES, _rotation=0)
            with patch.object(camera_module, "piexif", real_piexif):
                ArducamIMX477._process_captured_image(fake_self, path, ArducamIMX477.FULL_RES, 95)
            return real_piexif.load(path)["Exif"]

    def test_unique_id_matches_rig_folder_and_file_name(self):
        exif0 = self._process(0, "f_1000.jpg")
        exif1 = self._process(1, "f_1000.jpg")
        self.assertEqual(exif0[real_piexif.ExifIFD.ImageUniqueID], b"cam0_f_1000")
        self.assertEqual(exif1[real_piexif.ExifIFD.ImageUniqueID], b"cam1_f_1000")

    def test_body_serial_number_is_kept(self):
        exif = self._process(1, "f_2005.jpg")
        self.assertEqual(exif[real_piexif.ExifIFD.BodySerialNumber], b"cam2")


if __name__ == "__main__":
    unittest.main()
