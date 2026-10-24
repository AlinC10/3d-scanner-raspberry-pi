"""
Tests for the medium-severity fixes (BUGS_MOTORS_ENDSTOPS.md #6-#9) that need no hardware:
  - ArduinoBridge resynchronisation (#7) and connection epoch
  - Motor enabled-state tracking (#8)
  - /test/* state guard (#6)
  - atomic POST /scanner/start (#9)
Everything is loaded by file path with fake dependencies, so these tests do not depend on (or pollute) sys.modules
mocks installed by the other test files.
"""
import importlib.util
import os
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from tests.test_motor_end_delay import _load_motor_module, _FakeBridge

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _APIRouter:
    def __init__(self, *args, **kwargs):
        pass

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    get = post = put = delete = _decorator

    def add_api_route(self, *args, **kwargs):
        pass


def _web_stubs():
    """tests/__init__.py replaces pydantic/fastapi with MagicMocks; the routers need importable stand-ins."""
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _APIRouter
    fastapi.HTTPException = _HTTPException
    fastapi.BackgroundTasks = object
    fastapi.Query = lambda *args, **kwargs: None
    responses = types.ModuleType("fastapi.responses")
    responses.StreamingResponse = object
    fastapi.responses = responses
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type(
        "BaseModel", (), {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)}
    )
    pydantic.Field = lambda *args, **kwargs: None
    return {"fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic}


def _load(path, name, extra_modules):
    with patch.dict(sys.modules, extra_modules):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------------------------------------
# ArduinoBridge
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


class _FakeSerial:
    """In-memory serial port: lines queued in `script` are delivered once something is written."""

    def __init__(self, buffer=(), script=(), responder=None):
        self.buffer = list(buffer)   # bytes already waiting in the input buffer (stale data)
        self.script = list(script)   # answers the "Arduino" gives to the next write
        self.responder = responder   # optional callable(bytes) -> list of reply lines, replaces `script`
        self.written = []
        self.timeout = 3.0
        self.is_open = True
        self.input_resets = 0

    def write(self, data):
        self.written.append(bytes(data))
        if self.responder is not None:
            self.buffer.extend(self.responder(bytes(data)))
        elif data != bytes([0xFF]):
            self.buffer.extend(self.script)
            self.script = []

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.input_resets += 1
        self.buffer.clear()

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""


class TestBridgeResync(unittest.TestCase):
    def setUp(self):
        self.mod = _load("hardware/arduino_bridge.py", "bridge_under_test", _fake_serial_modules())

    def _bridge(self, serial_port):
        b = self.mod.ArduinoBridge.__new__(self.mod.ArduinoBridge)
        b.serial = serial_port
        b.port = None
        b.baudrate = 115200
        b._configs = []
        b.connection_epoch = 0
        b._write_lock = threading.RLock()
        b._response_lock = threading.RLock()
        return b

    def test_stale_reply_is_not_taken_for_the_answer(self):
        port = _FakeSerial(buffer=[b"0\n"], script=[b"4\n"])  # late "0" from an earlier timed-out move
        self.assertEqual(self._bridge(port).send_command("5 0 100 1 1000"), "4")

    def test_non_numeric_lines_are_skipped(self):
        port = _FakeSerial(script=[b"garbage\n", b"0\n"])
        self.assertEqual(self._bridge(port).send_command("3 0"), "0")

    def test_timeout_aborts_the_arduino_and_resyncs(self):
        port = _FakeSerial(script=[])  # never answers
        bridge = self._bridge(port)
        self.assertEqual(bridge.send_command("5 0 100 1 1000", timeout=0.1), "")
        self.assertIn(bytes([0xFF]), port.written)        # the still-running move is stopped
        self.assertGreaterEqual(port.input_resets, 2)    # before the write and after the resync
        self.assertEqual(port.timeout, 3.0)               # per-command timeout is restored

    def test_timeout_of_a_non_move_command_does_not_send_the_universal_stop(self):
        port = _FakeSerial(script=[])  # ENABLE never answered
        self._bridge(port).send_command("3 0", timeout=0.1)
        self.assertNotIn(bytes([0xFF]), port.written)  # an idle Arduino would cut every motor on 0xFF

    def test_not_configured_answer_restores_configuration_and_retries_once(self):
        def arduino(data):
            if data.startswith(b"2 "):
                return [b"0\n"]
            return [b"0\n"] if any(w.startswith(b"2 ") for w in port.written[:-1]) else [b"3\n"]

        port = _FakeSerial(responder=arduino)
        bridge = self._bridge(port)
        bridge._configs = ["2 0 6 7 8"]
        self.assertEqual(bridge.send_command("5 0 100 1 1000"), "0")
        self.assertEqual(port.written, [b"5 0 100 1 1000\n", b"2 0 6 7 8\n", b"5 0 100 1 1000\n"])
        self.assertEqual(bridge.connection_epoch, 1)  # drivers are disabled after a reset

    def test_not_configured_is_not_retried_forever(self):
        port = _FakeSerial(responder=lambda data: [b"0\n"] if data.startswith(b"2 ") else [b"3\n"])
        bridge = self._bridge(port)
        bridge._configs = ["2 0 6 7 8"]
        self.assertEqual(bridge.send_command("5 0 100 1 1000"), "3")
        self.assertEqual(len([w for w in port.written if w.startswith(b"5 ")]), 2)  # original + one retry

    def test_not_configured_without_stored_configuration_is_returned_as_is(self):
        port = _FakeSerial(script=[b"3\n"])
        self.assertEqual(self._bridge(port).send_command("5 0 100 1 1000"), "3")

    def test_reboot_banner_in_the_middle_of_a_command_is_an_abort(self):
        port = _FakeSerial(script=[b"READY\n"])  # the board reset: the command was lost, "0" must not be assumed
        bridge = self._bridge(port)
        self.assertEqual(bridge.send_command("5 0 100 1 1000"), "4")
        self.assertEqual(bridge.connection_epoch, 1)

    def test_next_command_after_timeout_gets_its_own_answer(self):
        port = _FakeSerial(script=[])
        bridge = self._bridge(port)
        bridge.send_command("5 0 100 1 1000", timeout=0.1)
        port.buffer.append(b"0\n")        # the late answer of the timed-out move shows up afterwards
        port.script = [b"0\n"]
        self.assertEqual(bridge.send_command("4 0"), "0")
        self.assertEqual(port.buffer, [])


# ----------------------------------------------------------------------------------------------------------
# Motor enabled state
# ----------------------------------------------------------------------------------------------------------
class _AbortingBridge(_FakeBridge):
    """Answers OK to everything except moves, which come back aborted ("4")."""

    def send_command(self, cmd, timeout=30.0):
        self.sent.append((cmd, timeout))
        is_move = cmd.split()[0] in ("5", "16")
        return "4" if is_move else "0"


class TestMotorEnabledState(unittest.TestCase):
    def _motor(self, bridge):
        bridge.connection_epoch = 1
        mod = _load_motor_module(bridge)
        motor = mod.Motor(motor_id=0, step_type="1/16")
        bridge.sent.clear()
        return mod, motor

    def test_successful_move_marks_motor_enabled(self):
        mod, motor = self._motor(_FakeBridge())
        self.assertFalse(motor.is_enabled)
        motor.rotate(steps=100, delay=0.001)
        self.assertTrue(motor.is_enabled)  # the firmware energizes the driver by itself

    def test_disable_after_move_without_enable_still_settles(self):
        mod, motor = self._motor(_FakeBridge())
        motor.rotate(steps=100, delay=0.001)
        with patch.object(mod.time, "sleep") as sleep:
            motor.disable()
        sleep.assert_called_once_with(0.5)

    def test_aborted_move_marks_motor_disabled(self):
        mod, motor = self._motor(_AbortingBridge())
        motor.enable()
        self.assertTrue(motor.is_enabled)
        self.assertFalse(motor.rotate(steps=100, delay=0.001))
        self.assertFalse(motor.is_enabled)

    def test_rotate_angle_and_distance_report_the_result(self):
        mod, motor = self._motor(_FakeBridge())
        self.assertTrue(motor.rotate_angle(angle=18, delay=0.001))
        self.assertTrue(motor.rotate_distance(distance=1.0, delay=0.001))
        mod, aborted = self._motor(_AbortingBridge())
        self.assertFalse(aborted.rotate_angle(angle=18, delay=0.001))

    def test_whole_step_helpers(self):
        mod, _ = self._motor(_FakeBridge())
        self.assertEqual(mod.angle_to_steps(18.0, mod.TURNTABLE_STEP_TYPE), 160)
        self.assertEqual(mod.distance_to_steps(100.0, mod.Z_STEP_TYPE), 10000)
        with self.assertRaises(mod.MotorError):
            mod.angle_to_steps(10.0, mod.TURNTABLE_STEP_TYPE)
        with self.assertRaises(mod.MotorError):
            mod.distance_to_steps(33.333, mod.Z_STEP_TYPE)

    def test_reconnect_invalidates_enabled_state(self):
        bridge = _FakeBridge()
        mod, motor = self._motor(bridge)
        motor.enable()
        self.assertTrue(motor.is_enabled)
        bridge.connection_epoch += 1  # the Arduino was reset by a reconnect
        self.assertFalse(motor.is_enabled)
        with patch.object(mod.time, "sleep") as sleep:
            motor.disable()
        sleep.assert_not_called()  # nothing is energized any more, no settling pause


# ----------------------------------------------------------------------------------------------------------
# Routers
# ----------------------------------------------------------------------------------------------------------
class _State:
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    PREPARE_ALLOWED = ("idle", "completed", "cancelled", "error")


def _router_package():
    pkg = types.ModuleType("router")
    pkg.__path__ = []
    return pkg


class _ZMoveRequest:
    distance_mm: float = 10.0
    direction: str = "up"
    delay: float = 0.001
    acceleration: bool = False
    start_delay: float | None = None
    ramp_percent: float = 0.2
    decel_percent: float | None = None
    end_delay: float | None = None


class _TurntableRotateRequest:
    angle: float = 18.0
    clockwise: bool = True
    delay: float = 0.001
    acceleration: bool = True
    start_delay: float | None = None
    ramp_percent: float = 0.2
    decel_percent: float | None = None
    end_delay: float | None = 0.015


class TestTestRouterGuard(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        fake_router_scanner = types.ModuleType("router.scanner")
        fake_router_scanner.scanner = self.scanner
        fake_router_scanner.ScannerState = _State
        for _name in ("scanner_stream_mjpeg", "scanner_stream_switch_active", "scanner_stream_set_quality",
                      "scanner_stream_sync_status"):
            setattr(fake_router_scanner, _name, lambda *args, **kwargs: None)

        fake_hw_scanner = types.ModuleType("hardware.scanner")
        fake_hw_scanner.ScannerError = type("ScannerError", (Exception,), {})
        fake_hw_scanner.ScannerBusyError = type("ScannerBusyError", (fake_hw_scanner.ScannerError,), {})
        self.busy_error = fake_hw_scanner.ScannerBusyError
        fake_endstop = types.ModuleType("hardware.endstop")
        fake_endstop.TopEndstopTriggered = type("TopEndstopTriggered", (Exception,), {})
        fake_endstop.BottomEndstopTriggered = type("BottomEndstopTriggered", (Exception,), {})
        fake_lights = types.ModuleType("hardware.lights")
        fake_lights.LightLockedError = type("LightLockedError", (Exception,), {})
        fake_schemas = types.ModuleType("schemas.scanner")
        fake_schemas.ZMoveRequest = _ZMoveRequest
        fake_schemas.TurntableRotateRequest = _TurntableRotateRequest

        self.mod = _load("router/test.py", "router_test_under_test", {
            **_web_stubs(),
            "router": _router_package(),
            "router.scanner": fake_router_scanner,
            "hardware.scanner": fake_hw_scanner,
            "hardware.endstop": fake_endstop,
            "hardware.lights": fake_lights,
            "schemas.scanner": fake_schemas,
        })

    def _assert_blocked(self, call):
        self.scanner.state = _State.RUNNING
        with self.assertRaises(_HTTPException) as ctx:
            call()
        self.assertEqual(ctx.exception.status_code, 409)
        self.scanner.turntable_motor.enable.assert_not_called()
        self.scanner.turntable_motor.disable.assert_not_called()
        self.scanner.z_axis_motor.enable.assert_not_called()
        self.scanner.z_axis_motor.disable.assert_not_called()
        self.scanner.cleanup.assert_not_called()

    def test_motor_endpoints_are_blocked_during_a_scan(self):
        self._assert_blocked(lambda: self.mod.test_motor_enable())
        self._assert_blocked(lambda: self.mod.test_motor_disable())
        self._assert_blocked(lambda: self.mod.test_turntable_rotate(_TurntableRotateRequest()))
        self._assert_blocked(lambda: self.mod.test_z_move(_ZMoveRequest()))
        self._assert_blocked(lambda: self.mod.test_cleanup())

    def test_homing_endpoints_return_409_not_500(self):
        self._assert_blocked(lambda: self.mod.test_home_bottom())
        self._assert_blocked(lambda: self.mod.test_move_to_top())

    def test_manual_enable_and_disable_answer_409_while_a_motion_is_running(self):
        self.scanner.state = _State.IDLE
        busy = self.busy_error("Another motion is already in progress.")
        self.scanner.motion_guard.return_value.__enter__.side_effect = busy
        for call in (lambda: self.mod.test_motor_enable(), lambda: self.mod.test_motor_disable()):
            with self.assertRaises(_HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 409)
        self.scanner.turntable_motor.enable.assert_not_called()
        self.scanner.z_axis_motor.disable.assert_not_called()

    def test_prepared_state_is_also_blocked(self):
        self.scanner.state = _State.PREPARED
        with self.assertRaises(_HTTPException):
            self.mod.test_motor_disable()

    def test_aborted_movements_are_not_reported_as_success(self):
        from_tests = self.mod
        self.scanner.state = _State.IDLE
        self.scanner.turntable_motor.rotate_angle.return_value = False
        with self.assertRaises(_HTTPException) as ctx:
            from_tests.test_turntable_rotate(_TurntableRotateRequest())
        self.assertEqual(ctx.exception.status_code, 500)
        self.scanner.move_z_up_distance.return_value = False
        with self.assertRaises(_HTTPException) as ctx:
            from_tests.test_z_move(_ZMoveRequest())
        self.assertEqual(ctx.exception.status_code, 500)

    def test_endpoints_work_when_idle(self):
        self.scanner.state = _State.IDLE
        out = self.mod.test_turntable_rotate(_TurntableRotateRequest())
        self.assertEqual(out["status"], "success")
        self.scanner.turntable_motor.rotate_angle.assert_called_once()
        self.assertEqual(self.scanner.turntable_motor.rotate_angle.call_args.kwargs["end_delay"], 0.015)


class _StartRequest:
    job_id = "job"


class _Response:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestStartIsAtomic(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.state = _State.PREPARED

        fake_mock_mod = types.ModuleType("hardware.scanner_mock")
        self.created = []
        fake_mock_mod.ScannerMock = lambda **kwargs: (self.created.append(1), self.scanner)[1]
        fake_mock_mod.ScannerState = _State

        fake_schemas = types.ModuleType("schemas.scanner")
        for name in ("PrepareRequest", "PrepareResponse", "StartResponse", "CancelResponse", "StatusResponse",
                     "StreamActiveRequest", "StreamQualityRequest"):
            setattr(fake_schemas, name, type(name, (_Response,), {}))
        fake_schemas.StartRequest = _StartRequest

        with patch.dict(os.environ, {"MOCK_HARDWARE": "1"}):
            self.mod = _load("router/scanner.py", "router_scanner_under_test", {
                **_web_stubs(),
                "router": _router_package(),
                "hardware.scanner_mock": fake_mock_mod,
                "schemas.scanner": fake_schemas,
                "cloud": MagicMock(),
                "cloud.runpod": MagicMock(),
                "cloud.cloudflare_r2": MagicMock(),
            })

    def _req(self):
        return MagicMock()

    def test_prepare_is_refused_while_a_motion_is_in_flight(self):
        busy = type("Busy", (Exception,), {"conflict": True})("Another motion is already in progress.")
        self.scanner.motion_guard.return_value.__enter__.side_effect = busy
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 409)
        self.scanner.setup_and_stream.assert_not_called()

    def test_second_start_is_rejected_while_the_first_is_pending(self):
        tasks = MagicMock()
        self.mod.start_scan(self._req(), tasks)
        tasks.add_task.assert_called_once()
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.start_scan(self._req(), tasks)
        self.assertEqual(ctx.exception.status_code, 409)
        tasks.add_task.assert_called_once()  # no second pipeline was queued

    def test_pending_flag_is_cleared_when_the_pipeline_ends(self):
        tasks = MagicMock()
        self.mod.start_scan(self._req(), tasks)
        self.scanner.scan.side_effect = RuntimeError("System must be prepared before scanning.")
        self.mod._orchestrate_pipeline(self._req())
        self.assertFalse(self.mod._start_state["pending"])

    def test_refused_scan_does_not_overwrite_the_state(self):
        self.scanner.state = _State.RUNNING  # a scan is already running
        self.scanner.scan.side_effect = RuntimeError("System must be prepared before scanning.")
        self.mod._orchestrate_pipeline(self._req())
        self.assertEqual(self.scanner.state, _State.RUNNING)  # not flipped to ERROR

    def test_pipeline_asks_scan_to_finish_in_processing_state(self):
        self.scanner._cancel_event.is_set.return_value = False
        self.mod._orchestrate_pipeline(self._req())
        self.assertEqual(self.scanner.scan.call_args.kwargs["finish_state"], _State.PROCESSING)

    def test_cancel_flag_after_scan_is_treated_as_cancelled(self):
        self.scanner._cancel_event.is_set.return_value = True
        self.scanner.state = _State.IDLE  # what cleanup() leaves behind before emergency_stop() publishes CANCELLED
        self.mod._orchestrate_pipeline(self._req())
        self.assertEqual(self.scanner.state, _State.CANCELLED)
        self.mod.runpod.launch_job.assert_not_called()

    def test_scanner_is_built_lazily_and_once(self):
        self.assertEqual(self.created, [])  # importing the router opened no hardware
        self.assertFalse(self.mod.scanner.initialized)
        _ = self.mod.scanner.state
        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.mod.scanner.initialized)
        self.mod.scanner.state = "marker"   # attribute writes go to the real scanner
        self.assertEqual(self.scanner.state, "marker")
        self.assertEqual(len(self.created), 1)

    def test_real_failures_still_set_error(self):
        self.scanner.scan.side_effect = ValueError("bad angle")
        self.mod._orchestrate_pipeline(self._req())
        self.assertEqual(self.scanner.state, _State.ERROR)


if __name__ == "__main__":
    unittest.main()
