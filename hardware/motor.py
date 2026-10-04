import logging
import threading
from typing import Optional

from hardware.arduino_bridge import ArduinoBridge, ArduinoCommand, ArduinoStatus

log = logging.getLogger(__name__)

T8_THREADED_ROD_STEP = 8  # mm

class MotorError(Exception):
    pass

class Motor:
    """
    Stepper motor controller for NEMA 17 stepper motor with TB6600 driver.
    Offloaded to Arduino via ArduinoBridge.
    """
    nema17_step_angle = 1.8  # degrees

    def __init__(
            self,
            motor_id: int = 0,
            step_pin: int = 3,
            dir_pin: int = 4,
            en_pin: Optional[int] = 2,
            step_type: str = "Full",
            port: Optional[str] = None
    ) -> None:
        """
        Initializes the motor by configuring its pins on the connected Arduino.
        
        :param motor_id: The unique ID for this motor (e.g., 0 for turntable, 1 for Z-axis).
        :type motor_id: int
        :param step_pin: Arduino digital pin connected to STEP on the driver. WARNING: Avoid Arduino Pin 13.
        :type step_pin: int
        :param dir_pin: Arduino digital pin connected to DIR on the driver.
        :type dir_pin: int
        :param en_pin: Arduino digital pin connected to EN on the driver to enable/disable it.
        :type en_pin: Optional[int]
        :param step_type: Default microstepping mode string.
        :type step_type: str
        :param port: Optional serial port string to pass to the ArduinoBridge singleton.
        :type port: Optional[str]
        """
        self.motor_id = motor_id
        self.step_pin = step_pin
        self.dir_pin = dir_pin
        self.en_pin = en_pin if en_pin is not None else -1
        self.step_type = step_type
        
        self.bridge = ArduinoBridge.get_instance(port=port)
        
        resp = self.bridge.send_command(f"{ArduinoCommand.CONFIG.value} {self.motor_id} {self.step_pin} {self.dir_pin} {self.en_pin}")
        if resp != str(ArduinoStatus.OK.value):
            raise MotorError(f"Failed to configure motor {self.motor_id} on Arduino: received error status '{resp}'")

    def handle_driver_fault(self) -> None:
        """
        Handles a motor driver fault by stopping the motor and raising an exception.
        """
        self.stop(release_torque=True)
        raise MotorError("Driver Fault Triggered! Stopping motor..")

    def enable(self) -> None:
        """
        Enables the motor driver by instructing the Arduino to set the EN pin LOW.
        """
        resp = self.bridge.send_command(f"{ArduinoCommand.ENABLE.value} {self.motor_id}")
        if resp != str(ArduinoStatus.OK.value):
            log.warning(f"Failed to enable motor {self.motor_id}: {resp}")

    def disable(self) -> None:
        """
        Disables the motor driver by instructing the Arduino to set the EN pin HIGH.
        """
        resp = self.bridge.send_command(f"{ArduinoCommand.DISABLE.value} {self.motor_id}")
        if resp != str(ArduinoStatus.OK.value):
            log.warning(f"Failed to disable motor {self.motor_id}: {resp}")

    def angle_to_steps_conversion(self, angle: int | float = 18, step_type: Optional[str] = None) -> int:
        """
        Converts a rotation angle into the corresponding number of motor steps.
        
        :param angle: The angle to rotate in degrees.
        :type angle: int | float
        :param step_type: The microstepping mode to use for conversion.
        :type step_type: Optional[str]
        :return: The exact number of integer steps required.
        :rtype: int
        """
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

    def distance_to_step_conversion(self, distance: float = 100.0, step_type: Optional[str] = None) -> int:
        """
        Converts a linear distance on the T8 threaded rod to the required number of steps.
        
        :param distance: The distance to travel in mm.
        :type distance: float
        :param step_type: The microstepping mode to use for conversion.
        :type step_type: Optional[str]
        :return: The exact number of integer steps required.
        :rtype: int
        """
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
        
        :param clockwise: Direction of rotation, True for clockwise.
        :type clockwise: bool
        :param angle: The angle to rotate in degrees.
        :type angle: int | float
        :param delay: The cruise step delay in seconds between pulses.
        :type delay: float | int
        :param step_type: The microstepping mode.
        :type step_type: Optional[str]
        :param verbose: Legacy argument (ignored in Arduino bridge).
        :type verbose: bool
        :param initial_delay: Legacy argument (ignored in Arduino bridge).
        :type initial_delay: float | int
        :param acceleration: Legacy argument (ignored).
        :type acceleration: bool
        :param start_delay: Legacy argument (ignored).
        :type start_delay: Optional[float]
        :param ramp_percent: Legacy argument (ignored).
        :type ramp_percent: float
        :param cancel_event: Legacy cooperative threading event.
        :type cancel_event: Optional[threading.Event]
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
        """
        Rotates the motor to move the carriage by a specific linear distance.
        
        :param clockwise: Direction of rotation.
        :type clockwise: bool
        :param distance: The distance to travel in mm.
        :type distance: int | float
        :param delay: The cruise step delay in seconds between pulses.
        :type delay: float | int
        :param step_type: The microstepping mode.
        :type step_type: Optional[str]
        :param verbose: Legacy argument (ignored in Arduino bridge).
        :type verbose: bool
        :param initial_delay: Legacy argument (ignored in Arduino bridge).
        :type initial_delay: float | int
        :param acceleration: Legacy argument (ignored).
        :type acceleration: bool
        :param start_delay: Legacy argument (ignored).
        :type start_delay: Optional[float]
        :param ramp_percent: Legacy argument (ignored).
        :type ramp_percent: float
        :param cancel_event: Legacy cooperative threading event.
        :type cancel_event: Optional[threading.Event]
        """
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
            cancel_event: Optional[threading.Event] = None) -> bool:
        """
        Rotates the motor by instructing the Arduino to generate microsecond-precise stepping pulses.
        
        :param clockwise: Direction of rotation, True for clockwise, False for counter-clockwise.
        :type clockwise: bool
        :param steps: Number of steps to rotate.
        :type steps: int
        :param delay: Cruise step delay in seconds between pulses.
        :type delay: float | int
        :param step_type: Microstepping mode (retained for signature compatibility).
        :type step_type: Optional[str]
        :param verbose: Legacy argument (ignored in Arduino bridge).
        :type verbose: bool
        :param initial_delay: Legacy argument (ignored in Arduino bridge).
        :type initial_delay: float | int
        :param acceleration: Legacy argument (ignored, trapezoidal accel is scheduled for a future update).
        :type acceleration: bool
        :param start_delay: Legacy argument (ignored).
        :type start_delay: Optional[float]
        :param ramp_percent: Legacy argument (ignored).
        :type ramp_percent: float
        :param cancel_event: Legacy cooperative threading event (use `Motor.stop()` instead).
        :type cancel_event: Optional[threading.Event]
        :return: True if the rotation completed successfully, False if it was aborted by an emergency stop.
        :rtype: bool
        """
        if steps <= 0:
            return True

        # Ensure delay in seconds is converted precisely to microseconds for the Arduino
        delay_us = int(float(delay) * 1_000_000)

        # Calculate a dynamic safety timeout 
        # Duration formula: steps * physical delay in seconds, + 50% buffer, + 3s fixed overhead
        expected_timeout = (steps * float(delay)) * 1.5 + 3.0

        dir_val = 1 if clockwise else 0
        
        cmd = f"{ArduinoCommand.ROTATE.value} {self.motor_id} {steps} {dir_val} {delay_us}"
        
        # This call will block until the Arduino completes all stepping or is aborted
        resp = self.bridge.send_command(cmd, timeout=expected_timeout)

        if resp == str(ArduinoStatus.OK.value):
            return True
        elif resp == str(ArduinoStatus.ERR_ABORTED.value):
            log.warning(f"Motor {self.motor_id} rotation was aborted mid-movement.")
            return False
        else:
            raise MotorError(f"Motor {self.motor_id} rotation failed with error status '{resp}'")

    def stop(self, release_torque: bool = False) -> None:
        """
        Interrupts a currently running rotate sequence by sending an out-of-band universal stop.
        
        :param release_torque: Whether to also release holding torque (default: False). Note: the out-of-band STOP command automatically releases torque on the Arduino side.
        :type release_torque: bool
        """
        self.bridge.send_stop()
        if release_torque:
            self.disable()

    def close(self) -> None:
        """
        Safely disables the motor and cleans up.
        """
        self.disable()
