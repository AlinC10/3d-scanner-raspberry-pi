import time
import threading
from typing import Optional
from .camera import Camera


class DualCamera:
    """
    Controls two cameras simultaneously using threading to minimize capture delay.
    Perfect for stereo 3D scanning.
    """

    def __init__(self, camera_id0: int = 0, camera_id1: int = 1) -> None:
        """
        Initialize dual camera controllers.

        :param camera_id0: CSI port index for the first camera (defaults to 0).
        :type camera_id0: int
        :param camera_id1: CSI port index for the second camera (defaults to 1).
        :type camera_id1: int
        """
        print("Initializing Dual Cameras... (This may take a few seconds)")
        self.cam0: Camera = Camera(camera_id=camera_id0)
        self.cam1: Camera = Camera(camera_id=camera_id1)
        print("Both cameras initialized successfully.")

    def take_photos(self, path0: str = "cam0_photo.jpg", path1: str = "cam1_photo.jpg") -> None:
        """
        Triggers both cameras simultaneously using threads.

        :param path0: File path to save the photo from camera 0.
        :type path0: str
        :param path1: File path to save the photo from camera 1.
        :type path1: str
        """
        t0 = threading.Thread(target=self.cam0.take_photo, args=(path0,))
        t1 = threading.Thread(target=self.cam1.take_photo, args=(path1,))

        t0.start()
        t1.start()

        # Wait for both independent captures to finish
        t0.join()
        t1.join()
        print(f"Dual capture complete: {path0} & {path1}")

    def start_dual_video(
        self,
        path0: str = "cam0_vid.mp4",
        path1: str = "cam1_vid.mp4",
        duration_sec: int = 5,
    ) -> None:
        """
        Records video on both cameras simultaneously.

        :param path0: File path to save the video from camera 0.
        :type path0: str
        :param path1: File path to save the video from camera 1.
        :type path1: str
        :param duration_sec: Duration in seconds to record video.
        :type duration_sec: int
        """
        t0 = threading.Thread(target=self.cam0.start_video, args=(path0, duration_sec))
        t1 = threading.Thread(target=self.cam1.start_video, args=(path1, duration_sec))

        t0.start()
        t1.start()
        t0.join()
        t1.join()

    def auto_focus(self) -> None:
        """
        Autofocuses both cameras simultaneously.
        """
        t0 = threading.Thread(target=self.cam0.auto_focus)
        t1 = threading.Thread(target=self.cam1.auto_focus)
        t0.start()
        t1.start()
        t0.join()
        t1.join()

    def fix_focus(self, focus_value: float | int = 5.0) -> None:
        """
        Sets same manual focus on both cameras.

        :param focus_value: Manual focus distance / lens position value.
        :type focus_value: float
        """
        self.cam0.fix_focus(focus_value)
        self.cam1.fix_focus(focus_value)

    def lock_exposure(self) -> None:
        """
        Locks exposure on both cameras.
        """
        self.cam0.lock_exposure()
        self.cam1.lock_exposure()

    def auto_exposure(self) -> None:
        """
        Re-enables auto exposure on both cameras.
        """
        self.cam0.auto_exposure()
        self.cam1.auto_exposure()

    def close(self) -> None:
        """
        Closes both cameras properly.
        """
        print("Shutting down Dual Cameras...")
        self.cam0.close()
        self.cam1.close()


