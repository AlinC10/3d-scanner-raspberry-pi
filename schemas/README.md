# Data Schemas & API Contracts (`schemas/`)

This directory contains the single source of truth for all **Pydantic v2 data models, validation constraints, and API contracts** across the 3D scanner system.

These schemas govern incoming HTTP request payloads, enforce hardware safety boundaries, validate network inputs, and serialize outgoing responses.

---

## 🏗️ Schema Organization

```
schemas/
├── scanner.py          # Core scanning, mechanical, camera, and cloud configurations
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
Controls the optional remote Meshroom photogrammetry on RunPod GPU workers (`extra="forbid"`).
* `launch_runpod`: `bool` (default: `false`) — When `false` the scan stops (`completed`) once the photos are verified in R2; when `true` the RunPod pipeline runs afterwards (and `/start` is refused with `400` if RunPod is not configured).
* `resolution_mp`: `float` (default: `12.0`) — Image resolution in MP. Enforced `0.0 < resolution_mp <= 13.0` (12.0 nominal, 12.33 real sensor value).
* `mode`: `Literal["single", "rig", "two-sides"]` (default: `"rig"`).
* `depthmap_downscale`: `int` (default: `2`) — Enforced `ge=1`. (2 balances RAM usage with high fidelity).
* `max_input_points`: `int` (default: `10000000`) — Maximum points considered during meshing (`ge=1000`).

---

### 📩 Request Payloads

#### `PrepareRequest`
Payload for `POST /scanner/prepare`.
* `enable_stream`: `bool` (default: `true`) — Whether to start the MJPEG live preview and return its URLs. (There is no `bitrate`: the preview is MJPEG, not H.264.)
* `illumination_brightness`: `float` (default: `1.0`) — LED brightness, enforced `0.0 < b <= 1.0` (`0` is refused: the scanner cannot scan in the dark).
* `z_start_mm`: `float` (default: `30.0`, `0 <= z_start_mm <= 100`) — Height above the bottom endstop where the scan starts; `0` stays on the endstop. Must be a whole number of motor steps (multiples of `0.01` mm), validated with the same conversion the motor uses. Not part of `camera_config`.
* `camera_config`: `DualCameraConfig` — Nested optical configuration block.

#### `StreamActiveRequest` / `StreamQualityRequest`
Optional JSON bodies of `POST /scanner/stream/active` (`camera_id`, `1`-`2`) and `POST /scanner/stream/quality` (`quality`, `1`-`100`); the same values are accepted as query parameters. The `/test/stream/*` endpoints are the same functions.

#### `StartRequest`
Payload for `POST /scanner/start` (`extra="forbid"`: unknown keys anywhere in the body are a `422`).
* `job_id`: `str` (default: `"meshroom-job"`) — Unique identifier for cloud run and asset naming.
* `mechanical`: `MechanicalConfig` — Mechanical motion settings.
* `cloud`: `CloudConfig` — RunPod switch (`launch_runpod`) and computing parameters.

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
```json
{
  "state": "running",
  "total_photos": 40,
  "stream_urls": ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"],
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
The progress fields are optional with safe defaults (`phase`/`phase_message`/`error`: `null`, counters: `0`, lists: `[]`). `missing` lists the R2 keys not found at the final verification; `error` carries the reason of an `error` state. See `router/docs/SCANNER_API.md`.

#### `StartResponse` (`202 Accepted`)
```json
{
  "status": "accepted",
  "message": "Pipeline started."
}
```

#### `CancelResponse` (`200 OK`)
```json
{
  "status": "success",
  "state": "cancelled"
}
```

---

## 2. System & Networking Models (`schemas/system/`)

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

### `SendFileRequest` (`schemas/system/bluetooth.py`)
Payload for `POST /system/bluetooth/send-file`.
* `device`: `BluetoothDevice` — Destination Bluetooth target.
* `file_path`: `str | Path` — Local path to the file/archive being pushed via OBEX.
