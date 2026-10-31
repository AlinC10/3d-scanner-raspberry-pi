from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated
import logging
import threading
import contextlib
import importlib
from schemas.scanner import *

try:
    FaceCaptureRequest
except NameError:
    class FaceCaptureRequest: pass

try:
    FaceCaptureResponse
except NameError:
    class FaceCaptureResponse: pass

try:
    FacePrepareRequest
except NameError:
    class FacePrepareRequest: pass

# What a prepare readies the rig for (hardware.scanner.ScanMode); plain strings so the mock scanner needs nothing more
MODE_OBJECT = "object"
MODE_FACE = "face"

import os
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE") == "1"

if MOCK_HARDWARE:
    from hardware.scanner_mock import ScannerMock as Scanner
    from hardware.scanner_mock import ScannerState
else:
    from hardware.scanner import Scanner, ScannerState
from pathlib import Path
from app_paths import results_directory
from storage.results import copy_photos, set_category
# cloud.job / cloud.runpod are imported lazily (see _load_cloud_job): a missing RunPod configuration must not affect scans
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


def _load_cloud_job():
    """Imports cloud.job (Turntable Studio's whole cloud flow for one scan) on first use."""
    return importlib.import_module("cloud.job")


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
        job_module = _load_cloud_job()
    except Exception as e:
        return f"The cloud module could not be loaded: {e}"

    missing = [name for name in ("RUNPOD_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_R2_API_TOKEN",
                                 "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
               if not os.environ.get(name, "").strip()]
    if missing:
        return f"missing environment variable(s) {', '.join(missing)}; configure them or send cloud.launch_runpod=false."
    image_env = getattr(job_module, "DEFAULT_IMAGE_ENV", "TURNTABLE_IMAGE")
    if not os.environ.get(image_env, "").strip():
        return f"{image_env} (the Turntable Studio Docker image) is not set; set it or send cloud.launch_runpod=false."
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


def _unavailable(error: BaseException) -> bool:
    """A missing device (ArduinoUnavailableError: `unavailable` is set) is a 503, not a server fault."""
    return getattr(error, "unavailable", False) is True


def require_arduino() -> None:
    """
    503 when the Arduino (motor controller) does not answer. Called before every request that moves a motor; the
    scanner reconnects first, so it also detects an Arduino plugged in after the server started (API-21).
    """
    check = getattr(scanner, "require_motors", None)
    if not callable(check):
        return
    try:
        check()
    except Exception as e:
        if _unavailable(e):
            raise HTTPException(status_code=503, detail=str(e))
        raise


def _finish_preparation_worker(kwargs: dict):
    scanner.finish_preparation(**kwargs)


def _start_preparation(background_tasks: BackgroundTasks, brightness: float, enable_stream: bool,
                       camera_config: dict, finish_kwargs: dict, **setup_kwargs) -> PrepareResponse:
    """
    Common part of /prepare and /face/prepare: refuses a busy rig or a wrong state before anything is touched, starts
    the lights and the cameras, and leaves the homing and the climb to a background task.
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
        scanner.lights.set_brightness(brightness)

        urls = scanner.setup_and_stream(enable_stream=enable_stream, **setup_kwargs, **camera_config)

        # z_start_mm is not a camera setting: it must not reach prepare_scan(**kwargs) through camera_config
        background_tasks.add_task(_finish_preparation_worker, {**camera_config, **finish_kwargs})

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
        # No Arduino: scanning is unavailable until it answers again (503)
        if _unavailable(e):
            raise HTTPException(status_code=503, detail=str(e))
        # A busy rig (ScannerBusyError, a locked light) is a temporary refusal, not a server fault: 409
        if getattr(e, "conflict", False) is True:
            raise HTTPException(status_code=409, detail=str(e))
        # Hardware errors, S3 bucket cleanup errors, etc.
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/prepare", response_model=PrepareResponse, status_code=202)
def prepare_scan(req: PrepareRequest, background_tasks: BackgroundTasks):
    """
    Prepares an object scan: lights, cameras and live stream at once; homing, climb to `z_start_mm` and camera lock in
    the background. Poll /status until `prepared` (mode `object`), then POST /scanner/start.
    """
    return _start_preparation(
        background_tasks, req.illumination_brightness, req.enable_stream, req.camera_config.model_dump(),
        {"z_start_mm": req.z_start_mm, "z_delay": req.z_delay}, scan_mode=MODE_OBJECT,
    )


@router.post("/face/prepare", response_model=PrepareResponse, status_code=202)
def prepare_face_scan(req: FacePrepareRequest, background_tasks: BackgroundTasks):
    """
    Prepares a face scan: lights, cameras and live stream at once; homing and climb to `start_z_mm` in the background.
    Poll /status until `prepared` (mode `face`), then POST /scanner/face/capture. MediaPipe and its model are checked
    first (400 when missing): a face scan cannot frame without them.
    """
    problem_check = getattr(scanner, "_face_detection_problem", None)
    problem = problem_check() if callable(problem_check) else None
    if isinstance(problem, str) and problem:
        raise HTTPException(status_code=400, detail=problem)
    return _start_preparation(
        background_tasks, req.illumination_brightness, req.enable_stream, req.camera_config.model_dump(),
        {"z_start_mm": req.start_z_mm, "upload_r2": req.upload_r2, "z_delay": req.z_delay}, scan_mode=MODE_FACE, check_cloud=req.upload_r2,
    )


# What a failed cloud stage needs to run again without a new scan (UX-3): set when run_cloud_job fails (the photos
# stay in R2 under jobs/<job_id>/input/ and on disk), cleared when a cloud stage succeeds
_cloud_retry: dict = {}


def _run_cloud_stage(cloud_job, options: dict) -> None:
    """
    Runs Turntable Studio on RunPod for the photos of scanner.job_id (already uploaded and verified) and saves the
    model in results/<job_id>/. The state must already be PROCESSING. Raises on a failed job (the caller ends in ERROR);
    the job is then remembered in `_cloud_retry` for POST /scanner/cloud/retry.
    """
    job_id = scanner.job_id
    _cloud_retry.clear()
    _cloud_retry.update(job_id=job_id, options=dict(options), images_dir=str(scanner.images_dir))
    _cloud_stage_body(cloud_job, options, job_id)
    if scanner.state == ScannerState.COMPLETED:
        _cloud_retry.clear()


def _cloud_stage_body(cloud_job, options: dict, job_id: str) -> None:
    # Technical detail (pod polling every few seconds, uploads, retries) goes to `cloud_message`; `phase_message`
    # changes only when a phase of the reconstruction is finished, so the screen shows a steady progress (CLOUD-24)
    def _progress(message: str) -> None:
        log.info("[Cloud %s] %s", job_id, message)
        if scanner.state == ScannerState.PROCESSING:
            scanner.cloud_message = message

    def _phase(message: str) -> None:
        log.info("[Cloud %s] Phase: %s", job_id, message)
        if scanner.state == ScannerState.PROCESSING:
            scanner.phase_message = message

    scanner.cloud_message = None
    _phase("Preparing the 3D reconstruction in the cloud")

    report = cloud_job.run_cloud_job(
        input_dir=str(Path(scanner.images_dir).parent),
        image_prefix=str(Path(scanner.images_dir).name) + "/",
        results_dir=str(results_directory()),
        job_id=job_id,
        pipeline_options=options,
        dry_run=False,
        skip_upload=True,
        cancel_event=scanner._cancel_event,
        on_progress=_progress,
        on_phase=_phase,
    )

    if _cancel_requested() or scanner.state == ScannerState.CANCELLED:
        scanner.state = ScannerState.CANCELLED
        log.info("Job cancelled during cloud processing.")
        return

    if report.get("result") != "ok":
        status = report.get("status") or {}
        raise RuntimeError(f"Turntable Studio job {job_id} failed: {status.get('state') or 'no pod result'} "
                           f"{status.get('error', '')}".strip())

    # 4. The result is already extracted in results/<job_id>/ (served by /library); the photos are kept beside it
    copy_photos(Path(scanner.images_dir), results_directory() / job_id)
    set_category(results_directory(), job_id, "object")
    scanner.result_id = job_id
    if _set_state_unless_cancelled(ScannerState.COMPLETED):
        log.info("Full pipeline completed: model in %s (cost estimate %s).",
                 report.get("results_dir"), report.get("cost_estimate"))


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
                finish_state=ScannerState.PROCESSING,  # no IDLE gap between the scan and the cloud pipeline
                job_id=req.job_id,
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

        # 3. Cloud processing: Turntable Studio on one RunPod pod (cloud.job.run_cloud_job). The photos are already in
        # jobs/<job_id>/input/ (uploaded and verified during the scan), so the upload step is skipped. Every pod is
        # terminated in run_pod's `finally` (also on a cancel: the cancel event is passed through), pods of older jobs
        # are terminated before a new one starts, and the R2 job folder is deleted once the result is extracted.
        cloud_job = _load_cloud_job()
        if not _set_state_unless_cancelled(ScannerState.PROCESSING):
            log.info("Scan was cancelled, aborting cloud pipeline.")
            return

        options = {
            "rig_mode": True,
            "resolution_level": req.cloud.resolution_level,
            "lift_mm": req.mechanical.z_axis.distance_mm,
            "step_deg": req.mechanical.turntable.angle,   # turntable step: the pod derives the camera poses from it
            "texturing_engine": "texrecon",
        }
        # Untitled: the object is named after its folder (the job id), the same name as in results/ and on a USB export
        options["title"] = req.cloud.title or scanner.job_id
        _run_cloud_stage(cloud_job, options)

    except Exception as e:
        log.error("Pipeline failed: %s", e)
        if scanner.state in (ScannerState.PROCESSING, ScannerState.DOWNLOADING):
            scanner.error = str(e)
        _set_state_unless_cancelled(ScannerState.ERROR)
    finally:
        with _start_lock:
            _start_state["pending"] = False

def _cloud_retry_problem() -> str | None:
    """Why POST /scanner/cloud/retry is refused now (None when it can run)."""
    if not _cloud_retry:
        return "There is no failed cloud reconstruction to retry."
    if scanner.state not in (ScannerState.ERROR, ScannerState.CANCELLED):
        return f"Cannot retry from state {scanner.state}; a retry follows a failed or cancelled reconstruction."
    if _cloud_retry.get("job_id") != getattr(scanner, "job_id", None):
        return "A newer scan replaced the photos of the failed reconstruction."
    images_dir = Path(_cloud_retry.get("images_dir") or "")
    if not images_dir.is_dir() or not any(images_dir.rglob("*.jpg")):
        return "The photos of the failed reconstruction are no longer on the Pi."
    return _runpod_config_problem()


def _retry_cloud_pipeline() -> None:
    """Background worker of POST /scanner/cloud/retry: the cloud stage again, same job and options."""
    try:
        if _cancel_requested() or not _set_state_unless_cancelled(ScannerState.PROCESSING):
            return
        _run_cloud_stage(_load_cloud_job(), dict(_cloud_retry["options"]))
    except Exception as e:
        log.error("Cloud retry failed: %s", e)
        if scanner.state == ScannerState.PROCESSING:
            scanner.error = str(e)
        _set_state_unless_cancelled(ScannerState.ERROR)
    finally:
        with _start_lock:
            _start_state["pending"] = False


@router.post("/cloud/retry", response_model=StartResponse, status_code=202)
def retry_cloud_reconstruction(background_tasks: BackgroundTasks):
    """
    Runs the 3D reconstruction of the last object scan again on RunPod (UX-3), after it failed or was cancelled in
    `processing`: same job id, same options, the photos already in the cloud (no new scan, no new upload). `400` when
    there is nothing to retry, a newer scan replaced the photos or RunPod is not configured; `409` while a run is busy.
    Billed like a scan.
    """
    with _start_lock:
        if _start_state["pending"] or getattr(scanner, "_stopping", False) is True:
            raise HTTPException(status_code=409, detail="A scan, a retry or an emergency stop is still running.")
        problem = _cloud_retry_problem()
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        _start_state["pending"] = True
        scanner.error = None
        if hasattr(scanner, "_cancel_event"):
            scanner._cancel_event.clear()
    background_tasks.add_task(_retry_cloud_pipeline)
    return StartResponse(status="accepted", message=f"Retrying the reconstruction of {scanner.job_id}.")


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
        if getattr(scanner, "mode", MODE_OBJECT) == MODE_FACE:
            raise HTTPException(
                status_code=400,
                detail="The scanner is prepared for a face scan; call POST /scanner/prepare for an object scan.",
            )
        if not _start_state["pending"]:
            require_arduino()   # the USB link may have dropped since /prepare
            # The object scan needs the ToF sensor (end of the object); it is reinitialised once if it stopped
            require_tof = getattr(scanner, "require_tof", None)
            if callable(require_tof):
                try:
                    require_tof()
                except Exception as e:
                    raise HTTPException(status_code=400, detail=str(e))
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

def _face_scan_worker(params: dict):
    """
    Background part of POST /face/capture. face_scan() publishes the outcome itself (COMPLETED with `face_result`,
    ERROR with `error`, or CANCELLED); here a failure is only logged.
    """
    try:
        scanner.face_scan(**params, begun=True, finish_state=ScannerState.COMPLETED)
    except Exception as e:
        log.error("Face scan failed: %s", e)
        # A failure before face_scan() changed the state (e.g. a /test move grabbed the rig in between) must not leave
        # the scanner RUNNING with nobody behind it
        if scanner.state == ScannerState.RUNNING:
            _set_state_unless_cancelled(ScannerState.ERROR)
    finally:
        with _start_lock:
            _start_state["pending"] = False


@router.post("/face/capture", response_model=FaceCaptureResponse, status_code=202)
def face_capture(req: FaceCaptureRequest, background_tasks: BackgroundTasks):
    """
    Starts the face capture in the background, after POST /scanner/face/prepare: the height (search with MediaPipe on
    fresh preview frames, `manual_z`, or none), the camera lock on the face, the synchronized stereo pair and the
    verified upload to R2.

    Answers 202 once the request is validated and the scanner is RUNNING; poll `GET /scanner/status` until the state
    is `completed` (the result is in `face_result`), `error` or `cancelled`. 400 when the scanner is not prepared for
    a face scan.
    """
    guard = getattr(scanner, "motion_guard", None)
    if guard is not None:
        try:
            with guard():
                pass
        except Exception as busy:
            if getattr(busy, "conflict", False):
                raise HTTPException(status_code=409, detail=str(busy))
            raise

    params = req.model_dump()
    with _start_lock:
        if _start_state["pending"]:
            raise HTTPException(status_code=409, detail="A scan was already accepted and is still running.")
        try:
            scanner.begin_face_scan(**params)
        except Exception as e:
            if _unavailable(e):
                raise HTTPException(status_code=503, detail=str(e))
            if getattr(e, "conflict", False) is True:
                raise HTTPException(status_code=409, detail=str(e))
            if isinstance(e, ValueError):
                raise HTTPException(status_code=422, detail=str(e))
            if isinstance(e, RuntimeError) or type(e).__name__.startswith("Scanner"):
                raise HTTPException(status_code=400, detail=str(e))   # preflight refused, scanner refused
            raise HTTPException(status_code=500, detail=str(e))
        _start_state["pending"] = True

    background_tasks.add_task(_face_scan_worker, params)
    return FaceCaptureResponse(state=scanner.state)

def _face_result() -> dict | None:
    """Outcome of the last completed face scan (None when there is none)."""
    value = getattr(scanner, "face_result", None)
    return value if isinstance(value, dict) else None


def _upload_progress() -> dict:
    """`seconds_since_last_upload` / `upload_drain_timeout_s` of the scanner (missing on older scanner objects)."""
    progress = getattr(scanner, "upload_progress", None)
    if not callable(progress):
        return {}
    try:
        value = progress()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


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

    mode = getattr(scanner, "mode", None)
    return StatusResponse(
        state=scanner.state,
        mode=mode if isinstance(mode, str) else None,
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
        face_result=_face_result(),
        job_id=_text("job_id"),
        result_id=_text("result_id"),
        cloud_message=_text("cloud_message"),
        **_upload_progress(),
    )


# ── Live preview endpoints ───────────────────────────────────────────────────
# These four are the only implementation: router/test.py registers the same functions under /test/stream/*.
# They accept the camera / quality both as a query parameter and as a JSON body (the /test endpoints always did).

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
            detail="Cameras are not streaming. Call POST /scanner/prepare first (or POST /test/stream/start for a manual session)."
        )

    # The cameras object is kept for the whole response: scanner.dual_cameras becomes None when the stream is stopped
    cams = scanner.dual_cameras
    if camera is not None and cams.supports_preview_stream:
        cams.active_camera = camera

    cam_idx = (camera - 1) if camera in (1, 2) else cams.active

    def frame_generator():
        cams.add_spectator(cam_idx)
        try:
            last_id = -1
            while getattr(scanner, "is_streaming", False) and scanner.dual_cameras is cams:
                if not cams.supports_preview_stream:
                    break  # this driver has no live preview
                frame_id, frame_bytes = cams.wait_jpeg(cam_idx, last_id, timeout=1.0)
                if frame_bytes and frame_id != last_id:
                    last_id = frame_id
                    yield (
                        b"--FRAME\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n\r\n"
                        + frame_bytes + b"\r\n"
                    )
        finally:
            cams.remove_spectator(cam_idx)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=FRAME"
    )


@router.post("/stream/active")
def scanner_stream_switch_active(
    camera_id: Annotated[Optional[int], Query(ge=1, le=2, description="Camera ID (1 or 2)")] = None,
    req: Optional[StreamActiveRequest] = None,
):
    """
    Switches active preview camera (1 for cam1, 2 for cam2) with zero latency.
    The camera id comes from the `camera_id` query parameter or from the JSON body.
    """
    target_id = camera_id if camera_id is not None else (req.camera_id if req is not None else None)
    if target_id is None:
        raise HTTPException(status_code=400, detail="Must provide camera_id via query param or JSON body.")

    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if not scanner.dual_cameras.supports_preview_stream:
        raise HTTPException(status_code=400, detail="Active camera switching not supported by current camera driver.")

    scanner.dual_cameras.active_camera = target_id
    return {
        "status": "success",
        "active_camera": scanner.dual_cameras.active_camera,
        "message": f"Switched preview to camera {target_id}."
    }


@router.post("/stream/quality")
def scanner_stream_set_quality(
    quality: Annotated[Optional[int], Query(ge=1, le=100, description="Quality 1-100")] = None,
    req: Optional[StreamQualityRequest] = None,
):
    """
    Dynamically updates live JPEG compression quality (1-100) on active stream.
    The quality comes from the `quality` query parameter or from the JSON body.
    """
    target_quality = quality if quality is not None else (req.quality if req is not None else None)
    if target_quality is None:
        raise HTTPException(status_code=400, detail="Must provide quality via query param or JSON body.")

    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    # stream_quality belongs to the shared interface: it never touches the quality of the scan photos
    scanner.dual_cameras.stream_quality = target_quality
    current_q = scanner.dual_cameras.stream_quality

    return {
        "status": "success",
        "quality": current_q,
        "message": f"Updated stream quality to {target_quality}."
    }


@router.get("/stream/sync-status")
def scanner_stream_sync_status():
    """
    Returns hardware/software synchronization diagnostics (SyncReady, offsets, framerates, drops).
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    return scanner.dual_cameras.get_sync_status()
