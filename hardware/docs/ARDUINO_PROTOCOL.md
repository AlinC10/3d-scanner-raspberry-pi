# Arduino Stepper Motor Controller - Architecture & Protocol Specification

This document serves as the comprehensive developer guide for the Arduino Uno stepper motor firmware (`hardware/arduino_control/`). It covers the C++ architecture, hardware-level design decisions, and the complete UART wire protocol used to communicate with the Raspberry Pi.

---

## 1. Overview

The 3D Scanner relies on two NEMA 17 stepper motors (Turntable and Z-Axis) driven by TB6600 driver modules. Driving stepping pulses directly from a Raspberry Pi running Linux introduces mechanical jitter due to the non-real-time nature of OS process scheduling and Python's garbage collection. 

To solve this, the pulse generation is offloaded to a dedicated Arduino Uno running a bare-metal C++ firmware. The Raspberry Pi acts as the high-level brain (handling kinematics, photos, and UI), while the Arduino acts as a strict, real-time slave execution unit.

---

## 2. Compilation and Upload (PlatformIO)

The firmware is managed via PlatformIO, ensuring reproducible builds and easy library management without the standard Arduino IDE.

**To compile the firmware:**
```bash
cd hardware/arduino_control
pio run
```

**To upload the firmware to an attached Arduino Uno:**
```bash
cd hardware/arduino_control
pio run --target upload
```

> **Tip for virtual environments:** If `pio` returns "command not found", ensure PlatformIO is in your system `PATH`, or use the direct binary path (e.g., `~/.platformio/penv/bin/pio run`) or activate its environment first.

---

## 3. Core C++ Architecture & Implemented Classes

The firmware is highly modularized, utilizing three primary internal libraries:

### `Motor` Class (`lib/Motor/`)
Acts as the hardware abstraction layer for the TB6600 stepper drivers.
* **Pin Management**: Encapsulates the `pulsePin`, `dirPin`, and `enPin` for a specific motor instance.
* **Power Management**: Selectively toggles the EN (Enable) pin via `enable()` and `disable()` to apply holding torque only when necessary, preventing driver chips from overheating when idle.
* **Microsecond Execution**: The `rotate()` method is a blocking `for`-loop that manually toggles the STEP pin using microsecond-precise delays to achieve perfectly smooth rotation.

### `CommandHandler` Class (`lib/CommandHandler/`)
The serial orchestrator responsible for reading the UART buffer and routing commands.
* **Non-Blocking Ingestion**: The `update()` loop pulls characters one by one without relying on Arduino's blocking `Serial.readStringUntil()`.
* **Zero-Allocation Parsing**: It fills a fixed-size `char` array and immediately dispatches it via a `switch` statement.
* **Emergency Intercepts**: Detects the `0xFF` emergency stop byte instantly, even mid-stream.

### `Protocol` Definitions (`lib/Protocol/`)
A centralized header file (`Protocol.h`) that formally maps `CommandOpcode` and `StatusCode` Enums, ensuring strict alignment between the C++ firmware and the Python `ArduinoBridge`.

---

## 4. Key Architectural & Embedded Design Decisions

Developing for an 8-bit, 16MHz ATmega328P microcontroller with only **2KB of SRAM** and **32KB of Flash** requires specific embedded engineering patterns.

### Eliminating Heap Fragmentation
The standard Arduino `String` class is strictly prohibited in this codebase. Dynamically concatenating strings and reallocating arrays causes SRAM fragmentation. Over hundreds of motor movements, this fragmentation would inevitably cause a stack collision, resulting in silent board freezes.
Instead, we use a single, static `char inputBuffer[64];` and rely on standard C-style pointer math (`strtol`, `strtoul`) to parse integers natively with zero heap allocations.

### Why Bytecodes & O(1) Command Dispatch
We utilize numeric bytecodes (e.g., `5` for ROTATE) rather than long strings (e.g., `ROTATE_MOTOR`). 
1. **Network Efficiency**: Transmitting `5 1 200 1 2000\n` uses only 15 bytes of the UART buffer.
2. **Parsing Speed**: The `CommandHandler` uses a simple `switch (cmd[0])` statement to dispatch commands in O(1) time. There is zero `strcmp` (string comparison) overhead, saving critical CPU cycles for motor stepping.

### Pass-by-Value vs. Pass-by-Reference
In desktop C++, passing complex types by reference (`const &`) is preferred. However, on 8-bit AVR microcontrollers, passing small primitives by reference is slower and consumes more memory:
* Passing a `uint8_t` (like a pin number) by value places it securely in a single 8-bit CPU register (e.g., `r24`), utilizing **0 bytes of SRAM** and taking 1 clock cycle to access.
* Passing a `const uint8_t&` creates a 16-bit pointer, forcing the compiler to spill the variable onto the SRAM stack (costing RAM) and requiring indirect load instructions (`LD`) which take 2–3 cycles. 
* **Rule**: Primitives $\le 2$ bytes (`uint8_t`, `int8_t`, `bool`, `uint16_t`) are always passed by value in this codebase.

### Hardware Safeguards: Optocouplers & Integer Underflow
1. **Optocoupler Saturation**: The TB6600 driver's internal optocoupler requires a minimum `10µs` HIGH pulse width for reliable edge detection. This `TB6600_REQ_MICROS_DELAY` is hardcoded into the stepper loop.
2. **Unsigned Underflow Protection**: If a requested pulse delay is less than 10µs (e.g., `5µs`), subtracting `10` from `5` on an unsigned 32-bit integer would underflow to `4,294,967,291 µs`, locking the board for ~71 minutes! We use a ternary clamp to protect against this:
   ```cpp
   const uint32_t lowDelay = (delayMicros > TB6600_REQ_MICROS_DELAY) ? (delayMicros - TB6600_REQ_MICROS_DELAY) : 1;
   ```
