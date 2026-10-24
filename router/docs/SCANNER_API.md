# Scanner Core API Documentation (`/scanner`)

The Scanner router (`../scanner.py`) orchestrates the end-to-end photogrammetry pipeline: camera stream initialization, mechanical positioning, 360° capture synchronization, photo upload to Cloudflare R2 with verification, and (optionally, per request) cloud GPU 3D reconstruction and mesh retrieval.

Interactive OpenAPI documentation is accessible at: `http://127.0.0.1:8000/docs#/Scanner`

> **Local access only.** The server listens on `127.0.0.1`: the API and the web page are reachable from the Raspberry Pi itself, not from the network (see the README for the launch commands). Every example uses `http://127.0.0.1:8000`.

---

## State Machine Architecture

The scanner operates as a strictly validated finite state machine (`ScannerState`):

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> PREPARING: POST /scanner/prepare
    COMPLETED --> PREPARING: POST /scanner/prepare
    CANCELLED --> PREPARING: POST /scanner/prepare
    ERROR --> PREPARING: POST /scanner/prepare

    state PREPARING {
        [*] --> SetupStream: Lights ON & Cameras Start
        SetupStream --> Homing: Carriage moves to bottom endstop
        Homing --> Positioning: Carriage rises z_start_mm (default 30mm) facing object
        Positioning --> CleanR2: Purge old files from R2 bucket
        CleanR2 --> LockFeatures: In-place AE/AWB & Focus calibration
        LockFeatures --> [*]
    }

    PREPARING --> PREPARED: Background task completes
    PREPARED --> RUNNING: POST /scanner/start

    state RUNNING {
        [*] --> CaptureSlice: Turntable 18° jumps & photo capture
        CaptureSlice --> ClimbZ: Move up z_move_mm
        ClimbZ --> CaptureSlice: Repeat until top limit switch
        CaptureSlice --> FinalizeUpload: Drain uploads, verify them in R2
        FinalizeUpload --> [*]
    }

    RUNNING --> COMPLETED: Photos verified in R2 (cloud.launch_runpod = false, default)
    RUNNING --> PROCESSING: Photos verified in R2 (cloud.launch_runpod = true)
    PROCESSING --> DOWNLOADING: RunPod photogrammetry completes
    DOWNLOADING --> COMPLETED: 3D output.zip extracted

    PREPARING --> CANCELLED: POST /scanner/cancel
    PREPARED --> CANCELLED: POST /scanner/cancel
    RUNNING --> CANCELLED: POST /scanner/cancel
    PROCESSING --> CANCELLED: POST /scanner/cancel
    DOWNLOADING --> CANCELLED: POST /scanner/cancel

    PREPARING --> ERROR: Mechanical / Cloud failure
    RUNNING --> ERROR: Photos missing in R2 / Motor error
    PROCESSING --> ERROR: RunPod OOM / timeout
    DOWNLOADING --> ERROR: R2 download failure
