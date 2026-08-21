from RpiMotorLib import RpiMotorLib

class Motor:
    def __init__(self, dir_pin=20, step_pin=21, mode_pins=(14, 15, 18)):
        """
        Initializes the NEMA 17 stepper motor with the DRV8825 driver.
        mode_pins: (M0, M1, M2) on DRV8825 used for Microstepping.
        If wired directly to 3.3V/5V or GND instead of Pi GPIOs, set mode_pins=(-1, -1, -1).
        """

        self.motor = RpiMotorLib.A4988Nema(dir_pin, step_pin, mode_pins, "DRV8825")
        
    def rotate(self, clockwise=True, steps=200, delay=0.002, step_type="Full"):
        """
        Rotates the motor.
        step_type can be: "Full", "Half", "1/4", "1/8", "1/16", "1/32".
        """
        # motor_go parameters: clockwise (bool), step_type (str), steps (int), step_delay (float), verbose (bool), initial_delay (float)
        self.motor.motor_go(clockwise, step_type, steps, delay, False, 0.05)

    def stop(self):
        """Interrupts a currently running motor_go loop."""
        self.motor.motor_stop()

# if __name__ == "__main__":
#     print("Testing DRV8825 motor driver...")
#     scanner_motor = ScannerMotor(dir_pin=20, step_pin=21, mode_pins=(14, 15, 18))
#
#     # For a standard NEMA 17 motor with 200 steps/rev, in 1/32 microstepping mode:
#     # One full rotation = 200 * 32 = 6400 steps
#     print("Rotating one full turn (1/32 microstepping)...")
#     scanner_motor.rotate(clockwise=True, steps=6400, delay=0.0005, step_type="1/32")
#     print("Test complete.")
