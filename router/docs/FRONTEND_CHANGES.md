# API changes for the front-end (2026-10-28 / 2026-10-29)

What changed in the scanner API since the last front-end update, what breaks, and how to use the new endpoints. The full references stay in `SCANNER_API.md`, `LIBRARY_API.md`, `SYSTEM_API.md`, `AI_API.md`; the interactive one is `http://127.0.0.1:8000/docs`.

The server listens on `127.0.0.1:8000` only (the page runs on the Pi); there is no authentication and no CORS. Every error body is `{"detail": "<message>"}`: show `detail` to the user.

> Status: covered by the server's tests, **not yet confirmed on the rig**.

---

## 1. Breaking changes (must be adapted)

| Where | Before | Now |
|---|---|---|
| Face scan | One call `POST /scanner/face/capture` with `start_z_mm`, `home`, `illumination_brightness`, `upload_r2` | Two calls: `POST /scanner/face/prepare` (those settings), then `POST /scanner/face/capture` (how to find the height). Sending the old keys to `/face/capture` is a `422`. `/face/capture` without a face prepare is a `400`. |
| `POST /scanner/start` | Allowed after any prepare | `400` after a face prepare (needs `POST /scanner/prepare`) |
| `POST /api/ai/chat` | Always `200` with `response` | `503` with `detail` when the assistant is off; check `GET /api/ai/status` first |
| `POST /system/wifi/connect` | `401` for any error | `401` only for a wrong / missing password; `400` other failures, `504` timeout, `409` during a scan |
| Network endpoints during a scan | Always run | `409` for scans and connection changes while a scan runs (see 5.1) |
| `GET /system/bluetooth/scan` | Every device BlueZ ever saw, including address-only ones | Only named devices in range now (query flags restore the old list) |
| `GET /system/wifi/scan` | Radio scan every call | Instant list by default; `?rescan=true` for a real radio scan |
| Motor requests without the Arduino | `500` / server down | `503` with a message naming the Arduino; the server keeps running |

Nothing was removed from `GET /scanner/status`; new fields have safe defaults.

---

## 2. `GET /scanner/status`: new fields

```json
{
  "state": "prepared",
  "mode": "face",
  "phase": "prepared",
  "phase_message": null,
  "face_result": null,
  "seconds_since_last_upload": null,
  "upload_drain_timeout_s": 300.0,
  "...": "older fields unchanged"
}
```

| Field | Meaning |
|---|---|
| `mode` | What the last prepare readied the rig for: `"object"`, `"face"` or `null`. Use it to show the right "start" button: `object` → `POST /scanner/start`, `face` → `POST /scanner/face/capture`. It stays set after the run. |
| `face_result` | After a face scan: `optimal_z_mm`, `best_score`, **`face_detected`**, **`warning`**, `photos`, `r2_uploaded` (see 3.3) |
| `seconds_since_last_upload` / `upload_drain_timeout_s` | Upload progress: e.g. "no upload progress for 2 min of 5" |

Poll every 1-2 s while `preparing` / `running`, every ~10 s otherwise.

---

## 3. Face scan (new two-step flow)

```
POST /scanner/face/prepare ─► poll until state "prepared", mode "face" ─► (subject sits)
POST /scanner/face/capture ─► poll until "completed" | "error" | "cancelled" ─► read face_result
```

### 3.1 `POST /scanner/face/prepare` → `202`

```json
{
  "enable_stream": true,
  "illumination_brightness": 0.5,
  "start_z_mm": 180.0,
  "upload_r2": true,
  "camera_config": {"focus": "auto"}
}
```

| Field | Default | Rules |
|---|---|---|
| `enable_stream` | `true` | The answer has `stream_urls` (MJPEG, use as `<img src>`) |
| `illumination_brightness` | `0.5` | `0 < b <= 1` |
| `start_z_mm` | `180.0` | `100..350`, multiples of `0.01` |
| `upload_r2` | `true` | Upload the photo pair to the cloud |
| `camera_config` | as `/scanner/prepare` | `focus: "auto"` → autofocus on the face at capture time; an integer (or `[cam1, cam2]`) → fixed focus |

Answer (same as `/scanner/prepare`):
```json
{"status": "accepted", "message": "...", "state": "preparing", "stream_urls": ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"]}
```
Errors: `400` MediaPipe/model missing on the Pi, or a wrong state; `409` busy / emergency stop in progress; `422` invalid body (unknown keys too); `503` no Arduino. The carriage homes and climbs in the background: wait for `state: "prepared"`.

### 3.2 `POST /scanner/face/capture` → `202`

```json
{"strategy": "sweep_best_score", "search_range_mm": 150.0, "step_mm": 8.0, "manual_z": null, "settle_s": 0.15}
```

