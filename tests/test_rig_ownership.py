import importlib.util
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.test_scanner_motion_safety import make_scanner, scanner_module  # noqa: E402

ScannerState = scanner_module.ScannerState


class ScanOwnsRigTest(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_scan_states_own_the_rig(self):
        for state in (ScannerState.PREPARING, ScannerState.PREPARED, ScannerState.RUNNING):
            self.s.state = state
            self.assertTrue(self.s.scan_owns_rig, state)
        for state in (ScannerState.IDLE, ScannerState.COMPLETED, ScannerState.ERROR, ScannerState.CANCELLED,
                      ScannerState.PROCESSING, ScannerState.DOWNLOADING):
            self.s.state = state
            self.assertFalse(self.s.scan_owns_rig, state)

    def test_a_manual_session_does_not_own_the_rig(self):
        self.s.state = ScannerState.PREPARING
        self.s._manual_session = True
        self.assertFalse(self.s.scan_owns_rig)

    def test_setup_and_stream_marks_manual_sessions_by_the_preflight_flag(self):
        with patch.object(scanner_module, "DualCamera"):
            self.s.setup_and_stream(enable_stream=False, camera_mode="staggered", preflight=False)
        self.assertEqual(self.s.state, ScannerState.PREPARING)
        self.assertTrue(self.s._manual_session)
        self.assertFalse(self.s.scan_owns_rig)


class StopStreamTest(unittest.TestCase):
    def setUp(self):
        self.s = make_scanner()

    def test_a_manual_session_returns_to_idle_with_the_lights_off(self):
        self.s.state = ScannerState.PREPARING
        self.s._manual_session = True
        self.s.stop_stream()
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.phase, "idle")
        self.assertFalse(self.s._manual_session)
        self.assertIsNone(self.s.dual_cameras)
        self.s.lights.off.assert_called()

    def test_a_scan_preparation_is_not_ended_by_stop_stream(self):
        self.s.state = ScannerState.PREPARING
        self.s._manual_session = False
        self.s.stop_stream()
        self.assertEqual(self.s.state, ScannerState.PREPARING)
        self.s.lights.off.assert_not_called()

    def test_a_new_prepare_is_accepted_after_a_manual_stream(self):
        self.s.state = ScannerState.PREPARING
        self.s._manual_session = True
        self.s.stop_stream()
        self.assertIn(self.s.state, ScannerState.PREPARE_ALLOWED)

    def test_cleanup_ends_the_manual_session_flag(self):
        self.s._manual_session = True
        self.s.cleanup()
        self.assertFalse(self.s._manual_session)


class LightOffRespectsTheLockTest(unittest.TestCase):
    """DimmableLight.off(force=False) is the outside-caller form: it never lifts the scan's brightness lock."""

    @classmethod
    def setUpClass(cls):
        # hardware.lights is replaced by a mock in tests/__init__.py: load the real file
        spec = importlib.util.spec_from_file_location(
            "lights_real", os.path.join(os.path.dirname(__file__), "..", "hardware", "lights.py"))
        cls.lights = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"hardware.relay": MagicMock()}):
            spec.loader.exec_module(cls.lights)

    def make_light(self, locked: bool):
        light = object.__new__(self.lights.DimmableLight)
        light._lock = threading.RLock()
        light._closed = False
        light._is_on = True
        light._locked = locked
        light._pwm = MagicMock()
        light._relay = MagicMock(closed=False)
        return light

    def test_a_locked_light_refuses_a_non_forced_off(self):
        light = self.make_light(locked=True)
        with self.assertRaises(self.lights.LightLockedError):
            light.off(force=False)
        self.assertTrue(light._is_on)
        self.assertTrue(light._locked)
        light._relay.off.assert_not_called()

    def make_off_light(self):
        light = self.make_light(locked=False)
        light._is_on = False
        light._brightness = 0.5
        light._relay.is_active = False
        return light

    def test_on_sets_the_flag_only_after_the_relay_and_the_pwm_are_set(self):
        light = self.make_off_light()
        seen = {}
        light._pwm.change_duty_cycle.side_effect = lambda duty: seen.update(flag_during_pwm=light._is_on, duty=duty)
        with patch.object(self.lights.time, "sleep"):
            light.on()
        self.assertFalse(seen["flag_during_pwm"])
        self.assertEqual(seen["duty"], 50.0)
        self.assertTrue(light._is_on)
        light._relay.on.assert_called_once()

    def test_a_failing_pwm_leaves_the_flag_off_and_the_error_visible(self):
        light = self.make_off_light()
        light._pwm.change_duty_cycle.side_effect = OSError("pwm sysfs write failed")
        with patch.object(self.lights.time, "sleep"):
            with self.assertRaises(OSError):
                light.on()
        self.assertFalse(light._is_on)

    def test_a_failing_relay_leaves_the_flag_off_and_never_touches_the_pwm(self):
        light = self.make_off_light()
        light._relay.on.side_effect = OSError("gpio busy")
        with self.assertRaises(OSError):
            light.on()
        self.assertFalse(light._is_on)
        light._pwm.change_duty_cycle.assert_not_called()

    def test_the_scanners_own_off_still_works_and_lifts_the_lock(self):
        light = self.make_light(locked=True)
        light.off()
        self.assertFalse(light._is_on)
        self.assertFalse(light._locked)
        light._relay.off.assert_called_once()

    def test_an_unlocked_light_turns_off_without_force(self):
        light = self.make_light(locked=False)
        light.off(force=False)
        self.assertFalse(light._is_on)


if __name__ == "__main__":
    unittest.main()
