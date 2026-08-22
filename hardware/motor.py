from typing import Tuple
from RpiMotorLib import RpiMotorLib


class Motor:
    """
    Stepper motor controller for NEMA 17 stepper motor with DRV8825 driver.
    """

    def __init__(
        self,
        dir_pin: int = 20,
        step_pin: int = 21,
        mode_pins: Tuple[int, int, int] = (14, 15, 18),
    ) -> None:
        """
        Initializes the NEMA 17 stepper motor with the DRV8825 driver.

        :param dir_pin: GPIO pin connected to DIR on the driver.
        :type dir_pin: int
        :param step_pin: GPIO pin connected to STEP on the driver.
        :type step_pin: int
        :param mode_pins: Tuple of (M0, M1, M2) GPIO pins used for microstepping (defaults to (14, 15, 18)).
        :type mode_pins: tuple[int, int, int]
        """
        self.motor = RpiMotorLib.A4988Nema(dir_pin, step_pin, mode_pins, "DRV8825")

    def rotate(
        self,
        clockwise: bool = True,
        steps: int = 200,
        delay: float = 0.002,
        step_type: str = "Full",
    ) -> None:
        """
        Rotates the motor.

        :param clockwise: Direction of rotation, True for clockwise, False for counter-clockwise.
        :type clockwise: bool
        :param steps: Number of steps to rotate.
        :type steps: int
        :param delay: Step delay in seconds between pulses.
        :type delay: float
        :param step_type: Microstepping mode ("Full", "Half", "1/4", "1/8", "1/16", "1/32").
        :type step_type: str
        """
        # motor_go parameters: clockwise (bool), step_type (str), steps (int), step_delay (float), verbose (bool), initial_delay (float)
        self.motor.motor_go(clockwise, step_type, steps, delay, False, 0.05)

    def stop(self) -> None:
        """
        Interrupts a currently running motor_go loop.
        """
        self.motor.motor_stop()
