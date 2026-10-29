# System & Network Management API (`/system`)

The System Router (`../system`) manages the Raspberry Pi's network connections: **Wi-Fi**, **Bluetooth** and **Ethernet**, through NetworkManager (`nmcli`) and BlueZ (`bluetoothctl`). The drivers are in `../../system/` (see `../../system/README.md`).

Interactive OpenAPI documentation: `http://127.0.0.1:8000/docs`

```
FastAPI App (/main_prod.py)
 └── /system (router/system/__init__.py)        GET /system/status
      ├── /wifi       -> router/system/wifi.py
      ├── /bluetooth  -> router/system/bluetooth.py
      └── /ethernet   -> router/system/ethernet.py
     router/system/guard.py: network_change_allowed() (409 while a scan runs)
```

---

## Quick Reference

| Subsystem | Method | Endpoint | Refused during a scan | Description |
|---|---|---|---|---|
| **All** | `GET` | `/system/status` | no | Every connection in one call (poll it) |
| **Wi-Fi** | `GET` | `/system/wifi/status` | no | Radio on/off |
| **Wi-Fi** | `POST` (or `GET`) | `/system/wifi/power/{on\|off}` | yes | Radio on or off |
| **Wi-Fi** | `GET` | `/system/wifi/scan?rescan=false` | yes | Networks in range |
| **Wi-Fi** | `GET` | `/system/wifi/saved` | no | Saved networks (no passwords) |
| **Wi-Fi** | `DELETE` | `/system/wifi/saved/{name}` | yes | Forget a saved network |
| **Wi-Fi** | `POST` | `/system/wifi/connect` | yes | Connect (no password needed for a saved network) |
| **Bluetooth** | `GET` | `/system/bluetooth/status` | no | Adapter powered |
| **Bluetooth** | `POST` (or `GET`) | `/system/bluetooth/power/{on\|off}` | yes | Adapter on or off |
| **Bluetooth** | `GET` | `/system/bluetooth/scan?duration=8` | yes | Named devices in range now |
| **Bluetooth** | `POST` | `/system/bluetooth/connect` | yes | Pair (if needed) and connect |
| **Bluetooth** | `POST` | `/system/bluetooth/send-file` | yes | OBEX file push |
| **Ethernet** | `GET` | `/system/ethernet/status` | no | Cable, state, profile, IP |
| **Ethernet** | `POST` | `/system/ethernet/connect` | yes | 802.1X wired network |

---

## Rules that apply to every endpoint

### During a scan (`409`)
While the scanner is `preparing`, `running`, `processing` or `downloading`, or still uploading photos, every endpoint that **scans the radio or changes a connection** answers `409` (`router/system/guard.py`). Wi-Fi and Bluetooth share the antenna of the Pi 5: a radio scan slows the upload, and a power or connection change can cut it. **Reading** (`GET /system/status`, the `.../status` endpoints, `GET /system/wifi/saved`) is always allowed and never disturbs the network.

### Error codes
Every network error is `{"detail": "<message>"}` with:

| Code | Meaning |
|---|---|
| `400` | The command failed (message from NetworkManager / BlueZ) |
| `401` | `wifi/connect`: the network refused the password, or a saved password is no longer valid: ask the user for it |
| `404` | `wifi/saved/{name}`: no such saved network |
| `409` | A scan is running (see above) |
| `422` | Invalid body or query (e.g. `duration` above 20) |
| `503` | The tool is missing (`nmcli`, `bluetoothctl`, `bt-obex`) or NetworkManager is not running |
| `504` | The command did not finish in time (every command has a deadline) |

The commands run in the C locale, so the success checks work whatever the language of the system is.

### Deadlines

| Command | Deadline |
|---|---|
| status, radio, profile listing | 15 s |
| `wifi/scan?rescan=true` | 25 s |
| `wifi/connect` | 50 s (NetworkManager may take 40 s) |
| `bluetooth/scan` | `duration` + 10 s for the discovery, + 10 s for all the device details |
| `bluetooth/connect` | pair 40 s, connect 30 s |
| `bluetooth/send-file` | 300 s |

