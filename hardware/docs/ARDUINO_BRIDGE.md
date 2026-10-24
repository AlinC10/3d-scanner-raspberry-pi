# Python ArduinoBridge - Architecture & Reference

This document covers `hardware/arduino_bridge.py`, which serves as the Python Hardware Abstraction Layer (HAL). It manages the lifecycle, thread safety, and communication flow between the Raspberry Pi's high-level logic and the Arduino Uno's C++ real-time execution unit.

---

## 1. Overview & Singleton Pattern

Because the 3D Scanner features two stepper motors (Turntable and Z-Axis) which both communicate over the **exact same physical USB serial cable**, they cannot instantiate independent serial connections. If `Motor 0` opens `/dev/ttyACM0`, an attempt by `Motor 1` to open the same port will result in an `OSError: [Errno 16] Device or resource busy`.

To resolve this, `ArduinoBridge` is implemented as a **Thread-Safe Singleton**. 
Whenever a `Motor` class is instantiated, it calls `ArduinoBridge.get_instance()`. The class checks an internal `_singleton_lock`; if the connection is already open, it simply returns the shared instance.

---

## 2. Bootloader Handshake & Port Discovery

### Smart Auto-Discovery
Instead of hardcoding a path like `/dev/ttyACM0` (which can drift to `ttyACM1` if replugged while running), the bridge iterates through `serial.tools.list_ports`. It actively scans the OS-level USB hardware descriptors for known controller chips:
`["ARDUINO", "CH340", "CH341", "CP210", "FTDI", "USB-SERIAL"]`.

### The DTR Hardware Reset
Opening a serial port to an Arduino Uno automatically drops the DTR pin, forcing a hard hardware reset. The Arduino will spend approximately 1.5 seconds trapped inside its Optiboot bootloader.
If Python sends configuration strings during this window, they are permanently lost.

To prevent this, `_connect()` initiates an **Event-Driven Handshake**:
1. It opens the port.
2. It blocks and polls the serial buffer for up to 3 seconds.
3. It waits for the C++ firmware's `setup()` function to emit the `READY\n` boot banner (older firmware printed `0\n`; the handshake accepts both).
4. Once received, communication is guaranteed to be safe.

---

## 3. The Two-Lock Threading Architecture

Because multiple threads (e.g., the web API, a background scanning loop, and safety endstop monitors) may all attempt to interact with the motors simultaneously, `ArduinoBridge` implements a rigorous **Two-Lock Architecture**:

1. **`_write_lock` (The Byte Lock)**
   Guarantees that physical bytes sent over UART do not interleave. (e.g., prevents Thread A writing `5 0 200` and Thread B writing `6 1` from mixing into `5 60 1 200`).

2. **`_response_lock` (The Transaction Lock)**
   This is the critical lock for synchronous commands. When Thread A wants to send a command that expects a response, it takes this lock. 
   No other standard command can be sent until Thread A successfully reads its response off the wire. This ensures that Thread B does not accidentally steal Thread A's `STATUS_OK` response from the serial buffer.

---

## 4. Timeouts & The Out-of-Band Abort

### Dynamic Timeouts
A standard web API expects responses within milliseconds. However, physical hardware moves at real-world speeds. A command to home the Z-axis (up to 49,000 steps: 490 mm at 100 steps/mm) takes about a minute at 1 ms per step.

Because the `ROTATE` command is synchronous (the Arduino will not return `0\n` until the final step is taken), the `send_command()` timeout is **dynamically adjusted** based on the kinematics:
$$ \text{Timeout} = \text{duration} \times 1.5 + 3.0\text{s} + 2 \times \max(\text{start\_us}, \text{end\_us}) / 10^6 $$
where `duration` is the exact time of the constant-acceleration phases (mean of the end speeds) plus the cruise steps. The last term covers the full final step period, which the mean-speed integral understates when the move ends on a long step (a `start_delay` of 50 ms, or a slow-stop `end_delay`). The firmware ramp was simulated step by step in `tests/test_ws_b_motor_bridge.py` and the timeout is asserted to be at least 1.2x the simulated duration over a grid of steps, delays and ramp percentages.
This ensures Python correctly blocks for the duration of the movement without falsely triggering a serial `TimeoutException`.

### Step-delay limits (`hardware/motor.py`)

