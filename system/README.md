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
| `nm.py` | One runner for every network command: deadline, C locale, missing tool -> 503, timeout -> 504, `nmcli -t` parser that respects `\:` | `subprocess`, `nmcli` |
| `wifi.py` | Radio, scan (`--rescan no` by default), saved profiles, connect (saved profile without password, failed new profile removed, old password kept on a refused new one) | `nmcli` |
| `bluetooth.py` | Discovery (only named devices seen now), pairing, connection, OBEX push | `bluetoothctl`, `bt-obex` |
| `ethernet.py` | Device list, built-in port, carrier, state, IPv4, 802.1X profile (replaced only once the new one connected) | `/sys/class/net/`, `nmcli` |
| `status.py` | `get_network_status()`: every connection in one read-only call (`GET /system/status`) | the modules above |
| `network_state.py` | Remembers the connections made (`state/network.json`, no passwords) and makes them again at startup (`restore()`, background thread) | the modules above |
| `file_control.py` | Directory sanitization with root-deletion guards | `pathlib` & `shutil` |
| `exception.py` | `SystemConnectionError` (with an HTTP `status_code`) and `check_and_raise_error` | `subprocess.CompletedProcess` |

The HTTP side (routes, the 409 guard during a scan, error codes) is documented in `../router/docs/SYSTEM_API.md`.

---

## 1. Runner (`system.nm`)

* `run(cmd, timeout)`: `subprocess.run` with `LC_ALL=C`; `CommandTimeoutError` (504) on a timeout (the child is killed), `ToolUnavailableError` (503) when the program is missing.
* `nmcli(args, timeout=15, error_type=..., check=None)`: with `check`, a non-zero exit raises `error_type(check + ": " + message)`; "NetworkManager is not running" becomes `ToolUnavailableError`.
* `split_terse(line)`: splits `nmcli -t` output on unescaped `:` (`My\:Net:80` -> `["My:Net", "80"]`).
* Deadlines: `NMCLI_TIMEOUT_S` 15, `WIFI_SCAN_TIMEOUT_S` 25, `WIFI_CONNECT_WAIT_S` 40 (`nmcli --wait`), `WIFI_CONNECT_TIMEOUT_S` 50.

## 2. Wi-Fi (`system.wifi`)

* `get_status()`, `set_wifi_power(state)`.
* `scan_wifi(rescan=False) -> dict[ssid, WIFICharacteristics]`: one entry per SSID (strongest), hidden SSIDs left out, `saved` set from the profiles. `rescan=True` forces a radio scan.
* `list_saved_networks() -> list[SavedNetwork]`, `find_saved_network(ssid)`, `delete_saved_network(name)` (404 when unknown).
* `connect_wifi(details) -> profile name`: see the rules in `SYSTEM_API.md`. `WIFIAuthenticationError` (401) when the password is refused.

## 3. Bluetooth (`system.bluetooth`)

* `run_bluetoothctl(args, timeout)`: deadline (`BLUETOOTHCTL_TIMEOUT_S` 10, pair 40, connect 30); timeout -> `BluetoothConnectionError` with `status_code` 504; missing tool -> 503.
* `scan_devices(duration=8, include_unnamed=False, include_out_of_range=False)`: discovery of `duration` s (max 20) whose output (`[NEW]` / `[CHG] Device ...`) tells which devices are in range; a device with an RSSI or connected also counts. Unnamed devices (`is_unnamed`: no name or the address as name) are left out. The `info` calls share one `INFO_BUDGET_S` (10 s). RSSI parsed from `-60` and `0xffffffc4 (-60)`.
* `connected_devices()`: `bluetoothctl devices Connected`.
* `is_paired(mac)`, `pair_device(mac)` (pair + trust), `connect_device(mac)`, `send_file(mac, path)` (300 s deadline).

## 3b. Ethernet (`system.ethernet`)

* `list_devices()`: every NetworkManager device `{device, type, state, connection}` (exit code checked).
* `get_ethernet_interface()`: `eth*`/`en*` first, any other Ethernet device otherwise, `None` when there is none.
* `get_status() -> {interface, cable_plugged, state, connection, ip}`; `device_ipv4(device)`.
* `connect_secure_ethernet(details) -> "enterprise-wired"`, `activate_profile(name)`.

## 3c. Status and restore (`system.status`, `system.network_state`)

* `get_network_status()`: `{ethernet, wifi, bluetooth, restore}`; a part that fails is `{"error": ...}`.
* `network_state.remember_*()` are called by the routes after a success; `restore()` makes the saved connections again (skips those already up, records each outcome in `last_restore`); `start_restore()` runs it in a daemon thread at startup (from `main_prod.lifespan`), not in development mode and not when nothing was saved.

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
 └── SystemConnectionError (status_code 400)
      ├── WIFIConnectionError
      │    └── WIFIAuthenticationError (401)
      ├── BluetoothConnectionError
      ├── EthernetConnectionError
      ├── CommandTimeoutError (504)
      └── ToolUnavailableError (503)
```

### `check_and_raise_error(res: CompletedProcess, msg: str, err_type: type[Exception]) -> None`
Helper function that inspects a `subprocess.CompletedProcess` instance. If `res.returncode != 0`, it extracts `stderr` (or `stdout`) and raises the specified error type with the command output attached.
