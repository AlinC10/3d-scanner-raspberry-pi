"""
WS-D (scanner core) tests, all with mocked hardware:
  D1 motion_guard      D2 cancel before a move   D3 stop order        D4 _stopping mitigation
  D5 exception safety  D6 late callback          D7 explicit enable   D8 zero photos
  D9 travel limit      D10 shots per ring        D11 Z idle policy    D12 router mapping
"""
import os
import threading
import types
import unittest
from unittest.mock import MagicMock, call, patch

# Importing this module installs the module stubs and the stand-in hardware.endstop used by make_scanner()
from tests.test_scanner_motion_safety import make_scanner, scanner_module, photo_slice
from tests.test_medium_fixes import _load, _web_stubs, _router_package, _HTTPException, _State, _Response

Scanner = scanner_module.Scanner
ScannerError = scanner_module.ScannerError
ScannerBusyError = scanner_module.ScannerBusyError
ScannerStoppingError = scanner_module.ScannerStoppingError
ScannerState = scanner_module.ScannerState
TopEndstopTriggered = scanner_module.TopEndstopTriggered

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestMotionGuard(unittest.TestCase):
    """D1"""

    def setUp(self):
        self.s = make_scanner()

    def _hold_in_thread(self, hold: threading.Event, entered: threading.Event):
        def run():
            with self.s.motion_guard():
                entered.set()
                hold.wait(2.0)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(entered.wait(1.0))
        return t

    def test_overlapping_z_moves_second_is_refused_and_protection_survives(self):
        started, release = threading.Event(), threading.Event()

        def rotate(**kwargs):
            started.set()
            release.wait(2.0)
            return True

        self.s.z_axis_motor.rotate.side_effect = rotate
        first = threading.Thread(target=lambda: self.s.move_z_up(steps=100), daemon=True)
        first.start()
        self.assertTrue(started.wait(1.0))

        with self.assertRaises(ScannerBusyError):
            self.s.move_z_up(steps=100)
        with self.assertRaises(ScannerBusyError):
            self.s.move_z_down(steps=100)
        self.assertEqual(self.s.z_axis_motor.rotate.call_count, 1)  # nothing was sent for the refused moves
        # The refused attempts must not have cleared the protection of the move in flight
        self.assertTrue(self.s._z_moving.is_set())
        self.s._on_top_endstop_hit()
        self.s.z_axis_motor.stop.assert_called_once()

        release.set()
        first.join(2.0)
        self.assertFalse(self.s._z_moving.is_set())

    def test_endstop_edge_still_triggers_stop_after_the_overlap(self):
        started, release = threading.Event(), threading.Event()

        def slow_rotate(**kwargs):
            started.set()
            release.wait(2.0)
            return True

        self.s.z_axis_motor.rotate.side_effect = slow_rotate
        first = threading.Thread(target=lambda: self.s.move_z_up(steps=100), daemon=True)
        first.start()
        started.wait(1.0)
        with self.assertRaises(ScannerBusyError):
            self.s.move_z_up(steps=100)
        release.set()
        first.join(2.0)

        def rotate(**kwargs):
            self.s._on_top_endstop_hit()  # the edge callback of a later, normal move
            return False

        self.s.z_axis_motor.stop.reset_mock()
        self.s.z_axis_motor.rotate.side_effect = rotate
        self.s.move_z_up(steps=100)
        self.s.z_axis_motor.stop.assert_called_once()

    def test_guard_is_reentrant_for_the_same_thread(self):
        with self.s.motion_guard():
            with self.s.motion_guard():
                self.s.move_z_up(steps=10)  # nested use inside one scan must not deadlock or raise
            self.s.move_z_down(steps=10)
        # fully released afterwards: another thread can take it
        result = []
        t = threading.Thread(target=lambda: result.append(self._try_guard()))
        t.start()
        t.join(1.0)
        self.assertEqual(result, [True])

    def _try_guard(self):
        try:
            with self.s.motion_guard():
                return True
        except ScannerBusyError:
            return False

    def test_other_thread_gets_busy_error_while_held(self):
        hold, entered = threading.Event(), threading.Event()
        t = self._hold_in_thread(hold, entered)
        try:
            with self.assertRaises(ScannerBusyError):
                with self.s.motion_guard():
                    pass
        finally:
            hold.set()
            t.join(1.0)
        self.assertTrue(self._try_guard())

    def test_guard_is_released_when_the_move_raises(self):
        self.s.up_endstop.is_active = True
        with self.assertRaises(TopEndstopTriggered):
            self.s.move_z_up(steps=10)
        self.assertTrue(self._try_guard())

    def test_homing_and_move_to_top_are_refused_while_busy(self):
        hold, entered = threading.Event(), threading.Event()
        t = self._hold_in_thread(hold, entered)
        try:
            with self.assertRaises(ScannerBusyError):
                self.s.home_z_axis()
            with self.assertRaises(ScannerBusyError):
                self.s.move_z_to_top()
            with self.assertRaises(ScannerBusyError):
                self.s.move_z_up_distance(distance=1.0)
        finally:
            hold.set()
            t.join(1.0)
        self.s.z_axis_motor.rotate.assert_not_called()
        self.s.z_axis_motor.enable.assert_not_called()

    def test_turntable_rotation_is_guarded(self):
        hold, entered = threading.Event(), threading.Event()
        t = self._hold_in_thread(hold, entered)
        try:
            with self.assertRaises(ScannerBusyError):
                self.s._capture_turntable_slice(
                    images_dir="/tmp/none", angle=18.0, steps=160, delay_turntable=0.001, check_tof=False
                )
        finally:
            hold.set()
            t.join(1.0)
        self.s.turntable_motor.rotate.assert_not_called()

    def test_busy_error_is_a_scanner_error(self):
        self.assertTrue(issubclass(ScannerBusyError, ScannerError))


