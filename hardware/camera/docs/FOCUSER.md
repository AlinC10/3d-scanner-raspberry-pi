# Voice Coil Motor (VCM) Driver (`../focuser.py`)

The `Focuser` class in `../focuser.py` is the low-level hardware driver for the **motorized Voice Coil Motor (VCM)** integrated into the **Arducam IMX477 (B0272)** camera module. 

It communicates directly with an onboard **DW9714-compatible 10-bit DAC driver** over the Raspberry Pi's I2C bus using `smbus2`.

---

## 🔍 Hardware Working Principle

A Voice Coil Motor (VCM) functions similarly to an audio speaker:
* An electromagnetic coil is suspended within a permanent magnetic field and counterbalanced by a mechanical spring.
* Applying current through the coil generates a Lorentz force that displaces the optical lens along its optical axis.
* **Higher DAC values (more current)** push the lens forward toward the object (**Macro / Near focus**).
* **Lower DAC values (less current)** allow the spring to pull the lens back toward the sensor (**Infinity / Far focus**).

```
  DAC Value:    0                                                     1023
  Focus Range:  Infinity (Far) ◄────────────────────────────────► Macro (Near)
  Lens State:   Relaxed against spring                        Max forward extension
```

---

## ⚡ I2C Protocol & Byte Packing

The DW9714 driver IC communicates at I2C slave address **`0x0C`** (`VCM_I2C_ADDR`) and expects a 10-bit DAC integer (`0` to `1023`):

### 1. Initialization Sequence
Before the driver can move the lens, it must be powered up by writing `0x00` to control register `0x02`:
```python
self._bus.write_byte_data(0x0C, 0x02, 0x00)
```

### 2. 10-bit DAC Register Format
The 10-bit target position is split across two bytes transmitted via `write_i2c_block_data`:
* **High Byte**: Bits $[9:4]$ masked to 6 bits:
  $$\text{high\_byte} = (\text{value} \gg 4) \ \& \ \text{0x3F}$$
* **Low Byte**: Bits $[3:0]$ shifted left by 4 bits:
  $$\text{low\_byte} = (\text{value} \ \& \ \text{0x0F}) \ll 4$$

```
  10-bit Position: [ D9  D8  D7  D6  D5  D4  D3  D2  D1  D0 ]
  High Byte:       [  0   0  D9  D8  D7  D6  D5  D4 ]  (Command byte)
  Low Byte:        [ D3  D2  D1  D0   0   0   0   0 ]  (Data byte)
```

---

## 🔌 Camera Power Gating & Deferred Init

On Raspberry Pi 4 and 5 compute modules, the CSI camera's I2C buses (`i2c-10` and `i2c-11`) are **power-gated by the Broadcom VideoCore ISP**.

> [!WARNING]
> **VCM Unreachable When Camera is Idle**:
> The physical VCM driver chip only receives power while `picamera2` is actively streaming or capturing. Writing to the I2C bus while the camera is closed results in an immediate Linux kernel `OSError: [Errno 121] Remote I/O error`.

### How `Focuser` Handles This
1. The constructor opens the I2C bus handle (`smbus2.SMBus(bus)`) and loads the last-saved focus state from disk, but **defers** the `0x02` power-on write.
2. The `_ensure_init()` method wraps the power-on write in a `try/except OSError` block, logging a debug notice if the camera is not yet streaming.
3. When `picamera2` boots, the first call to `set_position()` or `restore_state()` safely completes the initialization.

---

## 🛡️ Mechanical Shock Prevention: The Smooth Ramp

Because the lens assembly is suspended on microscopic springs, sending a sudden jump command (e.g. `0` $\to$ `800`) causes the lens to snap violently against its physical stops, producing:
* Audible clicking ("lens slap").
* Mechanical ringing and blurred photos.
* Premature fatigue on the VCM spring.

### The Smooth Ramp Implementation
In `set_position()`, large jumps are broken down into a sequence of microsteps:
* **Step Size**: Maximum 20 DAC units per step.
* **Step Delay**: 2 ms (`0.002s`) per microstep.
* **Settle Time**: A post-movement pause (`VCM_MOVE_DELAY_S = 0.05s`) allows mechanical oscillation to completely dampen before any shutter opens.

```python
# From hardware/camera/focuser.py
direction = 1 if target > self._position else -1
for p in range(self._position, target, direction * 20):
    self._write_raw(p)
    time.sleep(0.002)

self._write_raw(target)
self._position = target
```

---

## 💾 State Persistence Across Reboots

Because VCMs are spring-loaded, removing power causes the lens to mechanically snap back to position `0`. However, software may assume the lens is still focused at position `450`.

To prevent optical desynchronization:
1. Every time `set_position()` finishes, it writes the position to a bus-specific JSON file:
   ```
   hardware/camera/config/focus_state_bus10.json
   hardware/camera/config/focus_state_bus11.json
   ```
   ```json
   {"position": 420}
   ```
2. When the camera is rebooted or re-initialized, `restore_state()` is called:
   * Reads the saved target position.
   * Smoothly ramps the physical lens from `0` back to the saved target without needing a new autofocus sweep!

---

## 📚 Class Reference: `Focuser`

### Constructor
```python
Focuser(bus: int = VCM_I2C_BUS, addr: int = VCM_I2C_ADDR)
```
* `bus`: I2C bus number (`10` for CSI 0, `11` for CSI 1).
* `addr`: I2C slave address (default: `0x0C`).

---

### Properties

* **`position -> int`**: Returns the currently tracked DAC position (`0`–`1023`).

---

### Methods

#### `set_position(pos: int, settle: bool = True) -> None`
Drives the lens to an absolute position (`0` = $\infty$, `1023` = macro) using the smooth ramp algorithm.
* Clamps input between `VCM_MIN_POS` (0) and `VCM_MAX_POS` (1023).
* Automatically updates the JSON state file.
* If `settle=True`, waits for optical settling time.

#### `step(delta: int) -> None`
Relative focus adjustment.
* Positive $\Delta$: Moves focus closer (macro).
* Negative $\Delta$: Moves focus further away (infinity).

#### `reset() -> None`
Drives the lens smoothly to position `0` (infinity).

#### `restore_state() -> None`
Re-applies the last saved position from the JSON state cache. Called automatically after camera sensor boot.

#### `close() -> None`
Closes the `smbus2.SMBus` file descriptor.

---

## 💻 Standalone Python Usage Example

```python
import time
from picamera2 import Picamera2
from hardware.camera.focuser import Focuser

# 1. Boot the camera sensor to provide power to the VCM I2C bus
picam2 = Picamera2(camera_num=0)
picam2.configure(picam2.create_still_configuration())
picam2.start()

try:
    # 2. Instantiate Focuser on bus 10 (CSI port 0)
    focuser = Focuser(bus=10)
    
    # 3. Restore last known position
    focuser.restore_state()
    print(f"Current focus position: {focuser.position}")
    
    # 4. Smoothly move to macro focus (pos = 750)
    print("Ramping to macro position...")
    focuser.set_position(750)
    
    # 5. Step relative fine focus
    focuser.step(+15)  # Move slightly closer
    focuser.step(-30)  # Move slightly farther
    
    # 6. Return to infinity
    focuser.reset()
    
finally:
    focuser.close()
    picam2.stop()
    picam2.close()
```
