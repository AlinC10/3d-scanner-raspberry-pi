#!/usr/bin/env python3
"""Real-hardware tests for the Arducam IMX477 camera controller.

Examples:
    python3 test.py --camera 0
    python3 test.py --camera 1 --output /var/tmp/camera-tests

These tests intentionally do not provide mocked camera, I2C, or image
libraries. Run them on the Raspberry Pi with the camera and VCM connected.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Run real Arducam IMX477 hardware tests"
    )
    parser.add_argument(
        "--camera", type=int, choices=(0, 1), required=True,
        help="CSI camera index to test",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "test-output",
        help="Artifact directory (default: ./test-output)",
    )
    parser.add_argument(
        "--video-duration", type=float, default=3.0,
        help="Video test duration in seconds (default: 3)",
    )
    parser.add_argument(
        "--settle-time", type=float, default=2.0,
        help="AE/AWB settling time in seconds (default: 2)",
    )
    parser.add_argument(
        "--focus-settle", type=float, default=0.1,
        help="Additional wait after focus movement (default: 0.1)",
    )
    args, unittest_args = parser.parse_known_args()
    if args.video_duration <= 0 or args.settle_time < 0 or args.focus_settle < 0:
        parser.error(
            "durations and settling times must be non-negative; "
            "video duration must be positive"
        )
    return args, unittest_args


ARGS, UNITTEST_ARGS = parse_arguments()
RUN_DIR = (
    ARGS.output
    / f"camera{ARGS.camera}"
    / datetime.now().strftime("%Y%m%d_%H%M%S")
)
RUN_DIR.mkdir(parents=True, exist_ok=True)

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(f"Real tests require OpenCV and NumPy: {exc}") from exc

try:
    from camera import ArducamIMX477
    from config.vcm import VCM_MAX_POS, VCM_MIN_POS
except (ImportError, SystemExit) as exc:
    raise SystemExit(
        "Real tests require Raspberry Pi camera dependencies and the VCM driver. "
        f"Import failed: {exc}"
    ) from exc


def jpeg_dimensions(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise AssertionError(f"OpenCV could not decode JPEG: {path}")
    height, width = image.shape[:2]
    return width, height


class ReportingTestResult(unittest.TextTestResult):
    """Print an explicit result for every hardware test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes = []

    def _test_name(self, test):
        return test.id().split(".")[-1]

    def startTest(self, test):
        super().startTest(test)
        self.stream.writeln(f"RUN   {self._test_name(test)}")

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes.append(test)
        self.stream.writeln(f"PASS  {self._test_name(test)}")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        message = str(err[1]).strip() or getattr(err[0], "__name__", "Exception")
        self.stream.writeln(f"FAIL  {self._test_name(test)}: {message}")

    def addError(self, test, err):
        super().addError(test, err)
        message = str(err[1]).strip() or getattr(err[0], "__name__", "Exception")
        self.stream.writeln(f"ERROR {self._test_name(test)}: {message}")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.stream.writeln(f"SKIP  {self._test_name(test)}: {reason}")


class ReportingTestRunner(unittest.TextTestRunner):
    resultclass = ReportingTestResult


