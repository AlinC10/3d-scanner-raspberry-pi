# Time-of-Flight Sensor (`../tof.py`)

The `ToFSensor` class is a robust wrapper around the VL53L1X Time-of-Flight (ToF) laser distance sensor. It is utilized by the 3D scanner rig to provide **intelligent Z-axis termination** (the "Roof Capture" mechanism). 

By measuring the distance to a fixed background/backdrop, the sensor detects when the Z-axis carriage has risen higher than the physical object being scanned, allowing the system to cleanly terminate the scan early and save time.

> The rig previously used a VL53L0X. Its fixed 25° cone mixed the object, turntable and backdrop into one reading and gave 4-8 cm errors. The VL53L1X has a programmable SPAD array, which lets us narrow the cone in software.

---

## 🏗️ Hardware Architecture & Design

### The VL53L1X Sensor
The VL53L1X communicates via **I2C** (`SDA` and `SCL`, address `0x29`). The driver relies on the `adafruit-circuitpython-vl53l1x` library. XSHUT and GPIO1 are left unconnected.

### Configuration

| Setting | Value | Why |
| :--- | :--- | :--- |
| `distance_mode` | `1` (short) | Best ambient-light immunity, ranges up to ~136 cm. The rig measures 25-65 cm. |
| `roi_xy` | `(4, 4)` | Smallest receiving window, narrowest cone. |
| `roi_center` | `199` | Optical centre of the 16x16 SPAD array. |
| `timing_budget` | `200` ms | Lower jitter. Costs nothing, the scan loop already reserves 0.2 s per stop. |

All four are constructor parameters of `ToFSensor`. They are applied in a fixed order: `distance_mode` → `timing_budget` → `roi_xy` → `roi_center` → `start_ranging()`.
- The distance mode selects the table of valid timing budgets, so it must come first.
- **Setting `roi_xy` resets the ROI centre** to a corner SPAD whenever both sides are ≤ 10, so `roi_center` must be set *after* it. Otherwise the 4x4 window sits in a corner of the array and the sensor looks off-axis. The values read back from the sensor are logged at start-up.

### The Field of View (FOV) Cone
The receiving cone is set by the ROI size. The nominal figure for 4x4 is about **15°** (about 27° at 16x16 and 20° at 8x8). These angles come from ST's documentation and are **to be confirmed on the rig** (see "Calibration").

The area the sensor "sees" expands with distance. Make sure the backdrop covers the whole circle and that no cables or brackets encroach into it, or the sensor will read them instead of the backdrop.

| Distance to Background | Cone Width at 4x4 (~15°) | Old VL53L0X (25°) |
| :--- | :--- | :--- |
| **250 mm** | **66 mm** | 111 mm |
| **400 mm** | **105 mm** | 177 mm |
| **535 mm** | **141 mm** | 237 mm |
| **650 mm** | **171 mm** | 288 mm |

A 4x4 window has only 16 of 256 SPADs, so about 94% less light than full frame. If a dark backdrop returns too many invalid readings, widen the ROI in the order `4x4 → 6x6 → 8x8`, and re-check the cone width after each change. A lighter matte backdrop is cheaper than widening the ROI.

---

## 🧠 Software Implementation Details

### 1. Timing Budget
The sensor accepts only `15` (short mode only), `20`, `33`, `50`, `100`, `200` or `500` **milliseconds**. A longer budget improves repeatability (noise falls roughly with the square root of the time), but not absolute accuracy, which depends on the offset and the cone. Because the scan loop pauses at every stop for the object to settle, we are not constrained by speed, so the default is 200 ms. If measurements on the rig show no difference against 100 ms, 100 ms is enough.

### 2. Fresh Measurement per Read (`get_distance_mm`)
The sensor ranges continuously, so a sample may already be waiting from before the last turntable move. Every read therefore:
1. Discards the waiting sample with `clear_interrupt()`.
2. Polls `data_ready` every 5 ms, with a timeout of `2 × timing_budget + 100 ms`.
3. Reads the distance (centimetres from the driver, converted to millimetres) and clears the interrupt.

A read blocks for up to one timing budget (about 200 ms). The whole sequence is guarded by a `threading.Lock`, because the scan loop and the `/test/tof` endpoint (which FastAPI runs in a thread pool) can call it at the same time, and one caller's `clear_interrupt()` would otherwise consume the sample the other is waiting for.

