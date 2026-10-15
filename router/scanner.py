from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated
import logging
import threading
import contextlib
import importlib
from schemas.scanner import *

import os
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE") == "1"

if MOCK_HARDWARE:
    from hardware.scanner_mock import ScannerMock as Scanner
    from hardware.scanner_mock import ScannerState
else:
    from hardware.scanner import Scanner, ScannerState
import cloud.cloudflare_r2 as r2
# cloud.runpod is imported lazily (see _load_runpod): a missing RunPod package or configuration must not affect scans
# that do not launch RunPod.

log = logging.getLogger(__name__)

# Exception class raised by scan() when the verification finds photos missing from the bucket. Resolved defensively:
# older scanner modules and the mock scanner do not define it (the router then matches it by class name).
try:
    if MOCK_HARDWARE:
        from hardware.scanner_mock import ScannerUploadError as _ScannerUploadError
    else:
        from hardware.scanner import ScannerUploadError as _ScannerUploadError
    if not (isinstance(_ScannerUploadError, type) and issubclass(_ScannerUploadError, BaseException)):
        _ScannerUploadError = None
except ImportError:
    _ScannerUploadError = None

# At most this many missing keys are echoed by GET /status and the log
_MISSING_KEYS_REPORTED = 50
_MISSING_KEYS_LOGGED = 20

router = APIRouter(
    prefix="/scanner",
    tags=["Scanner"],
)

class _LazyScanner:
    """
    Process-wide scanner singleton that is built on first use instead of at import time.

    Building a Scanner opens the Arduino serial port (which resets the board) and claims the GPIO pins, so doing it
    as an import side effect also happened in tooling, tests and the --reload watcher. The application creates it
    explicitly in its lifespan startup (`scanner.get()`); attribute access builds it on demand otherwise.
    The hardware can only be owned by one process: run a single worker.
    """

    def __init__(self, factory):
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_instance", None)
        object.__setattr__(self, "_init_lock", threading.Lock())

    @property
    def initialized(self) -> bool:
        return object.__getattribute__(self, "_instance") is not None

    def get(self):
        instance = object.__getattribute__(self, "_instance")
        if instance is None:
            with object.__getattribute__(self, "_init_lock"):
                instance = object.__getattribute__(self, "_instance")
                if instance is None:
                    instance = object.__getattribute__(self, "_factory")()
                    object.__setattr__(self, "_instance", instance)
        return instance

    def __getattr__(self, name):
        return getattr(self.get(), name)

    def __setattr__(self, name, value):
        setattr(self.get(), name, value)


# `Scanner` is already the mock class when MOCK_HARDWARE=1 (see the imports above)
scanner = _LazyScanner(lambda: Scanner(xvs=False))

# A POST /start is accepted while the scanner is still PREPARED (scan() flips the state later, in a background
# task). This flag closes that window so two quick requests cannot start two pipelines.
_start_lock = threading.Lock()
_start_state = {"pending": False}

def _load_runpod():
    """Imports cloud.runpod on first use (only scans that set cloud.launch_runpod need it)."""
    return importlib.import_module("cloud.runpod")


def __getattr__(name):
    # Backward compatibility: `router.scanner.runpod` used to be a module-level import
    if name == "runpod":
        return _load_runpod()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_upload_error(error: BaseException) -> bool:
    if _ScannerUploadError is not None and isinstance(error, _ScannerUploadError):
        return True
    return type(error).__name__ == "ScannerUploadError"


def _runpod_config_problem() -> str | None:
    """
    Returns why RunPod cannot be launched with the current configuration (None when it looks usable), so a scan that
    asks for RunPod is refused before the physical scan instead of failing after it (API-10, CLOUD-11).
    """
    try:
        runpod_module = _load_runpod()
    except Exception as e:
        return f"The RunPod module could not be loaded: {e}"

    api_key = getattr(runpod_module, "RUNPOD_API_KEY", None)
    if not isinstance(api_key, str) or not api_key.strip():
        return "RUNPOD_API_KEY is not set; configure it or send cloud.launch_runpod=false."

    image = getattr(runpod_module, "IMAGE_NAME", None)
    if not isinstance(image, str) or not image.strip() or image.strip().lower().startswith("yourdockerhubuser/"):
        return "The RunPod image name is not configured (placeholder value); set it or send cloud.launch_runpod=false."
    return None


def _cancel_requested() -> bool:
    cancel_event = getattr(scanner, "_cancel_event", None)
    return cancel_event is not None and cancel_event.is_set() is True


