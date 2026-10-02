import threading
import time
from typing import Optional

from gpiozero import DigitalOutputDevice

T8_THREADED_ROD_STEP = 8  # mm


class MotorError(Exception):
    pass


class Motor:
    """
    Stepper motor controller for NEMA 17 stepper motor with TB6600 driver.
    """
    nema17_step_angle = 1.8  # degrees

    def __init__(
            self,
            dir_pin: int = 20,
            step_pin: int = 21,
            en_pin: Optional[int] = None,
            step_type: str = "Full"
    ) -> None:
        """
        Initializes the NEMA 17 stepper motor with the TB6600 driver.

        :param dir_pin: GPIO pin connected to DIR on the driver (defaults to 20).
        :type dir_pin: int
        :param step_pin: GPIO pin connected to STEP on the driver (defaults to 21).
        :type step_pin: int
        :param en_pin: GPIO pin connected to EN on the driver to enable/disable it.
                       Defaults to 16. Pass None if EN pin is hardwired or unused.
        :type en_pin: Optional[int]
        :param step_type: Default microstepping mode string.
        :type step_type: str
        """
        self.dir_device = DigitalOutputDevice(dir_pin)
        self.step_device = DigitalOutputDevice(step_pin)

        self.en_device = None
        if en_pin is not None:
            self.en_device = DigitalOutputDevice(en_pin)
            self.disable()  # Default to disabled until explicitly enabled

        self.step_type = step_type
        self._stop_event = threading.Event()

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

    def angle_to_steps_conversion(self, angle: int | float = 18, step_type: Optional[str] = None):
        if step_type is None:
            step_type = self.step_type

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
                f"Cannot rotate exactly {angle}° using '{step_type}' step type."
                f"This requires {steps_float:.2f} steps, which is not a whole integer. "
            )

        steps = round(steps_float)
        return steps

    def distance_to_step_conversion(self, distance: float = 100.0, step_type: Optional[str] = None):
        degrees_to_rotate = (distance / T8_THREADED_ROD_STEP) * 360.0

        return self.angle_to_steps_conversion(degrees_to_rotate, step_type)

    def rotate_angle(
            self,
            clockwise: bool = True,
            angle: int | float = 18,
            delay: float | int = 0.002,
            step_type: Optional[str] = None,
            verbose: bool = False,
            initial_delay: float | int = 0.05,
            acceleration: bool = True,
            start_delay: Optional[float] = None,
            ramp_percent: float = 0.2,
            cancel_event: Optional[threading.Event] = None) -> None:
        """
        Rotates the motor by a specific angle. Verifies if the requested angle
        is perfectly divisible by the given step_type. If not, raises an error.
        """
        if step_type is None:
            step_type = self.step_type

        steps = self.angle_to_steps_conversion(angle, step_type)
        
        if steps > 0:
            self.rotate(
                clockwise=clockwise, 
                steps=steps, 
                delay=delay, 
                step_type=step_type, 
                verbose=verbose,
                initial_delay=initial_delay,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                cancel_event=cancel_event
            )

    def rotate_distance(
            self,
            clockwise: bool = True,
            distance: int | float = 100.0,
            delay: float | int = 0.002,
            step_type: Optional[str] = None,
            verbose: bool = False,
            initial_delay: float | int = 0.05,
            acceleration: bool = True,
            start_delay: Optional[float] = None,
            ramp_percent: float = 0.2,
            cancel_event: Optional[threading.Event] = None) -> None:
        if step_type is None:
            step_type = self.step_type

        steps = self.distance_to_step_conversion(distance, step_type)
        if steps > 0:
            self.rotate(
                clockwise=clockwise,
                steps=steps,
                delay=delay,
                step_type=step_type,
                verbose=verbose,
                initial_delay=initial_delay,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                cancel_event=cancel_event
            )

    def rotate(
            self,
            clockwise: bool = True,
            steps: int = 200,
            delay: float | int = 0.002,
            step_type: Optional[str] = None,
            verbose: bool = False,
            initial_delay: float | int = 0.05,
            acceleration: bool = True,
            start_delay: Optional[float] = None,
            ramp_percent: float = 0.2,
            cancel_event: Optional[threading.Event] = None) -> None:
        """
        Rotates the motor using native GPIO stepping with optional trapezoidal
        acceleration and deceleration profiling to prevent inertial overshoot.

        :param clockwise: Direction of rotation, True for clockwise, False for counter-clockwise.
        :type clockwise: bool
        :param steps: Number of steps to rotate.
        :type steps: int
        :param delay: Cruise step delay in seconds between pulses.
        :type delay: float | int
        :param step_type: Microstepping mode (retained for signature compatibility).
        :type step_type: Optional[str]
        :param verbose: Write step diagnostics if True.
        :type verbose: bool
        :param initial_delay: Initial delay after setting direction pin before stepping.
        :type initial_delay: float | int
        :param acceleration: Whether to apply acceleration/deceleration ramping. Defaults to True.
        :type acceleration: bool
        :param start_delay: Starting/stopping delay for ramp. Defaults to max(delay * 3.0, 0.0025).
        :type start_delay: Optional[float]
        :param ramp_percent: Fraction of total steps spent accelerating and decelerating (0.0 to 0.5).
        :type ramp_percent: float
        :param cancel_event: Optional external threading.Event for cooperative cancellation.
        :type cancel_event: Optional[threading.Event]
        """
        if steps <= 0:
            return

        self._stop_event.clear()
        self.dir_device.value = 1 if clockwise else 0
        if initial_delay > 0:
            time.sleep(initial_delay)

        target_delay = float(delay)
        
        if acceleration:
            cushion_delay = float(start_delay) if start_delay is not None else max(target_delay * 3.0, 0.0025)
            bounded_ramp_percent = max(0.0, min(float(ramp_percent), 0.5))
            ramp_steps = min(int(steps * bounded_ramp_percent), steps // 2)
        else:
            cushion_delay = target_delay
            ramp_steps = 0

        for i in range(steps):
            if self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                if verbose:
                    print(f"[Motor] Interrupted at step {i}/{steps}")
                break

            # Calculate delay for this step (trapezoidal profile)
            if ramp_steps > 0:
                if i < ramp_steps:
                    progress = i / ramp_steps
                    current_delay = cushion_delay - progress * (cushion_delay - target_delay)
                elif i >= (steps - ramp_steps):
                    steps_from_end = steps - 1 - i
                    progress = steps_from_end / ramp_steps
                    current_delay = cushion_delay - progress * (cushion_delay - target_delay)
                else:
                    current_delay = target_delay
            else:
                current_delay = target_delay

            # Symmetric 50% duty cycle for maximum jitter tolerance in Python
            half_delay = current_delay / 2.0

            # Send step pulse
            self.step_device.on()
            time.sleep(half_delay)
            self.step_device.off()
            time.sleep(half_delay)
            if verbose:
                print(f"Steps count {i+1}", end="\r", flush=True)

    def stop(self, release_torque: Optional[bool] = False) -> None:
        """
        Interrupts a currently running rotate loop.
        """
        self._stop_event.set()
        if release_torque:
            self.disable()

    def close(self) -> None:
        """
        Hard-releases the GPIO pins used by the motor back to the OS.
        """
        if getattr(self, "dir_device", None) is not None:
            self.dir_device.close()
        if getattr(self, "step_device", None) is not None:
            self.step_device.close()
        if getattr(self, "en_device", None) is not None:
            self.en_device.close()

