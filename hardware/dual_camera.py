import logging
import time
from typing import Optional, List, Tuple

from .base_dual_camera import BaseDualCamera
from .camera import ArducamIMX477
from hardware.camera.config import (
    DEFAULT_QUALITY,
    DEFAULT_PHOTO_RESOLUTION,
    DEFAULT_VIDEO_RESOLUTION,
    DEFAULT_PREVIEW_RESOLUTION,
)

log = logging.getLogger(__name__)


class DualCamera(BaseDualCamera):
    """
    Controls two Arducam IMX477 cameras simultaneously using multi-threading.
    
    Inherits common lifecycle, focus controls, rotation, and helpers from BaseDualCamera.
    Enforces a mandatory 0.15s hardware stagger on concurrent thread submissions to prevent
    Raspberry Pi 5 VideoCore ISP memory contention crashes.
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
        **kwargs,
    ):
        """
        Initializes the dual camera setup.
        Accepts legacy camera_id_1/rotation_1 keyword arguments for backward compatibility.
        """
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

        log.info("Initializing DualCamera (CSI %d and %d)", camera_id1, camera_id2)

        self.cam1 = ArducamIMX477(
            camera_id=camera_id1,
            quality=quality,
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation1,
        )
        self.cam2 = ArducamIMX477(
            camera_id=camera_id2,
            quality=quality,
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation2,
        )

        self.cameras = [self.cam1, self.cam2]

    # ── Abstract Contract Implementation ───────────────────────

    def prepare_scan(self, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras for scanning.
        Enforces a 0.15s stagger between cam1 and cam2 thread execution to prevent
        VideoCore ISP memory allocation collisions.
        """
        kw1, kw2 = self._split_camera_kwargs(kwargs)

        f1 = self._executor.submit(self.cam1.prepare_scan, **kw1)
        time.sleep(0.15)  # Stagger hardware initialization
        f2 = self._executor.submit(self.cam2.prepare_scan, **kw2)

        res1 = f1.result()
        res2 = f2.result()

        # Synchronize AE/AWB controls from cam1 to cam2 if auto features were used
        self._sync_slave_controls(res1, res2, kwargs)

        log.info("DualCamera: Prepared camera settings: cam1=%s, cam2=%s", res1, res2)
        return [res1, res2]

    def start_stream(self, bitrate: int = 4_000_000) -> List[str]:
        """
        Starts RTSP live streaming on both cameras with a 0.15s stagger.
        """
        if not isinstance(bitrate, int):
            raise TypeError("Bitrate needs to be an integer (int)")

        f1 = self._executor.submit(self.cam1.start_stream, bitrate=bitrate)
        time.sleep(0.15)  # Stagger encoder & stream initialization
        f2 = self._executor.submit(self.cam2.start_stream, bitrate=bitrate)

        return [f1.result(), f2.result()]

    def stop_stream(self) -> None:
        """
        Stops RTSP live streams sequentially.
        """
        self._executor.submit(self.cam1.stop_stream)
        time.sleep(0.1)
        self._executor.submit(self.cam2.stop_stream)

    def capture_photo(
        self,
        output_prefix: str = "dual_photo",
        output_dir: str = ".",
        meshroom_rig: bool = True,
        resolution=None,
        quality: Optional[int] = None,
        raw: bool = False,
        show_preview: bool = False,
        preview_duration: float = 2.0,
        **kwargs,
    ) -> List[str]:
        """
        Capture photos from both cameras concurrently with a 0.15s stagger.
        Outputs are saved with 1-indexed naming (or Meshroom rig folders).
        """
        out1, out2 = self._resolve_capture_paths(output_prefix, output_dir, meshroom_rig)

        f1 = self._executor.submit(
            self.cam1.capture_photo,
            output=out1,
            resolution=resolution or self.cam1.size,
            quality=quality or self.cam1.quality,
            raw=raw,
            show_preview=show_preview,
            preview_duration=preview_duration,
        )
        time.sleep(0.15)  # Stagger JPEG encoder allocation
        f2 = self._executor.submit(
            self.cam2.capture_photo,
            output=out2,
            resolution=resolution or self.cam2.size,
            quality=quality or self.cam2.quality,
            raw=raw,
            show_preview=show_preview,
            preview_duration=preview_duration,
        )

        return [f1.result(), f2.result()]

    def record_video(
        self,
        output_prefix: str = "dual_video",
        output_dir: str = ".",
        duration: float = 10.0,
        resolution=None,
        quality: int = 25,
        **kwargs,
    ) -> List[str]:
        """
        Record videos from both cameras concurrently with a 0.15s stagger.
        """
        out1, out2 = self._resolve_video_paths(output_prefix, output_dir)

        f1 = self._executor.submit(
            self.cam1.record_video,
            output=out1,
            duration=duration,
            resolution=resolution or self.cam1.video_size,
            quality=quality or self.cam1.quality,
        )
        time.sleep(0.15)
        f2 = self._executor.submit(
            self.cam2.record_video,
            output=out2,
            duration=duration,
            resolution=resolution or self.cam2.video_size,
            quality=quality or self.cam2.quality,
        )

        return [f1.result(), f2.result()]
