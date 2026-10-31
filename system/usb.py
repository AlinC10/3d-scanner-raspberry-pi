"""
USB sticks: detection (``lsblk``), mounting / unmounting (``udisksctl``) and the export of one finished object.

Without a desktop session nothing mounts a stick automatically, so the export mounts it itself (udisks2, allowed to the
kiosk user by a polkit rule, see ``system/README.md``) and unmounts it when the copy is done: once the export status is
``done`` (``safe_to_remove``) the stick can be pulled out.

The export runs in one background thread at a time; its progress is :func:`export_status`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 20.0
# What an export contains: every model format and the object's description. Logs, output.zip (the same models
# again), status / stats / calibration files are left out.
EXPORT_DIRS = ("obj", "glb", "stl", "3mf")
EXPORT_FILES = ("object.json",)
# Free space kept on the stick on top of the archive (file system overhead)
SPACE_MARGIN = 1.05


class UsbError(Exception):
    """A USB operation failed (the message says why)."""


class UsbNotFoundError(UsbError):
    """No USB stick is plugged in (or not the one asked for)."""


class UsbBusyError(UsbError):
    """An export is already running."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
    except FileNotFoundError as e:
        raise UsbError(f"{cmd[0]} is not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise UsbError(f"{' '.join(cmd[:2])} did not answer within {COMMAND_TIMEOUT_S:.0f} s") from e


def list_drives() -> list[dict]:
    """
    The partitions of USB disks that carry a file system: ``device`` (/dev/sda1), ``label``, ``size_bytes``,
    ``fstype``, ``mountpoint`` (None when not mounted) and ``free_bytes`` (None when not mounted).
    """
    done = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TRAN,RM,HOTPLUG,LABEL,SIZE,FSTYPE,MOUNTPOINTS,TYPE"])
    if done.returncode != 0:
        raise UsbError(f"lsblk failed: {done.stderr.strip()}")
    try:
        devices = json.loads(done.stdout).get("blockdevices") or []
    except ValueError as e:
        raise UsbError(f"lsblk gave no JSON: {e}") from e

    drives = []
    for disk in devices:
        if disk.get("type") != "disk" or disk.get("tran") != "usb":
            continue
        # A stick formatted without a partition table carries the file system on the disk itself
        parts = disk.get("children") or [disk]
        for part in parts:
            if not part.get("fstype"):
                continue
            mounts = [m for m in (part.get("mountpoints") or []) if m]
            mountpoint = mounts[0] if mounts else None
            drives.append({
                "device": part.get("path"),
                "label": part.get("label"),
                "size_bytes": int(part.get("size") or 0),
                "fstype": part.get("fstype"),
                "mountpoint": mountpoint,
                "free_bytes": shutil.disk_usage(mountpoint).free if mountpoint else None,
                "disk": disk.get("path"),
            })
    return drives


def _pick_drive(device: Optional[str]) -> dict:
    drives = list_drives()
    if device:
        drives = [d for d in drives if d["device"] == device]
    if not drives:
        raise UsbNotFoundError(f"USB stick {device} is not plugged in." if device else
                               "No USB stick is plugged in. Insert one and try again.")
    return drives[0]


def mount(device: str) -> str:
    """Mounts the partition (udisksctl) and returns its mount point."""
    done = _run(["udisksctl", "mount", "-b", device, "--no-user-interaction"])
    if done.returncode != 0 and "AlreadyMounted" not in done.stderr:
        raise UsbError(f"Could not mount {device}: {done.stderr.strip()}")
    for drive in list_drives():
        if drive["device"] == device and drive["mountpoint"]:
            return drive["mountpoint"]
    raise UsbError(f"{device} is not mounted after udisksctl mount.")


def unmount(device: str) -> None:
    """Flushes the caches and unmounts the partition: the stick can then be pulled out."""
    os.sync()
    done = _run(["udisksctl", "unmount", "-b", device, "--no-user-interaction"])
    if done.returncode != 0 and "NotMounted" not in done.stderr:
        raise UsbError(f"Could not unmount {device}: {done.stderr.strip()}")


def eject(device: Optional[str] = None) -> dict:
    """
    Safe removal of a stick: flushes the caches, unmounts every partition of its disk and powers the disk off
    (``udisksctl power-off``; the polkit rule of ``kiosk/`` allows it). `device` is a partition from `list_drives`
    (default: the first stick). Returns ``{"device", "disk", "powered_off"}``; ``powered_off`` False means only the
    unmount worked (the stick is still safe to pull out).

    :raises UsbNotFoundError: no such stick.
    :raises UsbBusyError: an export is running.
    :raises UsbError: a partition could not be unmounted.
    """
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise UsbBusyError(f"An export of {_status['object_id']} is running; wait for it to end.")
    drive = _pick_drive(device)
    disk = drive.get("disk") or drive["device"]
    for part in list_drives():
        if (part.get("disk") or part["device"]) == disk and part["mountpoint"]:
            unmount(part["device"])
    done = _run(["udisksctl", "power-off", "-b", disk, "--no-user-interaction"])
    powered_off = done.returncode == 0
    if not powered_off:
        log.warning("[USB] %s unmounted but not powered off: %s", disk, done.stderr.strip())
    return {"device": drive["device"], "disk": disk, "powered_off": powered_off}


def export_files(folder: Path) -> list[tuple[Path, str]]:
    """(file, name in the archive) of everything exported from one result folder; names start with the folder name."""
    files = []
    for name in EXPORT_FILES:
        path = folder / name
        if path.is_file():
            files.append((path, f"{folder.name}/{name}"))
    for sub in EXPORT_DIRS:
        root = folder / sub
        if root.is_dir():
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                files.append((path, f"{folder.name}/{path.relative_to(folder).as_posix()}"))
    return files


# ── Export state (one export at a time) ──────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_status: dict = {"state": "idle", "object_id": None, "device": None, "archive": None, "bytes_total": 0,
                 "bytes_done": 0, "error": None, "safe_to_remove": True, "started_at": None, "finished_at": None}