class RealCameraTests(unittest.TestCase):
    camera: ArducamIMX477

    @classmethod
    def setUpClass(cls):
        print(f"\nTesting camera {ARGS.camera}; artifacts: {RUN_DIR}")
        cls.camera = ArducamIMX477(camera_id=ARGS.camera)
        cls.original_focus = cls.camera.focuser.position

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "camera"):
            return
        try:
            cls.camera.focus_reset()
        finally:
            cls.camera.close()

    def setUp(self):
        self.assertTrue(hasattr(type(self), "camera"))

    def _photo_path(self, name):
        return RUN_DIR / f"camera{ARGS.camera}_{name}.jpg"

    def _start_preview(self, resolution=None):
        self.camera._configure_preview(resolution)
        self.camera.picam2.start()
        time.sleep(ARGS.settle_time)

    def _stop_camera(self):
        try:
            self.camera.picam2.stop()
        except Exception:
            pass

    def _lock_capture_controls(self):
        self._start_preview((1280, 720))
        try:
            controls = self.camera.lock_auto_features(settle_time=0.0)
            self.assertFalse(controls["AeEnable"])
            self.assertFalse(controls["AwbEnable"])
            return controls
        finally:
            self._stop_camera()

    def test_camera_properties(self):
        properties = self.camera.picam2.camera_properties
        self.assertEqual(self.camera.camera_id, ARGS.camera)
        self.assertTrue(properties, "camera_properties is empty")
        self.assertIn("Model", properties)
        print(f"Camera properties: {properties}")

    def test_configuration_has_no_crop_or_roi(self):
        self.camera._configure_still((2800, 2100))
        still_config = self.camera.picam2.camera_configuration()
        self.assertIn("raw", still_config)
        self.camera._configure_video((1280, 720))
        self.camera._configure_preview((1280, 720))
        config = self.camera.picam2.camera_configuration()
        serialized = repr(config).lower()
        self.assertNotIn("roi", serialized)
        self.assertNotIn("crop", serialized)

    def test_preview_lifecycle_and_metadata(self):
        self._start_preview((1280, 720))
        try:
            metadata = self.camera.picam2.capture_metadata()
            self.assertIn("ExposureTime", metadata)
            self.assertGreater(metadata["ExposureTime"], 0)
            frame = self.camera.picam2.capture_array("main")
            self.assertIsInstance(frame, np.ndarray)
            self.assertGreater(frame.size, 0)
            print(f"Preview frame shape: {frame.shape}; metadata: {metadata}")
        finally:
            self._stop_camera()

    def test_manual_exposure_and_awb_controls(self):
        self._start_preview((1280, 720))
        try:
            controls = self.camera.lock_auto_features(settle_time=0.0)
            self.assertFalse(controls["AeEnable"])
            self.assertFalse(controls["AwbEnable"])
            self.assertIn("ExposureTime", controls)
            self.assertIn("AnalogueGain", controls)
            self.assertIn("ColourGains", controls)
        finally:
            self._stop_camera()

    def test_focus_positions_steps_and_reset(self):
        self.camera.focus_reset()
        self.assertEqual(self.camera.focuser.position, VCM_MIN_POS)
        self.camera.focus_set(512)
        time.sleep(ARGS.focus_settle)
        self.assertEqual(self.camera.focuser.position, 512)
        self.camera.focus_step(25)
        self.assertEqual(self.camera.focuser.position, 537)
        self.camera.focus_step(-25)
        self.assertEqual(self.camera.focuser.position, 512)
        self.camera.focus_set(VCM_MAX_POS)
        self.assertEqual(self.camera.focuser.position, VCM_MAX_POS)
        self.camera.focus_reset()
        self.assertEqual(self.camera.focuser.position, VCM_MIN_POS)

    def test_full_resolution_jpeg_capture(self):
        self._lock_capture_controls()
        output = self._photo_path("full")
        self.camera.capture_photo(
            output=str(output), resolution=self.camera.FULL_RES, quality=95
        )
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 1000)
        self.assertEqual(jpeg_dimensions(output), self.camera.FULL_RES)

    def test_six_megapixel_jpeg_capture(self):
        self._lock_capture_controls()
        resolution = (2800, 2100)
        output = self._photo_path("six_mp")
        self.camera.capture_photo(
            output=str(output), resolution=resolution, quality=95
        )
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 1000)
        self.assertEqual(jpeg_dimensions(output), resolution)

    def test_dng_capture(self):
        self._lock_capture_controls()
        resolution = (2800, 2100)
        normal = self._photo_path("paired_normal")
        output = self._photo_path("paired_raw")
        self.camera.capture_photo(
            output=str(normal), resolution=resolution, quality=95, raw=False
        )
        self.camera.capture_photo(
            output=str(output), resolution=resolution, quality=95, raw=True
        )
        dng = output.with_suffix(".dng")
        normal_image = cv2.imread(str(normal), cv2.IMREAD_UNCHANGED)
        raw_image = cv2.imread(str(output), cv2.IMREAD_UNCHANGED)
        if normal_image is None or raw_image is None:
            self.fail("OpenCV could not decode one of the paired JPEG captures")
        mean_difference = abs(float(normal_image.mean()) - float(raw_image.mean()))
        self.assertLess(
            mean_difference, 20.0,
            f"normal JPEG and RAW-request JPEG differ by {mean_difference:.2f}",
        )
        self.assertTrue(output.is_file())
        self.assertTrue(dng.is_file())
        self.assertGreater(dng.stat().st_size, 1000)

    def test_rotated_capture_dimensions(self):
        resolution = (1280, 960)
        expected_dimensions = {
            0: (1280, 960),
            90: (1280, 960),
            180: (1280, 960),
            270: (1280, 960),
        }
        try:
            for rotation, expected in expected_dimensions.items():
                self.camera.rotation = rotation
                output = self._photo_path(f"rotation_{rotation}")
                self.camera.capture_photo(
                    output=str(output), resolution=resolution, quality=95
                )
                self.assertEqual(jpeg_dimensions(output), expected)
        finally:
            self.camera.rotation = 0

    def test_software_autofocus(self):
        self._start_preview((1280, 720))
        original = self.camera.focuser.position
        try:
            result = self.camera.focus_sweep_autofocus(step=256)
            self.assertGreaterEqual(result, VCM_MIN_POS)
            self.assertLessEqual(result, VCM_MAX_POS)
        finally:
            self.camera.focus_set(original)
            self._stop_camera()

    def test_video_recording(self):
        output = RUN_DIR / f"camera{ARGS.camera}_video.mp4"
        self.camera.record_video(
            output=str(output),
            duration=ARGS.video_duration,
            resolution=(1280, 720),
            quality=25,
        )
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 1000)
        if shutil.which("ffprobe"):
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertGreater(float(result.stdout.strip()), 0)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RealCameraTests)
    runner = ReportingTestRunner(
        stream=sys.stdout, verbosity=0, resultclass=ReportingTestResult
    )
    result = cast(ReportingTestResult, runner.run(suite))
    print("\nTest summary")
    print(f"  Passed: {len(result.successes)}")
    print(f"  Failed: {len(result.failures)}")
    print(f"  Errors: {len(result.errors)}")
    print(f"  Skipped: {len(result.skipped)}")
    print(f"  Total: {result.testsRun}")
    print("\nPassed tests:")
    for test in result.successes:
        print(f"  PASS  {test.id().split('.')[-1]}")
    print("\nFailed tests:")
    for test, _ in result.failures:
        print(f"  FAIL  {test.id().split('.')[-1]}")
    print("\nErrored tests:")
    for test, _ in result.errors:
        print(f"  ERROR {test.id().split('.')[-1]}")
    print("\nSkipped tests:")
    for test, reason in result.skipped:
        print(f"  SKIP  {test.id().split('.')[-1]}: {reason}")
    print(f"  Artifacts: {RUN_DIR}")
    sys.exit(not result.wasSuccessful())