3. **16-Bit Timer Constraints**: Arduino's native `delayMicroseconds()` on a 16MHz AVR is limited to $\le 16383\,\mu\text{s}$ due to 16-bit timer overflow limits. We created a custom `safeDelayMicroseconds()` helper that seamlessly falls back to `delay(ms)` when exceeding 16ms, allowing ultra-slow rotational profiles without timer corruption.

---

## 5. Serial Configuration & Boot Handshake

* **Baud Rate**: 115200
* **Data Bits**: 8
* **Parity**: None
* **Stop Bits**: 1

When the Raspberry Pi opens the serial port, the Arduino Uno undergoes a DTR hardware reset. The Optiboot bootloader takes approximately 1.5 seconds to run.

Once the firmware boots and `setup()` finishes, it emits the ready banner:
```text
0\n
```
The Python `ArduinoBridge` strictly waits for this `0` before dispatching any instructions, ensuring commands are never swallowed by the bootloader.

---

## 6. Command Protocol & Bytecode Specification

Commands are sent as ASCII strings, separated by spaces, and terminated by a newline `\n`. Both hexadecimal equivalent and decimal integer bytecodes are supported.

| Opcode (Hex) | Opcode (Dec) | ASCII Alias | Payload Format | Expected Response | Description |
| :---: | :---: | :--- | :--- | :---: | :--- |
| `0x01` | `1` | `PING` | `1\n` or `PING\n` | `0\n` | Health check & connection handshake. |
| `0x02` | `2` | `CONFIG` | `2 <id> <step> <dir> <en>\n` | `0\n` or error | Configures `pinMode(OUTPUT)` for motor `<id>`. Motors start disabled. |
| `0x03` | `3` | `ENABLE` | `3 <id>\n` | `0\n` or error | Drives EN pin LOW (energizes coils to apply holding torque). |
| `0x04` | `4` | `DISABLE` | `4 <id>\n` | `0\n` or error | Drives EN pin HIGH (cuts coil current to cool motor & driver). |
| `0x05` | `5` | `ROTATE` | `5 <id> <steps> <dir> <us>\n` | `0\n` or `4\n` | Executes blocking stepping loop. `dir`: 1=CW, 0=CCW. |
| `0x06` | `6` | `STOP` | `6 <id>\n` | `0\n` or error | In-band software stop for specific motor `<id>`. Releases torque. |
| `0x10` | `16` | `ACCEL_ROTATE` | `16 <id> <steps> <dir> <target_us> <start_us> <accel> <decel>\n` | `0\n` or `4\n` | Trapezoidal velocity ramping move. |

---

## 7. Universal Out-of-Band Emergency Stop (`0xFF` / `!`)

Because the `ROTATE` (`5`) command is synchronous and blocking, a long 30-second homing movement will temporarily tie up the Arduino CPU. If a limit switch is triggered on the Pi, standard newline-terminated commands will sit unparsed in the UART buffer until the 30 seconds are over.

To solve this, we use an **Out-of-Band Universal Abort Byte**: `0xFF` (`255`).
> **Human Developer Alias:** For developers debugging via a serial terminal (like PlatformIO Monitor or PuTTY) where typing raw binary `0xFF` is impossible, typing `!` (ASCII 33) provides the exact same universal abort functionality.

* The Raspberry Pi injects `0xFF` onto the serial line *without* a newline. 
* Inside `Motor::rotate()`, between every single microsecond step, the C++ code runs `Serial.peek()`.
* If it detects `0xFF` or `!`, it instantly aborts the stepping loop (latency $\le 1.0\text{ ms}$).
* **Scope Differences:**
  * **Mid-Motion (`Motor::rotate`)**: The active, rotating motor cuts its own power (`this->disable()`), clears the serial buffer, and returns `STATUS_ERR_ABORTED` (`4\n`). (In this rig, only one motor moves at a time, so disabling the active one makes the whole system safe).
  * **Idle (`CommandHandler::update`)**: If the byte is received while idle, the global orchestrator loops over all configured motors and disables them all, cutting total system torque.

---

## 8. Status Return Codes (Standard Error Convention)

Following standard POSIX conventions, `0` indicates absolute success, while non-zero positive integers designate deterministic hardware or protocol failures.

| Return Code | Constant Name | Meaning / Description |
| :---: | :--- | :--- |
| **`0`** | `STATUS_OK` | **Success / Target Reached / Board Ready.** Command completed successfully. |
| **`1`** | `STATUS_ERR_UNKNOWN_CMD` | **Unknown Command.** Opcode or command string is unrecognized. |
| **`2`** | `STATUS_ERR_INVALID_ARG` | **Invalid Arguments.** Missing parameters, non-numeric values, or out-of-range motor ID. |
| **`3`** | `STATUS_ERR_NOT_CONFIGURED` | **Motor Not Configured.** Motor ID has not been initialized via `CONFIG`. |
| **`4`** | `STATUS_ERR_ABORTED` | **Aborted Mid-Motion.** Stepping was aborted early by emergency stop (`0xFF`). |
| **`5`** | `STATUS_ERR_BUSY` | **Subsystem Busy.** Controller is currently busy executing a non-preemptible operation. |

---

## 9. Reserved Opcodes for Future Features

Documented here to ensure future developers have clear allocations that will not collide with the core protocol:

| Opcode (Hex) | Opcode (Dec) | Command / Feature | Payload Format | Description |
| :---: | :---: | :--- | :--- | :--- |
| `0x11` | `17` | `SET_ACCEL` | `17 <id> <accel_rate> <jerk>\n` | Configure acceleration constants. |
| `0x20` | `32` | `GET_STATUS` | `32 <id>\n` | Query motor state and step position. |