def _set_state_unless_cancelled(new_state) -> bool:
    """
    Compare-and-set of the scanner state (API-1): writes `new_state` unless the run was cancelled meanwhile, in which
    case the state is left (or forced) as CANCELLED. The cancel flag is set before emergency_stop() has published the
    CANCELLED state, so it counts as well. Returns whether `new_state` was written.
    """
    lock = getattr(scanner, "_lock", None)
    guard = lock if hasattr(lock, "__enter__") and hasattr(lock, "__exit__") else contextlib.nullcontext()
    with guard:
        if scanner.state == ScannerState.CANCELLED:
            return False
        if _cancel_requested():
            scanner.state = ScannerState.CANCELLED
            return False
        scanner.state = new_state
        return True


def _finish_preparation_worker(kwargs: dict):
    scanner.finish_preparation(**kwargs)

@router.post("/prepare", response_model=PrepareResponse, status_code=202)
def prepare_scan(req: PrepareRequest, background_tasks: BackgroundTasks):
    """
    Safely triggers the homing, lighting, and camera initializations for the frontend framing.
    """
    # A manual /test move (or any motion still in flight) owns the rig: do not start preparing on top of it.
    # Entering and leaving the guard is only a non-blocking probe; `conflict` is duck-typed for the mock scanner.
    guard = getattr(scanner, "motion_guard", None)
    if guard is not None:
        try:
            with guard():
                pass
        except Exception as busy:
            if getattr(busy, "conflict", False):
                raise HTTPException(status_code=409, detail=str(busy))
            raise

    # Refuse a wrong state before touching anything: a refused /prepare must not change the LED brightness.
    # setup_and_stream() validates the state again, atomically; this is only the early, side-effect free check.
    if getattr(scanner, "_stopping", False) is True:
        raise HTTPException(status_code=409, detail="Cannot prepare. An emergency stop is still in progress.")
    state = scanner.state
    allowed = getattr(ScannerState, "PREPARE_ALLOWED", None)
    if isinstance(state, str) and allowed is not None and state not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot prepare. Scanner is currently {state}")

    try:
        scanner.lights.set_brightness(req.illumination_brightness)
        
        urls = scanner.setup_and_stream(
            enable_stream=req.enable_stream,
            bitrate=req.bitrate,
            **req.camera_config.model_dump()
        )
        
        background_tasks.add_task(_finish_preparation_worker, req.camera_config.model_dump())
        
        return PrepareResponse(
            status="accepted", 
            message="Live stream started. Rig homing and camera tuning in progress.", 
            state=scanner.state, 
            stream_urls=urls
        )
    except RuntimeError as e:
        # State validation failures. A refusal that is only temporary (an emergency stop is still tearing the previous
        # run down: `conflict` is set on the error) is a 409, not a 400. Duck-typed so the mock scanner needs no import.
        status = 409 if getattr(e, "conflict", False) else 400
        raise HTTPException(status_code=status, detail=str(e))
    except Exception as e:
        # A busy rig (ScannerBusyError, a locked light) is a temporary refusal, not a server fault: 409
        if getattr(e, "conflict", False) is True:
            raise HTTPException(status_code=409, detail=str(e))
        # Hardware errors, S3 bucket cleanup errors, etc.
        raise HTTPException(status_code=500, detail=str(e))

