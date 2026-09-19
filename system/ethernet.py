import subprocess
from pathlib import Path
from schemas.system.network import *
from system import exception

class EthernetConnectionError(exception.SystemConnectionError):
    """Raised when Ethernet operations fail."""
    pass

eth_interface = None

def get_ethernet_interface() -> str:
    """
    Auto-detects the active Ethernet network interface name using nmcli.
    Falls back to 'eth0' if not found.
    """
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "dev"],
            capture_output=True, text=True, check=False
        )
        for line in res.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1].strip() == "ethernet":
                return parts[0].strip()
    except Exception:
        pass

    return "eth0"  # Safe default fallback

def get_status() -> dict:
    """
    Checks the physical cable carrier state and NetworkManager connection state.
    """
    global eth_interface
    if eth_interface is None:
        eth_interface = get_ethernet_interface()

    carrier_path = Path(f"/sys/class/net/{eth_interface}/carrier")
    cable_plugged = False

    # 1. Check physical link (0 = unplugged, 1 = plugged)
    if carrier_path.exists():
        try:
            cable_plugged = carrier_path.read_text().strip() == "1"
        except IOError:
            cable_plugged = False

    # 2. Check NetworkManager status for that interface
    res = subprocess.run(
        ['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'dev'],
        capture_output=True, text=True
    )

    state = "disconnected"
    connection_name = None

    # Example output: "eth0:connected:Wired connection 1"
    for line in res.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == eth_interface:
            state = parts[1]
            if len(parts) > 2 and parts[2]:
                connection_name = parts[2]
            break

    return {
        "interface": eth_interface,
        "cable_plugged": cable_plugged,
        "state": state,  # e.g., 'connected', 'connecting', 'disconnected'
        "connection": connection_name  # e.g., 'Wired connection 1' or 'enterprise-wired'
    }


def connect_secure_ethernet(details: NetworkCredentials) -> None:
    """
    Connects to an 802.1X secured Ethernet port (Enterprise).
    Standard unmanaged Ethernet typically connects automatically via DHCP.
    """
    global eth_interface
    if eth_interface is None:
        eth_interface = get_ethernet_interface()

    if not details.identity:
        raise EthernetConnectionError("Secured Ethernet requires an identity parameter.")

    con_name = "enterprise-wired"

    # Clean up old profile without failing if it doesn't exist
    subprocess.run(["nmcli", "connection", "delete", con_name],
                   capture_output=True, text=True, check=False)

    # Note the 'type' is '802-3-ethernet' or 'ethernet', not 'wifi'
    cmd_add = [
        "nmcli", "connection", "add",
        "type", "ethernet",
        "con-name", con_name,
        "ifname", eth_interface,
        "802-1x.eap", "peap",
        "802-1x.phase2-auth", "mschapv2",
        "802-1x.identity", details.identity,
        "802-1x.password", details.password or ""
    ]

    add_res = subprocess.run(cmd_add, capture_output=True, text=True)
    if add_res.returncode != 0:
        err = add_res.stderr.strip() or add_res.stdout.strip()
        raise EthernetConnectionError(f"Failed to create Ethernet profile: {err}")

    up_res = subprocess.run(["nmcli", "connection", "up", con_name], capture_output=True, text=True)

    if "successfully activated" not in up_res.stdout:
        err = up_res.stderr.strip() or up_res.stdout.strip()
        raise EthernetConnectionError(f"Failed to connect: {err}")