from gpiozero import Button
import time

# Initialize identically to hardware/scanner.py
up_endstop = Button(22, pull_up=True, bounce_time=0.02)
down_endstop = Button(4, pull_up=True, bounce_time=0.02)

print("========================================")
print("🔍 Endstop Hardware Diagnostic Tool")
print(f"Current State -> TOP: {up_endstop.is_active}, BOTTOM: {down_endstop.is_active}")
print("Press the physical switches now. (CTRL+C to exit)")
print("========================================\n")

up_endstop.when_pressed = lambda: print("🟢 TOP (GPIO 22) PRESSED!")
up_endstop.when_released = lambda: print("🔴 TOP (GPIO 22) RELEASED!")

down_endstop.when_pressed = lambda: print("🟢 BOTTOM (GPIO 4) PRESSED!")
down_endstop.when_released = lambda: print("🔴 BOTTOM (GPIO 4) RELEASED!")

try:
    while True:
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\nExiting...")
