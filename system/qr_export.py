"""
Export of one finished object as a download link for a phone (`LIB-QR`): the archive ``<id>.zip`` (the model files
only, the same as the USB export) is uploaded to a Cloudflare R2 bucket kept only for exports (``R2_EXPORT_BUCKET``,
default ``scanner-exports``), and a signed download URL valid for ``EXPORT_LINK_TTL_S`` (24 h) is made; the front-end
shows it as a QR code (``qr_svg``). The phone downloads from R2 directly, over any network: nothing on the Pi is
opened to the network (the server stays on 127.0.0.1).

The export bucket is never touched by the cleanup of the scan jobs. Old archives are removed by a lifecycle rule of the
bucket (Cloudflare dashboard: delete objects after 1 day). An archive already uploaded for the same model files (same
names, sizes and modification times) is reused: only a new link is signed.

One export at a time, in a background thread; its progress is :func:`export_status`.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

EXPORT_BUCKET_ENV = "R2_EXPORT_BUCKET"
DEFAULT_EXPORT_BUCKET = "scanner-exports"
# Validity of a download link; R2 accepts at most 7 days for a signed URL (default 1 h)
def link_ttl_s() -> float:
    return min(float(os.environ.get("EXPORT_LINK_TTL_S", str(3600))), 7 * 24 * 3600.0)

LINK_TTL_S = link_ttl_s()
RUNNING_STATES = ("archiving", "uploading")


class QrExportError(Exception):
    """The export failed or cannot start (the message says why)."""


class QrExportBusyError(QrExportError):
    """An export is already running."""


class QrExportUnavailableError(QrExportError):
    """Cloudflare R2 is not configured (or the QR library is missing)."""


def export_bucket() -> str:
    return os.environ.get(EXPORT_BUCKET_ENV) or DEFAULT_EXPORT_BUCKET


_lock = threading.Lock()
_status: dict = {"state": "idle", "object_id": None, "archive": None, "url": None, "expires_at": None,
                 "bytes_total": 0, "bytes_done": 0, "reused": False, "error": None,
                 "started_at": None, "finished_at": None}


def export_status() -> dict:
    """``state``: ``idle`` | ``archiving`` | ``uploading`` | ``done`` | ``error``; ``url`` / ``expires_at`` when done."""
    with _lock:
        status = dict(_status)
    # An expired link is not offered any more
    if status["state"] == "done" and status["expires_at"] and status["expires_at"] <= time.time():
        status.update(url=None, state="expired")
    return status


def _set(**values) -> None:
    with _lock:
        _status.update(values)


def fingerprint(files: list[tuple[Path, str]]) -> str:
    """Identifies the archive content: archive names, sizes and modification times of the files."""
    h = hashlib.sha256()
    for path, arcname in files:
        st = path.stat()
        h.update(f"{arcname}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:32]


def _default_client():
    import cloud.cloudflare_r2 as r2
    return r2.client_from_env()


def _default_files(folder: Path) -> list[tuple[Path, str]]:
    from system.usb import export_files
    return export_files(folder)


def start_export(folder: Path, client_factory: Optional[Callable] = None,
                 files_fn: Optional[Callable[[Path], list]] = None) -> dict:
    """
    Starts the export of ``folder`` in the background and returns the status.

    :raises QrExportBusyError: an export is running.
    :raises QrExportError: the object has no model files.
    :raises QrExportUnavailableError: R2 is not configured (the client cannot be made).
    """
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise QrExportBusyError(f"An export of {_status['object_id']} is already running.")
    files = (files_fn or _default_files)(folder)
    if not files:
        raise QrExportError(f"Object {folder.name} has no model files to export.")
    try:
        client = (client_factory or _default_client)()
    except Exception as e:
        raise QrExportUnavailableError(f"Cloudflare R2 is not available: {e}") from e
    total = sum(p.stat().st_size for p, _ in files)
    with _lock:
        if _status["state"] in RUNNING_STATES:
            raise QrExportBusyError(f"An export of {_status['object_id']} is already running.")
        _status.update(state="archiving", object_id=folder.name, archive=f"{folder.name}.zip", url=None,
                       expires_at=None, bytes_total=total, bytes_done=0, reused=False, error=None,
                       started_at=time.time(), finished_at=None)
    threading.Thread(target=_export, args=(client, folder, files), name="qr-export", daemon=True).start()
    return export_status()


def _is_missing(error: Exception) -> bool:
    code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchBucket", "NoSuchKey", "NotFound")


def _schedule_auto_delete(client, bucket: str, key: str, delay_s: float) -> None:
    """Schedules the deletion of an export archive after its download link expires."""
    def _delete():
        try:
            client.delete_object(Bucket=bucket, Key=key)
            log.info("[QR export] Auto-deleted expired archive %s/%s after %.0f seconds.", bucket, key, delay_s)
        except Exception as e:
            log.warning("[QR export] Could not auto-delete expired archive %s/%s: %s", bucket, key, e)

    t = threading.Timer(max(0.01, float(delay_s)), _delete)
    t.daemon = True
    t.name = f"qr-expire-{key}"
    t.start()


def ensure_bucket(client, bucket: str) -> None:
    """
    Makes sure the export bucket exists: created when missing (needs an R2 key with "Admin Read & Write").

    :raises QrExportError: missing and it could not be created, or the key has no access to it.
    """
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        # Cloudflare R2 returns 403 on HeadBucket for tokens with "Object Read & Write"
        # (normal for non-admin tokens). If the bucket exists, object operations will succeed.
        if code in ("403", "AccessDenied", "Forbidden"):
            log.info("[QR export] HeadBucket returned %s (expected for R2 Object Read & Write tokens); proceeding directly to object operations.", code)
            return
        if not _is_missing(e):
            raise QrExportError(f"The R2 key has no access to the export bucket {bucket!r} ({e}); give the key "
                                f"access to it in the Cloudflare dashboard.") from e
    try:
        client.create_bucket(Bucket=bucket)
        log.info("[QR export] Created the R2 bucket %s.", bucket)
    except Exception as e:
        raise QrExportError(f"The export bucket {bucket!r} does not exist and this R2 key may not create it: create "
                            f"it in the Cloudflare dashboard (R2 > Create bucket, then a lifecycle rule that deletes "
                            f"objects after 1 day) ({e}).") from e


def _export(client, folder: Path, files: list[tuple[Path, str]]) -> None:
    name = folder.name
    key = f"{name}.zip"
    bucket = export_bucket()
    part = folder.parent / f".{name}.qr.zip.part"   # next to the results (not /tmp, which is RAM on the Pi)
    try:
        ensure_bucket(client, bucket)
        fp = fingerprint(files)
        reused = False
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            reused = (head.get("Metadata") or {}).get("fingerprint") == fp
        except Exception as e:
            if not _is_missing(e):
                raise
        if not reused:
            done = 0
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                for path, arcname in files:
                    zf.write(path, arcname)
                    done += path.stat().st_size
                    _set(bytes_done=done)
            size = part.stat().st_size
            _set(state="uploading", bytes_total=size, bytes_done=0)
            def progress(n: int) -> None:
                # boto3 calls it from its upload threads
                with _lock:
                    _status["bytes_done"] += n

            client.upload_file(str(part), bucket, key, Callback=progress,
                               ExtraArgs={"ContentType": "application/zip", "Metadata": {"fingerprint": fp}})
        ttl = link_ttl_s()
        url = client.generate_presigned_url(
            "get_object", ExpiresIn=int(ttl),
            Params={"Bucket": bucket, "Key": key, "ResponseContentDisposition": f'attachment; filename="{key}"'},
        )
        _set(state="done", url=url, expires_at=time.time() + ttl, reused=reused, finished_at=time.time())
        log.info("[QR export] %s ready in %s/%s (%s, valid %.0fs).", name, bucket, key, "reused" if reused else "uploaded", ttl)
        _schedule_auto_delete(client, bucket, key, ttl)
    except Exception as e:
        log.error("[QR export] Export of %s failed: %s", name, e)
        _set(state="error", error=str(e), finished_at=time.time())
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def qr_svg(url: str, scale: int = 8) -> str:
    """The QR code of `url` as an SVG document.

    :raises QrExportError: segno (the QR library) is not installed.
    """
    try:
        import segno
    except ImportError as e:
        raise QrExportUnavailableError("The QR library is missing on the Pi (pip install segno).") from e
    out = io.BytesIO()
    segno.make(url, error="m").save(out, kind="svg", scale=scale, border=2)
    return out.getvalue().decode()
