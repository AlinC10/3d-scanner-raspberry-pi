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

try:
    FlipResponse
except NameError:
    class FlipResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

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

try:
    from cloud.jobs_manager import jobs_manager, MAX_PARALLEL_CLOUD_JOBS
except Exception:
    MAX_PARALLEL_CLOUD_JOBS = 2
    import sys
    _mod = sys.modules.get("cloud.jobs_manager")
    if _mod is not None and type(_mod).__name__ != "MagicMock":
        jobs_manager = getattr(_mod, "jobs_manager", _mod)
        MAX_PARALLEL_CLOUD_JOBS = getattr(_mod, "MAX_PARALLEL_CLOUD_JOBS", 2)
    else:
        class _DummyJobManager:
            def register_job(self, *a, **k): pass
            def update_job(self, *a, **k): pass
            def get_job(self, *a, **k): return None
            def list_jobs(self): return []
            def get_active_pod_ids(self): return []
            def get_active_job_ids(self): return []
            def count_active_cloud_jobs(self, *a, **k): return 0
            def cancel_job(self, *a, **k): return False
        jobs_manager = _DummyJobManager()

def _new_job_id() -> str:
    try:
        from cloud.cloudflare_r2 import new_job_id
        return new_job_id()
    except Exception:
        import datetime, uuid
        return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
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
    job_img_dir = Path("input_images") / job_id / "rig"
    img_dir = job_img_dir if job_img_dir.is_dir() else Path(scanner.images_dir)
    _cloud_retry.clear()
    _cloud_retry.update(job_id=job_id, options=dict(options), images_dir=str(img_dir))
    try:
        _cloud_stage_body(cloud_job, options, job_id)
        if scanner.state == ScannerState.COMPLETED:
            _cloud_retry.clear()
    except Exception:
        # Move scan input photos to results/<job_id>/ even if cloud reconstruction fails
        dest_folder = results_directory() / job_id
        src_photos = job_img_dir if job_img_dir.is_dir() else Path(scanner.images_dir)
        dest_folder.mkdir(parents=True, exist_ok=True)
        copied = copy_photos(src_photos, dest_folder, move=True)
        set_category(results_directory(), job_id, "object")
        if isinstance(copied, (int, float)) and copied > 0 and not src_photos.exists() and _cloud_retry.get("job_id") == job_id:
            _cloud_retry["images_dir"] = str(dest_folder / "photos" / src_photos.name)
        raise


def _cloud_callbacks(job_id: str):
    """
    (on_progress, on_phase) of run_cloud_job. Technical detail (pod polling every few seconds, uploads, retries) goes
    to `cloud_message`; `phase_message` changes only when a phase of the reconstruction is finished, so the screen
    shows a steady progress (CLOUD-24). Both reach the screen only in PROCESSING: a pod started with the scan
    (streaming) logs its messages while the photos are taken and never touches the scan's own status.
    """
    def _progress(message: str) -> None:
        log.info("[Cloud %s] %s", job_id, message)
        jobs_manager.update_job(job_id, cloud_message=message)
        if scanner.state == ScannerState.PROCESSING and getattr(scanner, "job_id", None) == job_id:
            scanner.cloud_message = message

    def _phase(message: str) -> None:
        log.info("[Cloud %s] Phase: %s", job_id, message)
        jobs_manager.update_job(job_id, phase_message=message)
        if scanner.state == ScannerState.PROCESSING and getattr(scanner, "job_id", None) == job_id:
            scanner.phase_message = message

    return _progress, _phase


