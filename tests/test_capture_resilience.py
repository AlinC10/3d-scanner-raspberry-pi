"""
R3 / R6: a camera thread must survive a bad frame, a stalled sensor must end in a clear error after a timeout, and a
failed photo pair is retaken (see also TestPairRetry in test_fs_scanner.py).
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import hardware.dual_camera_rpi_sync as sync_mod
from hardware.base_dual_camera import CaptureError
from hardware.camera.camera import ArducamIMX477, CameraTimeoutError
from tests.test_fs_cameras import RealModeCase, SIZE


class GrabWorkerResilienceTest(RealModeCase):
    def wrap_capture_request(self, drv, cam_idx, wrapper):
        picam2 = drv.cameras[cam_idx].picam2
        original = picam2.capture_request
        picam2.capture_request = lambda wait=None: wrapper(original, wait)

    def test_a_frame_that_raises_does_not_kill_the_worker(self):
        drv = self.new_driver()
        seen = {"n": 0}

        def flaky(original, wait):
            req = original(wait=wait)
            seen["n"] += 1
            if seen["n"] <= 3:     # e.g. a MemoryError while copying a 12 MP frame
                req.get_metadata = MagicMock(side_effect=MemoryError("copy failed"))
            return req

        self.wrap_capture_request(drv, 0, flaky)
        paths = drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=1.0)
        self.assertEqual(len(paths), 2)
        self.assertGreater(seen["n"], 3)
        self.assertEqual(drv._worker_errors, [None, None])

    def test_a_camera_without_frames_is_reported_dead_and_the_capture_fails_fast(self):
        drv = self.new_driver()

        def stalled(original, wait):
            raise TimeoutError("timed out")

        self.wrap_capture_request(drv, 1, stalled)
        started = time.monotonic()
        with self.assertRaises(CaptureError) as ctx:
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=0.05)
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertIn("camera 2", str(ctx.exception))
        self.assertIn("no frame for", str(ctx.exception))
        self.assertIsNotNone(drv._worker_errors[1])
        self.assertIsNone(drv._worker_errors[0])

    def test_once_a_worker_is_dead_every_capture_fails_at_once(self):
        drv = self.new_driver()
        drv._start_pipeline()
        drv._worker_errors[0] = "no frame for 60 s"
        started = time.monotonic()
        with self.assertRaises(CaptureError) as ctx:
            drv.capture_photo(output_prefix="shot", output_dir=self.tmp)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("stopped delivering frames", str(ctx.exception))

    def test_repeated_errors_end_in_a_dead_worker(self):
        drv = self.new_driver()

        def failing(original, wait):
            raise RuntimeError("ioctl failed")

        self.wrap_capture_request(drv, 1, failing)
        with patch.object(sync_mod, "GRAB_MAX_CONSECUTIVE_ERRORS", 5):
            with self.assertRaises(CaptureError) as ctx:
                drv.capture_photo(output_prefix="shot", output_dir=self.tmp, sync_timeout=0.05)
        self.assertIn("failed 5 times in a row", str(ctx.exception))

    def test_a_restart_clears_the_dead_marks(self):
        drv = self.new_driver()
        drv._worker_errors = ["boom", None]
        drv._start_pipeline()
        self.assertEqual(drv._worker_errors, [None, None])


class MetadataTimeoutTest(unittest.TestCase):
    def make_camera(self, side_effect=None, result=None):
        cam = object.__new__(ArducamIMX477)
        cam.camera_id = 1
        cam._pending_controls = {}
        cam._last_metadata = None
        cam.picam2 = MagicMock(started=True)
        cam.picam2.capture_metadata.side_effect = side_effect
        if result is not None:
            cam.picam2.capture_metadata.return_value = result
        return cam

    def test_the_call_gets_the_timeout(self):
        cam = self.make_camera(result={"ExposureTime": 1000})
        self.assertEqual(cam._capture_metadata(timeout=5), {"ExposureTime": 1000})
        cam.picam2.capture_metadata.assert_called_once_with(wait=5)

    def test_the_default_timeout_is_the_configured_one(self):
        from hardware.camera.config import CAMERA_CALL_TIMEOUT_S
        cam = self.make_camera(result={})
        cam._capture_metadata()
        cam.picam2.capture_metadata.assert_called_once_with(wait=CAMERA_CALL_TIMEOUT_S)
        self.assertEqual(CAMERA_CALL_TIMEOUT_S, 60.0)

    def test_a_stalled_sensor_raises_a_camera_timeout_error(self):
        cam = self.make_camera(side_effect=TimeoutError("timed out"))
        with self.assertRaises(CameraTimeoutError) as ctx:
            cam._capture_metadata(timeout=2)
        self.assertIn("Camera 1", str(ctx.exception))
        self.assertIn("2 s", str(ctx.exception))
        self.assertTrue(issubclass(CameraTimeoutError, RuntimeError))

    def test_locking_auto_features_fails_with_the_timeout_error(self):
        cam = self.make_camera(side_effect=TimeoutError("timed out"))
        with self.assertRaises(CameraTimeoutError):
            cam.lock_auto_features(settle_time=0)
        cam.picam2.set_controls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
