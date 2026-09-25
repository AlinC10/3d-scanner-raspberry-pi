import logging
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor

from .camera import ArducamIMX477
from hardware.camera.config import (
    DEFAULT_QUALITY, 
    DEFAULT_PHOTO_RESOLUTION, 
    DEFAULT_VIDEO_RESOLUTION, 
    DEFAULT_PREVIEW_RESOLUTION
)

log = logging.getLogger(__name__)

class DualCamera:
    """
    Controls two Arducam IMX477 cameras simultaneously using multi-threading.
    
    This is intended to be used before migrating to a hardware-synchronized 
    XVS (Master-Slave) configuration. It provides a thread pool to dispatch
    identical commands (capture, focus, config) to both cameras in parallel.
    """

    def __init__(
        self,
        camera_id_1: int = 0,
        camera_id_2: int = 1,
        quality: int = DEFAULT_QUALITY,
        size: Optional[Tuple[int, int]] = None,
        video_size: Optional[Tuple[int, int]] = None,
        preview_size: Optional[Tuple[int, int]] = None,
        rotation_1: int = 0,
        rotation_2: int = 0,
    ):
        """
        Initializes the dual camera setup.
        """
        log.info("Initializing DualCamera (CSI %d and %d)", camera_id_1, camera_id_2)
        
        self.cam1 = ArducamIMX477(
            camera_id=camera_id_1, 
            quality=quality, 
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation_1
        )
        self.cam2 = ArducamIMX477(
            camera_id=camera_id_2, 
            quality=quality, 
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation_2
        )
        
        self.cameras = [self.cam1, self.cam2]
        
        # Use a ThreadPoolExecutor with 2 workers to run camera operations concurrently
        self._executor = ThreadPoolExecutor(max_workers=2)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Shutdown the thread pool and safely close both cameras."""
        self._executor.shutdown(wait=True)
        for cam in self.cameras:
            cam.close()
        log.info("DualCamera closed.")

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def quality(self) -> int:
        return self.cam1.quality

    @quality.setter
    def quality(self, quality: int):
        self.cam1.quality = quality
        self.cam2.quality = quality

    # ── Configuration & State ────────────────────────────────────────────────

    def rotate(self, direction: str) -> List[int]:
        """Rotates both cameras."""
        futures = [self._executor.submit(cam.rotate, direction) for cam in self.cameras]
        return [f.result() for f in futures]

    def apply_settings(self, **kwargs):
        """Apply image controls (exposure, gain, awb, etc.) to both cameras concurrently."""
        futures = [self._executor.submit(cam.apply_settings, **kwargs) for cam in self.cameras]
        for f in futures:
            f.result()

    def prepare_scan(self, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras concurrently for scanning.
        You can pass `focus=(300, 500)` to set different focus values on each camera.
        """
        focus_val = kwargs.pop("focus", None)
        
        if isinstance(focus_val, (tuple, list)) and len(focus_val) == 2 and isinstance(focus_val[0], (int, str, type(None))):
            # If a valid tuple is passed, give each camera its own focus parameter
            kwargs1 = kwargs.copy()
            kwargs1["focus"] = focus_val[0]
            
            kwargs2 = kwargs.copy()
            kwargs2["focus"] = focus_val[1]
            
            f1 = self._executor.submit(self.cam1.prepare_scan, **kwargs1)
            f2 = self._executor.submit(self.cam2.prepare_scan, **kwargs2)
            return [f1.result(), f2.result()]
        else:
            # Pass the exact same focus value (int, "auto", or None) to both
            kwargs["focus"] = focus_val
            futures = [self._executor.submit(cam.prepare_scan, **kwargs) for cam in self.cameras]
            return [f.result() for f in futures]

    def lock_auto_features(self, settle_time: float = 2.0) -> List[dict]:
        """Lock the current AE/AWB settings on both cameras concurrently."""
        futures = [self._executor.submit(cam.lock_auto_features, settle_time) for cam in self.cameras]
        return [f.result() for f in futures]

    # ── Focus ────────────────────────────────────────────────────────────────

    def focus_set(self, position: int | Tuple[int, int]):
        """
        Set absolute focus position.
        Pass an int to set both cameras to the same position.
        Pass a tuple like (300, 400) to set independent positions for cam1 and cam2.
        """
        if isinstance(position, (tuple, list)) and len(position) == 2:
            f1 = self._executor.submit(self.cam1.focus_set, position[0])
            f2 = self._executor.submit(self.cam2.focus_set, position[1])
            f1.result(); f2.result()
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
            f1.result(); f2.result()
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

    # ── Capture ──────────────────────────────────────────────────────────────

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
    ) -> List[str]:
        """
        Capture photos from both cameras concurrently.
        If meshroom_rig=True, outputs will be saved to output_dir/0/ and output_dir/1/
        with identical filenames for proper Meshroom rig detection.
        
        Returns:
            List of saved JPEG file paths.
        """
        import os
        futures = []
        
        for cam in self.cameras:
            if meshroom_rig:
                # Create the rig folders (0 and 1)
                rig_folder = os.path.join(output_dir, str(cam.camera_id))
                os.makedirs(rig_folder, exist_ok=True)
                # Meshroom requires identical filenames in different folders
                out_file = os.path.join(rig_folder, f"{output_prefix}.jpg")
            else:
                out_file = os.path.join(output_dir, f"{output_prefix}_cam{cam.camera_id}.jpg")
                
            f = self._executor.submit(
                cam.capture_photo,
                output=out_file,
                resolution=resolution or cam.size,
                quality=quality or cam.quality,
                raw=raw,
                show_preview=show_preview,
                preview_duration=preview_duration
            )
            futures.append(f)
            
        return [f.result() for f in futures]

    def record_video(
        self,
        output_prefix: str = "dual_video",
        duration: float = 10.0,
        resolution=None,
        quality: int = 25,
    ) -> List[str]:
        """
        Record videos from both cameras concurrently.
        Outputs will be automatically suffixed with their respective camera IDs.
        
        Returns:
            List of saved MP4 file paths.
        """
        futures = []
        for cam in self.cameras:
            out_file = f"{output_prefix}_cam{cam.camera_id}.mp4"
            f = self._executor.submit(
                cam.record_video,
                output=out_file,
                duration=duration,
                resolution=resolution or cam.video_size,
                quality=quality or cam.quality
            )
            futures.append(f)
            
        return [f.result() for f in futures]

    def start_stream(self, bitrate: int = 2_000_000):
        if not isinstance(bitrate, int):
            raise TypeError("Bitrate needs to be an integer (int)")

        futures = []
        for cam in self.cameras:
            f = self._executor.submit(cam.start_stream, bitrate=bitrate)
            futures.append(f)

        # return URL to the streams
        return [f.result() for f in futures]

    def stop_stream(self):
        for cam in self.cameras:
            self._executor.submit(cam.stop_stream)
