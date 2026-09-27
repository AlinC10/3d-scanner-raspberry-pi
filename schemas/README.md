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

#### `MotorConfig`
Defines timing for stepper motor pulse trains.
* `delay`: `float` (default: `0.0005`s) — Enforced `ge=0.00005`. Lower delays step faster; delays below 50 µs can cause step loss.

#### `MechanicalConfig`
Defines the spatial movement of the scanning rig.
* `angle`: `float` (default: `18.0°`) — Enforced `2.0° < angle <= 40.0°`. (18° yields 20 stops per 360° ring).
* `z_move_mm`: `float` (default: `100.0` mm) — Enforced `0.0 < z_move_mm < 150.0`. Vertical lift between rotational rings.
* `turntable`: `MotorConfig` — Pulse parameters for turntable stepper.
* `z_axis`: `MotorConfig` — Pulse parameters for lead screw Z-axis stepper.

#### `DualCameraConfig`
Complete image capture and optical parameter block.
* `master_id`: `Literal[0, 1]` (default: `0`) — Camera ID for the master sensor.
* `slave_id`: `Literal[0, 1]` (default: `1`) — Camera ID for the slave sensor.
* `quality`: `int` (default: `95`) — Enforced `10 <= quality <= 100` (JPEG compression quality).
* `photo_resolution`: `tuple[int, int]` (default: `(4056, 3040)`) — Full 12MP sensor capture size.
* `video_resolution`: `tuple[int, int]` (default: `(1920, 1080)`) — Stream encoding size.
* `preview_resolution`: `tuple[int, int]` (default: `(1280, 720)`) — Live preview stream resolution.
* `rotation`: `Literal[0, 90, 180, 270]` or `tuple` (default: `0`).
* `focus`: `int | str | tuple | None` — VCM focus position (`0`–`1023`), `"auto"` for sweep, or independent tuple `(300, 450)`.
* `settle_time`: `float` (default: `2.0`s) — Time to allow sensor to meter scene before locking.
* `exposure_time`: `int | None` — Manual exposure in microseconds.
* `analogue_gain`: `float | None` — Manual analog sensor gain.
* `colour_gains`: `tuple[float, float] | None` — Manual red/blue gains.
* `awb_mode`: `str | None` (default: `"auto"`).
* `brightness` / `contrast` / `saturation` / `sharpness`: `float | None` (normalized values).

#### `CloudConfig`
Controls remote Meshroom photogrammetry parameters on RunPod GPU workers.
* `resolution_mp`: `float` (default: `12.0`) — Image resolution in MP. Enforced `0.0 < resolution_mp < 12.0`.
* `mode`: `Literal["single", "rig", "two-sides"]` (default: `"rig"`).
* `depthmap_downscale`: `int` (default: `2`) — Enforced `ge=1`. (2 balances RAM usage with high fidelity).
* `max_input_points`: `int` (default: `10000000`) — Maximum points considered during meshing (`ge=1000`).

---

### 📩 Request Payloads

#### `PrepareRequest`
Payload for `POST /scanner/prepare`.
* `enable_stream`: `bool` (default: `true`) — Whether to boot RTSP encoder and return stream links.
* `bitrate`: `int` (default: `2000000`) — Stream bitrate in bps (`500,000 <= bitrate <= 10,000,000`).
* `camera_config`: `DualCameraConfig` — Nested optical configuration block.

#### `StartRequest`
Payload for `POST /scanner/start`.
* `job_id`: `str` (default: `"meshroom-job"`) — Unique identifier for cloud run and asset naming.
* `mechanical`: `MechanicalConfig` — Mechanical motion settings.
* `cloud`: `CloudConfig` — RunPod computing parameters.

---

### 📤 Response Payloads

#### `PrepareResponse` (`202 Accepted`)
```json
{
  "status": "accepted",
  "message": "Live stream started. Rig homing and camera tuning in progress.",
  "state": "preparing",
  "stream_urls": ["rtsp://<ip>:8554/cam0", "rtsp://<ip>:8554/cam1"]
}
```

#### `StatusResponse` (`200 OK`)
```json
{
  "state": "running",
  "total_photos": 40,
  "stream_urls": ["rtsp://<ip>:8554/cam0", "rtsp://<ip>:8554/cam1"]
}
```

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