### Connections made again at startup
Every successful connection (`wifi/connect`, `ethernet/connect`, `bluetooth/connect`) and every power switch is remembered in `state/network.json` (only *which* profiles and devices; the passwords stay in NetworkManager, the pairings in BlueZ). When the server starts, a background task makes them again: a connection already up is left alone, a failure is only logged, and the server works as before. The outcome is in `GET /system/status` → `restore`. Forgetting a network removes it from the list. Nothing is restored in development mode (`MOCK_HARDWARE=1`).

---

## `GET /system/status`

Everything the front-end needs to show the connections, in one request. It only reads what the system already knows (no radio scan), so it can be polled every 5-15 s, also during a scan. A part whose tool fails is `{"error": "..."}`; the others are still filled.

```json
{
  "ethernet": {
    "interface": "eth0",
    "cable_plugged": true,
    "state": "connected",
    "connection": "Wired connection 1",
    "ip": "192.168.1.20/24",
    "connected": true
  },
  "wifi": {
    "enabled": true,
    "interface": "wlan0",
    "state": "connected",
    "connected": true,
    "connection": "Home",
    "ssid": "Home",
    "signal": 70,
    "ip": "192.168.1.31/24"
  },
  "bluetooth": {
    "powered": true,
    "connected": true,
    "devices": [{"mac_address": "AA:BB:CC:DD:EE:FF", "name": "Speaker"}]
  },
  "restore": {"wifi": "already connected (Home)", "bluetooth AA:BB:CC:DD:EE:FF": "connected", "state": "done", "seconds": 4.2}
}
```

* `state` (Ethernet, Wi-Fi): the NetworkManager device state: `connected`, `connecting`, `disconnected`, `unavailable` (no cable / radio off / no such port).
* `ethernet.interface` is the built-in port (`eth*`/`en*`); a USB adapter or `usb0` only when there is no other one; `null` when there is no Ethernet device.
* `wifi.signal`: 0-100. `ip`: IPv4 with prefix, `null` when not connected.
* `restore`: `null` when nothing was restored (no saved state, development mode), `{"state": "running"}` while it runs, then one entry per part (`"connected ..."`, `"already connected ..."`, `"failed: <reason>"`).

---

## 1. Wi-Fi (`/system/wifi`)

### `GET /system/wifi/status`
`{"enabled": true}`: whether the radio is on.

### `POST /system/wifi/power/{state}`
`state`: `on` | `off`. Answer `{"state": "on"}`. `GET` still works (the current front-end uses it) but a change should be a `POST`.

### `GET /system/wifi/scan`
The networks in range, **one per SSID** (the strongest access point of a mesh), hidden networks left out, the connected one first, then by signal.

* Query `rescan` (default `false`): `false` returns the list NetworkManager already keeps (it refreshes it by itself, instant, no radio scan); `true` forces a radio scan (up to ~20 s). For an automatic refresh in the UI use `rescan=false`; keep `true` for a "refresh" button.

```json
[
  {"in_use": "*", "ssid": "Home", "signal": 70, "security": "WPA2", "saved": true},
  {"in_use": "", "ssid": "My:Net", "signal": 65, "security": "WPA2", "saved": false},
  {"in_use": "", "ssid": "Guest", "signal": 40, "security": "", "saved": false}
]
```

* `in_use`: `"*"` for the connected network. `security`: `""` for an open network. `saved`: a profile exists, connecting needs no password. SSIDs containing `:` are returned correctly.

### `GET /system/wifi/saved`
`[{"name": "Home", "ssid": "Home", "autoconnect": true}]`. The password is never returned.

### `DELETE /system/wifi/saved/{name}`
Forgets a saved network (`name` = profile name or SSID). `204`; `404` when there is no such network.

