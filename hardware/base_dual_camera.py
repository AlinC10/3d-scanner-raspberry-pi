from abc import ABC, abstractmethod
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor
import os
import logging

from .camera import ArducamIMX477
from hardware.camera.config import (
    DEFAULT_QUALITY,
    DEFAULT_PHOTO_RESOLUTION,
    DEFAULT_VIDEO_RESOLUTION,
    DEFAULT_PREVIEW_RESOLUTION,
)

log = logging.getLogger(__name__)


class BaseDualCamera(ABC):
    """
    Abstract Base Class providing common lifecycle, controls, and helpers
    for dual-camera setups (standard DualCamera and DualCameraXVS).
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

        self.size = size
        self.video_size = video_size
        self.preview_size = preview_size
        self.rotation1 = rotation1
        self.rotation2 = rotation2

        self._executor = ThreadPoolExecutor(max_workers=2)
        self._closed: bool = False
        self.cam1: Optional[ArducamIMX477] = None
        self.cam2: Optional[ArducamIMX477] = None
        self.cameras: List[ArducamIMX477] = []

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
        return getattr(self, "_stream_quality", 80)

    @stream_quality.setter
    def stream_quality(self, val: int):
        self._stream_quality = max(1, min(100, int(val)))

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

    def lock_auto_features(self, settle_time: float = 2.0) -> List[dict]:
        """Lock current AE/AWB settings and synchronize slave (cam2) to match master (cam1)."""
        futures = [self._executor.submit(cam.lock_auto_features, settle_time) for cam in self.cameras]
        res = [f.result() for f in futures]
        sync_controls = {
            "ExposureTime": res[0].get("ExposureTime"),
            "AnalogueGain": res[0].get("AnalogueGain"),
            "ColourGains": res[0].get("ColourGains"),
            "AeEnable": False,
            "AwbEnable": False,
        }
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
    def prepare_scan(self, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras for scanning.
        Subclasses MUST enforce timing contracts (e.g. 0.15s stagger in DualCamera;
        slave-first in DualCameraXVS).
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