| Field | Default | Meaning |
|---|---|---|
| `strategy` | `"sweep_best_score"` | `sweep_best_score`: search the range, go back to the best height. `direct_stop`: stop as soon as the face is centred. `none`: photo right where the carriage is. |
| `search_range_mm` | `150.0` | `10..250`; `start_z_mm + search_range_mm` must be `<= 475` (otherwise `422`) |
| `step_mm` | `8.0` | `1..25` |
| `manual_z` | `null` | `200..475`: go to this height, no search |
| `settle_s` | `0.15` | `0.05..2` |

Answer: `{"status": "accepted", "message": "...", "state": "running"}`. Errors: `400` not prepared for a face scan (prepare again: each capture needs a new prepare); `409` busy; `422` invalid; `503` no Arduino.

During the capture, `phase_message` says what happens ("Face scan: focusing and locking the cameras at Z=236.0 mm"). **Tell the subject to hold still** from the end of the search until the photo (autofocus + exposure lock take about 10 s).

### 3.3 Result

```json
{
  "state": "completed",
  "mode": "face",
  "phase": "done",
  "phase_message": null,
  "face_result": {
    "optimal_z_mm": 236.0,
    "best_score": 1.7,
    "face_detected": true,
    "warning": null,
    "photos": ["/.../input_images/rig/0/f_236.jpg", "/.../input_images/rig/1/f_236.jpg"],
    "r2_uploaded": true
  }
}
```

* `face_detected: false` is **not an error**: no face was found in the search range, the photo was taken at the start height, `best_score` is `null` and `warning` explains it (also in `phase_message`). Show the warning and offer to prepare again with another `start_z_mm`.
* `face_detected: null`: no search was done (`manual_z` or `strategy: "none"`).
* `warning` can also say the face was seen by one camera only.

### 3.4 Example

```js
const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
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
if (s.state !== "prepared") throw new Error(s.error ?? s.state);

// ... the user presses "Take photo"
r = await post("/scanner/face/capture", {strategy: "sweep_best_score"});
if (r.status !== 202) throw new Error((await r.json()).detail);
s = await waitFor(["completed", "error", "cancelled"]);
if (s.state === "completed" && s.face_result.warning) showWarning(s.face_result.warning);
```

`POST /scanner/cancel` stops both steps.

---

## 4. Library of finished 3D models (new: `/library`)

The models made by the reconstruction, read from the Pi. Load the list without files, then one model at a time.

### 4.1 `GET /library/objects?offset=0&limit=20`

```json
{
  "items": [
    {
      "id": "20261027-202750-21d2de",
      "object": {"title": "Parkside", "created_at": "2026-10-28T14:44:17+00:00",
                 "dimensions_mm": {"length": 71.0, "width": 54.3, "height": 226.9},
                 "volume_cm3": 363.74, "surface_area_cm2": 377.2, "watertight": true},
      "models": {".glb": ["low", "high"], ".stl": ["low", "high"]}
    }
  ],
  "total": 1, "offset": 0, "limit": 20, "next_offset": null
}
```

* Newest first. `limit` `1..100`. Next page: `offset = next_offset` until it is `null`.
* `object` is the object's `object.json` (title, dimensions, volume...); show what you need.
* `models` lists only what exists; disable the buttons of missing ones.

### 4.2 `GET /library/objects/{id}` → one item (`404` if unknown)

### 4.3 `GET /library/objects/{id}/model?format=glb&lod=low`

Returns the file itself (`model/gltf-binary` or `model/stl`), cacheable (`ETag`).

| Query | Values | Default |
|---|---|---|
| `format` | `.glb`, `.stl` (also `glb`, `stl`, `gltf`, `.gltf`, any case) | `.glb` |
| `lod` | `low`, `high` | `low` |

`404` unknown object or missing model, `422` other format/lod. Load `low` first, `high` on demand.

```js
import {GLTFLoader} from "three/addons/loaders/GLTFLoader.js";
import {STLLoader} from "three/addons/loaders/STLLoader.js";
const url = (id, format, lod) => `/library/objects/${id}/model?format=${format}&lod=${lod}`;
new GLTFLoader().load(url(id, "glb", "low"), gltf => scene.add(gltf.scene));
new STLLoader().load(url(id, "stl", "low"), geometry => scene.add(new THREE.Mesh(geometry, material)));
```

Planned (not yet available): export of selected models as a QR download link or to a USB stick, thumbnails, delete.

---

## 5. Network (`/system`)

### 5.1 Rule during a scan

While a scan is `preparing` / `running` / `processing` / `downloading` (or still uploading), these answer **`409`**: `wifi/scan`, `wifi/power`, `wifi/connect`, `DELETE wifi/saved/...`, `bluetooth/scan`, `bluetooth/power`, `bluetooth/connect`, `bluetooth/send-file`, `ethernet/connect`. Disable those controls while `GET /scanner/status` says a scan runs. Reading is always allowed.

### 5.2 `GET /system/status` (new): poll it every 5-15 s

Replaces guessing "the first network in the list is the connected one".

