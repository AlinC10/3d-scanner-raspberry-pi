"""
Export of one finished object to the connected Bluetooth device (OBEX push with ``bt-obex``), in the background.

The archive is the same as the USB export (``<object id>.zip``: every model format and object.json), built in a
temporary folder and pushed with ``bt-obex -p <mac> <file>``. One transfer at a time; :func:`cancel` (called by the
disconnect endpoint) kills it.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from system.usb import export_files

log = logging.getLogger(__name__)

TRANSFER_TIMEOUT_S = 600.0
RUNNING_STATES = ("archiving", "sending")


class BluetoothExportError(Exception):
    """The export cannot start (the message says why)."""


class NoConnectedDeviceError(BluetoothExportError):
    """No Bluetooth device is connected (or not the one asked for)."""


class BluetoothBusyError(BluetoothExportError):
    """A transfer is already running."""


_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_cancel = threading.Event()
_status: dict = {"state": "idle", "object_id": None, "device": None, "device_name": None, "archive": None,
                 "bytes_total": 0, "error": None, "started_at": None, "finished_at": None}


def export_status() -> dict:
    """``state``: ``idle`` | ``archiving`` | ``sending`` | ``done`` | ``error`` | ``cancelled``."""
    with _lock:
        return dict(_status)


def _set(**values) -> None:
    with _lock:
        _status.update(values)


def is_running(mac: Optional[str] = None) -> bool:
    with _lock:
        running = _status["state"] in RUNNING_STATES
        return running and (mac is None or (_status["device"] or "").upper() == mac.upper())


def start_export(folder: Path, connected: list, mac: Optional[str] = None) -> dict:
    """
    Starts sending ``folder`` as ``<name>.zip`` to the connected device (``mac``, or the only / first connected one).

    :param connected: the devices connected now (``system.bluetooth.connected_devices()``).
    :raises NoConnectedDeviceError: nothing connected, or ``mac`` is not connected.
    :raises BluetoothBusyError: a transfer is running.
    :raises BluetoothExportError: the object has no model files.
    """
    if mac:
        connected = [d for d in connected if d.mac_address.upper() == mac.upper()]
    if not connected:
        raise NoConnectedDeviceError(f"Bluetooth device {mac} is not connected." if mac else
                                     "No Bluetooth device is connected. Connect a phone first.")
    device = connected[0]
    files = export_files(folder)
    if not files:
        raise BluetoothExportError(f"Object {folder.name} has no model files to export.")
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise BluetoothBusyError(f"A Bluetooth transfer of {_status['object_id']} is already running.")
        _cancel.clear()
        _status.update(state="archiving", object_id=folder.name, device=device.mac_address, device_name=device.name,
                       archive=f"{folder.name}.zip", bytes_total=sum(p.stat().st_size for p, _ in files), error=None,
                       started_at=time.time(), finished_at=None)
    threading.Thread(target=_send, args=(device.mac_address, files, folder.name), name="bt-export",
                     daemon=True).start()
    return export_status()


def cancel(mac: Optional[str] = None) -> bool:
    """Stops the running transfer (to ``mac`` only, when given). Returns True when one was running."""
    global _proc
    with _lock:
        if _status["state"] not in RUNNING_STATES:
            return False
        if mac and (_status["device"] or "").upper() != mac.upper():
            return False
        _cancel.set()
        proc = _proc
    if proc is not None:
        _kill(proc)
    return True


def _kill(proc: subprocess.Popen) -> None:
    """Kills the transfer and anything it started (its own process group)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _send(mac: str, files: list, name: str, popen: Callable = subprocess.Popen) -> None:
    global _proc
    try:
        with tempfile.TemporaryDirectory(prefix="bt-export-") as tmp:
            archive = Path(tmp) / f"{name}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for path, arcname in files:
                    if _cancel.is_set():
                        break
                    zf.write(path, arcname)
            if _cancel.is_set():
                _set(state="cancelled", finished_at=time.time())
                return
            _set(state="sending", bytes_total=archive.stat().st_size)
            try:
                proc = popen(["bt-obex", "-p", mac, str(archive)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
            except FileNotFoundError:
                raise BluetoothExportError("`bt-obex` is not installed (sudo apt install bluez-tools).")
            with _lock:
                _proc = proc
            if _cancel.is_set():
                _kill(proc)
            try:
                output, _ = proc.communicate(timeout=TRANSFER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.communicate()
                raise BluetoothExportError(f"The transfer did not finish within {TRANSFER_TIMEOUT_S:.0f} s.")
            if _cancel.is_set():
                _set(state="cancelled", finished_at=time.time())
                log.info("[BT] Transfer of %s to %s cancelled.", name, mac)
                return
            if proc.returncode != 0:
                tail = (output or "").strip().splitlines()[-1:] or ["no output"]
                raise BluetoothExportError(f"The device refused or dropped the transfer: {tail[0]}")
        _set(state="done", finished_at=time.time())
        log.info("[BT] %s sent to %s.", name, mac)
    except Exception as e:
        log.error("[BT] Transfer of %s to %s failed: %s", name, mac, e)
        _set(state="error", error=str(e), finished_at=time.time())
    finally:
        with _lock:
            _proc = None