class TestCancelBeforeMove(unittest.TestCase):
    """D2"""

    def setUp(self):
        self.s = make_scanner()

    def test_cancel_during_a_scan_blocks_the_move_without_sending_anything(self):
        self.s.state = ScannerState.RUNNING
        self.s._cancel_event.set()
        self.assertFalse(self.s.move_z_up(steps=100))
        self.assertFalse(self.s.move_z_down(steps=100))
        self.s.z_axis_motor.rotate.assert_not_called()
        self.assertFalse(self.s._z_moving.is_set())

    def test_cancel_blocks_the_move_for_the_prepare_thread_whatever_the_state(self):
        # emergency_stop() publishes CANCELLED first: the straggler thread must still be stopped
        self.s.state = ScannerState.CANCELLED
        self.s._cancel_event.set()
        self.s._tls.automated = True
        self.assertFalse(self.s.move_z_up_distance(distance=30.0))
        self.s.z_axis_motor.rotate.assert_not_called()

    def test_stale_cancel_flag_does_not_block_a_manual_test_move(self):
        # after an emergency_stop() the flag stays set until the next prepare/scan; /test moves must still work
        self.s.state = ScannerState.CANCELLED
        self.s._cancel_event.set()
        self.assertTrue(self.s.move_z_up_distance(distance=1.0))
        self.s.z_axis_motor.rotate.assert_called_once()
        self.s.state = ScannerState.IDLE
        self.assertTrue(self.s.move_z_down_distance(distance=1.0))
        self.assertEqual(self.s.z_axis_motor.rotate.call_count, 2)

    def test_stale_cancel_flag_does_not_block_manual_homing(self):
        self.s.state = ScannerState.CANCELLED
        self.s._cancel_event.set()
        self.s.down_endstop.is_active = True
        self.s.home_z_axis()  # must not raise

    def test_home_and_move_to_top_check_cancel_before_the_first_command(self):
        self.s.state = ScannerState.PREPARING
        self.s._cancel_event.set()
        with self.assertRaises(ScannerError):
            self.s.home_z_axis()
        with self.assertRaises(ScannerError):
            self.s.move_z_to_top()
        self.assertEqual(self.s.z_axis_motor.mock_calls, [])  # no ENABLE, no ROTATE, no DISABLE

    def test_finish_preparation_does_nothing_when_cancelled_first(self):
        self.s.state = ScannerState.CANCELLED
        self.s._cancel_event.set()
        self.s.finish_preparation()
        self.assertEqual(self.s.z_axis_motor.mock_calls, [])
        self.assertEqual(self.s.state, ScannerState.CANCELLED)
        self.assertFalse(getattr(self.s._tls, "automated", False))

    def test_cancel_between_precheck_and_send_does_not_start_the_move(self):
        # the cancel lands after the caller's own check: _begin_z_motion() sees it under _z_stop_lock
        self.s.state = ScannerState.RUNNING
        real_begin = self.s._begin_z_motion

        def begin_after_cancel():
            self.s._cancel_event.set()
            return real_begin()

        self.s._begin_z_motion = begin_after_cancel
        self.assertFalse(self.s.move_z_up(steps=100))
        self.s.z_axis_motor.rotate.assert_not_called()

    def test_scan_does_not_lift_when_cancel_lands_before_the_climb_move(self):
        self.s.state = ScannerState.PREPARED
        self.s._capture_turntable_slice = photo_slice(self.s, True)
        # cancel appears right when the Z driver is energized (after the loop's own cancel checks)
        self.s.z_axis_motor.enable.side_effect = lambda *a, **k: self.s._cancel_event.set()
        self.s.scan()
        self.s.z_axis_motor.rotate.assert_not_called()


