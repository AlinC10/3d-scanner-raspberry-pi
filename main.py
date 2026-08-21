from hardware import Motor

# if __name__ == "__main__":
#     print("Testing DRV8825 motor driver...")
#     scanner_motor = Motor(dir_pin=20, step_pin=21, mode_pins=(14, 15, 18))
#
#     # For a standard NEMA 17 motor with 200 steps/rev, in 1/32 microstepping mode:
#     # One full rotation = 200 * 32 = 6400 steps
#     print("Rotating one full turn (1/32 microstepping)...")
#     scanner_motor.rotate(clockwise=True, steps=6400, delay=0.0005, step_type="1/32")
#     print("Test complete.")

# Example Usage
from hardware import DualCamera
if __name__ == "__main__":
    dual_cam = DualCamera()

    try:
        # Example 3D Scanning Prep:
        print("--- Prepping for scan ---")
        dual_cam.auto_focus()
        dual_cam.lock_exposure()

        # Take simultaneous pictures
        print("--- Taking dual pictures ---")
        dual_cam.take_photos("left_view.jpg", "right_view.jpg")

    finally:
        dual_cam.close()