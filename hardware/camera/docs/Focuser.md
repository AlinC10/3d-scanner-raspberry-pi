# Focuser Class

The `Focuser` class (`focuser.py`) acts as the low-level hardware driver for the Voice Coil Motor (VCM) built into the Arducam IMX477 B0272 cameras.

## Class Overview

The class interfaces with a DW9714-compatible VCM driver over I2C using `smbus2`. It translates integer focus positions (0 to 1023) into 10-bit DAC registers required by the hardware.

Since the I2C bus of the CSI camera is power-gated by the Raspberry Pi and is only active while the camera is actively streaming, the `Focuser` relies on delayed initialization and state persistence.

## State Persistence

The class saves its last known position to a JSON file in the `config/` directory (e.g., `focus_state_bus10.json`). This ensures that the motor's position logic is maintained across script restarts, preventing sudden mechanical snaps or desyncs when the hardware is powered back on.

## Properties

* **`position`** (int): Retrieves the currently tracked focus position (0 to 1023).

## Core Methods

### `__init__(self, bus: int, addr: int)`
Initializes the VCM controller for a specific I2C bus and address. Loads the previous motor position from the state file. Note that actual I2C initialization is deferred until the hardware is proven to be powered on.

### `set_position(self, pos: int, settle: bool = True)`
Moves the VCM to the absolute position between `VCM_MIN_POS` (0, infinity) and `VCM_MAX_POS` (1023, macro).
* Implements a **smooth ramp** algorithm. Instead of sending a single large jump command which causes physical snapping and mechanical shock, it increments the movement in microsteps of 20 units with a 2ms delay.
* Automatically saves the new position to the state JSON.
* If `settle` is `True`, adds a brief 60ms delay at the end to allow the lens to stop vibrating before a photo is taken.

### `step(self, delta: int)`
Moves the VCM by a relative step. A positive delta moves focus nearer (macro), while a negative delta moves focus further (infinity).

### `reset(self)`
Resets the focus strictly to position 0 (infinity).

### `restore_state(self)`
Forces the hardware to physically move to the `position` tracked in the state file. This is generally called automatically by the camera class just after the camera begins streaming, ensuring the physical lens location matches the software's understanding of it.
