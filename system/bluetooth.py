import subprocess
import time
from subprocess import CompletedProcess
from typing import Optional
from pathlib import Path
import re
from schemas.system.bluetooth import BluetoothDevice
from system import exception
from system.nm import CommandTimeoutError, ToolUnavailableError

class BluetoothConnectionError(exception.SystemConnectionError):
    pass

ALLOWED_ICONS = {"phone", "computer"}

# Every bluetoothctl call has a deadline: a hung adapter or a device that never answers must not block a request
# thread forever (repeated clicks used to exhaust the thread pool the scanner shares).
BLUETOOTHCTL_TIMEOUT_S = 10.0   # power, show, devices, info, trust
PAIR_TIMEOUT_S = 40.0           # pairing talks to the remote device
CONNECT_TIMEOUT_S = 30.0        # connecting talks to the remote device
SCAN_GRACE_S = 10.0             # extra time on top of the discovery duration
INFO_BUDGET_S = 10.0            # all the `info` calls of one scan together
SCAN_DURATION_DEFAULT_S = 8
SCAN_DURATION_MAX_S = 20

_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
# Lines printed by `bluetoothctl scan on` for a device seen during this discovery
_SEEN_RE = re.compile(r"\[(?:NEW|CHG)\]\s+Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})")


def run_bluetoothctl(args: list[str], timeout: float = BLUETOOTHCTL_TIMEOUT_S, **kwargs) -> CompletedProcess:
    """
    Runs `bluetoothctl <args>` with a deadline (the child is killed when it expires).

    :raises BluetoothConnectionError: when the command does not finish within `timeout` seconds.
    """
    try:
        return subprocess.run(["bluetoothctl", *args], timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as e:
        error = BluetoothConnectionError(f"`bluetoothctl {' '.join(args)}` did not finish within {timeout:g} s")
        error.status_code = CommandTimeoutError.status_code
        error.partial_output = e.output
        raise error
    except FileNotFoundError:
        raise ToolUnavailableError("`bluetoothctl` is not installed on this system (SYS-4).")

def check_and_raise_error(res: CompletedProcess, msg: str):
    exception.check_and_raise_error(res, msg, BluetoothConnectionError)

def power_on() -> bool:
    """Activates the Bluetooth adapter."""
    res = run_bluetoothctl(["power", "on"], capture_output=True, text=True)
    check_and_raise_error(res, f"Error in `power_on`")

    return "succeeded" in res.stdout.lower() or "already on" in res.stdout.lower()


def power_off() -> bool:
    """Deactivates the Bluetooth adapter."""
    res = run_bluetoothctl(["power", "off"], capture_output=True, text=True)
    check_and_raise_error(res, f"Error in `power_off`")

    return "succeeded" in res.stdout.lower()


def get_power_status() -> bool:
    """Returns True if the Bluetooth adapter is powered on."""
    res = run_bluetoothctl(["show"], capture_output=True, text=True)
    check_and_raise_error(res, f"Error in retrieving Bluetooth status")

    return "Powered: yes" in res.stdout


def check_device_class(device_class_line: str):
    match = re.search(r"\((\d+)\)", device_class_line.strip())

    if match:
        return int(match.group(1))
    else:
        # fallback in case only hex is shown (e.g. "x005a020c")
        parts = device_class_line.split(":", 1)[1].strip().split()

        try:
            return int(parts[0], 16)
        except ValueError:
            return None


def is_unnamed(name: Optional[str], mac: str) -> bool:
    """True for a device that has no real name: BlueZ shows its address instead (``AA-BB-CC-DD-EE-FF``)."""
    text = (name or "").strip()
    return not text or text.upper().replace("-", ":") == mac.upper() or _MAC_RE.fullmatch(text) is not None


def _seen_in_discovery(output) -> set[str]:
    """Addresses printed by `bluetoothctl scan on` ([NEW] / [CHG] Device ...): the devices in range right now."""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return {m.upper() for m in _SEEN_RE.findall(output or "")}


def _parse_info(info: str) -> dict:
    icon, rssi, device_class = "unknown", None, None
    for line in info.splitlines():
        if "Icon:" in line:
            icon = line.split(":", 1)[1].strip()
        elif "RSSI:" in line:
            # BlueZ prints "RSSI: -60" or, recently, "RSSI: 0xffffffc4 (-60)" (SYS-7)
            value = line.split(":", 1)[1].strip()
            match = re.search(r"\((-?\d+)\)", value) or re.match(r"(-?\d+)", value)
            rssi = int(match.group(1)) if match else None
        elif "Class:" in line:
            device_class = check_device_class(line)
    return {"icon": icon, "rssi": rssi, "class": device_class,
            "paired": "Paired: yes" in info, "connected": "Connected: yes" in info}


def scan_devices(duration: int = SCAN_DURATION_DEFAULT_S, allowed_icons: Optional[list[str]] = None,
                 include_unnamed: bool = False, include_out_of_range: bool = False) -> list:
    """
    Runs a discovery for `duration` seconds and lists the devices **seen during it** (S4).

    BlueZ remembers every device it ever saw (`bluetoothctl devices`); devices that are not in range now are left out
    (they are recognised by their [NEW]/[CHG] lines in the discovery output, or by an RSSI value), except the ones
    connected right now. Devices without a real name (BlueZ shows the address) are left out too. The whole call has
    one deadline: `duration + SCAN_GRACE_S` for the discovery and INFO_BUDGET_S for the details of the devices.

    :param duration: discovery time in seconds (1..SCAN_DURATION_MAX_S).
    :param include_unnamed: keep the devices that only have an address.
    :param include_out_of_range: keep every device BlueZ remembers (the old behaviour).
    :return: the devices, strongest signal first.
    """
    duration = max(1, min(int(duration), SCAN_DURATION_MAX_S))
    try:
        discovery = run_bluetoothctl(["--timeout", str(duration), "scan", "on"], timeout=duration + SCAN_GRACE_S,
                                     capture_output=True, text=True)
        output = discovery.stdout
    except BluetoothConnectionError as e:
        if isinstance(e, ToolUnavailableError):
            raise
        output = getattr(e, "partial_output", None)   # a discovery that overran is cut off; what it saw counts
    seen = _seen_in_discovery(output)

    res = run_bluetoothctl(["devices"], capture_output=True, text=True)
    check_and_raise_error(res, "Error on Bluetooth device scanning")

    deadline = time.monotonic() + INFO_BUDGET_S
    devices = []
    for line in res.stdout.split("\n"):
        # line format: "Device XX:XX:XX:XX:XX:XX Name"
        match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)$", line.strip())
        if not match:
            continue
        mac, name = match.group(1), match.group(2)
        if not include_unnamed and is_unnamed(name, mac):
            continue
        info = ""
        if time.monotonic() < deadline:
            try:
                info = run_bluetoothctl(["info", mac], capture_output=True, text=True).stdout
            except BluetoothConnectionError:
                info = ""  # no details for this device (icon "unknown")
        details = _parse_info(info)
        in_range = mac.upper() in seen or details["rssi"] is not None
        if not (include_out_of_range or in_range or details["connected"]):
            continue
        devices.append(BluetoothDevice(
            mac_address=mac, name=name, icon=details["icon"], paired=details["paired"],
            connected=details["connected"], rssi=details["rssi"], device_class=details["class"],
        ))
    devices.sort(key=lambda d: (d.rssi is None, -(d.rssi or 0)))
    return devices


