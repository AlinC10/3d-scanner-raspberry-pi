# Focuser Class

The `Focuser` class (`focuser.py`) acts as the low-level hardware driver for the Voice Coil Motor (VCM) built into the Arducam IMX477 B0272 cameras.

## Class Overview

The class interfaces with a DW9714-compatible VCM driver over I2C using `smbus2`. It translates integer focus positions (0 to 1023) into 10-bit DAC registers required by the hardware.

Since the I2C bus of the CSI camera is power-gated by the Raspberry Pi and is only active while the camera is actively streaming, the `Focuser` relies on delayed initialization and state persistence.

## State Persistence

The class saves its last known position to a JSON file in the `config/` directory (e.g., `focus_state_bus10.json`). This ensures that the motor's position logic is maintained across script restarts, preventing sudden mechanical snaps or desyncs when the hardware is powered back on.

## Properties

* **`position`** (int): Retrieves the last committed focus position (0 to 1023).
* **`hw_valid`** (bool): `True` only while the VCM is known to physically be at `position`. It is `False` after process start, after `invalidate()` and after a failed write.

## Errors

* **`FocuserUnavailable(RuntimeError)`**: smbus2 missing or the I2C bus cannot be opened (previously `sys.exit()`).
* **`FocuserWriteError(FocuserUnavailable)`**: an I2C write failed (after one retry). The position and the state file are not updated.

## Core Methods

### `__init__(self, bus: int, addr: int)`
Initializes the VCM controller for a specific I2C bus and address. Loads the previous motor position from the state file. Note that actual I2C initialization is deferred until the hardware is proven to be powered on.

### `set_position(self, pos: int, settle: bool = True)`
Moves the VCM to the absolute position between `VCM_MIN_POS` (0, infinity) and `VCM_MAX_POS` (1023, macro).
* Implements a **smooth ramp** algorithm. Instead of sending a single large jump command which causes physical snapping and mechanical shock, it increments the movement in microsteps of 20 units with a 2ms delay.
* The first call after a camera start (`hw_valid` False) is always written to the hardware (ramping from 0, the power-on position) even if the cached position equals the target.
* The position and the state JSON are committed only after the final write succeeded; a failed write is retried once and then raises `FocuserWriteError`.
* If `settle` is `True`, adds a brief 60ms delay at the end to allow the lens to stop vibrating before a photo is taken.

### `step(self, delta: int)`
Moves the VCM by a relative step. A positive delta moves focus nearer (macro), while a negative delta moves focus further (infinity).

### `reset(self)`
Resets the focus strictly to position 0 (infinity).

### `restore_state(self) -> bool`
Forces the hardware to physically move to the `position` tracked in the state file when the hardware state is unknown (`hw_valid` False). Returns `False` (and logs) instead of raising when the write fails.

### `invalidate(self)` (alias `mark_unpowered`)
Marks the VCM as unpowered/unknown. The camera classes call it whenever a camera stops; the next `set_position()` is forced and re-runs the VCM power-on write.

### `close(self)`
Closes the I2C bus. Idempotent.