```

---

## Endpoint Reference

| Method | Path | Status Code | Description |
|---|---|---|---|
| `POST` | `/scanner/prepare` | `202 Accepted` | Starts live stream immediately, homes carriage, and auto-tunes cameras in background |
| `POST` | `/scanner/start` | `202 Accepted` | Launches the physical scan and the verified upload to R2; the RunPod pipeline only when `cloud.launch_runpod` is `true` |
| `POST` | `/scanner/cancel` | `200 OK` | Emergency stop: cuts motor power, kills lights, closes streams, terminates cloud pods |
| `GET` | `/scanner/status` | `200 OK` | Polls current state, phase, photo counters (captured / uploaded / missing), failure reason, and active stream URLs |
| `GET` | `/scanner/stream/mjpeg` | `200 OK` | Direct multipart MJPEG stream for browser `<img>` tags (`?camera=1\|2`) with on-demand encoding |
| `POST` | `/scanner/stream/active` | `200 OK` | Instantaneous zero-latency active preview camera switch (`?camera_id=1\|2`) |
| `POST` | `/scanner/stream/quality` | `200 OK` | Dynamically updates live stream JPEG compression quality (`1..100`, default 80) |
| `GET` | `/scanner/stream/sync-status` | `200 OK` | Returns real-time libcamera `rpi.sync` status, frame rates, and offset telemetry |

---

## 1. Prepare Scan (`POST /scanner/prepare`)

Prepares the scanner for framing and calibration. 

### Behavior
1. **Immediate Execution (~1.5s)**:
   - Validates that state is in `[IDLE, COMPLETED, CANCELLED, ERROR]`.
   - Transitions state to `PREPARING`.
   - Powers ON the chamber lights.
   - Runs the preflight (Cloudflare R2 read/write/delete, ToF sensor, free disk space); a failure is a `400` and nothing is touched.
   - Initializes the dual cameras and starts the MJPEG live preview (there is no video encoder and no bitrate).
   - **Immediately returns HTTP 202** with the MJPEG preview URLs (`/scanner/stream/mjpeg?camera=1|2`), usable directly as `<img src>`.
2. **Background Execution**:
   - Drives the carriage down until the bottom limit switch triggers (`home_z_axis`).
   - Climbs `z_start_mm` (default **30mm**) above home to rest directly in front of the illuminated turntable object (`0` skips the climb).
   - Cleans up old assets in the Cloudflare R2 bucket.
   - Re-runs camera auto-tuning (`prepare_scan`) in position: settles auto-exposure and auto-white-balance on the illuminated object, then locks them.
   - Transitions state to `PREPARED`.

### Request Body (`PrepareRequest`)
Every field is optional. A client may send only the basic fields (photo quality / resolution) and leave the advanced ones out: whatever is missing takes the default declared in the schema (`camera_config` itself may be omitted too). A front-end that prefers to send all values may send the defaults below.
```json
{
  "enable_stream": true,
  "illumination_brightness": 1.0,
  "z_start_mm": 30.0,
  "camera_config": {
    "quality": 95,
    "photo_resolution": [4056, 3040],
    "focus": "auto",
    "copy_colour_gains": false
  }
}
```

| Field | Default | Rules |
|---|---|---|
| `enable_stream` | `true` | Start the MJPEG live preview. |
| `illumination_brightness` | `1.0` | **LED** brightness (PWM), `0 < v <= 1` (0 is refused). Not the image brightness below. |
| `z_start_mm` | `30.0` | Scan start height above the bottom endstop in mm, `0 <= v <= 100`, a whole number of motor steps (multiples of `0.01`; otherwise 422). `0` stays on the endstop. Not part of `camera_config`. |
| `camera_config.quality` | `95` | JPEG quality of the photos, 10-100. |
| `camera_config.photo_resolution` | `[4056, 3040]` | `[width, height]`: exactly `[4056, 3040]` (full sensor) or `[2028, 1520]` (2x2 binned, same field of view); any other value is a 422. |
| `camera_config.focus` | `"auto"` | `"auto"`: sharpness sweep on the lit object, best position per lens, locked for the scan. An integer: both lenses at that position (a value below 0 becomes 0, above 1023 becomes 1023). `[cam1, cam2]`: exactly two integers, clamped the same way (another length is a 422). `null` (or `[null, null]`): the last focus positions saved on the Pi. |
| `camera_config.preview_resolution` | `[960, 720]` | Live preview size, even numbers, not larger than the photo. |
| `camera_config.rotation` | `0` | `0/90/180/270`, or a `[cam1, cam2]` pair. |
| `camera_config.settle_time` | `2.0` | Seconds AE/AWB settle before they are locked. |
| `camera_config.exposure_time`, `analogue_gain`, `colour_gains`, `awb_mode` | `null`, `null`, `null`, `"auto"` | Manual exposure (µs, `> 0`), gain (`1-16`), `[red, blue]` white-balance gains. Left out = automatic, then locked for the whole scan. |
| `camera_config.copy_colour_gains` | `false` | When AE/AWB are locked, `false`: each camera keeps the white-balance gains of its own AWB (exposure time and gain are still copied from camera 1). `true`: camera 1's gains are copied to camera 2, which gives it a colour cast when the sensors differ. Default `rpi_sync` mode only. |
| `camera_config.brightness`, `contrast`, `saturation`, `sharpness` | `null` | **Image** adjustments. |
| `camera_config.master_id`, `slave_id` | `0`, `1` | Camera ids. |

Unknown keys inside `camera_config` are ignored (older clients keep working).

### Response (`202 Accepted` - `PrepareResponse`)
```json
{
  "status": "accepted",
  "message": "Live stream started. Rig homing and camera tuning in progress.",
  "state": "preparing",
  "stream_urls": [
    "/scanner/stream/mjpeg?camera=1",
    "/scanner/stream/mjpeg?camera=2"
  ]
}
```

### Error Responses
* `400 Bad Request`: If scanner is currently in an invalid state (e.g. `RUNNING` or `PROCESSING`), or the scanner refuses to prepare with a message (preflight refusal). The state is checked **before** anything is touched, so a refused `/prepare` does not change the LED brightness.
* `409 Conflict`: A temporary refusal: an emergency stop is still tearing the previous run down, or another motion (manual `/test` move, homing) is in progress. Retry shortly.
* `422 Unprocessable Entity`: Invalid body, e.g. `illumination_brightness` is `0` (the range is `0 < b <= 1.0`: the scanner cannot scan in the dark) or a request key is unknown.
* `500 Internal Server Error`: Hardware initialization failure.

### Example `curl`
```bash
curl -X POST http://localhost:8000/scanner/prepare \
     -H "Content-Type: application/json" \
     -d '{
       "enable_stream": true,
       "illumination_brightness": 1.0,
       "camera_config": {
         "master_id": 0,
         "slave_id": 1
       }
     }'