def connected_devices() -> list[BluetoothDevice]:
    """The devices connected right now (`bluetoothctl devices Connected`; read only, no discovery)."""
    res = run_bluetoothctl(["devices", "Connected"], capture_output=True, text=True)
    if res.returncode != 0:
        return []
    devices = []
    for line in res.stdout.splitlines():
        match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)$", line.strip())
        if match:
            devices.append(BluetoothDevice(mac_address=match.group(1), name=match.group(2), paired=True,
                                           connected=True))
    return devices


def filter_by_icon(devices: list[BluetoothDevice], allowed_icons: Optional[list[str]] = None) -> list[BluetoothDevice]:
    """Filters a list of devices based on an allowed set/list of icon names."""
    if allowed_icons is None:
        allowed_icons = ALLOWED_ICONS

    allowed = {icon.lower() for icon in allowed_icons}
    return [d for d in devices if (d.icon or "").lower() in allowed]


def pair_device(mac_address: str) -> None:
    """Uses bluetoothctl's built-in agent for seamless pairing."""
    res = run_bluetoothctl(["pair", mac_address], timeout=PAIR_TIMEOUT_S, capture_output=True, text=True)
    check_and_raise_error(res, "Error on pairing with the device")

    res = run_bluetoothctl(["trust", mac_address], capture_output=True, text=True)
    check_and_raise_error(res, "Error on trusting the device")


def connect_device(mac_address: str) -> None:
    res = run_bluetoothctl(["connect", mac_address], timeout=CONNECT_TIMEOUT_S, capture_output=True, text=True)
    check_and_raise_error(res, "Error on connecting to requested device")


def disconnect_device(mac_address: str) -> None:
    res = run_bluetoothctl(["disconnect", mac_address], timeout=CONNECT_TIMEOUT_S, capture_output=True, text=True)
    check_and_raise_error(res, "Error on disconnecting from the device")


def is_paired(mac_address: str) -> bool:
    res = run_bluetoothctl(["info", mac_address], capture_output=True, text=True)
    return res.returncode == 0 and "Paired: yes" in res.stdout

