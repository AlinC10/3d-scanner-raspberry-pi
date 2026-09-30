# Time-of-Flight Sensor (`../tof.py`)

The `ToFSensor` class is a robust wrapper around the VL53L0X Time-of-Flight (ToF) laser distance sensor. It is utilized by the 3D scanner rig to provide **intelligent Z-axis termination** (the "Roof Capture" mechanism). 

By measuring the distance to a fixed background/backdrop, the sensor detects when the Z-axis carriage has risen higher than the physical object being scanned, allowing the system to cleanly terminate the scan early and save time.

---

## 🏗️ Hardware Architecture & Design

### The VL53L0X Sensor
The VL53L0X communicates via **I2C** (`SDA` and `SCL`). The driver relies on the `adafruit-circuitpython-vl53l0x` library, abstracting the complex raw I2C photon counting mechanics into simple distance measurements.

### The 25° Field of View (FOV) Cone
The VL53L0X laser spreads out in a **25° conical field of view**. Because it is a cone, the area the sensor "sees" expands the further away the background is. 

You must ensure the physical backdrop is wide enough to cover this entire circular area, and that no cables or poles encroach into this space, or the sensor will read them instead of the backdrop.

| Distance to Background | Laser Cone Width (Diameter) | Real-world Equivalent |
| :--- | :--- | :--- |
| **380 mm** | **168.5 mm** (16.8 cm) | About the width of an open hand |
| **400 mm** | **177.4 mm** (17.7 cm) | |
| **450 mm** | **199.5 mm** (20.0 cm) | About the width of an iPad screen |
| **500 mm** | **221.7 mm** (22.2 cm) | |
| **550 mm** | **243.9 mm** (24.4 cm) | |
| **600 mm** | **266.0 mm** (26.6 cm) | Wider than a standard dinner plate |

---

## 🧠 Software Implementation Details

### 1. Timing Budget Optimization (High Accuracy)
The VL53L0X calculates distance by timing how long photons take to bounce back. The default measurement timing budget is `33ms`. 

Because our 3D scanner pauses the turntable for several hundred milliseconds at every step to allow the object to physically settle before capturing a photo, we are not constrained by speed. The `ToFSensor` initialization increases the timing budget to **200ms** (`200000 us`). This dramatically increases measurement accuracy and reduces floating point jitter/noise.

### 2. Infinity & Error Handling
When the sensor points at an empty room, or a distance beyond its physical maximum range (~1.2 - 2 meters), the raw driver returns `8190` or `8191`. 
The `get_distance_mm()` wrapper intercepts this and returns Python's `float('inf')` (Infinity) to safely represent "Clear / No Object".

If the I2C physical wires are disconnected or bumped during a scan, `__init__` catches the `ValueError`/`OSError` and permanently disables the sensor for that run. In this fallback mode, `get_distance_mm()` will always return `float('inf')`, allowing the scanner to gracefully finish scanning up to the physical endstop limit instead of crashing the Python backend.

### 3. Rapid Double-Read Debounce (`is_object_detected`)
Even with a high timing budget, laser sensors can occasionally bounce off stray dust particles or highly reflective surfaces at odd angles. 

To prevent the scanner from prematurely terminating a level due to a false positive, the `is_object_detected()` method employs a debounce algorithm:
1. It reads the distance. If the distance is smaller than `BACKGROUND - CLEARANCE_THRESHOLD`, it pauses for `10ms`.
2. It takes a second reading.
3. It only returns `True` if **both** readings confirm the object is present.

---

## 🚦 Integration in the Scanner Loop

During `Scanner.scan()`:
- At every rotational stop (e.g. 20 stops per 360° ring), `tof.is_object_detected()` is called.
- If it returns `True`, a boolean flag `object_detected_this_level` is permanently set to `True` for that specific Z-level ring.
- At the end of the 360° rotation, the scanner checks the flag. 
- If `object_detected_this_level` is `False`, the scanner knows it just did a full 360° rotation above the object's roof. It safely terminates the overall scan, skips moving the Z-axis any higher, and proceeds to the cleanup phase.

## Dependencies
Before running the code, we must install the Adafruit CircuitPython dependency inside your `.venv`:
```bash
pip install adafruit-circuitpython-vl53l0x Adafruit-Blinka
```
