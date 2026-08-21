import time
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput

class Camera:
    def __init__(self, camera_id=0):
        """
        Initializes the Arducam IMX477 camera on Raspberry Pi 5.
        camera_id: 0 for CSI port 0, 1 for CSI port 1.
        """
        self.camera_id = camera_id
        # Initialize Picamera2 bound to the specific CSI port
        try:
            self.cam = Picamera2(camera_num=camera_id)
        except TypeError:
            self.cam = Picamera2(camera_id)
        
        # Configure native high-resolution for IMX477 (12MP)
        self.config = self.cam.create_still_configuration(main={"size": (4056, 3040)})
        self.cam.configure(self.config)
        self.cam.start()

    def take_photo(self, output_path="photo.jpg"):
        """Takes a full-resolution JPEG photo."""
        print(f"[Camera {self.camera_id}] Taking photo...")
        self.cam.capture_file(output_path)
        print(f"[Camera {self.camera_id}] Saved to {output_path}")

    def start_video(self, output_path="video.mp4", duration_sec=5):
        """Captures a video for a set duration."""
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

    def auto_focus(self):
        """Triggers libcamera's Autofocus routine."""
        print(f"[Camera {self.camera_id}] Running Autofocus...")
        self.cam.set_controls({'AfMode': 1})      # Auto
        self.cam.set_controls({'AfTrigger': 0})   # Start autofocus cycle
        time.sleep(2)  # Wait a moment for focus to lock
        print(f"[Camera {self.camera_id}] Autofocus cycle complete.")

    def fix_focus(self, focus_value=5.0):
        """Sets a manual focus position."""
        print(f"[Camera {self.camera_id}] Setting fixed focus to {focus_value}")
        self.cam.set_controls({'AfMode': 0})      # Manual
        self.cam.set_controls({'LensPosition': focus_value})

    def lock_exposure(self):
        """Locks the current brightness/exposure settings."""
        print(f"[Camera {self.camera_id}] Locking exposure...")
        self.cam.set_controls({'AeEnable': False})
        self.cam.set_controls({'AwbEnable': False})

    def auto_exposure(self):
        """Re-enables auto exposure and auto white balance."""
        print(f"[Camera {self.camera_id}] Enabling auto exposure...")
        self.cam.set_controls({'AeEnable': True})
        self.cam.set_controls({'AwbEnable': True})

    def close(self):
        """Clean up the camera resources."""
        self.cam.stop()
        self.cam.close()
