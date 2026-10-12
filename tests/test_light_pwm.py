from rpi_hardware_pwm import HardwarePWM
from gpiozero import DigitalOutputDevice
import time

print("Testing Hardware PWM on Channel 0...")

try:
    # 1. Turn on Relay
    relay = DigitalOutputDevice(11, active_high=True, initial_value=False)
    relay.on()
    time.sleep(0.5)

    # 2. Try PWM chip 0, channel 0
    pwm = HardwarePWM(pwm_channel=0, hz=10000, chip=0)
    pwm.start(100) # 100% duty cycle
    
    print("PWM Started at 100%. Are the lights on? (Waiting 3 seconds)")
    time.sleep(3)
    
    pwm.stop()
    relay.off()
    print("Test finished.")
except Exception as e:
    print(f"Error: {e}")
