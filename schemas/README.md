# Data Schemas & API Contracts (`schemas/`)

This directory contains the single source of truth for all **Pydantic v2 data models, validation constraints, and API contracts** across the 3D scanner system.

These schemas govern incoming HTTP request payloads, enforce hardware safety boundaries, validate network inputs, and serialize outgoing responses.

---

## 🏗️ Schema Organization

```
schemas/
├── scanner.py          # Core scanning, mechanical, camera, face scan and cloud configurations
├── library.py          # /library: finished 3D objects (ModelRequest normalizes the format), see router/docs/LIBRARY_API.md
├── ai.py               # AI assistant: ChatRequest / ChatResponse / AiStatusResponse
└── system/             # Operating system & networking models
    ├── wifi.py         # Wi-Fi network characteristics
    ├── bluetooth.py    # Bluetooth device discovery & file transfer schemas
    └── network.py      # Shared network credentials (SSID, WPA/802.1X identity)
```

---

## 1. Scanner & Hardware Models (`schemas/scanner.py`)

### 📐 Configuration Models

#### `MechanicalConfig`
Spatial movement of the scanning rig (`extra="forbid"`: an unknown key is a `422`).
* `turntable`: `TurntableScanConfig` — `angle` (default `18.0°`, `0 < angle <= 180`, whole microsteps, must divide a full revolution), `delay`, `acceleration`, `ramp_percent`, `decel_percent`, `start_delay`, `end_delay`.
* `z_axis`: `ZAxisScanConfig` — `distance_mm` (default `100.0`, `0 < distance_mm <= 475` (the Z travel), whole steps), `delay`, `acceleration`, `ramp_percent`, `decel_percent`, `start_delay`, `end_delay`.
* The scan configs reject unknown keys (`extra="forbid"`), but still accept and ignore the legacy `turntable.clockwise` and `z_axis.direction`. The manual test requests (`TurntableRotateRequest`, `ZMoveRequest`) keep those two fields and their previous behaviour.

#### `DualCameraConfig`
Complete image capture and optical parameter block.
* `master_id`: `Literal[0, 1]` (default: `0`) — Camera ID for the master sensor.
* `slave_id`: `Literal[0, 1]` (default: `1`) — Camera ID for the slave sensor.
* `quality`: `int` (default: `95`) — Enforced `10 <= quality <= 100` (JPEG compression quality).
* `photo_resolution`: `(4056, 3040)` (default, full 12MP) or `(2028, 1520)` (2x2 binned, same field of view) — nothing else is accepted.
* `preview_resolution`: `tuple[int, int]` (default: `(960, 720)`) — size of the live MJPEG preview (even values, `160x120` up to `photo_resolution`; with the supported photo sizes the default `960x720` always fits).
* `rotation`: `Literal[0, 90, 180, 270]` or a `(cam1, cam2)` pair (default: `0`) — rotation of the saved photos (not of the live preview).
* `focus`: `int | "auto" | tuple | None` — VCM focus position (`0`–`1023`) or independent pair `(300, 450)`; `null` restores the saved position; `"auto"` sweeps in the in-position recalibration and keeps the best position locked for the scan.
* `settle_time`: `float` (default: `2.0`s, `0 <= settle_time <= 10`) — Time to allow sensor to meter scene before locking.
* `copy_colour_gains`: `bool` (default: `false`) — When AE/AWB are locked, `false`: each camera keeps the white-balance gains of its own AWB (exposure time and gain are still copied from camera 1); `true`: camera 1's gains are copied to camera 2 (which gave it a colour cast). `rpi_sync` mode only.
* `exposure_time`: `int | None` (`> 0`) — Manual exposure in microseconds. `0` is rejected (it gave black photos).
* `analogue_gain`: `float | None` (`1.0`–`16.0`) — Manual analog sensor gain.
* `colour_gains`: `tuple[float, float] | None` (each `> 0`) — Manual red/blue gains; `[null, null]` means not set.
* `awb_mode`: `str | None` (default: `"auto"`).
* `brightness` (`-1`–`1`) / `contrast` (`> 0`, `1.0` neutral) / `saturation` (`>= 0`) / `sharpness` (`>= 0`): `float | None`.
* Removed (nothing used them; still tolerated if a client sends them): `show_preview`, `keep_running`, `autofocus_step`, `autofocus_roi`, `video_resolution`.

