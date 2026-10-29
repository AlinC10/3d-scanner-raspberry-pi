# Motor Controller & Kinematics (`../motor.py`)

The `Motor` class in `../motor.py` is the Hardware Abstraction Layer (HAL) for the NEMA 17 stepper motors. 

**Important Architecture Update:** As part of the real-time execution migration, this class no longer toggles Raspberry Pi GPIO pins directly. Instead, it routes all mathematical pulse calculations and commands through the `ArduinoBridge` singleton to a dedicated Arduino Uno running bare-metal C++.

---

## ⚙️ Hardware Specifications

| Component | Specification | Description / Value |
|---|---|---|
| **Stepper Motor** | NEMA 17 Bipolar | `1.8°` step angle (200 full steps per 360° revolution) |
| **Driver Carrier** | TB6600 | Up to 1/16 microstepping, 5A peak current |
| **Z-Axis Lead Screw** | T8 Lead Screw | `8mm` pitch / lead (1 full 360° revolution = 8mm linear travel) |
| **Microcontroller**| Arduino Uno | 5V Logic, handles microsecond stepping pulses over USB Serial |

---

## 📌 Pinout & Rig Allocation

The `Motor` class now expects **Arduino Digital Pins**, NOT Raspberry Pi BCM GPIO pins.
In `../scanner.py`, two motor instances are initialized:

| Axis | Motor ID | Arduino DIR Pin | Arduino STEP Pin | Arduino EN Pin | Microstepping |
|---|---|---|---|---|---|
| **Turntable Motor** | `0` | `D7` | `D6` | `D8` | `1/16` (3200 steps/rev) at 1.8A |
| **Z-Axis Motor** | `1` | `D4` | `D5` | `D3` | `1/4` (800 steps/rev) at 1.8A |

The pins and microstepping above are the ones in `../scanner.py` (`Motor(motor_id=0, dir_pin=7, step_pin=6, en_pin=8, ...)` and `Motor(motor_id=1, dir_pin=4, step_pin=5, en_pin=3, ...)`); the microstepping constants are `TURNTABLE_STEP_TYPE = "1/16"` and `Z_STEP_TYPE = "1/4"` in `../motor.py`. They must match the DIP switches of the two TB6600 drivers. Note that the `Motor` constructor defaults (`step_pin=3, dir_pin=4, en_pin=2`) are not the rig wiring: always pass the pins explicitly.

> [!WARNING]
> **The Pin 13 Hazard**: Arduino Pin 13 is strictly avoided for stepper motor driver signals (especially EN or STEP) because the Optiboot bootloader pulses Pin 13 during the DTR serial reset, which can cause violent motor twitching on startup.

---

## 📐 Kinematics & Mathematics

### 1. Angle to Steps Conversion (`angle_to_steps_conversion`)

To rotate an axis by $\theta$ degrees, the number of required step pulses is calculated as:

$$\text{Step Angle} = 1.8^\circ \times \frac{\text{Numerator}}{\text{Denominator}}$$

$$\text{Steps} = \frac{\theta}{\text{Step Angle}}$$

#### Integer Divisibility & Drift Protection
To guarantee that 360° scans do not accumulate fractional step drift over dozens of stops, the conversion checks that the required step count is an exact integer:

$$\left| \text{Steps}_{\text{float}} - \text{round}(\text{Steps}_{\text{float}}) \right| \le 10^{-4}$$

If the requested angle cannot be cleanly represented by the configured microstepping resolution, a `MotorError` is raised immediately before sending commands to the Arduino.

#### Microstepping Resolution Table
| Microstepping (`step_type`) | Step Angle (deg) | Steps per 360° Revolution | Steps for 18.0° Jump |
|---|---|---|---|
| `"Full"` | $1.8^\circ$ | 200 | 10 |
| `"Half"` | $0.9^\circ$ | 400 | 20 |
| `"1/4"` *(Z-Axis)* | $0.45^\circ$ | 800 | 40 |
| `"1/8"` | $0.225^\circ$ | 1600 | 80 |
| `"1/16"` *(Turntable)* | $0.1125^\circ$ | 3200 | 160 |
| `"1/32"` | $0.05625^\circ$ | 6400 | 320 |

