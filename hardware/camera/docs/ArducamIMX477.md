# ArducamIMX477 Class

The `ArducamIMX477` class (`camera.py`) provides a high-level interface for the Arducam IMX477 B0272 motorized camera using `picamera2` on a Raspberry Pi 5.

## Class Overview

This class is responsible for managing the image sensor, capturing photos, recording video, and exposing a programmatic interface for hardware and software configurations. It automatically determines the correct I2C bus for the camera's VCM (Voice Coil Motor) based on the provided camera port (CSI 0 -> i2c-10, CSI 1 -> i2c-11).

## Properties

* **`quality`** (int): The default JPEG compression quality (0–100).
* **`size`** (tuple[int, int]): The still-image capture resolution.
* **`video_size`** (tuple[int, int]): The video recording resolution.
* **`preview_size`** (tuple[int, int]): The live preview resolution.
* **`rotation`** (int): The current software rotation angle in degrees (0, 90, 180, 270).

## Core Methods

### `__init__(self, camera_id=0, quality=95, size=None, video_size=None, preview_size=None, rotation=0)`
Initializes the camera instance, validates all resolution/quality configurations, identifies the correct VCM bus, and instantiates the `Focuser` class. 

### `rotate(self, direction: str) -> int`
Rotates the camera image by 90° increments. 
* **`direction`**: Accepts `'left'` (CCW 90°), `'right'` (CW 90°), or `'flip'` (180°).
* **Returns**: The new rotation angle.

### `apply_settings(self, ...)`
Applies image controls (brightness, contrast, saturation, sharpness, analogue gain, exposure time, AWB mode) to the active camera instance. If exposure/gain are manually set, it disables the respective auto-features.

### `lock_auto_features(self, settle_time: float = 2.0) -> dict`
Allows Auto Exposure (AE) and Auto White Balance (AWB) to settle, calculates the best current settings from the metadata, and locks those exact values onto the camera for subsequent shots. Returns the locked configuration dictionary.

### `prepare_scan(self, ...)`
Prepares and locks the camera for a repeatable 3D scanning sequence. This handles moving the focus, locking AE/AWB (or applying manual overrides), and saving the camera state. Highly recommended to use before a loop of photo captures for photogrammetry.

### `capture_photo(self, output=None, resolution=None, quality=None, raw=False, show_preview=False, preview_duration=2.0) -> str`
Captures a JPEG still (and optionally a DNG raw file if `raw=True`). Handles any required post-capture processing (like software rotation and resizing via OpenCV) to ensure images perfectly match the requested configuration.
* **Returns**: Path to the saved JPEG file.

### `record_video(self, output=None, duration=10.0, resolution=None, quality=25) -> str`
Records an H.264 video stream wrapped in an MP4 container.

## Focus Control Methods

* **`focus_set(self, position: int)`**: Set absolute focus position (0 to 1023).
* **`focus_step(self, delta: int)`**: Step focus incrementally by a delta (positive=near, negative=far).
* **`focus_reset(self)`**: Resets focus to the furthest distance (infinity, position 0).
* **`focus_sweep_autofocus(self, step=30, roi=...) -> int`**: Uses OpenCV to sweep the VCM hardware across its full range. Captures frames to calculate a Laplacian variance sharpness score within the center ROI, and returns/sets the motor to the sharpest position found.

## Interactive Tools

### `interactive_preview(self, resolution=None)`
Opens a live OpenCV preview window allowing the user to view the camera feed and interactively step focus using keyboard keys (`w/s`, `e/d`, `0-9`), test the software auto-focus (`a`), or capture a test JPEG (`c`).

## Lifecycle Management

### `close(self)`
Safely stops the `picamera2` instance and closes the I2C bus connection to the VCM driver.

### Context Management (`__enter__`, `__exit__`)
The class supports standard context manager blocks (the `with` statement) to ensure `close()` is automatically called when exiting the block.

## Internal Helpers

The class also defines several private helper methods for internal state management:
* **Validators:** `_validate_quality`, `_validate_size`, `_validate_rotation` to enforce types and bounds.
* **Picamera Configs:** `_configure_still`, `_configure_video`, `_configure_preview` to setup `picamera2` internal configurations before starting operations.
* **Image Processing:** `_process_captured_image` applies single-pass OpenCV resizing and rotation; `_sharpness` calculates the Laplacian variance for autofocus.
* **CLI/Preview Utilities:** `_interactive_preview_linebuf` (headless fallback for preview), `_print_info`, `_print_status`, `_print_preview_help`.