```

---

## 2. Start Scan (`POST /scanner/start`)

Starts the automated scanning workflow: physical scan, photo upload to R2 and verification of the uploaded set. The cloud 3D generation (RunPod) is **optional** and off by default (`cloud.launch_runpod`).

### Pre-conditions
* Scanner state **must** be `PREPARED` (obtained by polling `GET /scanner/status` after calling `/prepare`).

### Behavior
1. Validates `scanner.state == ScannerState.PREPARED`.
2. Returns HTTP `202 Accepted` immediately.
3. Dispatches the multi-stage background orchestrator:
   * **Stage 1 (Physical Scan & Upload - `RUNNING`)**:
     * Rotates turntable in incremental jumps (`angle`, default `18.0°` = 20 stops per 360° ring) using smooth trapezoidal velocity ramping (default 20% ramp) to eliminate inertia slip.
     * Synchronously captures photos from both cameras (2 photos per stop).
     * Pushes captured images into a background upload queue (uploads stream to Cloudflare R2 in parallel).
     * Climbs Z-axis by `z_move_mm` (default `100.0mm`) per elevation ring until the top limit switch triggers.
     * An upload worker retries failed uploads with backoff; when the scan ends it waits for the uploads to drain (30 minutes without a single success is the limit) and the bucket is verified against the manifest.
     * When the scan ends, the uploads are verified against the list of expected keys in the R2 bucket. Missing photos end the run in `error` (see below) and stay on disk.
   * **Stage 1b (Completion without RunPod - `COMPLETED`)** (`cloud.launch_runpod = false`, the default):
     * The state becomes `COMPLETED` as soon as the photos are verified in R2. `cloud.runpod` is not imported or called, so a missing RunPod key or package does not matter. Download the photos from the bucket for Meshroom.
   * **Stage 2 (Cloud Meshroom Photogrammetry - `PROCESSING`)** (only when `cloud.launch_runpod = true`):
     * Transitions state to `PROCESSING`.
     * Launches a cloud GPU pod on RunPod (e.g., RTX 4090 / RTX 5090).
     * Runs Meshroom pipeline: Feature Extraction (SIFT), Structure-from-Motion (SfM), DepthMap, Meshing, Texturing.
     * Automatically handles out-of-memory GPU fallbacks.
   * **Stage 3 (Asset Download - `DOWNLOADING`)** (only with RunPod):
     * Transitions state to `DOWNLOADING`.
     * Downloads `output.zip` containing the completed `.obj`, `.mtl`, and texture files from Cloudflare R2.
     * Extracts files to the local output directory.
   * **Stage 4 (Completion - `COMPLETED`)** (only with RunPod):
     * Transitions state to `COMPLETED`.

> A `CANCELLED` state is never overwritten: a cancel that arrives while the pipeline is finishing keeps the run `cancelled`.

### Request Body (`StartRequest`)
```json
{
  "job_id": "scan-vase-001",
  "mechanical": {
    "turntable": {
      "angle": 18.0,
      "delay": 0.0010,
      "acceleration": true,
      "ramp_percent": 0.2,
      "decel_percent": null,
      "start_delay": null,
      "end_delay": 0.015
    },
    "z_axis": {
      "distance_mm": 100.0,
      "delay": 0.0010,
      "acceleration": false,
      "ramp_percent": 0.2,
      "decel_percent": null,
      "start_delay": null,
      "end_delay": null
    }
  },
  "cloud": {
    "launch_runpod": false,
    "resolution_mp": 12.0,
    "mode": "rig",
    "depthmap_downscale": 2,
    "max_input_points": 10000000
  }
}
```

#### Mechanical & Motor Configuration Parameters

> A scan always rotates the platter in one fixed direction and only climbs, so the request has no `turntable.clockwise` and no `z_axis.direction`. A client that still sends them is not rejected: the fields are ignored. The manual test endpoints (`/test/turntable/rotate`, `/test/motor/z-move`) keep `clockwise` and `direction`.

| Field | Type | Default | Description |
|---|---|---|---|
| `turntable.angle` | `float` | `18.0` | Turntable rotation angle per slice in degrees. At most `180`, a whole number of microsteps (multiples of `0.1125°`) **and** it must divide a full revolution in whole steps (`3200 % steps == 0`, e.g. `18`, `22.5`, `11.25`; `33.75` is rejected) so a ring of photos closes exactly at 360°. |
| `z_axis.distance_mm` | `float` | `100.0` | Vertical travel distance in millimeters between rotational slices (`> 0`, at most `475`, the Z travel). |
| `turntable.delay` | `float` | `0.0010` | Cruise pulse delay in seconds (target maximum angular speed), range `0.0001`–`0.05`. Recommended for scans: `>= 0.0005`. |
| `turntable.acceleration` | `bool` | `true` | Enables trapezoidal velocity ramping to prevent heavy object slippage. |
| `turntable.ramp_percent` | `float` | `0.2` | Fraction of total steps for acceleration ramp (`0.0` to `1.0`). |
| `turntable.decel_percent` | `float \| null` | `null` | Separate deceleration fraction. If `null`, mirrors `ramp_percent`. |
| `turntable.start_delay` | `float \| null` | `null` | Initial launch delay in seconds, range `0.0001`–`0.05`. Defaults to `3 * delay` (light objects) or `5x–8x` for heavy items. |
| `turntable.end_delay` | `float \| null` | `0.015` | Step delay in seconds at the end of each deceleration (slow stop), range `0.005`–`0.030`; `null` disables it. Out-of-range values are rejected (`422`). |
| `z_axis.delay` | `float` | `0.0010` | Cruise pulse delay in seconds for vertical lead screw elevator, range `0.0001`–`0.05`. |
| `z_axis.acceleration` | `bool` | `false` | Disabled by default on Z-axis to prevent lead screw shuddering. |
| `z_axis.ramp_percent` | `float` | `0.2` | Fraction of steps for acceleration if `z_axis.acceleration` is enabled. |
| `z_axis.decel_percent` | `float \| null` | `null` | Optional separate deceleration fraction for Z-axis. |
| `z_axis.start_delay` | `float \| null` | `null` | Starting launch delay for Z-axis if acceleration is enabled, range `0.0001`–`0.05`. |
| `z_axis.end_delay` | `float \| null` | `null` | Optional slow-stop step delay for the Z-axis, range `0.005`–`0.030`; `null` (default) disables it. |
| `cloud.launch_runpod` | `bool` | `false` | `false`: the scan stops (`completed`) once the photos are verified in R2. `true`: the RunPod pipeline runs afterwards. With `true`, `/start` answers `400` when `RUNPOD_API_KEY` or the image name is missing/placeholder. |
| `cloud.resolution_mp` | `float` | `12.0` | Capture resolution in megapixels, `0 < mp <= 13.0` (12.0 nominal, 12.33 real sensor value). |
| `cloud.mode` | `string` | `"rig"` | `single`, `rig` or `two-sides`. |
| `cloud.depthmap_downscale` | `int` | `2` | Meshroom DepthMap downscale (`>= 1`). |
| `cloud.max_input_points` | `int` | `10000000` | Meshroom meshing input point cap (`>= 1000`). |

> **Unknown keys are rejected (`422`).** `StartRequest`, `mechanical`, `turntable`, `z_axis` and `cloud` use `extra="forbid"`: a misspelled or misplaced key (e.g. `mechanical.angle` instead of `mechanical.turntable.angle`) no longer silently falls back to the default. Only the legacy `turntable.clockwise` and `z_axis.direction` are still accepted (and ignored). The `camera_config` block of `/prepare` still tolerates extra keys.

### Response (`202 Accepted` - `StartResponse`)
```json
{
  "status": "accepted",
  "message": "Pipeline started."
}
```

### Error Responses
* `400 Bad Request`: If scanner state is not `PREPARED`, or `cloud.launch_runpod` is `true` and RunPod is not configured (`RUNPOD_API_KEY` missing, placeholder image name). Refused before the physical scan starts.
* `422 Unprocessable Entity`: an unknown key anywhere in the body (e.g. `mechanical.angle`, `mechanical.z_move_mm`); `cloud.resolution_mp` is `0` or above `13.0`; `turntable.angle` is not a whole number of microsteps (multiples of `0.1125°` at 1/16), is above `180`, or does not divide a full revolution in whole steps (`3200 % steps != 0`, e.g. `33.75` or `360`); `z_axis.distance_mm` is above `475` or not a whole number of steps (multiples of `0.01 mm` at 1/4); `delay` / `start_delay` is outside `0.0001`–`0.05` s. Validated up front, so a bad value no longer fails the scan after `prepare`. (The manual `POST /test/turntable/rotate` still accepts up to `360°`, see `TEST_API.md`.)
* `409 Conflict`: A previous `POST /scanner/start` was already accepted and its pipeline has not finished yet (prevents two simultaneous scans from a double click).
  ```json
  {"detail": "Cannot start scan from state idle. Must be PREPARED."}
  ```

### Example `curl`
```bash
curl -X POST http://localhost:8000/scanner/start \
     -H "Content-Type: application/json" \
     -d '{
       "job_id": "scan-sample-01",
       "mechanical": {
         "turntable": {
           "angle": 18.0,
           "delay": 0.0010,
           "acceleration": true,
           "ramp_percent": 0.4,
           "decel_percent": 0.6,
           "start_delay": 0.0060
         },
         "z_axis": {
           "distance_mm": 50.0,
           "delay": 0.0010,
           "acceleration": false
         }
       },
       "cloud": {
         "launch_runpod": false,
         "mode": "rig",
         "depthmap_downscale": 2
       }
     }'
