import time
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput


class Camera:
    """
    Arducam IMX477 camera controller on Raspberry Pi 5.
    """

    def __init__(self, camera_id: int = 0) -> None:
        """
        Initializes the Arducam IMX477 camera on Raspberry Pi 5.

        :param camera_id: CSI port index (0 for CSI port 0, 1 for CSI port 1).
        :type camera_id: int
        """
        self.camera_id: int = camera_id
        # Initialize Picamera2 bound to the specific CSI port
        try:
            self.cam = Picamera2(camera_num=camera_id)
        except TypeError:
            self.cam = Picamera2(camera_id)

        # Configure native high-resolution for IMX477 (12MP)
        self.config = self.cam.create_still_configuration(main={"size": (4056, 3040)})
        self.cam.configure(self.config)
        self.cam.start()

    def take_photo(self, output_path: str = "photo.jpg") -> None:
        """
        Takes a full-resolution JPEG photo.

        :param output_path: Destination file path for the captured photo.
        :type output_path: str
        """
        print(f"[Camera {self.camera_id}] Taking photo...")
        self.cam.capture_file(output_path)
        print(f"[Camera {self.camera_id}] Saved to {output_path}")

    def start_video(self, output_path: str = "video.mp4", duration_sec: int = 5) -> None:
        """
        Captures a video for a set duration.

        :param output_path: Destination file path for the recorded video.
        :type output_path: str
        :param duration_sec: Recording duration in seconds.
        :type duration_sec: int
        """
        print(f"[Camera {self.camera_id}] Recording video for {duration_sec}s...")
        vid_config = self.cam.create_video_configuration(main={"size": (1920, 1080)})
        self.cam.configure(vid_config)
        self.cam.start()

        # Trigger H264 hardware recording (Picamera2 current API).
        self.cam.start_recording(H264Encoder(), FileOutput(output_path))
        time.sleep(duration_sec)
        self.cam.stop_recording()

        # Revert to still configuration
        self.cam.configure(self.config)
        self.cam.start()
        print(f"[Camera {self.camera_id}] Video saved to {output_path}")

    def auto_focus(self) -> None:
        """
        Triggers libcamera's Autofocus routine.
        """
        print(f"[Camera {self.camera_id}] Running Autofocus...")
        self.cam.set_controls({"AfMode": 1})  # Auto
        self.cam.set_controls({"AfTrigger": 0})  # Start autofocus cycle
        time.sleep(2)  # Wait a moment for focus to lock
        print(f"[Camera {self.camera_id}] Autofocus cycle complete.")

    def fix_focus(self, focus_value: float = 5.0) -> None:
        """
        Sets a manual focus position.

        :param focus_value: Manual focus distance / lens position value.
        :type focus_value: float
        """
        print(f"[Camera {self.camera_id}] Setting fixed focus to {focus_value}")
        self.cam.set_controls({"AfMode": 0})  # Manual
        self.cam.set_controls({"LensPosition": focus_value})

    def lock_exposure(self) -> None:
        """
        Locks the current brightness/exposure settings.
        """
        print(f"[Camera {self.camera_id}] Locking exposure...")
        self.cam.set_controls({"AeEnable": False})
        self.cam.set_controls({"AwbEnable": False})

    def auto_exposure(self) -> None:
        """
        Re-enables auto exposure and auto white balance.
        """
        print(f"[Camera {self.camera_id}] Enabling auto exposure...")
        self.cam.set_controls({"AeEnable": True})
        self.cam.set_controls({"AwbEnable": True})

    def close(self) -> None:
        """
        Clean up the camera resources.
        """
        self.cam.stop()
        self.cam.close()