def _orchestrate_pipeline(req: StartRequest):
    """
    Background worker that runs the mechanical scan and, only when the request asks for it (cloud.launch_runpod), the
    cloud pipeline. Updates the global scanner.state natively so the frontend polling loop is always accurate.
    Every state write goes through _set_state_unless_cancelled(): a CANCELLED state is never overwritten.
    """
    try:
        # 1. Mechanical Scan (scan() returns once the photos are also verified in the bucket)
        try:
            scanner.scan(
                angle=req.mechanical.turntable.angle,
                delay_turntable=req.mechanical.turntable.delay,
                z_move_mm=req.mechanical.z_axis.distance_mm,
                delay_z_motor=req.mechanical.z_axis.delay,
                turntable_acceleration=req.mechanical.turntable.acceleration,
                turntable_start_delay=req.mechanical.turntable.start_delay,
                turntable_ramp_percent=req.mechanical.turntable.ramp_percent,
                turntable_decel_percent=req.mechanical.turntable.decel_percent,
                z_acceleration=req.mechanical.z_axis.acceleration,
                z_start_delay=req.mechanical.z_axis.start_delay,
                z_ramp_percent=req.mechanical.z_axis.ramp_percent,
                z_decel_percent=req.mechanical.z_axis.decel_percent,
                turntable_end_delay=req.mechanical.turntable.end_delay,
                z_end_delay=req.mechanical.z_axis.end_delay,
                finish_state=ScannerState.PROCESSING  # no IDLE gap between the scan and the cloud pipeline
            )
        except RuntimeError as e:
            # scan() refused to start (the scanner is no longer PREPARED, e.g. cancelled in between).
            # That is not a failure of the running state: do not overwrite it.
            log.warning("Scan not started: %s", e)
            return
        except Exception as e:
            if not _is_upload_error(e):
                raise
            # The physical scan finished but photos are missing from the bucket. The scanner already carries
            # `missing` and `error` (shown by /status); the files stay on disk.
            missing = list(getattr(e, "missing", None) or getattr(scanner, "missing", None) or [])
            log.error(
                "Upload verification failed: %d photo(s) missing from the bucket (first %d: %s): %s",
                len(missing), _MISSING_KEYS_LOGGED, missing[:_MISSING_KEYS_LOGGED], e,
            )
            _set_state_unless_cancelled(ScannerState.ERROR)
            return

        # Check if it was cancelled during the scan (the cancel flag is set before emergency_stop() has time to
        # publish the CANCELLED state)
        if scanner.state == ScannerState.CANCELLED or _cancel_requested():
            scanner.state = ScannerState.CANCELLED
            log.info("Scan was cancelled, aborting cloud pipeline.")
            return

        # Check if the upload worker failed or stalled
        if scanner.state == ScannerState.ERROR:
            log.error("Scan ended in error state (e.g. stalled uploads). Aborting cloud pipeline.")
            return

        # 2. RunPod is optional: without it the scan is done once the photos are in the bucket
        if getattr(req.cloud, "launch_runpod", False) is not True:
            if _set_state_unless_cancelled(ScannerState.COMPLETED):
                log.info("Scan completed: photos verified in the bucket (RunPod not requested).")
            return

        # 3. Cloud Processing
        runpod = _load_runpod()
        if not _set_state_unless_cancelled(ScannerState.PROCESSING):
            log.info("Scan was cancelled, aborting cloud pipeline.")
            return

        job = {
            "job_id": req.job_id,
            "photo_count": scanner.total_photos,
            "resolution_mp": req.cloud.resolution_mp,
            "mode": req.cloud.mode,
            "depthmap_downscale": req.cloud.depthmap_downscale,
            "max_input_points": req.cloud.max_input_points
        }

        success = runpod.launch_job(job, cancel_event=scanner._cancel_event)

        if _cancel_requested() or scanner.state == ScannerState.CANCELLED:
            scanner.state = ScannerState.CANCELLED
            log.info("Job cancelled during cloud processing.")
            return

        if not success:
            raise RuntimeError("RunPod processing failed.")

        # 4. Downloading Assets
        if not _set_state_unless_cancelled(ScannerState.DOWNLOADING):
            log.info("Job cancelled before the download.")
            return
        # R2 download_generated_obj defaults to dest_dir=os.getcwd() if not provided, but let's be explicit
        success = r2.download_generated_obj(object_name="output.zip", dest_dir=r2.OUTPUT_DIR)

        if not success:
            raise RuntimeError("Failed to download or extract output.zip from R2.")

        # 5. Completed! (not if a cancel arrived during the download)
        if _set_state_unless_cancelled(ScannerState.COMPLETED):
            log.info("Full pipeline completed successfully.")
        else:
            log.info("Job cancelled during the download; state stays CANCELLED.")

    except Exception as e:
        log.error("Pipeline failed: %s", e)
        _set_state_unless_cancelled(ScannerState.ERROR)
    finally:
        with _start_lock:
            _start_state["pending"] = False

@router.post("/start", response_model=StartResponse, status_code=202)
def start_scan(req: StartRequest, background_tasks: BackgroundTasks):
    """
    Delegates the mechanical scan and cloud execution to a background pipeline.
    """
    # RunPod requested: refuse now when it cannot work, not after the physical scan (API-10, CLOUD-11)
    if getattr(req.cloud, "launch_runpod", False) is True:
        problem = _runpod_config_problem()
        if problem:
            raise HTTPException(status_code=400, detail=f"RunPod is not configured: {problem}")

    with _start_lock:
        if scanner.state != ScannerState.PREPARED:
            raise HTTPException(status_code=400, detail=f"Cannot start scan from state {scanner.state}. Must be PREPARED.")
        if _start_state["pending"]:
            raise HTTPException(status_code=409, detail="A scan start was already accepted and is in progress.")
        _start_state["pending"] = True

    background_tasks.add_task(_orchestrate_pipeline, req)
    return StartResponse(status="accepted", message="Pipeline started.")

@router.post("/cancel", response_model=CancelResponse)
def cancel_scan():
    """
    Instantly stops all mechanical movement and terminates any running cloud pods.
    """
    try:
        scanner.emergency_stop()
        return CancelResponse(status="success", state=scanner.state)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/status", response_model=StatusResponse)
