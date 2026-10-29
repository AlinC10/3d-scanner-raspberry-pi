from fastapi import HTTPException

# While the scanner is in one of these states it owns the uplink: the photos are uploaded to R2 (or about to be). A
# radio scan slows the Wi-Fi down (Wi-Fi and Bluetooth share the antenna of the Pi) and a power or connection change
# can cut it (SYS-10, SYS-21). Reading the status is always allowed.
SCAN_BUSY_STATES = ("preparing", "running", "processing", "downloading")


def network_change_allowed() -> None:
    """
    FastAPI dependency of every route that scans the radio or changes a connection: 409 while a scan runs or uploads.
    The scanner is not built here (a server that never opened the rig has no scan).
    """
    from router.scanner import scanner
    if not getattr(scanner, "initialized", False):
        return
    state = getattr(scanner, "state", None)
    uploading = False
    progress = getattr(scanner, "upload_progress", None)
    if callable(progress):
        try:
            uploading = (progress() or {}).get("seconds_since_last_upload") is not None
        except Exception:
            uploading = False
    if state in SCAN_BUSY_STATES or uploading:
        raise HTTPException(
            status_code=409,
            detail=f"A scan is running (state {state}): network scans and connection changes are refused until it "
                   "ends, so the photo upload is not disturbed. GET /system/status still works.",
        )
