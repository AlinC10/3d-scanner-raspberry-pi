import time
import sys

from gpiozero import Buzzer
from gpiozero import OutputDevice
from hardware.motor import Motor
from hardware.dual_camera import DualCamera


def test_buzzer():
    print("\n[1/5] === Testing Buzzer ===")
    try:
        buzzer = Buzzer(pin=22)
        print("Emitting single 0.5s beep...")
        buzzer.beep(0.5)
        time.sleep(0.5)

        print("Emitting rapid sequence (3 beeps)...")
        buzzer.beep(on_time=0.1, off_time=0.1, n=3)

        buzzer.close()
        print("-> Buzzer test passed.")
    except Exception as e:
        print(f"-> Buzzer test failed: {e}")


def test_relay():
    print("\n[2/5] === Testing Relay (Lighting/Trigger) ===")
    try:
        # NOTE: If your relay turns ON when LOW, change active_high to False
        relay = OutputDevice(pin=17, active_high=True, initial_value=False)

        print("Turning relay ON...")
        relay.on()
        time.sleep(1)

        print("Turning relay OFF...")
        relay.off()
        time.sleep(1)

        print("Toggling relay back ON then OFF...")
        relay.toggle()
        time.sleep(1)
        relay.close()
        print("-> Relay test passed.")
    except Exception as e:
        print(f"-> Relay test failed: {e}")


def test_motor():
    print("\n[3/5] === Testing DRV8825 Motor ===")
    try:
        scanner_motor = Motor(dir_pin=20, step_pin=21, mode_pins=(14, 15, 18))
        print("Rotating one full turn (1/32 microstepping) clockwise...")
        # 200 step motor * 32 microsteps = 6400 steps
        scanner_motor.rotate(clockwise=True, steps=6400, delay=0.0005, step_type="1/32")
        print("-> Motor test passed.")
    except Exception as e:
        print(f"-> Motor test failed: {e}")


def test_sync_cameras():
    print("\n[4/5] === Testing Hardware Sync Cameras ===")
    try:
        print("Initializing XVS sync threads properly...")
        hw_cam = DualCamera(camera_id0=0, camera_id1=1)

        print("Running autofocus on both lenses...")
        hw_cam.auto_focus()

        print("Taking synced test photos...")
        hw_cam.take_photos("test_left.jpg", "test_right.jpg")

        hw_cam.close()
        print("-> Camera test passed.")
    except Exception as e:
        print(f"-> Camera test failed: {e}")


def test_system_integration():
    """
    Simulates a full working step of the 3D scanner using all combined rules.
    """
    print("\n[5/5] === Full Scanner System Integration Test ===")
    print("Simulating a 3D Scan sequence...")
    try:
        buzzer = Buzzer(pin=22)
        lights = OutputDevice(pin=17, active_high=True, initial_value=False)
        motor = Motor(dir_pin=20, step_pin=21, mode_pins=(14, 15, 18))
        hw_cam = DualCamera(camera_id0=0, camera_id1=1)

        print("\n--- Scan Step Sequence ---")
        # 1. Turn on lights
        print("1. Turning on ring lights...")
        lights.on()
        time.sleep(0.5)  # Let lights stabilize

        # 2. Focus cameras
        print("2. Focusing cameras...")
        hw_cam.auto_focus()

        # 3. Give warning beep
        print("3. Warning beep (Scan starting)...")
        buzzer.beep(0.3)

        # 4. Snap synchronized photos
        print("4. Snapping stereoscopic images...")
        hw_cam.take_photos("scan_step1_left.jpg", "scan_step1_right.jpg")

        # 5. Rotate turntable to next angle (e.g. 10 degrees = ~177 steps in 1/32 mode)
        print("5. Rotating turntable to next angle...")
        motor.rotate(clockwise=True, steps=178, delay=0.0005, step_type="1/32")

        # 6. Shut down step
        print("6. Shutting off lights and signaling completion...")
        lights.turn_off()
        buzzer.beep(on_time=0.1, off_time=0.1, n=2)

        # Cleanup
        hw_cam.close()
        lights.close()
        buzzer.close()
        print("-> System Integration test passed! All rules respected.")

    except Exception as e:
        print(f"-> System Integration test failed: {e}")


def main():
    print("==================================================")
    print("      3D Scanner Hardware Validation Suite        ")
    print("==================================================")

    # 1. Component wise tests
    test_buzzer()
    test_relay()
    test_motor()
    test_sync_cameras()

    # 2. Full simulation test
    print("\nPress ENTER to begin the Full System Integration Test, or Ctrl+C to exit.")
    try:
        input()
        test_system_integration()
    except KeyboardInterrupt:
        print("\nSkipping system integration. Shutting down gracefully.")
        sys.exit(0)


if __name__ == "__main__":
    main()