`Motor.rotate` raises `ValueError` (before anything is sent) when `delay` or `start_delay` is outside `[STEP_DELAY_MIN, STEP_DELAY_MAX]` = `[0.0001 s, 0.05 s]` (10 000 to 20 steps/s). `end_delay` keeps its own clamp to `[END_DELAY_MIN, END_DELAY_MAX]`, but a value `<= 0` is rejected. The STEP pulse is 50 us and the firmware enforces a period of at least 60 us, so a faster delay brings nothing.
With a speed ramp and `delay < 0.0004 s` the per-step `sqrtf` can make the step timing slip on the AVR: a single warning is logged (the move is not rejected). Prefer `acceleration=False` or `delay >= 0.0005` there.

### Response Resynchronisation

`send_command()` keeps the request/response stream aligned even after failures:

* The input buffer is flushed before every write, so a late reply of an earlier timed-out move (or the `0` boot banner of an Arduino that reset on its own) cannot be taken for the answer to the next command.
* Only numeric status lines are accepted; anything else is logged and skipped.
* On a timeout the bridge sends `0xFF` (the move may still be running), swallows the late `4`, flushes the buffer and returns `''`.
* On a timeout of a *non-move* command (`ENABLE`, `DISABLE`, `CONFIG`, ...) no `0xFF` is sent: nothing can be running, and a stop sent to an idle Arduino would cut the torque of every motor.
* The firmware boot banner is `READY` (not a numeric status). A `READY` line in the middle of a transaction means the board rebooted and the command was lost: it is reported as `4` (aborted) and bumps `connection_epoch`.
* An answer `3` (`ERR_NOT_CONFIGURED`) means the board reset without dropping the USB link and forgot the motor pins. The stored `CONFIG` commands are replayed and the command is retried once (the refused command did nothing, so the retry is safe).
* `connection_epoch` is incremented on every (re)connection. A reconnect resets the Arduino (all drivers disabled), and `Motor.is_enabled` uses the epoch to drop stale "energized" state.
* `stop_epoch` is incremented on every universal stop (`send_stop()`, and the `0xFF` of the timeout resync above). A `0xFF` received while the Arduino is idle disables **all** drivers, not only the one of the `Motor` that asked for it. `Motor.is_enabled` is therefore False when either `connection_epoch` or `stop_epoch` changed since the motor was last known energized (`enable()` or a successful `rotate()`, which re-arm it). The epoch is read *before* the command is sent, so a stop that lands during a move which still reports `0` also invalidates it. `Motor` reads it with `getattr(bridge, "stop_epoch", 0)`, so bridge stand-ins without the attribute keep working.

### Out-of-Band Emergency Stop (`0xFF`)
If the system is executing a 30-second rotation and an endstop limit switch is struck, Python must abort the rotation instantly. But because Thread A is holding the `_response_lock` waiting for the 30-second move to finish, a normal `STOP` command would be blocked.

To solve this, `send_stop()` is designated as an out-of-band bypass:
1. It ignores the `_response_lock`.
2. It acquires only the `_write_lock`.
3. It directly injects the raw `0xFF` byte onto the wire.
4. The Arduino firmware intercepts `0xFF` between steps, halts, cuts torque, and replies `4\n` (`STATUS_ERR_ABORTED`).
5. Thread A (which was blocking) receives the `4\n`, realizes the move was aborted, releases the `_response_lock`, and exits cleanly.

`send_stop(repeat=1, gap_s=0.02)` writes the `0xFF` byte `repeat` times with `gap_s` seconds between two bytes (none after the last one), all under `_write_lock` and without waiting for any reply. `repeat=2` is used by the scanner's emergency stop: the second byte catches a move that was queued right behind the aborted one, without first waiting for a blocking `DISABLE`. `repeat < 1` is treated as 1. `stop_epoch` is incremented once per call (not per byte). `Motor.stop(release_torque=False, repeat=1)` forwards `repeat`, marks the motor disabled and, thanks to `stop_epoch`, every other `Motor` on the bridge reports `is_enabled == False` as well.

### `Motor.disable()` result

`Motor.disable()` returns `bool`. If the reply to `DISABLE` is not `0` (error status or timeout `''`), the command is sent **once more**. `True` means the Arduino confirmed it and `is_enabled` is False. After two failures it logs a warning, returns `False` and never raises (the cleanup paths depend on that); `is_enabled` is left as it was, so a driver that was energized keeps being reported as energized, and `Motor.disable_unconfirmed` is set until a later `enable()`, successful `rotate()` or confirmed `disable()`.

---

## 5. Command Bytecodes (`ArduinoCommand`)

The following table maps the Python `ArduinoCommand` Enum values to the exact physical payload transmitted over UART to the Arduino.