```

---

## 3. Cancel / Emergency Stop (`POST /scanner/cancel`)

Instantly aborts all physical movements and cloud computing.

### Behavior
1. **Physical E-Stop**: Cuts motor power & torque on both the turntable and Z-axis steppers (`release_torque=True`).
2. **Lights & Cameras**: Powers off illumination LEDs and closes camera streams.
3. **Upload Abort**: Immediately cancels the background photo upload worker.
4. **Cloud Pod Termination**: If a RunPod job is active, immediately terminates the remote GPU pod to stop billing.
5. **State Transition**: Sets `scanner.state = CANCELLED`.

### Response (`200 OK` - `CancelResponse`)
```json
{
  "status": "success",
  "state": "cancelled"
}
```

### Example `curl`
```bash
curl -X POST http://localhost:8000/scanner/cancel
```

---

## 4. Get Status (`GET /scanner/status`)

Polling endpoint used by the frontend to track the active state, the phase, photo counters, the failure reason, and the MJPEG preview links.

### Recommended Polling Interval
* **During Preparation / Running / Processing**: Poll every **1.0 to 2.0 seconds**.
* **During Idle**: Stop polling or poll every 10 seconds.

### Response (`200 OK` - `StatusResponse`)
```json
{
  "state": "running",
  "total_photos": 40,
  "stream_urls": [
    "/scanner/stream/mjpeg?camera=1",
    "/scanner/stream/mjpeg?camera=2"
  ],
  "phase": "scanning",
  "phase_message": "Ring 2: capturing",
  "level": 2,
  "photos_captured": 24,
  "photos_uploaded": 22,
  "missing": [],
  "error": null,
  "tof_warnings": []
}
```

| Field | Type | Description |
|---|---|---|
| `state` | `string` | State machine state (table below). |
| `total_photos` | `int` | Photos captured so far (kept for backward compatibility). |
| `phase` | `string \| null` | Finer progress inside a state, e.g. `preparing`, `scanning`, `uploading`, `verifying`, `done`. `null` when the scanner does not report it. |
| `phase_message` | `string \| null` | Human readable detail of the phase. |
| `level` | `int` | Current ring (`0` = first). |
| `photos_captured` | `int` | Photos taken by the cameras. |
| `photos_uploaded` | `int` | Photos confirmed uploaded to R2. |
| `missing` | `string[]` | R2 keys not found at the final verification (at most 50 are returned). Non-empty only with `state: "error"`. |
| `error` | `string \| null` | Why the run ended in `error`. |
| `tof_warnings` | `string[]` | Time-of-flight sensor warnings (e.g. invalid readings) raised during the scan. |

All new fields have safe defaults (`null`, `0`, `[]`), so a client written for the old response keeps working.

### Error mapping
When `scan()` finishes the physical scan but the verification finds photos missing from the bucket, the pipeline ends in `state: "error"` with `missing` and `error` filled (the photos stay on disk). Any other failure ends in `error` too, except a cancel: `cancelled` is never overwritten.

### State Meaning Table
| `state` | Description | Typical Duration |
|---|---|---|
| `idle` | Scanner is powered on and waiting for commands | Indefinite |
| `preparing` | Cameras streaming, carriage homing, cloud sanitizing | 15–30 seconds |
| `prepared` | Carriage in position, AE/AWB locked, ready to scan | Until `/start` |
| `running` | Turntable rotating, taking photos, uploading to R2 | 2–5 minutes |
| `processing` | Finalising after the scan; with `cloud.launch_runpod = true` the RunPod GPU pipeline is executing | seconds (3–10 minutes with RunPod) |
| `downloading` | Downloading completed 3D model from R2 (RunPod only) | 10–30 seconds |
| `completed` | Photos verified in R2 (default) or, with RunPod, 3D model ready in output folder | Finished |
| `cancelled` | Aborted by user via `/cancel` | Finished |
| `error` | Hardware, upload verification (`missing` photos) or cloud failure occurred; see `error` | Finished |

### Example `curl`
```bash
curl -X GET http://localhost:8000/scanner/status
```

---

## 5. Native MJPEG Video Stream (`GET /scanner/stream/mjpeg`)

Delivers a high-framerate `multipart/x-mixed-replace` MJPEG stream directly consumable by HTML `<img>` elements in web browsers without requiring RTSP plugins, WebRTC transcoders, or HLS latency.

### Query Parameters
* `camera` (optional `int`, values `1` or `2`): Selects which camera sensor to stream from. If omitted, streams from the currently active camera (`cam1` by default).

### Behavior & Resource Optimization
1. **On-Demand Encoding (Spectator Tracking)**:
   - When a browser connects to the endpoint, the server registers the connection via `dual_cameras.add_spectator(cam_idx)`.
   - The camera grab loop only encodes frames into JPEG when `spectator_count > 0`. When no browsers are viewing, CPU encoding overhead is **0%**.
   - Upon connection closure or network teardown, the `finally` block unregisters via `remove_spectator(cam_idx)`.
2. **Decoupled Stream Quality**:
   - Compresses frames using `dual_cameras.stream_quality` (default: 80).
   - Keeps high-resolution photogrammetry captures completely isolated at full 95% quality.
3. **Multi-Client Concurrency**:
   - Supports multiple concurrent viewers on the same camera or different cameras simultaneously.
   - Each client independently pulls new frames via `wait_jpeg()` without duplicate camera grabs.

### Response (`200 OK`)
* **Headers**: `Content-Type: multipart/x-mixed-replace; boundary=FRAME`
* **Payload**: Continuous stream of JPEG frames wrapped in multipart boundaries.

### Example Usage (HTML)
```html
<!-- Live preview of Camera 1 -->
<img src="http://127.0.0.1:8000/scanner/stream/mjpeg?camera=1" alt="Camera 1 Live Feed" />

