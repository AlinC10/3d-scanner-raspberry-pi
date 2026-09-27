# System & Network Management API (`/system`)

The System Router (`../system`) provides REST APIs to configure and manage the Raspberry Pi's host-level network interfaces and connectivity: **Wi-Fi**, **Bluetooth**, and **Ethernet**.

Interactive OpenAPI documentation is accessible at: `http://<pi-ip>:8000/docs`

---

## Architecture & Mounting

The root router is defined in `../system/__init__.py` with prefix `/system` and bundles three sub-routers:

```
FastAPI App (/main_prod.py)
 └── /system (router/system/__init__.py)
      ├── /wifi       -> router/system/wifi.py
      ├── /bluetooth  -> router/system/bluetooth.py
      └── /ethernet   -> router/system/ethernet.py
```

---

## Quick Reference Table

| Subsystem | Method | Endpoint | Description |
|---|---|---|---|
| **Wi-Fi** | `GET` | `/system/wifi/status` | Read Wi-Fi radio power status (`enabled: true/false`) |
| **Wi-Fi** | `GET` | `/system/wifi/power/{state}` | Turn Wi-Fi radio `on` or `off` |
| **Wi-Fi** | `GET` | `/system/wifi/scan` | Scan and list available Wi-Fi access points |
| **Wi-Fi** | `POST` | `/system/wifi/connect` | Connect to a Wi-Fi network using credentials |
| **Bluetooth** | `GET` | `/system/bluetooth/status` | Read Bluetooth controller power status |
| **Bluetooth** | `GET` | `/system/bluetooth/power/{state}` | Turn Bluetooth controller `on` or `off` |
| **Bluetooth** | `GET` | `/system/bluetooth/scan` | Discover nearby Bluetooth devices |
| **Bluetooth** | `POST` | `/system/bluetooth/connect` | Pair and connect to a Bluetooth device |
| **Bluetooth** | `POST` | `/system/bluetooth/send-file` | Push a file to a connected device via OBEX |
| **Ethernet** | `GET` | `/system/ethernet/status` | Check physical cable link and assigned IP |
| **Ethernet** | `POST` | `/system/ethernet/connect` | Connect to enterprise 802.1X wired network |

---

## 1. Wi-Fi API (`/system/wifi`)

Manages the Raspberry Pi's `wlan0` wireless interface using Linux NetworkManager / `nmcli`.

### `GET /system/wifi/status`
Returns whether the wireless radio is currently powered on.

* **Response (`200 OK`)**:
  ```json
  {
    "enabled": true
  }
  ```

---

### `GET /system/wifi/power/{state}`
Turns the Wi-Fi radio on or off.

* **Path Parameters**:
  * `state`: `"on"` | `"off"`
* **Response (`200 OK`)**:
  ```json
  {
    "state": "on"
  }
  ```
* **Example `curl`**:
  ```bash
  curl -X GET http://localhost:8000/system/wifi/power/on
  ```

---

### `GET /system/wifi/scan`
Triggers an active scan of nearby 2.4 GHz and 5 GHz wireless networks.

* **Response (`200 OK`)**:
  ```json
  [
    {
      "in_use": "*",
      "ssid": "Studio-Mesh-5G",
      "signal": 85,
      "security": "WPA2"
    },
    {
      "in_use": "",
      "ssid": "Workshop_Guest",
      "signal": 42,
      "security": "WPA3"
    }
  ]
  ```
  * `in_use`: `"*"` indicates the currently connected network; empty string `""` otherwise.
  * `signal`: Signal strength percentage (`0`–`100`).
  * `security`: Encryption type (e.g. `"WPA2"`, `"WPA3"`, `"OPEN"`).

---

### `POST /system/wifi/connect`
Connects the scanner to a wireless network.

* **Request Body (`NetworkCredentials`)**:
  ```json
  {
    "ssid": "Studio-Mesh-5G",
    "password": "SecretPassword123",
    "identity": null
  }
  ```
  * `ssid` (string, required): Network name.
  * `password` (string, optional): WPA/WPA2/WPA3 passphrase.
  * `identity` (string, optional): Used for enterprise 802.1X authentication if required.

* **Response (`204 No Content`)**:
  Empty body confirming successful network association and DHCP lease acquisition.

* **Error Response (`401 Unauthorized`)**:
  Occurs if the password is incorrect or authentication fails.
  ```json
  {
    "detail": "Connection failed: 802-11-wireless-security.psk: invalid key"
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/system/wifi/connect \
       -H "Content-Type: application/json" \
       -d '{"ssid": "Studio-Mesh-5G", "password": "SecretPassword123"}'
  ```

---

## 2. Bluetooth API (`/system/bluetooth`)

Manages the onboard Bluetooth controller via `bluetoothctl` and BlueZ.

### `GET /system/bluetooth/status`
Returns whether the Bluetooth adapter is powered.