| Opcode (Dec) | Python Enum (`ArduinoCommand`) | Payload Format | Expected Response | Description |
| :---: | :--- | :--- | :---: | :--- |
| **`1`** | `PING` | `1\n` | `0\n` | Connection test & liveness handshake. |
| **`2`** | `CONFIG` | `2 <id> <step> <dir> <en>\n` | `0\n` | Configures digital pins for motor `<id>`. |
| **`3`** | `ENABLE` | `3 <id>\n` | `0\n` | Pulls driver EN pin LOW (energizes coils). |
| **`4`** | `DISABLE` | `4 <id>\n` | `0\n` | Pulls driver EN pin HIGH (releases torque). |
| **`5`** | `ROTATE` | `5 <id> <steps> <dir> <us>\n` | `0\n` or `4\n` | Executes physical step pulses (`dir`: 1=CW, 0=CCW). |
| **`6`** | `STOP` | `6 <id>\n` | `0\n` | In-band software stop for motor `<id>`. |
| **`16`** (`0x10`) | `ACCEL_ROTATE` | `16 <id> <steps> <dir> <target_us> <start_us> <accel> <decel> [<end_us>]\n` | `0\n` or `4\n` | Constant-acceleration speed-ramp move (speed is linear in time). Optional `<end_us>` is the step period of the last deceleration step (slow stop); omitted or `0` decelerates back down to `<start_us>`. |
| **`255`** (`0xFF`) | `EMERGENCY_STOP` | Raw `0xFF` byte (no `\n`) | `4\n` (from `ROTATE`) | Out-of-band Universal Abort: halts motion instantly in $\le 1\text{ ms}$. |

**Reserved Future Features:**
* `SET_ACCEL = 0x11` (17)
* `GET_STATUS = 0x20` (32)

---

## 6. Status Return Codes (Standard Error Convention)

All synchronous commands routed through `send_command()` will return exactly one of the following POSIX-style numeric status codes from the C++ firmware:

| Return Code | Python Enum Equivalent | Meaning / Description |
| :---: | :--- | :--- |
| **`0`** | `ArduinoStatus.OK` | **Success / Target Reached / Board Ready.** Command completed successfully. |
| **`1`** | `ArduinoStatus.ERR_UNKNOWN_CMD` | **Unknown Command.** Opcode or command string is unrecognized. |
| **`2`** | `ArduinoStatus.ERR_INVALID_ARG` | **Invalid Arguments.** Missing parameters, non-numeric values, or out-of-range motor ID. |
| **`3`** | `ArduinoStatus.ERR_NOT_CONFIGURED` | **Motor Not Configured.** Motor ID has not been initialized via `CONFIG`. |
| **`4`** | `ArduinoStatus.ERR_ABORTED` | **Aborted Mid-Motion.** Stepping was aborted early by emergency stop (`0xFF`). |
| **`5`** | `ArduinoStatus.ERR_BUSY` | **Subsystem Busy.** Controller is currently busy executing a non-preemptible operation. |

---

## 7. Class API Quick Reference

| Method | Parameters | Return | Description |
| :--- | :--- | :--- | :--- |
| **`get_instance()`** | `port=None`, `baudrate=115200` | `ArduinoBridge` | Thread-safe singleton constructor / accessor. |
| **`send_command()`** | `cmd: str`, `timeout=30.0` | `str` | Sends newline-terminated command under `_response_lock`. |
| **`send_stop()`** | `repeat=1`, `gap_s=0.02` | None | Out-of-band inject of `0xFF` `repeat` times (bypasses `_response_lock`); increments `stop_epoch`. |
| **`close()`** | None | None | Sends `0xFF` to cut torque and closes serial port. |

---

## 8. Quick Python Usage Example

Below is a minimal snippet showing how classes (like `Motor` or test scripts) can seamlessly interact with the Arduino via the Bridge:

```python
from hardware.arduino_bridge import ArduinoBridge, ArduinoCommand, ArduinoStatus

# 1. Retrieve the shared singleton connection
bridge = ArduinoBridge.get_instance()

# 2. Format a command using the Enums
# CONFIG <motor_id=0> <step_pin=6> <dir_pin=7> <en_pin=8>
cmd = f"{ArduinoCommand.CONFIG.value} 0 6 7 8"

# 3. Send and wait for synchronous response
resp = bridge.send_command(cmd)

# 4. Validate execution
if resp == str(ArduinoStatus.OK.value):
    print("Motor 0 configured successfully!")
else:
    print(f"Failed to configure Motor 0. Error Code: {resp}")
```