def get_status():
    """
    Poll this endpoint to observe the active pipeline state.
    """
    def _text(name):
        value = getattr(scanner, name, None)
        return value if isinstance(value, str) else None

    def _count(name):
        value = getattr(scanner, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _texts(name, limit=None):
        value = getattr(scanner, name, None)
        if not isinstance(value, (list, tuple, set, frozenset)):
            return []
        items = [str(item) for item in value]
        return items[:limit] if limit else items

    return StatusResponse(
        state=scanner.state,
        total_photos=_count("total_photos"),
        stream_urls=getattr(scanner, "stream_urls", None),
        phase=_text("phase"),
        phase_message=_text("phase_message"),
        level=_count("level"),
        photos_captured=_count("photos_captured"),
        photos_uploaded=_count("photos_uploaded"),
        missing=_texts("missing", _MISSING_KEYS_REPORTED),
        error=_text("error"),
        tof_warnings=_texts("tof_warnings"),
    )


@router.get("/stream/mjpeg")
def scanner_stream_mjpeg(
    camera: Annotated[Optional[int], Query(ge=1, le=2, description="Optional camera index (1 or 2) to activate")] = None
):
    """
    Streams multipart MJPEG video directly from the active camera to browser <img> elements.
    Accepts optional camera query parameter (1 or 2) to switch active view on connection.
    Only encodes JPEGs on demand while viewers are connected.
    """
    if scanner.dual_cameras is None or not getattr(scanner, "is_streaming", False):
        raise HTTPException(
            status_code=400,
            detail="Cameras are not streaming. Call /scanner/prepare first."
        )

    if camera is not None and hasattr(scanner.dual_cameras, "active_camera"):
        scanner.dual_cameras.active_camera = camera

    cam_idx = (camera - 1) if camera in (1, 2) else getattr(scanner.dual_cameras, "active", 0)

    def frame_generator():
        if hasattr(scanner.dual_cameras, "add_spectator"):
            scanner.dual_cameras.add_spectator(cam_idx)
        try:
            last_id = -1
            while getattr(scanner, "is_streaming", False):
                if not hasattr(scanner.dual_cameras, "wait_jpeg"):
                    break
                frame_id, frame_bytes = scanner.dual_cameras.wait_jpeg(cam_idx, last_id, timeout=1.0)
                if frame_bytes and frame_id != last_id:
                    last_id = frame_id
                    yield (
                        b"--FRAME\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n\r\n"
                        + frame_bytes + b"\r\n"
                    )
        finally:
            if hasattr(scanner.dual_cameras, "remove_spectator"):
                scanner.dual_cameras.remove_spectator(cam_idx)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=FRAME"
    )


@router.post("/stream/active")
def scanner_stream_switch_active(
    camera_id: Annotated[int, Query(ge=1, le=2, description="Camera ID (1 or 2)")]
):
    """
    Switches active preview camera (1 for cam1, 2 for cam2) with zero latency.
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if not hasattr(scanner.dual_cameras, "active_camera"):
        raise HTTPException(status_code=400, detail="Active camera switching not supported by current camera driver.")

    scanner.dual_cameras.active_camera = camera_id
    return {
        "status": "success",
        "active_camera": scanner.dual_cameras.active_camera,
        "message": f"Switched preview to camera {camera_id}."
    }


@router.post("/stream/quality")
def scanner_stream_set_quality(
    quality: Annotated[int, Query(ge=1, le=100, description="Quality 1-100")]
):
    """
    Dynamically updates live JPEG compression quality (1-100) on active stream.
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    try:
        from hardware.dual_camera_rpi_sync import DualCameraRPISync
        is_rpi_sync = isinstance(scanner.dual_cameras, DualCameraRPISync)
    except Exception:
        is_rpi_sync = False

    if is_rpi_sync:
        scanner.dual_cameras.stream_quality = quality
        current_q = scanner.dual_cameras.stream_quality
    else:
        scanner.dual_cameras.quality = quality
        current_q = scanner.dual_cameras.quality

    return {
        "status": "success",
        "quality": current_q,
        "message": f"Updated stream quality to {quality}."
    }


@router.get("/stream/sync-status")
def scanner_stream_sync_status():
    """
    Returns hardware/software synchronization diagnostics (SyncReady, offsets, framerates, drops).
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if hasattr(scanner.dual_cameras, "get_sync_status"):
        return scanner.dual_cameras.get_sync_status()

    return {
        "ready": True,
        "is_streaming": scanner.is_streaming,
        "mode": scanner.dual_cameras.__class__.__name__
    }