"""
The state of every connection in one call (`GET /system/status`, S1): only reads what NetworkManager and BlueZ already
know (no radio scan, no change), so it is safe to poll during a scan. Each part is read on its own: one tool that
fails gives `"error"` for that part, not a failed request.
"""
from typing import Any

from system import bluetooth, ethernet, network_state, nm, wifi


def _wifi(devices: list[dict]) -> dict[str, Any]:
    device = next((d for d in devices if d["type"] == "wifi"), None)
    result = {"enabled": wifi.get_status(), "interface": device["device"] if device else None,
              "state": device["state"] if device else "unavailable", "connected": False,
              "connection": None, "ssid": None, "signal": None, "ip": None}
    if device and device["state"] == "connected":
        result["connected"] = True
        result["connection"] = device["connection"]
        # The list NetworkManager already has (`--rescan no`): no radio scan
        res = nm.nmcli(["-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "no"])
        for line in res.stdout.splitlines():
            parts = nm.split_terse(line)
            if len(parts) >= 3 and parts[0] == "yes":
                result["ssid"] = parts[1] or None
                result["signal"] = int(parts[2]) if parts[2].isdigit() else None
                break
        result["ip"] = ethernet.device_ipv4(device["device"])
    return result


def _ethernet(devices: list[dict]) -> dict[str, Any]:
    status = ethernet.get_status(devices)
    status["connected"] = status["state"] == "connected"
    return status


def _bluetooth() -> dict[str, Any]:
    powered = bluetooth.get_power_status()
    devices = bluetooth.connected_devices() if powered else []
    return {"powered": powered, "connected": bool(devices),
            "devices": [{"mac_address": d.mac_address, "name": d.name} for d in devices]}


def _part(read) -> dict[str, Any]:
    try:
        return read()
    except Exception as e:
        return {"error": str(e)}


def get_network_status() -> dict[str, Any]:
    try:
        devices = ethernet.list_devices()
    except Exception as e:
        devices = None
        nm_error = str(e)
    if devices is None:
        wifi_part = {"error": nm_error, **_part(lambda: {"enabled": wifi.get_status()})}
        ethernet_part = {"error": nm_error}
    else:
        wifi_part = _part(lambda: _wifi(devices))
        ethernet_part = _part(lambda: _ethernet(devices))
    return {
        "ethernet": ethernet_part,
        "wifi": wifi_part,
        "bluetooth": _part(_bluetooth),
        "restore": network_state.last_restore,
    }
