import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOTOR_PATH = os.path.join(REPO_ROOT, "hardware", "motor.py")

# Load real Motor class in isolation without corrupting sys.modules
spec = importlib.util.spec_from_file_location("real_motor_isolated", MOTOR_PATH)
real_motor_mod = importlib.util.module_from_spec(spec)
real_motor_mod.serial = MagicMock()
real_motor_mod.serial.tools = MagicMock()
real_motor_mod.serial.tools.list_ports = MagicMock()
spec.loader.exec_module(real_motor_mod)

Motor = real_motor_mod.Motor
ArduinoCommand = real_motor_mod.ArduinoCommand
ArduinoStatus = real_motor_mod.ArduinoStatus

from hardware.scanner import Scanner, TopEndstopTriggered, BottomEndstopTriggered


class TestMotorSettling(unittest.TestCase):
    def setUp(self):
        self.mock_bridge = MagicMock()
        self.mock_bridge.send_command.return_value = str(ArduinoStatus.OK.value)
        self.mock_bridge.send_stop.return_value = True
        real_motor_mod.ArduinoBridge._instance = self.mock_bridge

        self.motor = Motor(motor_id=1)
        self.motor.bridge = self.mock_bridge

    @patch.object(real_motor_mod.time, "sleep")
    def test_disable_with_settle_delay(self, mock_sleep):
        self.motor.enable()
        self.assertTrue(self.motor.is_enabled)

        # Default settle_delay is 0.5s
        self.motor.disable(settle_delay=0.5)

        mock_sleep.assert_called_once_with(0.5)
        self.mock_bridge.send_command.assert_called_with(f"{ArduinoCommand.DISABLE.value} 1")
        self.assertFalse(self.motor.is_enabled)

    @patch.object(real_motor_mod.time, "sleep")
    def test_disable_when_already_disabled(self, mock_sleep):
        self.assertFalse(self.motor.is_enabled)
        self.motor.disable(settle_delay=0.5)

        mock_sleep.assert_not_called()
        self.mock_bridge.send_command.assert_called_with(f"{ArduinoCommand.DISABLE.value} 1")

    @patch.object(real_motor_mod.time, "sleep")
    def test_disable_zero_settle_delay(self, mock_sleep):
        self.motor.enable()
        self.motor.disable(settle_delay=0.0)

        mock_sleep.assert_not_called()
        self.mock_bridge.send_command.assert_called_with(f"{ArduinoCommand.DISABLE.value} 1")
        self.assertFalse(self.motor.is_enabled)

    @patch.object(real_motor_mod.time, "sleep")
    def test_stop_releases_torque_instantly(self, mock_sleep):
        self.motor.enable()
        # Emergency stop with release_torque=True must bypass settling delay
        self.motor.stop(release_torque=True)

        self.mock_bridge.send_stop.assert_called_once()
        mock_sleep.assert_not_called()
        self.assertFalse(self.motor.is_enabled)


class TestScannerEndstops(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner(xvs=False)

    def test_move_z_up_top_endstop(self):
        # 1. Blocked if switch is already active
        self.scanner.up_endstop.is_active = True
        with self.assertRaises(TopEndstopTriggered):
            self.scanner.move_z_up(steps=100)

        # 2. Blocked if switch triggers mid-motion
        self.scanner.up_endstop.is_active = False
        def trip_top(*args, **kwargs):
            self.scanner.up_endstop.is_active = True
            return False
        self.scanner.z_axis_motor.rotate.side_effect = trip_top

        with self.assertRaises(TopEndstopTriggered):
            self.scanner.move_z_up(steps=100)

    def test_move_z_down_bottom_endstop(self):
        # 1. Blocked if switch is already active
        self.scanner.down_endstop.is_active = True
        with self.assertRaises(BottomEndstopTriggered):
            self.scanner.move_z_down(steps=100)

        # 2. Blocked if switch triggers mid-motion
        self.scanner.down_endstop.is_active = False
        def trip_bottom(*args, **kwargs):
            self.scanner.down_endstop.is_active = True
            return False
        self.scanner.z_axis_motor.rotate.side_effect = trip_bottom

        with self.assertRaises(BottomEndstopTriggered):
            self.scanner.move_z_down(steps=100)

    def test_home_z_axis_catches_bottom_endstop(self):
        self.scanner.down_endstop.is_active = False
        def trip_bottom(*args, **kwargs):
            self.scanner.down_endstop.is_active = True
            return False
        self.scanner.z_axis_motor.rotate.side_effect = trip_bottom

        # home_z_axis catches BottomEndstopTriggered cleanly
        self.scanner.home_z_axis()
        self.scanner.z_axis_motor.disable.assert_called()


if __name__ == "__main__":
    unittest.main()
