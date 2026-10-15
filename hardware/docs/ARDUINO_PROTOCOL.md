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
* **Power Management**: Selectively toggles the EN (Enable) pin via `enable()` and `disable()` to apply holding torque only when necessary, preventing driver chips from overheating when idle. EN is active LOW (LOW = driver energized, HIGH = driver off). `pinSetup()` latches EN HIGH *before* switching the pin to `OUTPUT`, so a configured motor is de-energized from the moment `CONFIG` returns, and `disable()` always writes the pin (it never trusts the cached `enableStatus`), so `DISABLE` and `0xFF` are never no-ops.
* **Microsecond Execution**: `rotate()` and `rotateRamp()` are blocking loops that emit STEP pulses against an absolute `micros()` deadline (`stepAt()` / `waitUntil()`), polling for `0xFF` the whole time they wait. The step period floor is 60 us.

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
1. **Optocoupler Saturation**: The TB6600 driver's internal optocoupler requires a minimum HIGH pulse width for reliable edge detection. The firmware holds STEP HIGH for `50µs` (`delayMicroseconds(50)` in `stepAt()`).
2. **Period floor / unsigned underflow**: The step period is never computed by subtracting the pulse width from the requested delay. `stepAt()` simply schedules the next step `max(period, 60µs)` after the previous one, so a tiny or zero requested delay cannot underflow an unsigned 32-bit value.
3. **No long blocking delays**: Steps are scheduled with absolute `micros()` deadlines and a polling busy-wait (wrap-safe signed comparison), so periods above 16ms (the `delayMicroseconds()` limit on a 16MHz AVR) need no `delay(ms)` fallback and `0xFF` is still seen within ~1ms.

---

## 5. Serial Configuration & Boot Handshake

* **Baud Rate**: 115200
* **Data Bits**: 8
* **Parity**: None
* **Stop Bits**: 1

When the Raspberry Pi opens the serial port, the Arduino Uno undergoes a DTR hardware reset. The Optiboot bootloader takes approximately 1.5 seconds to run.

Once the firmware boots and `setup()` finishes, it emits the ready banner:
```text
READY\n
```
The Python `ArduinoBridge` waits for this line (older firmware printed `0\n`; both are accepted) before dispatching any instructions, ensuring commands are never swallowed by the bootloader. A `READY` line in the middle of a command means the board rebooted and the command was lost.

---

## 6. Command Protocol & Bytecode Specification

Commands are sent as ASCII strings, separated by spaces, and terminated by a newline `\n`. Both hexadecimal equivalent and decimal integer bytecodes are supported.

| Opcode (Hex) | Opcode (Dec) | ASCII Alias | Payload Format | Expected Response | Description |
| :---: | :---: | :--- | :--- | :---: | :--- |
| `0x01` | `1` | `PING` | `1\n` or `PING\n` | `0\n` | Health check & connection handshake. |
| `0x02` | `2` | `CONFIG` | `2 <id> <step> <dir> <en>\n` | `0\n` or error | Configures `pinMode(OUTPUT)` for motor `<id>`. EN is driven HIGH (driver off) before it becomes an output, so the motor is released as soon as `CONFIG` returns (see 6.3). |
| `0x03` | `3` | `ENABLE` | `3 <id>\n` | `0\n` or error | Drives EN pin LOW (energizes coils to apply holding torque). |
| `0x04` | `4` | `DISABLE` | `4 <id>\n` | `0\n` or error | Drives EN pin HIGH (cuts coil current to cool motor & driver). Always writes the pin, also if the motor is already disabled; idempotent. |
| `0x05` | `5` | `ROTATE` | `5 <id> <steps> <dir> <us>\n` | `0\n` or `4\n` | Executes blocking stepping loop. `dir`: 1=CW, 0=CCW. |
| `0x06` | `6` | `STOP` | `6 <id>\n` | `0\n` or error | In-band software stop for specific motor `<id>`. Releases torque. |
| `0x10` | `16` | `ACCEL_ROTATE` | `16 <id> <steps> <dir> <target_us> <start_us> <accel> <decel> [<end_us>]\n` | `0\n` or `4\n` | Constant-acceleration speed-ramp move. Optional `<end_us>` = slow-stop tail (see 6.1). |

### 6.1 `ACCEL_ROTATE` speed profile

| Argument | Meaning |
| :--- | :--- |
| `<target_us>` | Cruise step period. |
| `<start_us>` | Step period at the beginning of the acceleration ramp (clamped to `>= target_us`). |
| `<accel>` / `<decel>` | Number of steps spent accelerating / decelerating (scaled down proportionally if `accel + decel > steps`). |
| `<end_us>` *(optional)* | Step period of the **last** deceleration step. `0` or missing = `<start_us>` (previous behaviour). Clamped to `>= target_us`. |

