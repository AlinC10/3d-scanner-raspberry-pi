"""
The network connections the user made, kept on disk so they are made again when the server starts (S6).

What is kept is only *which* connections: the Wi-Fi profile, the 802.1X wired profile, the Bluetooth devices and the
radio switches. No password: the secrets stay in the NetworkManager profiles (and BlueZ keeps the pairings). The file
is written atomically (`network.json` in `state/` of the project, see `app_paths.NETWORK_STATE_FILE`).

`restore()` runs at startup in a background thread: every step has the deadline of its command, a failure is only
logged and recorded in `last_restore` (shown by `GET /system/status`), and the server works as before whatever happens.
"""
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app_paths import NETWORK_STATE_FILE

log = logging.getLogger(__name__)

_lock = threading.Lock()

# Outcome of the restore at startup, per subsystem: {"wifi": "connected 'Home'", "bluetooth": "failed: ..."}; also
# "running" while it runs. None when no restore ran (no saved state, or development mode).
last_restore: Optional[dict] = None


def _empty() -> dict:
    return {"wifi": {}, "ethernet": {}, "bluetooth": {}}


def load(path: Path = NETWORK_STATE_FILE) -> dict:
    """The saved state ({} parts when nothing was saved or the file is unreadable)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    state = _empty()
    if isinstance(data, dict):
        for key in state:
            if isinstance(data.get(key), dict):
                state[key] = data[key]
    return state


def _save(state: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".network-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(change: Callable[[dict], None], path: Path = NETWORK_STATE_FILE) -> None:
    """Applies `change(state)` to the saved state. Never raises: a state that cannot be saved only loses the restore."""
    with _lock:
        try:
            state = load(path)
            change(state)
            _save(state, path)
        except Exception as e:
            log.warning("[Network] Could not save the network state (%s): %s", path, e)


def remember_wifi_power(enabled: bool) -> None:
    update(lambda s: s["wifi"].update(enabled=enabled))


def remember_wifi_profile(profile: Optional[str]) -> None:
    update(lambda s: s["wifi"].update(profile=profile, enabled=True if profile else s["wifi"].get("enabled", True)))


def forget_wifi_profile(name: str) -> None:
    def change(s):
        if s["wifi"].get("profile") == name:
            s["wifi"]["profile"] = None
    update(change)


def remember_ethernet_profile(profile: Optional[str]) -> None:
    update(lambda s: s["ethernet"].update(profile=profile))


def remember_bluetooth_power(powered: bool) -> None:
    update(lambda s: s["bluetooth"].update(powered=powered))


def forget_bluetooth_device(mac: str) -> None:
    """The device is no longer reconnected when the server starts (the user disconnected it)."""
    update(lambda s: s["bluetooth"].update(devices=[
        d for d in s["bluetooth"].get("devices", []) if isinstance(d, str) and d.upper() != mac.upper()]))


def remember_bluetooth_device(mac: str) -> None:
    def change(s):
        devices = [d for d in s["bluetooth"].get("devices", []) if isinstance(d, str) and d.upper() != mac.upper()]
        s["bluetooth"]["devices"] = devices + [mac.upper()]
        s["bluetooth"]["powered"] = True
    update(change)


def _step(results: dict, key: str, fn: Callable[[], Any]) -> None:
    try:
        outcome = fn()
        results[key] = outcome if isinstance(outcome, str) else "ok"
    except Exception as e:
        results[key] = f"failed: {e}"
        log.warning("[Network] Restore of %s failed (the server works as before): %s", key, e)


def restore(path: Path = NETWORK_STATE_FILE) -> dict:
    """
    Makes the saved connections again. A connection that is already up is left alone (NetworkManager may have
    reconnected it by itself); each failure is recorded and the next part still runs.
    """
    global last_restore
    from system import bluetooth, ethernet, wifi

    state = load(path)
    results: dict = {}
    last_restore = {"state": "running"}
    started = time.monotonic()

    wifi_state = state["wifi"]
    if "enabled" in wifi_state:
        _step(results, "wifi_power", lambda: wifi.set_wifi_power("on" if wifi_state["enabled"] else "off"))
    profile = wifi_state.get("profile")
    if profile and wifi_state.get("enabled", True):
        def wifi_up():
            active = [d for d in ethernet.list_devices() if d["type"] == "wifi" and d["state"] == "connected"]
            if active:
                return f"already connected ({active[0]['connection']})"
            wifi._activate(profile, f"Saved network {profile!r} could not connect")
            return f"connected {profile!r}"
        _step(results, "wifi", wifi_up)

    wired = state["ethernet"].get("profile")
    if wired:
        def ethernet_up():
            status = ethernet.get_status()
            if status["state"] == "connected":
                return f"already connected ({status['connection']})"
            ethernet.activate_profile(wired)
            return f"connected {wired!r}"
        _step(results, "ethernet", ethernet_up)

    bt_state = state["bluetooth"]
    if "powered" in bt_state:
        _step(results, "bluetooth_power",
              lambda: (bluetooth.power_on if bt_state["powered"] else bluetooth.power_off)() and None)
    if bt_state.get("powered", True):
        for mac in bt_state.get("devices", []):
            def bt_up(mac=mac):
                if any(d.mac_address.upper() == mac for d in bluetooth.connected_devices()):
                    return "already connected"
                bluetooth.connect_device(mac)
                return "connected"
            _step(results, f"bluetooth {mac}", bt_up)

    results["state"] = "done"
    results["seconds"] = round(time.monotonic() - started, 1)
    last_restore = results
    log.info("[Network] Restore of the saved connections: %s", results)
    return results


def start_restore(path: Path = NETWORK_STATE_FILE) -> Optional[threading.Thread]:
    """Starts `restore()` in a daemon thread when a state was saved (nothing in development mode)."""
    if os.environ.get("MOCK_HARDWARE") == "1" or not Path(path).is_file():
        return None
    thread = threading.Thread(target=restore, args=(path,), name="network-restore", daemon=True)
    thread.start()
    return thread