#### `CloudConfig`
Turntable Studio on RunPod after the scan (`extra="forbid"`, `cloud/job.py`).
* `launch_runpod`: `bool` (default: `true`; object scans are always reconstructed on RunPod) — `false`: the scan stops (`completed`) once the photos are verified in R2. `true`: one pod is started automatically (no confirmation), its result extracted into `results/<job_id>/`, the R2 job folder deleted; `/start` is refused with `400` when the RunPod / Cloudflare / image variables are missing.
* `resolution_level`: `int` (default: `1`) — `0..3`, Turntable Studio dense level (each level halves both photo sides); also sizes the pod RAM.
* `title`: `str | null` (default: `null` = the job id, the folder name) — Model name in `object.json` (max 120).
* Removed 2026-10-30 (Meshroom): `resolution_mp`, `mode`, `depthmap_downscale`, `max_input_points` (now `422`).
* `StartRequest.job_id`: `str | null` (default `null` = new id) — pattern `YYYYMMDD-HHMMSS-xxxxxx` (the `/library` id).

---

### 📩 Request Payloads

#### `PrepareRequest`
Payload for `POST /scanner/prepare`.
* `enable_stream`: `bool` (default: `true`) — Whether to start the MJPEG live preview and return its URLs. (There is no `bitrate`: the preview is MJPEG, not H.264.)
* `illumination_brightness`: `float` (default: `1.0`) — LED brightness, enforced `0.0 < b <= 1.0` (`0` is refused: the scanner cannot scan in the dark).
* `z_start_mm`: `float` (default: `30.0`, `0 <= z_start_mm <= 100`) — Height above the bottom endstop where the scan starts; `0` stays on the endstop. Must be a whole number of motor steps (multiples of `0.01` mm), validated with the same conversion the motor uses. Not part of `camera_config`.
* `z_delay`: `float` (default `0.0010`, `0.0001..0.05`) — step delay of the climb to the start height.
* `camera_config`: `DualCameraConfig` — Nested optical configuration block.

#### `StreamActiveRequest` / `StreamQualityRequest`
Optional JSON bodies of `POST /scanner/stream/active` (`camera_id`, `1`-`2`) and `POST /scanner/stream/quality` (`quality`, `1`-`100`); the same values are accepted as query parameters. The `/test/stream/*` endpoints are the same functions.

#### `StartRequest`
Payload for `POST /scanner/start` (`extra="forbid"`: unknown keys anywhere in the body are a `422`).
* `job_id`: `str` (default: `"meshroom-job"`) — Unique identifier for cloud run and asset naming.
* `mechanical`: `MechanicalConfig` — Mechanical motion settings.
* `cloud`: `CloudConfig` — RunPod switch (`launch_runpod`) and computing parameters.

#### `FacePrepareRequest`
Payload for `POST /scanner/face/prepare` (`extra="forbid"`).
* `enable_stream`: `bool` (default `true`).
* `illumination_brightness`: `float` (default `0.5`, `0 < b <= 1`).
* `start_z_mm`: `float` (default `180.0`, `100 <= v <= 350`, whole Z steps): where the carriage waits and the search starts.
* `z_delay`: `float` (default `0.005`, `0.0001..0.05`) — step delay of every Z move of the face scan (FACE-19); kept for the capture.
* `upload_r2`: `bool` (default `true`): upload and verify the pair (the bucket is cleaned by the prepare).
* `camera_config`: `DualCameraConfig`. `focus: "auto"` = saved position during the search, autofocus on the face at the height found; an integer or a pair = that position, no autofocus. Manual `exposure_time` / `analogue_gain` / `colour_gains` are used for the photo; otherwise AE/AWB are locked on the face.

