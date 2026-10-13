# Scanner Core API Documentation (`/scanner`)

The Scanner router (`../scanner.py`) orchestrates the end-to-end photogrammetry pipeline: camera stream initialization, mechanical positioning, 360° capture synchronization, cloud GPU 3D reconstruction, and mesh retrieval.

Interactive OpenAPI documentation is accessible at: `http://<pi-ip>:8000/docs#/Scanner`

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
        Homing --> Positioning: Carriage rises 30mm facing object
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
        CaptureSlice --> FinalizeUpload: Watchdog verifies R2 uploads
        FinalizeUpload --> [*]
    }

    RUNNING --> PROCESSING: Photos uploaded to R2
    PROCESSING --> DOWNLOADING: RunPod photogrammetry completes
    DOWNLOADING --> COMPLETED: 3D output.zip extracted

    PREPARING --> CANCELLED: POST /scanner/cancel
    PREPARED --> CANCELLED: POST /scanner/cancel
    RUNNING --> CANCELLED: POST /scanner/cancel
    PROCESSING --> CANCELLED: POST /scanner/cancel
    DOWNLOADING --> CANCELLED: POST /scanner/cancel

    PREPARING --> ERROR: Mechanical / Cloud failure
    RUNNING --> ERROR: Upload stall / Motor error
    PROCESSING --> ERROR: RunPod OOM / timeout
    DOWNLOADING --> ERROR: R2 download failure
```

---

## Endpoint Reference

| Method | Path | Status Code | Description |
|---|---|---|---|
| `POST` | `/scanner/prepare` | `202 Accepted` | Starts live stream immediately, homes carriage, and auto-tunes cameras in background |
| `POST` | `/scanner/start` | `202 Accepted` | Launches the physical scan, cloud Meshroom pod, and asset download pipeline |
| `POST` | `/scanner/cancel` | `200 OK` | Emergency stop: cuts motor power, kills lights, closes streams, terminates cloud pods |
| `GET` | `/scanner/status` | `200 OK` | Polls current state, total captured photos, and active stream URLs |
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
   - Initializes the dual cameras and boots the H.264 RTSP video encoder.
   - **Immediately returns HTTP 202** with RTSP stream URLs so the frontend video player can display the live feed right away.
2. **Background Execution**:
   - Drives the carriage down until the bottom limit switch triggers (`home_z_axis`).
   - Climbs **+30mm** above home to rest directly in front of the illuminated turntable object.
   - Cleans up old assets in the Cloudflare R2 bucket.
   - Re-runs camera auto-tuning (`prepare_scan`) in position: settles auto-exposure and auto-white-balance on the illuminated object, then locks them.
   - Transitions state to `PREPARED`.

### Request Body (`PrepareRequest`)
```json
{
  "enable_stream": true,
  "bitrate": 2000000,
  "illumination_brightness": 1.0,
  "camera_config": {
    "master_id": 0,
    "slave_id": 1,
    "quality": 95,
    "photo_resolution": [4056, 3040],
    "video_resolution": [1920, 1080],
    "preview_resolution": [1280, 720],
    "rotation": 0,
    "focus": null,
    "show_preview": false,
    "settle_time": 2.0,
    "exposure_time": null,
    "analogue_gain": null,
    "colour_gains": null,
    "awb_mode": "auto",
    "brightness": null,
    "contrast": null,
    "saturation": null,
    "sharpness": null,
    "autofocus_step": 30,
    "keep_running": false
  }
}
```

### Response (`202 Accepted` - `PrepareResponse`)
```json
{
  "status": "accepted",
  "message": "Live stream started. Rig homing and camera tuning in progress.",
  "state": "preparing",
  "stream_urls": [
    "rtsp://192.168.1.100:8554/cam0",
    "rtsp://192.168.1.100:8554/cam1"
  ]
}
```

### Error Responses
* `400 Bad Request`: If scanner is currently in an invalid state (e.g. `RUNNING` or `PROCESSING`).
* `500 Internal Server Error`: Hardware initialization failure.

### Example `curl`
```bash
curl -X POST http://localhost:8000/scanner/prepare \
     -H "Content-Type: application/json" \
     -d '{
       "enable_stream": true,
       "bitrate": 2000000,
       "illumination_brightness": 1.0,
       "camera_config": {
         "master_id": 0,
         "slave_id": 1
       }
     }'
