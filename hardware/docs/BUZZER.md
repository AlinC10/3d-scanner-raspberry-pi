# Buzzer (`hardware/buzzer.py`)

A passive (tonal) buzzer gives audible feedback on the key events of a scan. It is optional: when it is missing or
cannot be opened, every call is a silent no-op and the scan is not affected.

## Wiring and configuration

| Setting | Value |
|---|---|
| Device | `gpiozero.TonalBuzzer(pin, mid_tone="A4", octaves=2)`: 110-1760 Hz, software PWM on the Pi 5 |
| Pin | `BUZZER_GPIO_PIN` (BCM), default `23`; `off`, `none`, `0` or empty turns it off |
| Tests / PC | `tests/__init__.py` sets `BUZZER_GPIO_PIN=off`; on a PC without a GPIO pin factory the buzzer logs a warning and stays off |

## Sounds

`Buzzer.play(pattern)` returns at once: the pattern plays on a daemon thread and a new pattern interrupts the one still
playing. `Scanner` plays them from the `state` setter (every state change, including the ones the routers make) and
once in the face scan:

| Event | When | Pattern |
|---|---|---|
| `ready` | state becomes `prepared` (object or face prepare finished) | one short beep (880 Hz) |
| `start` | state becomes `running` (scan or face capture started) | one short beep (880 Hz) |
| `hold_still` | face scan: right before the cameras focus and lock on the face | two short high beeps (1320 Hz) |
| `success` | state becomes `completed` | rising arpeggio C5-E5-G5-C6 |
| `error` | state becomes `error` or `cancelled` (cancel and emergency stop included) | low long tone, then three fast beeps |

Manual test: `GET /test/buzzer`, `POST /test/buzzer/play` `{"pattern": "..."}`, `POST /test/buzzer/tone`
`{"frequency_hz": 880, "duration_s": 0.5}` (`Buzzer.tone`, 110-1760 Hz, at most 5 s); see `router/docs/TEST_API.md`.
In the development app (`main_dev.py`) the simulated rig has a fake buzzer that only logs.

The patterns are in `PATTERNS` (`hardware/buzzer.py`), the state mapping in `STATE_SOUNDS` (`hardware/scanner.py`).
The development rig (`ScannerMock`) does not play the state sounds.

Covered by tests (`tests/test_buzzer.py`, `TestStateSounds` in `tests/test_scanner_motion_safety.py`, the `/test/buzzer`
endpoints in `tests/test_dev_app.py`), not confirmed on the rig.
