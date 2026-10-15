import logging
import threading
import time
from typing import Optional

from hardware.arduino_bridge import ArduinoBridge, ArduinoCommand, ArduinoStatus

log = logging.getLogger(__name__)

T8_THREADED_ROD_STEP = 8  # mm

# Microstepping configured on the TB6600 drivers (DIP switches); the request schemas validate against these.
TURNTABLE_STEP_TYPE = "1/16"
Z_STEP_TYPE = "1/4"

# Slow-stop tail of the deceleration ramp (seconds between the last step pulses).
END_DELAY_MIN = 0.005                # 5 ms  -> 200 microsteps/s
END_DELAY_MAX = 0.030                # 30 ms -> 33 microsteps/s
TURNTABLE_END_DELAY_DEFAULT = 0.015  # 15 ms -> 67 microsteps/s


class MotorError(Exception):
    pass


_warned: set = set()


def _warn_once(key: str, message: str, *args) -> None:
    """Logs a warning the first time `key` is seen (a per-step condition must not flood the log)."""
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def resolve_end_delay(end_delay: Optional[float], delay: float | int) -> Optional[float]:
    """
    Clamps a requested `end_delay` into [END_DELAY_MIN, END_DELAY_MAX] and never lets it be
    faster than the cruise delay. `None` (feature off) is returned unchanged.
    """
    if end_delay is None:
        return None

    clamped = min(max(float(end_delay), END_DELAY_MIN), END_DELAY_MAX)
    if clamped != float(end_delay):
        log.warning("end_delay %.4f s is outside [%.4f, %.4f]; clamped to %.4f s",
                    end_delay, END_DELAY_MIN, END_DELAY_MAX, clamped)
    return max(clamped, float(delay))


def _ramp_phase_seconds(steps: int, delay_a_us: int, delay_b_us: int) -> float:
    """
    Duration of `steps` steps under constant acceleration between step delays `delay_a_us` and
    `delay_b_us`: the average speed is the mean of the two end speeds.
    """
    if steps <= 0:
        return 0.0
    v_a = 1_000_000.0 / delay_a_us
    v_b = 1_000_000.0 / delay_b_us
    return 2.0 * steps / (v_a + v_b)

def angle_to_steps(angle: int | float, step_type: str, step_angle: float = 1.8) -> int:
    """
    Converts a rotation angle (degrees) into a whole number of steps for the given microstepping mode.

    :raises MotorError: when the angle is not an exact multiple of one (micro)step.
    """
    if "/" in step_type:
        num, den = step_type.split("/")
        num, den = int(num), int(den)
    elif step_type == "Half":
        num, den = 1, 2
    else:
        num, den = 1, 1

    steps_float = angle / (step_angle * num / den)

    # Allow a tiny margin for float math precision
    if abs(steps_float - round(steps_float)) > 1e-4:
        raise MotorError(
            f"Cannot rotate exactly {angle}° using '{step_type}' step type. "
            f"This requires {steps_float:.2f} steps, which is not a whole integer."
        )
    return round(steps_float)


