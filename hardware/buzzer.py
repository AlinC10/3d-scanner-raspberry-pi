"""
Audible feedback on a tonal (passive) buzzer: short tone patterns for the key events of a scan.

The buzzer is optional. Its GPIO pin comes from ``BUZZER_GPIO_PIN`` (BCM numbering, default 23); an empty value,
``off`` or ``none`` turns it off. When gpiozero or the pin is not available (a PC, a missing buzzer) every call is a
no-op: a sound must never fail or slow a scan. Patterns play on a daemon thread, so ``play`` returns at once; a new
pattern interrupts the one still playing.
"""
import logging
import os
import threading
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_BUZZER_PIN = 23
# Range of TonalBuzzer(mid_tone="A4", octaves=2) and the longest single test tone
MIN_FREQUENCY_HZ = 110.0
MAX_FREQUENCY_HZ = 1760.0
MAX_TONE_S = 5.0

# One note: (frequency in Hz, or None for a pause; duration in seconds). gpiozero's TonalBuzzer with octaves=2 around
# A4 plays 110-1760 Hz; every frequency below stays inside that range.
Note = Tuple[Optional[float], float]

PATTERNS: Dict[str, List[Note]] = {
    # Prepare finished / scan started: one short confirmation beep
    "ready": [(880.0, 0.12)],
    "start": [(880.0, 0.12)],
    # Face scan: the cameras start locking on the face, the subject must hold still
    "hold_still": [(1320.0, 0.08), (None, 0.06), (1320.0, 0.08)],
    # Face scan: the photo is taken FACE_PHOTO_WARNING_S after the first beep ("eyes open"): two beeps, then a higher one
    "photo_countdown": [(1047.0, 0.1), (None, 0.4), (1047.0, 0.1), (None, 0.4), (1568.0, 0.2)],
    # Scan (and processing) finished: rising arpeggio
    "success": [(523.0, 0.12), (659.0, 0.12), (784.0, 0.12), (1047.0, 0.25)],
    # Error, cancel or emergency stop: low long tone, then fast alert beeps
    "error": [(220.0, 0.5), (None, 0.1), (880.0, 0.08), (None, 0.06), (880.0, 0.08), (None, 0.06), (880.0, 0.08)],
}


def _pin_from_env() -> Optional[int]:
    raw = os.environ.get("BUZZER_GPIO_PIN", str(DEFAULT_BUZZER_PIN)).strip().lower()
    if raw in ("", "off", "none", "0"):
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("[Buzzer] BUZZER_GPIO_PIN=%r is not a pin number; the buzzer is off.", raw)
        return None


def _gpiozero_buzzer(pin: int):
    from gpiozero import TonalBuzzer
    return TonalBuzzer(pin, mid_tone="A4", octaves=2)


class Buzzer:
    """Non-blocking tone patterns on a TonalBuzzer; silent (no-op) when the device cannot be opened."""

    def __init__(self, pin: Optional[int] = None, factory: Optional[Callable[[int], object]] = None,
                 from_env: bool = True):
        """
        :param pin: BCM pin; None reads ``BUZZER_GPIO_PIN`` (when `from_env`) or leaves the buzzer off.
        :param factory: builds the device from the pin (tests pass a fake); default gpiozero's TonalBuzzer.
        :param from_env: read the pin from the environment when `pin` is None.
        """
        if pin is None and from_env:
            pin = _pin_from_env()
        self.pin = pin
        self._device = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if pin is None:
            log.info("[Buzzer] No buzzer pin configured; sounds are off.")
            return
        try:
            self._device = (factory or _gpiozero_buzzer)(pin)
            log.info("[Buzzer] Tonal buzzer on GPIO %d.", pin)
        except Exception as e:   # no gpiozero, no pin factory (a PC), pin busy...
            log.warning("[Buzzer] Could not open the buzzer on GPIO %s (%s); sounds are off.", pin, e)
            self._device = None

    @property
    def available(self) -> bool:
        return self._device is not None

    def play(self, pattern: str) -> None:
        """Starts `pattern` (a key of PATTERNS) in the background, interrupting the one playing. Never raises."""
        notes = PATTERNS.get(pattern)
        if notes is None:
            log.warning("[Buzzer] Unknown pattern %r.", pattern)
            return
        self._start(notes, pattern)

    def tone(self, frequency_hz: float, duration_s: float) -> None:
        """
        One tone in the background (the manual test), interrupting the sound playing.

        :raises ValueError: frequency outside MIN_FREQUENCY_HZ..MAX_FREQUENCY_HZ or duration outside (0, MAX_TONE_S].
        """
        if not MIN_FREQUENCY_HZ <= frequency_hz <= MAX_FREQUENCY_HZ:
            raise ValueError(f"Frequency must be {MIN_FREQUENCY_HZ:g}-{MAX_FREQUENCY_HZ:g} Hz.")
        if not 0 < duration_s <= MAX_TONE_S:
            raise ValueError(f"Duration must be more than 0 and at most {MAX_TONE_S:g} s.")
        self._start([(float(frequency_hz), float(duration_s))], "tone")

    def _start(self, notes: List[Note], name: str) -> None:
        if self._device is None:
            return
        with self._lock:
            self._interrupt_locked()
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(target=self._run, args=(notes, stop), name=f"buzzer-{name}", daemon=True)
            self._thread.start()

    def wait(self, timeout: Optional[float] = None) -> None:
        """Waits for the pattern playing now to end (for tests and for a sound before shutdown)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _interrupt_locked(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
        self._thread = None

    def _run(self, notes: List[Note], stop: threading.Event) -> None:
        device = self._device
        try:
            for frequency, duration in notes:
                if stop.is_set():
                    break
                if frequency is None:
                    device.stop()
                else:
                    device.play(frequency)
                if stop.wait(duration):
                    break
        except Exception as e:
            log.warning("[Buzzer] Could not play a sound: %s", e)
        finally:
            try:
                device.stop()
            except Exception:
                pass

    def close(self) -> None:
        """Stops any sound and releases the pin."""
        with self._lock:
            self._interrupt_locked()
            device, self._device = self._device, None
        if device is not None:
            try:
                device.close()
            except Exception as e:
                log.warning("[Buzzer] Error closing the buzzer: %s", e)