* Speed is a straight ramp in time (constant acceleration): for step `i` of an `n`-step ramp, `v^2 = v_a^2 + (v_b^2 - v_a^2) * i / n`, with `v = 1e6 / period_us`.
  * Acceleration: `v_a = 1/start_us` -> `v_b = 1/target_us`.
  * Deceleration: `v_a = 1/target_us` -> `v_b = 1/end_us`.
* Steps are scheduled against an absolute `micros()` deadline, so the cost of computing the next period is hidden inside the wait and does not stretch the ramp.
* Each ramp step costs a `sqrtf` plus a float divide (roughly 200-400 us on the ATmega328P, an estimate that has not been measured). Below a ~400 us step period this can exceed the period and make ramp steps run late; the firmware does not guard against it. The Python side enforces `STEP_DELAY_MIN = 0.0001 s`, and scans should use >= 0.0005 s.
* The firmware waits for the last step period to elapse before replying, so the rotor has settled on the final step when the Pi receives `0\n`.
* Old firmware ignores the extra argument and old hosts simply omit it, so the two sides can be upgraded independently.

### 6.2 Boot banner

After a reset the firmware prints `READY\n` once (older firmware printed `0\n`; the host handshake accepts both). It is deliberately not a numeric status, so the host can tell a reboot in the middle of a command from the `0` (OK) answer of that command. While no command is pending the host simply discards it.

### 6.3 EN pin boot state

* The EN pin is active LOW: LOW = driver energized (holding torque), HIGH = driver off (shaft free).
* After `CONFIG` the EN pin is HIGH and `isEnabled()` is `false`; the motor is free. Before this fix the pin came up LOW (AVR `pinMode(OUTPUT)` drives the PORT register, which is 0 after reset) while the firmware believed it was disabled, so `DISABLE` and `0xFF` were no-ops until the first `ENABLE` or move. **Re-flash is required** for the fix to take effect.
* `ROTATE` / `ACCEL_ROTATE` enable the motor automatically (EN LOW); `DISABLE`, `STOP` and `0xFF` drive EN HIGH again.

---

## 7. Universal Out-of-Band Emergency Stop (`0xFF` / `!`)

Because the `ROTATE` (`5`) command is synchronous and blocking, a long 30-second homing movement will temporarily tie up the Arduino CPU. If a limit switch is triggered on the Pi, standard newline-terminated commands will sit unparsed in the UART buffer until the 30 seconds are over.

To solve this, we use an **Out-of-Band Universal Abort Byte**: `0xFF` (`255`).
> **Human Developer Alias:** For developers debugging via a serial terminal (like PlatformIO Monitor or PuTTY) where typing raw binary `0xFF` is impossible, typing `!` (ASCII 33) provides the exact same universal abort functionality.

* The Raspberry Pi injects `0xFF` onto the serial line *without* a newline. 
* Inside `Motor::rotate()` and `Motor::rotateRamp()` the C++ code runs `Serial.peek()` continuously, both before each step and while waiting for the next step deadline.
* If it detects `0xFF` or `!`, it instantly aborts the stepping loop (latency $\le 1.0\text{ ms}$), even with step periods up to 30 ms (slow tail, slow constant-speed Z moves).
* **Scope Differences:**
  * **Mid-Motion (`Motor::rotate`)**: The active, rotating motor cuts its own power (`this->disable()`), clears the serial buffer, and returns `STATUS_ERR_ABORTED` (`4\n`). (In this rig, only one motor moves at a time, so disabling the active one makes the whole system safe).
  * **Idle (`CommandHandler::update`)**: If the byte is received while idle, the global orchestrator loops over all configured motors and drives EN HIGH on every one of them (unconditionally, independent of the cached enable state), cutting total system torque. No reply is sent for a `0xFF` received while idle (the protocol is unchanged).

---

## 7.1 Host-side test

`hardware/arduino_control/test/host/` contains a PlatformIO-independent C++ test that compiles `Motor.cpp` and `CommandHandler.cpp` against a fake `Arduino.h` (simulated pins, clock and serial):

```bash
cd hardware/arduino_control/test/host
./run.sh
```

It checks the EN boot state, `DISABLE`/`0xFF` behaviour at idle and mid-move, prompt abort inside a 30 ms step wait, immediate first pulse and that `<end_us>` reaches the last step period. It cannot validate real CPU timing (e.g. the `sqrtf` cost at very short periods); that needs a hardware test.

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