RUNNING_STATES = ("mounting", "archiving", "syncing")


def export_status() -> dict:
    """
    ``state``: ``idle`` | ``mounting`` | ``archiving`` | ``syncing`` | ``done`` | ``error``; ``safe_to_remove`` is
    False while the stick is in use (do not pull it out), True when idle, done, or after an error that left it
    unmounted.
    """
    with _lock:
        return dict(_status)


def _set(**values) -> None:
    with _lock:
        _status.update(values)


def start_export(folder: Path, device: Optional[str] = None) -> dict:
    """
    Checks that a stick is plugged in (UsbNotFoundError otherwise) and that no export is running (UsbBusyError), then
    starts the export of ``folder`` in the background and returns the status. The archive on the stick is
    ``<folder name>.zip`` in its root; an older one with the same name is replaced (no duplicates).
    """
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise UsbBusyError(f"An export of {_status['object_id']} is already running.")
    drive = _pick_drive(device)
    files = export_files(folder)
    if not files:
        raise UsbError(f"Object {folder.name} has no model files to export.")
    total = sum(p.stat().st_size for p, _ in files)
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise UsbBusyError(f"An export of {_status['object_id']} is already running.")
        _status.update(state="mounting", object_id=folder.name, device=drive["device"], archive=f"{folder.name}.zip",
                       bytes_total=total, bytes_done=0, error=None, safe_to_remove=False,
                       started_at=time.time(), finished_at=None)
    threading.Thread(target=_export, args=(drive, files, folder.name, total), name="usb-export", daemon=True).start()
    return export_status()


def _export(drive: dict, files: list[tuple[Path, str]], name: str, total: int) -> None:
    device = drive["device"]
    part = None
    try:
        mountpoint = drive["mountpoint"]
        if not mountpoint:
            mountpoint = mount(device)
        free = shutil.disk_usage(mountpoint).free
        if total * SPACE_MARGIN > free:
            raise UsbError(f"Not enough space on the USB stick: {total / 1e6:.0f} MB needed, {free / 1e6:.0f} MB free.")

        _set(state="archiving")
        final = Path(mountpoint) / f"{name}.zip"
        part = Path(mountpoint) / f".{name}.zip.part"
        done = 0
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for path, arcname in files:
                zf.write(path, arcname)
                done += path.stat().st_size
                _set(bytes_done=done)
        os.replace(part, final)   # an older export of the same object is replaced
        part = None

        _set(state="syncing")
        unmount(device)           # sync + unmount: the stick can be pulled out
        _set(state="done", safe_to_remove=True, finished_at=time.time())
        log.info("[USB] %s exported to %s (%s).", name, final, device)
    except Exception as e:
        log.error("[USB] Export of %s to %s failed: %s", name, device, e)
        if part is not None:
            try:
                part.unlink()
            except OSError:
                pass
        safe = False
        try:
            unmount(device)
            safe = True
        except Exception as unmount_error:
            log.error("[USB] %s could not be unmounted after the failure: %s", device, unmount_error)
        _set(state="error", error=str(e), safe_to_remove=safe, finished_at=time.time())
