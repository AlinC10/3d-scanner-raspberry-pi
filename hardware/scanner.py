import heapq
import itertools
import json
import logging
import random
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
import threading
import time
from queue import Queue
import queue

from hardware.camera.config import DEFAULT_PHOTO_RESOLUTION, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_QUALITY, \
    DEFAULT_VIDEO_RESOLUTION
import os
from .base_dual_camera import BaseDualCamera
from .dual_camera import DualCamera
from .dual_camera_xvs import DualCameraXVS
from .dual_camera_rpi_sync import DualCameraRPISync
from .endstop import Endstop, TopEndstopTriggered, BottomEndstopTriggered
from .motor import Motor, TURNTABLE_END_DELAY_DEFAULT, TURNTABLE_STEP_TYPE, Z_STEP_TYPE, angle_to_steps
from .lights import DimmableLight
from .tof import ToFSensor
import cloud.cloudflare_r2 as r2
from system import file_control as fc
import statistics

from .base_dual_camera import CaptureError

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_backoff(name: str, default: tuple) -> tuple:
    try:
        values = tuple(float(v) for v in os.environ[name].split(",") if v.strip())
        return values or default
    except (KeyError, ValueError):
        return default

# A photo name carries the shot number in 3 digits, so a ring holds at most 1000 photos per camera (more would make
# names of different rings collide, e.g. ring 1 shot 1000 and ring 11 shot 0).
MAX_SHOTS_PER_RING = 1000


def photo_file_stem(level: Union[int, str], shot: int) -> str:
    """
    Name (without extension) of the photo of ring `level`, shot `shot`, the format Meshroom accepts:
    "f_<level><shot on 3 digits>", e.g. f_1000 (ring 1, shot 0) and f_2019 (ring 2, shot 19).
    """
    return f"f_{level}{int(shot):03d}"


# Root of the photo tree: input_images/rig/0/f_1000.jpg, ... and manifest.json: the same "rig/<camera>/<name>"
# layout Meshroom expects (and the one the R2 download code writes). The cloud key is "rig/" + the path relative to
# it (see Scanner._cloud_key). Only this folder is wiped at the start of a scan, never input_images/ itself.
INPUT_IMAGES_DIR = str(Path(__file__).resolve().parent.parent / "input_images" / "rig")
CLOUD_KEY_PREFIX = "rig/"
MANIFEST_NAME = "manifest.json"
# The manifest is uploaded under this key AFTER the verification (never inside the photo set), best effort.
CLOUD_MANIFEST_KEY = "rig/_manifest.json"

# A photo whose mean luma (0-255) is below this is black: a closed lens cap, dead lights or a manual exposure of 0.
# The first photo pair of a scan is checked so a whole scan of black photos is never taken (SCAN_BLACK_FRAME_CHECK=0
# disables the check).
BLACK_FRAME_MEAN_MAX = _env_float("SCAN_BLACK_FRAME_MEAN_MAX", 3.0)

# Upload policy (module-level so tests can shrink it; each one can be overridden by an environment variable).
# Pause before attempt n+1 after attempt n failed (the last value repeats), +-UPLOAD_BACKOFF_JITTER of random spread.
UPLOAD_BACKOFF_S = _env_backoff("SCAN_UPLOAD_BACKOFF_S", (2.0, 5.0, 10.0, 20.0, 30.0))
UPLOAD_BACKOFF_JITTER = _env_float("SCAN_UPLOAD_BACKOFF_JITTER", 0.2)
# After this many failed attempts a file is PARKED: it stops occupying the queue and is retried every
# UPLOAD_PARK_RETRY_S seconds instead.
UPLOAD_ATTEMPTS_PER_FILE = int(_env_float("SCAN_UPLOAD_ATTEMPTS_PER_FILE", 8))
UPLOAD_PARK_RETRY_S = _env_float("SCAN_UPLOAD_PARK_RETRY_S", 60.0)
# Once the physical scan has ended, uploads may take at most this long without a single file succeeding.
UPLOAD_DRAIN_TIMEOUT_S = _env_float("SCAN_UPLOAD_DRAIN_TIMEOUT_S", 1800.0)
UPLOAD_DRAIN_POLL_S = 0.1
# Preflight refuses a scan when the photo directory has less free space than this.
MIN_FREE_DISK_MB = _env_float("SCAN_MIN_FREE_DISK_MB", 2048.0)
# The attribute exposed in /status keeps only the first entries of the missing list; the manifest keeps all of them.
MISSING_STATUS_CAP = 50

TOF_BACKGROUND_DISTANCE_MM = 320
# An object is detected only when the reading is closer than the backdrop by more than this
# margin, so ToF noise on the empty backdrop does not count as an object. Calibrate on the rig.
TOF_CLEARANCE_MARGIN_MM = 25
# Minimum pause after each turntable move before the next photo, so the object stops vibrating.
TURNTABLE_SETTLE_S = 0.2
# How often the Z-axis watchdog samples the limit switch in the direction of travel while the carriage moves.
ENDSTOP_POLL_S = 0.005
# Mechanical travel of the Z axis (specification, to be confirmed by measuring the rig) and the extra distance
# homing / move-to-top may travel past it before giving up. Their sum is the step budget of those two moves.
Z_TRAVEL_MAX_MM = 475
Z_HOMING_MARGIN_MM = 15
# Z moves of the preparation (homing, +30 mm climb, move to top): constant speed, no ramp, at the same step delay as
# the manual z-move and the scan climb (the profile that runs smoothly on the rig).
Z_PREPARE_DELAY_S = 0.0010

class ScannerError(Exception):
    pass

class ScannerBusyError(ScannerError):
    """Another motion (Z move, homing, move to top, turntable rotation, a /test move) is already in progress."""

    # Duck-typed by the routers (like ScannerStoppingError.conflict): a temporary refusal, HTTP 409
    conflict = True


class ScannerPreflightError(RuntimeError):
    """
    A check before /prepare failed (cloud storage unusable, ToF sensor missing, disk almost full). It is a
    RuntimeError, so the router answers 400 with the message, and the scanner state is left untouched (IDLE).
    """


class ScannerUploadError(ScannerError):
    """The scan ended but some photos are not in the bucket. `missing` is the sorted list of their cloud keys."""

    def __init__(self, message: str, missing: Optional[list] = None):
        super().__init__(message)
        self.missing = list(missing or [])


class ScannerStoppingError(RuntimeError):
    """
    A new run was refused because an emergency stop is still tearing the hardware down. It is a RuntimeError so the
    existing "invalid state" handling applies; `conflict` lets the router answer 409 instead of 400.
    """
    conflict = True

class ScannerState:
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"

    # States from which a new prepare_scan() cycle is allowed
    PREPARE_ALLOWED = (IDLE, COMPLETED, CANCELLED, ERROR)

    # Terminal states that cleanup() must not overwrite back to IDLE
    TERMINAL = (CANCELLED, ERROR, COMPLETED)


class ScanPhase:
    """Finer progress marker exposed in the status data (Scanner.phase), independent of ScannerState."""
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    SCANNING = "scanning"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"

T8_THREADED_ROD_STEP = 8 # mm