class TestEmergencyStopOrder(unittest.TestCase):
    """D3"""

    def test_both_motors_get_stop_repeat_2_before_any_disable(self):
        s = make_scanner()
        manager = MagicMock()
        manager.attach_mock(s.turntable_motor, "turntable")
        manager.attach_mock(s.z_axis_motor, "z")
        s.emergency_stop()

        names = [c[0] for c in manager.mock_calls]
        self.assertEqual(names[:2], ["turntable.stop", "z.stop"])
        self.assertEqual(manager.mock_calls[0], call.turntable.stop(repeat=2))
        self.assertEqual(manager.mock_calls[1], call.z.stop(repeat=2))
        self.assertIn("turntable.disable", names)
        self.assertIn("z.disable", names)
        first_disable = min(i for i, n in enumerate(names) if n.endswith(".disable"))
        self.assertGreaterEqual(first_disable, 2)

    def test_cancel_flag_is_set_before_the_motors_are_stopped(self):
        s = make_scanner()
        seen = []
        s.turntable_motor.stop.side_effect = lambda *a, **k: seen.append(s._cancel_event.is_set())
        s.emergency_stop()
        self.assertEqual(seen[0], True)


class TestStoppingMitigation(unittest.TestCase):
    """D4"""

    def setUp(self):
        self.s = make_scanner()
        self.s.state = ScannerState.RUNNING

    def test_cancelled_is_published_first_and_stopping_is_raised(self):
        seen = {}

        def first_stop(*args, **kwargs):
            seen.setdefault("state", self.s.state)
            seen.setdefault("stopping", self.s._stopping)

        self.s.turntable_motor.stop.side_effect = first_stop
        self.s.emergency_stop()
        self.assertEqual(seen, {"state": ScannerState.CANCELLED, "stopping": True})
        self.assertFalse(self.s._stopping)
        self.assertEqual(self.s.state, ScannerState.CANCELLED)

    def test_prepare_during_emergency_stop_is_refused_and_keeps_the_cancel_flag(self):
        outcome = {}

        def during_stop(*args, **kwargs):
            if "refused" in outcome:
                return
            try:
                self.s.setup_and_stream(enable_stream=False)
                outcome["refused"] = False
            except RuntimeError as e:
                outcome["refused"] = True
                outcome["error"] = e
            outcome["cancel_set"] = self.s._cancel_event.is_set()
            outcome["state"] = self.s.state

        self.s.turntable_motor.stop.side_effect = during_stop
        self.s.emergency_stop()
        self.assertTrue(outcome["refused"])
        self.assertTrue(outcome["cancel_set"])
        self.assertEqual(outcome["state"], ScannerState.CANCELLED)
        self.assertIsInstance(outcome["error"], ScannerStoppingError)
        self.assertTrue(outcome["error"].conflict)

    def test_cleanup_does_not_change_the_state_while_stopping(self):
        self.s.state = ScannerState.RUNNING
        self.s._stopping = True
        self.s.cleanup()
        self.assertEqual(self.s.state, ScannerState.RUNNING)
        self.s._stopping = False
        self.s.cleanup()
        self.assertEqual(self.s.state, ScannerState.IDLE)

    def test_prepare_is_allowed_again_after_the_emergency_stop(self):
        self.s.emergency_stop()
        self.assertFalse(self.s._stopping)
        with patch.object(scanner_module, "DualCamera") as camera_class:
            camera_class.return_value = MagicMock()
            self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.s.state, ScannerState.PREPARING)
        self.assertFalse(self.s._cancel_event.is_set())

    def test_overlapping_emergency_stops_keep_the_flag_until_the_last_one_ends(self):
        inner = {}

        def nested(*args, **kwargs):
            if inner:
                return
            inner["done"] = True
            self.s.emergency_stop()  # a second stop (e.g. shutdown) nested inside the first
            inner["stopping_after_nested"] = self.s._stopping

        self.s.turntable_motor.stop.side_effect = nested
        self.s.emergency_stop()
        self.assertTrue(inner["stopping_after_nested"])
        self.assertFalse(self.s._stopping)


