# Hardware Scanner Documentation (`scanner.py`)

The `scanner.py` module serves as the primary hardware abstraction layer and orchestrator for the physical 3D scanner rig. It manages the lifecycle of the motors, limit switches, relay lights, and dual-camera setup, while integrating tightly with background cloud uploading.

---

## 🏗 System Architecture & Concurrency

The `Scanner` class is designed to run within an asynchronous or multi-threaded environment (e.g., FastAPI). It employs several mechanisms to ensure thread safety, prevent hardware damage, and prevent network blockages.

### 1. Thread-Safe State Machine
The scanner enforces a strict state machine (`ScannerState`) to prevent conflicting hardware operations (e.g., trying to scan while homing).
- **Valid States**: `IDLE`, `PREPARING`, `PREPARED`, `RUNNING`, `PROCESSING`, `DOWNLOADING`, `COMPLETED`, `ERROR`, `CANCELLED`
- **Locking**: A threading lock (`self._lock`) is used *briefly* during state transitions. It is intentionally **released** before long-running physical tasks (like motor loops) begin, ensuring that endpoints like `/status` or `/cancel` are never blocked.
- **Terminal States**: The `cleanup()` routine specifically avoids overwriting terminal states (`COMPLETED`, `CANCELLED`, `ERROR`) back to `IDLE` so that consumers (like UI polling loops) can observe the final outcome.

### 2. Cooperative Cancellation (`emergency_stop`)
Because hardware is involved (stepper motors, live camera feeds, relays), forceful thread killing is dangerous. 
- The system uses `self._cancel_event = threading.Event()`.
- The main `scan()` loop checks `.is_set()` before every mechanical movement and camera capture. 
- When `emergency_stop()` is called, it triggers the event, immediately cuts power to the motor coils (`release_torque=True`), shuts off the lights, and closes the camera streams safely.

### 3. Background Upload Worker
Uploading high-resolution images sequentially during the mechanical scan would bottleneck the turntable.
- **Queueing**: As images are captured, they are pushed into a thread-safe `Queue`.
- **Worker Thread**: A daemon thread (`self._upload_thread`) pulls from the queue and streams assets to Cloudflare R2 simultaneously while the hardware continues capturing the next angle.
- **Resiliency (Retry + Watchdog)**: The worker features transient fault tolerance.
  - **Retry with Backoff**: If a network hiccup occurs, the worker retries the upload (up to 3 times) before giving up, ensuring no photos are dropped during brief Wi-Fi glitches. 
  - **Stalled Progress Watchdog**: Instead of a static timeout, `stop_upload_worker()` tracks `self._last_upload_time`. As long as photos successfully upload, the timer resets. It only aborts (and fails the scan) if 45 continuous seconds pass with zero successful network activity.

---

## ⚙️ Hardware Components

### 🔄 Turntable Motor
- **Type**: Stepper Motor via `RpiMotorLib`
- **Role**: Rotates the physical object precisely using angle-to-step conversions.
- **Pins**: `DIR=24`, `STEP=23`

### 🏗 Z-Axis Motor
- **Type**: Stepper Motor via `RpiMotorLib`
- **Role**: Elevates the camera array along the T8 threaded rod (`8mm` pitch).
- **Pins**: `DIR=19`, `STEP=26`

### 🛑 Limit Switches (Endstops)
- **Role**: Hardware interrupts for the Z-axis.
- **`up_endstop`**: Prevents the rig from crashing into the ceiling. Breaking this limit cleanly finishes a multi-level scan.
- **`down_endstop`**: Used exclusively during `prepare_scan()` to "home" the cameras to the bottom floor.
- **Interrupt Binding**: Limit switches are bound directly to `motor.stop()`. If triggered mid-movement, the motor immediately halts without waiting for the Python loop.

### 💡 Relay (Lighting)
- **Role**: High-power LEDs for consistent photogrammetry illumination.
- **Pin**: `11` (Active High)

### 📸 Dual Cameras
- The system defaults to `DualCameraXVS` (Cross-View Sync) allowing synchronous stereoscopic captures.
- **RAM Heuristic**: During a scan, `self.total_photos` tracks the exact number of physical JPEGs produced (2 per capture trigger). This metric dictates the RunPod RAM allocation threshold later in the pipeline.

---

## 🚀 Execution Flow

### 1. `prepare_scan()`
Preps the rig for the frontend UI.
- Validates the current state is `_PREPARE_ALLOWED` (`IDLE`, `COMPLETED`, `CANCELLED`, `ERROR`).
- Turns on the lights.
- Drives the Z-axis down until `down_endstop` is physically triggered.
- Erases the S3 bucket to prevent leftover artifacts from previous scans.
- Boots the cameras and generates an active live stream URL for frontend framing.

### 2. `scan()`
Executes the mechanical digitizing loop.
- Spins up the background `_upload_worker`.
- Captures an image slice (360° rotation based on `angle`).
- Moves the Z-axis UP by `z_move_mm`.
- Repeats until `up_endstop` is hit, or the scan is cancelled.
- **Finally block**: Drains the upload queue and powers down components.

### 3. `cleanup()`
- Re-secures the system state, turns off lights, and releases holding torque on stepper coils so they don't overheat while idle.
