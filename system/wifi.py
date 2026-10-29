from typing import Literal, Optional

from system import exception, nm
from schemas.system.wifi import *
from schemas.system.network import *

# NetworkManager connection type of a Wi-Fi profile
WIFI_TYPE = "802-11-wireless"


class WIFIConnectionError(exception.SystemConnectionError):
    """Raised when connecting to a Wi-Fi network fails."""
    pass


class WIFIAuthenticationError(WIFIConnectionError):
    """The network refused the password (or a saved one is no longer valid): the user must type it."""
    status_code = 401


# nmcli messages that mean "the secret is wrong or missing", in the C locale
_AUTH_FAILURE_MARKERS = (
    "secrets were required", "no secrets", "802-1x supplicant", "invalid psk", "property is invalid",
    "wrong password", "authentication",
)


def _nmcli(args: list[str], timeout: float = nm.NMCLI_TIMEOUT_S, check: Optional[str] = None):
    return nm.nmcli(args, timeout=timeout, error_type=WIFIConnectionError, check=check)


def set_wifi_power(state: Literal["on", "off"]):
    _nmcli(["radio", "wifi", state], check="Cannot change WIFI state")


def get_status() -> bool:
    """Return whether WI-FI is enabled or disabled."""
    res = _nmcli(["radio", "wifi"], check="Error when checking WIFI status")
    return res.stdout.strip() == "enabled"