class TestTeardownIsExceptionSafe(unittest.TestCase):
    """D5"""

    def test_emergency_stop_survives_a_failing_motor(self):
        s = make_scanner()
        s.state = ScannerState.RUNNING
        cameras = s.dual_cameras
        s.turntable_motor.stop.side_effect = RuntimeError("serial gone")
        s.emergency_stop()  # must not raise
        s.z_axis_motor.stop.assert_any_call(repeat=2)
        s.z_axis_motor.disable.assert_called()
        s.lights.off.assert_called()
        cameras.close.assert_called()
        self.assertIsNone(s.dual_cameras)
        self.assertEqual(s.state, ScannerState.CANCELLED)
        self.assertFalse(s._stopping)
        self.assertTrue(s._cancel_event.is_set())

    def test_emergency_stop_survives_failing_lights_and_cameras(self):
        s = make_scanner()
        cameras = s.dual_cameras
        s.lights.unlock_brightness.side_effect = RuntimeError("relay")
        s.lights.off.side_effect = RuntimeError("relay")
        cameras.stop_stream.side_effect = RuntimeError("camera busy")
        s.emergency_stop()
        cameras.close.assert_called()  # still attempted after stop_stream failed
        # the final STOP is still sent to both motors
        self.assertGreaterEqual(s.turntable_motor.stop.call_count, 2)
        self.assertGreaterEqual(s.z_axis_motor.stop.call_count, 2)
        self.assertEqual(s.state, ScannerState.CANCELLED)

    def test_cleanup_handles_every_step_even_when_the_first_fails(self):
        s = make_scanner()
        s.state = ScannerState.RUNNING
        cameras = s.dual_cameras
        s.turntable_motor.stop.side_effect = RuntimeError("serial gone")
        s.lights.off.side_effect = RuntimeError("relay")
        s.cleanup()  # must not raise
        s.z_axis_motor.stop.assert_called_with(release_torque=True)
        s.lights.unlock_brightness.assert_called()
        cameras.close.assert_called()
        self.assertIsNone(s.dual_cameras)
        self.assertEqual(s.state, ScannerState.IDLE)

    def test_cleanup_keeps_terminal_states(self):
        s = make_scanner()
        s.state = ScannerState.ERROR
        s.cleanup()
        self.assertEqual(s.state, ScannerState.ERROR)


class TestLateCallback(unittest.TestCase):
    """D6"""

    def test_flag_is_cleared_before_the_watcher_is_joined(self):
        s = make_scanner()
        s._begin_z_motion()
        seen = {}

        class Thread:
            def join(self, timeout=None):
                seen["moving_during_join"] = s._z_moving.is_set()
                s._on_top_endstop_hit()  # a late edge arriving while the watcher is being joined

        s._end_z_motion((threading.Event(), Thread()))
        self.assertFalse(seen["moving_during_join"])
        s.z_axis_motor.stop.assert_not_called()


