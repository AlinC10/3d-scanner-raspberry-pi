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
if not hasattr(mock_endstop, "TopEndstopTriggered") or isinstance(mock_endstop.TopEndstopTriggered, MagicMock):
    class MockTopEndstopTriggered(Exception): pass
    mock_endstop.TopEndstopTriggered = MockTopEndstopTriggered
if not hasattr(mock_endstop, "BottomEndstopTriggered") or isinstance(mock_endstop.BottomEndstopTriggered, MagicMock):
    class MockBottomEndstopTriggered(Exception): pass
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
        mock_sync_instance.start_stream.return_value = ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]

        with patch("hardware.scanner.DualCameraRPISync", return_value=mock_sync_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="rpi_sync")
            self.assertEqual(urls, ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"])
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
        mock_sync_instance.start_stream.return_value = ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]

        with patch("hardware.scanner.DualCameraRPISync", return_value=mock_sync_instance) as mock_cls:
            urls = self.scanner.setup_and_stream(enable_stream=True, camera_mode="unsupported_mode")
            self.assertEqual(urls, ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"])
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

    def _prepared_scanner(self):
        """A scanner in PREPARING with the hardware steps of finish_preparation mocked; returns the camera mock."""
        mock_dual = MagicMock()
        self.scanner.dual_cameras = mock_dual
        self.scanner.state = ScannerState.PREPARING
        self.scanner.lights.brightness = 100
        self.scanner.lights.is_on = True
        self.scanner.wait_for_light_stable = MagicMock(return_value=True)
        self.scanner.home_z_axis = MagicMock()
        self.scanner.move_z_up_distance = MagicMock(return_value=True)
        return mock_dual

    def test_finish_preparation_climbs_to_the_requested_start_height(self):
        self._prepared_scanner()
        self.scanner.finish_preparation(z_start_mm=12.5, camera_id1=0, camera_id2=1)
        self.scanner.move_z_up_distance.assert_called_once()
        self.assertEqual(self.scanner.move_z_up_distance.call_args.kwargs["distance"], 12.5)
        self.assertEqual(self.scanner.state, ScannerState.PREPARED)

    def test_finish_preparation_start_height_defaults_to_30_mm(self):
        self._prepared_scanner()
        self.scanner.finish_preparation()
        self.assertEqual(self.scanner.move_z_up_distance.call_args.kwargs["distance"], 30.0)

    def test_finish_preparation_with_zero_start_height_does_not_climb(self):
        self._prepared_scanner()
        self.scanner.finish_preparation(z_start_mm=0)
        self.scanner.home_z_axis.assert_called_once()
        self.scanner.move_z_up_distance.assert_not_called()
        self.assertEqual(self.scanner.state, ScannerState.PREPARED)

    def test_finish_preparation_pauses_after_the_homing_before_the_climb(self):
        import time
        import hardware.scanner as scanner_module
        self._prepared_scanner()
        stamps = {}
        self.scanner.home_z_axis = MagicMock(side_effect=lambda: stamps.setdefault("homed", time.monotonic()))
        self.scanner.move_z_up_distance = MagicMock(
            side_effect=lambda **kw: stamps.setdefault("climb", time.monotonic()) or True)
        with patch.object(scanner_module, "Z_HOME_SETTLE_S", 0.3):
            self.scanner.finish_preparation()
        self.assertGreaterEqual(stamps["climb"] - stamps["homed"], 0.29)

    def test_a_cancel_during_the_pause_after_the_homing_ends_the_preparation_without_the_climb(self):
        import threading
        import time
        import hardware.scanner as scanner_module
        self._prepared_scanner()
        threading.Timer(0.1, self.scanner._cancel_event.set).start()
        started = time.monotonic()
        with patch.object(scanner_module, "Z_HOME_SETTLE_S", 5.0):
            self.scanner.finish_preparation()
        self.assertLess(time.monotonic() - started, 2.0)
        self.scanner.move_z_up_distance.assert_not_called()

    def test_finish_preparation_refuses_a_start_height_that_is_not_whole_steps_before_moving(self):
        self._prepared_scanner()
        # the mocked Motor class hands every Scanner the same instance: undo the side effect for the other tests
        self.addCleanup(setattr, self.scanner.z_axis_motor.distance_to_step_conversion, "side_effect", None)
        self.scanner.z_axis_motor.distance_to_step_conversion.side_effect = ValueError("not a whole number of steps")
        self.scanner.finish_preparation(z_start_mm=10.005)
        self.scanner.home_z_axis.assert_not_called()
        self.scanner.move_z_up_distance.assert_not_called()
        self.assertEqual(self.scanner.state, ScannerState.ERROR)

    def test_finish_preparation_forwards_copy_colour_gains_to_a_locking_driver(self):
        mock_dual = self._prepared_scanner()
        self.scanner._lock_auto_supported = True
        self.scanner.finish_preparation(copy_colour_gains=True)
        self.assertIs(mock_dual.prepare_scan.call_args.kwargs["copy_colour_gains"], True)
        self.assertIs(mock_dual.prepare_scan.call_args.kwargs["lock_auto"], True)

    def test_finish_preparation_copy_colour_gains_defaults_to_false(self):
        mock_dual = self._prepared_scanner()
        self.scanner._lock_auto_supported = True
        self.scanner.finish_preparation()
        self.assertIs(mock_dual.prepare_scan.call_args.kwargs["copy_colour_gains"], False)

    def test_copy_colour_gains_is_part_of_the_interface_and_reaches_every_driver(self):
        mock_dual = self._prepared_scanner()
        self.scanner._lock_auto_supported = False
        self.scanner.finish_preparation(copy_colour_gains=True)
        self.assertIs(mock_dual.prepare_scan.call_args.kwargs["copy_colour_gains"], True)
        self.assertNotIn("lock_auto", mock_dual.prepare_scan.call_args.kwargs)

    def test_a_part_that_fails_to_open_releases_the_parts_opened_before_it(self):
        """PERI-7: with the PWM chip missing the pins, the I2C bus and the serial port must not stay claimed."""
        import hardware.scanner as scanner_module
        tof, turntable, z_axis = MagicMock(), MagicMock(), MagicMock()
        up_endstop, down_endstop = MagicMock(), MagicMock()
        with patch.object(scanner_module, "ToFSensor", return_value=tof), \
                patch.object(scanner_module, "Motor", side_effect=[turntable, z_axis]), \
                patch.object(scanner_module, "Endstop", side_effect=[up_endstop, down_endstop]), \
                patch.object(scanner_module, "DimmableLight", side_effect=RuntimeError("no PWM chip")):
            with self.assertRaises(RuntimeError) as ctx:
                Scanner()
        self.assertIn("no PWM chip", str(ctx.exception))
        for part in (tof, turntable, z_axis, up_endstop, down_endstop):
            part.close.assert_called()
        turntable.bridge.close.assert_called_once()

    def test_a_part_that_fails_early_releases_only_what_exists(self):
        import hardware.scanner as scanner_module
        tof = MagicMock()
        with patch.object(scanner_module, "ToFSensor", return_value=tof), \
                patch.object(scanner_module, "Motor", side_effect=RuntimeError("no Arduino")):
            with self.assertRaises(RuntimeError):
                Scanner()
        tof.close.assert_called_once()

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
