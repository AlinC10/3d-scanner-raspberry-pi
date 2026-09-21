from gpiozero import Button
from typing import Optional, Literal

class TopEndstopTriggered(Exception):
    """Raised when the Z-Axis top endstop is triggered."""
    pass

class BottomEndstopTriggered(Exception):
    """Raised when the Z-Axis bottom endstop is triggered (home position)."""
    pass

class Endstop(Button):
    """
    Wrapper for mechanical/optical endstops using gpiozero.Button.

    Default configuration:
    - pull_up=True: Uses Pi's internal pull-up resistor (pin pulled to 3.3V).
    - active_state=False: Pin connected to GND when triggered (active-low).
    - bounce_time=0.02: 20ms debounce filter to eliminate contact vibration.
    """
    def __init__(
        self,
        pin: int,
        pull_up: bool = True,
        active_state: Optional[bool] = False,
        bounce_time: Optional[float] = 0.02,
    ):
        super().__init__(
            pin=pin,
            pull_up=pull_up,
            active_state=active_state,
            bounce_time=bounce_time
        )