class TestExplicitEnable(unittest.TestCase):
    """D7, D11"""

    def test_z_driver_is_enabled_explicitly_before_the_start_position_move(self):
        s = make_scanner()
        s.state = ScannerState.PREPARING
        s.lights.brightness = 1.0
        s.lights.is_on = True
        s.wait_for_light_stable = lambda **kwargs: True
        s.down_endstop.is_active = True  # homing ends immediately
        s.finish_preparation()
        names = [c[0] for c in s.z_axis_motor.mock_calls]
        last_rotate = max(i for i, n in enumerate(names) if n == "rotate")
        self.assertIn("enable", names[:last_rotate])
        # the enable that precedes the +30 mm move comes after the homing disable
        last_enable = max(i for i, n in enumerate(names[:last_rotate]) if n == "enable")
        homing_disable = min(i for i, n in enumerate(names) if n == "disable")
        self.assertGreater(last_enable, homing_disable)
        # Z is released again once it has moved (it is energized only while it moves)
        self.assertIn("disable", names[last_rotate + 1:])

    def test_no_hold_torque_flag_exists(self):
        for name in os.listdir(os.path.join(ROOT, "hardware")):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, "hardware", name), encoding="utf-8") as f:
                    self.assertNotIn("Z_HOLD_TORQUE", f.read(), name)


class TestZeroPhotoScan(unittest.TestCase):
    """D8"""

    def setUp(self):
        self.s = make_scanner()
        self.s.state = ScannerState.PREPARED

    def test_scan_with_the_top_endstop_active_at_start_is_an_error(self):
        self.s.up_endstop.is_active = True
        with self.assertRaises(ScannerError):
            self.s.scan()
        self.assertNotEqual(self.s.state, ScannerState.COMPLETED)

    def test_cancelled_scan_without_photos_is_not_an_error(self):
        self.s._cancel_event.set()

        def cancelled(**kwargs):
            self.s._cancel_event.set()
            return False

        self.s._capture_turntable_slice = cancelled
        self.s.scan()  # must not raise

    def test_scan_with_photos_is_fine(self):
        self.s._capture_turntable_slice = photo_slice(self.s, False)
        self.s.scan()
        self.assertEqual(self.s.total_photos, 2)  # one capture = the photo of each camera


class TestTravelLimit(unittest.TestCase):
    """D9"""

    def setUp(self):
        self.s = make_scanner()

    @staticmethod
    def _budget_mm():
        return scanner_module.Z_TRAVEL_MAX_MM + scanner_module.Z_HOMING_MARGIN_MM

    def test_constants(self):
        # The real travel of the rod is set per rig; only the shape of the budget is fixed here
        self.assertGreater(scanner_module.Z_TRAVEL_MAX_MM, 0)
        self.assertEqual(scanner_module.Z_HOMING_MARGIN_MM, 15)

    def test_homing_budget_is_travel_plus_margin(self):
        with self.assertRaises(ScannerError):  # switch never reached within the budget
            self.s.home_z_axis()
        self.assertEqual(self.s.z_axis_motor.rotate.call_args.kwargs["steps"], self._budget_mm() * 100)
        self.s.z_axis_motor.distance_to_step_conversion.assert_called_with(distance=self._budget_mm())

    def test_move_to_top_budget_is_travel_plus_margin(self):
        with self.assertRaises(ScannerError):
            self.s.move_z_to_top()
        self.assertEqual(self.s.z_axis_motor.rotate.call_args.kwargs["steps"], self._budget_mm() * 100)

    def test_homing_and_move_to_top_run_at_constant_speed_by_default(self):
        for call in (self.s.home_z_axis, self.s.move_z_to_top):
            self.s.z_axis_motor.rotate.reset_mock()
            with self.assertRaises(ScannerError):
                call()
            kw = self.s.z_axis_motor.rotate.call_args.kwargs
            self.assertFalse(kw["acceleration"])
            self.assertIsNone(kw["start_delay"])
            self.assertEqual(kw["delay"], scanner_module.Z_PREPARE_DELAY_S)

    def test_acceleration_can_be_switched_on_for_homing_and_move_to_top(self):
        for call in (self.s.home_z_axis, self.s.move_z_to_top):
            self.s.z_axis_motor.rotate.reset_mock()
            with self.assertRaises(ScannerError):
                call(acceleration=True)
            kw = self.s.z_axis_motor.rotate.call_args.kwargs
            self.assertTrue(kw["acceleration"])
            self.assertEqual(kw["start_delay"], 0.004)

    def test_preparation_climb_of_30_mm_has_no_acceleration(self):
        self.s.state = ScannerState.PREPARING
        self.s.down_endstop.is_active = True      # homing succeeds immediately
        self.s.wait_for_light_stable = lambda **kwargs: True
        self.s.z_axis_motor.rotate.reset_mock()
        self.s.finish_preparation()
        climbs = [c for c in self.s.z_axis_motor.rotate.call_args_list if c.kwargs.get("clockwise") is True]
        self.assertTrue(climbs)
        for call in climbs:
            self.assertFalse(call.kwargs["acceleration"])
            self.assertEqual(call.kwargs["delay"], scanner_module.Z_PREPARE_DELAY_S)

    def test_acceleration_can_be_switched_off_for_homing_and_move_to_top(self):
        for call in (self.s.home_z_axis, self.s.move_z_to_top):
            self.s.z_axis_motor.rotate.reset_mock()
            with self.assertRaises(ScannerError):
                call(delay=0.001, acceleration=False)
            kw = self.s.z_axis_motor.rotate.call_args.kwargs
            self.assertFalse(kw["acceleration"])
            self.assertIsNone(kw["start_delay"])
            self.assertEqual(kw["delay"], 0.001)
            self.assertEqual(kw["steps"], self._budget_mm() * 100)

    def test_homing_failure_still_releases_the_torque(self):
        with self.assertRaises(ScannerError):
            self.s.home_z_axis()
        self.s.z_axis_motor.disable.assert_called()


