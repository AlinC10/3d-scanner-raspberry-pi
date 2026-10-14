import importlib.util
import os
import sys
import types
import unittest
from enum import IntEnum
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class _FakeCommand(IntEnum):
    PING = 0x01
    CONFIG = 0x02
    ENABLE = 0x03
    DISABLE = 0x04
    ROTATE = 0x05
    ACCEL_ROTATE = 0x10


class _FakeStatus(IntEnum):
    OK = 0
    ERR_ABORTED = 4


class _FakeBridge:
    def __init__(self):
        self.sent = []

    def send_command(self, cmd, timeout=30.0):
        self.sent.append((cmd, timeout))
        return str(_FakeStatus.OK.value)

    def send_stop(self):
        pass


def _load_motor_module(bridge):
    """Loads the real hardware/motor.py with a fake ArduinoBridge (no serial port needed)."""
    fake_bridge_mod = types.ModuleType("hardware.arduino_bridge")
    fake_bridge_mod.ArduinoCommand = _FakeCommand
    fake_bridge_mod.ArduinoStatus = _FakeStatus
    fake_bridge_mod.ArduinoBridge = types.SimpleNamespace(get_instance=lambda port=None: bridge)

    with patch.dict(sys.modules, {"hardware.arduino_bridge": fake_bridge_mod}):
        spec = importlib.util.spec_from_file_location("motor_under_test", os.path.join(ROOT, "hardware", "motor.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class TestMotorEndDelay(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeBridge()
        self.mod = _load_motor_module(self.bridge)
        self.motor = self.mod.Motor(motor_id=0, step_type="1/16")
        self.bridge.sent.clear()  # drop the CONFIG command

    def _rotate(self, **kwargs):
        params = dict(steps=3200, delay=0.001, ramp_percent=0.2, start_delay=0.003)
        params.update(kwargs)
        self.motor.rotate(**params)
        return self.bridge.sent[-1]

    def test_without_end_delay_command_is_unchanged(self):
        cmd, _ = self._rotate()
        self.assertEqual(cmd, "16 0 3200 1 1000 3000 640 640")

    def test_end_delay_is_appended_as_microseconds(self):
        cmd, _ = self._rotate(end_delay=0.015)
        self.assertEqual(cmd, "16 0 3200 1 1000 3000 640 640 15000")

    def test_end_delay_is_clamped_to_range(self):
        cmd_low, _ = self._rotate(end_delay=0.001)
        cmd_high, _ = self._rotate(end_delay=1.0)
        self.assertTrue(cmd_low.endswith(" 5000"), cmd_low)
        self.assertTrue(cmd_high.endswith(" 30000"), cmd_high)

    def test_end_delay_never_faster_than_cruise_delay(self):
        cmd, _ = self._rotate(delay=0.02, start_delay=0.06, end_delay=0.005)
        self.assertTrue(cmd.endswith(" 20000"), cmd)

    def test_end_delay_ignored_without_acceleration(self):
        cmd, _ = self._rotate(acceleration=False, end_delay=0.015)
        self.assertEqual(cmd, "5 0 3200 1 1000")

    def test_end_delay_ignored_without_deceleration_phase(self):
        cmd, _ = self._rotate(decel_percent=0.0, end_delay=0.015)
        self.assertEqual(cmd, "16 0 3200 1 1000 3000 640 0")

    def test_resolve_end_delay_none_passthrough(self):
        self.assertIsNone(self.mod.resolve_end_delay(None, 0.001))

    def test_timeout_uses_constant_acceleration_phase_times(self):
        # accel: 640 steps 333.3 -> 1000 steps/s = 0.96 s; cruise 1920 * 1 ms = 1.92 s;
        # decel: 640 steps 1000 -> 66.7 steps/s = 1.2 s; total 4.08 s -> *1.5 + 3
        _, timeout = self._rotate(end_delay=0.015)
        self.assertAlmostEqual(timeout, 4.08 * 1.5 + 3.0, places=2)

    def test_timeout_grows_with_slow_tail(self):
        _, timeout_plain = self._rotate()
        _, timeout_tail = self._rotate(end_delay=0.030)
        self.assertGreater(timeout_tail, timeout_plain)

    def test_rotate_angle_forwards_end_delay(self):
        self.motor.rotate_angle(angle=360, delay=0.001, ramp_percent=0.2, start_delay=0.003, end_delay=0.015)
        cmd, _ = self.bridge.sent[-1]
        self.assertTrue(cmd.endswith(" 15000"), cmd)


if __name__ == "__main__":
    unittest.main()
