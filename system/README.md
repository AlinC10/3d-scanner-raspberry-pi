# System Drivers & OS Integration (`system/`)

The `system/` package serves as the **low-level hardware-and-OS driver layer** for the 3D scanner on Raspberry Pi OS. While `router/system/` handles HTTP serialization and REST validation, this package directly controls Linux kernel subsystems, network daemons, and system utilities (`nmcli`, `bluetoothctl`, `bt-obex`, and `sysfs`).

---

## 📦 System Dependencies & Prerequisites

To enable these modules on a clean Raspberry Pi OS installation, the following system packages and services must be present:

```bash
# NetworkManager for Wi-Fi and Ethernet configuration
sudo apt-get install -y network-manager

# BlueZ and OBEX tools for Bluetooth pairing and file transfers
sudo apt-get install -y bluez bluez-tools

# Ensure the running user is in the required permission groups
sudo usermod -aG netdev,bluetooth $USER
```

---

## 📁 Module Summary

| Module | Core Responsibility | Underlying System Interface |
|---|---|---|
| [`wifi.py`](#1-wifi-systemwifi) | Wi-Fi scanning, radio toggling, WPA/WPA2/802.1X connection | `nmcli` (NetworkManager CLI) |
| [`bluetooth.py`](#2-bluetooth-systembluetooth) | Device discovery, pairing, trust, and OBEX file transfer | `bluetoothctl` (BlueZ) & `bt-obex` |
| [`ethernet.py`](#3-ethernet-systemethernet) | Physical carrier detection & Enterprise 802.1X profile setup | Linux `/sys/class/net/` & `nmcli` |
| [`file_control.py`](#4-file-control-systemfile_control) | Directory sanitization with root-deletion guards | `pathlib` & `shutil` |
| [`exception.py`](#5-exceptions-systemexception) | Centralized subprocess error inspection & exception hierarchy | `subprocess.CompletedProcess` |

---

## 1. Wi-Fi (`system.wifi`)

Controls the wireless interface via NetworkManager CLI (`nmcli`).

### `get_status() -> bool`
Queries whether the Wi-Fi radio is enabled.
* **Underlying Command**: `nmcli radio wifi`
* **Returns**: `True` if enabled, `False` otherwise.
* **Raises**: `WIFIConnectionError` on command failure.

### `set_wifi_power(state: Literal["on", "off"]) -> None`
Enables or disables the physical Wi-Fi radio.
* **Underlying Command**: `nmcli radio wifi on|off`
* **Raises**: `WIFIConnectionError` if NetworkManager rejects state change.

### `scan_wifi() -> dict[str, WIFICharacteristics]`
Performs an active scan of all nearby 2.4 GHz and 5 GHz networks.
* **Underlying Command**: `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi`
* **Behavior**: Parses colon-delimited tabular output, filters out hidden SSIDs, and deduplicates networks by SSID.
* **Returns**: Dictionary mapping `SSID -> WIFICharacteristics(in_use, ssid, signal, security)`.

### `connect_wifi(details: NetworkCredentials) -> None`
Associates with a wireless network.
* **Standard WPA-PSK / Open**:
  * Runs: `nmcli dev wifi connect <ssid> password <password>`
* **Enterprise 802.1X (PEAP/MSCHAPv2)**:
  * When `details.identity` is provided:
  * Deletes any existing conflict connection profile: `nmcli connection delete enterprise-<ssid>`
  * Creates a persistent 802.1X connection profile:
    ```bash
    nmcli connection add type wifi con-name enterprise-<ssid> ssid <ssid> \
        wifi-sec.key-mgmt wpa-eap 802-1x.eap peap 802-1x.phase2-auth mschapv2 \
        802-1x.identity <identity> 802-1x.password <password>
    ```
  * Activates the connection: `nmcli connection up enterprise-<ssid>`
* **Raises**: `WIFIConnectionError` if connection fails or credentials are rejected.

---

## 2. Bluetooth (`system.bluetooth`)

Interacts with the Linux BlueZ stack via `bluetoothctl` and the `bt-obex` daemon.

### `power_on() -> bool` / `power_off() -> bool`
Turns the Bluetooth controller on or off.
* **Underlying Command**: `bluetoothctl power on|off`
* **Returns**: `True` if state transition succeeded or was already in target state.

### `get_power_status() -> bool`
Queries the adapter's power flag.
* **Underlying Command**: `bluetoothctl show`
* **Returns**: `True` if `"Powered: yes"` is found in output.

### `scan_devices(duration: int = 8, allowed_icons: Optional[list[str]] = None) -> list[BluetoothDevice]`
Runs a timed discovery scan for nearby devices.
* **Sequence**:
  1. Starts discovery: `bluetoothctl --timeout <duration> scan on`
  2. Lists cached devices: `bluetoothctl devices`
  3. Queries device metadata per MAC: `bluetoothctl info <mac>`
  4. Parses device icon, pairing status, connection status, RSSI signal (dBm), and Class of Device (CoD).
* **Returns**: List of `BluetoothDevice` Pydantic models.

### `pair_device(mac_address: str) -> None`
Pairs with and marks the device as trusted.
* **Underlying Commands**:
  * `bluetoothctl pair <mac_address>`
  * `bluetoothctl trust <mac_address>`

### `connect_device(mac_address: str) -> None`
Establishes a baseband connection with a paired device.
* **Underlying Command**: `bluetoothctl connect <mac_address>`

### `send_file(mac_address: str, file_path: str | Path) -> None`
Pushes a file directly to the remote device over the Bluetooth OBEX Object Push Profile (OPP).
* **Underlying Command**: `bt-obex -p <mac_address> <file_path>`
* **Use Case**: Allows exporting generated `.zip` 3D scans directly to a smartphone or tablet without network access.
* **Raises**: `BluetoothConnectionError` if the device rejects the transfer or is unreachable.

---

## 3. Ethernet (`system.ethernet`)

Manages physical wired RJ45 connectivity.

### `get_ethernet_interface() -> str`
Auto-detects the active physical Ethernet interface name using `nmcli -t -f DEVICE,TYPE dev`.
* **Fallback**: Returns `"eth0"` if no interface is explicitly discovered.

### `get_status() -> dict`
Performs a dual-layer status check:
1. **Physical Link Layer (`sysfs`)**: Reads `/sys/class/net/<interface>/carrier` (`1` = cable inserted, `0` = unplugged).
2. **Network Layer (`nmcli`)**: Checks connection state (`connected`, `connecting`, `disconnected`) and active profile name.
* **Returns**:
  ```python
  {
      "interface": "eth0",
      "cable_plugged": True,
      "state": "connected",
      "connection": "Wired connection 1"
  }
  ```

### `connect_secure_ethernet(details: NetworkCredentials) -> None`
Configures and activates an 802.1X authenticated Ethernet connection for enterprise environments.
* **Underlying Command**:
  ```bash
  nmcli connection add type ethernet con-name enterprise-wired ifname <iface> \
      802-1x.eap peap 802-1x.phase2-auth mschapv2 \
      802-1x.identity <identity> 802-1x.password <password>
  nmcli connection up enterprise-wired
  ```

---

## 4. File Control (`system.file_control`)

### `create_clean_dir(dir_path: str | Path) -> Path`
Safely purges and recreates a directory for scratch files or scan output.
* **Safety Guards**:
  * Evaluates `.resolve()` absolute path.
  * **Explicitly forbids**: `/` (root), `~` (user home directory), or paths with $\le 2$ components (e.g. `/home`, `/var`).
  * Raises `ValueError("Dangerous path deletion prevented: ...")` if an unsafe path is passed.
* **Behavior**: Completely wipes existing folder via `shutil.rmtree` and creates a fresh, empty folder via `mkdir(parents=True)`.

---

## 5. Exceptions & Error Propagation (`system.exception`)

All driver-level exceptions inherit from `SystemConnectionError`:

```
Exception
 └── SystemConnectionError
      ├── WIFIConnectionError
      ├── BluetoothConnectionError
      └── EthernetConnectionError
```

### `check_and_raise_error(res: CompletedProcess, msg: str, err_type: type[Exception]) -> None`
Helper function that inspects a `subprocess.CompletedProcess` instance. If `res.returncode != 0`, it extracts `stderr` (or `stdout`) and raises the specified error type with the command output attached.
