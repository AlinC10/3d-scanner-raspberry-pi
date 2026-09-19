import subprocess
from subprocess import CompletedProcess
from typing import Literal

from system import exception
from schemas.system.wifi import *
from schemas.system.network import *

class WIFIConnectionError(exception.SystemConnectionError):
    """Raised when connecting to a Wi-Fi network fails."""
    pass

def check_and_raise_error(res: CompletedProcess, msg: str):
    exception.check_and_raise_error(res, msg, WIFIConnectionError)

def set_wifi_power(state: Literal["on", "off"]):
    res = subprocess.run(["nmcli", "radio", "wifi", state], capture_output=True, text=True)
    check_and_raise_error(res, "Cannot change WIFI state")

def get_status() -> bool:
    """Return whether WI-FI is enabled or disabled."""
    res = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True)
    check_and_raise_error(res, "Error when checking WIFI status")

    res = res.stdout.strip()
    return res == "enabled"

def scan_wifi() -> dict[str, WIFICharacteristics]:
    # nmcli (NetworkManager) for scanning networks
    result = subprocess.run(['nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'dev', 'wifi'], capture_output=True,
                            text=True)
    check_and_raise_error(result, "Error when scanning WIFI Network")

    unique_networks = {}
    # parse networks (e.g.: NetworkName:80:WPA2)
    for line in result.stdout.split('\n'):
        if line:
            parts = line.split(':')

            # ignore hidden networks without SSID
            if len(parts) >= 4:
                ssid = parts[1].strip()

                if unique_networks.get(ssid, None) is None:
                    # parts[0] -> IN-USE ('*' if active, '' otherwise)
                    # parts[1] -> SSID
                    # parts[2] -> SIGNAL
                    # parts[3] -> SECURITY

                    unique_networks[ssid] = WIFICharacteristics(
                        in_use=parts[0],
                        ssid=ssid,
                        signal=int(parts[2]),
                        security=parts[3]
                        )

    return unique_networks


def connect_wifi(details: NetworkCredentials):
    """Check network credential to connect to the Ethernet."""
    ssid = details.ssid
    password = details.password
    identity = details.identity


    if identity:
        con_name = f"enterprise-{ssid}"

        # Remove previous profile if it exists to avoid conflicts
        subprocess.run(["nmcli", "connection", "delete", con_name],
                       capture_output=True, text=True)

        # Create the 802.1X connection profile
        cmd_add = [
            "nmcli", "connection", "add",
            "type", "wifi",
            "con-name", con_name,
            "ssid", ssid,
            "wifi-sec.key-mgmt", "wpa-eap",
            "802-1x.eap", "peap",
            "802-1x.phase2-auth", "mschapv2",
            "802-1x.identity", identity,
            "802-1x.password", password or ""
        ]
        add_res = subprocess.run(cmd_add, capture_output=True, text=True)

        if add_res.returncode != 0:
            raise WIFIConnectionError(f"Failed to create enterprise profile: {add_res.stderr}")

        # Activate the profile
        res = subprocess.run(["nmcli", "connection", "up", con_name],
                             capture_output=True, text=True)
    else:
        if password:
            cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password]
        else:
            cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
        res = subprocess.run(cmd, capture_output=True, text=True)

    if "successfully activated" not in res.stdout:
        raise WIFIConnectionError(f"Failed to connect: {res.stderr or res.stdout}")