<!-- Live preview of Camera 2 -->
<img src="http://127.0.0.1:8000/scanner/stream/mjpeg?camera=2" alt="Camera 2 Live Feed" />
```

### Example `curl`
```bash
curl -N http://localhost:8000/scanner/stream/mjpeg?camera=1 > /dev/null
```

---

## 6. Active Preview Camera Switch (`POST /scanner/stream/active`)

Instantly toggles the active preview camera between Camera 1 (upper) and Camera 2 (lower).

### Query Parameters
* `camera_id` (required `int`, `1` or `2`): Target camera ID.

### Behavior
* **Zero Loading Latency**:
  - In `DualCameraRPISync` mode, both cameras continuously grab frames into DMA triple buffers (`buffer_count=3`).
  - Switching `active_camera` updates an internal index pointer. If the user changes views or toggles preview links, the video switches **immediately** with zero reconnect loading delay.

### Response (`200 OK`)
```json
{
  "status": "success",
  "active_camera": 2,
  "message": "Switched preview to camera 2."
}
```

### Example `curl`
```bash
curl -X POST "http://localhost:8000/scanner/stream/active?camera_id=2"
```

---

## 7. Dynamic Stream Quality Control (`POST /scanner/stream/quality`)

Dynamically adjusts the live preview JPEG compression quality on the fly without stopping the stream or dropping frames.

### Query Parameters
* `quality` (required `int`, `1` to `100`): Target JPEG compression quality. Defaults to 80.

### Behavior
* In `DualCameraRPISync`, updates the live `stream_quality` property.
* **Non-Destructive**: Photogrammetry still image quality (`quality=95`) is completely protected and remains unaffected.
* Provides instant bandwidth adaptation for low-signal Wi-Fi setups.

### Response (`200 OK`)
```json
{
  "status": "success",
  "quality": 75,
  "message": "Updated stream quality to 75."
}
```

### Example `curl`
```bash
curl -X POST "http://localhost:8000/scanner/stream/quality?quality=75"
```

---

## 8. Frame Synchronization Status (`GET /scanner/stream/sync-status`)

Returns real-time telemetry from the dual camera driver: `rpi.sync` readiness, the newest frame of each camera, dropped frames, the sub-frame shutter offset and what each sensor is using. (Also available as `GET /test/stream/sync-status`: the same function.)

### Response (`200 OK`)
```json
{
  "ready": true,
  "cam1_ready": true,
  "cam2_ready": true,
  "cam1_seq": 1420,
  "cam2_seq": 1420,
  "active_camera": 1,
  "offset_ms": 0.028,
  "drops": [0, 0],
  "stream_fps": 10.0,
  "last_jpeg_kb": 85.0,
  "quality": 95,
  "stream_quality": 80,
  "spectators": [1, 0],
  "cam1_controls": {"exposure_time": 10000, "analogue_gain": 1.5, "colour_gains": [1.8, 1.6], "sensor_timestamp": 123456789},
  "cam2_controls": {"exposure_time": 10000, "analogue_gain": 1.5, "colour_gains": [2.0, 1.5], "sensor_timestamp": 123456812}
}
```
Drivers without a continuous sync pipeline (`xvs`, `staggered`) answer only `{"ready": true, "is_streaming": <bool>, "mode": "<driver class>"}`.

### Telemetry Field Guide (`DualCameraRPISync`)
* `ready`: `true` when both cameras reported `SyncReady` (`cam1_ready`, `cam2_ready` per camera).
* `cam1_seq`, `cam2_seq`: sequence number of the newest frame of each camera.
* `active_camera`: the camera of the live preview (1 or 2).
* `offset_ms`: shutter phase difference between the two cameras, modulo the frame period (100 ms at the default 10 fps). Measured on the rig at about 0.037 ms; `null` until both cameras have delivered a frame.
* `drops`: frames dropped, per camera `[cam1, cam2]`.
* `stream_fps`: JPEG frames per second published to the browsers over the last 2 s (0 when nobody watches; the preview encodes only while a viewer is connected).
* `last_jpeg_kb`: size of the last preview JPEG. `quality` / `stream_quality`: JPEG quality of the photos / of the preview.
* `spectators`: connected viewers `[cam1, cam2]`.
* `cam1_controls`, `cam2_controls`: what each sensor is really using now (`exposure_time` in microseconds, `analogue_gain`, `colour_gains`, `sensor_timestamp`); a black picture shows up here as a tiny exposure.

### Example `curl`
```bash
curl -X GET http://localhost:8000/scanner/stream/sync-status
```

---

## Frontend Integration Pattern (JavaScript / TypeScript)

```typescript
// 1. Prepare Scanner and Display Live MJPEG Stream
async function prepareScanner() {
  const res = await fetch("http://127.0.0.1:8000/scanner/prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      enable_stream: true,
      camera_config: {
        quality: 95,
        photo_resolution: [4056, 3040]
      }
    })
  });
  const data = await res.json();
  
  // Option A: Point an <img> tag directly to the native low-latency MJPEG stream
  const imgElement = document.getElementById("live-stream") as HTMLImageElement;
  imgElement.src = "http://127.0.0.1:8000/scanner/stream/mjpeg?camera=1";

  // Option B: use the URLs returned by the server (the same MJPEG endpoints)
  if (data.stream_urls) {
    console.log("Preview URLs:", data.stream_urls);
  }

  // Poll until scanner reaches PREPARED state
  await pollUntilState("prepared");
}