```

---

## 2. Start Scan (`POST /scanner/start`)

Starts the automated scanning and cloud 3D generation workflow.

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
     * Stalled-upload watchdog guarantees all photos are securely stored in R2.
   * **Stage 2 (Cloud Meshroom Photogrammetry - `PROCESSING`)**:
     * Transitions state to `PROCESSING`.
     * Launches a cloud GPU pod on RunPod (e.g., RTX 4090 / RTX 5090).
     * Runs Meshroom pipeline: Feature Extraction (SIFT), Structure-from-Motion (SfM), DepthMap, Meshing, Texturing.
     * Automatically handles out-of-memory GPU fallbacks.
   * **Stage 3 (Asset Download - `DOWNLOADING`)**:
     * Transitions state to `DOWNLOADING`.
     * Downloads `output.zip` containing the completed `.obj`, `.mtl`, and texture files from Cloudflare R2.
     * Extracts files to the local output directory.
   * **Stage 4 (Completion - `COMPLETED`)**:
     * Transitions state to `COMPLETED`.

### Request Body (`StartRequest`)
```json
{
  "job_id": "scan-vase-001",
  "mechanical": {
    "turntable": {
      "angle": 18.0,
      "clockwise": true,
      "delay": 0.0010,
      "acceleration": true,
      "ramp_percent": 0.2,
      "decel_percent": null,
      "start_delay": null
    },
    "z_axis": {
      "distance_mm": 100.0,
      "direction": "up",
      "delay": 0.0010,
      "acceleration": false,
      "ramp_percent": 0.2,
      "decel_percent": null,
      "start_delay": null
    }
  },
  "cloud": {
    "resolution_mp": 12.0,
    "mode": "rig",
    "depthmap_downscale": 2,
    "max_input_points": 10000000
  }
}
```

#### Mechanical & Motor Configuration Parameters

| Field | Type | Default | Description |
|---|---|---|---|
| `turntable.angle` | `float` | `18.0` | Turntable rotation angle per slice in degrees (must yield integer microsteps). |
| `turntable.clockwise` | `bool` | `true` | Turntable rotation direction. |
| `z_axis.distance_mm` | `float` | `100.0` | Vertical travel distance in millimeters between rotational slices. |
| `z_axis.direction` | `string` | `"up"` | Direction of Z-axis carriage travel. |
| `turntable.delay` | `float` | `0.0010` | Cruise pulse delay in seconds (target maximum angular speed). |
| `turntable.acceleration` | `bool` | `true` | Enables trapezoidal velocity ramping to prevent heavy object slippage. |
| `turntable.ramp_percent` | `float` | `0.2` | Fraction of total steps for acceleration ramp (`0.0` to `1.0`). |
| `turntable.decel_percent` | `float \| null` | `null` | Separate deceleration fraction. If `null`, mirrors `ramp_percent`. |
| `turntable.start_delay` | `float \| null` | `null` | Initial launch delay in seconds. Defaults to `3 * delay` (light objects) or `5x–8x` for heavy items. |
| `z_axis.delay` | `float` | `0.0010` | Cruise pulse delay in seconds for vertical lead screw elevator. |
| `z_axis.acceleration` | `bool` | `false` | Disabled by default on Z-axis to prevent lead screw shuddering. |
| `z_axis.ramp_percent` | `float` | `0.2` | Fraction of steps for acceleration if `z_axis.acceleration` is enabled. |
| `z_axis.decel_percent` | `float \| null` | `null` | Optional separate deceleration fraction for Z-axis. |
| `z_axis.start_delay` | `float \| null` | `null` | Starting launch delay for Z-axis if acceleration is enabled. |

### Response (`202 Accepted` - `StartResponse`)
```json
{
  "status": "accepted",
  "message": "Pipeline started."
}
```

### Error Responses
* `400 Bad Request`: If scanner state is not `PREPARED`.
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
         "angle": 18.0,
         "z_move_mm": 50.0,
         "turntable": {
           "delay": 0.0010,
           "acceleration": true,
           "ramp_percent": 0.4,
           "decel_percent": 0.6,
           "start_delay": 0.0060
         },
         "z_axis": {
           "delay": 0.0010,
           "acceleration": false
         }
       },
       "cloud": {
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

Polling endpoint used by the frontend to track active state, photo count, and RTSP stream links.

### Recommended Polling Interval
* **During Preparation / Running / Processing**: Poll every **1.0 to 2.0 seconds**.
* **During Idle**: Stop polling or poll every 10 seconds.

### Response (`200 OK` - `StatusResponse`)
```json
{
  "state": "running",
  "total_photos": 40,
  "stream_urls": [
    "rtsp://192.168.1.100:8554/cam0",
    "rtsp://192.168.1.100:8554/cam1"
  ]
}
```

### State Meaning Table
| `state` | Description | Typical Duration |
|---|---|---|
| `idle` | Scanner is powered on and waiting for commands | Indefinite |
| `preparing` | Cameras streaming, carriage homing, cloud sanitizing | 15–30 seconds |
| `prepared` | Carriage in position, AE/AWB locked, ready to scan | Until `/start` |
| `running` | Turntable rotating, taking photos, uploading to R2 | 2–5 minutes |
| `processing` | RunPod GPU photogrammetry pipeline executing | 3–10 minutes |
| `downloading` | Downloading completed 3D model from R2 | 10–30 seconds |
| `completed` | 3D model ready in output folder | Finished |
| `cancelled` | Aborted by user via `/cancel` | Finished |
| `error` | Hardware or cloud failure occurred | Finished |

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
<img src="http://<pi-ip>:8000/scanner/stream/mjpeg?camera=1" alt="Camera 1 Live Feed" />

<!-- Live preview of Camera 2 -->
<img src="http://<pi-ip>:8000/scanner/stream/mjpeg?camera=2" alt="Camera 2 Live Feed" />
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

Returns real-time telemetry from the dual camera driver, including libcamera `rpi.sync` lock state, per-camera framerates, dropped frame counts, and sub-frame modulo offsets.

### Response (`200 OK`)
```json
{
  "ready": true,
  "status": "SYNC_LOCKED",
  "camera_1": {
    "fps": 24.1,
    "frames_grabbed": 1420,
    "frames_dropped": 0
  },
  "camera_2": {
    "fps": 24.1,
    "frames_grabbed": 1420,
    "frames_dropped": 0
  },
  "offset_modulo_ms": 0.028,
  "active_camera": 1
}
```

### Telemetry Field Guide
* `ready`: `true` when both camera sensors are active and the synchronization loop has established frame alignment.
* `status`: Current synchronization health (`"SYNC_LOCKED"`, `"STAGGERED"`, `"XVS_HARDWARE"`).
* `offset_modulo_ms`: Inter-camera frame timestamp phase difference modulo the 41.6ms frame interval. In `rpi.sync` mode, this settles to **$<0.05\text{ ms}$ (28 µs)**.
* `frames_dropped`: Monitored per camera. Verifies that DMA buffers are released rapidly ($<5\text{ ms}$) without exhausting kernel memory.

### Example `curl`
```bash
curl -X GET http://localhost:8000/scanner/stream/sync-status
```

---

## Frontend Integration Pattern (JavaScript / TypeScript)

```typescript
// 1. Prepare Scanner and Display Live MJPEG Stream
async function prepareScanner() {
  const res = await fetch("http://<pi-ip>:8000/scanner/prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      enable_stream: true,
      bitrate: 4000000,
      camera_config: {
        quality: 95,
        photo_resolution: [4056, 3040],
        preview_resolution: [960, 720]
      }
    })
  });
  const data = await res.json();
  
  // Option A: Point an <img> tag directly to the native low-latency MJPEG stream
  const imgElement = document.getElementById("live-stream") as HTMLImageElement;
  imgElement.src = "http://<pi-ip>:8000/scanner/stream/mjpeg?camera=1";

  // Option B: Connect an H.264 video player to the RTSP stream URL if available
  if (data.stream_urls) {
    console.log("RTSP URLs available:", data.stream_urls);
  }

  // Poll until scanner reaches PREPARED state
  await pollUntilState("prepared");
}

