import importlib.util
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

TOF_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hardware", "tof.py")


class FakeVL53L1X:
    """Stand-in for adafruit_vl53l1x.VL53L1X that records every access in order."""
    instances = []

    def __init__(self, i2c):
        self.events = []
        self.cm_values = [40.0]   # distance returned by successive reads (last value repeats)
        self.ready = True
        self.read_delay = 0.0
        self._reads = 0
        FakeVL53L1X.instances.append(self)

    def __setattr__(self, name, value):
        if name in ("distance_mode", "timing_budget", "roi_xy", "roi_center"):
            self.events.append(("set", name, value))
        object.__setattr__(self, name, value)

    def _tid(self):
        return threading.get_ident()

    def start_ranging(self):
        self.events.append(("start_ranging",))

    def stop_ranging(self):
        self.events.append(("stop_ranging",))

    def clear_interrupt(self):
        self.events.append(("clear", self._tid()))

    @property
    def data_ready(self):
        self.events.append(("ready", self._tid()))
        return self.ready

    @property
    def distance(self):
        self.events.append(("dist", self._tid()))
        if self.read_delay:
            time.sleep(self.read_delay)
        idx = min(self._reads, len(self.cm_values) - 1)
        self._reads += 1
        return self.cm_values[idx]


def load_tof_module(sensor_cls):
    """Loads the real hardware/tof.py (tests/__init__.py stubs 'hardware.tof' with a MagicMock)."""
    fake_lib = MagicMock()
    fake_lib.VL53L1X = sensor_cls
    with patch.dict(sys.modules, {"board": MagicMock(), "busio": MagicMock(), "adafruit_vl53l1x": fake_lib}):
        spec = importlib.util.spec_from_file_location("tof_real", TOF_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def make_sensor(**kwargs):
    FakeVL53L1X.instances.clear()
    module = load_tof_module(FakeVL53L1X)
    with patch("atexit.register"):
        tof = module.ToFSensor(**kwargs)
    return tof, (FakeVL53L1X.instances[-1] if FakeVL53L1X.instances else None)


class ToFSensorTests(unittest.TestCase):
    def test_centimetres_are_converted_to_millimetres(self):
        tof, fake = make_sensor()
        fake.cm_values = [40.2]
        self.assertAlmostEqual(tof.get_distance_mm(), 402.0)

    def test_invalid_range_status_returns_inf(self):
        tof, fake = make_sensor()
        fake.cm_values = [None]
        self.assertEqual(tof.get_distance_mm(), float("inf"))

    def test_timeout_when_no_measurement_returns_inf(self):
        tof, fake = make_sensor(timing_budget_ms=15)
        fake.ready = False
        start = time.monotonic()
        self.assertEqual(tof.get_distance_mm(), float("inf"))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_roi_center_is_set_after_roi_xy(self):
        tof, fake = make_sensor()
        names = [e[1] for e in fake.events if e[0] == "set"]
        self.assertEqual(names, ["distance_mode", "timing_budget", "roi_xy", "roi_center"])
        self.assertLess(fake.events.index(("set", "roi_center", 199)), fake.events.index(("start_ranging",)))
        self.assertIn(("set", "roi_xy", (4, 4)), fake.events)
        self.assertIn(("set", "timing_budget", 200), fake.events)

    def test_constructor_failure_leaves_sensor_unavailable(self):
        class Broken:
            def __init__(self, i2c):
                raise RuntimeError("Wrong sensor ID or type!")

        module = load_tof_module(Broken)
        with patch("atexit.register"):
            tof = module.ToFSensor()
        self.assertFalse(tof.available)
        self.assertEqual(tof.get_distance_mm(), float("inf"))
        tof.close()  # must not raise

    def test_is_object_detected_needs_two_readings_below_threshold(self):
        tof, fake = make_sensor()
        fake.cm_values = [30.0, 80.0]      # 300 mm then 800 mm
        self.assertFalse(tof.is_object_detected(threshold=510))

        tof, fake = make_sensor()
        fake.cm_values = [30.0, 31.0]
        self.assertTrue(tof.is_object_detected(threshold=510))

    def test_is_object_detected_stops_after_first_clear_reading(self):
        tof, fake = make_sensor()
        fake.cm_values = [80.0, 30.0]
        self.assertFalse(tof.is_object_detected(threshold=510))
        self.assertEqual(fake._reads, 1)

    def test_every_read_flushes_before_waiting(self):
        tof, fake = make_sensor()
        fake.events.clear()
        tof.get_distance_mm()
        kinds = [e[0] for e in fake.events]
        self.assertEqual(kinds[0], "clear")
        self.assertEqual(kinds[-1], "clear")
        self.assertIn("dist", kinds)

    def test_concurrent_reads_do_not_interleave(self):
        tof, fake = make_sensor()
        fake.read_delay = 0.01      # widens the window in which a missing lock would show
        fake.events.clear()

        threads = [threading.Thread(target=lambda: [tof.get_distance_mm() for _ in range(5)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = fake.events
        # one read = clear, ready (the running measurement), clear, ready (the next one), dist, clear: all from the
        # same thread
        self.assertEqual(len(events) % 6, 0)
        for i in range(0, len(events), 6):
            group = events[i:i + 6]
            self.assertEqual([e[0] for e in group], ["clear", "ready", "clear", "ready", "dist", "clear"])
            self.assertEqual(len({e[1] for e in group}), 1)

    def test_close_is_idempotent(self):
        tof, fake = make_sensor()
        i2c = tof.i2c
        tof.close()
        tof.close()
        self.assertEqual([e for e in fake.events if e == ("stop_ranging",)], [("stop_ranging",)])
        i2c.deinit.assert_called_once()
        self.assertFalse(tof.available)
        self.assertEqual(tof.get_distance_mm(), float("inf"))


if __name__ == "__main__":
    unittest.main()