class HardwareSyncDualCamera:
    """
    Controls two cameras simultaneously using PERFECT 0-millisecond hardware sync (XVS pins).
    Prerequisites:
    1. XVS and GND pins are physically jumped via wire between both cameras.
    2. Pi /boot/firmware/config.txt is configured to set cam1 as the Sync Slave.
    """

    def __init__(self, master_id: int = 0, slave_id: int = 1) -> None:
        """
        Initialize hardware-synchronized dual cameras.

        :param master_id: Camera ID for the master camera (defaults to 0).
        :type master_id: int
        :param slave_id: Camera ID for the slave camera (defaults to 1).
        :type slave_id: int
        """
        print("Initializing Hardware-Synced Dual Cameras...")
        self.master_id: int = master_id
        self.slave_id: int = slave_id

        self.master_cam: Optional[Camera] = None
        self.slave_cam: Optional[Camera] = None

        # CRITICAL STARTUP SEQUENCE FOR HARDWARE SYNC:
        # A slave camera strictly wait for the XVS electrical pulse from the master to begin streaming frames.
        # If the master starts before the slave is listening, the slave might timeout and crash.
        # If the slave is initiated normally, it will "hang" blocking the code until the master starts.
        # Therefore, we MUST start them using background threads so they initialize properly.

        def start_slave() -> None:
            print(f"[Hardware Sync] Starting Slave Camera ({self.slave_id}) - Waiting for XVS...")
            self.slave_cam = Camera(camera_id=self.slave_id)

        def start_master() -> None:
            print(f"[Hardware Sync] Starting Master Camera ({self.master_id}) - Sending XVS pulse...")
            self.master_cam = Camera(camera_id=self.master_id)

        # 1. Placed Slave camera in a listening/waiting state
        t_slave = threading.Thread(target=start_slave)
        t_slave.start()

        # 2. Give the OS a tiny fraction of a second to ensure the Slave is fully waiting
        time.sleep(0.1)

        # 3. Start the Master camera (sending the XVS pulse, waking up the slave perfectly in sync)
        t_master = threading.Thread(target=start_master)
        t_master.start()

        # Wait until both are fully online
        t_slave.join()
        t_master.join()
        print("Hardware Sync Cameras initialized successfully! Streams are locked together.")

    def take_photos(
        self,
        master_path: str = "cam0_photo.jpg",
        slave_path: str = "cam1_photo.jpg",
    ) -> None:
        """
        Triggers both cameras. The python trigger delay doesn't matter anymore,
        because the cameras' hardware buffers are perfectly frame-locked by the XVS wire.

        :param master_path: File path to save master camera image.
        :type master_path: str
        :param slave_path: File path to save slave camera image.
        :type slave_path: str
        """
        # We still use threads so Python doesn't block sequentially,
        # but the actual image capture timing is governed by the linked sensors.
        t_slave = threading.Thread(target=self.slave_cam.take_photo, args=(slave_path,))
        t_master = threading.Thread(target=self.master_cam.take_photo, args=(master_path,))

        # Slave readies first, then master triggers
        t_slave.start()
        time.sleep(0.01)
        t_master.start()

        t_slave.join()
        t_master.join()
        print(f"Perfect Hardware Sync capture complete: {master_path} & {slave_path}")

    def auto_focus(self) -> None:
        """
        Autofocuses both cameras simultaneously.
        """
        t_slave = threading.Thread(target=self.slave_cam.auto_focus)
        t_master = threading.Thread(target=self.master_cam.auto_focus)

        t_slave.start()
        t_master.start()
        t_slave.join()
        t_master.join()

    def fix_focus(self, focus_value: float| int = 5.0) -> None:
        """
        Sets same manual focus on both cameras.

        :param focus_value: Manual focus distance / lens position value.
        :type focus_value: float
        """
        self.slave_cam.fix_focus(focus_value)
        self.master_cam.fix_focus(focus_value)

    def lock_exposure(self) -> None:
        """
        Locks exposure on both cameras.
        """
        self.slave_cam.lock_exposure()
        self.master_cam.lock_exposure()

    def auto_exposure(self) -> None:
        """
        Re-enables auto exposure on both cameras.
        """
        self.slave_cam.auto_exposure()
        self.master_cam.auto_exposure()

    def close(self) -> None:
        """
        Closes both cameras properly.
        """
        print("Shutting down Hardware Sync Dual Cameras...")
        # Master should stop streaming first to stop pulsing,
        # or Slave stopped first. Usually stopping slave first is safer
        # so it doesn't crash when XVS disappears.
        self.slave_cam.close()
        self.master_cam.close()
