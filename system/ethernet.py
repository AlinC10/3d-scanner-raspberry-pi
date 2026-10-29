from pathlib import Path
from typing import Optional

from schemas.system.network import *
from system import exception, nm

class EthernetConnectionError(exception.SystemConnectionError):
    """Raised when Ethernet operations fail."""
    pass

# Profile name of the 802.1X wired connection created by connect_secure_ethernet()
ENTERPRISE_PROFILE = "enterprise-wired"


def _nmcli(args: list[str], timeout: float = nm.NMCLI_TIMEOUT_S, check: Optional[str] = None):
    return nm.nmcli(args, timeout=timeout, error_type=EthernetConnectionError, check=check)


def list_devices() -> list[dict]:
    """Every NetworkManager device: {device, type, state, connection} (`nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev`)."""
    res = _nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"], check="Cannot list the network devices")
    devices = []
    for line in res.stdout.splitlines():
        parts = nm.split_terse(line)
        if len(parts) >= 4:
            devices.append({"device": parts[0], "type": parts[1], "state": parts[2], "connection": parts[3] or None})
    return devices


def get_ethernet_interface(devices: Optional[list[dict]] = None) -> Optional[str]:
    """
    The built-in Ethernet port: an `ethernet` device named eth*/en* first (a USB adapter or the `usb0` gadget only when
    there is no other one). None when there is no Ethernet device at all (SYS-15: no fake "eth0").
    """
    candidates = [d["device"] for d in (devices if devices is not None else list_devices()) if d["type"] == "ethernet"]
    for name in candidates:
        if name.startswith(("eth", "end", "enp", "eno")):
            return name
    return candidates[0] if candidates else None


def device_ipv4(device: str) -> Optional[str]:
    """First IPv4 address of a device (`192.168.1.20/24`), None when it has none."""
    res = _nmcli(["-g", "IP4.ADDRESS", "dev", "show", device])
    if res.returncode != 0:
        return None
    first = res.stdout.strip().split("|")[0].strip()
    return first or None


def get_status(devices: Optional[list[dict]] = None) -> dict:
    """
    Cable (carrier) and NetworkManager state of the Ethernet port, its profile and its IPv4 address.

    :raises EthernetConnectionError: NetworkManager could not be queried (it is no longer shown as "disconnected").
    """
    devices = devices if devices is not None else list_devices()
    interface = get_ethernet_interface(devices)
    if interface is None:
        return {"interface": None, "cable_plugged": False, "state": "unavailable", "connection": None, "ip": None}

    cable_plugged = False
    carrier_path = Path(f"/sys/class/net/{interface}/carrier")
    try:
        cable_plugged = carrier_path.read_text().strip() == "1"   # 0 = unplugged, 1 = plugged
    except OSError:
        cable_plugged = False   # the file cannot be read while the interface is down

    device = next(d for d in devices if d["device"] == interface)
    connected = device["state"] == "connected"
    return {
        "interface": interface,
        "cable_plugged": cable_plugged,
        "state": device["state"],             # connected, connecting, disconnected, unavailable (no cable)
        "connection": device["connection"],   # e.g. 'Wired connection 1' or 'enterprise-wired'
        "ip": device_ipv4(interface) if connected else None,
    }


def activate_profile(name: str) -> None:
    """Activates a saved wired profile (used by the restore at startup)."""
    res = _nmcli(["--wait", str(nm.WIFI_CONNECT_WAIT_S), "connection", "up", "id", name],
                 timeout=nm.WIFI_CONNECT_TIMEOUT_S)
    if res.returncode != 0:
        raise EthernetConnectionError(f"Failed to connect: {(res.stderr or res.stdout).strip()}")


def connect_secure_ethernet(details: NetworkCredentials) -> str:
    """
    Connects to an 802.1X secured Ethernet port (Enterprise) and returns the profile name.
    Standard unmanaged Ethernet typically connects automatically via DHCP.

    The new profile is created under a temporary name and replaces the old one only once it connected, so a wrong
    password does not destroy a working profile (SYS-12); a failed one is removed (SYS-22).
    """
    interface = get_ethernet_interface()
    if interface is None:
        raise EthernetConnectionError("There is no Ethernet port on this system.")
    if not details.identity:
        raise EthernetConnectionError("Secured Ethernet requires an identity parameter.")

    candidate = f"{ENTERPRISE_PROFILE}-new"
    _nmcli(["connection", "delete", "id", candidate])   # leftover of an interrupted attempt
    _nmcli([
        "connection", "add", "type", "ethernet", "con-name", candidate, "ifname", interface,
        "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
        "802-1x.identity", details.identity, "802-1x.password", details.password or "",
    ], check="Failed to create Ethernet profile")
    try:
        activate_profile(candidate)
    except exception.SystemConnectionError:
        _nmcli(["connection", "delete", "id", candidate])
        raise
    _nmcli(["connection", "delete", "id", ENTERPRISE_PROFILE])
    _nmcli(["connection", "modify", "id", candidate, "connection.id", ENTERPRISE_PROFILE])
    return ENTERPRISE_PROFILE