// 2. Switch Camera View in UI with Zero Latency
async function switchCamera(cameraId: 1 | 2) {
  // Option A: Call the switch API to flip the default stream
  await fetch(`http://<pi-ip>:8000/scanner/stream/active?camera_id=${cameraId}`, { method: "POST" });
  
  // Option B: Or simply update the <img> src query parameter directly!
  const imgElement = document.getElementById("live-stream") as HTMLImageElement;
  imgElement.src = `http://<pi-ip>:8000/scanner/stream/mjpeg?camera=${cameraId}`;
}

// 3. Start the Photogrammetry Scan
async function startScan(jobId: string) {
  await fetch("http://<pi-ip>:8000/scanner/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: jobId,
      mechanical: {
        angle: 18.0,
        z_move_mm: 80.0,
        turntable: {
          delay: 0.0010,
          acceleration: true,
          ramp_percent: 0.2
        },
        z_axis: {
          delay: 0.0010,
          acceleration: false
        }
      },
      cloud: { mode: "rig", depthmap_downscale: 2 }
    })
  });

  // Track progress through completion
  await pollUntilState("completed");
}

// 4. Polling helper
async function pollUntilState(targetState: string) {
  while (true) {
    const res = await fetch("http://<pi-ip>:8000/scanner/status");
    const status = await res.json();
    
    console.log(`Current state: ${status.state}, Photos: ${status.total_photos}`);
    
    if (status.state === targetState) return status;
    if (status.state === "error" || status.state === "cancelled") {
      throw new Error(`Scan ended with state: ${status.state}`);
    }
    
    await new Promise(r => setTimeout(r, 1500));
  }
}
```