def _cloud_stage_body(cloud_job, options: dict, job_id: str) -> None:
    _progress, _phase = _cloud_callbacks(job_id)
    scanner.cloud_message = None
    _phase("Preparing the 3D reconstruction in the cloud")
    jobs_manager.update_job(job_id, status="processing", phase="processing")

    job_rec = jobs_manager.get_job(job_id) if hasattr(jobs_manager, "get_job") else None
    job_cancel = getattr(job_rec, "cancel_event", None)
    cancel_ev = job_cancel if isinstance(job_cancel, threading.Event) else None

    job_img_dir = Path("input_images") / job_id / "rig"
    retry_img_dir = Path(_cloud_retry.get("images_dir") or "") if _cloud_retry else None
    if job_img_dir.is_dir():
        img_dir = job_img_dir
    elif retry_img_dir is not None and retry_img_dir.is_dir():
        img_dir = retry_img_dir
    else:
        img_dir = Path(scanner.images_dir)

    report = cloud_job.run_cloud_job(
        input_dir=str(img_dir.parent),
        image_prefix=img_dir.name + "/",
        results_dir=str(results_directory()),
        job_id=job_id,
        pipeline_options=options,
        dry_run=False,
        skip_upload=True,
        cancel_event=cancel_ev,
        on_progress=_progress,
        on_phase=_phase,
        active_pod_ids=jobs_manager.get_active_pod_ids() if hasattr(jobs_manager, "get_active_pod_ids") else [],
        active_job_ids=jobs_manager.get_active_job_ids() if hasattr(jobs_manager, "get_active_job_ids") else [],
        on_pod_created=lambda pod_id: jobs_manager.update_job(job_id, pod_id=pod_id),
    )
    _finish_cloud_report(report, job_id)


def _finish_cloud_report(report: dict, job_id: str) -> None:
    """The end of a cloud stage (normal or streaming): result kept in results/<job_id>/, state COMPLETED or raise."""
    job_rec = jobs_manager.get_job(job_id) if hasattr(jobs_manager, "get_job") else None
    job_cancelled = job_rec is not None and getattr(job_rec, "status", None) == "cancelled"
    if getattr(scanner, "job_id", None) == job_id:
        if _cancel_requested() or scanner.state == ScannerState.CANCELLED or job_cancelled:
            scanner.state = ScannerState.CANCELLED
            log.info("Job %s cancelled during cloud processing.", job_id)
            return
    elif job_cancelled:
        log.info("Background job %s cancelled during cloud processing.", job_id)
        return

    if report.get("result") != "ok":
        status = report.get("status") or {}
        err = f"Turntable Studio job {job_id} failed: {status.get('state') or 'no pod result'} {status.get('error', '')}".strip()
        jobs_manager.update_job(job_id, status="failed", phase="error", error=err)
        raise RuntimeError(err)

    cost = report.get("cost_estimate")
    jobs_manager.update_job(job_id, status="completed", phase="done", result_id=job_id, cost_estimate=cost)
    if getattr(scanner, "job_id", None) == job_id:
        scanner.result_id = job_id
        if _set_state_unless_cancelled(ScannerState.COMPLETED):
            log.info("Full pipeline completed: model in %s (cost estimate %s).",
                     report.get("results_dir"), cost)
    dest_folder = results_directory() / job_id
    job_img_dir = Path("input_images") / job_id / "rig"
    src_photos = job_img_dir if job_img_dir.is_dir() else Path(scanner.images_dir)
    dest_folder.mkdir(parents=True, exist_ok=True)
    copy_photos(src_photos, dest_folder, move=True)
    set_category(results_directory(), job_id, "object")


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _stream_pod_requested(req: StartRequest) -> bool:
    """
    The pod starts with the scan (streaming) when RunPod is requested, the scan is not a flip scan (the pod would
    wait, billed, up to FLIP_TIMEOUT_S for the turn-over) and cloud.start_pod_with_scan says so (null: env
    SCAN_CLOUD_STREAM, defaults to on).
    """
    if getattr(req.cloud, "launch_runpod", False) is not True or getattr(req.mechanical, "flip", False) is True:
        return False
    wanted = getattr(req.cloud, "start_pod_with_scan", None)
    return _truthy_env("SCAN_CLOUD_STREAM", default="1") if wanted is None else bool(wanted)