```json
{
  "ethernet": {"interface": "eth0", "cable_plugged": true, "state": "connected", "connection": "Wired connection 1", "ip": "192.168.1.20/24", "connected": true},
  "wifi": {"enabled": true, "interface": "wlan0", "state": "connected", "connected": true, "connection": "Home", "ssid": "Home", "signal": 70, "ip": "192.168.1.31/24"},
  "bluetooth": {"powered": true, "connected": true, "devices": [{"mac_address": "AA:BB:CC:DD:EE:FF", "name": "Speaker"}]},
  "restore": {"wifi": "already connected (Home)", "state": "done", "seconds": 4.2}
}
```

* `connected` (bool) on each part is the value to show; `ssid` / `devices` say to what.
* `state`: `connected`, `connecting`, `disconnected`, `unavailable` (no cable, radio off, no such port).
* A part may be `{"error": "..."}` when its tool failed; the other parts are still valid.
* `restore`: the connections of the last run are made again when the server starts. `null` = nothing to restore; `{"state": "running"}` = still trying (show "reconnecting..."); then one line per connection (`"connected ..."`, `"already connected ..."`, `"failed: ..."`).

### 5.3 Wi-Fi

* `GET /system/wifi/scan?rescan=false` → `[{"in_use": "*", "ssid": "Home", "signal": 70, "security": "WPA2", "saved": true}, ...]`, one entry per SSID, connected first then by signal. Use `rescan=false` for automatic refresh (instant), `rescan=true` for a "Refresh" button (up to ~20 s). SSIDs with `:` now work.
* **New `saved` flag**: `true` → connect without asking for the password.
* `POST /system/wifi/connect` `{"ssid": "Home", "password": ""}` for a saved network; `{"ssid": "Cafe", "password": "..."}` for a new one; add `"identity": "user"` for an enterprise network. `204` on success. **`401` → ask for the password** (also when a saved password stopped working) and send it again. A wrong password never deletes a working saved network.
* New `GET /system/wifi/saved` → `[{"name": "Home", "ssid": "Home", "autoconnect": true}]` (no passwords).
* New `DELETE /system/wifi/saved/{name}` → `204` ("Forget network"); `404` unknown.
* `POST /system/wifi/power/{on|off}`: use `POST` (`GET` still works for now, will be removed).

### 5.4 Bluetooth

* `GET /system/bluetooth/scan?duration=8` (`1..20` s; the request lasts about that long) → named devices in range now, strongest first, plus the connected ones. Debug flags: `include_unnamed=true` (address-only devices), `include_out_of_range=true` (every remembered device).
* `rssi` is now filled (dBm) when BlueZ reports it.
* `POST /system/bluetooth/connect` `{"mac_address": "AA:BB:..."}`: pairs only if needed, then connects (reconnecting a paired device works now).
* `POST /system/bluetooth/power/{on|off}`: use `POST`.

### 5.5 Ethernet

`GET /system/ethernet/status` now also returns `ip`, and `interface: null, state: "unavailable"` when there is no port. `GET /system/status` has the same data.

### 5.6 Error codes of `/system`

| Code | Show |
|---|---|
| `400` | `detail` (the system's message) |
| `401` | "Wrong password": ask again (Wi-Fi) |
| `404` | Unknown saved network |
| `409` | "Not possible during a scan" |
| `503` | A system tool is missing / NetworkManager stopped (configuration problem of the Pi) |
| `504` | "No answer in time, try again" |

---

## 6. AI assistant (changed on 2026-10-28)

* New `GET /api/ai/status` → `{"on": true, "loading": false, "reason": null}`. Enable the chat only when `on`; while `loading` show "starting"; otherwise show `reason`.
* `POST /api/ai/chat` answers `503` with `detail` when the assistant is off (it used to always answer `200`). See `AI_API.md`.

---

## 7. Checklist for the front-end

1. Face scan: two buttons/steps (prepare, then capture); remove `home`, `start_z_mm`, `illumination_brightness`, `upload_r2` from the capture request and put the last three in the prepare.
2. Use `status.mode` to choose `/scanner/start` vs `/scanner/face/capture`.
3. Show `face_result.warning` when present.
4. New "Library" view: `/library/objects` with paging, viewer with `GLTFLoader` / `STLLoader`, `low` first.
5. Network panel: poll `GET /system/status`; show `saved` networks and connect them without a password; handle `401` by asking for it; "Forget" button; `rescan=false` for auto refresh; disable network actions during a scan (`409`).
6. Bluetooth: remove the client-side filtering of address-only names (the server does it).
7. Use `POST` for `wifi/power` and `bluetooth/power`.
8. Show `detail` of every error; handle `503` (no Arduino, AI off, tool missing) and `504`.
9. Escape SSIDs and Bluetooth names when inserting them in the page (`textContent`, not `innerHTML`): a network name can contain HTML (known issue SYS-1).
