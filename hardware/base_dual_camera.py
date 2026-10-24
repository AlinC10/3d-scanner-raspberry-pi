from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor
import os
import logging
import threading

from .camera import ArducamIMX477
from hardware.camera.config import (
    DEFAULT_QUALITY,
    DEFAULT_PHOTO_RESOLUTION,
    DEFAULT_VIDEO_RESOLUTION,
    DEFAULT_PREVIEW_RESOLUTION,
)

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """A still capture did not produce a usable image pair (missing frame, empty file, closed driver)."""


DEFAULT_STREAM_SIZE = (1024, 768)


class BaseDualCamera(ABC):
    """
    Abstract Base Class providing common lifecycle, controls, and helpers
    for dual-camera setups (DualCameraRPISync, DualCamera and DualCameraXVS).

    This class is THE interface of every dual-camera driver: the scanner and the routers talk to `scanner.dual_cameras`
    only through the members declared here (no `hasattr` / `isinstance` checks on a concrete class). A member that only
    some drivers implement still exists here with a safe default, and the driver advertises what it really supports
    through the capability flags below. When a driver gains a new feature that the scanner or a router uses, declare
    it here first (see FUTURE_WORK.md).

    Capabilities (class attributes, False unless the driver sets them):
      - ``supports_preview_stream``: live MJPEG preview (``wait_jpeg``, spectators, ``active_camera``).
      - ``supports_lock_auto``: ``prepare_scan(lock_auto=True)`` freezes AE/AWB after the cameras are in sync and
        copies cam1's exposure time and analogue gain to cam2 (its white-balance gains too only with
        ``copy_colour_gains=True``), and the returned dicts carry ``locked``/``ready``. Drivers without it always lock
        AE/AWB inside every ``prepare_scan`` (the ``lock_auto`` and ``copy_colour_gains`` arguments are accepted and
        ignored).
      - ``supported_photo_sizes``: the still sizes (W, H) the driver can capture without cropping the field of view,
        or None when it does not restrict them. A driver that sets it refuses any other ``size`` in its constructor.
    """

    supports_preview_stream: bool = False
    supports_lock_auto: bool = False
    supported_photo_sizes: Optional[Tuple[Tuple[int, int], ...]] = None

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
        stream_size: Optional[Tuple[int, int]] = None,
        stream_quality: int = 80,
    ):
        self.camera_id1 = camera_id1
        self.camera_id2 = camera_id2

        self.quality_val = quality


        if size is None:
            size = DEFAULT_PHOTO_RESOLUTION
        if video_size is None:
            video_size = DEFAULT_VIDEO_RESOLUTION
        if preview_size is None:
            preview_size = DEFAULT_PREVIEW_RESOLUTION

        if self.supported_photo_sizes is not None and tuple(size) not in self.supported_photo_sizes:
            raise ValueError(
                f"{type(self).__name__} supports the photo sizes {list(self.supported_photo_sizes)}, not {tuple(size)}"
            )

        self.size = size
        self.video_size = video_size
        self.preview_size = preview_size
        self.rotation1 = rotation1
        self.rotation2 = rotation2

        # Live-preview state shared by every driver (only drivers with supports_preview_stream feed it)
        self.stream_size = tuple(stream_size) if stream_size else DEFAULT_STREAM_SIZE
        self._stream_quality = max(1, min(100, int(stream_quality)))
        self._active_idx = 0  # 0 for cam1, 1 for cam2
        self._spectators = [0, 0]  # viewers connected to cam1 (idx 0) and cam2 (idx 1)
        self._spectator_lock = threading.Lock()
        # Result of the last autofocus sweep (one position per lens), None until one ran
        self.autofocus_positions: Optional[List[int]] = None

        self._executor = ThreadPoolExecutor(max_workers=2)
        self._closed: bool = False
        self.cam1: Optional[ArducamIMX477] = None
        self.cam2: Optional[ArducamIMX477] = None
        self.cameras: List[ArducamIMX477] = []
        # Set after each successful capture_photo (drivers that support it):
        # {"paths": [p1, p2], "cameras": [{exposure_time, analogue_gain,
        # colour_gains, sensor_timestamp, width, height}, {...}]}
        self.last_capture_info: Optional[dict] = None

    # ── Backward-Compatibility Properties ──────────────────────
    @property
    def master(self) -> Optional[ArducamIMX477]:
        return self.cam1

    @property
    def slave(self) -> Optional[ArducamIMX477]:
        return self.cam2

    # ── Lifecycle ──────────────────────────────────────────────
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """
        Idempotent teardown releasing thread pool and camera resources.
        Catches errors per camera to prevent unreleased CSI kernel locks.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True

        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            log.warning("ThreadPoolExecutor shutdown error: %s", e)

        for cam in self.cameras:
            if cam is not None:
                try:
                    cam.close()
                except Exception as e:
                    log.error("Failed closing camera %s: %s", getattr(cam, "camera_id", "?"), e)
        log.info("%s closed cleanly.", self.__class__.__name__)

    # ── Shared State & Controls ────────────────────────────────
    @property
    def is_streaming(self) -> bool:
        return any(
            getattr(cam.picam2, "recording", False)
            for cam in self.cameras
            if cam and hasattr(cam, "picam2")
        )

    @property
    def quality(self) -> int:
        return self.cam1.quality if self.cam1 else self.quality_val

    @quality.setter
    def quality(self, quality: int):
        self.quality_val = quality
        for cam in self.cameras:
            if cam:
                cam.quality = quality

    @property
    def stream_quality(self) -> int:
        """JPEG encoding quality for the low-resolution live preview stream (1-100)."""
        return self._stream_quality

    @stream_quality.setter
    def stream_quality(self, val: int):
        self._stream_quality = max(1, min(100, int(val)))
        log.info("%s: Stream JPEG quality updated to %d", type(self).__name__, self._stream_quality)

    # ── Live preview (meaningful when supports_preview_stream) ─
    @property
    def active_camera(self) -> int:
        """Returns the 1-indexed active preview camera (1 or 2)."""
        return self._active_idx + 1

    @active_camera.setter
    def active_camera(self, camera_id: int):
        """Sets the active preview camera (1 for cam1, 2 for cam2)."""
        if camera_id not in (1, 2):
            raise ValueError(f"active_camera must be 1 or 2 (got {camera_id})")
        self._active_idx = camera_id - 1
        log.info("%s: Active preview switched to camera %d", type(self).__name__, camera_id)

    @property
    def active(self) -> int:
        """Returns the 0-indexed active preview camera (0 or 1)."""
        return self._active_idx

    @active.setter
    def active(self, idx: int):
        """Sets the active preview camera (0 for cam1, 1 for cam2)."""
        if idx not in (0, 1):
            raise ValueError(f"active must be 0 or 1 (got {idx})")
        self._active_idx = idx

    @staticmethod
    def _spectator_index(camera_idx: int) -> int:
        """Maps a 0-indexed or 1-indexed camera number to 0 or 1."""
        return camera_idx if camera_idx in (0, 1) else (camera_idx - 1 if camera_idx in (1, 2) else 0)

    def add_spectator(self, camera_idx: int) -> None:
        """Increments the number of viewers of camera_idx (0-indexed or 1-indexed)."""
        idx = self._spectator_index(camera_idx)
        with self._spectator_lock:
            self._spectators[idx] += 1
            log.info("%s: Spectator connected to cam%d (active: %d)", type(self).__name__, idx + 1, self._spectators[idx])

    def remove_spectator(self, camera_idx: int) -> None:
        """Decrements the number of viewers of camera_idx (0-indexed or 1-indexed)."""
        idx = self._spectator_index(camera_idx)
        with self._spectator_lock:
            self._spectators[idx] = max(0, self._spectators[idx] - 1)
            log.info("%s: Spectator disconnected from cam%d (active: %d)", type(self).__name__, idx + 1, self._spectators[idx])

    def has_spectators(self, camera_idx: Optional[int] = None) -> bool:
        """True when camera_idx (or any camera) has viewers."""
        with self._spectator_lock:
            if camera_idx is not None:
                return self._spectators[self._spectator_index(camera_idx)] > 0
            return any(c > 0 for c in self._spectators)

    def wait_jpeg(self, camera_idx: int = 0, last_id: int = -1, timeout: float = 1.0) -> Tuple[int, Optional[bytes]]:
        """
        Waits up to `timeout` seconds for a JPEG preview frame newer than `last_id` and returns (frame_id, jpeg_bytes),
        or (last_id, None) on timeout. Drivers without ``supports_preview_stream`` have no preview: they return
        (last_id, None) immediately, so callers check the capability first.
        """
        return last_id, None

    def get_latest_lores_frames(self, timeout: float = 1.0, fresh: bool = False) -> Tuple[Optional[Any], Optional[Any]]:
        """
        Returns the most recent in-memory preview frames (cam1, cam2) as BGR numpy arrays.
        Used for real-time computer vision (e.g. MediaPipe auto-framing) with zero disk I/O.
        Default returns (None, None); implemented by continuous stream drivers (DualCameraRPISync).
        """
        return None, None

    def get_sync_status(self) -> Dict[str, Any]:
        """
        Synchronisation diagnostics (readiness, offsets, drops, fps). Drivers without a continuous sync pipeline return
        just their mode and streaming state.
        """
        return {"ready": True, "is_streaming": self.is_streaming, "mode": type(self).__name__}

    def rotate(self, direction: str) -> List[int]:
        """Rotates both cameras."""
        futures = [self._executor.submit(cam.rotate, direction) for cam in self.cameras]
        return [f.result() for f in futures]

    def apply_settings(self, **kwargs):
        """Apply image controls (exposure, gain, awb, etc.) to both cameras concurrently."""
        futures = [self._executor.submit(cam.apply_settings, **kwargs) for cam in self.cameras]
        for f in futures:
            f.result()

    def focus_set(self, position: int | Tuple[int, int]):
        """
        Set absolute focus position.
        Pass an int to set both cameras to the same position.
        Pass a tuple like (300, 400) to set independent positions for cam1 and cam2.
        """
        if isinstance(position, (tuple, list)) and len(position) == 2:
            f1 = self._executor.submit(self.cam1.focus_set, position[0])
            f2 = self._executor.submit(self.cam2.focus_set, position[1])
            f1.result()
            f2.result()
        else:
            futures = [self._executor.submit(cam.focus_set, position) for cam in self.cameras]
            for f in futures:
                f.result()

    def focus_step(self, delta: int | Tuple[int, int]):
        """
        Step focus by delta.
        Pass an int to step both cameras equally.
        Pass a tuple like (10, -10) to step them independently.
        """
        if isinstance(delta, (tuple, list)) and len(delta) == 2:
            f1 = self._executor.submit(self.cam1.focus_step, delta[0])
            f2 = self._executor.submit(self.cam2.focus_step, delta[1])
            f1.result()
            f2.result()
        else:
            futures = [self._executor.submit(cam.focus_step, delta) for cam in self.cameras]
            for f in futures:
                f.result()

    def focus_reset(self):
        """Reset focus to infinity for both cameras concurrently."""
        futures = [self._executor.submit(cam.focus_reset) for cam in self.cameras]
        for f in futures:
            f.result()

    def focus_sweep_autofocus(self, step: int = 30, roi: tuple = (0.3, 0.3, 0.4, 0.4)) -> List[int]:
        """Perform software sweep autofocus on both cameras concurrently."""
        futures = [self._executor.submit(cam.focus_sweep_autofocus, step=step, roi=roi) for cam in self.cameras]
        return [f.result() for f in futures]

    def lock_auto_features(self, settle_time: float = 2.0, copy_colour_gains: bool = True) -> List[dict]:
        """
        Lock current AE/AWB settings of both cameras and synchronize the exposure of the slave (cam2) to the master.

        ``copy_colour_gains``: True also copies cam1's white-balance gains onto cam2. False (the scanner's default)
        leaves cam2 on the gains its own AWB settled on, which it has just locked: the two sensors do not answer
        alike to the same gains, so copying them gives cam2 a colour cast (a bluish one on this rig).
        """
        futures = [self._executor.submit(cam.lock_auto_features, settle_time) for cam in self.cameras]
        res = [f.result() for f in futures]
        sync_controls = {
            "ExposureTime": res[0].get("ExposureTime"),
            "AnalogueGain": res[0].get("AnalogueGain"),
            "AeEnable": False,
        }
        if copy_colour_gains:
            sync_controls["ColourGains"] = res[0].get("ColourGains")
            sync_controls["AwbEnable"] = False
        sync_controls = {k: v for k, v in sync_controls.items() if v is not None}
        if sync_controls and len(self.cameras) > 1 and self.cam2:
            if getattr(self.cam2.picam2, "started", False):
                self.cam2.picam2.set_controls(sync_controls)
            self.cam2._pending_controls.update(sync_controls)
            if len(res) > 1:
                res[1].update(sync_controls)
        return res

    # ── Template Helpers (Defensive & 1-Indexed) ───────────────
    def _split_camera_kwargs(self, kwargs: dict) -> Tuple[dict, dict]:
        """
        Shallow-copies kwargs to avoid in-place caller mutation,
        splitting independent tuple values for focus and rotation.
        """
        k = kwargs.copy()
        focus_val = k.pop("focus", None)
        rotation_val = k.pop("rotation", None)
        kw1, kw2 = k.copy(), k.copy()
        if isinstance(focus_val, (tuple, list)) and len(focus_val) == 2:
            kw1["focus"], kw2["focus"] = focus_val[0], focus_val[1]
        else:
            kw1["focus"] = kw2["focus"] = focus_val

        if isinstance(rotation_val, (tuple, list)) and len(rotation_val) == 2:
            kw1["rotation"], kw2["rotation"] = rotation_val[0], rotation_val[1]
        else:
            kw1["rotation"] = kw2["rotation"] = rotation_val
        return kw1, kw2

    def _sync_slave_controls(self, res1: dict, res2: dict, original_kwargs: dict):
        """
        Synchronizes AE/AWB controls from cam1 to cam2 using defensive dictionary lookups.
        """
        needs_sync = False
        sync_controls = {"AeEnable": False, "AwbEnable": False}
        if original_kwargs.get("exposure_time") is None:
            sync_controls["ExposureTime"] = res1.get("controls", {}).get("ExposureTime")
            sync_controls["AnalogueGain"] = res1.get("controls", {}).get("AnalogueGain")
            needs_sync = True
        if original_kwargs.get("colour_gains") is None:
            sync_controls["ColourGains"] = res1.get("controls", {}).get("ColourGains")
            needs_sync = True
        sync_controls = {k: v for k, v in sync_controls.items() if v is not None}
        if needs_sync and sync_controls and self.cam2:
            if getattr(self.cam2.picam2, "started", False):
                self.cam2.picam2.set_controls(sync_controls)
            self.cam2._pending_controls.update(sync_controls)
            if "controls" in res2:
                res2["controls"].update(sync_controls)

    def _resolve_capture_paths(self, output_prefix: str, output_dir: str, meshroom_rig: bool) -> Tuple[str, str]:
        """
        Generates output JPEG paths.
        - If meshroom_rig=True: output_dir/0/prefix.jpg and output_dir/1/prefix.jpg.
        - If meshroom_rig=False: 1-indexed output_dir/prefix_cam1.jpg and prefix_cam2.jpg.
        """
        os.makedirs(output_dir, exist_ok=True) # Ensures parent directory exists in non-rig mode
        cam1_id = getattr(self.cam1, "camera_id", 0)
        cam2_id = getattr(self.cam2, "camera_id", 1)
        if meshroom_rig:
            dir0 = os.path.join(output_dir, str(cam1_id))
            dir1 = os.path.join(output_dir, str(cam2_id))
            os.makedirs(dir0, exist_ok=True)
            os.makedirs(dir1, exist_ok=True)
            return os.path.join(dir0, f"{output_prefix}.jpg"), os.path.join(dir1, f"{output_prefix}.jpg")
        return (
            os.path.join(output_dir, f"{output_prefix}_cam{cam1_id + 1}.jpg"),
            os.path.join(output_dir, f"{output_prefix}_cam{cam2_id + 1}.jpg"),
        )

    def _resolve_video_paths(self, output_prefix: str, output_dir: str, extension: str = ".mp4") -> Tuple[str, str]:
        """
        Generates output video paths with 1-indexed cam1 and cam2 suffixes.
        """
        cam1_id = getattr(self.cam1, "camera_id", 0)
        cam2_id = getattr(self.cam2, "camera_id", 1)
        return (
            os.path.join(output_dir, f"{output_prefix}_cam{cam1_id + 1}{extension}"),
            os.path.join(output_dir, f"{output_prefix}_cam{cam2_id + 1}{extension}"),
        )

    # ── Abstract Contract ──────────────────────────────────────
    @abstractmethod
    def prepare_scan(self, *, lock_auto: bool = False, copy_colour_gains: bool = False, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras for scanning.
        Subclasses MUST enforce timing contracts (e.g. 0.15s stagger in DualCamera;
        slave-first in DualCameraXVS).

        ``lock_auto``: only drivers with ``supports_lock_auto`` use it (AE/AWB are frozen when it is True, left running
        when False); the others always lock in every call and ignore it.
        ``copy_colour_gains``: when AE/AWB are locked, False leaves cam2 on the white-balance gains its own AWB settled
        on (exposure time and analogue gain are still copied from cam1); True also copies cam1's gains to cam2. Part of
        the shared interface; a driver that does not implement it ignores it (see PROBLEME_RAMASE.md).
        ``focus`` (kwarg): "auto" runs the sharpness sweep, an int sets both lenses, a pair sets (cam1, cam2).
        """
        pass

    @abstractmethod
    def start_stream(self, bitrate: int = 4_000_000) -> List[str]:
        """Start live stream and return stream URLs."""
        pass

    @abstractmethod
    def stop_stream(self) -> None:
        """Stop active live stream."""
        pass

    @abstractmethod
    def capture_photo(
        self,
        output_prefix: str = "dual_photo",
        output_dir: str = ".",
        meshroom_rig: bool = True,
        **kwargs,
    ) -> List[str]:
        """Capture still image pair."""
        pass

    @abstractmethod
    def record_video(
        self,
        output_prefix: str = "dual_video",
        duration: float = 10.0,
        **kwargs,
    ) -> List[str]:
        """Record video pair."""
        pass