---

### 2. Linear Distance Conversion (`distance_to_step_conversion`)

The Z-axis camera carriage rides on a standard **T8 lead screw** with a pitch of $8\text{ mm}$ (`T8_THREADED_ROD_STEP = 8`):

$$\theta_{\text{degrees}} = \left( \frac{\text{Distance in mm}}{8\text{ mm}} \right) \times 360.0^\circ$$

$$\text{Steps} = \text{angle\_to\_steps\_conversion}(\theta_{\text{degrees}}, \text{step\_type})$$

#### Examples at 1/4 Microstepping (the Z axis of the rig):
At 1/4 microstepping one revolution is 800 steps = 8 mm, so the Z axis makes **100 steps/mm**.
* Moving **`1.0 mm`** $\to 45.0^\circ \to$ **100 steps**
* Moving **`30.0 mm`** (Home offset) $\to 1350.0^\circ \to$ **3,000 steps**
* Moving **`100.0 mm`** (Ring step) $\to 4500.0^\circ \to$ **10,000 steps**
* Maximum travel **`150 mm`** $\to$ **15,000 steps**; homing / move-to-top limit **`165 mm`** (150 mm + 15 mm margin) $\to$ **16,500 steps** (per `../../plans/implemented/MOTOR_FIXES_PLAN.md` 3.1, D3).

> [!NOTE]
> The same distances at 1/16 would be 400 steps/mm (1 mm = 400 steps). The Z axis is **not** configured at 1/16; do not use those numbers for the rig.

---

### 3. Speed Ramp & Slow Stop (`end_delay`)

The Arduino generates the step pulses, controlling the **period between steps** (`delay`, speed = `1 / delay`). `rotate()` sends:

| Parameter | Meaning |
|---|---|
| `delay` | Cruise step period (seconds). |
| `start_delay` | Step period at the start of the acceleration ramp (default `3 * delay`). |
| `ramp_percent` / `decel_percent` | Fraction of the steps used to accelerate / decelerate. |
| `end_delay` | **Optional.** Step period reached at the last deceleration step, so the move ends slowly. |

```
speed
  v_cruise      ┌────────────┐
               /              \
  v_start     /                \
                                 \
                                  v_end   <- last steps are slow (end_delay)
              accel   cruise    decel      -> steps
```

* **Constant acceleration:** the ramps are straight lines in speed over time (`v^2` is linear in the step count). The previous firmware interpolated the *delay* linearly, which gave a hyperbolic speed curve with a very large acceleration at one end of the ramp.
* **Slow stop:** with `end_delay` the deceleration goes from `1/delay` down to `1/end_delay` instead of `1/start_delay`. Ending at only a few tens of steps per second leaves the turntable (on a bearing) almost no kinetic energy, even with a heavy object.
* **Range and defaults** (constants in `motor.py`):

| Constant | Value | Meaning |
|---|---|---|
| `END_DELAY_MIN` | `0.005` s | 200 microsteps/s |
| `END_DELAY_MAX` | `0.030` s | 33 microsteps/s |
| `TURNTABLE_END_DELAY_DEFAULT` | `0.015` s | 67 microsteps/s; default for the turntable in `Scanner.scan()` and the API schema |

  `Motor.rotate()` clamps a requested value into `[END_DELAY_MIN, END_DELAY_MAX]` (logging a warning) and never lets it be faster than `delay`. The API schemas reject values outside the range (HTTP 422).
