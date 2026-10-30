# Autofocus sharpness sweep
AF_STEP          = 30
AF_ROI           = (0.3, 0.3, 0.4, 0.4)   # (x, y, w, h) as frame fractions
# Early stop of the coarse sweep (DualCameraRPISync): a lens stops at the first position whose score is more than
# AF_DROP_FRACTION below its best so far, once that best is at least AF_MIN_RISE above the score at position 0 (so the
# noise of the flat, blurred start of the range cannot stop it before the peak). Without such a rise it sweeps to the end.
AF_DROP_FRACTION = 0.03
AF_MIN_RISE      = 0.20

# Default camera settings
DEFAULT_PHOTO_RESOLUTION = (4056, 3040)  # Full sensor resolution
# The only still sizes of the scanner: both cover the whole field of view of the IMX477 (the second is its 2x2 binned
# mode), so the intrinsics Meshroom estimates stay valid. Any other size would need a crop or a non-native mode.
SUPPORTED_PHOTO_RESOLUTIONS = ((4056, 3040), (2028, 1520))
DEFAULT_VIDEO_RESOLUTION = (1920, 1080)  # 1080p
DEFAULT_PREVIEW_RESOLUTION = (960, 720) # 4:3 720p
# DEFAULT_PREVIEW_RESOLUTION = (1440, 1080) # 4:3 1080p
DEFAULT_QUALITY  = 95          # JPEG quality 0–100

# A blocking libcamera/picamera2 call (frame metadata, next request) that gets no frame within this many seconds
# means the sensor or the pipeline is stalled: it raises CameraTimeoutError instead of waiting forever.
# Override with the CAMERA_CALL_TIMEOUT_S environment variable.
import os as _os
try:
    CAMERA_CALL_TIMEOUT_S = float(_os.environ.get("CAMERA_CALL_TIMEOUT_S", "60"))
except ValueError:
    CAMERA_CALL_TIMEOUT_S = 60.0