### `POST /system/wifi/connect`
Body (`NetworkCredentials`):
```json
{"ssid": "Home", "password": "", "identity": null}
```
* **Saved network** (`saved: true` in the scan): send an empty `password`; the saved profile is used. `401` if the saved password is no longer accepted: ask the user and send it.
* **Saved network with a new password**: the profile is updated; if the new password is refused (`401`) the old one is kept.
* **New network**: a profile is created; if the connection fails it is removed again (NetworkManager does not keep retrying a wrong password).
* **Enterprise (802.1X)**: send `identity` (user) and `password`; profile `enterprise-<ssid>`, same rules.

Answer `204`. Errors: `401` wrong / missing password, `400` other failure (network out of range, radio off), `504` timeout, `409` during a scan.

---

## 2. Bluetooth (`/system/bluetooth`)

### `GET /system/bluetooth/status`
`{"powered": true}`.

### `POST /system/bluetooth/power/{state}`
`state`: `on` | `off`. Answer `{"powered": "on"}`. `GET` still works. A blocked adapter answers `400` with the hint `rfkill unblock bluetooth`.

### `GET /system/bluetooth/scan`
Runs a discovery and lists the devices **seen during it** that have a **real name**, strongest signal first, plus the devices connected right now.

| Query | Default | Meaning |
|---|---|---|
| `duration` | `8` | Discovery seconds, `1..20` |
| `include_unnamed` | `false` | Also list devices that only show their address (`AA-BB-CC-...`) |
| `include_out_of_range` | `false` | Also list devices BlueZ remembers from earlier but that are not in range now |

```json
[
  {"mac_address": "AA:BB:CC:DD:EE:01", "name": "Phone", "icon": "phone", "paired": false, "connected": false, "rssi": -60, "device_class": null},
  {"mac_address": "AA:BB:CC:DD:EE:05", "name": "Speaker", "icon": "audio-card", "paired": true, "connected": true, "rssi": null, "device_class": null}
]
```
`rssi` is in dBm (`null` when BlueZ did not report it).

### `POST /system/bluetooth/connect`
Body: a `BluetoothDevice` (only `mac_address` is needed). Pairs only when the device is not paired yet, then connects; the device is reconnected when the server starts. Answer: the device.

### `POST /system/bluetooth/send-file`
Body: `{"device": {"mac_address": "..."}, "file_path": "..."}`. `204` when sent. (Known issue SYS-2: any path on the Pi is accepted; see `PROBLEME_RAMASE.md`.)

---

## 3. Ethernet (`/system/ethernet`)

### `GET /system/ethernet/status`
```json
{"interface": "eth0", "cable_plugged": true, "state": "connected", "connection": "Wired connection 1", "ip": "192.168.1.20/24"}
```
`interface: null, state: "unavailable"` when there is no Ethernet port. A NetworkManager error is an error (`400` / `503`), not a fake `disconnected`.

### `POST /system/ethernet/connect`
802.1X wired network. Body `{"ssid": "", "password": "...", "identity": "user"}` (`identity` required). The new profile replaces `enterprise-wired` only once it connected; a failed one is removed. Answer `{"status": "success", "message": "Enterprise Ethernet connected"}`.

---

## Data Models

* `NetworkCredentials` (`../../schemas/system/network.py`): `ssid`, `password` (default `""`), `identity` (default `null`).
* `WIFICharacteristics` (`../../schemas/system/wifi.py`): `in_use`, `ssid`, `signal`, `security`, `saved`.
* `SavedNetwork` (`../../schemas/system/wifi.py`): `name`, `ssid`, `autoconnect`.
* `BluetoothDevice` (`../../schemas/system/bluetooth.py`): `mac_address`, `name`, `icon`, `paired`, `connected`, `rssi`, `device_class`.

Open problems of this layer (XSS through SSID names in the front-end, passwords in the process arguments, `send-file` paths, 802.1X certificate validation) are listed in `PROBLEME_RAMASE.md` (levels 4 and 5).
