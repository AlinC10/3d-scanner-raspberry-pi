## Goal Description
Establish a robust, asynchronous execution pipeline connecting the local `scanner.py` hardware loop to the cloud processing backend (`RunPod` & `Cloudflare R2`) via FastAPI. This highly detailed, granular plan breaks the execution down into bite-sized, sequential steps.

## Proposed Changes

### Phase A: Hardware & Scanner Resilience
*Files modified: `hardware/scanner.py`*

**Step 1: Expand `ScannerState`**
- Add `PROCESSING`, `DOWNLOADING`, `COMPLETED`, `ERROR`, and `CANCELLED` natively to the `ScannerState` class.

**Step 2: Update State Transition Guards & Cleanup**
- Modify `prepare_scan()` to accept transitions from terminal states: `if self.state not in (ScannerState.IDLE, ScannerState.COMPLETED, ScannerState.CANCELLED, ScannerState.ERROR): raise...`
- Update `cleanup()` to only safely power down hardware and skip overwriting the state if it is currently `CANCELLED`, `ERROR`, or `COMPLETED`.

**Step 3: Track Photo Count for Heuristics**
- Add `self.total_photos = 0` to initialization.
- Because this is a dual-camera rig, each capture triggers 2 physical JPEGs. Ensure `self.total_photos` increments by 2 per trigger (e.g., `self.total_photos += len(photo_paths)`) so the RAM heuristic correctly receives the total physical image count fed into Meshroom.

**Step 4: Initialize Cooperative Cancellation & Event Flags**
- Add `self._cancel_event = threading.Event()` to the scanner's `__init__`.
- Ensure `self._cancel_event.clear()` is called at the beginning of `prepare_scan()` or `scan()`.
- Sprinkle `if self._cancel_event.is_set(): break` at the top of the outer and inner `scan()` loops.

**Step 5: Implement `emergency_stop()`**
- Create the `emergency_stop()` method to instantly trigger the active hardware interrupt, **valid from any state**:
  - Set `self._cancel_event`.
  - Execute `self.turntable_motor.stop(release_torque=True)` and `self.z_axis_motor.stop(release_torque=True)`.
  - Cut the relay lights and shut down camera streams.
  - Set `self.state = ScannerState.CANCELLED`.

**Step 6: Fix Upload Queue Deadlocks**
- Update the `stop_upload_worker(timeout=120.0)` logic to include a hard timeout constraint to prevent indefinite hanging if a network socket fails.
- Safely extract path directories using `Path(file_path).relative_to(...)` rather than brittle string slicing.

**Step 7: Pre-Scan Cloud Sanitization**
- Import `cloud.cloudflare_r2 as r2` and invoke `r2.delete_all_files_from_bucket()` inside `prepare_scan()` to guarantee a clean bucket before capturing new assets. If this fails, abort `prepare_scan` immediately.

---

### Phase B: Cloud Architecture Fixes
*Files modified: `cloud/runpod.py`, `cloud/cloudflare_r2.py`*

**Step 8: Implement Boto3 Timeouts & Absolute Paths**
- In `cloudflare_r2.py`, refactor `INPUT_IMAGES` and other relative paths to use absolute dynamic paths via `Path(__file__).resolve().parent.parent`.
- Configure the `boto3.client` with strict socket timeouts (`connect_timeout=5, read_timeout=15, max_attempts=2`) to prevent indefinite hanging during Wi-Fi drops.
- Add `file_exists_and_is_new(object_name, bucket, after_timestamp)` utilizing `boto3.head_object` to check both existence and the `LastModified` field to prevent false positives from old pipeline outputs.

**Step 9: Fix Cloud Module Import Paths**
- In `runpod.py`, update `import cloudflare_r2 as r2` to `import cloud.cloudflare_r2 as r2`.
- Update `from ram_heuristic` to `from cloud.ram_heuristic`.

**Step 10: Implement RunPod `terminate_pod()`**
- In `runpod.py`, write a new GraphQL request function to call `podTerminate` to avoid lingering disk billing. 
- Replace existing `stop_pod()` calls with `terminate_pod()`.

**Step 11: Inject Cancellation and Polling Timeouts into `launch_job`**
- Update `launch_job` signature: `def launch_job(job: dict, cancel_event=None)`.
- Use the instant-wakeup `wait()` feature in the polling loop: `if cancel_event and cancel_event.wait(timeout=15): terminate_pod(); return False`.
- Implement a hard maximum loop timeout (e.g., 60 minutes) to prevent infinite billing if Meshroom hangs.

**Step 12: Upgrade the Uptime RAM Heuristic**
- Still in `launch_job`, refine the success criteria:
  - If `uptime <= 120`: Assume RAM failure, retry the next GPU.
  - If `uptime > 120`: Ping R2 via `r2.file_exists_and_is_new("output.zip", after_timestamp=job_start_time)`. Return `True` on existence; return `False` if missing or outdated.

---

### Phase C: FastAPI Orchestration
*Files modified: `router/scanning.py`*

**Step 13: Define Singleton Hardware State**
- Initialize a global `scanner = Scanner()` instance to manage hardware throughout the FastAPI application lifespan.

**Step 14: Decouple `/prepare` Endpoint**
- Create `POST /scanning/prepare`. This safely calls `scanner.prepare_scan()`, turning on the lights and live stream for frontend framing without starting the rotation loop.

**Step 15: Create the Orchestrator Pipeline (`/start`)**
- Create `POST /scanning/start`.
- Validates state is `PREPARED`.
- Return `202 Accepted` immediately, delegating the workload to a `fastapi.BackgroundTasks` runner to prevent orphaned threading.
- Background task logic sequentially executes, explicitly updating intermediate states so the UI polling loop is always accurate: 
  1. `scanner.scan()`
  2. `scanner.state = ScannerState.PROCESSING`
  3. `runpod.launch_job(cancel_event)`
  4. `scanner.state = ScannerState.DOWNLOADING`
  5. `r2.download_generated_obj(dest_dir)`
  6. `scanner.state = ScannerState.COMPLETED`
- Wrap the background pipeline in a `try...finally` block. If it fails, securely set `scanner.state = ScannerState.ERROR` (but only if `scanner.state != ScannerState.CANCELLED`).

**Step 16: Create `/cancel` and `/status` Endpoints**
- Create `POST /scanning/cancel` to immediately execute `scanner.emergency_stop()`.
- Update `GET /scanning/status` to transparently return `scanner.state`, allowing the UI to poll and observe `COMPLETED`, `CANCELLED`, or `ERROR` events smoothly.
