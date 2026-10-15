"""
Tests for workstream WS-B (hardware/motor.py + hardware/arduino_bridge.py), MOTOR_FIXES_PLAN.md items B1-B5:
  B1  STEP_DELAY_MIN / STEP_DELAY_MAX validation, zero-safe ramp duration, fast-ramp warning
  B2  serial timeout vs. a discrete step-by-step simulation of the firmware ramp (Motor.cpp rotateRamp)
  B3  stop_epoch: a universal stop invalidates is_enabled on every motor sharing the bridge
  B4  send_stop(repeat, gap_s) and Motor.stop(release_torque, repeat)
  B5  disable(): one retry, bool result, never raises
Everything is loaded by file path with fake serial / fake bridge objects: no hardware, no real sleeping.
"""
import importlib.util
import itertools
import math
import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

from tests.test_motor_end_delay import _load_motor_module, _FakeBridge

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ----------------------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------------------
def _fake_serial_modules():
    fake = types.ModuleType("serial")
    fake.SerialException = type("SerialException", (Exception,), {})
    fake.Serial = object
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    fake.tools = tools
    tools.list_ports = list_ports
    return {"serial": fake, "serial.tools": tools, "serial.tools.list_ports": list_ports}


def _load_bridge_module():
    with patch.dict(sys.modules, _fake_serial_modules()):
        spec = importlib.util.spec_from_file_location(
            "bridge_ws_b", os.path.join(ROOT, "hardware", "arduino_bridge.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _FakeSerial:
    """In-memory serial port. Every normal command is answered with `reply`; 0xFF gets no answer."""

    def __init__(self, reply=b"0\n"):
        self.reply = reply
        self.buffer = []
        self.written = []
        self.timeout = 3.0
        self.is_open = True

    def write(self, data):
        self.written.append(bytes(data))
        if bytes(data) != b"\xff":
            self.buffer.append(self.reply)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.buffer.clear()

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""


def _real_bridge(mod, serial_port):
    """An ArduinoBridge built without __init__ (which would open a port), as the other bridge tests do."""
    b = mod.ArduinoBridge.__new__(mod.ArduinoBridge)
    b.serial = serial_port
    b.port = None
    b.baudrate = 115200
    b._configs = []
    b.connection_epoch = 0
    b.stop_epoch = 0
    b._write_lock = threading.RLock()
    b._response_lock = threading.RLock()
    return b


class _ScriptedBridge(_FakeBridge):
    """Answers DISABLE (opcode 4) from `disable_replies`; everything else is OK. Exceptions are raised."""

    def __init__(self, disable_replies=()):
        super().__init__()
        self.disable_replies = list(disable_replies)

    def send_command(self, cmd, timeout=30.0):
        self.sent.append((cmd, timeout))
        if cmd.split()[0] == "4":
            reply = self.disable_replies.pop(0) if self.disable_replies else "0"
            if isinstance(reply, Exception):
                raise reply
            return reply
        return "0"


class _BridgeWithoutStopEpoch:
    """A bridge written before stop_epoch existed (no such attribute at all)."""

    def __init__(self):
        self.sent = []
        self.stops = []

    def send_command(self, cmd, timeout=30.0):
        self.sent.append((cmd, timeout))
        return "0"

    def send_stop(self, repeat=1, gap_s=0.02):
        self.stops.append(repeat)


# ----------------------------------------------------------------------------------------------------------
# B1: delay limits
# ----------------------------------------------------------------------------------------------------------
class TestStepDelayLimits(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeBridge()
        self.mod = _load_motor_module(self.bridge)
        self.motor = self.mod.Motor(motor_id=0, step_type="1/16")
        self.bridge.sent.clear()
        self.mod._warned.clear()

    def test_contract_constants(self):
        self.assertEqual(self.mod.STEP_DELAY_MIN, 0.0001)
        self.assertEqual(self.mod.STEP_DELAY_MAX, 0.05)

    def test_out_of_range_delay_is_rejected_without_touching_the_bridge(self):
        for delay in (0, 0.0, -0.001, 1e-7, 0.00009, 0.2, 1, float("nan")):
            with self.subTest(delay=delay):
                with self.assertRaises(ValueError) as ctx:
                    self.motor.rotate(steps=100, delay=delay)
                self.assertIn("delay", str(ctx.exception))
        self.assertEqual(self.bridge.sent, [])

    def test_out_of_range_start_delay_is_rejected(self):
        for start_delay in (0, -0.001, 1e-7, 0.051, 3.0):
            with self.subTest(start_delay=start_delay):
                with self.assertRaises(ValueError) as ctx:
                    self.motor.rotate(steps=100, delay=0.001, start_delay=start_delay)
                self.assertIn("start_delay", str(ctx.exception))
        self.assertEqual(self.bridge.sent, [])

    def test_non_positive_end_delay_is_rejected(self):
        for end_delay in (0, 0.0, -0.01, float("nan")):
            with self.subTest(end_delay=end_delay):
                with self.assertRaises(ValueError) as ctx:
                    self.motor.rotate(steps=100, delay=0.001, end_delay=end_delay)
                self.assertIn("end_delay", str(ctx.exception))

    def test_boundaries_and_defaults_are_accepted(self):
        for kwargs in (
                {},  # defaults of rotate(): delay 2 ms, ramp 20 %
                {"delay": self.mod.STEP_DELAY_MIN},
                {"delay": self.mod.STEP_DELAY_MAX},
                {"delay": 0.001, "start_delay": self.mod.STEP_DELAY_MIN},
                {"delay": 0.001, "start_delay": self.mod.STEP_DELAY_MAX},
                {"delay": 0.0015, "start_delay": 0.004, "ramp_percent": 0.1},   # Z homing defaults
                {"delay": 0.001, "start_delay": 0.004, "end_delay": 0.015},     # scan defaults
                {"delay": 0.001, "acceleration": False},
        ):
            with self.subTest(**kwargs):
                self.assertTrue(self.motor.rotate(steps=100, **kwargs))

    def test_end_delay_above_max_is_still_clamped_not_rejected(self):
        self.assertTrue(self.motor.rotate(steps=100, delay=0.001, end_delay=1.0))
        self.assertTrue(self.bridge.sent[-1][0].endswith(" 30000"))

    def test_ramp_phase_seconds_is_zero_safe(self):
        f = self.mod._ramp_phase_seconds
        for a, b in ((0, 0), (0, 1000), (1000, 0), (-5, -5)):
            with self.subTest(a=a, b=b):
                value = f(100, a, b)
                self.assertTrue(math.isfinite(value) and value >= 0)
        self.assertEqual(f(0, 0, 0), 0.0)
        self.assertAlmostEqual(f(100, 1000, 1000), 0.1)

    def test_fast_ramp_warns_once(self):
        with self.assertLogs(self.mod.log, level="WARNING") as captured:
            for _ in range(4):
                self.motor.rotate(steps=500, delay=0.0003, start_delay=0.001)
        warnings = [r for r in captured.records if "slip" in r.getMessage()]
        self.assertEqual(len(warnings), 1)
        # not rejected: the ramp command was sent each time
        self.assertEqual(len([c for c, _ in self.bridge.sent if c.startswith("16 ")]), 4)

    def test_no_fast_ramp_warning_at_or_above_400us_or_without_ramp(self):
        with self.assertLogs(self.mod.log, level="WARNING") as captured:
            self.mod.log.warning("sentinel")  # assertLogs needs at least one record
            self.motor.rotate(steps=500, delay=0.0004, start_delay=0.001)
            self.motor.rotate(steps=500, delay=0.0005)
            self.motor.rotate(steps=500, delay=0.0002, acceleration=False)
        self.assertEqual([r.getMessage() for r in captured.records], ["sentinel"])


# ----------------------------------------------------------------------------------------------------------
# B2: timeout vs. discrete simulation of the firmware ramp
# ----------------------------------------------------------------------------------------------------------
def _simulate_firmware_seconds(cmd: str) -> float:
    """
    Duration in seconds of a move as the Arduino executes it: sum of the step periods.
    Ports `Motor::rotate` (opcode 5) and `Motor::rotateRamp` (opcode 16) of
    hardware/arduino_control/lib/Motor/Motor.cpp, including the 60 us floor of `stepAt` and the final wait
    for the last period.
    """
    parts = [int(p) for p in cmd.split()]
    if parts[0] == 5:
        _, _, steps, _, delay_us = parts
        # rotate(): 50 us HIGH + (delay - 50) LOW, or 50 + 50 when delay <= 50
        period = delay_us if delay_us > 50 else 100
        return steps * period / 1e6

    _, _, steps, _, target, start, accel, decel = parts[:8]
    end = parts[8] if len(parts) > 8 else 0
    if start < target:
        start = target
    if end == 0:
        end = start
    if end < target:
        end = target
    if accel + decel > steps:
        total = accel + decel
        accel = accel * steps // total
        decel = steps - accel
    cruise = steps - (accel + decel)

    def floor60(period):
        return 60 if period < 60 else period

    def period_from_v2(v2):
        return int(1_000_000.0 / math.sqrt(v2) + 0.5)

    v_start2 = (1_000_000.0 / start) ** 2
    v_cruise2 = (1_000_000.0 / target) ** 2
    v_end2 = (1_000_000.0 / end) ** 2

    total_us = 0
    for i in range(accel):
        total_us += floor60(period_from_v2(v_start2 + (v_cruise2 - v_start2) * (i + 1) / accel))
    total_us += cruise * floor60(target)
    for i in range(decel):
        total_us += floor60(period_from_v2(v_cruise2 - (v_cruise2 - v_end2) * (i + 1) / decel))
    return total_us / 1e6


class TestTimeoutAgainstFirmwareSimulation(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeBridge()
        self.mod = _load_motor_module(self.bridge)
        self.motor = self.mod.Motor(motor_id=0, step_type="1/16")
        self.bridge.sent.clear()
        self.mod._warned.clear()

    def test_formula_adds_two_final_periods(self):
        # 3200 steps, 1 ms cruise, 3 ms start, 20 %/20 %, 30 ms end: integral = 0.96 + 1.92 + 2*640/(1000+33.33)
        self.motor.rotate(steps=3200, delay=0.001, start_delay=0.003, ramp_percent=0.2, end_delay=0.030)
        _, timeout = self.bridge.sent[-1]
        duration = 2 * 640 / (1000 + 1000 / 3) + 1.92 + 2 * 640 / (1000 + 1000 / 30)
        self.assertAlmostEqual(timeout, duration * 1.5 + 3.0 + 2 * 0.030, places=3)

    def test_long_final_step_does_not_cause_false_abort(self):
        # Worst case of the old formula: a few steps at the 50 ms limit, the last one lasts a whole start_delay
        self.motor.rotate(steps=3, delay=0.001, start_delay=0.05, ramp_percent=1.0)
        cmd, timeout = self.bridge.sent[-1]
        self.assertGreaterEqual(timeout, 1.2 * _simulate_firmware_seconds(cmd))
        self.assertGreaterEqual(timeout, 3.0 + 2 * 0.05)

    def test_grid_timeout_covers_simulated_duration(self):
        grid = itertools.product(
            (1, 2, 3, 50, 800, 3200),                 # steps
            (0.0001, 0.0005, 0.001, 0.002, 0.01, 0.05),  # delay
            (None, 0.003, 0.02, 0.05),                # start_delay
            (None, 0.005, 0.015, 0.03),               # end_delay
            (0.0, 0.2, 0.5, 1.0),                     # ramp_percent (accel and decel)
        )
        checked = 0
        for steps, delay, start_delay, end_delay, ramp in grid:
            self.bridge.sent.clear()
            self.motor.rotate(steps=steps, delay=delay, start_delay=start_delay,
                              end_delay=end_delay, ramp_percent=ramp)
            cmd, timeout = self.bridge.sent[-1]
            simulated = _simulate_firmware_seconds(cmd)
            checked += 1
            if timeout < 1.2 * simulated:
                self.fail(f"timeout {timeout:.3f}s < 1.2 x simulated {simulated:.3f}s for "
                          f"steps={steps} delay={delay} start={start_delay} end={end_delay} ramp={ramp}: {cmd!r}")
        self.assertEqual(checked, 6 * 6 * 4 * 4 * 4)

    def test_grid_asymmetric_ramps_and_no_acceleration(self):
        for steps, delay, accel_p, decel_p in itertools.product(
                (3, 50, 3200), (0.0005, 0.002, 0.05), (0.0, 0.1, 0.9), (0.0, 0.3, 1.0)):
            self.bridge.sent.clear()
            self.motor.rotate(steps=steps, delay=delay, start_delay=0.05, end_delay=0.03,
                              ramp_percent=accel_p, decel_percent=decel_p)
            cmd, timeout = self.bridge.sent[-1]
            self.assertGreaterEqual(timeout, 1.2 * _simulate_firmware_seconds(cmd), cmd)
        for steps, delay in itertools.product((1, 50, 3200), (0.0001, 0.001, 0.05)):
            self.bridge.sent.clear()
            self.motor.rotate(steps=steps, delay=delay, acceleration=False)
            cmd, timeout = self.bridge.sent[-1]
            self.assertTrue(cmd.startswith("5 "), cmd)
            self.assertGreaterEqual(timeout, 1.2 * _simulate_firmware_seconds(cmd), cmd)


# ----------------------------------------------------------------------------------------------------------
# B3: stop_epoch
# ----------------------------------------------------------------------------------------------------------
class TestStopEpoch(unittest.TestCase):
    def _two_motors(self, bridge):
        mod = _load_motor_module(bridge)
        turntable = mod.Motor(motor_id=0, step_type="1/16")
        z_axis = mod.Motor(motor_id=1, step_type="1/4")
        return mod, turntable, z_axis

    def test_stop_on_one_motor_invalidates_the_other(self):
        bridge = _FakeBridge()
        _, turntable, z_axis = self._two_motors(bridge)
        z_axis.rotate(steps=100, delay=0.001)
        turntable.rotate(steps=100, delay=0.001)
        self.assertTrue(z_axis.is_enabled)
        self.assertTrue(turntable.is_enabled)

        turntable.stop()
        self.assertFalse(turntable.is_enabled)
        self.assertFalse(z_axis.is_enabled)  # the idle 0xFF disabled it too

    def test_successful_rotate_or_enable_rearms(self):
        bridge = _FakeBridge()
        _, turntable, z_axis = self._two_motors(bridge)
        z_axis.rotate(steps=100, delay=0.001)
        turntable.stop()
        self.assertFalse(z_axis.is_enabled)

        z_axis.rotate(steps=100, delay=0.001)
        self.assertTrue(z_axis.is_enabled)
        self.assertFalse(turntable.is_enabled)

        turntable.enable()
        self.assertTrue(turntable.is_enabled)
        self.assertTrue(z_axis.is_enabled)  # re-arming one motor does not touch the other

    def test_disable_after_stale_state_does_not_settle(self):
        bridge = _FakeBridge()
        mod, turntable, z_axis = self._two_motors(bridge)
        z_axis.rotate(steps=100, delay=0.001)
        turntable.stop()
        with patch.object(mod.time, "sleep") as sleep:
            z_axis.disable()
        sleep.assert_not_called()

    def test_stop_during_a_move_invalidates_its_successful_result(self):
        class _StoppedWhileRunning(_FakeBridge):
            def send_command(self, cmd, timeout=30.0):
                reply = super().send_command(cmd, timeout)
                if cmd.startswith("5 "):
                    self.stop_epoch += 1  # 0xFF from another thread landed just before the "0" was returned
                return reply

        bridge = _StoppedWhileRunning()
        _, turntable, _ = self._two_motors(bridge)
        self.assertTrue(turntable.rotate(steps=100, delay=0.001, acceleration=False))
        self.assertFalse(turntable.is_enabled)

    def test_bridge_without_stop_epoch_attribute_keeps_working(self):
        bridge = _BridgeWithoutStopEpoch()
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        motor.rotate(steps=100, delay=0.001)
        self.assertTrue(motor.is_enabled)
        motor.stop()
        self.assertFalse(motor.is_enabled)
        self.assertEqual(bridge.stops, [1])

    def test_real_bridge_counts_every_stop(self):
        bridge_mod = _load_bridge_module()
        port = _FakeSerial()
        bridge = _real_bridge(bridge_mod, port)
        self.assertEqual(bridge.stop_epoch, 0)
        bridge.send_stop()
        bridge.send_stop(repeat=3, gap_s=0)
        self.assertEqual(bridge.stop_epoch, 2)  # one per call, not per byte

        mod = _load_motor_module(bridge)
        turntable = mod.Motor(motor_id=0, step_type="1/16")
        z_axis = mod.Motor(motor_id=1, step_type="1/4")
        z_axis.rotate(steps=10, delay=0.001)
        self.assertTrue(z_axis.is_enabled)
        turntable.stop()
        self.assertFalse(z_axis.is_enabled)
        z_axis.rotate(steps=10, delay=0.001)
        self.assertTrue(z_axis.is_enabled)

    def test_stop_epoch_is_also_bumped_by_the_timeout_resync_stop(self):
        bridge_mod = _load_bridge_module()
        port = _FakeSerial(reply=b"")  # never answers
        bridge = _real_bridge(bridge_mod, port)
        self.assertEqual(bridge.send_command("5 0 100 1 1000", timeout=0.05), "")
        self.assertIn(b"\xff", port.written)
        self.assertEqual(bridge.stop_epoch, 1)

        port.written.clear()
        self.assertEqual(bridge.send_command("3 0", timeout=0.05), "")  # non-move: no 0xFF, no bump
        self.assertNotIn(b"\xff", port.written)
        self.assertEqual(bridge.stop_epoch, 1)


# ----------------------------------------------------------------------------------------------------------
# B4: send_stop(repeat, gap_s), Motor.stop(release_torque, repeat)
# ----------------------------------------------------------------------------------------------------------
class TestSendStopRepeat(unittest.TestCase):
    def setUp(self):
        self.bridge_mod = _load_bridge_module()
        self.port = _FakeSerial()
        self.bridge = _real_bridge(self.bridge_mod, self.port)

    def test_default_is_one_byte_and_no_pause(self):
        with patch.object(self.bridge_mod.time, "sleep") as sleep:
            self.bridge.send_stop()
        self.assertEqual(self.port.written, [b"\xff"])
        sleep.assert_not_called()

    def test_repeat_two_writes_two_bytes_with_one_gap(self):
        with patch.object(self.bridge_mod.time, "sleep") as sleep:
            self.bridge.send_stop(repeat=2, gap_s=0.01)
        self.assertEqual(self.port.written, [b"\xff", b"\xff"])
        sleep.assert_called_once_with(0.01)

    def test_repeat_below_one_still_sends_one_byte(self):
        self.bridge.send_stop(repeat=0)
        self.assertEqual(self.port.written, [b"\xff"])

    def test_does_not_wait_for_a_reply_or_take_the_response_lock(self):
        held = threading.Event()
        release = threading.Event()

        def hold_transaction():
            with self.bridge._response_lock:  # a long move is "in flight"
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold_transaction, daemon=True)
        holder.start()
        self.assertTrue(held.wait(2))
        try:
            stopper = threading.Thread(target=lambda: self.bridge.send_stop(repeat=2, gap_s=0.001), daemon=True)
            stopper.start()
            stopper.join(2)
            self.assertFalse(stopper.is_alive(), "send_stop blocked on _response_lock")
            self.assertEqual(self.port.written, [b"\xff", b"\xff"])
            self.assertEqual(self.port.buffer, [])  # nothing was read or expected back
        finally:
            release.set()
            holder.join(2)

    def test_serial_error_is_logged_not_raised(self):
        exc_type = self.bridge_mod.serial.SerialException

        def boom(_data):
            raise exc_type("gone")

        self.port.write = boom
        with self.assertLogs(self.bridge_mod.log, level="ERROR"):
            self.bridge.send_stop(repeat=2, gap_s=0)
        self.assertEqual(self.bridge.stop_epoch, 1)

    def test_motor_stop_forwards_repeat(self):
        bridge = _FakeBridge()
        calls = []
        bridge.send_stop = lambda repeat=1, gap_s=0.02: calls.append(repeat)
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        motor.stop()
        motor.stop(repeat=2)
        motor.stop(release_torque=True, repeat=3)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual([c for c, _ in bridge.sent if c.startswith("4 ")], ["4 0"])  # only the release_torque one

    def test_motor_stop_with_release_torque_disables_after_the_stops(self):
        bridge = _FakeBridge()
        order = []
        bridge.send_stop = lambda repeat=1, gap_s=0.02: order.append(("stop", repeat))
        orig = bridge.send_command
        bridge.send_command = lambda cmd, timeout=30.0: (order.append(("cmd", cmd)), orig(cmd, timeout))[1]
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        order.clear()
        motor.stop(release_torque=True, repeat=2)
        self.assertEqual(order, [("stop", 2), ("cmd", "4 0")])


# ----------------------------------------------------------------------------------------------------------
# B5: disable() retry and result
# ----------------------------------------------------------------------------------------------------------
class TestDisableRetry(unittest.TestCase):
    def _motor(self, bridge):
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        motor.enable()
        bridge.sent.clear()
        return mod, motor

    @staticmethod
    def _disables(bridge):
        return [c for c, _ in bridge.sent if c.split()[0] == "4"]

    def test_confirmed_first_time_sends_once(self):
        bridge = _ScriptedBridge(["0"])
        _, motor = self._motor(bridge)
        self.assertTrue(motor.is_enabled)
        self.assertTrue(motor.disable(settle_delay=0.0))
        self.assertFalse(motor.is_enabled)
        self.assertEqual(self._disables(bridge), ["4 0"])

    def test_empty_then_ok_retries_and_succeeds(self):
        bridge = _ScriptedBridge(["", "0"])
        _, motor = self._motor(bridge)
        self.assertTrue(motor.disable(settle_delay=0.0))
        self.assertFalse(motor.is_enabled)
        self.assertEqual(self._disables(bridge), ["4 0", "4 0"])
        self.assertFalse(motor.disable_unconfirmed)

    def test_error_status_then_ok_retries_and_succeeds(self):
        bridge = _ScriptedBridge(["3", "0"])
        _, motor = self._motor(bridge)
        self.assertTrue(motor.disable(settle_delay=0.0))
        self.assertFalse(motor.is_enabled)

    def test_two_failures_return_false_warn_and_do_not_raise(self):
        bridge = _ScriptedBridge(["", ""])
        mod, motor = self._motor(bridge)
        with self.assertLogs(mod.log, level="WARNING") as captured:
            result = motor.disable(settle_delay=0.0)
        self.assertIs(result, False)
        self.assertEqual(self._disables(bridge), ["4 0", "4 0"])  # exactly one retry
        self.assertTrue(any("may still be energized" in r.getMessage() for r in captured.records))
        # truthful: the driver was energized and nobody confirmed it was released
        self.assertTrue(motor.is_enabled)
        self.assertTrue(motor.disable_unconfirmed)

    def test_later_confirmed_disable_clears_the_flag(self):
        bridge = _ScriptedBridge(["", "", "0"])
        _, motor = self._motor(bridge)
        self.assertFalse(motor.disable(settle_delay=0.0))
        self.assertTrue(motor.disable(settle_delay=0.0))
        self.assertFalse(motor.is_enabled)
        self.assertFalse(motor.disable_unconfirmed)

    def test_bridge_exception_is_swallowed(self):
        bridge = _ScriptedBridge([RuntimeError("Arduino is not connected"), RuntimeError("still not")])
        _, motor = self._motor(bridge)
        self.assertFalse(motor.disable(settle_delay=0.0))  # must not raise: cleanup paths depend on it

    def test_failed_disable_of_a_motor_that_was_not_enabled_stays_disabled(self):
        bridge = _ScriptedBridge(["", ""])
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        self.assertFalse(motor.disable(settle_delay=0.0))
        self.assertFalse(motor.is_enabled)

    def test_close_and_stop_with_release_do_not_raise_on_failure(self):
        bridge = _ScriptedBridge(["", "", "", ""])
        _, motor = self._motor(bridge)
        motor.stop(release_torque=True)
        motor.close()


if __name__ == "__main__":
    unittest.main()
