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


class DualCameraXVS(BaseDualCamera):
    """
    Controls two Arducam IMX477 cameras using Hardware XVS Synchronization.
    
    IMPORTANT: This class assumes hardware and OS are configured for XVS:
    1. A physical jumper cable connects the XVS pins between the two cameras.
    2. /boot/firmware/config.txt assigns one camera as 'source' (Master/cam1)
       and the other as 'sink' (Slave/cam2).
         
    CRITICAL BEHAVIOR:
    In XVS mode, the Slave camera (cam2) will freeze and wait for the Master
    camera (cam1) to start generating timing pulses. Therefore, the Slave camera
    MUST be started before the Master camera, and they must be started in separate
    threads to prevent deadlocking.
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
        Initializes the XVS synchronized dual camera setup.
        Accepts legacy master_id/slave_id/camera_id_1 keyword arguments for backward compatibility.
        """
        camera_id1 = kwargs.pop("master_id", kwargs.pop("camera_id_1", camera_id1))
        camera_id2 = kwargs.pop("slave_id", kwargs.pop("camera_id_2", camera_id2))
        rotation1 = kwargs.pop("master_rotation", kwargs.pop("rotation_1", rotation1))
        rotation2 = kwargs.pop("slave_rotation", kwargs.pop("rotation_2", rotation2))

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

        log.info("Initializing DualCameraXVS (Master: CSI %d, Slave: CSI %d)", camera_id1, camera_id2)

        # 1. Instantiate Master (cam1)
        self.cam1 = ArducamIMX477(
            camera_id=camera_id1,
            quality=quality,
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation1,
        )

        # 2. START Master Stream temporarily (generates clock pulses so Slave can safely probe sensor)
        self.cam1.picam2.configure(self.cam1.picam2.create_preview_configuration())
        self.cam1.picam2.start()
        time.sleep(0.5)

        # 3. Instantiate Slave (cam2) safely
        self.cam2 = ArducamIMX477(
            camera_id=camera_id2,
            quality=quality,
            size=size,
            video_size=video_size,
            preview_size=preview_size,
            rotation=rotation2,
        )

        # 4. Stop Master preview stream
        self.cam1.picam2.stop()

        self.cameras = [self.cam1, self.cam2]

    # ── Abstract Contract Implementation ───────────────────────

    def prepare_scan(self, **kwargs) -> List[dict]:
        """
        Prepare and lock both cameras concurrently for scanning.
        Enforces XVS sequencing: Slave thread starts FIRST, pauses, Master starts SECOND.
        """
        kw1, kw2 = self._split_camera_kwargs(kwargs)

        # 1. Start Slave thread FIRST (blocks waiting for XVS pulses)
        f_slave = self._executor.submit(self.cam2.prepare_scan, **kw2)

        # 2. Give the OS a tiny moment to initiate the slave thread
        time.sleep(0.1)

        # 3. Start Master camera (generates XVS pulses, unblocking Slave)
        f_master = self._executor.submit(self.cam1.prepare_scan, **kw1)

        master_res = f_master.result()
        slave_res = f_slave.result()

        # Synchronize AE/AWB controls from Master (cam1) to Slave (cam2)
        self._sync_slave_controls(master_res, slave_res, kwargs)

        log.info("DualCameraXVS: Prepared camera settings: master=%s, slave=%s", master_res, slave_res)
        return [master_res, slave_res]

    def start_stream(self, bitrate: int = 4_000_000) -> List[str]:
        """
        Starts RTSP live streams with Slave started before Master.
        """
        if not isinstance(bitrate, int):
            raise TypeError("Bitrate needs to be an integer (int)")

        f_slave = self._executor.submit(self.cam2.start_stream, bitrate=bitrate)
        time.sleep(0.1)
        f_master = self._executor.submit(self.cam1.start_stream, bitrate=bitrate)

        return [f_master.result(), f_slave.result()]

    def stop_stream(self) -> None:
        """
        Stops RTSP live streams sequentially.
        """
        self._executor.submit(self.cam2.stop_stream)
        time.sleep(0.1)
        self._executor.submit(self.cam1.stop_stream)

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
        **kwargs,
    ) -> List[str]:
        """
        Capture hardware-synchronized photos with Slave queued before Master.
        """
        out_master, out_slave = self._resolve_capture_paths(output_prefix, output_dir, meshroom_rig)

        # SLAVE FIRST
        f_slave = self._executor.submit(
            self.cam2.capture_photo,
            output=out_slave,
            resolution=resolution or self.cam2.size,
            quality=quality or self.cam2.quality,
            raw=raw,
            show_preview=show_preview,
            preview_duration=preview_duration,
        )

        # MASTER SECOND
        f_master = self._executor.submit(
            self.cam1.capture_photo,
            output=out_master,
            resolution=resolution or self.cam1.size,
            quality=quality or self.cam1.quality,
            raw=raw,
            show_preview=show_preview,
            preview_duration=preview_duration,
        )

        return [f_master.result(), f_slave.result()]

    def record_video(
        self,
        output_prefix: str = "xvs_video",
        output_dir: str = ".",
        duration: float = 10.0,
        resolution=None,
        quality: int = 25,
        **kwargs,
    ) -> List[str]:
        """
        Record videos with Slave queued before Master.
        """
        out_master, out_slave = self._resolve_video_paths(output_prefix, output_dir)

        # SLAVE FIRST
        f_slave = self._executor.submit(
            self.cam2.record_video,
            output=out_slave,
            duration=duration,
            resolution=resolution or self.cam2.video_size,
            quality=quality or self.cam2.quality,
        )

        # MASTER SECOND
        f_master = self._executor.submit(
            self.cam1.record_video,
            output=out_master,
            duration=duration,
            resolution=resolution or self.cam1.video_size,
            quality=quality or self.cam1.quality,
        )

        return [f_master.result(), f_slave.result()]
