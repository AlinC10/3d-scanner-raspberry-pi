import os
import time
import logging
import threading
from collections import OrderedDict, deque
from typing import Optional, List, Tuple, Dict, Any

from .base_dual_camera import BaseDualCamera, CaptureError
from .camera import ArducamIMX477
from hardware.camera.config import (
    DEFAULT_QUALITY,
    DEFAULT_PHOTO_RESOLUTION,
    DEFAULT_VIDEO_RESOLUTION,
    DEFAULT_PREVIEW_RESOLUTION,
)

try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None
    PICAMERA2_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

log = logging.getLogger(__name__)

# rpi.sync synchronization mode constants
SYNC_OFF = 0
SYNC_SERVER = 1
SYNC_CLIENT = 2


def clamp_framerate(resolution: Tuple[int, int], requested_fps: int) -> int:
    """
    Clamps requested framerate based on IMX477 physical sensor limits.
    - Full 12 MP readout (4056x3040) physically cannot exceed 10 fps.
    - 2x2 binned mode (2028x1520) can operate up to 40 fps.
    """
    w, h = resolution
    if w > 2028 or h > 1520:
        return min(max(requested_fps, 1), 10)
    else:
        return min(max(requested_fps, 1), 40)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _summarize_metadata(md: Any) -> Dict[str, Any]:
    """Extracts the exposure-related request metadata recorded with every still (None when unavailable)."""
    if not isinstance(md, dict):
        md = {}
    exposure = md.get("ExposureTime")
    gain = md.get("AnalogueGain")
    ts = md.get("SensorTimestamp")
    gains = md.get("ColourGains")
    if isinstance(gains, (tuple, list)) and len(gains) == 2 and all(_is_number(g) for g in gains):
        gains = [float(gains[0]), float(gains[1])]
    else:
        gains = None
    return {
        "exposure_time": int(exposure) if _is_number(exposure) else None,
        "analogue_gain": float(gain) if _is_number(gain) else None,
        "colour_gains": gains,
        "sensor_timestamp": int(ts) if _is_number(ts) else None,
    }


