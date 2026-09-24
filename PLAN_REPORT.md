# Implementation Progress Report

## Phase A: Hardware & Scanner Resilience - Completed Steps

### ✅ Step 1: Expand `ScannerState`
- Fixed typo: `"canceled"` → `"cancelled"` for consistency.
- All 9 states are now defined: `IDLE`, `PREPARING`, `PREPARED`, `RUNNING`, `PROCESSING`, `DOWNLOADING`, `COMPLETED`, `ERROR`, `CANCELLED`.

### ✅ Step 2: Update State Transition Guards & Cleanup
- `prepare_scan()` now safely accepts `IDLE`, `COMPLETED`, `CANCELLED`, and `ERROR` as valid starting states without requiring a hard restart.
- `cleanup()` no longer overwrites terminal states (`CANCELLED`, `ERROR`, `COMPLETED`) back to `IDLE`. This ensures that FastAPI and the UI can correctly read the final outcome of a scan.

### ✅ Step 3: Track Photo Count for Heuristics
- Added `self.total_photos = 0` initialized for each scan.
- Because it's a dual-camera setup, the counter increments accurately by `len(photo_paths)` (2 per capture). This ensures the cloud RAM heuristic gets the correct total physical image count fed into Meshroom.

### ✅ Step 4: Cooperative Cancellation & Event Flags
- Added `self._cancel_event = threading.Event()` for cooperative threading.
- `_cancel_event.clear()` is called at the start of `scan()`.
- Added cancellation checkpoints throughout the `scan()` loops: before each photo, before rotating the turntable, and before starting a new Z-level.
- The `finally` block in `scan()` conditionally skips waiting for uploads if the scan was aborted by the user.

### ✅ Step 5: Implement `emergency_stop()`
- Created the `emergency_stop()` method to instantly trigger the active hardware interrupt, valid from any state.
- It sets `self._cancel_event.set()`, instantly interrupts running motor loops using `release_torque=True`, cuts the relay lights, and shuts down camera streams.
- Securely updates the state to `ScannerState.CANCELLED`.

### ✅ Step 6: Fix Upload Queue Deadlocks
- Updated `stop_upload_worker(timeout=120.0)` to poll the queue status rather than indefinitely waiting with `queue.join()`. This establishes a hard timeout constraint to prevent indefinite hanging if a network socket fails.
- Replaced brittle string slicing inside the `_upload_worker` loop by extracting relative paths with `path_obj.relative_to(images_dir)`. This properly handles nesting.

### ✅ Step 7: Pre-Scan Cloud Sanitization
- Invoked `r2.delete_all_files_from_bucket()` inside `prepare_scan()` right after motor homing to guarantee a clean bucket before capturing new assets. If this network call fails, `prepare_scan()` cleanly aborts.

---

## Bug Fixes & Refinements

### ✅ Critical Fix: Lock Scope in `scan()` and `prepare_scan()`
Previously, `with self._lock:` wrapped the entire function body for both `prepare_scan` and `scan`. The lock is now isolated to just the few microseconds covering state validation and transition. The physical hardware loops run **outside** the lock, allowing `emergency_stop()` to instantly intervene from another thread without deadlocking.

### ✅ Minor: `print()` → `logging`
To prepare the system for headless FastAPI deployments:
- `_upload_worker()` uses `log.info()` and `log.error()`.
- The scan abort message uses `log.warning()`.

### ✅ Minor: Extracted class-level tuples
The `_PREPARE_ALLOWED` and `_TERMINAL` tuple definitions are now class-level attributes on `ScannerState` rather than repeatedly instantiated during method execution.

## Phase B (Ongoing): Cloud Architecture & Upload Resilience

- **Implemented Resilient Producer-Consumer Upload Queue:**
  - Upgraded the `_upload_worker` in `hardware/scanner.py` to use a **Retry with Backoff** pattern, ensuring brief Wi-Fi glitches (5-15s) no longer drop captured photos.
  - Replaced the hard 120s static queue timeout with a **Stalled Progress Watchdog** in `stop_upload_worker()`. The orchestrator now tracks the timestamp of the last successful upload, allowing massive scans to complete gracefully over slow connections while still failing fast (in 45s) on permanent network disconnects.
  - Added OS-level socket timeout constraints to `boto3` via `botocore.config.Config` inside `cloud/cloudflare_r2.py` (`connect_timeout=5`, `read_timeout=15`). This prevents Python threads from indefinitely hanging in I/O wait on broken TCP sockets.
