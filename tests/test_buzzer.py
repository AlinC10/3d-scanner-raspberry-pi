import importlib.util
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# Loaded by path: the package hardware/__init__ may pull in Pi-only modules
_spec = importlib.util.spec_from_file_location("buzzer_under_test", Path(__file__).parent.parent / "hardware" / "buzzer.py")
buzzer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(buzzer)


class FakeTonal:
    """Records what the buzzer was told to play."""

    def __init__(self, pin):
        self.pin = pin
        self.events = []
        self.closed = False

    def play(self, frequency):
        self.events.append(("play", frequency))

    def stop(self):
        self.events.append(("stop",))

    def close(self):
        self.closed = True


class TestBuzzer(unittest.TestCase):
    def setUp(self):
        self.devices = []

        def factory(pin):
            device = FakeTonal(pin)
            self.devices.append(device)
            return device

        self.factory = factory

    def test_pattern_plays_every_note_and_ends_silent(self):
        b = buzzer.Buzzer(pin=23, factory=self.factory)
        with patch.object(buzzer, "PATTERNS", {"t": [(440.0, 0.0), (None, 0.0), (880.0, 0.0)]}):
            b.play("t")
            b.wait(2)
        events = self.devices[0].events
        self.assertEqual([e for e in events if e[0] == "play"], [("play", 440.0), ("play", 880.0)])
        self.assertEqual(events[-1], ("stop",))

    def test_every_pattern_stays_in_the_tonal_buzzer_range(self):
        for name, notes in buzzer.PATTERNS.items():
            for frequency, duration in notes:
                self.assertGreater(duration, 0, name)
                if frequency is not None:
                    self.assertTrue(110 <= frequency <= 1760, (name, frequency))

    def test_new_pattern_interrupts_the_one_playing(self):
        b = buzzer.Buzzer(pin=23, factory=self.factory)
        with patch.object(buzzer, "PATTERNS", {"long": [(440.0, 30.0)], "short": [(880.0, 0.0)]}):
            b.play("long")
            first = b._thread
            b.play("short")
            b.wait(2)
            self.assertFalse(first.is_alive())
        self.assertIn(("play", 880.0), self.devices[0].events)

    def test_no_pin_or_failing_device_is_silent(self):
        def boom(pin):
            raise RuntimeError("no pin factory")

        for b in (buzzer.Buzzer(pin=None, from_env=False), buzzer.Buzzer(pin=23, factory=boom)):
            self.assertFalse(b.available)
            b.play("success")       # no exception, no thread
            self.assertIsNone(b._thread)
            b.close()

    def test_pin_from_env(self):
        for raw, expected in (("23", 23), ("off", None), ("", None), ("x", None)):
            with patch.dict("os.environ", {"BUZZER_GPIO_PIN": raw}):
                self.assertEqual(buzzer._pin_from_env(), expected)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(buzzer._pin_from_env(), buzzer.DEFAULT_BUZZER_PIN)

    def test_a_failing_device_does_not_raise_from_play(self):
        class Broken(FakeTonal):
            def play(self, frequency):
                raise ValueError("out of range")

        b = buzzer.Buzzer(pin=23, factory=Broken)
        b.play("ready")
        b.wait(2)
        b.close()
        self.assertIsNone(b._device)

    def test_tone_plays_one_note_and_checks_its_range(self):
        b = buzzer.Buzzer(pin=23, factory=self.factory)
        b.tone(440.0, 0.01)
        b.wait(2)
        self.assertIn(("play", 440.0), self.devices[0].events)
        for frequency, duration in ((100.0, 0.5), (2000.0, 0.5), (440.0, 0.0), (440.0, 6.0)):
            with self.assertRaises(ValueError):
                b.tone(frequency, duration)

    def test_close_releases_the_device(self):
        b = buzzer.Buzzer(pin=23, factory=self.factory)
        b.close()
        self.assertTrue(self.devices[0].closed)
        self.assertFalse(b.available)


if __name__ == "__main__":
    unittest.main()
