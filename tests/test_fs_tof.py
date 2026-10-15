"""
First-scan package P4 (PERI-2/3/10/11, SCAN-5): the ToF sensor must keep "invalid reading" apart from
"no object", and /test/tof must answer 200 with a shape the operator can read when the reading is invalid.
The real hardware/tof.py is loaded by path with fake board/busio/adafruit_vl53l1x modules.
"""
import importlib.util
import json
import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from tests.test_medium_fixes import _load, _web_stubs

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOF_PATH = os.path.join(ROOT, "hardware", "tof.py")


class FakeDriver:
    """Scripted VL53L1X: `script` entries are cm values, None (invalid status), "timeout" or an Exception."""
    instances = []

    def __init__(self, i2c):
        self.script = []
        self.reads = 0
        self.stopped = False
        FakeDriver.instances.append(self)

    def start_ranging(self):
        pass

    def stop_ranging(self):
        self.stopped = True

    def clear_interrupt(self):
        pass

    @property
    def data_ready(self):
        return not (self.script and self.script[0] == "timeout")

    @property
    def distance(self):
        item = self.script.pop(0) if self.script else 53.5
        self.reads += 1
        if isinstance(item, Exception):
            raise item
        return item


def load_tof(driver_cls=FakeDriver, missing=()):
    """Loads hardware/tof.py; modules named in `missing` are blocked so importing them raises ImportError."""
    fake_lib = MagicMock()
    fake_lib.VL53L1X = driver_cls
    modules = {"board": MagicMock(), "busio": MagicMock(), "adafruit_vl53l1x": fake_lib}
    for name in missing:
        modules[name] = None
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("tof_fs_under_test", TOF_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def make_sensor(script=None, **kwargs):
    FakeDriver.instances.clear()
    module = load_tof()
    with patch("atexit.register"):
        tof = module.ToFSensor(timing_budget_ms=kwargs.pop("timing_budget_ms", 15), **kwargs)
    fake = FakeDriver.instances[-1]
    fake.script = list(script or [])
    return tof, fake


class TestReadDistance(unittest.TestCase):
    def test_valid_reading_is_millimetres(self):
        tof, _ = make_sensor([40.2])
        self.assertAlmostEqual(tof.read_distance_mm(), 402.0)

    def test_driver_none_is_none_not_inf(self):
        tof, _ = make_sensor([None])
        self.assertIsNone(tof.read_distance_mm())

    def test_timeout_is_none(self):
        tof, _ = make_sensor(["timeout"])
        start = time.monotonic()
        self.assertIsNone(tof.read_distance_mm())
        self.assertLess(time.monotonic() - start, 1.0)

    def test_driver_exception_is_none(self):
        tof, _ = make_sensor([OSError("I2C error")])
        self.assertIsNone(tof.read_distance_mm())

    def test_non_finite_or_non_positive_is_none(self):
        tof, _ = make_sensor([float("inf"), float("nan"), 0, -1.0])
        for _ in range(4):
            self.assertIsNone(tof.read_distance_mm())

    def test_get_distance_mm_keeps_inf_for_legacy_callers(self):
        tof, _ = make_sensor([None, 30.0])
        self.assertEqual(tof.get_distance_mm(), float("inf"))
        self.assertAlmostEqual(tof.get_distance_mm(), 300.0)

    def test_get_distance_mm_is_built_on_read_distance_mm(self):
        tof, _ = make_sensor()
        with patch.object(tof, "read_distance_mm", return_value=None):
            self.assertEqual(tof.get_distance_mm(), float("inf"))
        with patch.object(tof, "read_distance_mm", return_value=123.0):
            self.assertEqual(tof.get_distance_mm(), 123.0)


class TestProbeObject(unittest.TestCase):
    THRESHOLD = 510.0

    def probe(self, script, **kwargs):
        tof, fake = make_sensor(script)
        return tof.probe_object(self.THRESHOLD, **kwargs), fake

    def test_two_valid_close_readings_is_true(self):
        result, fake = self.probe([30.0, 31.0])
        self.assertIs(result, True)
        self.assertEqual(fake.reads, 2)

    def test_valid_far_first_is_false_and_short_circuits(self):
        result, fake = self.probe([53.5, 30.0])
        self.assertIs(result, False)
        self.assertEqual(fake.reads, 1)

    def test_reading_exactly_at_threshold_is_false(self):
        result, _ = self.probe([51.0, 30.0])
        self.assertIs(result, False)

    def test_invalid_first_is_none(self):
        result, fake = self.probe([None, 30.0])
        self.assertIsNone(result)
        self.assertEqual(fake.reads, 1)

    def test_close_then_invalid_is_none(self):
        result, _ = self.probe([30.0, None])
        self.assertIsNone(result)

    def test_close_then_timeout_is_none(self):
        result, _ = self.probe([30.0, "timeout"])
        self.assertIsNone(result)

    def test_close_then_far_is_false(self):
        result, _ = self.probe([30.0, 53.5])
        self.assertIs(result, False)

    def test_far_then_anything_is_false(self):
        for follow in (None, 30.0, OSError("x")):
            result, fake = self.probe([53.5, follow])
            self.assertIs(result, False)
            self.assertEqual(fake.reads, 1)

    def test_three_samples(self):
        result, fake = self.probe([30.0, 31.0, 32.0], samples=3)
        self.assertIs(result, True)
        self.assertEqual(fake.reads, 3)

    def test_is_object_detected_is_true_only_for_true(self):
        tof, _ = make_sensor([30.0, 31.0])
        self.assertTrue(tof.is_object_detected(self.THRESHOLD))
        tof, _ = make_sensor([53.5])
        self.assertFalse(tof.is_object_detected(self.THRESHOLD))
        tof, _ = make_sensor([None])
        self.assertFalse(tof.is_object_detected(self.THRESHOLD))

    def test_unavailable_sensor_probe_is_none(self):
        tof, _ = make_sensor()
        tof.available = False
        self.assertIsNone(tof.probe_object(self.THRESHOLD))


class TestRobustInit(unittest.TestCase):
    def test_import_does_not_raise_when_libraries_are_missing(self):
        for missing in (("board",), ("busio",), ("adafruit_vl53l1x",), ("board", "busio", "adafruit_vl53l1x")):
            module = load_tof(missing=missing)
            tof = module.ToFSensor()
            self.assertFalse(tof.available)
            tof.close()

    def test_unavailable_sensor_reads_none_without_waiting(self):
        module = load_tof(missing=("adafruit_vl53l1x",))
        tof = module.ToFSensor(timing_budget_ms=500)
        start = time.monotonic()
        self.assertIsNone(tof.read_distance_mm())
        self.assertEqual(tof.get_distance_mm(), float("inf"))
        self.assertIsNone(tof.probe_object(510.0))
        self.assertFalse(tof.is_object_detected(510.0))
        self.assertLess(time.monotonic() - start, 0.1)

    def test_failed_driver_init_deinits_the_bus(self):
        class Broken:
            def __init__(self, i2c):
                raise RuntimeError("Wrong sensor ID or type!")

        module = load_tof(Broken)
        bus = MagicMock()
        module.busio.I2C = MagicMock(return_value=bus)
        with patch("atexit.register") as register:
            tof = module.ToFSensor()
        bus.deinit.assert_called_once()
        self.assertFalse(tof.available)
        self.assertIsNone(tof.i2c)
        register.assert_not_called()
        self.assertIsNone(tof.read_distance_mm())
        tof.close()
        bus.deinit.assert_called_once()

    def test_failed_bus_open_is_unavailable(self):
        module = load_tof()
        module.busio.I2C = MagicMock(side_effect=OSError("no bus"))
        tof = module.ToFSensor()
        self.assertFalse(tof.available)
        tof.close()

    def test_close_is_idempotent(self):
        tof, fake = make_sensor()
        bus = tof.i2c
        tof.close()
        tof.close()
        self.assertTrue(fake.stopped)
        bus.deinit.assert_called_once()
        self.assertFalse(tof.available)
        self.assertIsNone(tof.read_distance_mm())


class TestToFEndpoint(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        fake_router_scanner = types.ModuleType("router.scanner")
        fake_router_scanner.scanner = self.scanner
        fake_router_scanner.ScannerState = MagicMock()
        router_pkg = types.ModuleType("router")
        router_pkg.__path__ = []
        fake_hw_scanner = types.ModuleType("hardware.scanner")
        fake_hw_scanner.ScannerError = type("ScannerError", (Exception,), {})
        fake_hw_scanner.ScannerBusyError = type("ScannerBusyError", (fake_hw_scanner.ScannerError,), {})
        fake_endstop = types.ModuleType("hardware.endstop")
        fake_endstop.TopEndstopTriggered = type("TopEndstopTriggered", (Exception,), {})
        fake_endstop.BottomEndstopTriggered = type("BottomEndstopTriggered", (Exception,), {})
        fake_lights = types.ModuleType("hardware.lights")
        fake_lights.LightLockedError = type("LightLockedError", (Exception,), {})
        fake_motor = types.ModuleType("hardware.motor")
        fake_motor.STEP_DELAY_MIN = 0.0
        fake_motor.STEP_DELAY_MAX = 1.0
        fake_schemas = types.ModuleType("schemas.scanner")
        for name in ("ZMoveRequest", "TurntableRotateRequest"):
            setattr(fake_schemas, name, type(name, (), {}))
        self.mod = _load("router/test.py", "router_test_tof_under_test", {
            **_web_stubs(),
            "router": router_pkg,
            "router.scanner": fake_router_scanner,
            "hardware.scanner": fake_hw_scanner,
            "hardware.endstop": fake_endstop,
            "hardware.lights": fake_lights,
            "hardware.motor": fake_motor,
            "schemas.scanner": fake_schemas,
        })

    def test_valid_reading(self):
        self.scanner.tof.available = True
        self.scanner.tof.read_distance_mm.return_value = 535.2
        self.assertEqual(self.mod.get_tof_distance(), {
            "status": "success", "distance_mm": 535.2, "valid": True, "available": True})

    def test_invalid_reading_is_200_with_null_distance(self):
        self.scanner.tof.available = True
        self.scanner.tof.read_distance_mm.return_value = None
        self.assertEqual(self.mod.get_tof_distance(), {
            "status": "invalid", "distance_mm": None, "valid": False, "available": True})

    def test_unavailable_sensor(self):
        self.scanner.tof.available = False
        self.scanner.tof.read_distance_mm.return_value = None
        self.assertEqual(self.mod.get_tof_distance(), {
            "status": "invalid", "distance_mm": None, "valid": False, "available": False})

    def test_read_exception_does_not_become_500(self):
        self.scanner.tof.available = True
        self.scanner.tof.read_distance_mm.side_effect = RuntimeError("boom")
        result = self.mod.get_tof_distance()
        self.assertFalse(result["valid"])
        self.assertIsNone(result["distance_mm"])
        self.assertTrue(result["available"])

    def test_response_is_json_safe(self):
        self.scanner.tof.available = True
        self.scanner.tof.read_distance_mm.return_value = None
        json.dumps(self.mod.get_tof_distance(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