* **Off by default** on `Motor` and the Z-axis (`end_delay=None`: decelerate down to `start_delay`). It is ignored, with a warning, when `acceleration=False` or when the move has no deceleration steps.
* **Step-period limits (per `../../plans/implemented/MOTOR_FIXES_PLAN.md` 3.1, D4):** `delay` and `start_delay` are bounded by `STEP_DELAY_MIN = 0.0001` s (10 kHz) and `STEP_DELAY_MAX = 0.05` s (20 microsteps/s) in `motor.py`; `Motor.rotate()` raises `ValueError` outside this range and the API schemas answer 422. `end_delay` uses its own, narrower range (`END_DELAY_MIN`..`END_DELAY_MAX`, see the table above) and is clamped, not rejected, when called from Python.
* **Why 0.0001 s is the floor:** the STEP pulse is 50 us and the firmware enforces a period of at least 60 us, so `0.00005` would not be faster. For ramps with `delay < 0.0004` the `sqrtf` cost on the AVR can make steps slip in time (the real period exceeds the commanded one); `Motor.rotate` logs a warning once in that case.
* **Recommended scan speed:** `delay >= 0.0005` s. On the turntable (1/16) `0.0005` s = 2000 microsteps/s = 0.625 rev/s (~37 RPM); on Z (1/4) `0.0005` s = 2000 steps/s = 20 mm/s. Smaller values are only for manual tests, preferably with `acceleration=false`.
* **Z travel (per `../../plans/implemented/MOTOR_FIXES_PLAN.md` 3.1, D3):** the maximum Z travel is `150 mm` (`ZMoveRequest.distance_mm <= 150`, `Z_TRAVEL_MAX_MM`); homing and move-to-top are limited to `150 + 15 = 165 mm` = 16,500 steps at 1/4, so a missing endstop is reported as an error instead of driving the carriage far past the real stroke.
* **Short moves:** the ramp length is a *fraction of the steps*, so an 18° move (160 steps at 1/16) has only 32 deceleration steps at `decel_percent=0.2`. For heavy objects use a lower cruise speed (e.g. `delay=0.002`) and a larger `decel_percent` (e.g. `0.5`).

---

## ⚡ Thermal & Coil Power Management

Stepper motors consume full holding current even when stationary, which causes coils and drivers to overheat if left energized while idle. 
Because the Python script controls the high-level orchestration, it dictates power states via the `ENABLE` and `DISABLE` Arduino opcodes.

* **`enable()`**: Sends `0x03` to the Arduino. The Arduino drives the `EN` pin **LOW**. Energizes the motor coils for motion.
* **`disable()`**: Sends `0x04` to the Arduino. The Arduino drives the `EN` pin **HIGH**. Cuts current to the motor coils, releasing holding torque.

> [!NOTE]
> **EN level at boot:** after the firmware fix A1 (needs a re-flash) the Arduino drives `EN` **HIGH** (driver off) before configuring the pin, so both motors are free to turn by hand right after the server starts. With the older firmware the pin was LOW (energized) after `CONFIG` while the firmware believed the motor was disabled.

### Settling pause (`disable(settle_delay=0.5)`)

`Motor.disable()` keeps the coils energized for `settle_delay = 0.5 s` before cutting the current. This is a **mechanical hold**: the rotor / lead screw / platter stops and settles under holding torque instead of coasting or dropping, and the following photo is not taken while the platter is still moving. That the pause also avoids inductive flyback or back-EMF transients on the supply is an **unverified hypothesis**; it has not been measured on this rig and must not be relied on as an electrical protection.

### Z axis is energized only while it moves

Per `../../plans/implemented/MOTOR_FIXES_PLAN.md` 3.1 (D2): the Z axis motor is disabled whenever it is idle (between rings, after homing, after the +30 mm move) and is energized only for the duration of a move (`enable()` is called explicitly before each move). There is no option to hold Z torque between rings. With the T8x8 lead screw (8 mm lead, 100 steps/mm at 1/4) the carriage has to be self-holding when released; verify it on the bench with the camera load (see `PRE_TEST_CHECKLIST.md`).

---

## 📚 Class Reference: `Motor`

### Constructor
```python
Motor(
    motor_id: int = 0,
    step_pin: int = 3,
    dir_pin: int = 4,
    en_pin: Optional[int] = 2,
    step_type: str = "Full",
    port: Optional[str] = None
)
```
* `motor_id`: Unique identifier (e.g., 0 for turntable, 1 for Z-axis) mapped on the Arduino.
* `step_pin`: Arduino Digital Pin for `STEP`.
* `dir_pin`: Arduino Digital Pin for `DIR`.
* `en_pin`: Arduino Digital Pin for `EN`.
* `step_type`: Mathematical microstepping mode string.
* `port`: Optional override for the `ArduinoBridge` singleton connection.