def distance_to_steps(distance: float, step_type: str) -> int:
    """Converts a linear distance (mm) on the T8 threaded rod into whole steps (see `angle_to_steps`)."""
    return angle_to_steps((distance / T8_THREADED_ROD_STEP) * 360.0, step_type)


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
        
        self._is_enabled = False
        self._enabled_epoch = -1  # bridge connection epoch in which the driver was last known to be energized
        self.bridge = ArduinoBridge.get_instance(port=port)
        
        resp = self.bridge.send_command(f"{ArduinoCommand.CONFIG.value} {self.motor_id} {self.step_pin} {self.dir_pin} {self.en_pin}")
        if resp != str(ArduinoStatus.OK.value):
            raise MotorError(f"Failed to configure motor {self.motor_id} on Arduino: received error status '{resp}'")

    def _bridge_epoch(self) -> int:
        """Counts serial (re)connections: every reconnect resets the Arduino, which drops all drivers to disabled."""
        return getattr(self.bridge, "connection_epoch", 0)

    @property
    def is_enabled(self) -> bool:
        """Returns True if the motor is currently energized / holding torque."""
        if not self._is_enabled:
            return False
        return self._enabled_epoch == self._bridge_epoch()

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
        else:
            self._is_enabled = True
            self._enabled_epoch = self._bridge_epoch()

    def disable(self, settle_delay: float = 0.5, interrupt: Optional[threading.Event] = None) -> None:
        """
        Disables the motor driver after an optional holding torque settling window.

        :param settle_delay: Time in seconds to hold rigid magnetic torque before cutting power.
                             Defaults to 0.5s for normal mechanical stabilization.
                             Set to 0.0 for immediate shutoff (e.g. emergency stops).
        :type settle_delay: float
        :param interrupt: Optional event that cuts the settling window short when set (e.g. a scan
                          cancellation), so a cancel is not delayed by the pause.
        :type interrupt: Optional[threading.Event]
        """
        if settle_delay > 0 and self.is_enabled:
            if interrupt is not None:
                interrupt.wait(settle_delay)
            else:
                time.sleep(settle_delay)

        resp = self.bridge.send_command(f"{ArduinoCommand.DISABLE.value} {self.motor_id}")
        if resp != str(ArduinoStatus.OK.value):
            log.warning(f"Failed to disable motor {self.motor_id}: {resp}")
        else:
            self._is_enabled = False

    def angle_to_steps_conversion(self, angle: int | float = 18, step_type: Optional[str] = None) -> int:
        """
        Converts a rotation angle into the corresponding number of motor steps.
        
        :param angle: The angle to rotate in degrees.
        :type angle: int | float
        :param step_type: The microstepping mode to use for conversion.
        :type step_type: Optional[str]
        :return: The exact number of integer steps required.
        :rtype: int
        :raises MotorError: when the angle is not a whole number of steps.
        """
        if step_type is None:
            step_type = self.step_type
        return angle_to_steps(angle, step_type, self.nema17_step_angle)

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
            decel_percent: Optional[float] = None,
            cancel_event: Optional[threading.Event] = None,
            end_delay: Optional[float] = None) -> bool:
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
        :param acceleration: Whether to use a speed ramp (see `rotate`).
        :type acceleration: bool
        :param start_delay: Starting step delay of the ramp (see `rotate`).
        :type start_delay: Optional[float]
        :param ramp_percent: Fraction of the steps used for acceleration (see `rotate`).
        :type ramp_percent: float
        :param decel_percent: Fraction of the steps used for deceleration (see `rotate`).
        :type decel_percent: Optional[float]
        :param cancel_event: Legacy cooperative threading event.
        :type cancel_event: Optional[threading.Event]
        :param end_delay: Step delay (s) at the end of the deceleration ramp for a slow stop; clamped to
                          [END_DELAY_MIN, END_DELAY_MAX]. None disables it (see `rotate`).
        :type end_delay: Optional[float]
        """
        if step_type is None:
            step_type = self.step_type

        steps = self.angle_to_steps_conversion(angle, step_type)
        if steps > 0:
            return self.rotate(
                clockwise=clockwise,
                steps=steps,
                delay=delay,
                step_type=step_type,
                verbose=verbose,
                initial_delay=initial_delay,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                decel_percent=decel_percent,
                cancel_event=cancel_event,
                end_delay=end_delay
            )
        return True

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
            decel_percent: Optional[float] = None,
            cancel_event: Optional[threading.Event] = None,
            end_delay: Optional[float] = None) -> bool:
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
        :param acceleration: Whether to use a speed ramp (see `rotate`).
        :type acceleration: bool
        :param start_delay: Starting step delay of the ramp (see `rotate`).
        :type start_delay: Optional[float]
        :param ramp_percent: Fraction of the steps used for acceleration (see `rotate`).
        :type ramp_percent: float
        :param decel_percent: Fraction of the steps used for deceleration (see `rotate`).
        :type decel_percent: Optional[float]
        :param cancel_event: Legacy cooperative threading event.
        :type cancel_event: Optional[threading.Event]
        :param end_delay: Step delay (s) at the end of the deceleration ramp for a slow stop; clamped to
                          [END_DELAY_MIN, END_DELAY_MAX]. None disables it (see `rotate`).
        :type end_delay: Optional[float]
        """
        if step_type is None:
            step_type = self.step_type

        steps = self.distance_to_step_conversion(distance, step_type)
        if steps > 0:
            return self.rotate(
                clockwise=clockwise,
                steps=steps,
                delay=delay,
                step_type=step_type,
                verbose=verbose,
                initial_delay=initial_delay,
                acceleration=acceleration,
                start_delay=start_delay,
                ramp_percent=ramp_percent,
                decel_percent=decel_percent,
                cancel_event=cancel_event,
                end_delay=end_delay
            )
        return True

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
            decel_percent: Optional[float] = None,
            cancel_event: Optional[threading.Event] = None,
            end_delay: Optional[float] = None) -> bool:
        """
        Rotates the motor with a constant-acceleration speed ramp via the Arduino bridge.

        The Arduino ramps the speed linearly in time: from 1/start_delay up to 1/delay over the acceleration
        steps, cruises, then slows from 1/delay down to 1/end_delay (1/start_delay if `end_delay` is None).

        :param clockwise: Direction of rotation, True for clockwise, False for counter-clockwise.
        :param steps: Number of steps to rotate.
        :param delay: Cruise step delay in seconds between pulses.
        :param step_type: Microstepping mode (retained for signature compatibility).
        :param verbose: Legacy argument (ignored in Arduino bridge).
        :param initial_delay: Legacy argument (ignored in Arduino bridge).
        :param acceleration: Whether to use acceleration (if False, ramp_percent is treated as 0.0).
        :param start_delay: Starting delay (in seconds) for the ramp. Defaults to delay * 3 (suitable for light loads). For heavy loads (>5kg), manual override to 5x-8x is recommended.
        :param ramp_percent: Percentage of total steps to use for acceleration (0.0 to 1.0). If decel_percent is None, this is also used for deceleration.
        :param decel_percent: Optional explicit percentage of total steps for deceleration.
        :param cancel_event: Legacy cooperative threading event (use `Motor.stop()` instead).
        :param end_delay: Optional step delay (seconds) reached at the last step of the deceleration, so the move
                          ends slowly. Clamped to [END_DELAY_MIN, END_DELAY_MAX] and never faster than `delay`.
                          None (default) keeps the old behaviour (decelerate down to `start_delay`). Ignored (with a
                          warning) when there is no deceleration phase.
        :return: True if the rotation completed successfully, False if it was aborted by an emergency stop.
        """
        if steps <= 0:
            return True
        if delay <= 0:
            raise ValueError("Delay must be > 0")

        if not acceleration:
            ramp_percent = 0.0
            decel_percent = 0.0
        else:
            ramp_percent = ramp_percent or 0.0

        # Validate percentages to prevent negative step bounds leading to uint32_t overflow
        for p in (ramp_percent, decel_percent):
            if p is not None and not (0.0 <= p <= 1.0):
                raise ValueError(f"Ramp percentages must be between 0.0 and 1.0. Got {p}")

        # Calculation logic
        target_us = int(round(float(delay) * 1_000_000))
        start_us = int(round((float(start_delay) if start_delay else float(delay) * 3) * 1_000_000))
        
        # Prevent underflow
        if start_us < target_us:
            start_us = target_us

        accel_steps = int(steps * ramp_percent)
        decel_steps = int(steps * decel_percent) if decel_percent is not None else accel_steps
        
        # Cap to ensure accel + decel don't exceed total steps
        if accel_steps + decel_steps > steps:
            total_ramp = accel_steps + decel_steps
            accel_steps = int(steps * (accel_steps / total_ramp))
            decel_steps = steps - accel_steps

        # Slow-stop tail: only meaningful when there is a deceleration phase
        end_us: Optional[int] = None
        if end_delay is not None:
            if decel_steps == 0:
                _warn_once("end_delay_no_decel",
                           "end_delay ignored: the move has no deceleration phase (acceleration=%s).", acceleration)
            else:
                end_us = int(round(resolve_end_delay(end_delay, delay) * 1_000_000))

        # Timeout from the exact duration of each constant-acceleration phase (mean of the end speeds)
        cruise_steps = steps - (accel_steps + decel_steps)
        decel_end_us = end_us if end_us is not None else start_us
        duration_s = (
            _ramp_phase_seconds(accel_steps, start_us, target_us)
            + cruise_steps * float(delay)
            + _ramp_phase_seconds(decel_steps, target_us, decel_end_us)
        )
        expected_timeout = duration_s * 1.5 + 3.0

        dir_val = 1 if clockwise else 0

        if accel_steps == 0 and decel_steps == 0:
            cmd = f"{ArduinoCommand.ROTATE.value} {self.motor_id} {steps} {dir_val} {target_us}"
        else:
            cmd = f"{ArduinoCommand.ACCEL_ROTATE.value} {self.motor_id} {steps} {dir_val} {target_us} {start_us} {accel_steps} {decel_steps}"
            if end_us is not None:
                cmd += f" {end_us}"
        
        # This call will block until the Arduino completes all stepping or is aborted
        resp = self.bridge.send_command(cmd, timeout=expected_timeout)

        if resp == str(ArduinoStatus.OK.value):
            # The firmware energizes the driver by itself when it runs a move
            self._is_enabled = True
            self._enabled_epoch = self._bridge_epoch()
            return True
        elif resp == str(ArduinoStatus.ERR_ABORTED.value):
            # An abort (emergency stop, serial loss + Arduino reset) leaves the driver disabled
            self._is_enabled = False
            log.warning(f"Motor {self.motor_id} rotation was aborted mid-movement.")
            return False
        else:
            raise MotorError(f"Motor {self.motor_id} rotation failed with error status '{resp}'")

    def stop(self, release_torque: bool = False) -> None:
        """
        Interrupts a currently running rotate sequence by sending an out-of-band universal stop.
        
        :param release_torque: Whether to also release holding torque (default: False).
        :type release_torque: bool
        """
        self.bridge.send_stop()
        self._is_enabled = False
        if release_torque:
            self.disable(settle_delay=0.0)

    def close(self) -> None:
        """
        Safely disables the motor and cleans up.
        """
        self.disable(settle_delay=0.0)