class TestShotsPerRing(unittest.TestCase):
    """D10"""

    def _shots(self, angle, steps):
        s = make_scanner()
        with patch.object(scanner_module, "TURNTABLE_SETTLE_S", 0.0):
            s._capture_turntable_slice(
                images_dir="/tmp/none", angle=angle, steps=steps, delay_turntable=0.001, check_tof=False
            )
        return s.dual_cameras.capture_photo.call_count

    def test_22_5_degrees_gives_16_shots(self):
        self.assertEqual(self._shots(22.5, 200), 16)  # round(360 / 22.5) agrees, 3200 // 200 is exact

    def test_18_degrees_gives_20_shots(self):
        self.assertEqual(self._shots(18.0, 160), 20)

    def test_a_step_that_does_not_divide_the_turn_is_not_rounded_up(self):
        # 33.75 deg = 300 steps: 3200 // 300 = 10 shots (337.5 deg), not round(360 / 33.75) = 11 (371 deg)
        self.assertEqual(self._shots(33.75, 300), 10)


class TestRouterPrepareMapping(unittest.TestCase):
    """D4 (router side), D12"""

    def setUp(self):
        self.scanner = MagicMock()
        fake_mock_mod = types.ModuleType("hardware.scanner_mock")
        fake_mock_mod.ScannerMock = lambda **kwargs: self.scanner
        fake_mock_mod.ScannerState = _State

        fake_schemas = types.ModuleType("schemas.scanner")
        for name in ("PrepareRequest", "PrepareResponse", "StartRequest", "StartResponse", "CancelResponse",
                     "StatusResponse", "StreamActiveRequest", "StreamQualityRequest"):
            setattr(fake_schemas, name, type(name, (_Response,), {}))

        with patch.dict(os.environ, {"MOCK_HARDWARE": "1"}):
            self.mod = _load("router/scanner.py", "router_scanner_ws_d", {
                **_web_stubs(),
                "router": _router_package(),
                "hardware.scanner_mock": fake_mock_mod,
                "schemas.scanner": fake_schemas,
                "cloud": MagicMock(),
                "cloud.runpod": MagicMock(),
                "cloud.cloudflare_r2": MagicMock(),
            })

    def _req(self):
        req = MagicMock()
        req.camera_config.model_dump.return_value = {}
        return req

    def test_prepare_refused_during_emergency_stop_is_409(self):
        self.scanner.setup_and_stream.side_effect = ScannerStoppingError("stopping")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_prepare_refused_by_state_stays_400(self):
        self.scanner.setup_and_stream.side_effect = RuntimeError("Cannot prepare. Scanner is currently running")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_scanner_proxy_is_still_lazy(self):
        self.assertFalse(self.mod.scanner.initialized)
        _ = self.mod.scanner.state
        self.assertTrue(self.mod.scanner.initialized)


if __name__ == "__main__":
    unittest.main()