def scan_wifi(rescan: bool = False) -> dict[str, WIFICharacteristics]:
    """
    The networks in range, one per SSID (the strongest access point of a mesh), hidden ones left out, each marked
    `saved` when a profile for it exists (connecting to it needs no password).

    :param rescan: True forces a radio scan (`--rescan yes`, up to ~20 s, disturbs the uplink a little); False uses
        the list NetworkManager already has (`--rescan no`, instant). NetworkManager refreshes that list by itself.
    :raises CommandTimeoutError: the scan overran WIFI_SCAN_TIMEOUT_S (504).
    """
    result = _nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes" if rescan else "no"],
        timeout=nm.WIFI_SCAN_TIMEOUT_S if rescan else nm.NMCLI_TIMEOUT_S,
        check="Error when scanning WIFI Network",
    )
    saved = {p.ssid for p in list_saved_networks()}

    unique_networks: dict[str, WIFICharacteristics] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = nm.split_terse(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid:
            continue   # hidden network
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        known = unique_networks.get(ssid)
        if known is None or strength > known.signal or in_use == "*":
            unique_networks[ssid] = WIFICharacteristics(
                in_use=in_use if known is None or in_use == "*" else known.in_use,
                ssid=ssid,
                signal=strength if known is None else max(strength, known.signal),
                security=security,
                saved=ssid in saved,
            )
    return unique_networks


def list_saved_networks() -> list[SavedNetwork]:
    """The Wi-Fi profiles NetworkManager keeps (their secrets stay in NetworkManager, never returned)."""
    res = _nmcli(["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"], check="Cannot list saved networks")
    saved = []
    for line in res.stdout.splitlines():
        parts = nm.split_terse(line)
        if len(parts) < 3 or parts[1] != WIFI_TYPE:
            continue
        name = parts[0]
        ssid_res = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", "id", name])
        ssid = ssid_res.stdout.strip() if ssid_res.returncode == 0 and ssid_res.stdout.strip() else name
        saved.append(SavedNetwork(name=name, ssid=ssid, autoconnect=parts[2] == "yes"))
    return saved


def find_saved_network(ssid: str) -> Optional[SavedNetwork]:
    for profile in list_saved_networks():
        if profile.ssid == ssid or profile.name == ssid:
            return profile
    return None


def delete_saved_network(name: str) -> None:
    """Forgets a saved Wi-Fi profile (by profile name or SSID)."""
    profile = find_saved_network(name)
    if profile is None:
        error = WIFIConnectionError(f"No saved Wi-Fi network named {name!r}.")
        error.status_code = 404
        raise error
    _nmcli(["connection", "delete", "id", profile.name], check="Cannot forget the network")


def _raise_connect_error(res, what: str) -> None:
    message = (res.stderr or res.stdout or "").strip()
    lowered = message.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        raise WIFIAuthenticationError(f"{what}: the network refused the password ({message}).")
    raise WIFIConnectionError(f"{what}: {message}")


def _activate(name: str, what: str) -> None:
    res = _nmcli(["--wait", str(nm.WIFI_CONNECT_WAIT_S), "connection", "up", "id", name],
                 timeout=nm.WIFI_CONNECT_TIMEOUT_S)
    if res.returncode != 0:
        _raise_connect_error(res, what)


def _profile_names() -> set[str]:
    res = _nmcli(["-t", "-f", "NAME", "connection", "show"])
    return {nm.split_terse(line)[0] for line in res.stdout.splitlines() if line}


def connect_wifi(details: NetworkCredentials) -> str:
    """
    Connects to a Wi-Fi network and returns the name of the profile used.

    * A **saved** network and no password: the saved profile is activated, no password needed. If it is refused
      (the password changed), WIFIAuthenticationError (401) asks the user for it.
    * A saved network and a new password: the profile is updated and activated; if the new password is refused the
      old one is put back, so a typo does not destroy a working profile (SYS-12).
    * A new network: a profile is created; if the connection fails it is removed again, so NetworkManager does not
      keep retrying a wrong password (SYS-17, SYS-22).
    * With `identity`: an 802.1X (PEAP/MSCHAPv2) profile `enterprise-<ssid>`, same rules.

    :raises WIFIAuthenticationError: the password is wrong or missing (401).
    :raises WIFIConnectionError: any other failure (400); CommandTimeoutError (504) when NetworkManager overran.
    """
    ssid = details.ssid
    password = details.password or ""
    identity = details.identity
    saved = find_saved_network(f"enterprise-{ssid}" if identity else ssid)

    if saved is not None and not password:
        _activate(saved.name, f"Saved network {ssid!r} could not connect")
        return saved.name

    if saved is not None:
        # New password for a known network: keep the old one to put it back if the new one is refused
        key = "802-1x.password" if identity else "802-11-wireless-security.psk"
        old = _nmcli(["-s", "-g", key, "connection", "show", "id", saved.name])
        old_secret = old.stdout.strip() if old.returncode == 0 else None
        settings = (["802-1x.identity", identity, "802-1x.password", password] if identity
                    else ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
        _nmcli(["connection", "modify", "id", saved.name, *settings], check="Cannot update the saved network")
        try:
            _activate(saved.name, f"Could not connect to {ssid!r}")
        except WIFIConnectionError:
            if old_secret:
                _nmcli(["connection", "modify", "id", saved.name, key, old_secret])
            raise
        return saved.name

    before = _profile_names()
    try:
        if identity:
            name = f"enterprise-{ssid}"
            _nmcli([
                "connection", "add", "type", "wifi", "con-name", name, "ssid", ssid,
                "wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap", "802-1x.phase2-auth", "mschapv2",
                "802-1x.identity", identity, "802-1x.password", password,
            ], check="Failed to create enterprise profile")
            _activate(name, f"Could not connect to {ssid!r}")
            return name
        cmd = ["--wait", str(nm.WIFI_CONNECT_WAIT_S), "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        res = _nmcli(cmd, timeout=nm.WIFI_CONNECT_TIMEOUT_S)
        if res.returncode != 0:
            _raise_connect_error(res, f"Could not connect to {ssid!r}")
        created = _profile_names() - before
        return next(iter(created), ssid)
    except exception.SystemConnectionError:
        # A profile created by this failed attempt is removed again (a wrong password would be retried by itself)
        for leftover in _profile_names() - before:
            _nmcli(["connection", "delete", "id", leftover])
        raise
