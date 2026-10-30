# Automated 3D Scanner - Technical Documentation


## Table of contents

1. [System overview](#1-system-overview)
2. [Hardware architecture](#2-hardware-architecture)
3. [Electro-mechanical and kinematic systems](#3-electro-mechanical-and-kinematic-systems)
4. [Vision and synchronization](#4-vision-and-synchronization)
5. [Active illumination and sensing](#5-active-illumination-and-sensing)
6. [Automated photometric stabilization](#6-automated-photometric-stabilization)
7. [Edge software: state machine and scan modes](#7-edge-software-state-machine-and-scan-modes)
8. [REST API reference](#8-rest-api-reference)
9. [Cloud orchestration and photogrammetry](#9-cloud-orchestration-and-photogrammetry)
10. [Outputs and model library](#10-outputs-and-model-library)
11. [Network and system management](#11-network-and-system-management)
12. [AI assistant](#12-ai-assistant)
13. [Manual hardware tests and calibration](#13-manual-hardware-tests-and-calibration)
14. [Front-end integration guide](#14-front-end-integration-guide)
15. [Reliability, safety and error handling](#15-reliability-safety-and-error-handling)
16. [Key figures and formulas](#16-key-figures-and-formulas)
17. [Known limitations, planned work, open points](#17-known-limitations-planned-work-open-points)
18. [Change log](#18-change-log)

---

## 1. System overview

The scanner is a hybrid **edge-to-cloud Internet of Things (IoT)** system. Local acquisition hardware (turntable, Z carriage, two cameras, lights, sensors) is decoupled from the compute-intensive photogrammetric reconstruction, which runs on ephemeral cloud GPUs.

### 1.1 Components

| Layer | Component | Role |
|---|---|---|
| Edge orchestrator | Raspberry Pi 5 (8 GB RAM, BCM2712 Cortex-A76 @ 2.4 GHz), Debian Trixie | Hosts the FastAPI REST service and the web page, runs the state machine, configures the cameras (libcamera / Picamera2), manages concurrent upload workers to Cloudflare R2 (S3-compatible) |
| Real-time motion MCU | Arduino Uno R3 (ATmega328P @ 16 MHz) | Dedicated motion co-processor: step generation, endstops, emergency abort |
| Link | USB serial, 115,200 baud | Orchestrator to MCU commands |
| Object storage | Cloudflare R2 | Photo exchange between edge and cloud |
| Cloud compute | AliceVision Meshroom on ephemeral RunPod GPUs | Photogrammetric reconstruction |

### 1.2 Why a separate motion MCU

Linux scheduling jitter (tens to hundreds of microseconds) is comparable to the motor step period (1 ms). That jitter can excite mechanical resonances and cause missed steps. The Arduino therefore generates **cycle-counted delays at 62.5 ns resolution** (one cycle at 16 MHz), independent of the Pi's scheduler.

### 1.3 High-level data flow

```
 Web page (browser)
      │  HTTP/JSON, MJPEG preview
      ▼
 ┌──────────────────────────── Raspberry Pi 5 ─────────────────────────────┐
 │ FastAPI  ──►  State machine  ──►  Camera control (Picamera2, XVS sync)  │
 │    │                │                    │                              │
 │    │                ├──► LED control (relay + PWM), ToF, endstops       │
 │    │                └──► Upload workers ───────────────► Cloudflare R2  │
 └────┼─────────────────────────────────────────────────────────────────────┘
      │ USB serial, 115,200 baud
      ▼
 Arduino Uno R3 ──► TB6600 (turntable) / driver (Z axis) ──► NEMA 17 ×2
                                                  R2 ──► RunPod GPU (Meshroom)
```

> **Note on the reconstruction path:** the current API marks the cloud reconstruction as *legacy* (`cloud.launch_runpod` defaults to `false`, states `processing` / `downloading` are only used with it), while the model library serves finished models from the Pi (section 10). See section 17 for the open question on how the two paths relate.

---

## 2. Hardware architecture

### 2.1 Bill of materials (as documented)

| Subsystem | Part | Key parameters |
|---|---|---|
| Compute | Raspberry Pi 5 | 8 GB, 2.4 GHz, RP1 I/O controller (hardware PWM) |
| Motion MCU | Arduino Uno R3 | ATmega328P, 16 MHz |
| Turntable drive | NEMA 17 + TB6600 driver | 12 V, 1.5 A, 1/16 microstepping |
| Z drive | NEMA 17 + T8 lead screw | 8 mm lead, anti-backlash nut, MGN9 rails, 1/4 microstepping |
| Cameras | 2 × Arducam IMX477 | 12.3 MP, 4056 × 3040, 1.55 µm pixel, f = 3.9 mm, 10-bit VCM focus |
| Lighting | 12 V LED strips | Relay (GPIO11), D4184 MOSFET dimmer, 10 kHz hardware PWM (GPIO12) |
| Height sensor | VL53L0X Time-of-Flight | 940 nm, I²C |
| Endstops | 2 × switches | GPIO4, GPIO22, internal pull-ups, 20 ms debounce |
| Storage / cloud | Cloudflare R2, RunPod | S3-compatible, ephemeral GPU hosts |

### 2.2 Pin assignment (Raspberry Pi)

| Signal | GPIO | Notes |
|---|---|---|
| Endstop (bottom / top) | GPIO4, GPIO22 | Internal pull-ups, 20 ms debounce. The sources do not say which GPIO is bottom and which is top **(open)** |
| LED relay | GPIO11 | Switched only at zero current (section 5.2) |
| LED PWM | GPIO12 | 10 kHz hardware PWM from the RP1 |

### 2.3 Mechanical geometry

The scanner uses a **cylindrical-coordinate manipulator** geometry: a rotating turntable (angle θ) and a vertical carriage (height Z), with the cameras at a fixed radial distance.

| Parameter | Value |
|---|---|
| Turntable diameter | Ø260 mm |
| Z travel | up to 475 mm (API limit) |
| Camera radial distance | 300 mm |
| Camera angular separation | 20° |
| Maximum bounding box for Draco accuracy figure | 260 mm |

---

## 3. Electro-mechanical and kinematic systems

### 3.1 Turntable

- NEMA 17 stepper, TB6600 driver, 12 V / 1.5 A, **1/16 microstepping**.
- Angular resolution: **0.1125° per microstep** (1.8° ÷ 16).
- A standard **18° stop = 160 microsteps**.
- Arc resolution at the rim of the Ø260 mm table: **0.255 mm** (0.1125° × π/180 × 130 mm).
- API rule: the stop angle must be a multiple of 0.1125°, at most 180°, and must divide 360° exactly (for example 18°, 22.5°, 11.25°).
- Stops per full ring = 360° ÷ stop angle **(derived)**: 20 stops for 18°.

### 3.2 Z-axis carriage

- NEMA 17, T8 lead screw (8 mm lead), anti-backlash nut, MGN9 rails, **1/4 microstepping**.
- Vertical resolution: **0.01 mm** (8 mm ÷ (200 steps × 4)).
- Hardware endstops (GPIO4, GPIO22) with internal pull-ups and a **20 ms debounce filter** define homing and the absolute limits.
- API rules: distances are multiples of 0.01 mm, greater than 0 and at most 475 mm.

### 3.3 Motion profile

- The firmware executes a **trapezoidal velocity profile**.
- For one 18° stop: move duration **224 ms**, cruise speed **112.5°/s** (0.1125° per 1 ms step).
- Parameters exposed through the API for both axes:

| Parameter | Meaning | Range / default |
|---|---|---|
| `delay` | Step period in seconds (larger = slower) | 0.0001 to 0.05 s, default 0.001 |
| `acceleration` | Smooth start / stop | Turntable default `true`; Z default `false` (keep `false`) |
| `ramp_percent` | Acceleration ramp share | 0 to 1, default 0.2 |
| `decel_percent` | Deceleration ramp share | 0 to 1, default `null` |
| `start_delay` | Step period at the start of the ramp | 0.0001 to 0.05 or `null` |
| `end_delay` | Step period at the end of the ramp | 0.005 to 0.030 or `null` (turntable default 0.015) |

### 3.4 Emergency abort

An **out-of-band 0xFF byte** on the serial link aborts motion. Abort latency is bounded to **≤ 4.1 ms**. At the API level this is `POST /scanner/cancel`, which stops motors, lights, cameras and uploads at any time.

### 3.5 Homing and start height

`prepare` homes the rig on the bottom endstop, then climbs to the start height (`z_start_mm`, 0 to 100 mm for object scans; `start_z_mm`, 100 to 350 mm for face scans).

---

## 4. Vision and synchronization

### 4.1 Sensors and geometry

| Parameter | Value |
|---|---|
| Sensor | 2 × Arducam IMX477, 12.3 MP |
| Image size | 4056 × 3040 (alternative 2028 × 1520) |
| Pixel pitch | 1.55 µm |
| Focal length | 3.9 mm |
| Focus | 10-bit voice coil motor (codes 0 to 1023) |
| Radial distance | 300 mm |
| Angular separation | 20° |
| Ground sampling distance | ≈ 0.12 mm/px (300 mm × 1.55 µm ÷ 3.9 mm ≈ 0.119 mm) **(derived check)** |
| Expected depth precision (convergent pair) | σ_Z ≈ 0.17 mm |

### 4.2 Frame synchronization

Sub-microsecond frame alignment uses the **External Vertical Synchronization (XVS)** hardware pad. The slave camera is initialized first and blocks until the master emits vertical-sync pulses. `GET /scanner/stream/sync-status` exposes diagnostics: `ready`, `offset_ms`, `drops`, `stream_fps`, `spectators` and exposure per camera.

### 4.3 Camera configuration

Camera settings (`camera_config`) are shared by both prepare endpoints. Unknown keys are ignored.

| Field | Default | Rules |
|---|---|---|
| `quality` | 95 | JPEG quality 10 to 100 |
| `photo_resolution` | [4056, 3040] | [4056, 3040] or [2028, 1520] |
| `preview_resolution` | [960, 720] | Even numbers, not larger than the photo |
| `focus` | `"auto"` | `"auto"` (autofocus), integer 0 to 1023 (both lenses), `[cam1, cam2]`, or `null` (last saved position) |
| `rotation` | 0 | 0 / 90 / 180 / 270, or `[cam1, cam2]` |
| `settle_time` | 2.0 | Seconds (0 to 10) before exposure is locked |
| `exposure_time` | `null` | Manual exposure in µs (> 0); `null` = automatic |
| `analogue_gain` | `null` | Manual gain 1 to 16 |
| `colour_gains` | `null` | Manual white balance `[red, blue]` |
| `awb_mode` | `"auto"` | White-balance mode |
| `copy_colour_gains` | `false` | `true` copies camera 1's white balance to camera 2 |
| `brightness`, `contrast`, `saturation`, `sharpness` | `null` | Image adjustments |
| `master_id`, `slave_id` | 0, 1 | Camera ids |

### 4.4 Live preview

An MJPEG stream per camera is available between a prepare (or `/test/stream/start`) and the end of the run. Reload the `<img>` after every new prepare. See section 8.6.

---

## 5. Active illumination and sensing

### 5.1 LED PWM control

12 V LED strips are switched by a relay (GPIO11) and dimmed by a D4184 MOSFET using **10 kHz hardware PWM** (GPIO12) generated by the RP1 controller. At this frequency the residual PWM ripple is bounded to **≤ 0.5%**, which prevents rolling-shutter banding. The API brightness range is 0 < b ≤ 1.

### 5.2 Zero-current switching (ZCS)

The control logic strictly enforces ZCS. The duty cycle is held at 0 for a **50 ms contact-settling time** whenever the relay opens or closes, so the relay never switches under load and contact arcing is prevented.

Sequence when turning lights on **(derived from the rule)**:

1. Duty cycle = 0.
2. Close relay.
3. Wait 50 ms.
4. Ramp duty cycle to the requested brightness.

Turning off is the reverse: duty to 0, wait 50 ms, open relay.

### 5.3 Adaptive ToF scanning

A **VL53L0X** Time-of-Flight sensor (940 nm, I²C) detects the object height. A debounced boolean gate ends the scan early: if **two consecutive readings** exceed the back-wall threshold (**535 mm**), the scanning loop terminates.

API behaviour related to the sensor:

- A missing or failing sensor makes `POST /scanner/prepare` (preflight) or `POST /scanner/start` answer `400`.
- Remarks during a scan appear in `tof_warnings` of `GET /scanner/status`.
- `GET /test/tof` returns the live reading (section 13).

---

## 6. Automated photometric stabilization

### 6.1 Thermal gate

LEDs change output as they warm up. Without an external light sensor, the system infers luminous flux from the camera's own exposure and gain:

```
m(t) = E(t) · G(t)  ∝  1 / Φ(t)
```

where E is exposure time, G is gain and Φ is luminous flux. Scanning starts only when the drift of m(t) is **< 3% over a 10-second window**. This is why `preparing` can take **15 to 30 s, and up to about 2 minutes** with light stabilisation.

### 6.2 Autofocus

A contrast-based sweep tests voice-coil codes across **P = {0, 30, ..., 1020}** (35 positions **(derived)**) and selects the code that maximizes the **variance of the Laplacian** inside a central **40% × 40% region of interest**.

For face scans, `focus: "auto"` autofocuses on the face at capture time; the subject must hold still for about 10 s after the search while autofocus and exposure lock.

---

## 7. Edge software: state machine and scan modes

### 7.1 State machine

`GET /scanner/status` reports `state`:

| `state` | Meaning | Next action |
|---|---|---|
| `idle` | Waiting | prepare |
| `preparing` | Lights, cameras, homing in progress (15-30 s; up to ~2 min with light stabilisation) | wait |
| `prepared` | Ready; `mode` says for what | start (object) or capture (face) |
| `running` | Scanning / capturing / uploading | wait or cancel |
| `processing`, `downloading` | Only with the legacy cloud reconstruction (off by default) | wait |
| `completed` | Done; for a face scan `face_result` is set | prepare again |
| `cancelled` | Stopped by `/scanner/cancel` | prepare again |
| `error` | Failed; `error` says why | prepare again |

```
Object scan:  POST /scanner/prepare      → prepared (mode "object") → POST /scanner/start        → completed
Face scan:    POST /scanner/face/prepare → prepared (mode "face")   → POST /scanner/face/capture → completed
Any time:     POST /scanner/cancel       → cancelled
```

Polling: every 1-2 s while `preparing` or `running`, about every 10 s otherwise.

### 7.2 Phases

`phase` gives finer progress than `state`: `idle`, `preparing`, `prepared`, `scanning`, `uploading`, `verifying`, `done`, `error`, `cancelled`.

### 7.3 Object scan

1. **Prepare**: lights, cameras and preview start at once; homing, climb to the start height and camera calibration run in the background. A preflight refuses with `400` if the cloud, the distance sensor or the disk space is not OK (`detail` says which).
2. **Start**: for each **ring** (a full turntable revolution at one height) the rig stops every `turntable.angle` degrees, captures a synchronized stereo pair, and uploads in parallel. After each ring the Z axis climbs `z_axis.distance_mm`.
3. **Early end**: the ToF gate (section 5.3) ends the loop when the object is no longer in view.
4. **Verify**: after capture, the system checks that all photos exist in the cloud. Missing photos are listed in `missing` (at most 50) and the run ends in `error`.
5. **Upload watchdog**: the run fails after `upload_drain_timeout_s` (default 300 s) without a successful upload; `seconds_since_last_upload` shows the current idle time.

### 7.4 Face scan

A single stereo capture at the best height for a face, in **two steps** (prepare, then capture). Each capture needs its own prepare.

- Face detection uses **MediaPipe** and its model; both must be installed on the Pi, otherwise `400`.
- Strategies (`strategy`):
  - `sweep_best_score`: sweep the search range and return to the best-scoring height.
  - `direct_stop`: stop as soon as the face is centred.
  - `none`: photograph where the carriage currently is.
- `manual_z` (200 to 475 mm) goes straight to a height without a search.
- Constraint: `start_z_mm + search_range_mm ≤ 475`, otherwise `422`.

### 7.5 Cancellation

`POST /scanner/cancel` performs an emergency stop of motors, lights, cameras and uploads. While it finishes, new commands may answer `409`.

---

## 8. REST API reference

### 8.1 General rules

| Item | Rule |
|---|---|
| Base URL | `http://127.0.0.1:8000`. The server listens on the Pi only; the page is served by the same server at `/`, so relative URLs work |
| Authentication / CORS | None |
| Format | JSON in and out (`Content-Type: application/json`), except the MJPEG stream and model files |
| Errors | Always `{"detail": "<message>"}`. For `422`, `detail` is a list of field errors |
| Interactive docs | `/docs` while the server runs |

#### Status codes

| Code | Meaning in this API |
|---|---|
| 200 / 202 / 204 | Done / accepted and running in the background (poll `/scanner/status`) / done, empty body |
| 400 | Refused in this state, or a command failed |
| 401 | Wi-Fi password wrong or missing |
| 404 | Unknown object, model or saved network |
| 409 | Busy: another scan or motion runs, an emergency stop is finishing, or a network action during a scan |
| 422 | Invalid body or query |
| 500 | Hardware or server error |
| 503 | A device or service is missing: the Arduino, the AI assistant, a system tool |
| 504 | A network command did not answer in time |

### 8.2 `GET /scanner/status`

```json
{
  "state": "running",
  "mode": "object",
  "total_photos": 40,
  "stream_urls": ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"],
  "phase": "scanning",
  "phase_message": "Ring 2: capturing",
  "level": 2,
  "photos_captured": 24,
  "photos_uploaded": 22,
  "missing": [],
  "error": null,
  "tof_warnings": [],
  "face_result": null,
  "seconds_since_last_upload": 3.2,
  "upload_drain_timeout_s": 300.0
}
```

| Field | Meaning |
|---|---|
| `mode` | `"object"`, `"face"` or `null`: what the last prepare was for (stays set after the run) |
| `stream_urls` | MJPEG preview URLs (`null` when no stream) |
| `phase` | See section 7.2 |
| `phase_message` | Text for the user (also face scan warnings) |
| `level` | Current ring (object scan) |
| `photos_captured` / `photos_uploaded` | Progress counters |
| `missing` | Photos missing in the cloud after verification (with `state: "error"`, at most 50) |
| `error` | Why the run ended in `error` |
| `tof_warnings` | Distance-sensor remarks during an object scan |
| `face_result` | After a face scan (section 8.5) |
| `seconds_since_last_upload` / `upload_drain_timeout_s` | Upload watchdog |

Nothing was removed from this endpoint in the October 2026 change; the new fields (`mode`, `face_result`, upload fields) have safe defaults.

### 8.3 `POST /scanner/prepare` → 202 (object scan)

Every field is optional.

```json
{
  "enable_stream": true,
  "illumination_brightness": 1.0,
  "z_start_mm": 30.0,
  "camera_config": {"quality": 95, "photo_resolution": [4056, 3040], "focus": "auto"}
}
```

| Field | Default | Rules |
|---|---|---|
| `enable_stream` | `true` | Start the MJPEG preview |
| `illumination_brightness` | 1.0 | LED power, 0 < b ≤ 1 |
| `z_start_mm` | 30.0 | Start height above the bottom, 0 to 100 mm, multiples of 0.01 |
| `camera_config` | see section 4.3 | |

Answer:

```json
{"status": "accepted", "message": "Live stream started. Rig homing and camera tuning in progress.",
 "state": "preparing", "stream_urls": ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]}
```

Errors: `400` wrong state (for example `running`) or preflight refused; `409` busy or emergency stop finishing; `422` invalid body; `503` Arduino not answering; `500` hardware.

### 8.4 `POST /scanner/start` → 202 (object scan)

Needs `state: "prepared"` and `mode: "object"`. The body is optional; unknown keys are refused (`422`).

```json
{
  "job_id": "scan-vase-001",
  "mechanical": {
    "turntable": {"angle": 18.0, "delay": 0.001, "acceleration": true, "ramp_percent": 0.2,
                  "decel_percent": null, "start_delay": null, "end_delay": 0.015},
    "z_axis": {"distance_mm": 100.0, "delay": 0.001, "acceleration": false, "ramp_percent": 0.2,
               "decel_percent": null, "start_delay": null, "end_delay": null}
  },
  "cloud": {"launch_runpod": false}
}
```

| Field | Default | Rules |
|---|---|---|
| `turntable.angle` | 18.0 | Degrees per photo stop; ≤ 180, multiple of 0.1125, divides 360° exactly |
| `turntable.delay` / `z_axis.delay` | 0.001 | 0.0001 to 0.05 s |
| `turntable.acceleration` | `true` | Smooth start / stop |
| `ramp_percent` / `decel_percent` | 0.2 / `null` | 0 to 1 |
| `start_delay` | `null` | 0.0001 to 0.05 |
| `end_delay` | 0.015 (turntable), `null` (Z) | 0.005 to 0.030 or `null` |
| `z_axis.distance_mm` | 100.0 | Climb between rings, > 0, ≤ 475, multiple of 0.01 |
| `z_axis.acceleration` | `false` | Keep `false` |
| `cloud.launch_runpod` | `false` | Keep `false` (legacy reconstruction) |

Answer: `{"status": "accepted", "message": "Pipeline started."}`.
Errors: `400` not prepared, prepared for a face scan, or distance sensor missing; `409` a start was already accepted; `422` invalid values; `503` Arduino.

### 8.5 Face scan endpoints

#### `POST /scanner/face/prepare` → 202

```json
{"enable_stream": true, "illumination_brightness": 0.5, "start_z_mm": 180.0, "upload_r2": true,
 "camera_config": {"focus": "auto"}}
```

| Field | Default | Rules |
|---|---|---|
| `illumination_brightness` | 0.5 | 0 < b ≤ 1 |
| `start_z_mm` | 180.0 | Where the carriage waits and the search starts, 100 to 350 mm, multiples of 0.01 |
| `upload_r2` | `true` | Upload the photo pair to the cloud |
| `camera_config` | as above | `focus: "auto"` = autofocus on the face at capture; a number = fixed focus |

Answer: same shape as `/scanner/prepare`. Errors: `400` MediaPipe or its model missing, or wrong state; `409`; `422` (unknown keys too); `503`.

#### `POST /scanner/face/capture` → 202

Needs `state: "prepared"` and `mode: "face"`.

```json
{"strategy": "sweep_best_score", "search_range_mm": 150.0, "step_mm": 8.0, "manual_z": null, "settle_s": 0.15}
```

| Field | Default | Rules |
|---|---|---|
| `strategy` | `"sweep_best_score"` | `sweep_best_score`, `direct_stop`, `none` |
| `search_range_mm` | 150.0 | 10 to 250; `start_z_mm + search_range_mm ≤ 475` |
| `step_mm` | 8.0 | 1 to 25 |
| `manual_z` | `null` | 200 to 475: go there, no search |
| `settle_s` | 0.15 | 0.05 to 2 |

Answer: `{"status": "accepted", "message": "...", "state": "running"}`. Errors: `400` not prepared for a face scan; `409`; `422`; `503`. Old keys (`start_z_mm`, `home`, `illumination_brightness`, `upload_r2`) on capture now give `422`.

#### Result (`state: "completed"`)

```json
"face_result": {"optimal_z_mm": 236.0, "best_score": 1.7, "face_detected": true, "warning": null,
                "photos": [".../rig/0/f_236.jpg", ".../rig/1/f_236.jpg"], "r2_uploaded": true}
```

- `face_detected: false` with a `warning`: no face was found, the photo was taken at the start height, `best_score` is `null`. This is **not an error**; offer a new prepare with another `start_z_mm`.
- `face_detected: null`: no search was done (`manual_z` or `strategy: "none"`).
- `warning` can also report that only one camera saw the face.

### 8.6 Live preview

| Method | Path | Use |
|---|---|---|
| GET | `/scanner/stream/mjpeg?camera=1` | MJPEG stream for `<img src>`; `camera` is 1 or 2 (optional: the active camera). `400` when the cameras are not streaming |
| POST | `/scanner/stream/active?camera_id=2` | Switch the preview camera (or body `{"camera_id": 2}`) |
| POST | `/scanner/stream/quality?quality=75` | Preview JPEG quality 1 to 100 (or body `{"quality": 75}`) |
| GET | `/scanner/stream/sync-status` | Diagnostics: `ready`, `offset_ms`, `drops`, `stream_fps`, `spectators`, exposure per camera |

```html
<img src="/scanner/stream/mjpeg?camera=1" alt="Camera 1">
```

### 8.7 `POST /scanner/cancel` → 200

Emergency stop of everything. Answer: `{"status": "success", "state": "cancelled"}`.

---

## 9. Cloud orchestration and photogrammetry

The photogrammetric pipeline uses **AliceVision Meshroom** on ephemeral **RunPod** GPUs. In the current API this path is controlled by `cloud.launch_runpod` (default `false`, described as legacy); see section 17.

### 9.1 Memory-aware sizer

To prevent silent Out-of-Memory (OOM) failures, the orchestrator predicts RAM with an empirical model:

```
M_req = 1.1 · ( 4 GB + N · R · K + P / (1.5 · 10^6) )
```

- Jobs requiring **≤ 64 GB** go to consumer GPUs.
- Larger jobs go to high-memory hosts.
- The meaning of the symbols N, R, K and P is not defined in the sources **(open)**. Document them here once confirmed (likely candidates: image count, per-image resolution factor, per-image constant, and a point or feature count, but this is not stated).

### 9.2 Feature matching

- **DSP-SIFT** descriptors, matched via approximate nearest neighbours.
- **AC-RANSAC** (A Contrario RANSAC) verifies the epipolar constraints automatically, without fixed pixel tolerances.

### 9.3 Rig-constrained bundle adjustment

The stereo rig is declared through modified EXIF tags, so all cameras of the rig share one relative pose. Pose unknowns in incremental SfM drop from

```
6 · n_c · n_p     to     6 · n_p + 6 · (n_c − 1)
```

(n_c cameras per rig, n_p rig positions). For n_c = 2 this approaches a 50% reduction for large n_p; the source quotes **49%**. This heavily stabilizes bundle adjustment on weakly textured geometries.

### 9.4 Densification and meshing

1. Depth maps by **Semi-Global Matching (SGM)**, downscaled by a factor of 2.
2. Point cloud to **Delaunay tetrahedralization**.
3. Inside / outside labelling with an **s-t minimum graph cut**.
4. Smoothing with **two Laplacian iterations**.

### 9.5 Draco compression

The final GLB uses `KHR_draco_mesh_compression` with **14 quantization bits**. Over a 260 mm bounding box the quantization step is 260 mm ÷ 2¹⁴ ≈ 15.9 µm, so the maximum geometric distortion is **≤ 7.9 µm** (half a step) **(derived check)**.

---

## 10. Outputs and model library

Finished models are stored on the Pi and served by the library endpoints.

### 10.1 `GET /library/objects?offset=0&limit=20`

```json
{
  "items": [{
    "id": "20261027-202750-21d2de",
    "object": {"title": "Parkside", "description": "", "created_at": "2026-10-28T14:44:17+00:00",
               "dimensions_mm": {"length": 71.0, "width": 54.3, "height": 226.9},
               "volume_cm3": 363.74, "surface_area_cm2": 377.2, "watertight": true},
    "models": {".glb": ["low", "high"], ".stl": ["low", "high"]}
  }],
  "total": 1, "offset": 0, "limit": 20, "next_offset": null
}
```

- Newest first; `limit` 1 to 100; next page with `offset = next_offset` until it is `null`.
- `object` is the reconstruction's `object.json`.
- `models` lists only files that exist; disable buttons for the missing ones.

### 10.2 `GET /library/objects/{id}`

One item as above. `404` when unknown.

### 10.3 `GET /library/objects/{id}/model?format=glb&lod=low`

Returns the file (`model/gltf-binary` or `model/stl`), cacheable (ETag).

| Parameter | Values | Default |
|---|---|---|
| `format` | `.glb` / `.stl` (also `glb`, `stl`, `gltf`, any case) | `.glb` |
| `lod` | `low` (light) or `high` | `low` |

Errors: `404` missing model, `422` other values. Load `low` first.

```js
const url = (id, format, lod) => `/library/objects/${id}/model?format=${format}&lod=${lod}`;
new GLTFLoader().load(url(id, "glb", "low"), gltf => scene.add(gltf.scene));
new STLLoader().load(url(id, "stl", "low"), geo => scene.add(new THREE.Mesh(geo, material)));
```

### 10.4 Mesh quality indicators

`watertight`, `dimensions_mm`, `volume_cm3` and `surface_area_cm2` are reported per object. `watertight: true` means the mesh is closed and suitable for volume computation and 3D printing (STL).

---

## 11. Network and system management

While the scanner is `preparing`, `running`, `processing`, `downloading` or still uploading, the actions marked **guarded** answer `409`. Reading state is always allowed.

### 11.1 `GET /system/status`

Reads all connections in one call (poll every 5 to 15 s). It never disturbs a scan.

```json
{
  "ethernet": {"interface": "eth0", "cable_plugged": true, "state": "connected",
               "connection": "Wired connection 1", "ip": "192.168.1.20/24", "connected": true},
  "wifi": {"enabled": true, "interface": "wlan0", "state": "connected", "connected": true,
           "connection": "Home", "ssid": "Home", "signal": 70, "ip": "192.168.1.31/24"},
  "bluetooth": {"powered": true, "connected": true,
                "devices": [{"mac_address": "AA:BB:CC:DD:EE:FF", "name": "Speaker"}]},
  "restore": {"wifi": "already connected (Home)", "state": "done", "seconds": 4.2}
}
```

- A part can be `{"error": "..."}` while the others stay valid.
- `state`: `connected`, `connecting`, `disconnected`, `unavailable`.
- `restore`: reconnection of the last run's connections at startup: `null` (nothing to restore), `{"state": "running"}`, then one line per connection.

### 11.2 Wi-Fi

| Method | Path | Body / query | Answer |
|---|---|---|---|
| GET | `/system/wifi/status` | | `{"enabled": true}` |
| POST | `/system/wifi/power/{on\|off}` (guarded) | | `{"state": "on"}` |
| GET | `/system/wifi/scan?rescan=false` (guarded) | `rescan=true` forces a radio scan (~20 s) | `[{"in_use": "*", "ssid": "Home", "signal": 70, "security": "WPA2", "saved": true}]` |
| GET | `/system/wifi/saved` | | `[{"name": "Home", "ssid": "Home", "autoconnect": true}]` (no passwords) |
| DELETE | `/system/wifi/saved/{name}` (guarded) | | 204; 404 unknown |
| POST | `/system/wifi/connect` (guarded) | `{"ssid": "Home", "password": "", "identity": null}` | 204; 401 wrong or missing password |

- Scan returns one entry per SSID, connected first, then by signal. `security: ""` means an open network. `saved: true` means connect with an empty password. SSIDs containing `:` are supported.
- `identity` is only for enterprise (802.1X) networks.
- A wrong password never deletes a working saved network.
- Wi-Fi connect error mapping: `401` only for a wrong or missing password; `400`, `504`, `409` otherwise.

### 11.3 Bluetooth

| Method | Path | Body / query | Answer |
|---|---|---|---|
| GET | `/system/bluetooth/status` | | `{"powered": true}` |
| POST | `/system/bluetooth/power/{on\|off}` (guarded) | | `{"powered": "on"}` |
| GET | `/system/bluetooth/scan?duration=8` (guarded) | `duration` 1 to 20 s; `include_unnamed`, `include_out_of_range` (debug) | list of devices |
| POST | `/system/bluetooth/connect` (guarded) | `{"mac_address": "AA:BB:CC:DD:EE:FF"}` | the device |

Device object:

```json
{"mac_address": "AA:BB:CC:DD:EE:01", "name": "Phone", "icon": "phone", "paired": false, "connected": false,
 "rssi": -60, "device_class": null}
```

- The scan lasts about `duration` seconds and returns named devices in range now, strongest first, plus connected ones.
- `connect` pairs only when needed, so reconnecting a paired device works.
- `GET` on the power endpoints still works for now but will be removed; use `POST`.

### 11.4 Ethernet

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/system/ethernet/status` | | `{"interface": "eth0", "cable_plugged": true, "state": "connected", "connection": "...", "ip": "192.168.1.20/24"}` |
| POST | `/system/ethernet/connect` (guarded) | `{"ssid": "", "password": "...", "identity": "user"}` | `{"status": "success", "message": "Enterprise Ethernet connected"}` |

`connect` is only for 802.1X (enterprise) wired networks; a normal cable connects by itself. Without a port: `interface: null`, `state: "unavailable"`.

---

## 12. AI assistant

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/ai/status` | | `{"on": true, "loading": false, "reason": null}` |
| POST | `/api/ai/chat` | `{"question": "...", "history": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "..."}]}` | `{"response": "..."}` |

- Check the status at page load (and again while `loading`). Enable the chat only when `on`; otherwise show `reason`.
- `chat` answers `503` when the assistant is off and `500` on an assistant error.
- The documents the assistant answers from are served at `/ai-docs/<file>`.

---

## 13. Manual hardware tests and calibration

For a service / calibration page. Every motion endpoint answers `409` while a scan owns the rig (`preparing`, `prepared`, `running`) or another motion runs, and `503` without the Arduino.

| Method | Path | Body / query | Answer |
|---|---|---|---|
| GET | `/test/endstops` | | `{"bottom_endstop": true, "top_endstop": false}` |
| GET | `/test/tof` | | `{"status": "success", "distance_mm": 412, "valid": true, "available": true}` (`status: "invalid"`, `distance_mm: null` without a reading) |
| POST | `/test/lights/enable` | `{"brightness": 0.5}` (optional, 0 to 1) | `{"status": "success", "state": "enabled", "brightness": 0.5, "message": "..."}` |
| POST | `/test/lights/disable` | | `{"status": "success", "state": "disabled", "message": "..."}` |
| GET | `/test/motor/status` | | `{"turntable": "enabled", "z_axis": "disabled"}` |
| POST | `/test/motor/enable?target=all` | `target`: `turntable`, `z_axis`, `all` | `{"status": "success", "target": "all", "state": "enabled"}` |
| POST | `/test/motor/disable?target=all` | same | same, `"state": "disabled"` |
| POST | `/test/motor/home-bottom` | query `delay`, `start_delay`, `ramp_percent`, `acceleration` (optional) | `{"status": "success", "message": "...", "bottom_endstop": true}` |
| POST | `/test/motor/move-to-top` | same | `{"status": "success", "message": "...", "top_endstop": true}` |
| POST | `/test/motor/z-move` | `{"distance_mm": 15.0, "direction": "up", "delay": 0.001, "acceleration": false}` | `{"status": "success", "direction": "up", "distance_mm": 15.0}`; `400` at a limit switch |
| POST | `/test/turntable/rotate` | `{"angle": 18.0, "clockwise": true, "delay": 0.001, "acceleration": true, "ramp_percent": 0.2, "end_delay": 0.015}` | `{"status": "success", "angle": 18.0, "clockwise": true}` |
| POST | `/test/stream/start` | | `{"status": "success", "stream_urls": [...], "is_streaming": true}` |
| POST | `/test/stream/stop` | | `{"status": "success", "is_streaming": false, "message": "..."}` |
| GET | `/test/stream/status` | | `{"is_streaming": true, "stream_urls": [...]}` |
| GET / POST | `/test/stream/mjpeg`, `/test/stream/active`, `/test/stream/quality`, `/test/stream/sync-status` | as `/scanner/stream/*` | same functions |
| POST | `/test/camera/snap` | | `{"status": "success", "photo_paths": [...], "count": 2}` (starts the cameras if needed; `409` during a scan) |
| POST | `/test/cleanup` | | `{"status": "success", "message": "...", "state": "idle", "is_streaming": false, "lights_on": false}` |

Limits: `z-move` `distance_mm` > 0, ≤ 475, multiples of 0.01, `direction` `up` / `down`; `rotate` `angle` ≤ 360, multiples of 0.1125; `delay` and `start_delay` 0.0001 to 0.05.

### 13.1 Suggested bring-up checklist **(derived)**

1. `GET /test/endstops`: both endstops report the expected idle state.
2. `POST /test/motor/enable?target=all`, then `POST /test/motor/home-bottom`: bottom endstop becomes `true`.
3. `POST /test/motor/move-to-top`: top endstop becomes `true`; measure the travel against the 475 mm limit.
4. `POST /test/turntable/rotate` with 360°: check the table returns to the same mark (a missed-step check at the chosen `delay`).
5. `GET /test/tof`: the reading is valid and plausible.
6. `POST /test/lights/enable`, then `POST /test/stream/start`: check preview, banding and `sync-status` (`offset_ms`, `drops`).
7. `POST /test/camera/snap`: two photos returned.
8. `POST /test/cleanup`: rig back to `idle`, lights off, stream stopped.

---

## 14. Front-end integration guide

### 14.1 Recommended page flows

**Object scan:** form (brightness, start height, camera settings) → `POST /scanner/prepare` → show `stream_urls` → poll until `prepared` → form (angle, Z step, speed) → `POST /scanner/start` → progress from `phase`, `level`, `photos_captured` / `photos_uploaded` → `completed` (or show `error` / `missing`). Keep a **Cancel** button visible from prepare to the end.

**Face scan:** `POST /scanner/face/prepare` → preview, poll until `prepared` → user presses "Take photo" → `POST /scanner/face/capture` → show `phase_message` ("hold still") → `completed` → show `face_result.warning` if any.

**Library:** list with paging → open an object → load the `low` model → button for `high` / STL.

**Network panel:** poll `/system/status`; Wi-Fi list from `/system/wifi/scan` (auto refresh with `rescan=false`, button with `rescan=true`); connect saved networks without a password, ask on `401`; "Forget" via `DELETE`; disable network actions while a scan runs.

### 14.2 Example: face scan in two steps

```js
const post = (url, body) => fetch(url, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
async function waitFor(states) {
  for (;;) {
    const s = await (await fetch("/scanner/status")).json();
    if (states.includes(s.state)) return s;
    await new Promise(r => setTimeout(r, 1000));
  }
}
let r = await post("/scanner/face/prepare", {start_z_mm: 180});
if (r.status !== 202) throw new Error((await r.json()).detail);
let s = await waitFor(["prepared", "error", "cancelled"]);
// ... the user presses "Take photo"
r = await post("/scanner/face/capture", {strategy: "sweep_best_score"});
if (r.status !== 202) throw new Error((await r.json()).detail);
s = await waitFor(["completed", "error", "cancelled"]);
if (s.face_result?.warning) showWarning(s.face_result.warning);
```

`POST /scanner/cancel` stops either step.

### 14.3 Front-end rules

- Show `detail` on every error; handle `503` (no Arduino, AI off, tool missing) and `504`.
- Insert SSIDs and Bluetooth names with `textContent`, never `innerHTML`: a network name can contain HTML.
- Pick the start button from `status.mode`: `object` → `/scanner/start`, `face` → `/scanner/face/capture`.
- Disable network controls while a scan runs.
- Drop any client-side filtering of address-only Bluetooth names (the server now filters).

---

## 15. Reliability, safety and error handling

| Concern | Mechanism |
|---|---|
| Missed motor steps | Cycle-counted step timing on the Arduino, trapezoidal velocity profile |
| Emergency stop | 0xFF out-of-band byte, abort latency ≤ 4.1 ms; `POST /scanner/cancel` |
| Endstop bounce | 20 ms debounce, hardware limits for Z |
| Relay wear / arcing | Zero-current switching with 50 ms settling |
| Banding / flicker | 10 kHz PWM, ripple ≤ 0.5% |
| Exposure drift | Thermal gate: < 3% drift over 10 s before scanning |
| Out of focus | Contrast-based autofocus (variance of Laplacian) |
| Out of memory in the cloud | Memory-aware sizer with a 1.1 safety factor |
| Lost uploads | Verification step, `missing` list (max 50), `upload_drain_timeout_s` watchdog (300 s) |
| Early end of scan | Debounced ToF gate (2 consecutive readings beyond 535 mm) |
| Missing Arduino | Motor requests answer `503` naming the Arduino; the server keeps running |
| Concurrency | `409` when another scan or motion runs, or during an emergency stop |
| Network actions during a scan | Guarded endpoints answer `409` so a Wi-Fi change cannot interrupt uploads |

---

## 16. Key figures and formulas

| Quantity | Value | Source / derivation |
|---|---|---|
| Turntable microstep | 0.1125° | 1.8° ÷ 16 |
| 18° stop | 160 microsteps, 224 ms | source |
| Cruise speed | 112.5°/s | 0.1125° per 1 ms |
| Rim arc resolution | 0.255 mm | r = 130 mm |
| Z resolution | 0.01 mm | 8 mm ÷ (200 × 4) |
| Z travel | 475 mm | API limit |
| MCU timing resolution | 62.5 ns | 1 ÷ 16 MHz |
| Serial link | 115,200 baud | source |
| Abort latency | ≤ 4.1 ms | source |
| Camera resolution | 4056 × 3040 | source |
| GSD | ≈ 0.12 mm/px | 300 mm × 1.55 µm ÷ 3.9 mm |
| Depth precision | σ_Z ≈ 0.17 mm | source |
| PWM | 10 kHz, ripple ≤ 0.5% | source |
| Relay settling | 50 ms | source |
| ToF early-stop threshold | 535 mm, 2 consecutive readings | source |
| Thermal gate | < 3% over 10 s | source |
| Autofocus codes | 0 to 1020 step 30 (35 positions) | source, count derived |
| RAM model | M_req = 1.1(4 GB + N·R·K + P/1.5·10⁶) | source |
| Consumer-GPU limit | ≤ 64 GB | source |
| Draco | 14 bits, ≤ 7.9 µm over 260 mm | source |
| Stops per ring (18°) | 20 | 360 ÷ 18 |

---

## 17. Known limitations, planned work, open points

### 17.1 Status of the latest API changes

The server changes of 28 and 29 October 2026 are **covered by the server's tests but not yet confirmed on the physical rig**.

### 17.2 Planned, not yet available

- Export of selected models as a QR download link or to a USB stick.
- Model thumbnails.
- Deleting models from the library.
- Removal of `GET` for `/system/{wifi|bluetooth}/power/{on|off}` (use `POST`).

### 17.3 Open points to confirm

1. **Symbols of the RAM model** (N, R, K, P) are not defined in the sources.
2. **Reconstruction location:** the architecture describes cloud reconstruction on RunPod, while the API calls it legacy (`launch_runpod: false`, `processing` / `downloading` states "only with the legacy cloud reconstruction") and serves models from the Pi. Clarify which reconstruction path is the default and where finished models are produced.
3. **Endstop mapping:** which of GPIO4 and GPIO22 is the bottom and which is the top endstop.
4. **`total_photos` semantics:** the status example shows `total_photos: 40` at ring 2; document how it is computed (stops per ring, number of rings, number of cameras).
5. **Authentication:** the API has no authentication and no CORS. This is acceptable only because the server listens on the Pi itself; document the intended access model if remote access is ever added.

---

## 18. Change log

### 28-29 October 2026 (API)

**Breaking changes**

| Where | Before | Now |
|---|---|---|
| Face scan | One call `POST /scanner/face/capture` with `start_z_mm`, `home`, `illumination_brightness`, `upload_r2` | Two calls: `/scanner/face/prepare` takes those settings, then `/scanner/face/capture`. Old keys on capture: 422. Capture without a face prepare: 400 |
| `POST /scanner/start` | Allowed after any prepare | 400 after a face prepare |
| `POST /api/ai/chat` | Always 200 | 503 with `detail` when the assistant is off |
| `POST /system/wifi/connect` | 401 for every error | 401 only for a wrong or missing password; 400, 504, 409 otherwise |
| Network actions during a scan | Always run | 409 |
| `GET /system/bluetooth/scan` | Every remembered device, address-only names included | Named devices in range now |
| `GET /system/wifi/scan` | Radio scan on every call | Instant list; `?rescan=true` for a radio scan |
| Motor requests, no Arduino | 500 or server down | 503 naming the Arduino; the server keeps running |

**New endpoints**

- `POST /scanner/face/prepare`
- `GET /library/objects`, `GET /library/objects/{id}`, `GET /library/objects/{id}/model`
- `GET /system/status`
- `GET /system/wifi/saved`, `DELETE /system/wifi/saved/{name}`
- `GET /api/ai/status`

**Changed behaviour**

- `GET /scanner/status`: new fields `mode`, `face_result` (with `face_detected` and `warning`), `seconds_since_last_upload`, `upload_drain_timeout_s`.
- Wi-Fi scan: new `saved` flag, SSIDs with `:` now work.
- Bluetooth scan: `rssi` filled, `duration` 1 to 20 s, debug flags.
- Bluetooth connect: pairs only when needed.
- Ethernet status: adds `ip`; `interface: null` and `state: "unavailable"` when there is no port.

### Front-end migration checklist

1. Face scan in two steps; remove `home`, `start_z_mm`, `illumination_brightness`, `upload_r2` from the capture request and send the last three to the prepare.
2. Use `status.mode` to choose between `/scanner/start` and `/scanner/face/capture`.
3. Show `face_result.warning` when present.
4. Add a Library view with paging, GLTFLoader and STLLoader, `low` first.
5. Network panel: poll `/system/status`, connect saved networks without a password, ask on 401, add Forget, use `rescan=false` for auto refresh, disable actions during a scan.
6. Bluetooth: drop client-side filtering of address-only names.
7. Use `POST` for Wi-Fi and Bluetooth power.
8. Show `detail` for every error; handle 503 and 504.
9. Insert SSIDs and Bluetooth names with `textContent`.

*Repository reference (from the source artifact):* `router/docs/FRONTEND_CHANGES.md`, with full references in `SCANNER_API.md`, `LIBRARY_API.md`, `SYSTEM_API.md` and `AI_API.md`.
