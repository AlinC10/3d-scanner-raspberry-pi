"""
WS-C tests (MOTOR_FIXES_PLAN.md, C1-C5): request schema limits and the manual hardware test router.

  - Schemas are checked with the REAL pydantic in a subprocess (tests/__init__.py replaces pydantic with a MagicMock
    in this process), the same way tests/test_schema_validation.py does it.
  - router/test.py is loaded by file path with stub fastapi / pydantic / scanner modules.
"""
import ast
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import TurntableRotateRequest, TurntableScanConfig, ZMoveRequest, MechanicalConfig, StartRequest

def ok(factory):
    try:
        factory()
        return True
    except Exception:
        return False

z = ZMoveRequest()
t = TurntableRotateRequest()
defaults = {
    "z": {k: getattr(z, k) for k in
          ("distance_mm", "direction", "delay", "acceleration", "start_delay", "ramp_percent", "decel_percent", "end_delay")},
    "turntable": {k: getattr(t, k) for k in
                  ("angle", "delay", "acceleration", "start_delay", "ramp_percent", "decel_percent", "end_delay")},
}

print(json.dumps({
    # scan configuration (MechanicalConfig.turntable)
    "scan_18": ok(lambda: TurntableScanConfig(angle=18.0)),
    "scan_22_5": ok(lambda: TurntableScanConfig(angle=22.5)),
    "scan_180": ok(lambda: TurntableScanConfig(angle=180.0)),
    "scan_33_75": ok(lambda: TurntableScanConfig(angle=33.75)),
    "scan_360": ok(lambda: TurntableScanConfig(angle=360.0)),
    "scan_10": ok(lambda: TurntableScanConfig(angle=10.0)),
    "nested_scan_360": ok(lambda: MechanicalConfig(turntable={"angle": 360.0})),
    "nested_scan_33_75": ok(lambda: MechanicalConfig(turntable={"angle": 33.75})),
    "nested_scan_22_5": ok(lambda: MechanicalConfig(turntable={"angle": 22.5})),
    "mechanical_default_type": type(MechanicalConfig().turntable).__name__,
    "mechanical_defaults": ok(lambda: MechanicalConfig()),
    "start_defaults": ok(lambda: StartRequest()),
    # manual test endpoint request
    "test_360": ok(lambda: TurntableRotateRequest(angle=360.0)),
    "test_33_75": ok(lambda: TurntableRotateRequest(angle=33.75)),
    "test_10": ok(lambda: TurntableRotateRequest(angle=10.0)),
    "test_361": ok(lambda: TurntableRotateRequest(angle=361.0)),
    # delays
    "t_delay_0": ok(lambda: TurntableRotateRequest(delay=0)),
    "t_delay_1": ok(lambda: TurntableRotateRequest(delay=1.0)),
    "t_delay_0_00001": ok(lambda: TurntableRotateRequest(delay=0.00001)),
    "t_delay_min": ok(lambda: TurntableRotateRequest(delay=0.0001)),
    "t_delay_max": ok(lambda: TurntableRotateRequest(delay=0.05)),
    "t_start_5": ok(lambda: TurntableRotateRequest(start_delay=5)),
    "t_start_ok": ok(lambda: TurntableRotateRequest(start_delay=0.004)),
    "z_delay_0": ok(lambda: ZMoveRequest(delay=0)),
    "z_delay_1": ok(lambda: ZMoveRequest(delay=1.0)),
    "z_delay_0_00001": ok(lambda: ZMoveRequest(delay=0.00001)),
    "z_delay_min": ok(lambda: ZMoveRequest(delay=0.0001)),
    "z_start_5": ok(lambda: ZMoveRequest(start_delay=5)),
    "z_start_ok": ok(lambda: ZMoveRequest(start_delay=0.004)),
    "scan_delay_1": ok(lambda: TurntableScanConfig(delay=1.0)),
    "nested_z_delay_1": ok(lambda: MechanicalConfig(z_axis={"delay": 1.0})),
    # Z distance
    "z_150": ok(lambda: ZMoveRequest(distance_mm=150.0)),
    "z_475": ok(lambda: ZMoveRequest(distance_mm=475.0)),
    "z_476": ok(lambda: ZMoveRequest(distance_mm=476.0)),
    "z_151": ok(lambda: ZMoveRequest(distance_mm=151.0)),
    "z_200": ok(lambda: ZMoveRequest(distance_mm=200.0)),
    "z_33_333": ok(lambda: ZMoveRequest(distance_mm=33.333)),
    "defaults": defaults,
}))
""" % ROOT


class TestSchemasWithRealPydantic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    # C3 / D1
    def test_scan_config_accepts_angles_that_divide_a_revolution(self):
        self.assertTrue(self.r["scan_18"])
        self.assertTrue(self.r["scan_22_5"])
        self.assertTrue(self.r["scan_180"])

    def test_scan_config_rejects_angles_that_do_not_close_the_circle(self):
        self.assertFalse(self.r["scan_33_75"])   # 300 steps: whole step, but 3200 % 300 != 0
        self.assertFalse(self.r["scan_360"])     # a single photo per ring
        self.assertFalse(self.r["scan_10"])      # not a whole number of steps

    def test_mechanical_config_uses_the_scan_config(self):
        self.assertEqual(self.r["mechanical_default_type"], "TurntableScanConfig")
        self.assertFalse(self.r["nested_scan_360"])
        self.assertFalse(self.r["nested_scan_33_75"])
        self.assertTrue(self.r["nested_scan_22_5"])
        self.assertTrue(self.r["mechanical_defaults"])
        self.assertTrue(self.r["start_defaults"])

    def test_manual_test_request_keeps_the_full_turn(self):
        self.assertTrue(self.r["test_360"])
        self.assertTrue(self.r["test_33_75"])    # whole steps only, no divisibility rule
        self.assertFalse(self.r["test_10"])
        self.assertFalse(self.r["test_361"])

    # C2
    def test_turntable_delay_bounds(self):
        self.assertFalse(self.r["t_delay_0"])
        self.assertFalse(self.r["t_delay_1"])
        self.assertFalse(self.r["t_delay_0_00001"])
        self.assertTrue(self.r["t_delay_min"])
        self.assertTrue(self.r["t_delay_max"])
        self.assertFalse(self.r["t_start_5"])
        self.assertTrue(self.r["t_start_ok"])

    def test_z_delay_bounds(self):
        self.assertFalse(self.r["z_delay_0"])
        self.assertFalse(self.r["z_delay_1"])
        self.assertFalse(self.r["z_delay_0_00001"])
        self.assertTrue(self.r["z_delay_min"])
        self.assertFalse(self.r["z_start_5"])
        self.assertTrue(self.r["z_start_ok"])

    def test_scan_config_delay_bounds_apply_to_the_nested_models(self):
        self.assertFalse(self.r["scan_delay_1"])
        self.assertFalse(self.r["nested_z_delay_1"])

    def test_z_distance_limit_is_the_travel(self):
        # the longest move is the whole travel of the rod (475 mm, hardware/rig_limits.py)
        self.assertTrue(self.r["z_150"])
        self.assertTrue(self.r["z_151"])
        self.assertTrue(self.r["z_200"])
        self.assertTrue(self.r["z_475"])
        self.assertFalse(self.r["z_476"])
        self.assertFalse(self.r["z_33_333"])

    # C4
    def test_z_move_defaults_match_scan_defaults(self):
        with open(os.path.join(ROOT, "hardware", "scanner.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        scan = next(
            node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "Scanner"
            for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "scan"
        )
        args = scan.args.args + scan.args.kwonlyargs
        defaults = [None] * (len(scan.args.args) - len(scan.args.defaults)) + list(scan.args.defaults) + list(scan.args.kw_defaults)
        scan_defaults = {}
        for arg, node in zip(args, defaults):
            if node is None:
                continue
            try:
                scan_defaults[arg.arg] = ast.literal_eval(node)
            except ValueError:
                pass  # a named constant, compared by the schema test below

        z = self.r["defaults"]["z"]
        self.assertEqual(z["acceleration"], False)
        self.assertEqual(z["delay"], 0.0010)
        self.assertIsNone(z["start_delay"])
        self.assertEqual(z["ramp_percent"], 0.2)
        self.assertIsNone(z["decel_percent"])
        self.assertIsNone(z["end_delay"])
        self.assertEqual(z["acceleration"], scan_defaults["z_acceleration"])
        self.assertEqual(z["delay"], scan_defaults["delay_z_motor"])
        self.assertEqual(z["start_delay"], scan_defaults["z_start_delay"])
        self.assertEqual(z["ramp_percent"], scan_defaults["z_ramp_percent"])
        self.assertEqual(z["end_delay"], scan_defaults["z_end_delay"])

        t = self.r["defaults"]["turntable"]
        self.assertEqual(t["acceleration"], scan_defaults["turntable_acceleration"])
        self.assertEqual(t["delay"], scan_defaults["delay_turntable"])
        self.assertEqual(t["start_delay"], scan_defaults["turntable_start_delay"])
        self.assertEqual(t["ramp_percent"], scan_defaults["turntable_ramp_percent"])


# ----------------------------------------------------------------------------------------------------------
# router/test.py with stubs
# ----------------------------------------------------------------------------------------------------------
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


class _QueryInfo:
    def __init__(self, default=None, **kwargs):
        self.default = default
        self.kwargs = kwargs


class _State:
    IDLE = "idle"
    RUNNING = "running"
    PREPARE_ALLOWED = ("idle", "completed", "cancelled", "error")


class _ZMoveRequest:
    distance_mm = 10.0
    direction = "up"
    delay = 0.001
    acceleration = False
    start_delay = None
    ramp_percent = 0.2
    decel_percent = None
    end_delay = None


class _TurntableRotateRequest:
    angle = 18.0
    clockwise = True
    delay = 0.001
    acceleration = True
    start_delay = None
    ramp_percent = 0.2
    decel_percent = None
    end_delay = 0.015


class _ScannerError(Exception):
    pass


class _ScannerBusyError(_ScannerError):
    pass


def _load_router(scanner):
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _APIRouter
    fastapi.HTTPException = _HTTPException
    fastapi.Query = _QueryInfo
    responses = types.ModuleType("fastapi.responses")
    responses.StreamingResponse = object
    fastapi.responses = responses
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type("BaseModel", (), {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)})
    pydantic.Field = lambda *args, **kwargs: None

    router_pkg = types.ModuleType("router")
    router_pkg.__path__ = []
    router_scanner = types.ModuleType("router.scanner")
    router_scanner.scanner = scanner
    router_scanner.ScannerState = _State
    for _name in ("scanner_stream_mjpeg", "scanner_stream_switch_active", "scanner_stream_set_quality",
                  "scanner_stream_sync_status"):
        setattr(router_scanner, _name, lambda *args, **kwargs: None)
    hw_scanner = types.ModuleType("hardware.scanner")
    hw_scanner.ScannerError = _ScannerError
    hw_scanner.ScannerBusyError = _ScannerBusyError
    hw_motor = types.ModuleType("hardware.motor")
    hw_motor.STEP_DELAY_MIN = 0.0001
    hw_motor.STEP_DELAY_MAX = 0.05
    endstop = types.ModuleType("hardware.endstop")
    endstop.TopEndstopTriggered = type("TopEndstopTriggered", (Exception,), {})
    endstop.BottomEndstopTriggered = type("BottomEndstopTriggered", (Exception,), {})
    lights = types.ModuleType("hardware.lights")
    lights.LightLockedError = type("LightLockedError", (Exception,), {})
    schemas = types.ModuleType("schemas.scanner")
    schemas.ZMoveRequest = _ZMoveRequest
    schemas.TurntableRotateRequest = _TurntableRotateRequest

    modules = {
        "fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic,
        "router": router_pkg, "router.scanner": router_scanner,
        "hardware.scanner": hw_scanner, "hardware.motor": hw_motor, "hardware.endstop": endstop,
        "hardware.lights": lights, "schemas.scanner": schemas,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("router_test_ws_c", os.path.join(ROOT, "router", "test.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _BusyGuard:
    """A non-blocking guard whose lock is already taken by someone else."""
    def __enter__(self):
        raise _ScannerBusyError("another motion is in progress")

    def __exit__(self, *exc):
        return False


class TestRouterMotionGuard(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.state = _State.IDLE
        self.mod = _load_router(self.scanner)

    def _busy(self):
        self.scanner.motion_guard.return_value = _BusyGuard()

    def _assert_409_and_untouched(self, call):
        with self.assertRaises(_HTTPException) as ctx:
            call()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("another motion", ctx.exception.detail.lower())
        for name in ("move_z_up_distance", "move_z_down_distance", "home_z_axis", "move_z_to_top"):
            getattr(self.scanner, name).assert_not_called()
        self.scanner.turntable_motor.rotate_angle.assert_not_called()
        # the running motion owns the coils: the rejected request must not enable or cut them
        self.scanner.turntable_motor.enable.assert_not_called()
        self.scanner.turntable_motor.disable.assert_not_called()
        self.scanner.z_axis_motor.enable.assert_not_called()
        self.scanner.z_axis_motor.disable.assert_not_called()

    def test_every_motion_endpoint_answers_409_when_busy(self):
        self._busy()
        self._assert_409_and_untouched(lambda: self.mod.test_z_move(_ZMoveRequest()))
        self._assert_409_and_untouched(lambda: self.mod.test_turntable_rotate(_TurntableRotateRequest()))
        self._assert_409_and_untouched(lambda: self.mod.test_home_bottom(0.0015, 0.004, 0.1))
        self._assert_409_and_untouched(lambda: self.mod.test_move_to_top(0.0015, 0.004, 0.1))

    def test_stream_start_takes_no_bitrate_and_skips_the_cloud_preflight(self):
        self.scanner.is_streaming = False
        self.scanner.stream_urls = None
        self.scanner.setup_and_stream.return_value = ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]
        self.mod.test_stream_start()
        self.scanner.setup_and_stream.assert_called_once_with(enable_stream=True, preflight=False)

    def test_camera_snap_skips_the_cloud_preflight(self):
        self.scanner.dual_cameras = None
        self.scanner.setup_and_stream.side_effect = lambda **kw: setattr(self.scanner, "dual_cameras", MagicMock())
        self.mod.test_camera_snap()
        self.scanner.setup_and_stream.assert_called_once_with(enable_stream=False, preflight=False)

    def test_busy_error_from_inside_the_motion_is_a_409_not_a_500(self):
        # e.g. the scanner method takes the guard itself and finds it taken
        busy = _ScannerBusyError("another motion is in progress")
        self.scanner.move_z_up_distance.side_effect = busy
        self.scanner.turntable_motor.rotate_angle.side_effect = busy
        self.scanner.home_z_axis.side_effect = busy
        self.scanner.move_z_to_top.side_effect = busy
        for call in (
            lambda: self.mod.test_z_move(_ZMoveRequest()),
            lambda: self.mod.test_turntable_rotate(_TurntableRotateRequest()),
            lambda: self.mod.test_home_bottom(0.0015, 0.004, 0.1),
            lambda: self.mod.test_move_to_top(0.0015, 0.004, 0.1),
        ):
            with self.assertRaises(_HTTPException) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 409)
        # whatever the rejected move did, the coils it energized are released again
        self.scanner.z_axis_motor.disable.assert_called()
        self.scanner.turntable_motor.disable.assert_called()

    def test_http_exceptions_pass_through_unchanged(self):
        self.scanner.home_z_axis.side_effect = _HTTPException(409, "x")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.test_home_bottom(0.0015, 0.004, 0.1)
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, "x"))

    def test_other_scanner_errors_stay_500(self):
        self.scanner.home_z_axis.side_effect = _ScannerError("switch not reached")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.test_home_bottom(0.0015, 0.004, 0.1)
        self.assertEqual(ctx.exception.status_code, 500)
        self.scanner.move_z_up_distance.side_effect = _ScannerError("boom")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.test_z_move(_ZMoveRequest())
        self.assertEqual(ctx.exception.status_code, 500)

    def test_motion_runs_inside_the_guard_and_releases_the_coils_before_it_ends(self):
        events = []
        guard = MagicMock()
        guard.__enter__.side_effect = lambda *a: events.append("enter")
        guard.__exit__.side_effect = lambda *a: events.append("exit") or False
        self.scanner.motion_guard.return_value = guard
        self.scanner.z_axis_motor.enable.side_effect = lambda: events.append("enable")
        self.scanner.z_axis_motor.disable.side_effect = lambda: events.append("disable")
        self.scanner.move_z_up_distance.side_effect = lambda **kw: events.append("move") or True
        out = self.mod.test_z_move(_ZMoveRequest())
        self.assertEqual(out["status"], "success")
        self.assertEqual(events, ["enter", "enable", "move", "disable", "exit"])

    def test_turntable_rotation_runs_inside_the_guard(self):
        self.scanner.turntable_motor.rotate_angle.return_value = True
        out = self.mod.test_turntable_rotate(_TurntableRotateRequest())
        self.assertEqual(out["status"], "success")
        self.scanner.motion_guard.assert_called_once_with()
        self.scanner.turntable_motor.disable.assert_called_once()

class TestRouterLimitsAndMessages(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        self.mod = _load_router(self.scanner)

    def test_homing_query_parameters_are_bounded(self):
        for fn in (self.mod.test_home_bottom, self.mod.test_move_to_top):
            params = inspect.signature(fn).parameters
            for name in ("delay", "start_delay"):
                kw = params[name].default.kwargs
                self.assertEqual((kw["ge"], kw["le"]), (0.0001, 0.05), f"{fn.__name__}.{name}")
            kw = params["ramp_percent"].default.kwargs
            self.assertEqual((kw["ge"], kw["le"]), (0.0, 1.0))

    def test_cleanup_409_points_to_scanner_cancel(self):
        for state in ("preparing", "prepared", "running"):
            self.scanner.state = state
            with self.assertRaises(_HTTPException) as ctx:
                self.mod.test_cleanup()
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("POST /scanner/cancel", ctx.exception.detail)
            self.scanner.cleanup.assert_not_called()

    def test_cleanup_works_when_idle(self):
        self.scanner.state = _State.IDLE
        self.assertEqual(self.mod.test_cleanup()["status"], "success")
        self.scanner.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
