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
| `timing_budget` | `200` ms | Lower jitter. Not free: every read waits up to one budget (about 0.2 s), and a stop makes up to two reads, see section 4. |

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

### 2. Fresh Measurement per Read (`read_distance_mm`)
The sensor ranges continuously, so a sample may already be waiting from before the last turntable move. Every read therefore:
1. Discards the waiting sample with `clear_interrupt()`.
2. Polls `data_ready` every 5 ms, with a timeout of `2 × timing_budget + 100 ms`.
3. Reads the distance (centimetres from the driver, converted to millimetres) and clears the interrupt.

A read blocks for up to one timing budget (about 200 ms). The whole sequence is guarded by a `threading.Lock`, because the scan loop and the `/test/tof` endpoint (which FastAPI runs in a thread pool) can call it at the same time, and one caller's `clear_interrupt()` would otherwise consume the sample the other is waiting for.

**Limit of "fresh".** A measurement that was already in progress when `clear_interrupt()` ran finishes afterwards and is returned, so the sample can be up to one timing budget (about 200 ms) older than the call. The scanner's settling pause is what keeps this harmless; the driver does not throw that sample away.

### 3. Invalid Reading vs No Object
The wrapper separates two different things:

| Outcome | Meaning | Returned by `read_distance_mm()` |
| :--- | :--- | :--- |
| **Valid reading, far** | The sensor sees the backdrop (about 535 mm). No object in the cone. | the distance in mm |
| **Valid reading, near** | An object is in the cone. | the distance in mm |
| **Invalid reading** | Unknown. Not "no object". | `None` |

`read_distance_mm()` returns `None` when:
- the sensor is not available (library missing, I2C wires disconnected, wrong model ID at start-up). The call returns immediately, it does not wait;
- the driver reports `None`, which happens when the range status is not valid (no target, signal too weak, out of range);
- the wait for a measurement times out;
- an I2C or driver error occurs;
- the value is not a finite positive number.

It never returns `inf`. The empty backdrop is itself a **valid** reading, so an invalid reading cannot mean "nothing there": a dark or absorbing object, a failing sensor and a loose cable all look like this, and none of them is evidence that the object has ended. The scan must therefore not end because of invalid readings.

`get_distance_mm()` is kept for older callers and is built on `read_distance_mm()`: it returns `float('inf')` when the reading is `None`. It cannot tell "invalid" from "far", so new code should not use it for decisions.

At start-up `__init__` never raises. If `board`, `busio` or `adafruit_vl53l1x` cannot be imported, importing `tof.py` still works and the sensor is marked `available = False` with a warning. If the driver constructor or the configuration fails after the I2C bus was opened, the bus is released (`deinit`) and the sensor is unavailable for that run. `close()` can be called any number of times.

### 4. Object Probe (`probe_object`, `is_object_detected`)
Laser sensors can occasionally bounce off dust or highly reflective surfaces at odd angles. To prevent a false positive, `probe_object(threshold, samples=2)` takes up to `samples` **consecutive fresh readings**. Each read waits for its own measurement, so the second one is always taken entirely after the first and after the turntable has stopped. It returns `Optional[bool]`, reading by reading:

| Reading | Result |
| :--- | :--- |
| invalid (`None`) | stop, return `None` (no decision, even if an earlier reading was below the threshold) |
| valid and `>= threshold` | stop, return `False` (backdrop seen, definite) |
| valid and `< threshold` | take the next reading |
| `samples` valid readings, all `< threshold` | return `True` (object present) |

So `False` is always backed by a valid far reading; invalid readings only ever give `None`. The caller decides what `None` means (the scanner counts it as "unknown", not "object finished").

`is_object_detected(threshold)` stays for compatibility and is `True` only when `probe_object(threshold)` is `True`; both `False` and `None` give `False`.

**Cost.** Each read takes up to one timing budget, so a probe takes about one budget when the first reading is far or invalid and two budgets (about 0.4 s at 200 ms) when the object is present. The second read is not free: the scan loop reserves 0.2 s per stop and the scanner only sleeps what remains of it, so a stop with an object is longer than 0.2 s whenever the probe is made. A shorter timing budget (see section 1) lowers this directly.

The threshold used by the scanner is `TOF_BACKGROUND_DISTANCE_MM - TOF_CLEARANCE_MARGIN_MM` (both constants in `scanner.py`), so ToF noise on the empty backdrop is not mistaken for an object.

### 5. `GET /test/tof`
Always answers `200`. It never fails because of an invalid reading.

```json
{ "status": "success", "distance_mm": 535.2, "valid": true,  "available": true }
{ "status": "invalid", "distance_mm": null,  "valid": false, "available": true }
{ "status": "invalid", "distance_mm": null,  "valid": false, "available": false }
```

- `valid: false, available: true`: the sensor is there but the reading is invalid (no target, weak signal, timeout, I2C error).
- `available: false`: the sensor never initialised (see the start-up warning in the log: library missing, wires, wrong model ID). Restart after fixing the cause.

---

## 🚦 Integration in the Scanner Loop

During `Scanner.scan()`:
- At every rotational stop (e.g. 20 stops per 360° ring), after the turntable has moved, `tof.is_object_detected()` (or `probe_object()`, to keep an invalid reading apart from a far one) is called until the object has been seen on that level. The call also counts towards the settling pause, and the scanner sleeps whatever remains of 0.2 s.
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

- **Systematic misses.** The 20 stops on a ring are not independent chances. They help when the miss depends on the angle (asymmetric object, partial coverage), but a miss caused by the material is the same on every stop. If the sensor sees through or past the object and returns a *valid* far reading (transparent, glossy), the scan ends after level 1 and the log shows `ToF Sensor cleared the object at level 1`. If the material gives an *invalid* reading (very dark), the result is unknown, not "no object".
- **An object that absorbs all light** returns an invalid reading, not a far one. It is reported as unknown (`None`), never as "no object", so it cannot end the scan by itself; but it also gives no evidence that the object is there.
- **Invalid readings on every stop** (sensor unplugged, flaky I2C, very dark backdrop) mean the roof can never be confirmed. How the scanner reacts is decided in `scanner.py`, not here.
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