// 2. Switch Camera View in UI with Zero Latency
async function switchCamera(cameraId: 1 | 2) {
  // Option A: Call the switch API to flip the default stream
  await fetch(`http://127.0.0.1:8000/scanner/stream/active?camera_id=${cameraId}`, { method: "POST" });
  
  // Option B: Or simply update the <img> src query parameter directly!
  const imgElement = document.getElementById("live-stream") as HTMLImageElement;
  imgElement.src = `http://127.0.0.1:8000/scanner/stream/mjpeg?camera=${cameraId}`;
}

// 3. Start the Photogrammetry Scan
async function startScan(jobId: string) {
  await fetch("http://127.0.0.1:8000/scanner/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: jobId,
      mechanical: {
        turntable: {
          angle: 18.0,
          delay: 0.0010,
          acceleration: true,
          ramp_percent: 0.2
        },
        z_axis: {
          distance_mm: 80.0,
          delay: 0.0010,
          acceleration: false
        }
      },
      cloud: { launch_runpod: false, mode: "rig", depthmap_downscale: 2 }
    })
  });

  // Track progress through completion
  await pollUntilState("completed");
}

// 4. Polling helper
async function pollUntilState(targetState: string) {
  while (true) {
    const res = await fetch("http://127.0.0.1:8000/scanner/status");
    const status = await res.json();
    
    console.log(`Current state: ${status.state} (${status.phase ?? "-"}), photos: ${status.photos_uploaded}/${status.photos_captured}`);

    if (status.state === targetState) return status;
    if (status.state === "error" || status.state === "cancelled") {
      const why = status.error ?? `${status.missing.length} photo(s) missing`;
      throw new Error(`Scan ended with state: ${status.state} (${why})`);
    }
    
    await new Promise(r => setTimeout(r, 1500));
  }
}
```
