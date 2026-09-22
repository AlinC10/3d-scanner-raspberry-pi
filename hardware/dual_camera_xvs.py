import logging
import time
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor

from camera import ArducamIMX477
from camera.config import DEFAULT_QUALITY

log = logging.getLogger(__name__)

class DualCameraXVS:
    """
    Controls two Arducam IMX477 cameras using Hardware XVS Synchronization.
    
    IMPORTANT: This class assumes your hardware and OS are configured for XVS:
    1. A physical jumper cable connects the XVS pins between the two cameras.
    2. /boot/firmware/config.txt assigns one camera as 'source' (Master) 
       and the other as 'sink' (Slave), e.g.:
         dtoverlay=imx477,camera0,xvs_source
         dtoverlay=imx477,camera1,xvs_sink
         
    CRITICAL BEHAVIOR:
    In XVS mode, the Slave camera will completely freeze and wait for the Master 
    camera to start generating timing pulses. Therefore, the Slave camera MUST 
    be started before the Master camera, and they must be started in separate 
    threads to prevent Python from deadlocking.
    """

    def __init__(
        self,
        master_id: int = 0,
        slave_id: int = 1,
        quality: int = DEFAULT_QUALITY,
        size: Optional[Tuple[int, int]] = None,
        video_size: Optional[Tuple[int, int]] = None,
        preview_size: Optional[Tuple[int, int]] = None,
        master_rotation: int = 0,
        slave_rotation: int = 0,
    ):
        """
        Initializes the XVS synchronized dual camera setup.
        """
        log.info("Initializing DualCameraXVS (Master: CSI %d, Slave: CSI %d)", master_id, slave_id)
        
        self.master = ArducamIMX477(
            camera_id=master_id, quality=quality, size=size,
            video_size=video_size, preview_size=preview_size, rotation=master_rotation
        )
        self.slave = ArducamIMX477(
            camera_id=slave_id, quality=quality, size=size,
            video_size=video_size, preview_size=preview_size, rotation=slave_rotation
        )
        
        self.cameras = [self.master, self.slave]
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
        log.info("DualCameraXVS closed.")

    # ── Configuration & State ────────────────────────────────────────────────

    def rotate(self, direction: str) -> List[int]:
        futures = [self._executor.submit(cam.rotate, direction) for cam in self.cameras]
        return [f.result() for f in futures]

    def apply_settings(self, **kwargs):
        futures = [self._executor.submit(cam.apply_settings, **kwargs) for cam in self.cameras]
        for f in futures:
            f.result()

    def prepare_scan(self, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras concurrently for scanning.
        """
        focus_val = kwargs.pop("focus", None)
        kwargs_master = kwargs.copy()
        kwargs_slave = kwargs.copy()
        
        if isinstance(focus_val, (tuple, list)) and len(focus_val) == 2 and isinstance(focus_val[0], (int, str, type(None))):
            kwargs_master["focus"] = focus_val[0]
            kwargs_slave["focus"] = focus_val[1]
        else:
            kwargs_master["focus"] = focus_val
            kwargs_slave["focus"] = focus_val

        # START SEQUENCE IS CRITICAL FOR XVS:
        # 1. Start the Slave camera thread FIRST. It will block waiting for XVS.
        f_slave = self._executor.submit(self.slave.prepare_scan, **kwargs_slave)
        
        # 2. Give the OS a tiny moment to initiate the slave thread.
        time.sleep(0.1)
        
        # 3. Start the Master camera. This generates the XVS pulses, unblocking the Slave.
        f_master = self._executor.submit(self.master.prepare_scan, **kwargs_master)
        
        return [f_master.result(), f_slave.result()]

    def start_stream(self, bitrate: int = 2_000_000):
        if not isinstance(bitrate, int):
            raise TypeError("Bitrate needs to be an integer (int)")

        f_slave = self._executor.submit(self.slave.start_stream, bitrate=bitrate)

        time.sleep(0.1)

        f_master = self._executor.submit(self.master.start_stream, bitrate=bitrate)

        # return URL to the streams
        return [f_master.result(), f_slave.result()]

    def stop_stream(self):
        self._executor.submit(self.slave.stop_stream)

        self._executor.submit(self.master.stop_stream)

    def lock_auto_features(self, settle_time: float = 2.0) -> List[dict]:
        futures = [self._executor.submit(cam.lock_auto_features, settle_time) for cam in self.cameras]
        return [f.result() for f in futures]

    # ── Focus ────────────────────────────────────────────────────────────────

    def focus_set(self, position: int | Tuple[int, int]):
        if isinstance(position, (tuple, list)) and len(position) == 2:
            f_master = self._executor.submit(self.master.focus_set, position[0])
            f_slave = self._executor.submit(self.slave.focus_set, position[1])
            f_master.result(); f_slave.result()
        else:
            futures = [self._executor.submit(cam.focus_set, position) for cam in self.cameras]
            for f in futures:
                f.result()
            
    def focus_step(self, delta: int | Tuple[int, int]):
        if isinstance(delta, (tuple, list)) and len(delta) == 2:
            f_master = self._executor.submit(self.master.focus_step, delta[0])
            f_slave = self._executor.submit(self.slave.focus_step, delta[1])
            f_master.result(); f_slave.result()
        else:
            futures = [self._executor.submit(cam.focus_step, delta) for cam in self.cameras]
            for f in futures:
                f.result()

    def focus_reset(self):
        futures = [self._executor.submit(cam.focus_reset) for cam in self.cameras]
        for f in futures:
            f.result()

    def focus_sweep_autofocus(self, step: int = 30, roi: tuple = (0.3, 0.3, 0.4, 0.4)) -> List[int]:
        futures = [self._executor.submit(cam.focus_sweep_autofocus, step=step, roi=roi) for cam in self.cameras]
        return [f.result() for f in futures]

    # ── Capture ──────────────────────────────────────────────────────────────

    def capture_photo(
        self,
        output_prefix: str = "xvs_photo",
        output_dir: str = ".",
        meshroom_rig: bool = True,
        resolution=None,
        quality: Optional[int] = None,
        raw: bool = False,
        show_preview: bool = False,
        preview_duration: float = 2.0,
    ) -> List[str]:
        """
        Capture hardware-synchronized photos.
        If meshroom_rig=True, outputs will be saved to output_dir/0/ and output_dir/1/
        with identical filenames for proper Meshroom rig detection.
        """
        import os
        
        if meshroom_rig:
            # Create the rig folders (0 and 1)
            dir0 = os.path.join(output_dir, str(self.master.camera_id))
            dir1 = os.path.join(output_dir, str(self.slave.camera_id))
            os.makedirs(dir0, exist_ok=True)
            os.makedirs(dir1, exist_ok=True)
            
            # Meshroom requires identical filenames in different folders
            out_master = os.path.join(dir0, f"{output_prefix}.jpg")
            out_slave = os.path.join(dir1, f"{output_prefix}.jpg")
        else:
            out_master = os.path.join(output_dir, f"{output_prefix}_master_cam{self.master.camera_id}.jpg")
            out_slave  = os.path.join(output_dir, f"{output_prefix}_slave_cam{self.slave.camera_id}.jpg")
        
        # SLAVE FIRST
        f_slave = self._executor.submit(
            self.slave.capture_photo,
            output=out_slave, resolution=resolution or self.slave.size, quality=quality or self.slave.quality,
            raw=raw, show_preview=show_preview, preview_duration=preview_duration
        )
        
        # MASTER SECOND
        f_master = self._executor.submit(
            self.master.capture_photo,
            output=out_master, resolution=resolution or self.master.size, quality=quality or self.master.quality,
            raw=raw, show_preview=show_preview, preview_duration=preview_duration
        )
            
        return [f_master.result(), f_slave.result()]

    def record_video(
        self,
        output_prefix: str = "xvs_video",
        duration: float = 10.0,
        resolution=None,
        quality: int = 25,
    ) -> List[str]:
        out_master = f"{output_prefix}_master_cam{self.master.camera_id}.mp4"
        out_slave  = f"{output_prefix}_slave_cam{self.slave.camera_id}.mp4"
        
        # SLAVE FIRST
        f_slave = self._executor.submit(
            self.slave.record_video,
            output=out_slave, duration=duration, resolution=resolution or self.slave.video_size, quality=quality or self.slave.quality
        )
        
        # time.sleep(0.1)
        
        # MASTER SECOND
        f_master = self._executor.submit(
            self.master.record_video,
            output=out_master, duration=duration, resolution=resolution or self.master.video_size, quality=quality or self.master.quality
        )
            
        return [f_master.result(), f_slave.result()]