#### `FaceCaptureRequest`
Payload for `POST /scanner/face/capture` (`extra="forbid"`; `home`, `start_z_mm`, `upload_r2`, `illumination_brightness` moved to the prepare and are refused here).
* `strategy`: `"sweep_best_score"` (default), `"direct_stop"` or `"none"` (photo at the prepared height).
* `search_range_mm`: `float` (default `150.0`, `10 <= v <= 250`); prepared height + range `<= 475` is checked by the scanner (422).
* `step_mm`: `float` (default `8.0`, `1 <= v <= 25`).
* `manual_z`: `float | null` (default `null`, `200 <= v <= 475`): skips the search.
* `settle_s`: `float` (default `0.15`, `0.05 <= v <= 2`).
* `z_delay`: `float | null` (default `null` = the prepare's value).
* `title`: `str | null` (default `null` = the job id) — name of the face model.
* `reconstruct`: `bool` (default `true`) — local 3D face model on the Pi (`hardware/face_reconstruction.py`, FACE-20); state `processing` while it runs, `error` if it fails.
* Every distance must be a whole number of Z motor steps (multiples of `0.01` mm): `422` before anything moves.

---

### 📤 Response Payloads

#### `PrepareResponse` (`202 Accepted`)
```json
{
  "status": "accepted",
  "message": "Live stream started. Rig homing and camera tuning in progress.",
  "state": "preparing",
  "stream_urls": ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]
}
```

#### `StatusResponse` (`200 OK`)
* New 2026-10-30: `job_id`, `result_id` (`/library` id of the saved model); `face_result` adds `result_id`, `metrics`, `reconstruction_error`; phase `reconstructing_face`.
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
`mode` is what the last prepare readied the rig for (`"object"`, `"face"`, `null`). `face_result` (`FaceCaptureResult`: `optimal_z_mm`, `best_score`, `face_detected`, `warning`, `photos`, `r2_uploaded`) is set once a face scan completed. `seconds_since_last_upload` / `upload_drain_timeout_s` show the upload progress (`null` when no upload runs). The progress fields are optional with safe defaults (`phase`/`phase_message`/`error`: `null`, counters: `0`, lists: `[]`). `missing` lists the R2 keys not found at the final verification; `error` carries the reason of an `error` state. See `router/docs/SCANNER_API.md`.

#### `StartResponse` (`202 Accepted`)
```json
{
  "status": "accepted",
  "message": "Pipeline started."
}
```

#### `FaceCaptureResponse` (`202 Accepted`)
```json
{
  "status": "accepted",
  "message": "Face scan started. Poll GET /scanner/status until the state is completed.",
  "state": "running"
}
```
The face scan runs in the background; its result is `StatusResponse.face_result`.

#### `CancelResponse` (`200 OK`)
```json
{
  "status": "success",
  "state": "cancelled"
}
```

---

## 2. AI Assistant Models (`schemas/ai.py`)

* `ChatRequest`: `question` (`str`, required), `history` (`[{"role": "user" | "assistant", "text": str}]`, default `[]`).
* `ChatResponse`: `response` (`str`).
* `AiStatusResponse`: `on` (`bool`), `loading` (`bool`), `reason` (`str | null`). See `router/docs/AI_API.md`.

---

## 3. System & Networking Models (`schemas/system/`)

### `NetworkCredentials` (`schemas/system/network.py`)
Used for Wi-Fi and 802.1X Ethernet associations.
* `ssid`: `str` — Network SSID name.
* `password`: `Optional[str]` (default: `""`) — WPA/WPA2/WPA3 passphrase.
* `identity`: `Optional[str]` (default: `None`) — Username for Enterprise 802.1X PEAP/MSCHAPv2.

### `WIFICharacteristics` (`schemas/system/wifi.py`)
Output model for `GET /system/wifi/scan`.
* `in_use`: `Optional[str]` — `"*"` if active connection; `""` otherwise.
* `ssid`: `str` — Access point name.
* `signal`: `int` — Signal strength percentage (`0`–`100`).
* `security`: `str` — Protocol (e.g. `"WPA2"`, `"WPA3"`, `"OPEN"`).

### `BluetoothDevice` (`schemas/system/bluetooth.py`)
Model for discovered and paired Bluetooth targets.
* `mac_address`: `str` — Validated against RegEx `^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$`.
* `name`: `str` (default: `"Unknown"`).
* `icon`: `Optional[str]` — Freedesktop category (`"phone"`, `"computer"`).
* `paired`: `bool` — Adapter pairing state.
* `connected`: `bool` — Active baseband connection.
* `rssi`: `Optional[int]` — Signal strength in dBm.
* `device_class`: `Optional[int]` — 24-bit Bluetooth Class of Device (CoD).

#### Library (`schemas/library.py`)
* `UpdateObjectRequest` (`PATCH /library/objects/{id}`, `extra="forbid"`): `title` (1..120), `description` (<= 2000), `dimensions_mm` (`ObjectDimensions`: `length` / `width` / `height` > 0), `volume_cm3` (> 0); all optional.
* `UsbExportRequest`: `device` (`/dev/...`, optional). `UsbDrive`, `UsbExportStatus` (`state`, `bytes_done`, `bytes_total`, `safe_to_remove`, ...).
