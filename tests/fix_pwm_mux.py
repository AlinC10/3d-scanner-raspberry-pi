import subprocess
import time
from rpi_hardware_pwm import HardwarePWM
from gpiozero import DigitalOutputDevice

print("1. Forcing GPIO 12 to PWM mode (ALT0) using pinctrl...")
try:
    subprocess.run(["pinctrl", "set", "12", "a0"], check=True)
    print("   -> GPIO 12 successfully mapped to Hardware PWM!")
except Exception as e:
    print(f"   -> pinctrl command failed: {e}")

print("2. Turning on Relay...")
relay = DigitalOutputDevice(11, active_high=True, initial_value=False)
relay.on()
time.sleep(0.5)

print("3. Starting Hardware PWM...")
try:
    pwm = HardwarePWM(pwm_channel=0, hz=10000, chip=0)
    pwm.start(100)
    print("   -> PWM is running at 100%. Are the lights ON?")
    time.sleep(4)
    pwm.stop()
except Exception as e:
    print(f"   -> PWM Error: {e}")
finally:
    relay.off()
    relay.close()
    print("Test finished.")
