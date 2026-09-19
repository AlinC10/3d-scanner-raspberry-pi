import subprocess
from subprocess import CompletedProcess
from typing import Optional
from pathlib import Path
import re
from schemas.system.bluetooth import BluetoothDevice
from system import exception

class BluetoothConnectionError(exception.SystemConnectionError):
    pass

ALLOWED_ICONS = {"phone", "computer"}

def check_and_raise_error(res: CompletedProcess, msg: str):
    exception.check_and_raise_error(res, msg, BluetoothConnectionError)

def power_on() -> bool:
    """Activates the Bluetooth adapter."""
    res = subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, text=True)
    check_and_raise_error(res, f"Error in `power_on`")

    return "succeeded" in res.stdout.lower() or "already on" in res.stdout.lower()


def power_off() -> bool:
    """Deactivates the Bluetooth adapter."""
    res = subprocess.run(["bluetoothctl", "power", "off"], capture_output=True, text=True)
    check_and_raise_error(res, f"Error in `power_off`")

    return "succeeded" in res.stdout.lower()


def get_power_status() -> bool:
    """Returns True if the Bluetooth adapter is powered on."""
    res = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True)
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


def scan_devices(duration: int = 8, allowed_icons: Optional[list[str]] = None) -> list:
    """
    :param duration: runs discovery for 'duration' seconds
    :type duration: int
    :param allowed_icons:
    :type allowed_icons:
    :return: all discovered devices
    :rtype: list
    """
    subprocess.run(["bluetoothctl", "--timeout", str(duration), "scan", "on"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

    res = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True)

    check_and_raise_error(res, "Error on Bluetooth device scanning")

    devices_list = res.stdout.split("\n")

    devices = []

    for device in devices_list:
        # line format: "Device XX:XX:XX:XX:XX:XX Name"
        match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)$", device.strip())

        if match:
            mac, name = match.group(1), match.group(2)

            # fetch details (icon, paired, status)
            info = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True).stdout
            icon = "unknown"
            rssi = None
            device_class = None

            matches = 0
            for line in info.splitlines():
                if "Icon:" in line:
                    icon = line.split(":", 1)[1].strip()
                    matches += 1
                if "RSSI:" in line:
                    try:
                        val = line.split(":", 1)[1].strip().split()[0]
                        rssi = int(val)
                        matches += 1
                    except (ValueError, IndexError):
                        rssi = None
                if "Class:" in line:
                    device_class = check_device_class(line)
                    matches += 1

                if matches == 3:
                    break

            devices.append(BluetoothDevice(
                mac_address=mac,
                name=name,
                icon=icon,
                paired="Paired: yes" in info,
                connected="Connected: yes" in info,
                rssi=rssi,
                device_class=device_class
            ))

    return devices


def filter_by_icon(devices: list[BluetoothDevice], allowed_icons: Optional[list[str]] = None) -> list[BluetoothDevice]:
    """Filters a list of devices based on an allowed set/list of icon names."""
    if allowed_icons is None:
        allowed_icons = ALLOWED_ICONS

    allowed = {icon.lower() for icon in allowed_icons}
    return [d for d in devices if (d.icon or "").lower() in allowed]


def pair_device(mac_address: str) -> None:
    """Uses bluetoothctl's built-in agent for seamless pairing."""
    res = subprocess.run(["bluetoothctl", "pair", mac_address], capture_output=True, text=True)
    check_and_raise_error(res, "Error on pairing with the device")

    res = subprocess.run(["bluetoothctl", "trust", mac_address], capture_output=True, text=True)
    check_and_raise_error(res, "Error on trusting the device")


def connect_device(mac_address: str) -> None:
    res = subprocess.run(["bluetoothctl", "connect", mac_address], capture_output=True, text=True)
    check_and_raise_error(res, "Error on connecting to requested device")


def send_file(mac_address: str, file_path: str | Path):
    """Uses bt-obex for 1-command OBEX push transfer."""
    cmd = ["bt-obex", "-p", mac_address, str(file_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)

    check_and_raise_error(res, "File transfer failed")

    # print("File transfer completed successfully!")