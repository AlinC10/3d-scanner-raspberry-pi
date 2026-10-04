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
| **Turntable Motor** | `0` | `D4` | `D3` | `D2` | `1/16` (3200 steps/rev) at 1.8A |
| **Z-Axis Motor** | `1` | `D7` | `D6` | `D5` | `1/4` (800 steps/rev) at 1.8A |

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

#### Examples at 1/16 Microstepping:
* Moving **`1.0 mm`** $\to 45.0^\circ \to$ **400 steps**
* Moving **`30.0 mm`** (Home offset) $\to 1350.0^\circ \to$ **12,000 steps**
* Moving **`100.0 mm`** (Ring step) $\to 4500.0^\circ \to$ **40,000 steps**

---

## ⚡ Thermal & Coil Power Management

Stepper motors consume full holding current even when stationary, which causes coils and drivers to overheat if left energized while idle. 
Because the Python script controls the high-level orchestration, it dictates power states via the `ENABLE` and `DISABLE` Arduino opcodes.

* **`enable()`**: Sends `0x03` to the Arduino. The Arduino drives the `EN` pin **LOW**. Energizes the motor coils for motion.
* **`disable()`**: Sends `0x04` to the Arduino. The Arduino drives the `EN` pin **HIGH**. Cuts current to the motor coils, releasing holding torque.

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

### Core Methods

#### `rotate(clockwise: bool = True, steps: int = 200, delay: float = 0.002, **kwargs) -> bool`
Converts the float `delay` (seconds) into integer `delay_us` (microseconds). Dispatches the `ROTATE` (`0x05`) command to the Arduino. Dynamically calculates a PySerial timeout based on the formula: `steps * delay * 1.5 + 3.0s`. Returns `True` if completed, or `False` if aborted early by `0xFF`.

#### `stop(release_torque: bool = False) -> None`
Immediately injects the out-of-band `0xFF` emergency stop byte directly over the UART wire, bypassing all transaction locks. The Arduino intercepts this in sub-milliseconds and halts any active `rotate` loop. If `release_torque=True`, it explicitly follows up with a `DISABLE` command.

*(For a full list of methods and signatures, refer to the Sphinx docstrings inside `motor.py`).*

---

## 💻 Python Usage Examples

### 1. 18° Turntable Step (Single Capture Jump)
```python
from hardware.motor import Motor

# Turntable mapping on Arduino: D4, D3, D2
turntable = Motor(motor_id=0, step_pin=3, dir_pin=4, en_pin=2, step_type="1/16")

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

# Z-Axis mapping on Arduino: D7, D6, D5
z_axis = Motor(motor_id=1, step_pin=6, dir_pin=7, en_pin=5, step_type="1/4")

z_axis.enable()
try:
    # Climb 30.0 mm up
    z_axis.rotate_distance(clockwise=True, distance=30.0, delay=0.0010)
finally:
    z_axis.disable()
```
