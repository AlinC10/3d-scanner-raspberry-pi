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
    - active_state: Automatically inferred by gpiozero (active-low when pull_up=True).
    - bounce_time=0.02: 20ms debounce filter to eliminate contact vibration.
    """
    def __init__(
        self,
        pin: int,
        pull_up: Optional[bool] = True,
        active_state: Optional[bool] = None,
        bounce_time: Optional[float] = 0.02,
    ):
        if pull_up is not None and active_state is not None:
            active_state = None

        super().__init__(
            pin=pin,
            pull_up=pull_up,
            active_state=active_state,
            bounce_time=bounce_time
        )