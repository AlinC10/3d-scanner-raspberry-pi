from typing import Tuple, Optional

from RpiMotorLib import RpiMotorLib
from gpiozero import DigitalOutputDevice, DigitalInputDevice


class MotorError(Exception):
    pass


class Motor:
    """
    Stepper motor controller for NEMA 17 stepper motor with DRV8825 driver.
    """
    nema17_step_angle = 1.8  # degrees

    def __init__(
            self,
            dir_pin: int = 20,
            step_pin: int = 21,
            mode_pins: Tuple[int, int, int] = (-1, -1, -1),
            en_pin: Optional[int] = None,
            flt_pin: Optional[int] = None
    ) -> None:
        """
        Initializes the NEMA 17 stepper motor with the DRV8825 driver.

        :param dir_pin: GPIO pin connected to DIR on the driver (defaults to 20).
        :type dir_pin: int
        :param step_pin: GPIO pin connected to STEP on the driver (defaults to 21).
        :type step_pin: int
        :param mode_pins: Tuple of (M0, M1, M2) GPIO pins used for microstepping.
                          Set to (-1, -1, -1) to disable library control when
                          microstepping is physically hardwired. Defaults to (-1, -1, -1).
        :type mode_pins: Tuple[int, int, int]
        :param en_pin: GPIO pin connected to EN on the driver to enable/disable it.
                       Defaults to 16. Pass None if EN pin is hardwired or unused.
        """
        self.motor = RpiMotorLib.A4988Nema(dir_pin, step_pin, mode_pins, "DRV8825")

        self.en_device = None
        if en_pin is not None:
            self.en_device = DigitalOutputDevice(en_pin)
            self.disable()  # Default to disabled until explicitly enabled

        self.flt_device = None
        if flt_pin is not None:
            # active_state=False means is_active=True when pin is LOW (fault condition)
            self.flt_device = DigitalInputDevice(
                pin=flt_pin,
                pull_up=True,
                active_state=False,
                bounce_time=0.1
            )
            self.flt_device.when_activated = self.handle_driver_fault

    def handle_driver_fault(self) -> None:
        self.stop()
        raise MotorError("Driver Fault Triggered! Stopping motor..")

    def enable(self) -> None:
        """
        Enables the motor driver (sets EN pin LOW).
        """
        if self.en_device:
            self.en_device.off()

    def disable(self) -> None:
        """
        Disables the motor driver (sets EN pin HIGH).
        """
        if self.en_device:
            self.en_device.on()

    def angle_to_steps_conversion(self, angle: int | float = 18, step_type: str = "Full"):
        # Determine multiplier based on step_type
        if "/" in step_type:
            num, den = step_type.split("/")
            num, den = int(num), int(den)
        elif step_type == "Half":
            num, den = 1, 2
        else:
            num, den = 1, 1

        step_angle = self.nema17_step_angle * num / den

        # Calculate exactly how many steps this requires
        steps_float = angle / step_angle

        # Check if the required steps is a whole number (allowing a tiny margin for float math precision)
        if abs(steps_float - round(steps_float)) > 1e-4:
            raise MotorError(
                f"Cannot rotate exactly {angle}° using '{step_type}' microstepping. "
                f"This requires {steps_float:.2f} steps, which is not a whole integer. "
                f"Please choose a different step_type (e.g. 1/8, 1/16) or change the angle."
            )

        steps = round(steps_float)
        return steps

    def rotate_angle(
            self,
            clockwise: bool = True,
            angle: int | float = 18,
            delay: float | int = 0.002,
            step_type: str = "Full",
            verbose: bool = False,
            initial_delay: float | int = 0.05) -> None:
        """
        Rotates the motor by a specific angle. Verifies if the requested angle
        is perfectly divisible by the given step_type. If not, raises an error.
        """
        steps = self.angle_to_steps_conversion(angle, step_type)
        
        if steps > 0:
            self.rotate(
                clockwise=clockwise, 
                steps=steps, 
                delay=delay, 
                step_type=step_type, 
                verbose=verbose,
                initial_delay=initial_delay
            )


    def rotate(
            self,
            clockwise: bool = True,
            steps: int = 200,
            delay: float | int = 0.002,
            step_type: str = "Full",
            verbose: bool = False,
            initial_delay: float | int = 0.05) -> None:
        """
        Rotates the motor.

        :param clockwise: Direction of rotation, True for clockwise, False for counter-clockwise.
        :type clockwise: bool
        :param steps: Number of steps to rotate.
        :type steps: int
        :param delay: Step delay in seconds between pulses.
        :type delay: float | int
        :param step_type: Microstepping mode (e.g., "Full", "Half", "1/32").
                          This string is ignored by the hardware if mode_pins are set to (-1, -1, -1).
        :type step_type: str
        :param verbose: Write pin actions
        :type verbose: bool
        :param initial_delay: Initial delay after GPIO pins initialized but before motor is moved.
        :type initial_delay: float | int
        """
        # motor_go parameters: clockwise (bool), step_type (str), steps (int), step_delay (float), verbose (bool),
        # initial_delay (float)
        self.motor.motor_go(clockwise, step_type, steps, delay, verbose, initial_delay)

    def stop(self, release_torque: Optional[bool] = False) -> None:
        """
        Interrupts a currently running motor_go loop.
        """
        self.motor.motor_stop()
        if release_torque:
            self.disable()
