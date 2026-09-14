# DualCamera Class

The `DualCamera` class (`dual_camera.py`) provides an interface to control two Arducam IMX477 cameras simultaneously.

## Class Overview

This class uses a `ThreadPoolExecutor` from the `concurrent.futures` module to dispatch hardware and software commands to both cameras simultaneously. This allows parallel execution of functions such as capturing photos, autofocus sweeps, and updating settings without the latency of sequential execution.

*Note: This multithreading approach is an intermediate software synchronization step until full hardware synchronization via the XVS (Master-Slave) line is configured.*

## Setup & Initialization

### `__init__(self, camera_id_1=0, camera_id_2=1, quality=95, size=None, video_size=None, preview_size=None, rotation_1=0, rotation_2=0)`
Initializes two instances of the `ArducamIMX477` class and creates a thread pool with 2 workers. Both cameras share the resolution and quality settings but can have independent rotations (since the physical bracket may position them differently).

## Concurrent Operations

All primary methods in `ArducamIMX477` have been mapped in the `DualCamera` class to run on both cameras at the same time:

### Core Controls
* **`rotate(self, direction: str) -> List[int]`**: Rotates both cameras. Returns a list of the new rotation angles.
* **`apply_settings(self, **kwargs)`**: Sends simultaneous property updates (exposure, gain, AWB, etc.) to both cameras.
* **`prepare_scan(self, **kwargs) -> List[dict]`**: Prepares both cameras for 3D scanning by moving focus and locking exposure metrics simultaneously. You can pass `focus=(300, 500)` to set independent focus positions for each camera during preparation!

### Focus Controls
* **`focus_set(self, position: int | Tuple[int, int])`**: Actuates the VCMs to a specified absolute target position. Pass an `int` to set both cameras identically, or a tuple like `(300, 500)` to set independent positions concurrently.
* **`focus_step(self, delta: int | Tuple[int, int])`**: Adjusts the focus relative to the current position. Pass an `int` to step both cameras equally, or a tuple like `(10, -20)` to step them independently.
* **`focus_reset(self)`**: Resets both lenses to infinity (position 0).
* **`focus_sweep_autofocus(self, step=30, roi=...) -> List[int]`**: Initiates the OpenCV sharpness sweep on both cameras concurrently. Returns the optimal focus positions for both cameras.

### Capture Methods
* **`capture_photo(self, output_prefix="photo", output_dir=".", meshroom_rig=True, ...) -> List[str]`**
  Instructs both cameras to fire their shutters at the same time. If `meshroom_rig=True` (default), the files are saved into `output_dir/0/` and `output_dir/1/` with identical filenames, which allows Meshroom to automatically link them as a rigid stereo rig. If set to `False`, files are saved to `output_dir` with suffixes indicating the camera ID (e.g., `photo_cam0.jpg`). Returns the list of saved paths.
* **`record_video(self, output_prefix="dual_video", ...) -> List[str]`**
  Starts parallel H.264 video recording on both cameras.

## Lifecycle Management

* **`close(self)`**: Gracefully shuts down the thread pool, waits for pending operations to finish, and closes both `picamera2` instances.
* **Context Management (`__enter__`, `__exit__`)**: Fully supported for safe automatic resource cleanup.