def _stream_max_levels(req: StartRequest) -> int:
    """
    Most rings this scan can take (the pod's RAM is chosen before the ToF ends the scan): the Z travel above the
    start height divided by the climb per ring, + 1. SCAN_STREAM_MAX_LEVELS overrides it.
    """
    env = os.environ.get("SCAN_STREAM_MAX_LEVELS", "").strip()
    if env:
        return max(1, int(float(env)))
    try:
        from hardware.rig_limits import Z_TRAVEL_MAX_MM
        travel = float(Z_TRAVEL_MAX_MM)
    except Exception:
        travel = 475.0
    try:
        from hardware.scanner import Z_START_DEFAULT_MM
        start = float(Z_START_DEFAULT_MM)
    except Exception:
        start = 30.0
    climb = max(1.0, float(req.mechanical.z_axis.distance_mm))
    return max(1, min(12, int((travel - start) // climb) + 1))


def _cloud_options(req: StartRequest, job_id: str) -> dict:
    options = {
        "rig_mode": True,
        "resolution_level": req.cloud.resolution_level,
        "lift_mm": req.mechanical.z_axis.distance_mm,
        "step_deg": req.mechanical.turntable.angle,   # turntable step: the pod derives the camera poses from it
        "texturing_engine": "texrecon",
    }
    # Untitled: the object is named after its folder (the job id), the same name as in results/ and on a USB export
    options["title"] = req.cloud.title or job_id
    return options


class _StreamCloudStage:
    """
    The cloud stage started together with the scan (streaming pod, Turntable Studio v10+): run_cloud_job(stream=True)
    in a thread, from the start of the photos. The pod boots, downloads each photo as the upload worker puts it in
    R2 and pre-processes it; it starts the reconstruction when capture_done() writes capture_done.json (after the
    scan verified every photo). stop() (scan failed or cancelled) writes cancel.json and terminates the pod.
    """

    def __init__(self, cloud_job, options: dict, job_id: str, n_photos: int):
        self.cloud_job, self.options, self.job_id, self.n_photos = cloud_job, dict(options), job_id, int(n_photos)
        self.abort = threading.Event()
        self.started = threading.Event()
        self.report: Optional[dict] = None
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, name=f"cloud-stream-{job_id}", daemon=True)

    def start(self) -> "_StreamCloudStage":
        log.info("[Cloud %s] Starting the pod with the scan (up to %d photos).", self.job_id, self.n_photos)
        self.thread.start()
        self.started.wait(timeout=1.0)
        return self

    def _run(self) -> None:
        _progress, _phase = _cloud_callbacks(self.job_id)
        job_rec = jobs_manager.get_job(self.job_id) if hasattr(jobs_manager, "get_job") else None
        job_cancel = getattr(job_rec, "cancel_event", None)
        events = [self.abort]
        if isinstance(job_cancel, threading.Event):
            events.append(job_cancel)
        cancel_ev = self.cloud_job.AnyEvent(*events) if hasattr(self.cloud_job, "AnyEvent") else self.abort
        job_img_dir = Path("input_images") / self.job_id / "rig"
        img_dir = job_img_dir if job_img_dir.exists() else Path(scanner.images_dir)
        try:
            self.started.set()
            self.report = self.cloud_job.run_cloud_job(
                input_dir=str(img_dir.parent),
                image_prefix=img_dir.name + "/",
                results_dir=str(results_directory()),
                job_id=self.job_id,
                pipeline_options=self.options,
                dry_run=False,
                skip_upload=True,
                cancel_event=cancel_ev,
                on_progress=_progress,
                on_phase=_phase,
                stream=True,
                stream_n_photos=self.n_photos,
                active_pod_ids=jobs_manager.get_active_pod_ids() if hasattr(jobs_manager, "get_active_pod_ids") else [],
                active_job_ids=jobs_manager.get_active_job_ids() if hasattr(jobs_manager, "get_active_job_ids") else [],
                on_pod_created=lambda pod_id: jobs_manager.update_job(self.job_id, pod_id=pod_id),
            )
        except BaseException as e:          # reported by the orchestrator after the scan
            log.error("[Cloud %s] Streaming cloud stage failed: %s", self.job_id, e)
            self.error = e
        finally:
            self.started.set()

    def capture_done(self, keys, step_deg: float) -> None:
        if hasattr(self.cloud_job, "calls"):
            for _ in range(200):
                if self.cloud_job.calls or self.error is not None or not self.thread.is_alive():
                    break
                threading.Event().wait(0.01)
        self.cloud_job.write_capture_done(self.job_id, keys, step_deg)

    def stop(self, reason: str) -> None:
        """The scan will not deliver its photos: the pod stops (cancel.json + termination by run_pod)."""
        log.info("[Cloud %s] Stopping the pod started with the scan: %s", self.job_id, reason)
        self.abort.set()
        self.cloud_job.write_cancel(self.job_id)
        self.thread.join(timeout=120)

    def wait(self) -> None:
        self.thread.join()


def _orchestrate_pipeline(req: StartRequest):
    """
    Background worker that runs the mechanical scan and, only when the request asks for it (cloud.launch_runpod), the
    cloud pipeline. Updates the global scanner.state natively so the frontend polling loop is always accurate.
    Every state write goes through _set_state_unless_cancelled(): a CANCELLED state is never overwritten.
    """
    stream_stage: Optional[_StreamCloudStage] = None
    stream_lock = threading.Lock()
    job_id = req.job_id
    try:
        try:
            cloud_job = _load_cloud_job() if getattr(req.cloud, "launch_runpod", False) is True else None
        except Exception:
            cloud_job = None
        job_id = job_id or (cloud_job and getattr(cloud_job, "r2", None) and hasattr(cloud_job.r2, "new_job_id") and cloud_job.r2.new_job_id()) or _new_job_id()

        def _start_stream_pod(trigger_reason: str = "potential_last_level"):
            nonlocal stream_stage
            if not _stream_pod_requested(req):
                return
            with stream_lock:
                if stream_stage is not None:
                    return
                cloud_count = jobs_manager.count_active_cloud_jobs(exclude_job_id=job_id) if hasattr(jobs_manager, "count_active_cloud_jobs") else 0
                if isinstance(cloud_count, int) and isinstance(MAX_PARALLEL_CLOUD_JOBS, int) and cloud_count >= MAX_PARALLEL_CLOUD_JOBS:
                    log.warning("Active cloud jobs limit reached (%d): streaming pod postponed until after scan.", MAX_PARALLEL_CLOUD_JOBS)
                    return
                try:
                    cj = _load_cloud_job()
                    n_photos = cj.stream_n_images(req.mechanical.turntable.angle, _stream_max_levels(req))
                    log.info("[Cloud %s] Starting streaming pod on %s (%d photos anticipated).", job_id, trigger_reason, n_photos)
                    stream_stage = _StreamCloudStage(cj, _cloud_options(req, job_id), job_id, n_photos).start()
                    if hasattr(jobs_manager, "update_job"):
                        jobs_manager.update_job(job_id, meta={"streaming": True})
                except Exception as e:
                    log.warning("Could not start streaming pod on %s (%s): it will run after the scan.", trigger_reason, e)
                    stream_stage = None

        # 0. Streaming: if eager start requested (SCAN_POD_EARLY=1), launch before photos; otherwise pod launch is deferred
        # until ToF checks shot 0 of a level and does not detect the object (potential last level).
        if _stream_pod_requested(req) and _truthy_env("SCAN_POD_EARLY", default="0"):
            _start_stream_pod("scan_start_early")

        jobs_manager.register_job(
            job_id=job_id,
            kind="object",
            status="running",
            phase="scanning",
            meta={
                "title": getattr(req.cloud, "title", None),
                "launch_runpod": getattr(req.cloud, "launch_runpod", True),
                "streaming": stream_stage is not None,
            },
        )

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
                job_id=job_id,
                on_potential_last_level=lambda lvl: _start_stream_pod(f"potential_last_level_{lvl}"),
                # [SCAN-FLIP disabled temporarily]
                # flip=getattr(req.mechanical, "flip", False) is True,
            )
        except RuntimeError as e:
            # scan() refused to start (the scanner is no longer PREPARED, e.g. cancelled in between).
            # That is not a failure of the running state: do not overwrite it.
            log.warning("Scan not started: %s", e)
            jobs_manager.update_job(job_id, status="failed", phase="error", error=str(e))
            if stream_stage is not None:
                stream_stage.stop("scan not started")
            return
        except Exception as e:
            if stream_stage is not None:
                stream_stage.stop(f"scan failed: {e}")
                stream_stage = None
            if not _is_upload_error(e):
                jobs_manager.update_job(job_id, status="failed", phase="error", error=str(e))
                raise
            # The physical scan finished but photos are missing from the bucket. The scanner already carries
            # `missing` and `error` (shown by /status); the files stay on disk.
            missing = list(getattr(e, "missing", None) or getattr(scanner, "missing", None) or [])
            log.error(
                "Upload verification failed: %d photo(s) missing from the bucket (first %d: %s): %s",
                len(missing), _MISSING_KEYS_LOGGED, missing[:_MISSING_KEYS_LOGGED], e,
            )
            jobs_manager.update_job(job_id, status="failed", phase="error", error=str(e))
            _set_state_unless_cancelled(ScannerState.ERROR)
            return

        # Check if it was cancelled during the scan (the cancel flag is set before emergency_stop() has time to
        # publish the CANCELLED state)
        if scanner.state == ScannerState.CANCELLED or _cancel_requested():
            scanner.state = ScannerState.CANCELLED
            jobs_manager.update_job(job_id, status="cancelled", phase="cancelled")
            log.info("Scan was cancelled, aborting cloud pipeline.")
            if stream_stage is not None:
                stream_stage.stop("scan cancelled")
            return

        # Check if the upload worker failed or stalled
        if scanner.state == ScannerState.ERROR:
            log.error("Scan ended in error state (e.g. stalled uploads). Aborting cloud pipeline.")
            jobs_manager.update_job(job_id, status="failed", phase="error", error=getattr(scanner, "error", "upload error"))
            if stream_stage is not None:
                stream_stage.stop("scan ended in error")
            return

        # 2. RunPod is optional: without it the scan is done once the photos are in the bucket
        if getattr(req.cloud, "launch_runpod", False) is not True:
            job_img_dir = Path("input_images") / job_id / "rig"
            src_photos = job_img_dir if job_img_dir.is_dir() else Path(scanner.images_dir)
            dest_folder = results_directory() / job_id
            dest_folder.mkdir(parents=True, exist_ok=True)
            copy_photos(src_photos, dest_folder, move=True)
            set_category(results_directory(), job_id, "object")
            jobs_manager.update_job(job_id, status="completed", phase="done")
            if _set_state_unless_cancelled(ScannerState.COMPLETED):
                log.info("Scan completed: photos verified in the bucket (RunPod not requested).")
            return

        # 3. Cloud processing: Turntable Studio on one RunPod pod (cloud.job.run_cloud_job).
        # Decouple the physical rig: physical capture and upload verification are done.
        # Clearing pending flag allows a new prepare / scan to begin while cloud reconstructs in background.
        with _start_lock:
            _start_state["pending"] = False
        jobs_manager.update_job(job_id, status="processing", phase="processing")

        cloud_job = _load_cloud_job()
        if stream_stage is not None:
            # The pod is already running: tell it the photos are complete (exactly those the scan verified)
            try:
                stream_stage.capture_done(list(getattr(scanner, "uploaded_keys", None) or []),
                                          req.mechanical.turntable.angle)
            except Exception as e:
                log.warning("Could not write capture_done.json (%s): the pod is started again after the scan.", e)
                stream_stage.stop("capture_done.json not written")
                stream_stage = None
        if not _set_state_unless_cancelled(ScannerState.PROCESSING):
            log.info("Scan was cancelled, aborting cloud pipeline.")
            if stream_stage is not None:
                stream_stage.stop("cancelled before processing")
            return

        options = _cloud_options(req, scanner.job_id)
        if stream_stage is not None:
            job_img_dir = Path("input_images") / job_id / "rig"
            img_dir = job_img_dir if job_img_dir.is_dir() else Path(scanner.images_dir)
            _cloud_retry.clear()
            _cloud_retry.update(job_id=stream_stage.job_id, options=dict(options),
                                images_dir=str(img_dir))
            scanner.cloud_message = None
            scanner.phase_message = "3D reconstruction started in the cloud (server already running)"
            stream_stage.wait()
            stage, stream_stage = stream_stage, None
            if stage.error is not None and not (_cancel_requested() or scanner.state == ScannerState.CANCELLED):
                # The pod started with the scan failed before giving a result (e.g. RunPod refused it): the photos
                # are all in R2, so the usual cloud stage runs now
                log.warning("Streaming cloud stage failed (%s): running the cloud stage after the scan.", stage.error)
                _run_cloud_stage(cloud_job, options)
            else:
                _finish_cloud_report(stage.report or {}, stage.job_id)
                if scanner.state == ScannerState.COMPLETED:
                    _cloud_retry.clear()
        else:
            if hasattr(jobs_manager, "count_active_cloud_jobs"):
                while True:
                    cloud_count = jobs_manager.count_active_cloud_jobs(exclude_job_id=job_id)
                    if not (isinstance(cloud_count, int) and isinstance(MAX_PARALLEL_CLOUD_JOBS, int) and cloud_count >= MAX_PARALLEL_CLOUD_JOBS):
                        break
                    if _cancel_requested() or scanner.state == ScannerState.CANCELLED:
                        return
                    log.info("[Job %s] Waiting in queue for an active cloud slot (limit %d)...", job_id, MAX_PARALLEL_CLOUD_JOBS)
                    jobs_manager.update_job(job_id, status="queued", phase="queued")
                    time.sleep(1.0)
            _run_cloud_stage(cloud_job, options)

    except Exception as e:
        log.error("Pipeline failed: %s", e)
        if stream_stage is not None:
            stream_stage.stop(f"pipeline failed: {e}")
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

# [SCAN-FLIP disabled temporarily]
# def _flip_decision(proceed: bool) -> FlipResponse:
#     decide = getattr(scanner, "flip_decision", None)
#     if not callable(decide):
#         raise HTTPException(status_code=400, detail="This scanner does not support flip scans.")
#     try:
#         decide(proceed)
#     except RuntimeError as e:   # ScannerModeError: not waiting for a flip
#         raise HTTPException(status_code=400, detail=str(e))
#     return FlipResponse(
#         state=scanner.state,
#         message="Scanning the second side." if proceed else "Finishing with the first side only.",
#     )
#
#
# @router.post("/flip/continue", response_model=FlipResponse)
# def flip_continue():
#     """
#     SCAN-FLIP: the object was turned over; the scanner scans the second side on the way down (state `running`,
#     `session` 1). Only in state `awaiting_flip` (a scan started with `mechanical.flip: true`); `400` otherwise.
#     """
#     return _flip_decision(True)
#
#
# @router.post("/flip/skip", response_model=FlipResponse)
# def flip_skip():
#     """
#     SCAN-FLIP: no second side; the scan finishes with the first side (upload check, then the cloud as usual). Only in
#     state `awaiting_flip`; `400` otherwise. `POST /scanner/cancel` still cancels the whole scan.
#     """
#     return _flip_decision(False)


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

def _face_scan_worker(params: dict, face_job_id: str):
    """
    Background part of POST /face/capture. face_scan() publishes the outcome itself (COMPLETED with `face_result`,
    ERROR with `error`, or CANCELLED); here a failure is only logged.
    """
    def _release_pending():
        with _start_lock:
            _start_state["pending"] = False

    try:
        jobs_manager.register_job(
            job_id=face_job_id,
            kind="face",
            status="running",
            phase="scanning",
            meta=dict(params),
        )
        scanner.face_scan(
            **params,
            job_id=face_job_id,
            begun=True,
            finish_state=ScannerState.COMPLETED,
            on_capture_done=_release_pending,
        )
        res_id = getattr(scanner, "result_id", None) or face_job_id
        job_rec = jobs_manager.get_job(face_job_id) if hasattr(jobs_manager, "get_job") else None
        if job_rec and getattr(job_rec, "status", None) == "cancelled":
            final_status = "cancelled"
        else:
            final_status = (
                "completed"
                if scanner.state == ScannerState.COMPLETED
                else ("error" if scanner.state == ScannerState.ERROR else "cancelled")
            )
        jobs_manager.update_job(
            face_job_id,
            status=final_status,
            phase=getattr(scanner, "phase", "done"),
            result_id=res_id,
        )
    except Exception as e:
        log.error("Face scan failed: %s", e)
        jobs_manager.update_job(face_job_id, status="failed", phase="error", error=str(e))
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
    face_job_id = getattr(scanner, "job_id", None) or _new_job_id()
    scanner.job_id = face_job_id
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

    background_tasks.add_task(_face_scan_worker, params, face_job_id)
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

    def _seconds(name):
        value = getattr(scanner, name, None)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    mode = getattr(scanner, "mode", None)
    return StatusResponse(
        state=scanner.state,
        mode=mode if isinstance(mode, str) else None,
        total_photos=_count("total_photos"),
        stream_urls=getattr(scanner, "stream_urls", None),
        phase=_text("phase"),
        phase_message=_text("phase_message"),
        level=_count("level"),
        session=_count("session"),
        flip_seconds_left=_seconds("flip_seconds_left"),
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
