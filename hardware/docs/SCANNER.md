# Scanner Hardware Controller (`../scanner.py`)

The `Scanner` class in `../scanner.py` is the central hardware abstraction layer and real-time physical orchestrator of the 3D scanner rig. 

It manages the lifecycle of the stepper motors, safety limit switches, high-power illumination LEDs, stereoscopic camera feeds, and background cloud uploads. It is engineered specifically for thread-safe, non-blocking operation inside multi-threaded asynchronous servers (such as FastAPI).

---

## 🏗️ Architectural Pillars

```
                     ┌───────────────────────────────────────────────┐
                     │          Scanner Singleton Instance           │
                     └───────┬──────────────┬──────────────┬─────────┘
                             │              │              │
                ┌────────────┴───┐   ┌──────┴─────┐  ┌─────┴────────────┐
                │ Stepper Motors │   │ Endstops   │  │ Dual Cameras     │
                │ Turntable/Z    │   │ Up / Down  │  │ XVS Master/Slave │
                └────────────────┘   └────────────┘  └─────┬────────────┘
                                                           │
                                             ┌─────────────┴─────────────┐
                                             │ Parallel Upload Pipeline  │
                                             │ Queue + Watchdog -> R2    │
                                             └───────────────────────────┘
```

1. **Non-Blocking Lock Scoping**: Mutex locks (`self._lock`) are acquired *only* for state validation and microsecond-level state transitions. Long-running mechanical loops run strictly **outside** the lock, ensuring API requests like `/status` and `/cancel` are processed with zero latency.
2. **Cooperative Thread Cancellation**: Instead of forcefully terminating Python threads, a `threading.Event()` (`self._cancel_event`) is checked at every critical hardware boundary (before each rotational stop, photo trigger, and Z-axis lift).
3. **Producer-Consumer Background Uploads**: Capturing images and uploading to Cloudflare R2 are decoupled through an asynchronous worker queue with automatic retry backoff and a 45-second stalled-progress watchdog.
4. **Hardware Safety Interrupts**: Both limit switches are hardwired at the driver level to `motor.stop()`. If a limit switch triggers, motor movement stops immediately at the hardware interrupt level without waiting for Python interpreter cycles.

---

## 🚦 State Machine (`ScannerState`)

The scanner enforces strict lifecycle state transitions:

```
  IDLE ──────────► PREPARING ──────────► PREPARED ──────────► RUNNING
    ▲                  │                    │                   │
    │                  ▼                    ▼                   ▼
    │              CANCELLED            CANCELLED           PROCESSING
    │                  ▲                    ▲                   │
    │                  │                    │                   ▼
    │               ERROR                ERROR             DOWNLOADING
    │                  ▲                    ▲                   │
    │                  │                    │                   ▼
    └──────────────────┴────────────────────┴────────────── COMPLETED
```

* **`PREPARE_ALLOWED = ("idle", "completed", "cancelled", "error")`**: Allows re-preparing after a finished or aborted run without requiring an application restart.
* **`TERMINAL = ("completed", "cancelled", "error")`**: Preserved by `cleanup()` so API consumers and frontend polling loops can read the final run outcome without having it prematurely overwritten back to `IDLE`.

---

## 🔌 Hardware Subsystems & Allocation

| Subsystem | Class | Pins / Interface | Description |
|---|---|---|---|
| **Turntable Motor** | `Motor` | `DIR=24`, `STEP=23`, `EN=18` | Platter rotation (1/4 microstepping = 800 steps/rev). |
| **Z-Axis Motor** | `Motor` | `DIR=19`, `STEP=26`, `EN=21` | Vertical lead screw elevator (1/16 microstepping = 3200 steps/rev). |
| **Top Endstop** | `Endstop` | `GPIO 2` (Pull-Up) | Prevents carriage from over-traveling into top chassis. |
| **Bottom Endstop** | `Endstop` | `GPIO 3` (Pull-Up) | Home reference position for Z-axis homing. |
| **Illumination** | `Relay` | `GPIO 11` (Active High) | Controls LED lighting strips inside the scan chamber. |
| **Dual Cameras** | `DualCameraXVS` / `DualCamera` | CSI-2 Interfaces (`cam0`, `cam1`) | Dual Arducam IMX477 sensors with hardware frame sync. (See [CAMERA.md](CAMERA.md) for subsystem overview and [DUAL_CAMERAS.md](DUAL_CAMERAS.md) for stereo architecture). |

---

## 🎬 Two-Phase Preparation Architecture

To deliver an immediate live video feed to the frontend while accommodating 15–40 seconds of physical carriage travel, preparation is divided into two distinct phases:

### Phase 1: Immediate Stream Startup (`setup_and_stream`)
* **Execution**: Synchronous (duration: ~1.5s).
* **Actions**:
  1. Validates state in `PREPARE_ALLOWED` and transitions to `PREPARING`.
  2. Powers ON chamber lights (`lights.on()`).
  3. Instantiates `DualCamera` / `DualCameraXVS` with user capture/stream parameters.
  4. Calls `dual_cameras.prepare_scan(..., keep_running=True)` to initialize the sensor pipeline.
  5. Boots the H.264 RTSP live stream encoder and stores stream links in `self.stream_urls`.
  6. **Returns `stream_urls` immediately** to the caller.

