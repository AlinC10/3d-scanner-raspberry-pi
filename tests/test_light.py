from gpiozero import DigitalOutputDevice
import time

print("========================================")
print("💡 Light Hardware Diagnostic Tool")
print("Bypassing Hardware PWM to test pure wiring/power.")
print("========================================\n")

# Initialize Relay on GPIO 11
relay = DigitalOutputDevice(11, active_high=True, initial_value=False)

# Initialize MOSFET Gate on GPIO 12 (as a simple digital pin, 100% brightness)
mosfet = DigitalOutputDevice(12, active_high=True, initial_value=False)

try:
    print("1. Turning ON Relay (You should hear a click)")
    relay.on()
    time.sleep(1)

    print("2. Turning ON MOSFET (Lights should be at 100% brightness now!)")
    mosfet.on()
    time.sleep(3)

    print("3. Turning OFF MOSFET")
    mosfet.off()
    time.sleep(1)

    print("4. Turning OFF Relay")
    relay.off()
    print("\nTest complete!")

except Exception as e:
    print(f"Error: {e}")
finally:
    relay.close()
    mosfet.close()