class DualCameraRPISync(BaseDualCamera):
    """
    Dual camera controller utilizing Raspberry Pi software synchronization (`rpi.sync`).

    Operates both Arducam IMX477 cameras continuously at identical framerates:
      - cam1 (CSI-0): SyncMode = 1 (SYNC_SERVER)
      - cam2 (CSI-1): SyncMode = 2 (SYNC_CLIENT)

    Streams a lean lores (1024x768) YUV420 feed from the active camera to FastAPI MJPEG consumers,
    while maintaining synchronized sequence counters for full 12 MP photogrammetry still capture.
    """

    def __init__(
        self,
        camera_id1: int = 0,
        camera_id2: int = 1,
        quality: int = DEFAULT_QUALITY,
        size: Optional[Tuple[int, int]] = None,
        video_size: Optional[Tuple[int, int]] = None,
        preview_size: Optional[Tuple[int, int]] = None,
        rotation1: int = 0,
        rotation2: int = 0,
        fps: int = 10,
        stream_size: Tuple[int, int] = (1024, 768),
        stream_quality: int = 80,
        sync_frames: int = 30,
        buffer_count: int = 3,
        ready_timeout: float = 30.0,
        auto_start: bool = False,
        **kwargs,
    ):
        camera_id1 = kwargs.pop("camera_id_1", camera_id1)
        camera_id2 = kwargs.pop("camera_id_2", camera_id2)
        rotation1 = kwargs.pop("rotation_1", rotation1)
        rotation2 = kwargs.pop("rotation_2", rotation2)

        super().__init__(
            camera_id1=camera_id1,
            camera_id2=camera_id2,
            quality=quality,
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation1=rotation1,
            rotation2=rotation2,
        )

        log.info("Initializing DualCameraRPISync (CSI %d and %d)", camera_id1, camera_id2)

        # Single Hardware Ownership: instantiate production ArducamIMX477 instances
        self.cam1 = ArducamIMX477(
            camera_id=camera_id1,
            quality=quality,
            size=self.size,
            video_size=self.video_size,
            preview_size=self.preview_size,
            rotation=rotation1,
        )
        self.cam2 = ArducamIMX477(
            camera_id=camera_id2,
            quality=quality,
            size=self.size,
            video_size=self.video_size,
            preview_size=self.preview_size,
            rotation=rotation2,
        )
        self.cameras = [self.cam1, self.cam2]

        self.stream_size = stream_size
        self._stream_quality = max(1, min(100, int(stream_quality)))
        self.fps = clamp_framerate(self.size, fps)
        self.period_ns = int(1e9 / self.fps)
        self.period_us = int(1e6 / self.fps)
        self.sync_frames = sync_frames
        self.buffer_count = buffer_count
        self.ready_timeout = ready_timeout

        # Internal state & synchronization primitives
        self._running = False
        self._is_streaming = False
        self._active_idx = 0  # 0 for cam1, 1 for cam2

        # Spectator tracking: number of active viewers connected to cam1 (idx 0) and cam2 (idx 1)
        self._spectators = [0, 0]
        self._spectator_lock = threading.Lock()

        self._cond = threading.Condition()
        self._ready = [threading.Event(), threading.Event()]
        self._latest = [-1, -1]
        self._drops = [0, 0]
        self._ts_hist: List[OrderedDict] = [OrderedDict(), OrderedDict()]

        self._lores_slots: List[Optional[Tuple[int, int, int, Any]]] = [None, None]
        self._lores_slot: Optional[Tuple[int, int, int, Any]] = None  # (cam_idx, seq, ts, yuv_array)
        self._still: List[Optional[Tuple[int, int, Any, Dict[str, Any]]]] = [None, None]  # (seq, ts, still_yuv_array, metadata_summary)
        self._raw_reqs: List[Any] = [None, None]
        self._want_raw: bool = False
        self._still_target: Optional[int] = None
        self._force_still = [False, False]
        self._still_lock = threading.Lock()

        # JPEG publishing state for FastAPI MJPEG streaming (per camera)
        self._jpeg_cond = threading.Condition()
        self._jpegs: List[Optional[bytes]] = [None, None]
        self._jpeg_ids: List[int] = [0, 0]
        self._jpeg: Optional[bytes] = None  # Legacy single-slot alias for active camera
        self._jpeg_id: int = 0
        self._pub_times: deque = deque(maxlen=30)
        self._last_jpeg_kb: float = 0.0

        self._threads: List[threading.Thread] = []
        self._closing = False  # set at the start of close(), before stop_stream()

        if auto_start:
            self._start_pipeline()

    # ── Properties ─────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming and self._running

    @property
    def active_camera(self) -> int:
        """Returns 1-indexed active preview camera (1 or 2)."""
        return self._active_idx + 1

    @active_camera.setter
    def active_camera(self, camera_id: int):
        """Sets active preview camera (1 for cam1, 2 for cam2)."""
        if camera_id not in (1, 2):
            raise ValueError(f"active_camera must be 1 or 2 (got {camera_id})")
        self._active_idx = camera_id - 1
        log.info("DualCameraRPISync: Active preview switched to camera %d", camera_id)

    @property
    def active(self) -> int:
        """Returns 0-indexed active preview camera (0 or 1)."""
        return self._active_idx

    @active.setter
    def active(self, idx: int):
        """Sets active preview camera (0 for cam1, 1 for cam2)."""
        if idx not in (0, 1):
            raise ValueError(f"active must be 0 or 1 (got {idx})")
        self._active_idx = idx

    @property
    def stream_quality(self) -> int:
        """JPEG encoding quality for the low-resolution MJPEG live preview stream (1-100)."""
        return self._stream_quality

    @stream_quality.setter
    def stream_quality(self, val: int):
        self._stream_quality = max(1, min(100, int(val)))
        log.info("DualCameraRPISync: Stream JPEG quality updated to %d", self._stream_quality)

    def add_spectator(self, camera_idx: int) -> None:
        """Increment active spectator count for camera_idx (0-indexed or 1-indexed)."""
        idx = camera_idx if camera_idx in (0, 1) else (camera_idx - 1 if camera_idx in (1, 2) else 0)
        with self._spectator_lock:
            self._spectators[idx] += 1
            log.info("DualCameraRPISync: Spectator connected to cam%d (active: %d)", idx + 1, self._spectators[idx])

    def remove_spectator(self, camera_idx: int) -> None:
        """Decrement active spectator count for camera_idx (0-indexed or 1-indexed)."""
        idx = camera_idx if camera_idx in (0, 1) else (camera_idx - 1 if camera_idx in (1, 2) else 0)
        with self._spectator_lock:
            self._spectators[idx] = max(0, self._spectators[idx] - 1)
            log.info("DualCameraRPISync: Spectator disconnected from cam%d (active: %d)", idx + 1, self._spectators[idx])

    def has_spectators(self, camera_idx: Optional[int] = None) -> bool:
        """Check if there are active spectators for camera_idx or any camera."""
        with self._spectator_lock:
            if camera_idx is not None:
                idx = camera_idx if camera_idx in (0, 1) else (camera_idx - 1 if camera_idx in (1, 2) else 0)
                return self._spectators[idx] > 0
            return any(c > 0 for c in self._spectators)

    # ── Continuous Streaming & Grab Lifecycle ──────────────────

    def _start_pipeline(self):
        """Configures dual streams and starts continuous rpi.sync background grab loops."""
        if self._closed or self._closing:
            raise RuntimeError("DualCameraRPISync is closed; create a new instance to restart the cameras")
        if self._is_streaming or self._running:
            return

        log.info(
            "DualCameraRPISync: Configuring continuous rpi.sync pipeline (main=%s, lores=%s, fps=%d)",
            self.size,
            self.stream_size,
            self.fps,
        )

        if not PICAMERA2_AVAILABLE:
            log.warning("picamera2 is not available; dual camera sync pipeline running in stub mode.")
            self._running = True
            self._is_streaming = True
            self._ready[0].set()
            self._ready[1].set()
            return

        started: List[Any] = []
        try:
            # Configure both cameras
            # cam1 is SYNC_SERVER, cam2 is SYNC_CLIENT
            modes = [SYNC_SERVER, SYNC_CLIENT]
            for idx, (cam, mode) in enumerate(zip(self.cameras, modes)):
                p2 = cam.picam2
                controls = {
                    "FrameDurationLimits": (self.period_us, self.period_us),
                    "SyncMode": mode,
                }
                if mode == SYNC_SERVER:
                    controls["SyncFrames"] = self.sync_frames

                config = p2.create_video_configuration(
                    main={"size": self.size, "format": "YUV420"},
                    lores={"size": self.stream_size, "format": "YUV420"},
                    sensor={"output_size": self.size, "bit_depth": 12},
                    buffer_count=self.buffer_count,
                    controls=controls,
                )
                p2.configure(config)

            self._running = True
            self._ready[0].clear()
            self._ready[1].clear()

            # Startup order: Client (cam2) starts first to bind its listening IPC socket;
            # Server (cam1) starts second to broadcast sync pulses.
            log.info("DualCameraRPISync: Starting cam2 (SyncClient)...")
            self.cam2.picam2.start()
            started.append(self.cam2)
            time.sleep(0.05)
            log.info("DualCameraRPISync: Starting cam1 (SyncServer)...")
            self.cam1.picam2.start()
            started.append(self.cam1)

            # Launch background worker threads
            self._threads = [
                threading.Thread(target=self._grab_worker, args=(0,), name="RPISync-Grab-0", daemon=True),
                threading.Thread(target=self._grab_worker, args=(1,), name="RPISync-Grab-1", daemon=True),
                threading.Thread(target=self._publisher_worker, name="RPISync-Publisher", daemon=True),
            ]
            for t in self._threads:
                t.start()
        except Exception:
            # Roll back: never leave a half-started pipeline behind (the next call
            # would silently see _running=True and wait for a SyncReady that never comes).
            log.exception("DualCameraRPISync: pipeline start failed; rolling back")
            self._running = False
            self._is_streaming = False
            for t in self._threads:
                if t.is_alive():
                    t.join(timeout=1.0)
            self._threads = []
            for cam in reversed(self.cameras):
                if cam in started or getattr(getattr(cam, "picam2", None), "started", False):
                    try:
                        cam.picam2.stop()
                    except Exception as exc:
                        log.warning("DualCameraRPISync: rollback stop failed on cam %s: %s", getattr(cam, "camera_id", "?"), exc)
                self._invalidate_focus(cam)
            raise

        self._is_streaming = True
        log.info("DualCameraRPISync: Continuous stream pipeline started.")

    @staticmethod
    def _invalidate_focus(cam: Any) -> None:
        """The CSI bus (and the VCM) is unpowered while a camera is stopped: force the next focus write."""
        try:
            focuser = getattr(cam, "focuser", None)
            if focuser is not None:
                focuser.invalidate()
        except Exception as exc:
            log.debug("DualCameraRPISync: focuser.invalidate() failed: %s", exc)

    def _grab_worker(self, cam_idx: int):
        """
        Dedicated background worker thread for each camera.
        Continuously pulls capture requests, monitors SyncReady metadata, tracks sequence numbers,
        selectively copies lores frames for the active camera, and executes instantaneous req.release()
        to protect the hardware DMA CMA buffer pool.
        """
        cam = self.cameras[cam_idx]
        seq = -1
        last_ts = None
        started = False

        log.info("DualCameraRPISync: Grab worker %d started.", cam_idx + 1)

        while self._running:
            # Drain any dynamic controls queued in ArducamIMX477._pending_controls
            if cam._pending_controls and getattr(cam.picam2, "started", False):
                try:
                    ctrls = cam._pending_controls.copy()
                    cam._pending_controls.clear()
                    cam.picam2.set_controls(ctrls)
                except Exception as e:
                    log.warning("DualCameraRPISync: Failed applying pending controls on cam%d: %s", cam_idx + 1, e)

            try:
                req = cam.picam2.capture_request()
            except Exception as e:
                if not self._running:
                    break
                log.warning("DualCameraRPISync: capture_request failed on cam%d: %s", cam_idx + 1, e)
                time.sleep(0.02)
                continue

            try:
                md = req.get_metadata()
                if isinstance(md, dict):
                    cam._last_metadata = md  # lets apply_settings lock a missing exposure
                if not started:
                    if md.get("SyncReady", False):
                        started = True
                        self._ready[cam_idx].set()
                        log.info("DualCameraRPISync: Camera %d reached SyncReady state.", cam_idx + 1)
                    else:
                        continue  # Sync aligning; release and await SyncReady

                ts = md.get("SensorTimestamp") if isinstance(md, dict) else None
                if not isinstance(ts, (int, float)):
                    ts = time.monotonic_ns()

                if last_ts is None:
                    seq = 0
                    gap = 1
                else:
                    gap = max(1, round((ts - last_ts) / self.period_ns))
                    seq += gap

                last_ts = ts
                self._latest[cam_idx] = seq

                # Selective Lores Copy: Only copy if spectators are watching this camera
                lores = None
                with self._spectator_lock:
                    has_spec = self._spectators[cam_idx] > 0
                    fallback = (not any(self._spectators)) and (cam_idx == self._active_idx)
                if has_spec or fallback:
                    lores = req.make_array("lores").copy()

                # On-demand Still Capture Check
                with self._cond:
                    t = self._still_target
                    want_still = (
                        (t is not None and seq >= t and self._still[cam_idx] is None)
                        or self._force_still[cam_idx]
                    )
                    want_raw = self._want_raw if want_still else False

                still = None
                still_meta = None
                raw_req = None
                if want_still:
                    if want_raw:
                        try:
                            req.acquire()
                            raw_req = req
                        except Exception as e:
                            log.warning("DualCameraRPISync: req.acquire() failed: %s", e)
                    # 18.5 MB RAM copy of full 12 MP YUV420 plane
                    still = req.make_array("main").copy()
                    still_meta = _summarize_metadata(md)
                    self._force_still[cam_idx] = False

            finally:
                # CRITICAL: Always release DMA buffer back to libcamera kernel pool within 3-5 ms!
                req.release()

            # Update state under condition lock
            with self._cond:
                if gap > 1:
                    self._drops[cam_idx] += (gap - 1)

                h = self._ts_hist[cam_idx]
                h[seq] = ts
                while len(h) > 32:
                    h.popitem(last=False)

                if lores is not None:
                    self._lores_slots[cam_idx] = (cam_idx, seq, ts, lores)
                    self._lores_slot = (cam_idx, seq, ts, lores)

                if still is not None and self._still[cam_idx] is None:
                    self._still[cam_idx] = (seq, ts, still, still_meta)
                    if raw_req is not None:
                        self._raw_reqs[cam_idx] = raw_req

                self._cond.notify_all()

        log.info("DualCameraRPISync: Grab worker %d stopped.", cam_idx + 1)

    def _publisher_worker(self):
        """
        Background publisher thread.
        Encodes lores frames to JPEG on demand only for cameras with active spectators
        (or active preview fallback) and notifies waiting FastAPI MJPEG HTTP streaming consumers.
        """
        log.info("DualCameraRPISync: MJPEG publisher worker started.")

        while self._running:
            with self._cond:
                self._cond.wait_for(
                    lambda: any(s is not None for s in self._lores_slots) or not self._running,
                    timeout=1.0,
                )
                slots = []
                for idx in range(2):
                    if self._lores_slots[idx] is not None:
                        slots.append(self._lores_slots[idx])
                        self._lores_slots[idx] = None
                self._lores_slot = None

            if not self._running:
                break
            if not slots:
                continue

            for cam_idx, seq, ts, yuv in slots:
                with self._spectator_lock:
                    has_spec = self._spectators[cam_idx] > 0
                    fallback = (not any(self._spectators)) and (cam_idx == self._active_idx)

                if not (has_spec or fallback):
                    continue

                try:
                    if OPENCV_AVAILABLE:
                        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                        res = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self._stream_quality])
                        if isinstance(res, (tuple, list)) and len(res) == 2:
                            ok, jpg = res
                            if not ok or not hasattr(jpg, "tobytes"):
                                continue
                            data = jpg.tobytes()
                        else:
                            continue
                    else:
                        data = b""

                    self._last_jpeg_kb = len(data) / 1024.0
                    self._pub_times.append(time.monotonic())

                    with self._jpeg_cond:
                        self._jpegs[cam_idx] = data
                        self._jpeg_ids[cam_idx] += 1
                        if cam_idx == self._active_idx:
                            self._jpeg = data
                            self._jpeg_id += 1
                        self._jpeg_cond.notify_all()
                except Exception as e:
                    log.warning("DualCameraRPISync: MJPEG encoding error on cam%d: %s", cam_idx + 1, e)

        log.info("DualCameraRPISync: MJPEG publisher worker stopped.")

    def wait_jpeg(
        self,
        camera_or_last_id: int = -1,
        last_id: Optional[int] = None,
        timeout: float = 1.0,
        camera_idx: Optional[int] = None,
    ) -> Tuple[int, Optional[bytes]]:
        """
        Wait for a new JPEG frame. Supports both per-camera and legacy signatures:
        - wait_jpeg(cam_idx, last_id, timeout)
        - wait_jpeg(last_id, timeout)
        - wait_jpeg(camera_idx=0, last_id=last_id, timeout=timeout)
        Returns (frame_id, jpeg_bytes) or (last_id, None) on timeout.
        """
        if camera_idx is not None:
            cam_idx = camera_idx if camera_idx in (0, 1) else (camera_idx - 1 if camera_idx in (1, 2) else self._active_idx)
            lid = last_id if last_id is not None else -1
        elif last_id is not None:
            if camera_or_last_id in (0, 1):
                cam_idx = camera_or_last_id
            elif camera_or_last_id == 2:
                cam_idx = 1
            else:
                cam_idx = self._active_idx
            lid = last_id
        else:
            cam_idx = self._active_idx
            lid = camera_or_last_id

        with self._jpeg_cond:
            if not self._jpeg_cond.wait_for(
                lambda: self._jpeg_ids[cam_idx] != lid or not self._running,
                timeout=timeout,
            ):
                return lid, None
            return self._jpeg_ids[cam_idx], self._jpegs[cam_idx]

    def get_sync_status(self) -> Dict[str, Any]:
        """Returns diagnostic telemetry on sync status, offsets, drops, and stream fps."""
        now = time.monotonic()
        recent_pub = [t for t in self._pub_times if now - t <= 2.0]
        stream_fps = len(recent_pub) / 2.0 if len(recent_pub) > 1 else 0.0

        with self._cond:
            offset_ms = None
            if len(self._ts_hist[0]) > 0 and len(self._ts_hist[1]) > 0:
                # Calculate sub-frame phase delta using modulo arithmetic around frame period
                latest_ts0 = next(reversed(self._ts_hist[0].values()))
                latest_ts1 = next(reversed(self._ts_hist[1].values()))
                raw_diff_ns = latest_ts1 - latest_ts0
                half_period_ns = self.period_ns // 2
                aligned_diff_ns = ((raw_diff_ns + half_period_ns) % self.period_ns) - half_period_ns
                offset_ms = round(aligned_diff_ns / 1e6, 3)

            ready_states = [e.is_set() for e in self._ready]
            drops = list(self._drops)
            latest_seqs = list(self._latest)

        with self._spectator_lock:
            specs = list(self._spectators)

        return {
            "ready": all(ready_states),
            "cam1_ready": ready_states[0],
            "cam2_ready": ready_states[1],
            "cam1_seq": latest_seqs[0],
            "cam2_seq": latest_seqs[1],
            "active_camera": self.active_camera,
            "offset_ms": offset_ms,
            "drops": drops,
            "stream_fps": stream_fps,
            "last_jpeg_kb": self._last_jpeg_kb,
            "quality": self.quality,
            "stream_quality": self._stream_quality,
            "spectators": specs,
        }

    # ── Abstract Contract Implementation ───────────────────────

    def prepare_scan(self, *, lock_auto: bool = False, **kwargs) -> List[dict]:
        """
        Prepares both cameras for scanning under continuous rpi.sync.
        Awaits SyncReady alignment within ready_timeout (one shared deadline).

        ``lock_auto=True`` additionally freezes AE/AWB after SyncReady: both cameras
        settle, cam1's ExposureTime/AnalogueGain/ColourGains are written back with
        AE/AWB disabled and copied to cam2, so every photo of the scan uses the
        same exposure and white balance. Call it once the lighting is stable.
        The default (False) leaves AE/AWB running (e.g. the first, cold call).

        The returned per-camera info dicts carry ``locked`` (bool) and the locked
        ``exposure_time`` / ``analogue_gain`` / ``colour_gains`` (None when not locked).
        An optional ``settle_time`` kwarg (seconds, default 2.0) sets the AE/AWB settle
        time before locking.
        """
        self._start_pipeline()

        kw1, kw2 = self._split_camera_kwargs(kwargs)
        if "focus" in kwargs and kwargs["focus"] is not None:
            self.focus_set(kwargs["focus"])
        else:
            # No explicit focus: put the lenses back to the saved position. After a
            # reboot/camera restart the VCM is at 0, so the write is forced (CAM-2).
            failed_focus = []
            for cam in self.cameras:
                try:
                    restored = cam.focuser.restore_state()
                except Exception as exc:
                    log.error("DualCameraRPISync: could not restore focus on cam %s: %s", getattr(cam, "camera_id", "?"), exc)
                    restored = False
                if restored is False:
                    failed_focus.append(getattr(cam, "camera_id", "?"))
            if failed_focus and PICAMERA2_AVAILABLE:
                # With the VCM at 0 after a boot every photo would be out of focus: do not scan.
                raise RuntimeError(f"Could not restore the lens focus position on camera(s) {failed_focus}.")

        # Apply any runtime image controls passed in kwargs (exposure, gain, awb, etc.)
        control_keys = ("exposure_time", "analogue_gain", "colour_gains", "awb_mode", "brightness", "contrast", "saturation", "sharpness")
        runtime_controls = {k: kwargs[k] for k in control_keys if k in kwargs and kwargs[k] is not None}
        if runtime_controls:
            self.apply_settings(**runtime_controls)

        log.info("DualCameraRPISync: Awaiting SyncReady (timeout=%.1fs)...", self.ready_timeout)
        deadline = time.monotonic() + self.ready_timeout
        ready1 = self._ready[0].wait(max(0.0, deadline - time.monotonic()))
        ready2 = self._ready[1].wait(max(0.0, deadline - time.monotonic()))

        if not (ready1 and ready2):
            log.warning(
                "DualCameraRPISync: SyncReady wait ended: cam1=%s, cam2=%s. Continuing with best-effort sync.",
                ready1,
                ready2,
            )
        else:
            log.info("DualCameraRPISync: Both cameras reported SyncReady!")

        locks: List[Optional[dict]] = [None, None]
        if lock_auto:
            if ready1 and ready2:
                locks = self._lock_auto_exposure(runtime_controls, kwargs.get("settle_time"))
            else:
                log.warning("DualCameraRPISync: AE/AWB lock skipped because the cameras are not SyncReady.")

        def _info(idx: int, cam: Any, ready: bool) -> dict:
            lock = locks[idx]
            gains = lock.get("ColourGains") if lock else None
            return {
                "camera_id": getattr(cam, "camera_id", idx),
                "ready": ready,
                "seq": self._latest[idx],
                "photo_resolution": self.size,
                "preview_resolution": self.stream_size,
                "quality": self.quality,
                "stream_quality": self.stream_quality,
                "fps": self.fps,
                "focus": getattr(getattr(cam, "focuser", None), "position", None),
                "rotation": getattr(cam, "rotation", 0),
                "locked": lock is not None and all(
                    lock.get(k) is not None for k in ("ExposureTime", "AnalogueGain", "ColourGains")
                ),
                "exposure_time": lock.get("ExposureTime") if lock else None,
                "analogue_gain": lock.get("AnalogueGain") if lock else None,
                "colour_gains": [float(g) for g in gains] if gains is not None else None,
            }

        cam1_info = _info(0, self.cam1, ready1)
        cam2_info = _info(1, self.cam2, ready2)
        log.info("DualCameraRPISync: Prepared camera settings: cam1=%s, cam2=%s", cam1_info, cam2_info)
        return [cam1_info, cam2_info]

    def _lock_auto_exposure(self, runtime_controls: dict, settle_time: Optional[float]) -> List[Optional[dict]]:
        """
        Re-arms AE/AWB (so a second scan does not inherit the previous scan's locked values),
        lets them settle, then freezes cam1's result and copies it to cam2.
        Returns the per-camera lock results ([None, None] when locking failed).
        """
        try:
            rearm = {}
            if "exposure_time" not in runtime_controls and "analogue_gain" not in runtime_controls:
                rearm["AeEnable"] = True
            if "colour_gains" not in runtime_controls:
                rearm["AwbEnable"] = True
            if rearm:
                for cam in self.cameras:
                    cam._pending_controls.update(rearm)
                    if getattr(cam.picam2, "started", False):
                        cam.picam2.set_controls(rearm)

            if settle_time is None:
                settle_time = 2.0 if PICAMERA2_AVAILABLE else 0.0
            res = self.lock_auto_features(settle_time=float(settle_time))
            locks: List[Optional[dict]] = [dict(r) if isinstance(r, dict) else None for r in res[:2]]
            while len(locks) < 2:
                locks.append(None)
            if locks[0] is not None and locks[1] is not None:
                for key in ("ExposureTime", "AnalogueGain", "ColourGains"):
                    locks[1][key] = locks[0].get(key)  # cam2 was synced to cam1 by lock_auto_features
            log.info(
                "DualCameraRPISync: AE/AWB locked (exposure=%s us, gain=%s, colour_gains=%s)",
                locks[0].get("ExposureTime") if locks[0] else None,
                locks[0].get("AnalogueGain") if locks[0] else None,
                locks[0].get("ColourGains") if locks[0] else None,
            )
            return locks
        except Exception as exc:
            log.error("DualCameraRPISync: AE/AWB lock failed, exposure is NOT constant: %s", exc)
            return [None, None]

    def start_stream(self, bitrate: int = 4_000_000) -> List[str]:
        """
        Ensures continuous stream pipeline is active and returns local MJPEG streaming routes.
        """
        self._start_pipeline()
        return [
            "/test/stream/mjpeg?camera=1",
            "/test/stream/mjpeg?camera=2",
        ]

    def stop_stream(self) -> None:
        """Stops background worker threads and releases camera streaming pipelines."""
        if not self._running and not self._is_streaming:
            return

        log.info("DualCameraRPISync: Stopping stream pipeline...")
        self._running = False
        self._is_streaming = False

        with self._cond:
            self._cond.notify_all()
        with self._jpeg_cond:
            self._jpeg_cond.notify_all()

        for t in self._threads:
            if t.is_alive():
                t.join(timeout=1.0)
        self._threads.clear()

        for cam in self.cameras:
            if cam and hasattr(cam, "picam2") and getattr(cam.picam2, "started", False):
                try:
                    cam.picam2.stop()
                except Exception as e:
                    log.warning("DualCameraRPISync: Error stopping picam2 on cam %s: %s", getattr(cam, "camera_id", "?"), e)
            self._invalidate_focus(cam)

        log.info("DualCameraRPISync: Stream pipeline stopped.")

    def capture_photo(
        self,
        output_prefix: str = "dual_photo",
        output_dir: str = ".",
        meshroom_rig: bool = True,
        **kwargs,
    ) -> List[str]:
        """
        Capture still image pair with Two-Tier Latching (Zero Dropped Photos policy).

        1. Ensures continuous sync pipeline is active.
        2. Tier 1 (Strict Sync): Sets target_seq = max(cam1_seq, cam2_seq) + 2 with 250ms deadline.
        3. Tier 2 (Resilient Fallback): If 250ms expires, forces immediate latching of the latest available frames.
        4. Releases hardware DMA requests instantly in grab workers (3-5 ms).
        5. Concurrently in ThreadPoolExecutor:
           - Encodes still_yuv to JPEG directly at output path on disk.
           - If raw=True, calls req.save_dng() and releases the acquired request.
           - Invokes cam._process_captured_image(path, resolution, quality) to inject photogrammetry EXIF
             metadata (BodySerialNumber = b"cam1" / b"cam2", focal length) and apply optional software resizing.

        Returns [path1, path2] and stores per-camera exposure metadata in
        ``self.last_capture_info``.

        Raises CaptureError (real hardware mode only) when the driver is closed,
        when a frame for either camera is missing, or when an output file is
        missing/empty after encoding. Stub mode (no picamera2) writes placeholder
        files and never raises.
        """
        if self._closed or self._closing:
            raise CaptureError("capture_photo called on a closed DualCameraRPISync")
        self._start_pipeline()

        path1, path2 = self._resolve_capture_paths(output_prefix, output_dir, meshroom_rig)
        resolution = kwargs.get("resolution", self.size)
        quality = kwargs.get("quality", self.quality)
        raw = kwargs.get("raw", False)
        sync_timeout = kwargs.get("sync_timeout", 0.25)

        with self._still_lock:
            # Stub mode fallback if not running on physical hardware with Picamera2
            if not PICAMERA2_AVAILABLE or not self._running:
                log.info("DualCameraRPISync: Capturing in stub mode for %s and %s", path1, path2)
                for cam_idx, p in enumerate([path1, path2]):
                    if not os.path.exists(p):
                        with open(p, "wb") as f:
                            f.write(b"")
                    try:
                        self.cameras[cam_idx]._process_captured_image(p, resolution, quality)
                    except Exception as e:
                        log.debug("Stub _process_captured_image: %s", e)
                self.last_capture_info = {
                    "paths": [path1, path2],
                    "cameras": [
                        dict(_summarize_metadata(None), width=None, height=None) for _ in range(2)
                    ],
                }
                return [path1, path2]

            self.last_capture_info = None

            # Tier 1: Arm grab workers for target sequence matching
            with self._cond:
                self._still = [None, None]
                self._raw_reqs = [None, None]
                self._want_raw = raw

                seq0 = self._latest[0]
                seq1 = self._latest[1]
                target_seq = max(seq0, seq1) + 2
                self._still_target = target_seq
                self._cond.notify_all()

            # Await Tier 1 sequence match within sync_timeout
            start_t = time.monotonic()
            with self._cond:
                while (self._still[0] is None or self._still[1] is None) and self._running:
                    remaining = sync_timeout - (time.monotonic() - start_t)
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)

            # Check if Tier 1 succeeded or Tier 2 Fallback needed
            with self._cond:
                s0 = self._still[0]
                s1 = self._still[1]

            if s0 is not None and s1 is not None:
                diff_ms = abs(s0[1] - s1[1]) / 1e6
                log.info(
                    "DualCameraRPISync: Tier 1 Strict Sync SUCCESS (cam1_seq=%d, cam2_seq=%d, delta=%.2f ms)",
                    s0[0],
                    s1[0],
                    diff_ms,
                )
            else:
                # Tier 2 Resilient Fallback - Zero Dropped Photos!
                log.warning(
                    "DualCameraRPISync: Sync window timed out (250ms). Triggering Tier 2 Fallback Latch (cam1_ready=%s, cam2_ready=%s).",
                    s0 is not None,
                    s1 is not None,
                )
                with self._cond:
                    if self._still[0] is None:
                        self._force_still[0] = True
                    if self._still[1] is None:
                        self._force_still[1] = True
                    self._still_target = None  # Cancel target_seq restriction
                    self._cond.notify_all()

                fallback_deadline = time.monotonic() + 2.0
                with self._cond:
                    while (self._still[0] is None or self._still[1] is None) and self._running:
                        remaining = fallback_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cond.wait(timeout=remaining)

            # Retrieve latched still buffers and raw requests
            with self._cond:
                still_pair = list(self._still)
                raw_pair = list(self._raw_reqs)
                # Reset capture triggers
                self._still_target = None
                self._force_still = [False, False]
                self._want_raw = False

            # Never report success without both frames (DUAL-2): release held DMA requests and fail.
            missing = [i + 1 for i in range(2) if still_pair[i] is None]
            if missing:
                for held in raw_pair:
                    if held is not None:
                        try:
                            held.release()
                        except Exception:
                            pass
                raise CaptureError(
                    f"no frame received from camera(s) {missing} "
                    f"(running={self._running}, closed={self._closed or self._closing})"
                )

            def _process_one(cam_idx: int, out_path: str) -> Dict[str, Any]:
                still_item = still_pair[cam_idx]
                raw_req = raw_pair[cam_idx]
                cam = self.cameras[cam_idx]
                out_w = out_h = None

                # Save raw DNG if acquired
                if raw and raw_req is not None:
                    try:
                        dng_path = os.path.splitext(out_path)[0] + ".dng"
                        raw_req.save_dng(dng_path)
                        log.info("DualCameraRPISync: Saved raw DNG %s", dng_path)
                    except Exception as exc:
                        log.warning("DualCameraRPISync: Failed saving DNG %s: %s", out_path, exc)
                    finally:
                        try:
                            raw_req.release()
                        except Exception:
                            pass

                # A stale file from an earlier capture must not pass the size check below
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except OSError as exc:
                    log.warning("DualCameraRPISync: could not remove stale %s: %s", out_path, exc)

                # Encode still_yuv to disk as JPEG
                yuv = still_item[2]
                if OPENCV_AVAILABLE and yuv is not None and hasattr(yuv, "shape"):
                    try:
                        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                        # The ISP pads YUV420 rows to the buffer stride; drop the padding
                        # columns so they never reach the JPEG (no-op when stride == width).
                        shape = getattr(bgr, "shape", None)
                        if isinstance(shape, tuple) and len(shape) >= 2:
                            if isinstance(shape[1], int) and shape[1] > self.size[0]:
                                bgr = bgr[:, : self.size[0]]
                            out_h, out_w = int(shape[0]), min(int(shape[1]), self.size[0])
                        cv2.imwrite(out_path, bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    except Exception as exc:
                        log.warning("DualCameraRPISync: Failed cv2.imwrite for %s: %s", out_path, exc)

                if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    raise CaptureError(f"camera {cam_idx + 1}: no image written to {out_path}")

                # Invoke cam._process_captured_image to inject EXIF and resize
                try:
                    cam._process_captured_image(out_path, resolution, quality)
                except Exception as exc:
                    log.warning("DualCameraRPISync: _process_captured_image error on %s: %s", out_path, exc)

                if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    raise CaptureError(f"camera {cam_idx + 1}: {out_path} is empty after post-processing")

                # Final image size as _process_captured_image leaves it (resize, then rotate)
                full = getattr(cam, "FULL_RES", None)
                if out_w is not None and isinstance(resolution, tuple) and isinstance(full, tuple) and resolution != full:
                    out_w, out_h = resolution
                if out_w is not None and getattr(cam, "rotation", 0) in (90, 270):
                    out_w, out_h = out_h, out_w

                info = dict(still_item[3] or _summarize_metadata(None))
                info["width"], info["height"] = out_w, out_h
                return info

            # Dispatch concurrently via BaseDualCamera._executor
            try:
                futures = [
                    self._executor.submit(_process_one, 0, path1),
                    self._executor.submit(_process_one, 1, path2),
                ]
            except RuntimeError as exc:  # executor already shut down (close() raced us)
                raise CaptureError(f"capture aborted, driver is shutting down: {exc}") from exc
            infos: List[Optional[Dict[str, Any]]] = []
            first_error: Optional[BaseException] = None
            for fut in futures:
                try:
                    infos.append(fut.result())
                except Exception as exc:
                    infos.append(None)
                    first_error = first_error or exc
            if first_error is not None:
                if isinstance(first_error, CaptureError):
                    raise first_error
                raise CaptureError(f"capture failed: {first_error}") from first_error

            self.last_capture_info = {"paths": [path1, path2], "cameras": infos}
            log.info("DualCameraRPISync: Photos delivered successfully: %s, %s", path1, path2)
            return [path1, path2]

    def record_video(
        self,
        output_prefix: str = "dual_video",
        duration: float = 10.0,
        **kwargs,
    ) -> List[str]:
        """Video recording is not supported under continuous rpi.sync still mode."""
        raise NotImplementedError("Video recording is not supported under continuous rpi.sync mode.")

    def close(self):
        """
        Idempotent teardown: stops the streaming pipeline first, then closes the
        executor and both cameras (stop recording, Picamera2.close(), focuser bus).
        After close() the instance refuses to start the cameras again.
        """
        if getattr(self, "_closed", False):
            return
        self._closing = True  # blocks _start_pipeline()/capture_photo() while we tear down
        try:
            self.stop_stream()
        except Exception as e:
            log.warning("DualCameraRPISync: stop_stream failed during close: %s", e)
        super().close()
