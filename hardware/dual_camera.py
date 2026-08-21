import threading
from camera import Camera

class DualCamera:
    """
    Controls two cameras simultaneously using threading to minimize capture delay.
    Perfect for stereo 3D scanning.
    """

    def __init__(self, camera_id0: int=0, camera_id1: int=1):
        print("Initializing Dual Cameras... (This may take a few seconds)")
        self.cam0 = Camera(camera_id=camera_id0)
        self.cam1 = Camera(camera_id=camera_id1)
        print("Both cameras initialized successfully.")

    def take_photos(self, path0="cam0_photo.jpg", path1="cam1_photo.jpg"):
        """Triggers both cameras simultaneously using threads."""
        t0 = threading.Thread(target=self.cam0.take_photo, args=(path0,))
        t1 = threading.Thread(target=self.cam1.take_photo, args=(path1,))

        t0.start()
        t1.start()

        # Wait for both independent captures to finish
        t0.join()
        t1.join()
        print(f"Dual capture complete: {path0} & {path1}")

    def start_dual_video(self, path0="cam0_vid.mp4", path1="cam1_vid.mp4", duration_sec=5):
        """Records video on both cameras simultaneously."""
        t0 = threading.Thread(target=self.cam0.start_video, args=(path0, duration_sec))
        t1 = threading.Thread(target=self.cam1.start_video, args=(path1, duration_sec))

        t0.start()
        t1.start()
        t0.join()
        t1.join()

    def auto_focus(self):
        """Autofocuses both cameras simultaneously."""
        t0 = threading.Thread(target=self.cam0.auto_focus)
        t1 = threading.Thread(target=self.cam1.auto_focus)
        t0.start()
        t1.start()
        t0.join()
        t1.join()

    def fix_focus(self, focus_value=5.0):
        """Sets same manual focus on both cameras."""
        self.cam0.fix_focus(focus_value)
        self.cam1.fix_focus(focus_value)

    def lock_exposure(self):
        """Locks exposure on both cameras"""
        self.cam0.lock_exposure()
        self.cam1.lock_exposure()

    def auto_exposure(self):
        self.cam0.auto_exposure()
        self.cam1.auto_exposure()

    def close(self):
        """Closes both cameras properly."""
        print("Shutting down Dual Cameras...")
        self.cam0.close()
        self.cam1.close()
