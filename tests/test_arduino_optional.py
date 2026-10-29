"""
API-21 / Z-ACC-2: the server without the Arduino, and the speed profile of the Z helpers.

  * Motor() does not raise when the Arduino is missing: `configured` is False and `configure()` tries again;
  * Scanner.require_motors() raises ArduinoUnavailableError when the bridge does not answer or a motor cannot be
    configured, and passes once it can;
  * a scan prepare checks the Arduino before any state change, the manual stream (preflight=False) does not;
  * move_z_up / move_z_down / Motor.rotate_distance default to constant speed; the turntable helpers keep the ramp.
"""
import inspect
import unittest
from unittest.mock import MagicMock

from tests.test_motor_end_delay import _FakeBridge, _load_motor_module


class _OfflineBridge(_FakeBridge):
    """A bridge whose port never opened: every command raises, like ArduinoBridge.send_command without a port."""

    def __init__(self):
        super().__init__()
        self.online = False

    def send_command(self, cmd, timeout=30.0):
        if not self.online:
            raise RuntimeError("Arduino is not connected. Cannot send command.")
        return super().send_command(cmd, timeout)


class MotorWithoutArduinoTest(unittest.TestCase):
    def test_construction_does_not_raise_and_configure_retries(self):
        bridge = _OfflineBridge()
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=1, step_pin=5, dir_pin=4, en_pin=3, step_type="1/4")
        self.assertFalse(motor.configured)
        self.assertIn("not connected", motor.config_error)

        bridge.online = True
        self.assertTrue(motor.configure())
        self.assertTrue(motor.configured)
        self.assertEqual(bridge.sent[-1][0], "2 1 5 4 3")

    def test_a_refused_config_is_reported_not_raised(self):
        bridge = _FakeBridge()
        bridge.send_command = lambda cmd, timeout=30.0: "3"
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0)
        self.assertFalse(motor.configured)
        self.assertIn("'3'", motor.config_error)

    def test_rotate_distance_is_constant_speed_by_default_and_rotate_keeps_the_ramp(self):
        mod = _load_motor_module(_FakeBridge())
        defaults = lambda fn: inspect.signature(fn).parameters["acceleration"].default
        self.assertIs(defaults(mod.Motor.rotate_distance), False)
        self.assertIs(defaults(mod.Motor.rotate), True)
        self.assertIs(defaults(mod.Motor.rotate_angle), True)


class ScannerRequireMotorsTest(unittest.TestCase):
    def setUp(self):
        from hardware.scanner import Scanner
        self.scanner = Scanner(xvs=False)
        self.bridge = MagicMock()
        self.bridge.ensure_connected.return_value = True
        # the mocked Motor class returns one shared object: give each axis its own
        self.scanner.turntable_motor = MagicMock()
        self.scanner.z_axis_motor = MagicMock()
        for motor in (self.scanner.turntable_motor, self.scanner.z_axis_motor):
            motor.bridge = self.bridge
            motor.configured = True

    def test_an_answering_arduino_passes(self):
        self.scanner.require_motors()
        self.bridge.ensure_connected.assert_called_once()

    def test_no_answer_raises_a_clear_unavailable_error(self):
        from hardware.scanner import ArduinoUnavailableError
        self.bridge.ensure_connected.return_value = False
        with self.assertRaises(ArduinoUnavailableError) as ctx:
            self.scanner.require_motors()
        self.assertIn("Arduino", str(ctx.exception))
        self.assertTrue(ctx.exception.unavailable)

    def test_an_unconfigured_motor_is_configured_again_or_reported(self):
        from hardware.scanner import ArduinoUnavailableError
        z = self.scanner.z_axis_motor
        z.configured = False
        z.configure.return_value = True
        self.scanner.require_motors()
        z.configure.assert_called_once()

        z.configure.return_value = False
        z.config_error = "Failed to configure motor 1"
        with self.assertRaises(ArduinoUnavailableError) as ctx:
            self.scanner.require_motors()
        self.assertIn("Failed to configure motor 1", str(ctx.exception))

    def test_a_scan_prepare_is_refused_before_any_state_change(self):
        from hardware.scanner import ArduinoUnavailableError, ScannerState
        self.bridge.ensure_connected.return_value = False
        self.scanner.preflight = MagicMock()
        with self.assertRaises(ArduinoUnavailableError):
            self.scanner.setup_and_stream()
        self.assertEqual(self.scanner.state, ScannerState.IDLE)
        self.scanner.preflight.assert_not_called()
        self.scanner.lights.on.assert_not_called()

    def test_the_manual_stream_does_not_need_the_arduino(self):
        self.bridge.ensure_connected.return_value = False
        self.scanner._close_cameras = MagicMock()
        self.scanner.lights.brightness = 1.0
        try:
            self.scanner.setup_and_stream(preflight=False)
        except Exception as e:   # the fake cameras may refuse later; the Arduino must not be the reason
            self.assertNotIn("Arduino", str(e))
        self.bridge.ensure_connected.assert_not_called()


class ZHelperDefaultsTest(unittest.TestCase):
    def test_the_z_helpers_run_at_constant_speed_unless_asked(self):
        from hardware.scanner import Scanner
        for name in ("move_z_up", "move_z_down"):
            with self.subTest(helper=name):
                default = inspect.signature(getattr(Scanner, name)).parameters["acceleration"].default
                self.assertIs(default, False)

    def test_move_z_up_distance_sends_no_ramp_by_default(self):
        from hardware.scanner import Scanner
        scanner = Scanner(xvs=False)
        scanner.up_endstop.is_active = False
        scanner.z_axis_motor.distance_to_step_conversion.return_value = 100
        scanner.z_axis_motor.rotate.return_value = True
        scanner._start_endstop_watch = MagicMock(return_value=None)
        scanner._end_z_motion = MagicMock()
        self.assertTrue(scanner.move_z_up_distance(distance=1.0))
        self.assertIs(scanner.z_axis_motor.rotate.call_args.kwargs["acceleration"], False)


if __name__ == "__main__":
    unittest.main()
