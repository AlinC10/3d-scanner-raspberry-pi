# Illumination System (`../lights.py`)

The `DimmableLight` class controls the 12V LED illumination used for photogrammetry. It provides robust brightness control via Hardware PWM and absolute power isolation via a mechanical relay, with built-in thread safety and Zero-Current Switching (ZCS).

---

## 🏗️ Hardware Architecture & Design

The lighting system uses two interconnected hardware components on the Raspberry Pi 5:

1. **Mechanical Relay (GPIO 11):** Provides physical isolation, ensuring the 12V LED strips draw absolutely zero power when off.
2. **D4184 MOSFET Module (GPIO 12):** Receives a 10kHz Hardware PWM signal from the Pi's RP1 chip to smoothly dim the LEDs.

### Zero-Current Switching (ZCS)
To prevent electrical arcing and extend the life of the relay contacts, the software enforces Zero-Current Switching:
* **Turn ON:** The software sets PWM to 0%, closes the relay, waits 50ms for the mechanical contacts to settle, and *then* ramps the PWM to the target brightness.
* **Turn OFF:** The software drops the PWM to 0% *first*, instantly cutting the current, and then opens the relay.

---

## ⚙️ OS & System Configuration

Because this class uses true Hardware PWM from the Raspberry Pi 5's RP1 chip, you must configure the OS before it will work.

### 1. Enable Hardware PWM in Device Tree
Edit your `/boot/firmware/config.txt` and add the PWM overlay:
```ini
dtoverlay=pwm-2chan
```
*Note: A reboot is required after modifying this file.*

### 2. Install Python Dependencies
The software relies on the `rpi-hardware-pwm` library:
```bash
pip install rpi-hardware-pwm
```

### 3. Grant Sysfs Permissions (For Non-Root Users)
The FastAPI server runs as a standard user. By default, Linux restricts access to `/sys/class/pwm`. Run these commands once to permanently grant permission to the `gpio` group:
```bash
echo 'SUBSYSTEM=="pwm*", PROGRAM="/bin/sh -c '\''chown -R root:gpio /sys/class/pwm && chmod -R 770 /sys/class/pwm'\''"' | sudo tee /etc/udev/rules.d/99-pwm.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -a -G gpio $USER
```
*Note: Log out and log back in for the group membership to take effect.*

---

## 🧠 Software Implementation Details

### Intelligent PWM Chip Discovery
The Raspberry Pi 5 has multiple PWM controllers (including one dedicated to the Active Cooler fan). The `_find_pwm_chip()` method automatically scans `/sys/class/pwm` to find the correct RP1 PWM controller:
1. It looks for the specific RP1 base address marker (`1f00098000`).
2. If the marker isn't found, it safely falls back by checking if the chip has exactly 2 channels (`npwm == 2`), avoiding accidentally hijacking the CPU fan.

### Thread Safety and Scan Locking
The class is fully thread-safe, utilizing a `threading.RLock()`.
During an active 360-degree scan, the `Scanner` calls `lock_brightness()`. While locked, any API requests to `toggle()` or `set_brightness()` will raise a `LightLockedError`, preventing external apps from accidentally dimming the lights and ruining the photogrammetry exposure.

---

## 🚀 API Usage

```python
from hardware.lights import DimmableLight, LightLockedError

# Initialize the light controller
lights = DimmableLight(
    relay_pin=11, 
    pwm_channel=0, 
    frequency=10000, 
    initial_brightness=1.0
)

# Basic Control
lights.on()                   # Turns on at 100% (or previous brightness)
lights.set_brightness(0.5)    # Dims to 50%
lights.toggle()               # Turns off
lights.is_on                  # Returns False

# Scan Locking
lights.lock_brightness()
try:
    lights.set_brightness(0.8)
except LightLockedError:
    print("Cannot change brightness during an active scan!")

lights.unlock_brightness()

# Graceful Shutdown
lights.close()
```