### Phase 2: Asynchronous Homing & Auto-Tuning (`finish_preparation`)
* **Execution**: Asynchronous background task (via `FastAPI.BackgroundTasks`).
* **Actions**:
  1. Safely strips out initialization kwargs (`master_id`, `slave_id`, `video_resolution`) to prevent downstream signature errors.
  2. Moves the carriage down to the bottom endstop (`home_z_axis()`).
  3. Checks for cancellation: `if self._cancel_event.is_set(): return`.
  4. Raises carriage by **+30.0 mm** to rest directly in front of the illuminated object.
  5. Cleans previous scan assets from Cloudflare R2 bucket (`r2.delete_all_files_from_bucket()`).
  6. **Re-calls `dual_cameras.prepare_scan(**kwargs, keep_running=True)`**: Because `picam2.started` is already `True`, stream reconfiguration is bypassed. The cameras dynamically settle auto-exposure and auto-white-balance on the illuminated object and freeze them in-place.
  7. Transitions state:
     ```python
     with self._lock:
         if self.state != ScannerState.CANCELLED:
             self.state = ScannerState.PREPARED
     ```

---

## 🔄 Mechanical Scan Loop (`scan`)

Triggered by `POST /scanner/start`, this method handles the physical 3D capture:

1. **State Lock & Upload Thread**:
   - Transitions state to `RUNNING`.
   - Starts daemon worker thread `_upload_worker` pointing at the local `input_images/` directory.
2. **Multi-Level Elevation Loop**:
   - Calculates step count for `z_move_mm` via `distance_to_step_conversion()`.
   - Loops while `not self.up_endstop.is_active` and `not self._cancel_event.is_set()`:
     - **360° Rotational Ring**:
       - Calculates steps per jump (`angle_to_steps_conversion(angle)`).
       - Captures stereoscopic photo pair (`capture_photo()`).
       - Enqueues photo paths into `self.upload_queue`.
       - Increments `self.total_photos += len(photo_paths)`.
       - Rotates turntable by `angle`.
       - Pauses for motor vibration settling (`0.1s`).
     - **Elevation Step**:
       - Climbs UP by `z_move_mm`.
3. **Finalization (`finally`)**:
   - Executes `cleanup()`.
   - Signals `_worker_stop_event` to finish remaining uploads.
   - If cancelled, skips waiting for uploads; otherwise, awaits queue drain.

---

## ☁️ Resilient Upload Queue & Watchdog

To prevent slow Wi-Fi or transient socket disconnects from stalling the scanner or dropping images:

1. **Retry with Exponential Backoff**:
   - Inside `_upload_worker()`, if `r2.upload_file()` fails, the worker sleeps for 5 seconds and retries up to 3 times before recording an error.
2. **Stalled Progress Watchdog**:
   - Inside `stop_upload_worker()`, instead of an arbitrary static timeout, the system tracks `self._last_upload_time`.
   - As long as photos continue to upload, the watchdog stays alive.
   - If 45 continuous seconds elapse with zero successful uploads while items remain in the queue, a network stall is declared, and state transitions to `ERROR`.
3. **Queue Sentinel & Counter Integrity**:
   - An exit event (`_worker_stop_event`) dictates loop shutdown. Every item popped from the queue triggers `upload_queue.task_done()` to prevent queue counter deadlocks.

---

## 🛑 Emergency Stop & Safety

### `emergency_stop()`
Can be called at any time from any thread:
* Sets `self._cancel_event.set()`.
* Calls `turntable_motor.stop(release_torque=True)` and `z_axis_motor.stop(release_torque=True)`.
* Turns OFF relay lights.
* Closes active camera streams and releases hardware video locks.
* Safely sets `self.state = ScannerState.CANCELLED`.

### `cleanup()`
Called during normal teardown or error recovery:
* Cuts torque to stepper motor coils so they do not overheat while standing still.
* Powers off lights.
* Stops camera streams.
* Does **not** overwrite terminal states (`CANCELLED`, `ERROR`, `COMPLETED`).

---

## 📚 Class Method Reference

### Lifecycle & Motion

#### `setup_and_stream(enable_stream: bool = True, bitrate: int = 2_000_000, **kwargs) -> list[str] | None`
Starts camera sensor, boots RTSP stream, and returns stream URLs.

#### `finish_preparation(**kwargs) -> None`
Background worker for homing down, +30mm offset, R2 bucket cleanup, and in-place camera calibration.

#### `home_z_axis() -> None`
Drives carriage downward continuously in 20-step increments until `down_endstop` triggers.

#### `move_z_to_top(delay: float = 0.0010) -> None`
Drives carriage upward continuously until `up_endstop` triggers.

#### `move_z_up_distance(distance: float = 100.0, delay: float = 0.001) -> None`
Moves carriage UP by distance in millimeters. Blocked if top limit switch is active.

#### `move_z_down_distance(distance: float = 100.0, delay: float = 0.001) -> None`
Moves carriage DOWN by distance in millimeters. Blocked if bottom limit switch is active.

#### `scan(angle: float = 18.0, delay_turntable: float = 0.0010, z_move_mm: float = 100.0, delay_z_motor: float = 0.0010) -> None`
Executes full physical scanning and parallel image upload sequence.

#### `generate_livestream(bitrate: int = 2_000_000) -> list[str] | None`
Generates RTSP stream endpoints using active camera array.

#### `stop_stream() -> None`
Terminates RTSP stream and releases camera devices.

#### `emergency_stop() -> None`
Immediately halts all motion, cuts coil power, kills lights, and aborts ongoing tasks.

#### `cleanup() -> None`
Safe power down of motors and lights without clearing terminal states.
