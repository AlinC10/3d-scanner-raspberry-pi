# Motor Controller & Kinematics (`../motor.py`)

The `Motor` class in `../motor.py` is the hardware abstraction layer for driving **NEMA 17 stepper motors** using **Texas Instruments TB6600 stepper motor drivers** on Raspberry Pi OS.

It encapsulates microstepping pulse calculations, angular rotation math, linear T8 lead screw kinematics, and thermal/power management via active-low enable control.

---

## ⚙️ Hardware Specifications

| Component | Specification | Description / Value |
|---|---|---|
| **Stepper Motor** | NEMA 17 Bipolar | `1.8°` step angle (200 full steps per 360° revolution) |
| **Driver Carrier** | TB6600 | Up to 1/16 microstepping, 5A peak current |
| **Z-Axis Lead Screw** | T8 Lead Screw | `8mm` pitch / lead (1 full 360° revolution = 8mm linear travel) |
| **Logic Voltage** | 3.3V GPIO | Compatible with Raspberry Pi 4 / 5 GPIO logic levels |

---

## 📌 Pinout & Rig Allocation

In the 3D scanner architecture, two separate `Motor` instances are initialized in [`../scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/scanner.py):

| Axis | Role | DIR Pin | STEP Pin | EN Pin | Default Microstepping |
|---|---|---|---|---|---|
| **Turntable Motor** | Platter 360° rotation | `GPIO 24` | `GPIO 23` | `GPIO 18` | `1/4` (800 steps/rev) |
| **Z-Axis Motor** | Camera vertical elevator | `GPIO 19` | `GPIO 26` | `GPIO 21` | `1/16` (3200 steps/rev) |

> [!NOTE]
> **Hardwired Microstepping Pins (`mode_pins=(-1, -1, -1)`)**:
> The TB6600 driver mode pins are hardwired physically on the driver board (via pull-up/pull-down resistors). 
The `step_type` parameter is used exclusively by our Python conversion math to calculate the required pulse count.

---

## 📐 Kinematics & Mathematics

### 1. Angle to Steps Conversion (`angle_to_steps_conversion`)

To rotate an axis by $\theta$ degrees, the number of required step pulses is calculated as:

$$\text{Step Angle} = 1.8^\circ \times \frac{\text{Numerator}}{\text{Denominator}}$$

$$\text{Steps} = \frac{\theta}{\text{Step Angle}}$$

#### Integer Divisibility & Drift Protection
To guarantee that 360° scans do not accumulate fractional step drift over dozens of stops, the conversion checks that the required step count is an exact integer:

$$\left| \text{Steps}_{\text{float}} - \text{round}(\text{Steps}_{\text{float}}) \right| \le 10^{-4}$$

If the requested angle cannot be cleanly represented by the configured microstepping resolution, a `MotorError` is raised immediately before moving the hardware.

#### Microstepping Resolution Table
| Microstepping (`step_type`) | Step Angle (deg) | Steps per 360° Revolution | Steps for 18.0° Jump |
|---|---|---|---|
| `"Full"` | $1.8^\circ$ | 200 | 10 |
| `"Half"` | $0.9^\circ$ | 400 | 20 |
| `"1/4"` *(Turntable)* | $0.45^\circ$ | 800 | 40 |
| `"1/8"` | $0.225^\circ$ | 1600 | 80 |
| `"1/16"` *(Z-Axis)* | $0.1125^\circ$ | 3200 | 160 |
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

The TB6600 `EN` (Enable) pin is **active-low**:
* **`enable()`**: Drives `EN` pin **LOW** (`en_device.off()`). Energizes the motor coils for motion.
* **`disable()`**: Drives `EN` pin **HIGH** (`en_device.on()`). Cuts current to the motor coils, releasing holding torque.

### Defensive Best Practice
* Motors are initialized in the **`disabled`** state.
* Coils are energized only immediately before `rotate()`, `rotate_angle()`, or `rotate_distance()`.
* Calling `stop(release_torque=True)` halts the pulse generator and immediately calls `disable()`.

---

## 📚 Class Reference: `Motor`

### Constructor
```python
Motor(
    dir_pin: int = 20,
    step_pin: int = 21,
    en_pin: Optional[int] = None,
    step_type: str = "Full"
)
```
* `dir_pin`: BCM GPIO pin connected to driver `DIR`.
* `step_pin`: BCM GPIO pin connected to driver `STEP`.
* `en_pin`: BCM GPIO pin connected to driver `EN`. Pass `None` if hardwired to GND.
* `step_type`: Default microstepping string (`"Full"`, `"Half"`, `"1/4"`, `"1/8"`, `"1/16"`, `"1/32"`).

---

### Methods

#### `enable() -> None`
Enables the driver output stages by setting `EN` pin LOW.

#### `disable() -> None`
Shuts down driver output stages by setting `EN` pin HIGH, dropping holding torque.

#### `angle_to_steps_conversion(angle: float = 18, step_type: Optional[str] = None) -> int`
Calculates step count for a given rotation angle. Raises `MotorError` if not cleanly divisible.

#### `distance_to_step_conversion(distance: float = 100.0, step_type: Optional[str] = None) -> int`
Converts millimeters of linear T8 rod travel into integer step pulses.

#### `rotate_angle(clockwise: bool = True, angle: float = 18, delay: float = 0.002, step_type: Optional[str] = None, initial_delay: float = 0.05) -> None`
Rotates the motor by exact degrees. Verifies angle divisibility before starting.

#### `rotate_distance(clockwise: bool = True, distance: float = 100.0, delay: float = 0.002, step_type: Optional[str] = None, initial_delay: float = 0.05) -> None`
Moves linear lead screw carriage by distance in millimeters.

#### `rotate(clockwise: bool = True, steps: int = 200, delay: float = 0.002, step_type: Optional[str] = None, verbose: bool = False, initial_delay: float = 0.05, acceleration: bool = True, start_delay: Optional[float] = None, ramp_percent: float = 0.2, cancel_event: Optional[threading.Event] = None) -> None`
Native GPIO step pulse dispatcher with smooth trapezoidal / triangular acceleration and deceleration profiling.
* `acceleration`: When `True` (default), smoothly ramps speed up and down to eliminate inertial overshoot with heavy objects. Set to `False` for constant-velocity stepping.
* `start_delay`: Initial and final crawl delay. Defaults to `max(delay * 3.0, 0.0025)`.
* `ramp_percent`: Percentage of total steps spent accelerating and decelerating (defaults to `0.20`, max `0.50`).
* `cancel_event`: Optional external `threading.Event` checked alongside the internal `_stop_event` on every step pulse for cooperative cancellation.

#### `stop(release_torque: bool = False) -> None`
Signals the internal `threading.Event` to immediately break an active `rotate()` loop in sub-milliseconds. If `release_torque=True`, immediately calls `disable()`.

#### `handle_driver_fault() -> None`
Emergency handler for TB6600 `FAULT` conditions. Stops motor and raises `MotorError`.

---

## 💻 Python Usage Examples

### 1. 18° Turntable Step (Single Capture Jump)
```python
from hardware.motor import Motor

# Turntable: DIR=24, STEP=23, EN=18, 1/4 microstepping
turntable = Motor(dir_pin=24, step_pin=23, en_pin=18, step_type="1/4")

turntable.enable()
try:
    # Rotate 18 degrees clockwise with 0.5ms step pulse delay
    turntable.rotate_angle(clockwise=True, angle=18.0, delay=0.0010)
finally:
    turntable.disable()  # Cut power so motor doesn't get hot while camera captures
```

### 2. Linear Elevator Movement (Z-Axis)
```python
from hardware.motor import Motor

# Z-Axis: DIR=19, STEP=26, EN=21, 1/16 microstepping
z_axis = Motor(dir_pin=19, step_pin=26, en_pin=21, step_type="1/16")

z_axis.enable()
try:
    # Climb 30.0 mm up
    z_axis.rotate_distance(clockwise=True, distance=30.0, delay=0.0010)
finally:
    z_axis.disable()
```

### 3. Safe Homing Loop with Interrupt
```python
from hardware.motor import Motor
from hardware.endstop import Endstop, BottomEndstopTriggered

z_motor = Motor(dir_pin=19, step_pin=26, en_pin=21, step_type="1/16")
bottom_switch = Endstop(pin=3, pull_up=True)

# Hardware safety interrupt: switch press immediately halts the motor
bottom_switch.when_pressed = z_motor.stop

z_motor.enable()
try:
    while not bottom_switch.is_active:
        z_motor.rotate(clockwise=False, steps=20, delay=0.0010)
finally:
    z_motor.stop(release_torque=True)
```