class Scanner:
    def __init__(self, xvs: Optional[bool] = None, camera_mode: Optional[str] = None):
        self.tof = ToFSensor()
        self.turntable_motor = Motor(motor_id=0, dir_pin=7, step_pin=6, en_pin=8, step_type=TURNTABLE_STEP_TYPE)
        self.z_axis_motor = Motor(motor_id=1, dir_pin=4, step_pin=5, en_pin=3, step_type=Z_STEP_TYPE)

        self.up_endstop = Endstop(pin=22, pull_up=True, bounce_time=0.02)
        self.down_endstop = Endstop(pin=4, pull_up=True, bounce_time=0.02)

        # Wire up safety interrupts: Any endstop hit immediately aborts in-flight Z-axis steps
        self.up_endstop.when_pressed = self._on_top_endstop_hit
        self.down_endstop.when_pressed = self._on_bottom_endstop_hit

        self.lights = DimmableLight(relay_pin=11, pwm_channel=0, frequency=10000)

        self.dual_cameras: Optional[BaseDualCamera] = None

        if camera_mode is not None:
            self.camera_mode = camera_mode.lower()
        elif xvs is True:
            self.camera_mode = "xvs"
        elif xvs is False:
            self.camera_mode = os.environ.get("CAMERA_MODE", "rpi_sync").lower()
        else:
            self.camera_mode = os.environ.get("CAMERA_MODE", "rpi_sync").lower()

        self.xvs = (self.camera_mode == "xvs")

        self.state = ScannerState.IDLE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        # Set only while a Z move is in flight. The endstop callbacks send a *universal* STOP byte, so they must
        # not fire it while the Z axis is idle (it would abort the turntable or cut every motor).
        self._z_moving = threading.Event()
        # The universal STOP must be sent at most once per Z move: a second one would reach an idle Arduino and
        # cut the other motor as well.
        self._z_stop_sent = False
        self._z_stop_lock = threading.Lock()
        # Direction of the Z move in flight ("up" / "down"; None between moves): an endstop edge only stops a move
        # that travels towards that switch. A late or bouncing edge of the switch the carriage is leaving must not
        # abort the next move (it did: the +30 mm climb right after homing).
        self._z_direction: Optional[str] = None

        # Motion guard (see motion_guard()): owner thread + nesting depth, both protected by the mutex.
        self._motion_mutex = threading.Lock()
        self._motion_owner: Optional[int] = None
        self._motion_depth = 0
        # Marks threads that run the prepare/scan sequences: only those honour a pending cancel inside the Z helpers
        # (see _cancel_applies()).
        self._tls = threading.local()
        # True while emergency_stop() is tearing the hardware down; a new /prepare is refused and cleanup() leaves the
        # state alone meanwhile. The depth counter keeps overlapping emergency stops from clearing it too early.
        self._stopping = False
        self._stopping_depth = 0

        self._level = 0
        self.total_photos = 0
        self.stream_urls = None

        # Root of the photo tree and of manifest.json (an instance attribute so tests can point it elsewhere)
        self.images_dir = INPUT_IMAGES_DIR

        # used for uploading the images when they are captured
        self.upload_queue = Queue()
        self._upload_thread: Optional[threading.Thread] = None
        self._worker_stop_event = threading.Event()
        # Upload bookkeeping, all protected by _upload_lock. `_pending` holds the files that still have to be
        # resolved (queued, waiting for a retry, or parked); `_upload_failed` the files that failed permanently or
        # are parked right now. `_upload_gen` lets a worker that outlived its scan recognise it is stale.
        self._upload_lock = threading.Lock()
        self._upload_gen = 0
        self._pending: set[str] = set()
        self._uploaded: set[str] = set()
        self._upload_failed: set[str] = set()
        self._upload_keys: dict[str, str] = {}
        self._last_upload_time = time.monotonic()

        # Manifest of the running scan (see _register_photos), protected by _manifest_lock
        self._manifest_lock = threading.Lock()
        self._manifest: Optional[dict] = None
        self._manifest_dir: Optional[str] = None

        # Status data (read by the router with getattr). `photos_captured` is derived from total_photos.
        self.phase = ScanPhase.IDLE
        self.phase_message: Optional[str] = None
        self.photos_uploaded = 0
        self.missing: list[str] = []
        self.error: Optional[str] = None
        self.tof_warnings: list[str] = []
        self.uploaded_keys: list[str] = []
        self.upload_verified: Optional[bool] = None
        self._phase_note: Optional[str] = None   # sticky message (e.g. first-ring ToF remark) kept across phases
        # Whether prepare_scan() of the selected camera driver accepts lock_auto (set by setup_and_stream)
        self._lock_auto_supported = False


    @property
    def is_streaming(self):
        if self.dual_cameras:
            return self.dual_cameras.is_streaming
        return False

    @property
    def level(self):
        return self._level

    @property
    def photos_captured(self) -> int:
        return self.total_photos

    @property
    def upload_failed(self) -> bool:
        """True while at least one photo has failed permanently or is parked (boolean view of `_upload_failed`)."""
        with self._upload_lock:
            return bool(self._upload_failed)

    def _set_phase(self, phase: str, message: Optional[str] = None, error: Optional[str] = None) -> None:
        """Updates the status data in one place. A message of None falls back to the sticky note, if any."""
        self.phase = phase
        self.phase_message = message if message is not None else self._phase_note
        if error is not None:
            self.error = error

    def _on_top_endstop_hit(self) -> None:
        """Invoked when the physical upper ceiling limit switch is pressed."""
        if not self._z_moving.is_set():
            log.warning("[Scanner] TOP limit switch pressed while the Z-axis is not moving; ignored.")
            return
        if self._z_direction == "down":
            log.info("[Scanner] TOP limit switch edge ignored: the carriage is moving down, away from it.")
            return
        log.warning("[Scanner] TOP limit switch triggered mid-motion!")
        self._stop_z_once()

    def _on_bottom_endstop_hit(self) -> None:
        """Invoked when the physical bottom home limit switch is pressed."""
        if not self._z_moving.is_set():
            log.warning("[Scanner] BOTTOM limit switch pressed while the Z-axis is not moving; ignored.")
            return
        if self._z_direction == "up":
            log.info("[Scanner] BOTTOM limit switch edge ignored: the carriage is moving up, away from it.")
            return
        log.info("[Scanner] BOTTOM limit switch triggered.")
        self._stop_z_once()

    @contextmanager
    def motion_guard(self):
        """
        Non-blocking exclusive right to move the rig (Z move, homing, move to top, turntable rotation, /test moves).

        Two overlapping Z moves would share the `_z_moving` flag and the single stop budget, and the first one to end
        would clear the endstop protection of the other. If another thread holds the guard this raises
        ScannerBusyError immediately instead of waiting. It is re-entrant for the owning thread, so the helpers nest
        freely inside one scan (scan -> move_z_up, home_z_axis -> move_z_down, ...).
        """
        me = threading.get_ident()
        with self._motion_mutex:
            if self._motion_depth > 0 and self._motion_owner != me:
                raise ScannerBusyError("Another motion is already in progress.")
            self._motion_owner = me
            self._motion_depth += 1
        try:
            yield
        finally:
            with self._motion_mutex:
                self._motion_depth -= 1
                if self._motion_depth == 0:
                    self._motion_owner = None

    def _cancel_applies(self) -> bool:
        """
        True when a set cancel flag must stop a move that is about to start.

        `_cancel_event` stays set after an emergency_stop() until the next prepare/scan clears it, and the /test
        endpoints use the same Z helpers: a stale flag must not block a manual move. The flag is therefore honoured
        only for the prepare/scan sequences (they mark their thread), and for any caller while a prepare/scan owns the
        rig (state PREPARING / PREPARED / RUNNING).
        """
        if not self._cancel_event.is_set():
            return False
        if getattr(self._tls, "automated", False):
            return True
        return self.state in (ScannerState.PREPARING, ScannerState.PREPARED, ScannerState.RUNNING)

    def _begin_z_motion(self) -> bool:
        """
        Marks a Z move as in flight (the endstop callbacks only act while this is set).

        Returns False, without marking anything, when a cancel applies: the check shares `_z_stop_lock` with
        emergency_stop() setting the flag, so a cancel is either seen here or the stop that follows it finds the
        move already flagged.
        """
        with self._z_stop_lock:
            if self._cancel_applies():
                return False
            self._z_stop_sent = False
            self._z_moving.set()
        return True

    def _end_z_motion(self, watcher) -> None:
        # Clear the flag first (under the lock the callbacks check it with): a callback arriving while the watcher is
        # joined must not send the universal STOP to an Arduino that is already idle.
        with self._z_stop_lock:
            self._z_moving.clear()
            self._z_direction = None
        if watcher is not None:
            stop_event, thread = watcher
            stop_event.set()
            thread.join(timeout=0.5)

    def _stop_z_once(self) -> None:
        """
        Sends the (universal) STOP at most once per Z move, however many sources notice the endstop, and never after
        the move has ended: the check and the send share a lock with `_end_z_motion`, so a late callback cannot
        reach an idle Arduino (which would cut the torque of every motor).
        """
        with self._z_stop_lock:
            if self._z_stop_sent or not self._z_moving.is_set():
                return
            self._z_stop_sent = True
            self.z_axis_motor.stop()

    def _start_endstop_watch(self, endstop, label: str):
        """
        Level-based watchdog for the limit switch in the direction of travel. The gpiozero callback reacts to the
        press *edge* only; this also catches a switch that became active between the pre-check and the first step,
        or an edge that was missed. Returns (stop_event, thread) for `_end_z_motion`.
        """
        stop_event = threading.Event()

        def watch():
            while not stop_event.wait(ENDSTOP_POLL_S):
                if endstop.is_active:
                    log.warning("[Scanner] %s limit switch active during Z travel (watchdog); stopping.", label)
                    self._stop_z_once()
                    return

        thread = threading.Thread(target=watch, name=f"z-endstop-watch-{label.lower()}", daemon=True)
        thread.start()
        return stop_event, thread

    # ── Upload pipeline ────────────────────────────────────────────────────────────────────────────────────────

    def _cloud_key(self, path: Union[str, Path], base_dir: Optional[Union[str, Path]] = None) -> str:
        """
        The R2 object key of a photo: "rig/" + its path relative to the photo root (e.g. "rig/0/f_1000.jpg"),
        which keeps the camera sub-directories the downstream download code (rig_mode) expects.

        :raises ValueError: if the path is not below the photo root.
        """
        rel = Path(path).relative_to(base_dir if base_dir is not None else self.images_dir)
        return f"{CLOUD_KEY_PREFIX}{rel.as_posix()}"

    def _enqueue_upload(self, path: str, key: str) -> None:
        with self._upload_lock:
            self._upload_keys[path] = key
            self._pending.add(path)
        self.upload_queue.put(path)

    def _backoff_delay(self, failed_attempts: int) -> float:
        schedule = UPLOAD_BACKOFF_S or (30.0,)
        base = schedule[min(failed_attempts - 1, len(schedule) - 1)]
        jitter = max(0.0, UPLOAD_BACKOFF_JITTER)
        return max(0.0, base * (1.0 + random.uniform(-jitter, jitter)))

    def _try_upload(self, path: str) -> str:
        """
        One upload attempt. Returns "ok", "retry" (transient failure) or "permanent" (the file cannot ever be
        uploaded: missing, empty, or outside the photo tree).
        """
        try:
            size = os.path.getsize(path)
        except OSError as e:
            log.error("[UploadWorker] %s is missing on disk (%s); it will never be uploaded.", path, e)
            return "permanent"
        if size <= 0:
            log.error("[UploadWorker] %s is empty (0 bytes); it will never be uploaded.", path)
            return "permanent"

        key = self._upload_keys.get(path)
        if key is None:
            try:
                key = self._cloud_key(path)
            except ValueError:
                log.error("[UploadWorker] %s is outside %s; it will never be uploaded.", path, self.images_dir)
                return "permanent"

        log.info("[UploadWorker] Uploading %s to R2 as %s ...", path, key)
        try:
            return "ok" if r2.upload_file(path, object_name=key) else "retry"
        except Exception as e:  # upload_file is documented not to raise; a driver bug must not kill the worker
            log.warning("[UploadWorker] Upload of %s raised: %s", path, e)
            return "retry"

    def _upload_worker(self, stop_event: threading.Event, gen: int = 0):
        """
        Background thread that uploads photos. A file that fails is retried after a backoff WITHOUT blocking the
        files behind it; after UPLOAD_ATTEMPTS_PER_FILE failed attempts it is parked and retried every
        UPLOAD_PARK_RETRY_S. Missing / empty files fail immediately and permanently.
        """
        retry: list = []                 # heap of (due, seq, path): files waiting for their next attempt
        parked: dict[str, float] = {}    # path -> next due time
        attempts: dict[str, int] = {}
        seq = itertools.count()

        def stopped() -> bool:
            return stop_event.is_set() or self._cancel_event.is_set()

        while not stopped():
            now = time.monotonic()
            path = None
            from_park = False
            if retry and retry[0][0] <= now:
                path = heapq.heappop(retry)[2]
            else:
                dues = [retry[0][0]] if retry else []
                dues += list(parked.values())
                timeout = 0.2 if not dues else min(0.2, max(0.01, min(dues) - now))
                try:
                    item = self.upload_queue.get(timeout=timeout)
                    self.upload_queue.task_done()
                except queue.Empty:
                    item = None
                if item is not None:
                    path = item
                else:
                    now = time.monotonic()
                    for candidate, due in parked.items():
                        if due <= now:
                            path, from_park = candidate, True
                            del parked[candidate]
                            break
            if path is None or stopped():
                continue

            try:
                outcome = self._try_upload(path)
            except Exception as e:
                log.error("[UploadWorker] Unexpected error processing %s: %s", path, e)
                outcome = "retry"

            with self._upload_lock:
                current = (gen == self._upload_gen)
                if outcome == "ok":
                    if current:
                        self._pending.discard(path)
                        self._upload_failed.discard(path)
                        self._uploaded.add(path)
                        self.photos_uploaded = len(self._uploaded)
                        self._last_upload_time = time.monotonic()
                    attempts.pop(path, None)
                    continue
                if outcome == "permanent":
                    if current:
                        self._pending.discard(path)
                        self._upload_failed.add(path)
                    attempts.pop(path, None)
                    continue

            # Transient failure
            attempts[path] = attempts.get(path, 0) + 1
            if from_park or attempts[path] >= UPLOAD_ATTEMPTS_PER_FILE:
                if not from_park:
                    log.error("[UploadWorker] %s failed %d times; parked, retried every %.0fs.",
                              path, attempts[path], UPLOAD_PARK_RETRY_S)
                    with self._upload_lock:
                        if gen == self._upload_gen:
                            self._upload_failed.add(path)
                parked[path] = time.monotonic() + UPLOAD_PARK_RETRY_S
            else:
                delay = self._backoff_delay(attempts[path])
                log.warning("[UploadWorker] Attempt %d/%d of %s failed; retrying in %.1fs.",
                            attempts[path], UPLOAD_ATTEMPTS_PER_FILE, path, delay)
                heapq.heappush(retry, (time.monotonic() + delay, next(seq), path))

    def start_upload_worker(self):
        """Starts a fresh worker thread and resets the upload bookkeeping of the previous scan."""
        # A worker of the previous scan may still be winding down: it must not touch the new bookkeeping
        if self._upload_thread is not None:
            self._worker_stop_event.set()
        while not self.upload_queue.empty():
            try:
                self.upload_queue.get_nowait()
                self.upload_queue.task_done()
            except queue.Empty:
                break

        with self._upload_lock:
            self._upload_gen += 1
            gen = self._upload_gen
            self._pending = set()
            self._uploaded = set()
            self._upload_failed = set()
            self._upload_keys = {}
            self._last_upload_time = time.monotonic()
            self.photos_uploaded = 0

        self._worker_stop_event = threading.Event()
        self._upload_thread = threading.Thread(
            target=self._upload_worker, args=(self._worker_stop_event, gen), daemon=True, name="upload-worker"
        )
        self._upload_thread.start()

    def _drain_uploads(self, timeout_s: Optional[float] = None) -> str:
        """
        Waits until every queued photo is resolved (uploaded, or failed permanently).

        The deadline (UPLOAD_DRAIN_TIMEOUT_S) counts from the start of the drain or from the last successful
        upload, whichever is later, so a slow but progressing link is never cut off. A cancel aborts it promptly.

        :return: "done", "timeout", "cancelled" or "worker_dead".
        """
        limit = UPLOAD_DRAIN_TIMEOUT_S if timeout_s is None else timeout_s
        drain_start = time.monotonic()
        while True:
            if self._cancel_event.is_set():
                return "cancelled"
            with self._upload_lock:
                pending = len(self._pending)
                reference = max(self._last_upload_time, drain_start)
            if pending == 0:
                return "done"
            thread = self._upload_thread
            if thread is None or not thread.is_alive():
                return "worker_dead"
            if time.monotonic() - reference > limit:
                return "timeout"
            self._cancel_event.wait(UPLOAD_DRAIN_POLL_S)

    def stop_upload_worker(self, wait_for_completion: bool = True, stall_timeout: Optional[float] = None) -> str:
        """
        Stops the worker, optionally waiting for the remaining uploads first (see _drain_uploads).

        :param stall_timeout: overrides UPLOAD_DRAIN_TIMEOUT_S for this wait.
        :return: the drain outcome ("done" when nothing was waited for).
        """
        outcome = "done"
        thread = self._upload_thread
        if thread is not None and wait_for_completion and thread.is_alive():
            outcome = self._drain_uploads(stall_timeout)
        self._worker_stop_event.set()
        if thread is not None:
            self.upload_queue.put(None)  # wakes the worker at once
            thread.join(timeout=5.0)
        self._upload_thread = None
        return outcome

    # ── Manifest ───────────────────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _new_manifest() -> dict:
        return {
            "scan_id": f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expected_keys": [],
            "photos": [],
            "uploaded": [],
            "missing": [],
            "complete": False,
        }

    def _begin_manifest(self, images_dir: str) -> None:
        with self._manifest_lock:
            self._manifest_dir = str(images_dir)
            self._manifest = self._new_manifest()
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Atomically replaces manifest.json (temp file + os.replace): a reader never sees a half-written file."""
        with self._manifest_lock:
            if self._manifest is None or self._manifest_dir is None:
                return
            with self._upload_lock:
                self._manifest["uploaded"] = sorted(
                    self._upload_keys[p] for p in self._uploaded if p in self._upload_keys
                )
            target = Path(self._manifest_dir) / MANIFEST_NAME
            tmp = target.with_name(MANIFEST_NAME + ".tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._manifest, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except OSError as e:
                raise ScannerError(f"Could not write the scan manifest {target}: {e}")

    @staticmethod
    def _mean_luma(path: str) -> Optional[float]:
        """Mean luma (0-255) of a JPEG, read at 1/8 size; None when it cannot be judged (no OpenCV, unreadable)."""
        try:
            import cv2
            import numpy as np
            img = cv2.imread(str(path), cv2.IMREAD_REDUCED_GRAYSCALE_8)
            if not isinstance(img, np.ndarray) or img.size == 0:
                return None
            return float(img.mean())
        except Exception:
            return None

    def _require_not_black(self, paths, capture_info: Optional[dict]) -> None:
        cams = []
        if isinstance(capture_info, dict):
            cams = list(capture_info.get("cameras") or [])
        for index, path in enumerate(paths):
            mean = self._mean_luma(path)
            if mean is None or mean >= BLACK_FRAME_MEAN_MAX:
                continue
            cam = cams[index] if index < len(cams) and isinstance(cams[index], dict) else {}
            raise ScannerError(
                f"The photo of camera {index + 1} is black (mean brightness {mean:.1f}/255, camera reports "
                f"exposure_time={cam.get('exposure_time')} us, analogue_gain={cam.get('analogue_gain')}). Check the "
                "lens cap, that the lights are on, and that exposure_time / analogue_gain are not set to a "
                "too small manual value in /scanner/prepare."
            )

    def _register_photos(
        self,
        images_dir: str,
        photo_paths,
        level,
        shot: int,
        capture_info: Optional[dict] = None,
    ) -> None:
        """
        Validates a freshly captured set of photos (each file must exist and be non-empty), records it in the
        manifest and queues it for upload. Counts it in total_photos.

        :raises ScannerError: on an empty result or an invalid file; nothing is queued then.
        """
        paths = [str(p) for p in (photo_paths or [])]
        if not paths:
            raise ScannerError(f"The cameras returned no photo at level {level}, shot {shot}.")

        sizes = []
        for index, path in enumerate(paths):
            try:
                size = os.path.getsize(path)
            except OSError:
                raise ScannerError(f"Camera {index} did not produce {path} (level {level}, shot {shot}).")
            if size <= 0:
                raise ScannerError(f"Camera {index} produced an empty file {path} (level {level}, shot {shot}).")
            sizes.append(size)

        try:
            keys = [self._cloud_key(p, images_dir) for p in paths]
        except ValueError as e:
            raise ScannerError(f"A photo was saved outside the photo directory {images_dir}: {e}")

        # The first pair of a scan must not be black: stop right here instead of after 160 black photos
        if self.total_photos == 0 and os.environ.get("SCAN_BLACK_FRAME_CHECK", "1") != "0":
            self._require_not_black(paths, capture_info)

        # The per-camera exposure controls are trusted only when they describe exactly this capture
        cams_info = []
        if isinstance(capture_info, dict) and [str(p) for p in capture_info.get("paths", [])] == paths:
            cams_info = list(capture_info.get("cameras") or [])

        with self._manifest_lock:
            if self._manifest is None:
                self._manifest = self._new_manifest()
                self._manifest_dir = str(images_dir)
            for index, (path, key, size) in enumerate(zip(paths, keys, sizes)):
                cam = cams_info[index] if index < len(cams_info) and isinstance(cams_info[index], dict) else {}
                self._manifest["expected_keys"].append(key)
                self._manifest["photos"].append({
                    "key": key,
                    "path": path,
                    "size": size,
                    "level": level,
                    "shot": shot,
                    "camera": index,
                    "exposure_time": cam.get("exposure_time"),
                    "analogue_gain": cam.get("analogue_gain"),
                    "colour_gains": cam.get("colour_gains"),
                    "sensor_timestamp": cam.get("sensor_timestamp"),
                })
                log.info("[Scanner] Photo %s: exposure_time=%s analogue_gain=%s colour_gains=%s",
                         key, cam.get("exposure_time"), cam.get("analogue_gain"), cam.get("colour_gains"))

        # Track total physical images for the RAM heuristic
        self.total_photos += len(paths)

        # Manifest first, then the upload: whatever gets uploaded is always listed
        self._write_manifest()
        for path, key in zip(paths, keys):
            self._enqueue_upload(path, key)

    def _manifest_photo_count(self) -> int:
        with self._manifest_lock:
            return len(self._manifest["photos"]) if self._manifest else 0

    def _expected_keys(self) -> list[str]:
        with self._manifest_lock:
            return list(self._manifest["expected_keys"]) if self._manifest else []

    def move_z_up(self, steps: int = 20, delay: float = 0.002, step_type: Optional[str] = None, acceleration: bool = True, start_delay: Optional[float] = 0.004, ramp_percent: float = 0.2, decel_percent: Optional[float] = None, end_delay: Optional[float] = None) -> bool:
        """
        Move Z-axis UP. Blocked if the top endstop is already active or hit mid-flight.

        :raises ScannerBusyError: if another motion is in progress (nothing is sent).
        :return: False without sending anything when a cancel is pending (see `_cancel_applies`).
        """
        with self.motion_guard():
            # Flag first, then check the endstop: a press between the two would otherwise be ignored by the callback.
            self._z_direction = "up"       # before the flag: a stale edge of the other switch is then ignored
            if not self._begin_z_motion():
                return False
            watcher = None
            try:
                if self.up_endstop.is_active:
                    raise TopEndstopTriggered("Cannot move UP: Top limit switch is already reached!")

                watcher = self._start_endstop_watch(self.up_endstop, "TOP")
                success = self.z_axis_motor.rotate(
                    clockwise=True,
                    steps=steps,
                    delay=delay,
                    step_type=step_type,
                    acceleration=acceleration,
                    start_delay=start_delay,
                    ramp_percent=ramp_percent,
                    decel_percent=decel_percent,
                    end_delay=end_delay
                )
            finally:
                self._end_z_motion(watcher)
            if not success and self.up_endstop.is_active:
                raise TopEndstopTriggered("Top limit switch hit during upward travel!")
            return success

    def move_z_down(self, steps: int = 20, delay: float = 0.002, step_type: Optional[str] = None, acceleration: bool = True, start_delay: Optional[float] = 0.004, ramp_percent: float = 0.2, decel_percent: Optional[float] = None, end_delay: Optional[float] = None) -> bool:
        """
        Move Z-axis DOWN. Blocked if the bottom endstop is already active or hit mid-flight.

        :raises ScannerBusyError: if another motion is in progress (nothing is sent).
        :return: False without sending anything when a cancel is pending (see `_cancel_applies`).
        """
        with self.motion_guard():
            # Flag first, then check the endstop: a press between the two would otherwise be ignored by the callback.
            self._z_direction = "down"     # before the flag: a stale edge of the other switch is then ignored
            if not self._begin_z_motion():
                return False
            watcher = None
            try:
                if self.down_endstop.is_active:
                    raise BottomEndstopTriggered("Cannot move DOWN: Bottom (home) limit switch is already reached!")

                watcher = self._start_endstop_watch(self.down_endstop, "BOTTOM")
                success = self.z_axis_motor.rotate(
                    clockwise=False,
                    steps=steps,
                    delay=delay,
                    step_type=step_type,
                    acceleration=acceleration,
                    start_delay=start_delay,
                    ramp_percent=ramp_percent,
                    decel_percent=decel_percent,
                    end_delay=end_delay
                )
            finally:
                self._end_z_motion(watcher)
            if not success and self.down_endstop.is_active:
                raise BottomEndstopTriggered("Bottom limit switch hit during downward travel!")
            return success
        
    def move_z_up_angle(self, angle: float = 18.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.angle_to_steps_conversion(angle=angle, step_type=step_type)
        return self.move_z_up(steps=steps, step_type=step_type, **kwargs)

    def move_z_down_angle(self, angle: float = 18.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.angle_to_steps_conversion(angle=angle, step_type=step_type)
        return self.move_z_down(steps=steps, step_type=step_type, **kwargs)

    def move_z_up_distance(self, distance: float = 100.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.distance_to_step_conversion(distance=distance, step_type=step_type)
        return self.move_z_up(steps=steps, step_type=step_type, **kwargs)

    def move_z_down_distance(self, distance: float = 100.0, step_type: Optional[str] = None, **kwargs) -> bool:
        steps = self.z_axis_motor.distance_to_step_conversion(distance=distance, step_type=step_type)
        return self.move_z_down(steps=steps, step_type=step_type, **kwargs)

    def home_z_axis(self, delay: float = Z_PREPARE_DELAY_S, start_delay: float = 0.004, ramp_percent: float = 0.1, acceleration: bool = False):
        """
        The z-axis motor will rotate until it reaches the bottom (home) endstop.

        The travel is limited to Z_TRAVEL_MAX_MM + Z_HOMING_MARGIN_MM (16500 steps at 1/4 microstepping).
        With ``acceleration=False`` the carriage runs at the constant ``delay`` (``start_delay`` and ``ramp_percent``
        are ignored), like the manual z-move.

        :raises ScannerBusyError: if another motion is in progress (nothing is sent).
        :raises ScannerError: if the bottom endstop was not reached (move aborted or cancelled, serial error, or the
            travel limit ran out). Motor errors are propagated, never swallowed.
        """
        with self.motion_guard():
            if self._cancel_applies():
                raise ScannerError("Homing cancelled before it started.")
            homed = False
            self.z_axis_motor.enable()
            try:
                self.move_z_down(
                    steps=self.z_axis_motor.distance_to_step_conversion(distance=Z_TRAVEL_MAX_MM + Z_HOMING_MARGIN_MM),
                    delay=delay,
                    acceleration=acceleration,
                    start_delay=start_delay if acceleration else None,
                    ramp_percent=ramp_percent
                )
            except BottomEndstopTriggered:
                homed = True
            finally:
                self.z_axis_motor.disable() # Automatically settles for 0.5s then cuts power

            # The move can also end on the switch without raising (it was released again, or the callback raced the check)
            if homed or self.down_endstop.is_active:
                log.info("[Scanner] Carriage successfully homed to bottom endstop.")
                return
            raise ScannerError("Homing failed: the bottom endstop was not reached.")

    def move_z_to_top(self, delay: float = Z_PREPARE_DELAY_S, start_delay: float = 0.004, ramp_percent: float = 0.1, acceleration: bool = False):
        """
        The z-axis motor will rotate UP until it reaches the top endstop.

        The travel is limited to Z_TRAVEL_MAX_MM + Z_HOMING_MARGIN_MM (16500 steps at 1/4 microstepping).
        With ``acceleration=False`` the carriage runs at the constant ``delay`` (``start_delay`` and ``ramp_percent``
        are ignored), like the manual z-move.

        :raises ScannerBusyError: if another motion is in progress (nothing is sent).
        :raises ScannerError: if the top endstop was not reached (move aborted or cancelled, serial error, or the
            travel limit ran out). Motor errors are propagated, never swallowed.
        """
        with self.motion_guard():
            if self._cancel_applies():
                raise ScannerError("Move to top cancelled before it started.")
            reached = False
            self.z_axis_motor.enable()
            try:
                self.move_z_up(
                    steps=self.z_axis_motor.distance_to_step_conversion(distance=Z_TRAVEL_MAX_MM + Z_HOMING_MARGIN_MM),
                    delay=delay,
                    acceleration=acceleration,
                    start_delay=start_delay if acceleration else None,
                    ramp_percent=ramp_percent
                )
            except TopEndstopTriggered:
                reached = True
            finally:
                self.z_axis_motor.disable() # Automatically settles for 0.5s then cuts power

            if reached or self.up_endstop.is_active:
                log.info("[Scanner] Carriage successfully reached top endstop.")
                return
            raise ScannerError("Move to top failed: the top endstop was not reached.")

    def wait_for_light_stable(
        self,
        samples: int = 4,
        tolerance: float = 0.03,
        timeout_s: float = 60.0,
        stable_time_s: float = 10.0,
    ) -> bool:
        """
        Monitors ExposureTime * AnalogueGain metadata to confirm LED thermal stabilization.
        """
        if not self.dual_cameras:
            return False

        cams = [cam for cam in self.dual_cameras.cameras if getattr(cam, "picam2", None)]

        if not cams or not getattr(cams[0].picam2, "started", False):
            return False

        log.info("[Light] Verifying optical light stabilization...")
        try:
            for cam in cams:
                for key in ("AeEnable", "AwbEnable", "ExposureTime", "AnalogueGain", "ColourGains"):
                    cam._pending_controls.pop(key, None)
                cam.picam2.set_controls({"AeEnable": True, "AwbEnable": True})
        except Exception as e:
            log.warning("[Light] Could not enable AE for light stabilization: %s", e)
            return False

        if self._cancel_event.wait(timeout=0.4):
            return False

        def measure() -> float:
            vals = []
            for _ in range(samples):
                if self._cancel_event.is_set():
                    return 0.0
                try:
                    md = cams[0].picam2.capture_metadata()
                    exp = md.get("ExposureTime")
                    gain = md.get("AnalogueGain")
                    if exp and gain and exp > 0 and gain > 0:
                        vals.append(float(exp) * float(gain))
                except Exception:
                    pass
                if self._cancel_event.wait(timeout=0.06):
                    return 0.0
            return statistics.median(vals) if vals else 0.0

        start = time.monotonic()
        anchor_val = measure()
        anchor_time = time.monotonic()

        while time.monotonic() - start < timeout_s:
            if self._cancel_event.wait(timeout=1.0):
                return False

            if not self.lights.is_on:
                log.warning("[Light] Illumination was turned off or lost during stabilization.")
                return False

            cur_val = measure()
            if anchor_val <= 0 or cur_val <= 0:
                anchor_val = cur_val
                anchor_time = time.monotonic()
                continue

            drift = abs(cur_val - anchor_val) / anchor_val
            log.info("[Light] Thermal Drift: %.2f%% relative to anchor", drift * 100)

            if drift >= tolerance:
                anchor_val = cur_val
                anchor_time = time.monotonic()
            else:
                if time.monotonic() - anchor_time >= stable_time_s:
                    log.info("[Light] Illumination stabilized.")
                    return True

        log.warning("[Light] Thermal stabilization window timed out.")
        return False

    def setup_and_stream(
        self,
        enable_stream: bool = True,
        camera_mode: Optional[str] = None,
        preflight: bool = True,
        **kwargs
    ) -> list[str] | None:
        """
        Phase 1: Initializes cameras, gets initial stream image, and starts livestream.
        Returns the stream_urls immediately so the frontend can connect.

        :param preflight: run the scan preflight (R2, ToF, disk space) first. The manual /test endpoints pass False:
            they only need the cameras and the lights, not the cloud.
        """
        # The MJPEG preview has no bitrate; legacy callers may still pass one
        kwargs.pop("bitrate", None)

        # Cheap state validation first (a /prepare in the wrong state must not pay for the cloud preflight)
        with self._lock:
            self._check_prepare_allowed()

        # Checked BEFORE any state change: a failure leaves the scanner IDLE and the router answers 400 with the text
        if preflight:
            try:
                self.preflight()
            except ScannerPreflightError as e:
                self._set_phase(ScanPhase.ERROR, error=str(e))
                raise

        # Lock only for state validation and transition
        with self._lock:
            # An emergency stop is still tearing the previous run down: starting now would clear its cancel flag
            self._check_prepare_allowed()
            self.state = ScannerState.PREPARING
            self._cancel_event.clear()
            self.stream_urls = None
            self.total_photos = 0
            self._reset_status()
            self._set_phase(ScanPhase.PREPARING)

        try:
            # Cameras of an earlier run that were never released would make the new ones fail with "device busy",
            # and a brightness lock left behind would freeze the dimmer at an old value.
            self._close_cameras()
            self.lights.unlock_brightness()

            # A 0% brightness is refused BEFORE the lights are switched on and before the bucket is wiped
            self._require_brightness()
            self.lights.on()

            # Determine camera driver mode
            selected_mode = (
                camera_mode
                or kwargs.pop("camera_mode", None)
                or kwargs.pop("mode", None)
                or self.camera_mode
            )
            if isinstance(selected_mode, str):
                selected_mode = selected_mode.lower()

            if selected_mode in ("rpi_sync", "sync", "dual_camera_rpi_sync"):
                CameraClass = DualCameraRPISync
            elif selected_mode in ("xvs", "dual_camera_xvs"):
                CameraClass = DualCameraXVS
            elif selected_mode in ("staggered", "dual_camera", "legacy"):
                CameraClass = DualCamera
            else:
                log.warning("[Scanner] Unrecognized camera mode '%s', falling back to DualCameraRPISync", selected_mode)
                CameraClass = DualCameraRPISync

            # retrieve cameras arguments
            cam1_id = kwargs.pop("camera_id1", kwargs.pop("camera_id_1", kwargs.pop("master_id", 0)))
            cam2_id = kwargs.pop("camera_id2", kwargs.pop("camera_id_2", kwargs.pop("slave_id", 1)))

            quality = kwargs.get("quality", DEFAULT_QUALITY)
            photo_resolution = kwargs.get("photo_resolution", DEFAULT_PHOTO_RESOLUTION)
            video_resolution = kwargs.pop("video_resolution", DEFAULT_VIDEO_RESOLUTION)
            preview_resolution = kwargs.get("preview_resolution", DEFAULT_PREVIEW_RESOLUTION)

            # The photo rotation belongs to the camera objects (constructor), not to prepare_scan(): one value for
            # both cameras or a (cam1, cam2) pair
            rotation = kwargs.pop("rotation", None)
            if isinstance(rotation, (tuple, list)) and len(rotation) == 2:
                rotation1, rotation2 = rotation
            else:
                rotation1 = rotation2 = rotation or 0

            camera_kwargs = {
                "quality": quality,
                "size": photo_resolution,
                "video_size": video_resolution,
                "preview_size": preview_resolution,
                "rotation1": rotation1,
                "rotation2": rotation2,
            }

            self._lock_auto_supported = (CameraClass is DualCameraRPISync)
            if self._lock_auto_supported:
                # The MJPEG preview of the sync driver is its lores stream: preview_resolution sizes it
                camera_kwargs["stream_size"] = tuple(preview_resolution)
            self.dual_cameras = CameraClass(
                camera_id1=cam1_id,
                camera_id2=cam2_id,
                **camera_kwargs
            )

            # A cancel (emergency_stop) that landed while the cameras were being constructed has already run its
            # teardown: the cameras assigned just now would stay open with nobody to close them.
            if self._abort_prepare_if_cancelled():
                return None

            # keep_running=True is required so the livestream can work!
            kwargs["keep_running"] = True

            stream_camera_settings = {
                "camera_mode": selected_mode,
                "driver": getattr(CameraClass, "__name__", None) or type(CameraClass).__name__,
                "camera_id1": cam1_id,
                "camera_id2": cam2_id,
                "photo_resolution": photo_resolution,
                "preview_resolution": preview_resolution,
                "video_resolution": video_resolution,
                "photo_quality": quality,
                "stream_quality": getattr(self.dual_cameras, "stream_quality", 80),
                "enable_stream": enable_stream,
                **{k: v for k, v in kwargs.items() if k not in ("photo_resolution", "preview_resolution", "quality")},
            }
            log.info("[Scanner] Initializing camera stream & scan configuration: %s", stream_camera_settings)

            # prepare cameras for scanning (initial setup facing whatever direction)
            prepare_results = self.dual_cameras.prepare_scan(**kwargs)
            log.info("[Scanner] Applied camera settings from prepare_scan: %s", prepare_results)

            if self._abort_prepare_if_cancelled():
                return None

            if enable_stream:
                self.stream_urls = self.generate_livestream()

            if self._abort_prepare_if_cancelled():
                return None

            return self.stream_urls

        except Exception as e:
            self.cleanup()
            self._set_phase(ScanPhase.ERROR, error=f"Error in setup_and_stream: {e}")
            raise ScannerError(f"Error in setup_and_stream: {str(e)}")

    def _check_prepare_allowed(self) -> None:
        """Raises when a /prepare is not allowed right now. The caller holds self._lock."""
        # An emergency stop is still tearing the previous run down: starting now would clear its cancel flag
        if self._stopping:
            raise ScannerStoppingError("Cannot prepare. An emergency stop is still in progress.")
        if self.state not in ScannerState.PREPARE_ALLOWED:
            raise RuntimeError(f"Cannot prepare. Scanner is currently {self.state}")

    def _reset_status(self) -> None:
        """Clears the status data of the previous run (called when a new prepare starts)."""
        with self._upload_lock:
            self.photos_uploaded = 0
        self.missing = []
        self.error = None
        self.tof_warnings = []
        self.uploaded_keys = []
        self.upload_verified = None
        self._phase_note = None
        self.phase_message = None

    def _require_brightness(self) -> None:
        try:
            brightness = float(self.lights.brightness)
        except (TypeError, ValueError):
            return  # an unreadable value is not a "0%" setting
        if brightness <= 0:
            raise ScannerError("Brightness is 0%; cannot start a photogrammetry scan.")

    def _abort_prepare_if_cancelled(self) -> bool:
        """True (after releasing what this prepare opened) when a cancel arrived during setup_and_stream."""
        if not self._cancel_event.is_set():
            return False
        log.info("[Scanner] Prepare cancelled during camera setup; releasing the cameras.")
        self._try_step("camera release after cancel", self._close_cameras)
        self._try_step("lights off after cancel", self.lights.off)
        self.phase = ScanPhase.CANCELLED
        return True

    def preflight(self) -> None:
        """
        Checks, before a prepare starts, what would otherwise only fail after the scan: the cloud storage (credentials,
        write/read/delete rights), the ToF sensor and the free space for the photos. Stops at the first problem.

        :raises ScannerPreflightError: with a human readable message. SCAN_PREFLIGHT=0 skips the checks (development).
        """
        if os.environ.get("SCAN_PREFLIGHT", "1") == "0":
            log.warning("[Scanner] Preflight checks skipped (SCAN_PREFLIGHT=0).")
            return

        try:
            r2.preflight()
        except Exception as e:
            raise ScannerPreflightError(f"Cloud storage check failed: {e}")

        if not getattr(self.tof, "available", True):
            raise ScannerPreflightError(
                "The ToF distance sensor is not available (not detected or not initialised); "
                "the scan could not tell where the object ends."
            )
        try:
            first_reading = self.tof.read_distance_mm()
        except Exception as e:
            raise ScannerPreflightError(f"The ToF distance sensor failed a test reading: {e}")
        if first_reading is None:
            raise ScannerPreflightError(
                "The ToF distance sensor returned no valid test reading (check wiring and that the backdrop is in "
                "view)."
            )

        # The photo directory may not exist yet: measure the nearest existing parent
        probe = Path(self.images_dir)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free_mb = shutil.disk_usage(probe).free / (1024 * 1024)
        except OSError as e:
            raise ScannerPreflightError(f"Could not read the free disk space of {probe}: {e}")
        if free_mb < MIN_FREE_DISK_MB:
            raise ScannerPreflightError(
                f"Only {free_mb:.0f} MB of disk space left under {probe}; at least {MIN_FREE_DISK_MB:.0f} MB are "
                "needed for a scan."
            )

    def finish_preparation(self, **camera_kwargs):
        """
        Phase 2 & 3: Homes carriage, cleans R2, recalibrates cameras in front of the object.
        Designed to run as a background task.
        """
        # This thread now runs the prepare sequence: the Z helpers honour a pending cancel for it (_cancel_applies)
        self._tls.automated = True
        try:
            # Pop off initialization arguments that would crash ArducamIMX477
            camera_kwargs.pop("camera_id1", None)
            camera_kwargs.pop("camera_id2", None)
            camera_kwargs.pop("camera_id_1", None)
            camera_kwargs.pop("camera_id_2", None)
            camera_kwargs.pop("master_id", None)
            camera_kwargs.pop("slave_id", None)
            camera_kwargs.pop("video_resolution", None)
            camera_kwargs.pop("rotation", None)
            camera_kwargs.pop("camera_mode", None)
            camera_kwargs.pop("mode", None)

            # A cancel that landed before the first command must not start the homing at all
            if self._cancel_event.is_set(): return

            # home the motor (a cancel during the move makes the homing report "not reached": not an error)
            try:
                self.home_z_axis()
            except ScannerError:
                if self._cancel_event.is_set():
                    return
                raise
            if self._cancel_event.is_set(): return

            # Energize explicitly: homing ended with disable(), do not rely on the firmware's auto-enable
            self.z_axis_motor.enable()
            if not self.move_z_up_distance(distance=30.0, delay=Z_PREPARE_DELAY_S, acceleration=False):
                # Never leave the Z driver energized on an abort or a cancel (it is idle now)
                self.z_axis_motor.disable(settle_delay=0.0)
                if self._cancel_event.is_set():
                    return
                raise ScannerError("The Z-axis move to the scan start position (+30 mm) was aborted.")
            # The lead screw holds the carriage: do not leave the Z driver energized (1.8 A) through the light
            # stabilization and the wait for /scanner/start (the firmware energizes it by itself for every move).
            self.z_axis_motor.disable(interrupt=self._cancel_event)
            if self._cancel_event.is_set(): return

            # Reject scan if brightness is 0 (also checked before the lights went on in setup_and_stream; the user may
            # have changed it since), and always BEFORE the bucket is wiped
            self._require_brightness()

            # Clean up the cloud bucket before capturing new assets: a leftover key of an older scan would be
            # ingested with the new set. An incomplete cleanup is an error, not a warning.
            try:
                removed = r2.delete_all_files_from_bucket()
            except Exception as e:
                raise ScannerError(f"Cloud sanitization failed: {e}")
            log.info("[Scanner] Bucket cleaned before the scan (%s object(s) removed).", removed)

            if self._cancel_event.is_set(): return

            self.lights.lock_brightness()

            try:
                if not self.lights.is_on:
                    self.lights.on()

                if not self.lights.is_on:
                    raise ScannerError("Failed to energize LED illumination.")

                stable = self.wait_for_light_stable(timeout_s=90.0, stable_time_s=10.0, tolerance=0.03)
                
                if self._cancel_event.is_set():
                    self.lights.unlock_brightness()
                    return
                    
                if not stable:
                    raise ScannerError("Light thermal stabilization timed out.")

                if not self.lights.is_on:
                    raise ScannerError("Lights were turned off before camera lock.")

                # Recalibrate AE/AWB and focus in front of the illuminated object
                camera_kwargs["keep_running"] = True
                # Only this call locks AE/AWB (after the light has stabilised and in front of the object); the first
                # prepare_scan of setup_and_stream stays unlocked so the stream can adapt.
                if self._lock_auto_supported:
                    camera_kwargs["lock_auto"] = True
                log.info("[Scanner] In-position camera recalibration settings (home + 30mm): %s", camera_kwargs)
                recalib_results = self.dual_cameras.prepare_scan(**camera_kwargs)
                log.info("[Scanner] Applied recalibration settings from prepare_scan: %s", recalib_results)
                if self._lock_auto_supported:
                    unlocked = [
                        r for r in (recalib_results if isinstance(recalib_results, (list, tuple)) else [recalib_results])
                        if isinstance(r, dict) and not (r.get("locked") and r.get("ready", True))
                    ]
                    if unlocked:
                        raise ScannerError(
                            "AE/AWB lock failed on at least one camera; exposure would drift during the scan."
                        )
                
            except Exception:
                self.lights.unlock_brightness()
                self.lights.off()
                raise
            
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.PREPARED
                    self._level = 0
                    self._set_phase(ScanPhase.PREPARED)
                    log.info("[Scanner] Preparation fully completed!")

        except Exception as e:
            log.error(f"Background preparation failed: {e}")
            self.cleanup()
            with self._lock:
                if self.state != ScannerState.CANCELLED:
                    self.state = ScannerState.ERROR
                    self._set_phase(ScanPhase.ERROR, error=str(e))
        finally:
            self._tls.automated = False

    def generate_livestream(self):
        """Starts or retrieves livestream URLs."""
        if self.state not in (ScannerState.PREPARING, ScannerState.PREPARED) or self.dual_cameras is None:
            return None

        if not self.is_streaming:
            try:
                self.stream_urls = self.dual_cameras.start_stream()
                return self.stream_urls
            except Exception as e:
                self.cleanup()
                raise ScannerError(f"generate_livestream error: {str(e)}")
        else:
            if not self.stream_urls:
                try:
                    self.stream_urls = self.dual_cameras.start_stream()
                except Exception:
                    pass
            return self.stream_urls

    def stop_stream(self):
        """Stops the live stream and releases the camera devices."""
        if self.dual_cameras:
            try:
                self.dual_cameras.stop_stream()
                self.dual_cameras.close()
            except Exception as e:
                log.warning("[Scanner] Error stopping stream: %s", e)
            finally:
                self.dual_cameras = None
                self.stream_urls = None

    def _capture_turntable_slice(
        self,
        images_dir: str,
        angle: float,
        steps: int,
        delay_turntable: float,
        turntable_acceleration: bool = True,
        turntable_start_delay: Optional[float] = None,
        turntable_ramp_percent: float = 0.2,
        turntable_decel_percent: Optional[float] = None,
        level_label: Optional[Union[int, str]] = None,
        check_tof: bool = True,
        turntable_end_delay: Optional[float] = TURNTABLE_END_DELAY_DEFAULT,
    ) -> Optional[bool]:
        """
        Captures a 360-degree slice of photos by rotating the turntable in increments of `angle`.

        Returns the ToF verdict of the ring: True when an object was seen at any stop, False when at least one stop
        saw the backdrop with valid readings and none saw the object (the object has ended), None when no stop gave a
        valid reading (unknown: the caller must NOT treat it as "no object"). With check_tof False it returns False.
        """
        if level_label is None:
            level_label = self._level

        # Whole shots per revolution from the integer step counts (exact: no round(360 / angle) drift)
        full_turn_steps = angle_to_steps(360, TURNTABLE_STEP_TYPE)
        total_shots = max(1, full_turn_steps // steps)
        if total_shots > MAX_SHOTS_PER_RING:
            raise ScannerError(
                f"A ring of {total_shots} photos per camera is too many: the photo names hold the shot number "
                f"in 3 digits (at most {MAX_SHOTS_PER_RING} per ring). Use a larger turntable angle."
            )
        object_seen = False
        backdrop_count = 0
        unknown_count = 0
        threshold = TOF_BACKGROUND_DISTANCE_MM - TOF_CLEARANCE_MARGIN_MM

        for shot in range(total_shots):
            if self._cancel_event.is_set():
                break

            # emergency_stop() closes the cameras and sets self.dual_cameras = None from another thread
            cameras = self.dual_cameras
            if cameras is None:
                if self._cancel_event.is_set():
                    break
                raise ScannerError("The camera system is not available.")

            # 1. Capture 2 photos using DualCamera / DualCameraXVS (Master & Slave)
            try:
                photo_paths = cameras.capture_photo(
                    output_prefix=photo_file_stem(level_label, shot),
                    output_dir=images_dir,
                    meshroom_rig=True
                )
            except CaptureError as e:
                raise ScannerError(f"Photo capture failed at level {level_label}, shot {shot}: {e}")

            # 2. Verify both files, record them in the manifest and queue them for upload (never waits for uploads)
            self._register_photos(
                images_dir, photo_paths, level_label, shot,
                capture_info=getattr(cameras, "last_capture_info", None),
            )

            if self._cancel_event.is_set():
                break

            # 3. Rotate turntable to next position
            with self.motion_guard():
                rotated = self.turntable_motor.rotate(
                    clockwise=True,
                    steps=steps,
                    delay=delay_turntable,
                    acceleration=turntable_acceleration,
                    start_delay=turntable_start_delay,
                    ramp_percent=turntable_ramp_percent,
                    decel_percent=turntable_decel_percent,
                    end_delay=turntable_end_delay,
                    verbose=False
                )
            if not rotated:
                if self._cancel_event.is_set():
                    break
                # An aborted rotation means every following photo would be taken at a wrong angle
                raise ScannerError(
                    f"Turntable rotation aborted at level {level_label}, shot {shot}; scan positions are no longer valid."
                )

            # --- Settle Time & ToF Object Detection ---
            settle_start = time.monotonic()
            if check_tof and not object_seen:
                verdict = self._probe_tof(threshold)
                log.debug("[ToF] level %s shot %s: threshold=%.0f mm, verdict=%s", level_label, shot, threshold, verdict)
                if verdict is True:
                    object_seen = True
                    log.debug("Object detected by ToF sensor.")
                elif verdict is False:
                    backdrop_count += 1
                else:
                    unknown_count += 1

            remaining_settle = TURNTABLE_SETTLE_S - (time.monotonic() - settle_start)
            if remaining_settle > 0:
                time.sleep(remaining_settle)

        if object_seen:
            return True
        # A lone valid far reading among invalid ones is not proof that the object ended (a dark object absorbs the
        # laser): require that valid backdrop readings outnumber the invalid ones.
        if backdrop_count > unknown_count:
            return False
        return None if check_tof else False

    def _probe_tof(self, threshold: float) -> Optional[bool]:
        """True: object present. False: the backdrop was seen with valid readings. None: no valid reading."""
        try:
            verdict = self.tof.probe_object(threshold)
        except Exception as e:
            log.warning("[ToF] Probe failed: %s", e)
            return None
        return verdict if isinstance(verdict, bool) else None

    def _note_tof_unknown(self, level) -> None:
        message = (
            f"Level {level}: the ToF sensor returned no valid reading at any stop of the ring; "
            "the object end could not be checked, the scan continues upwards."
        )
        log.warning("[Scanner] %s", message)
        self.tof_warnings.append(message)

    def scan(
        self, 
        angle: float = 18.0, 
        delay_turntable: float = 0.0010,
        z_move_mm: float = 100.0,
        delay_z_motor: float = 0.0010,
        turntable_acceleration: bool = True,
        turntable_start_delay: Optional[float] = None,
        turntable_ramp_percent: float = 0.2,
        turntable_decel_percent: Optional[float] = None,
        z_acceleration: bool = False,
        z_start_delay: Optional[float] = None,
        z_ramp_percent: float = 0.2,
        z_decel_percent: Optional[float] = None,
        turntable_end_delay: Optional[float] = TURNTABLE_END_DELAY_DEFAULT,
        z_end_delay: Optional[float] = None,
        finish_state: str = ScannerState.IDLE
    ):
        """
        Capture mode: Loop rotating turntable and moving z-axis while streaming.
        Stops when the top endstop is triggered, cancellation is requested,
        or the requested logic completes.

        :param angle: The turntable rotation angle per step in degrees.
        :param delay_turntable: Cruise step delay in seconds between pulses for turntable.
        :param z_move_mm: Vertical distance to elevate the Z-axis carriage between slices.
        :param delay_z_motor: Cruise step delay in seconds between pulses for Z-axis motor.
        :param turntable_acceleration: Whether to use velocity ramping on the turntable.
        :param turntable_start_delay: Starting delay in seconds for turntable ramp.
        :param turntable_ramp_percent: Fraction (0.0 - 1.0) of steps used for turntable acceleration.
        :param turntable_decel_percent: Optional separate fraction (0.0 - 1.0) for turntable deceleration.
        :param z_acceleration: Whether to use velocity ramping on the Z-axis.
        :param z_start_delay: Starting delay in seconds for Z-axis ramp.
        :param z_ramp_percent: Fraction (0.0 - 1.0) of steps used for Z-axis acceleration.
        :param z_decel_percent: Optional separate fraction (0.0 - 1.0) for Z-axis deceleration.
        :param turntable_end_delay: Step delay in seconds at the end of the turntable deceleration, so every
            stop ends slowly (clamped to [END_DELAY_MIN, END_DELAY_MAX]). None disables the slow tail.
        :param z_end_delay: Same as turntable_end_delay for the Z-axis (None = off, the default).
        :param finish_state: State to leave the scanner in after a scan that completed normally. cleanup() resets it
            to IDLE, which would briefly allow a new /prepare before the cloud pipeline starts; the orchestrator
            passes PROCESSING to close that gap. Failed and cancelled scans keep their own terminal state.

        The physical scan ends first (motors released, lights off, cameras closed). The state stays RUNNING while the
        remaining uploads drain (phase "uploading") and the bucket is verified against the manifest (phase
        "verifying"); only then does the state change. A failed scan does not wait for the upload backlog.

        :raises ScannerUploadError: the scan ended but photos are missing from the bucket (`.missing`). The local
            files and the manifest are left untouched; the state is ERROR.
        """
        # Lock only for state validation, transition, and variable setup
        with self._lock:
            if self.state != ScannerState.PREPARED:
                raise RuntimeError("System must be prepared before scanning.")
            if self.dual_cameras is None:
                raise ScannerError("Dual Camera System is not initialized. Run prepare_scan() before scan().")

            # Validate the mechanical parameters BEFORE touching any state or hardware: a bad angle or z_move_mm
            # must leave the scanner PREPARED, not half-started (energized motors, running upload worker).
            steps = self.turntable_motor.angle_to_steps_conversion(angle)
            z_steps = self.z_axis_motor.distance_to_step_conversion(distance=z_move_mm)

            # Reset cancellation flag and photo counter for a fresh scan
            self._cancel_event.clear()
            self.total_photos = 0

            self.state = ScannerState.RUNNING
            self._reset_status()
            self._set_phase(ScanPhase.SCANNING)

        # The slow-stop tail is part of the deceleration ramp: without acceleration there is nothing to apply it to
        if not turntable_acceleration:
            turntable_end_delay = None
        if not z_acceleration:
            z_end_delay = None
        scan_ok = False

        # Lock released - physical execution begins without blocking emergency_stop()
        images_dir = str(self.images_dir)

        # This thread now runs the scan: the Z helpers honour a pending cancel for it (_cancel_applies)
        self._tls.automated = True
        try:
            # Everything that touches the hardware lives inside the try, so cleanup() always runs
            self.turntable_motor.enable()
            self.z_axis_motor.disable() # Z-axis is idle while taking photos on the first layer

            # The previous scan's photos are wiped here (the bucket was wiped in finish_preparation): from this point
            # on the directory holds only this scan, and the manifest starts empty.
            try:
                fc.create_clean_dir(images_dir)
            except Exception as e:
                raise ScannerError(f"Could not clean the local photo directory {images_dir}: {e}")
            if Path(images_dir).is_dir() and any(Path(images_dir).iterdir()):
                raise ScannerError(f"The local photo directory {images_dir} is not empty after the cleanup.")

            self._begin_manifest(images_dir)
            self.start_upload_worker()

            self._level = 1
            
            # Until the top endstop triggers or cancellation is requested
            while not self.up_endstop.is_active:
                # Check for cancellation before starting a new Z-level slice
                if self._cancel_event.is_set():
                    log.warning("[Scanner] Scan aborted by user.")
                    break

                ring_verdict = self._capture_turntable_slice(
                    images_dir=images_dir,
                    angle=angle,
                    steps=steps,
                    delay_turntable=delay_turntable,
                    turntable_acceleration=turntable_acceleration,
                    turntable_start_delay=turntable_start_delay,
                    turntable_ramp_percent=turntable_ramp_percent,
                    turntable_decel_percent=turntable_decel_percent,
                    level_label=self._level,
                    check_tof=True,
                    turntable_end_delay=turntable_end_delay,
                )

                # Exit outer loop if cancelled during the inner loop
                if self._cancel_event.is_set():
                    break

                # --- End of Level Evaluation ---
                # False: the sensor saw the backdrop with valid readings (and never the object): the object ended.
                # None: no valid reading at all: unknown, never a reason to end the scan (the top endstop bounds it).
                if ring_verdict is False:
                    if self._level == 1:
                        self._phase_note = (
                            "The ToF sensor saw only the backdrop at the first ring: the scan ends after one ring. "
                            "Check the object position and the ToF threshold."
                        )
                        self._set_phase(self.phase)
                    log.info(f"ToF Sensor cleared the object at level {self._level}. Terminating scan early.")
                    self.turntable_motor.disable()
                    break
                if ring_verdict is None:
                    self._note_tof_unknown(self._level)
                    if self._level == 1:
                        self._phase_note = (
                            "The ToF sensor gave no valid reading at the first ring; the scan continues without "
                            "end-of-object detection."
                        )
                        self._set_phase(self.phase)

                # If endstop was triggered during the slice, exit loop
                if self.up_endstop.is_active:
                    self.turntable_motor.disable()
                    break

                try:
                    # The settle pauses are interruptible and the cancel flag is re-checked before every step:
                    # a cancel that lands during a pause must not still lift the carriage.
                    self.turntable_motor.disable(interrupt=self._cancel_event)  # Holding torque settles for 0.5s then cuts power
                    if self._cancel_event.is_set():
                        break
                    self.z_axis_motor.enable()      # Energize Z-axis
                    if self._cancel_event.is_set():
                        break
                    z_moved = self.move_z_up(
                        steps=z_steps,
                        delay=delay_z_motor,
                        acceleration=z_acceleration,
                        start_delay=z_start_delay,
                        ramp_percent=z_ramp_percent,
                        decel_percent=z_decel_percent,
                        end_delay=z_end_delay
                    )
                    self.z_axis_motor.disable(interrupt=self._cancel_event)     # Done climbing, settles for 0.5s then cuts power
                    if self._cancel_event.is_set():
                        break
                    if not z_moved:
                        raise ScannerError("The Z-axis climb was aborted before reaching the next level.")
                    if self.up_endstop.is_active:
                        # The climb ended exactly on the top switch: rotate() reported success, but the carriage is at
                        # the ceiling. Take the final ring instead of silently leaving the loop without it.
                        raise TopEndstopTriggered("Top limit switch reached at the end of the climb.")
                    self.turntable_motor.enable()   # Re-enable turntable for next slice
                except TopEndstopTriggered:
                    log.info("[Scanner] Carriage reached the TOP ceiling during climb. Performing final top-level photo ring...")
                    self.z_axis_motor.disable()
                    self.turntable_motor.enable()
                    self._level += 1
                    self._capture_turntable_slice(
                        images_dir=images_dir,
                        angle=angle,
                        steps=steps,
                        delay_turntable=delay_turntable,
                        turntable_acceleration=turntable_acceleration,
                        turntable_start_delay=turntable_start_delay,
                        turntable_ramp_percent=turntable_ramp_percent,
                        turntable_decel_percent=turntable_decel_percent,
                        level_label=self._level,
                        check_tof=False,
                        turntable_end_delay=turntable_end_delay,
                    )
                    self.turntable_motor.disable()
                    break
                
                self._level += 1

            # A loop that ended without a single photo (e.g. the top switch was already active) is not a scan
            if self.total_photos == 0 and not self._cancel_event.is_set():
                raise ScannerError("The scan finished without capturing any photo.")

            # Every photo of the scan is in the manifest, i.e. both files were validated and queued
            if not self._cancel_event.is_set() and self._manifest_photo_count() != self.total_photos:
                raise ScannerError(
                    f"The manifest lists {self._manifest_photo_count()} photo(s) but {self.total_photos} were taken."
                )

            scan_ok = True

        except Exception as e:
            if self._cancel_event.is_set():
                # The cancel tore the hardware down (cameras closed, motors stopped) under the scan thread: the
                # resulting error is a consequence, not a failure.
                log.info("[Scanner] Scan stopped by cancellation (%s).", e)
            else:
                message = f"Error during scan: {str(e)}"
                # ERROR is published before anything else is torn down, and the backlog is not waited for
                with self._lock:
                    if self.state == ScannerState.RUNNING:
                        self.state = ScannerState.ERROR
                self._set_phase(ScanPhase.ERROR, error=message)
                raise ScannerError(message)
        finally:
            self._tls.automated = False
            completed = scan_ok and not self._cancel_event.is_set()
            if completed:
                # The hardware is released as soon as the last photo is taken: the lights and cameras must not stay
                # on for the upload. The state stays RUNNING until the drain and the verification are done.
                self._teardown_hardware()
            else:
                self.cleanup()
                # A failed or cancelled scan does not wait for the upload backlog
                self.stop_upload_worker(wait_for_completion=False)

        if self._cancel_event.is_set() or not scan_ok:
            self._set_phase(ScanPhase.CANCELLED)
            return

        try:
            self._drain_and_verify()
        finally:
            self._level = 0
            self.stop_upload_worker(wait_for_completion=False)

        if self._cancel_event.is_set():
            self._set_phase(ScanPhase.CANCELLED)
            return

        self._set_phase(ScanPhase.DONE)
        with self._lock:
            if self.state == ScannerState.RUNNING:
                self.state = finish_state

    def _drain_and_verify(self) -> None:
        """
        After the physical scan: waits for the upload backlog (phase "uploading"), then checks the bucket against the
        manifest (phase "verifying"). Returns quietly when the scan was cancelled meanwhile.

        :raises ScannerUploadError: photos are missing from the bucket; the state becomes ERROR.
        """
        self._set_phase(ScanPhase.UPLOADING)
        outcome = self._drain_uploads()
        if outcome == "cancelled" or self._cancel_event.is_set():
            return
        if outcome != "done":
            log.error("[Scanner] Upload drain ended with '%s'; %d photo(s) unresolved.", outcome, len(self._pending))

        self._set_phase(ScanPhase.VERIFYING)
        expected = self._expected_keys()
        with self._upload_lock:
            local_missing = sorted(
                self._upload_keys[p] for p in self._pending | self._upload_failed if p in self._upload_keys
            )
        verify_error = None
        try:
            missing = sorted(r2.verify_keys(expected))
        except Exception as e:
            verify_error = f"The bucket could not be listed to verify the upload: {e}"
            log.error("[Scanner] %s", verify_error)
            missing = local_missing
        if self._cancel_event.is_set():
            return

        missing_set = set(missing)
        self.uploaded_keys = [k for k in expected if k not in missing_set]
        self.missing = missing[:MISSING_STATUS_CAP]
        self.upload_verified = (not missing) and verify_error is None
        self.photos_uploaded = len(self.uploaded_keys)

        with self._manifest_lock:
            if self._manifest is not None:
                self._manifest["missing"] = list(missing)
                self._manifest["complete"] = bool(self.upload_verified)
        try:
            self._write_manifest()
            self._upload_manifest()
        except ScannerError as e:
            log.warning("[Scanner] Final manifest update failed: %s", e)

        if not self.upload_verified:
            if missing:
                message = f"{len(missing)} of {len(expected)} photo(s) are missing from the bucket."
            else:
                message = verify_error
            with self._lock:
                if self.state == ScannerState.RUNNING:
                    self.state = ScannerState.ERROR
            self._set_phase(ScanPhase.ERROR, error=message)
            raise ScannerUploadError(message, missing)

    def _upload_manifest(self) -> None:
        """Best effort: publishes manifest.json as rig/_manifest.json, outside the photo set (after verification)."""
        if self._manifest_dir is None:
            return
        path = str(Path(self._manifest_dir) / MANIFEST_NAME)
        try:
            if not r2.upload_file(path, object_name=CLOUD_MANIFEST_KEY):
                log.warning("[Scanner] The manifest could not be uploaded to %s.", CLOUD_MANIFEST_KEY)
        except Exception as e:
            log.warning("[Scanner] The manifest could not be uploaded to %s: %s", CLOUD_MANIFEST_KEY, e)

    @staticmethod
    def _try_step(what: str, fn, *args, **kwargs) -> bool:
        """Runs one teardown step; a failure is logged and never stops the remaining steps."""
        try:
            fn(*args, **kwargs)
            return True
        except Exception as e:
            log.error("[Scanner] Teardown step '%s' failed: %s", what, e)
            return False

    def _close_cameras(self) -> None:
        """Detaches the camera system first (a concurrent teardown then sees None) and shuts it down step by step."""
        cameras, self.dual_cameras = self.dual_cameras, None
        self.stream_urls = None
        if cameras:
            self._try_step("camera stop_stream", cameras.stop_stream)
            self._try_step("camera close", cameras.close)

    def emergency_stop(self):
        """
        Instantly stops all hardware and aborts ongoing processes.

        Never raises: every step runs in its own try/except, and the state ends as CANCELLED whatever failed.
        """
        log.warning("[Scanner] EMERGENCY STOP TRIGGERED!")

        # Publish CANCELLED first and raise `_stopping`, so a /prepare arriving now is refused instead of clearing the
        # cancel flag under the threads that are still winding down. The flag is set under `_z_stop_lock` too: a Z
        # move either sees it in _begin_z_motion() or is already flagged as moving when the stop below goes out.
        with self._lock:
            self._stopping_depth += 1
            self._stopping = True
            self.state = ScannerState.CANCELLED
            self.phase = ScanPhase.CANCELLED
            with self._z_stop_lock:
                self._cancel_event.set()

        try:
            motors = (("turntable", self.turntable_motor), ("z", self.z_axis_motor))

            # 1. Stop BOTH motors before anything else: the universal STOP is sent twice back to back, and no DISABLE
            #    (which blocks until the running move has ended) sits between the two.
            for name, motor in motors:
                self._try_step(f"{name} stop", motor.stop, repeat=2)

            # 2. Now release the torque
            for name, motor in motors:
                self._try_step(f"{name} disable", motor.disable, settle_delay=0.0)

            # 3. Cut lights
            self._try_step("lights unlock_brightness", self.lights.unlock_brightness)
            self._try_step("lights off", self.lights.off)

            # 4. Shut down cameras if running
            self._close_cameras()

            # 5. Final STOP: catches a move a straggler thread managed to start while the above was running
            for name, motor in motors:
                self._try_step(f"{name} final stop", motor.stop)
        finally:
            with self._lock:
                self._stopping_depth -= 1
                if self._stopping_depth <= 0:
                    self._stopping_depth = 0
                    self._stopping = False
                self.state = ScannerState.CANCELLED

    def cleanup(self):
        """
        Safely powers down all hardware (motors, lights, cameras). Every step is exception-safe: a failing motor does
        not leave the lights on or the cameras open.

        Does NOT overwrite terminal states (CANCELLED, ERROR, COMPLETED) back to IDLE,
        so the orchestrator and API can correctly observe the final outcome. It does not touch the state at all while
        an emergency_stop() is in progress (that one publishes the final state itself).
        """
        # Only reset to IDLE if we're not already in a terminal state
        with self._lock:
            if self.state not in ScannerState.TERMINAL and not self._stopping:
                self.state = ScannerState.IDLE

        self._teardown_hardware()
        self._level = 0

    def _teardown_hardware(self) -> None:
        """Releases motors, lights and cameras (every step exception-safe). Does not touch the state."""
        self._try_step("turntable stop", self.turntable_motor.stop, release_torque=True)
        self._try_step("z stop", self.z_axis_motor.stop, release_torque=True)
        self._try_step("lights unlock_brightness", self.lights.unlock_brightness)
        self._try_step("lights off", self.lights.off)

        self._close_cameras()

    def close_hardware(self) -> None:
        """
        Explicitly closes all hardware components, releasing GPIO pins and I2C bus.
        """
        log.info("[Scanner] Releasing all GPIO and hardware resources...")
        components = (
            ("turntable_motor", getattr(self, "turntable_motor", None)),
            ("z_axis_motor", getattr(self, "z_axis_motor", None)),
            ("up_endstop", getattr(self, "up_endstop", None)),
            ("down_endstop", getattr(self, "down_endstop", None)),
            ("lights", getattr(self, "lights", None)),
            ("tof", getattr(self, "tof", None)),
        )
        for name, component in components:
            if component and hasattr(component, "close"):
                try:
                    component.close()
                except Exception as e:
                    log.warning(f"[Scanner] Error closing {name}: {e}")

        # Ensure the ArduinoBridge is explicitly closed (sends 0xFF and releases serial port)
        if getattr(self, "turntable_motor", None) is not None:
            bridge = getattr(self.turntable_motor, "bridge", None)
            if bridge and hasattr(bridge, "close"):
                try:
                    bridge.close()
                except Exception as e:
                    log.warning(f"[Scanner] Error closing ArduinoBridge: {e}")