The constructor sends `CONFIG` through `configure()` and **never raises** for it: when the Arduino is missing (the server starts anyway, API-21) or refuses the configuration, `configured` is `False` and `config_error` says why. `configure()` sends it again (`Scanner.require_motors()` does so before a motor request); the bridge also replays it on every reconnect.

### Core Methods

> **Default speed profile.** `rotate()` and `rotate_angle()` (the turntable) default to `acceleration=True`. `rotate_distance()` (the Z axis) defaults to `acceleration=False`, like `Scanner.move_z_up/down`: the Z axis runs at constant speed unless a caller asks for the ramp (decision 2026-10-28, Z-ACC-2).

#### `rotate(clockwise: bool = True, steps: int = 200, delay: float = 0.002, acceleration: bool = True, ramp_percent: float = 0.2, decel_percent: float = None, start_delay: float = None, end_delay: float = None, **kwargs) -> bool`
Rotates the motor using a constant-acceleration speed ramp (see *Speed Ramp & Slow Stop*).
* Converts float `delay` (seconds) into integer `target_us` (microseconds).
* Supports asymmetric ramping (different `ramp_percent` and `decel_percent`).
* `delay` / `start_delay` must lie in `[STEP_DELAY_MIN, STEP_DELAY_MAX]` = `[0.0001, 0.05]` s, otherwise `ValueError` (per `../../plans/implemented/MOTOR_FIXES_PLAN.md` 3.1, D4).
* `end_delay` (optional, seconds): step period at the last deceleration step for a slow stop. Clamped to `[0.005, 0.030]`; `None` keeps the old behaviour. Sent as the optional 8th argument of `ACCEL_ROTATE`.
* Dispatches `ACCEL_ROTATE` (`0x10`) if ramping is used, otherwise falls back to constant-velocity `ROTATE` (`0x05`).
* Dynamically calculates a PySerial timeout from the exact duration of each constant-acceleration phase (`t = 2 * steps / (v_a + v_b)`).
* Returns `True` if completed, or `False` if aborted early by `0xFF`.

#### `stop(release_torque: bool = False, repeat: int = 1) -> None`
Immediately injects the out-of-band `0xFF` emergency stop byte directly over the UART wire, bypassing all transaction locks. The Arduino intercepts this in sub-milliseconds and halts any active `rotate` loop. `repeat` sends the byte several times (used by `emergency_stop`; the `repeat` parameter is a contract of `../../plans/implemented/MOTOR_FIXES_PLAN.md` section 1, see `ARDUINO_BRIDGE.md`). `0xFF` is universal (not per motor): it stops whatever is moving and de-energizes all motors, so `is_enabled` is cleared on every motor. If `release_torque=True`, it explicitly follows up with a `DISABLE` command.

*(For a full list of methods and signatures, refer to the Sphinx docstrings inside `motor.py`).*

---

## 💻 Python Usage Examples

### 1. 18° Turntable Step (Single Capture Jump)
```python
from hardware.motor import Motor

# Turntable mapping on Arduino: DIR D7, STEP D6, EN D8
turntable = Motor(motor_id=0, step_pin=6, dir_pin=7, en_pin=8, step_type="1/16")

turntable.enable()
try:
    # Rotate 18 degrees clockwise with 1.0ms step pulse delay
    # The Motor class automatically converts 0.0010s to 1000us for the Arduino
    turntable.rotate_angle(clockwise=True, angle=18.0, delay=0.0010)
finally:
    turntable.disable()  # Cut power so motor doesn't get hot while camera captures
```

### 2. Linear Elevator Movement (Z-Axis)
```python
from hardware.motor import Motor

# Z-Axis mapping on Arduino: DIR D4, STEP D5, EN D3
z_axis = Motor(motor_id=1, step_pin=5, dir_pin=4, en_pin=3, step_type="1/4")

z_axis.enable()
try:
    # Climb 30.0 mm up (3000 steps at 1/4 microstepping, 100 steps/mm), at constant speed (the default here)
    z_axis.rotate_distance(clockwise=True, distance=30.0, delay=0.0010)
finally:
    z_axis.disable()
```
