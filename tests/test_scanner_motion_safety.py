import atexit
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock

# Platform modules that are not available (or must not be touched) off the rig.
for mod in [
    "piexif", "cv2", "smbus2", "picamera2", "RPi", "RPi.GPIO", "serial", "serial.tools", "serial.tools.list_ports",
    "gpiozero", "cloud", "cloud.cloudflare_r2", "cloud.runpod", "system", "system.file_control",
    "hardware.motor", "hardware.lights", "hardware.tof",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


# Real exception classes, so `raise` / `except` work. hardware.endstop is replaced with a tiny stand-in module.
class TopEndstopTriggered(Exception):
    pass


class BottomEndstopTriggered(Exception):
    pass


_endstop_mod = types.ModuleType("hardware.endstop")
_endstop_mod.Endstop = MagicMock()
_endstop_mod.TopEndstopTriggered = TopEndstopTriggered
_endstop_mod.BottomEndstopTriggered = BottomEndstopTriggered
sys.modules["hardware.endstop"] = _endstop_mod

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware import scanner as scanner_module  # noqa: E402
from hardware.scanner import Scanner, ScannerError, ScannerState  # noqa: E402

# hardware.scanner imported the exceptions from whatever hardware.endstop was in sys.modules at that moment
TopEndstopTriggered = scanner_module.TopEndstopTriggered
BottomEndstopTriggered = scanner_module.BottomEndstopTriggered

# hardware.motor is a MagicMock here: give the scanner the real whole-step conversion (1/16 microstepping, 0.1125 deg)
scanner_module.angle_to_steps = lambda angle, step_type=None, step_angle=1.8: round(angle / 0.1125)


def _photo_set(images_dir, level=1, shot=0, content=b"jpeg"):
    """Writes the two files of one capture (camera sub-directories 0/ and 1/) and returns their paths."""
    paths = []
    for cam in (0, 1):
        cam_dir = os.path.join(images_dir, str(cam))
        os.makedirs(cam_dir, exist_ok=True)
        path = os.path.join(cam_dir, f"lvl_{level}_shot_{shot}.jpg")
        with open(path, "wb") as f:
            f.write(content)
        paths.append(path)
    return paths


def make_scanner():
    s = Scanner(camera_mode="staggered")
    # Every scanner gets its own photo directory: scan() writes the manifest and the photos there
    s.images_dir = tempfile.mkdtemp(prefix="scanner_test_images_")
    atexit.register(shutil.rmtree, s.images_dir, True)
    s.turntable_motor = MagicMock()
    s.z_axis_motor = MagicMock()
    s.up_endstop = MagicMock(is_active=False)
    s.down_endstop = MagicMock(is_active=False)
    s.tof = MagicMock()
    s.lights = MagicMock()
    s.dual_cameras = MagicMock()
    # A capture writes two real, non-empty files like the drivers do
    shots = {}

    def capture(output_prefix="dual_photo", output_dir=".", **kwargs):
        shots[output_prefix] = shots.get(output_prefix, 0) + 1
        paths = []
        for cam in (0, 1):
            cam_dir = os.path.join(output_dir, str(cam))
            os.makedirs(cam_dir, exist_ok=True)
            path = os.path.join(cam_dir, f"{output_prefix}.jpg")
            with open(path, "wb") as f:
                f.write(b"jpeg")
            paths.append(path)
        return paths

    s.dual_cameras.capture_photo.side_effect = capture
    s.dual_cameras.last_capture_info = None
    # Real conversions: 1/4 microstepping, 100 steps per mm
    s.z_axis_motor.distance_to_step_conversion.side_effect = lambda distance, step_type=None: round(distance * 100)
    s.turntable_motor.angle_to_steps_conversion.side_effect = lambda angle, step_type=None: round(angle / 0.1125)
    s.turntable_motor.rotate.return_value = True
    s.z_axis_motor.rotate.return_value = True
    return s


class TestEndstopCallbacks(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_callbacks_do_not_stop_when_z_is_idle(self):
        self.s._on_top_endstop_hit()
        self.s._on_bottom_endstop_hit()
        self.s.z_axis_motor.stop.assert_not_called()

    def test_callbacks_stop_while_z_is_moving(self):
        self.s._z_moving.set()
        self.s._on_top_endstop_hit()
        self.s.z_axis_motor.stop.assert_called_once()

    def test_flag_is_set_only_during_the_z_move(self):
        seen = []

        def rotate(**kwargs):
            seen.append(self.s._z_moving.is_set())
            return True

        self.s.z_axis_motor.rotate.side_effect = rotate
        self.s.move_z_up(steps=10)
        self.assertEqual(seen, [True])
        self.assertFalse(self.s._z_moving.is_set())

    def test_flag_is_cleared_when_the_move_raises(self):
        self.s.up_endstop.is_active = True
        with self.assertRaises(TopEndstopTriggered):
            self.s.move_z_up(steps=10)
        self.assertFalse(self.s._z_moving.is_set())


def photo_slice(scanner, detected=False):
    """A stand-in for _capture_turntable_slice that "takes" one photo set, like a real ring does."""
    counter = {"n": 0}

    def _slice(**kwargs):
        counter["n"] += 1
        scanner._register_photos(
            kwargs["images_dir"], _photo_set(kwargs["images_dir"], level=scanner._level, shot=counter["n"]),
            scanner._level, counter["n"],
        )
        return detected

    return _slice


class TestRotationResultIsChecked(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def _slice(self):
        return self.s._capture_turntable_slice(
            images_dir="/tmp/none", angle=18.0, steps=160, delay_turntable=0.001, check_tof=False
        )

    def test_aborted_rotation_raises_when_not_cancelled(self):
        self.s.turntable_motor.rotate.return_value = False
        with self.assertRaises(ScannerError):
            self._slice()

    def test_aborted_rotation_after_cancel_stops_quietly(self):
        def rotate(**kwargs):
            self.s._cancel_event.set()
            return False

        self.s.turntable_motor.rotate.side_effect = rotate
        self._slice()  # must not raise
        self.assertEqual(self.s.turntable_motor.rotate.call_count, 1)


class TestScanSetup(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()
        self.s.state = ScannerState.PREPARED

    def test_invalid_z_move_leaves_scanner_untouched(self):
        self.s.z_axis_motor.distance_to_step_conversion.side_effect = ValueError("not a whole number of steps")
        with self.assertRaises(ValueError):
            self.s.scan(z_move_mm=12.3456)
        self.assertEqual(self.s.state, ScannerState.PREPARED)
        self.s.turntable_motor.enable.assert_not_called()
        self.assertIsNone(self.s._upload_thread)

    def test_setup_failure_still_runs_cleanup(self):
        scanner_module.fc.create_clean_dir.side_effect = OSError("disk full")
        try:
            with self.assertRaises(ScannerError):
                self.s.scan()
            self.s.turntable_motor.stop.assert_called_with(release_torque=True)
            self.s.lights.off.assert_called()
        finally:
            scanner_module.fc.create_clean_dir.side_effect = None


class TestCancelDuringClimb(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()
        self.s.state = ScannerState.PREPARED
        # one ring that always "sees" the object so the scan wants to climb
        self.s._capture_turntable_slice = lambda **kwargs: True

    def test_cancel_during_turntable_settle_does_not_lift_z(self):
        def turntable_disable(*args, **kwargs):
            self.s._cancel_event.set()  # the user cancels while the 0.5 s settle pause is running

        self.s.turntable_motor.disable.side_effect = turntable_disable
        self.s.scan()
        self.s.z_axis_motor.enable.assert_not_called()
        self.s.z_axis_motor.rotate.assert_not_called()

    def test_settle_pauses_are_interruptible(self):
        calls = []

        def turntable_disable(*args, **kwargs):
            calls.append(kwargs.get("interrupt"))
            self.s._cancel_event.set()

        self.s.turntable_motor.disable.side_effect = turntable_disable
        self.s.scan()
        self.assertIs(calls[0], self.s._cancel_event)

    def test_aborted_climb_without_cancel_is_an_error(self):
        self.s.z_axis_motor.rotate.return_value = False
        with self.assertRaises(ScannerError):
            self.s.scan()


class TestHoming(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_homing_succeeds_when_endstop_is_hit(self):
        self.s.down_endstop.is_active = True  # move_z_down raises BottomEndstopTriggered straight away
        self.s.home_z_axis()  # must not raise
        self.s.z_axis_motor.disable.assert_called()

    def test_homing_fails_when_endstop_not_reached(self):
        self.s.z_axis_motor.rotate.return_value = True  # full travel budget done, switch never pressed
        with self.assertRaises(ScannerError):
            self.s.home_z_axis()
        self.s.z_axis_motor.disable.assert_called()  # torque is still released

    def test_homing_propagates_motor_errors(self):
        self.s.z_axis_motor.rotate.side_effect = RuntimeError("serial timeout")
        with self.assertRaises(RuntimeError):
            self.s.home_z_axis()
        self.s.z_axis_motor.disable.assert_called()

    def test_move_to_top_fails_when_endstop_not_reached(self):
        with self.assertRaises(ScannerError):
            self.s.move_z_to_top()

    def test_move_to_top_succeeds_when_endstop_is_hit(self):
        self.s.up_endstop.is_active = True
        self.s.move_z_to_top()  # must not raise

    def test_finish_preparation_goes_to_error_when_homing_fails(self):
        self.s.state = ScannerState.PREPARING
        self.s.z_axis_motor.rotate.return_value = True
        self.s.finish_preparation()
        self.assertEqual(self.s.state, ScannerState.ERROR)

    def test_finish_preparation_ignores_homing_abort_after_cancel(self):
        self.s.state = ScannerState.PREPARING

        def rotate(**kwargs):
            self.s._cancel_event.set()
            return False

        self.s.z_axis_motor.rotate.side_effect = rotate
        self.s.finish_preparation()
        self.assertNotEqual(self.s.state, ScannerState.ERROR)


class TestEndstopWatchdog(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_watchdog_stops_when_the_switch_goes_active_without_an_edge_callback(self):
        stopped = threading.Event()
        self.s.z_axis_motor.stop.side_effect = lambda *a, **k: stopped.set()

        def rotate(**kwargs):
            self.s.up_endstop.is_active = True  # level change only: the gpiozero callback never fires
            self.assertTrue(stopped.wait(1.0), "watchdog did not stop the Z motor")
            return False

        self.s.z_axis_motor.rotate.side_effect = rotate
        with self.assertRaises(TopEndstopTriggered):
            self.s.move_z_up(steps=100)
        self.s.z_axis_motor.stop.assert_called_once()

    def test_callback_and_watchdog_send_a_single_stop(self):
        def rotate(**kwargs):
            self.s.up_endstop.is_active = True
            self.s._on_top_endstop_hit()           # the edge callback fires...
            threading.Event().wait(0.05)           # ...and the watchdog polls a few times afterwards
            return False

        self.s.z_axis_motor.rotate.side_effect = rotate
        with self.assertRaises(TopEndstopTriggered):
            self.s.move_z_up(steps=100)
        self.s.z_axis_motor.stop.assert_called_once()

    def test_stop_budget_is_renewed_for_every_move(self):
        def rotate(**kwargs):
            self.s._on_bottom_endstop_hit()
            return False

        self.s.z_axis_motor.rotate.side_effect = rotate
        for _ in range(2):
            self.s.down_endstop.is_active = False
            self.s.move_z_down(steps=100)
        self.assertEqual(self.s.z_axis_motor.stop.call_count, 2)

    def test_no_watchdog_thread_is_left_running(self):
        before = threading.active_count()
        self.s.move_z_up(steps=100)
        self.assertEqual(threading.active_count(), before)


class TestLowSeverityFixes(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()
        self.s.state = ScannerState.PREPARED

    # 11
    def test_climb_ending_on_top_switch_still_takes_the_final_ring(self):
        slices = MagicMock(side_effect=photo_slice(self.s, True))
        self.s._capture_turntable_slice = slices

        def rotate(**kwargs):
            self.s.up_endstop.is_active = True  # the climb ends exactly on the switch and rotate() still says OK
            return True

        self.s.z_axis_motor.rotate.side_effect = rotate
        self.s.scan()
        self.assertEqual(slices.call_count, 2)
        self.assertFalse(slices.call_args_list[1].kwargs["check_tof"])

    # 13
    def test_end_delay_is_dropped_when_the_turntable_does_not_accelerate(self):
        slices = MagicMock(side_effect=photo_slice(self.s, False))
        self.s._capture_turntable_slice = slices
        self.s.scan(turntable_acceleration=False)
        self.assertIsNone(slices.call_args.kwargs["turntable_end_delay"])

    def test_end_delay_is_kept_when_the_turntable_accelerates(self):
        slices = MagicMock(side_effect=photo_slice(self.s, False))
        self.s._capture_turntable_slice = slices
        self.s.scan(turntable_acceleration=True, turntable_end_delay=0.02)
        self.assertEqual(slices.call_args.kwargs["turntable_end_delay"], 0.02)

    # 14
    def test_missing_cameras_after_cancel_stops_quietly(self):
        self.s.dual_cameras = None
        self.s._cancel_event.set()
        result = self.s._capture_turntable_slice(
            images_dir="/tmp/none", angle=18.0, steps=160, delay_turntable=0.001, check_tof=False
        )
        self.assertFalse(result)

    def test_missing_cameras_without_cancel_is_an_error(self):
        self.s.dual_cameras = None
        with self.assertRaises(ScannerError):
            self.s._capture_turntable_slice(
                images_dir="/tmp/none", angle=18.0, steps=160, delay_turntable=0.001, check_tof=False
            )

    def test_scan_error_caused_by_a_cancel_is_not_raised(self):
        def boom(**kwargs):
            self.s._cancel_event.set()
            raise AttributeError("'NoneType' object has no attribute 'capture_photo'")

        self.s._capture_turntable_slice = boom
        self.s.scan()  # must not raise

    # 15
    def test_finish_state_is_applied_after_a_normal_scan(self):
        self.s._capture_turntable_slice = photo_slice(self.s, False)
        self.s.scan(finish_state=ScannerState.PROCESSING)
        self.assertEqual(self.s.state, ScannerState.PROCESSING)

    def test_default_finish_state_is_idle(self):
        self.s._capture_turntable_slice = photo_slice(self.s, False)
        self.s.scan()
        self.assertEqual(self.s.state, ScannerState.IDLE)

    def test_finish_state_is_not_applied_when_the_scan_fails(self):
        def boom(**kwargs):
            raise RuntimeError("camera exploded")

        self.s._capture_turntable_slice = boom
        with self.assertRaises(ScannerError):
            self.s.scan(finish_state=ScannerState.PROCESSING)
        self.assertNotEqual(self.s.state, ScannerState.PROCESSING)

    def test_finish_state_is_not_applied_after_a_cancel(self):
        def cancelled(**kwargs):
            self.s._cancel_event.set()
            return True

        self.s._capture_turntable_slice = cancelled
        self.s.scan(finish_state=ScannerState.PROCESSING)
        self.assertNotEqual(self.s.state, ScannerState.PROCESSING)


class TestFoundBugs(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_z_driver_is_released_after_the_start_position_move(self):
        self.s.state = ScannerState.PREPARING
        self.s.lights.brightness = 1.0
        self.s.lights.is_on = True
        self.s.wait_for_light_stable = lambda **kwargs: True
        self.s.down_endstop.is_active = True  # homing succeeds immediately
        self.s.finish_preparation()
        self.assertEqual(self.s.state, ScannerState.PREPARED)
        calls = [c[0] for c in self.s.z_axis_motor.mock_calls]
        last_move = max(i for i, name in enumerate(calls) if name == "rotate")
        self.assertIn("disable", calls[last_move + 1:])  # released after the +30 mm move

    def test_z_driver_is_released_when_the_start_position_move_aborts(self):
        self.s.state = ScannerState.PREPARING
        self.s.down_endstop.is_active = True  # homing succeeds immediately
        self.s.z_axis_motor.rotate.return_value = False  # the +30 mm move is aborted
        self.s.finish_preparation()
        self.assertEqual(self.s.state, ScannerState.ERROR)
        calls = [c[0] for c in self.s.z_axis_motor.mock_calls]
        last_move = max(i for i, name in enumerate(calls) if name == "rotate")
        self.assertIn("disable", calls[last_move + 1:])

    def test_late_endstop_callback_after_the_move_is_ignored(self):
        self.s._begin_z_motion()
        self.s._end_z_motion(None)
        self.s._on_top_endstop_hit()
        self.s._stop_z_once()
        self.s.z_axis_motor.stop.assert_not_called()

    def test_stop_is_still_sent_during_the_move(self):
        self.s._begin_z_motion()
        self.s._stop_z_once()
        self.s._stop_z_once()
        self.s.z_axis_motor.stop.assert_called_once()
        self.s._end_z_motion(None)


if __name__ == "__main__":
    unittest.main()