### 3. Infinity & Error Handling
The wrapper returns Python's `float('inf')` (meaning "clear / no object") when:
- the driver reports `None`, which happens when the range status is not valid (no target, signal too weak, out of range);
- the wait for a measurement times out;
- an I2C error occurs.

If the I2C wires are disconnected or the model ID is wrong at start-up (`Wrong sensor ID or type!`), `__init__` catches the exception and permanently disables the sensor for that run. In this fallback mode `get_distance_mm()` always returns `float('inf')`.

### 4. Double-Read Confirmation (`is_object_detected`)
Laser sensors can occasionally bounce off dust or highly reflective surfaces at odd angles. To prevent a false positive, `is_object_detected()` requires **two consecutive fresh readings**, both below the threshold. Each read waits for its own measurement, so the second one is always taken entirely after the first and after the turntable has stopped. If the first reading is not below the threshold, the second is skipped.

The threshold used by the scanner is `TOF_BACKGROUND_DISTANCE_MM - TOF_CLEARANCE_MARGIN_MM` (both constants in `scanner.py`), so ToF noise on the empty backdrop is not mistaken for an object.

---

## 🚦 Integration in the Scanner Loop

During `Scanner.scan()`:
- At every rotational stop (e.g. 20 stops per 360° ring), after the turntable has moved, `tof.is_object_detected()` is called until the object has been seen on that level. The call also counts towards the settling pause, and the scanner sleeps whatever remains of 0.2 s.
- If it returns `True`, a boolean flag `object_detected_this_level` is permanently set to `True` for that specific Z-level ring.
- At the end of the 360° rotation, the scanner checks the flag. 
- If `object_detected_this_level` is `False`, the scanner knows it just did a full 360° rotation above the object's roof. It safely terminates the overall scan, skips moving the Z-axis any higher, and proceeds to the cleanup phase.

---

## 📐 Calibration

Calibration is done on the rig, with the empty turntable and the lights on at scan brightness after they have thermally stabilised. Use `GET /test/tof` for the readings.

| Constant (`scanner.py`) | How to set it |
| :--- | :--- |
| `TOF_BACKGROUND_DISTANCE_MM` | Mean reading of the empty backdrop at several Z heights. |
| `TOF_CLEARANCE_MARGIN_MM` | Several times the measured spread, expected 20-30 mm. |

Also check:
- **Cone:** slide a card in from the side at the object distance and note where the reading starts to change. The change should be symmetric left/right and up/down. This gives the real cone width and confirms the ROI is centred.
- **Backdrop signal:** ~50 readings of the empty backdrop; if more than a few percent are invalid, see the ROI note above.
- **LEDs:** ~50 readings with the lights on and ~50 with them off. A difference of more than a few mm in mean or spread means the strips emit IR in the 940 nm band.
- **Materials:** a matte black, a glossy/metallic and a matte white object, ~20 readings each at mid-height and above the object.

*Measured values and the LED/material results are not recorded here yet; fill this section in after calibrating on the rig.*

---

## ⚠️ Known Limitations

- **Systematic misses.** The 20 stops on a ring are not independent chances. They help when the miss depends on the angle (asymmetric object, partial coverage), but a miss caused by the material (very dark, transparent, glossy) is the same on every stop and ends the scan after level 1. The log then shows `ToF Sensor cleared the object at level 1`.
- **`None` and "empty" are the same reading.** An object that absorbs all light cannot be told apart from a clear backdrop. If the material check shows this is a real problem, a possible change is to treat an invalid reading as "object present" when the backdrop itself reliably returns valid readings.
- **Objects narrower than the cone.** At 4x4 the cone is about 10-14 cm wide at working distance. Narrower parts return a mix of object and backdrop.
- **Sloped or curved tops** reflect light sideways and return weakly, which is where the roof detection matters most.
- **Glossy and transparent objects:** specular reflection sends light away from the sensor, and transparent material lets it through, so the sensor sees the backdrop.
- **Translucent materials** (wax, foam, marble) bias the distance by a few mm. This does not affect detecting presence.
- **Ambient IR.** Direct sunlight or halogen/incandescent lamps add noise in the sensor band; keep the rig away from windows.
- **Obstacles in the cone** (cables, carriage brackets, turntable edge) read as an object on every stop, so the scan then runs up to the top endstop.

## Dependencies
Before running the code, we must install the Adafruit CircuitPython dependency inside your `.venv`:
```bash
pip install adafruit-circuitpython-vl53l1x Adafruit-Blinka
```