* **Response (`200 OK`)**:
  ```json
  {
    "powered": true
  }
  ```

---

### `GET /system/bluetooth/power/{state}`
Enables or disables the Bluetooth radio.

* **Path Parameters**:
  * `state`: `"on"` | `"off"`
* **Response (`200 OK`)**:
  ```json
  {
    "powered": "on"
  }
  ```

---

### `GET /system/bluetooth/scan`
Scans for discoverable Bluetooth devices in proximity.

* **Response (`200 OK`)**:
  ```json
  [
    {
      "mac_address": "AA:BB:CC:DD:EE:FF",
      "name": "Alin's Phone",
      "icon": "phone",
      "paired": true,
      "connected": false,
      "rssi": -58,
      "device_class": 5898764
    }
  ]
  ```

---

### `POST /system/bluetooth/connect`
Pairs with and connects to a target Bluetooth device.

* **Request Body (`BluetoothDevice`)**:
  ```json
  {
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "name": "Alin's Phone"
  }
  ```
* **Response (`200 OK`)**: Returns the paired `BluetoothDevice` object.

---

### `POST /system/bluetooth/send-file`
Transfers a file (such as a generated 3D model or diagnostic log) to a connected device via OBEX file transfer.

* **Request Body (`SendFileRequest`)**:
  ```json
  {
    "device": {
      "mac_address": "AA:BB:CC:DD:EE:FF"
    },
    "file_path": "/home/alin/Desktop/3d-scanner-raspberry-pi/output/scan.zip"
  }
  ```
* **Response (`204 No Content`)**: Empty response indicating successful file transmission.
* **Error Response (`400 Bad Request`)**:
  ```json
  {
    "detail": "Transfer failed: Device not reachable or OBEX rejected"
  }
  ```

---

## 3. Ethernet API (`/system/ethernet`)

Manages the physical RJ45 wired interface (`eth0`).

### `GET /system/ethernet/status`
Checks if a physical network cable is plugged into the port and whether an IPv4 address has been assigned by DHCP.

* **Response (`200 OK`)**:
  ```json
  {
    "connected": true,
    "ip_address": "192.168.1.150",
    "speed": "1000Mbps",
    "duplex": "full"
  }
  ```

---

### `POST /system/ethernet/connect`
Configures authentication credentials for enterprise wired networks (802.1X EAP-TLS / PEAP).

* **Request Body (`NetworkCredentials`)**:
  ```json
  {
    "ssid": "Wired-802.1X",
    "identity": "scanner_user",
    "password": "CorporatePassword"
  }
  ```
* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "message": "Enterprise Ethernet connected"
  }
  ```

---

## Pydantic Data Models

All system request and response schemas are located in [`../../schemas/system`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/schemas/system/).

### `NetworkCredentials` ([`../../schemas/system/network.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/schemas/system/network.py))
```python
class NetworkCredentials(BaseModel):
    ssid: str
    password: Optional[str] = ""
    identity: Optional[str] = None
```

### `WIFICharacteristics` ([`../../schemas/system/wifi.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/schemas/system/wifi.py))
```python
class WIFICharacteristics(BaseModel):
    in_use: Optional[str] = ""  # '*' if currently active network
    ssid: str
    signal: int
    security: str
```

### `BluetoothDevice` ([`../../schemas/system/bluetooth.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/schemas/system/bluetooth.py))
```python
class BluetoothDevice(BaseModel):
    mac_address: str  # RegEx validated: ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$
    name: str = "Unknown"
    icon: Optional[str] = None
    paired: bool = False
    connected: bool = False
    rssi: Optional[int] = None
    device_class: Optional[int] = None
```

---

## Error Handling & Exception Mapping

System errors originating from underlying OS command calls inherit from `SystemConnectionError` ([`../../system/exception.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/system/exception.py)):

* `WIFIConnectionError` $\to$ Mapped to `HTTP 401 Unauthorized` in `../system/wifi.py`.
* `BluetoothConnectionError` $\to$ Mapped to `HTTP 400 Bad Request` via global exception handler in `../../main_prod.py`.
* `SystemConnectionError` $\to$ Caught by `system_connection_error_handler` in `../../main_prod.py`, returning structured JSON:
  ```json
  {
    "detail": "Error description from hardware or system driver"
  }
  ```

---

## Roadmap & Planned Enhancements ([`../system/TODOS.md`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/router/system/TODOS.md))

* **Aggregated Health Endpoint**: Add `GET /system/status` in `../system/__init__.py` to fetch Wi-Fi, Bluetooth, and Ethernet status in a single round-trip.
* **Credential Vaulting**: Store verified Wi-Fi credentials locally on the Pi to eliminate re-prompting on reconnect.
* **USB Router (`../system/usb.py`)**: Implement auto-mount and status reporting for external USB flash drives (exporting 3D models directly to a thumb drive).
