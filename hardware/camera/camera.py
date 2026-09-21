import sys
import time
import logging
import piexif
from pathlib import Path
from datetime import datetime
from typing import Optional

from config import *
from focuser import Focuser

# ── Third-party ──────────────────────────────────────────────────────────────
try:
    from picamera2 import Picamera2, Preview
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput
    from libcamera import controls as libcontrols
except ImportError:
    sys.exit(
        "[ERROR] picamera2 not found. Install with:\n"
        "  sudo apt install python3-picamera2"
    )


try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    logging.warning("OpenCV not found — autofocus (sharpness sweep) disabled.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _vcm_bus_for_camera(camera_id: int) -> int:
    """
    Return the I2C bus number for the VCM (Voice Coil Motor) on the given
    camera port.

    On RPi 5 the CSI camera I2C buses are:
        cam0 → i2c-10  (via RP1 i2c@88000)
        cam1 → i2c-11  (via RP1 i2c@80000)

    The VCM shares the same I2C bus as the IMX477 sensor but at address 0x0C.
    It is only reachable while the camera is actively streaming (the bus is
    power-gated), so probing at init time is unreliable.
    """
    bus_map = {0: 10, 1: 11}
    bus = bus_map.get(camera_id)
    if bus is not None:
        log.info("VCM for camera %d → i2c-%d (addr=0x%02X)", camera_id, bus, VCM_I2C_ADDR)
        return bus
    log.warning("Unknown camera_id %d — defaulting VCM to i2c-%d", camera_id, VCM_I2C_BUS)
    return VCM_I2C_BUS


# ─────────────────────────────────────────────────────────────────────────────
# Camera controller
# ─────────────────────────────────────────────────────────────────────────────
class ArducamIMX477:
    """
    High-level interface for the Arducam IMX477 B0272 motorized camera.

    Features:
      - Still capture (JPEG / DNG raw)
      - Video recording (H.264 / MP4)
      - Live preview (Qt or DRM)
      - Manual focus control (VCM steps or absolute position)
      - Software autofocus via Laplacian sharpness sweep (needs OpenCV)
    """

    FULL_RES = (4056, 3040)
    HD_RES   = (1920, 1080)
    SD_RES   = (1280,  720)

    # Valid rotation angles (degrees, counter-clockwise)
    _VALID_ROTATIONS = (0, 90, 180, 270)

    def __init__(
        self,
        camera_id: int = 0,
        quality: int = DEFAULT_QUALITY,
        size: tuple[int, int] | None = None,
        video_size: tuple[int, int] | None = None,
        preview_size: tuple[int, int] | None = None,
        rotation: int = 0,
    ) -> None:
        """
        Initializes the Arducam IMX477 camera on Raspberry Pi 5.

        :param camera_id: CSI port index (0 for CSI port 0, 1 for CSI port 1).
        :type camera_id: int
        :param quality: Default JPEG quality from 0 to 100 (default: 95).
        :type quality: int
        :param size: Still-image resolution (default: 4056×3040).
        :type size: tuple[int, int], optional
        :param video_size: Video recording resolution (default: 1920×1080).
        :type video_size: tuple[int, int], optional
        :param preview_size: Preview resolution (default: 1920×1080).
        :type preview_size: tuple[int, int], optional
        :param rotation: Image rotation in degrees (0, 90, 180, 270).
        :type rotation: int
        """
        self.camera_id: int = camera_id
        self._quality = self._validate_quality(quality)
        self._size = self._validate_size(size or DEFAULT_PHOTO_RESOLUTION)
        self._video_size = self._validate_size(video_size or DEFAULT_VIDEO_RESOLUTION)
        self._preview_size = self._validate_size(preview_size or DEFAULT_PREVIEW_RESOLUTION)
        self._rotation = self._validate_rotation(rotation)
        self._pending_controls = {}
        self._scan_prepared = False

        # Select the correct I2C bus for this camera's VCM
        vcm_bus = _vcm_bus_for_camera(camera_id)
        self.focuser = Focuser(bus=vcm_bus)

        # Initialize Picamera2 bound to the specific CSI port
        self.picam2 = Picamera2(camera_id)

        # Keep backward-compatible alias
        self.camera_num = camera_id

        log.info("ArducamIMX477 ready (camera index %d, quality=%d, size=%s, rotation=%d°)",
                 camera_id, self._quality, self._size, self._rotation)

    # ── Validators ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_quality(quality: int) -> int:
        """Validate and clamp JPEG quality to [0, 100]."""
        if not isinstance(quality, int):
            raise TypeError("quality must be an integer")
        return max(0, min(100, quality))

    @staticmethod
    def _validate_size(size: tuple[int, int]) -> tuple[int, int]:
        """Validate that size is a tuple of two positive integers."""
        if (
            not isinstance(size, tuple)
            or len(size) != 2
            or not all(isinstance(v, int) and v > 0 for v in size)
        ):
            raise ValueError("size must be a tuple of two positive integers")
        return size

    @staticmethod
    def _validate_rotation(rotation: int) -> int:
        """Validate rotation angle (must be 0, 90, 180, or 270)."""
        if not isinstance(rotation, int):
            raise TypeError("rotation must be an integer")
        rotation = rotation % 360
        if rotation not in (0, 90, 180, 270):
            raise ValueError(f"rotation must be 0, 90, 180, or 270 — got {rotation}")
        return rotation

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def quality(self) -> int:
        """Return the default JPEG quality."""
        return self._quality

    @quality.setter
    def quality(self, quality: int) -> None:
        """Set the default JPEG quality (0–100)."""
        self._quality = self._validate_quality(quality)

    @property
    def size(self) -> tuple[int, int]:
        """Return the configured still-image resolution."""
        return self._size

    @size.setter
    def size(self, size: tuple[int, int]) -> None:
        """Set the still-image resolution."""
        self._size = self._validate_size(size)

    @property
    def video_size(self) -> tuple[int, int]:
        """Return the configured video resolution."""
        return self._video_size

    @video_size.setter
    def video_size(self, size: tuple[int, int]) -> None:
        """Set the video recording resolution."""
        self._video_size = self._validate_size(size)

    @property
    def preview_size(self) -> tuple[int, int]:
        """Return the configured preview resolution."""
        return self._preview_size

    @preview_size.setter
    def preview_size(self, size: tuple[int, int]) -> None:
        """Set the preview resolution."""
        self._preview_size = self._validate_size(size)

    @property
    def rotation(self) -> int:
        """Return the current image rotation angle in degrees."""
        return self._rotation

    @rotation.setter
    def rotation(self, rotation: int) -> None:
        """Set the image rotation angle (0, 90, 180, or 270)."""
        self._rotation = self._validate_rotation(rotation)

    # ── Rotation ──────────────────────────────────────────────────────────────

    def rotate(self, direction: str) -> int:
        """
        Rotate the camera image by 90° in the given direction.

        :param direction: One of 'left' (counter-clockwise 90°),
                          'right' (clockwise 90°), or 'flip' (180°).
        :type direction: str
        :returns: The new rotation angle in degrees.
        :rtype: int
        :raises ValueError: If direction is not 'left', 'right', or 'flip'.
        """
        direction = direction.strip().lower()
        if direction == "left":
            self._rotation = (self._rotation + 90) % 360
        elif direction == "right":
            self._rotation = (self._rotation - 90) % 360
        elif direction == "flip":
            self._rotation = (self._rotation + 180) % 360
        else:
            raise ValueError(
                f"direction must be 'left', 'right', or 'flip' — got '{direction}'"
            )
        log.info("Rotation → %d°", self._rotation)
        return self._rotation

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _ts(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Camera configuration ──────────────────────────────────────────────────
    def _configure_still(self, resolution=None, lores_format = "YUV420"):
        preview_resolution = resolution or self._preview_size
        cfg = self.picam2.create_still_configuration(
            # Capture the complete sensor image; requested smaller stills are
            # downscaled after capture so they do not change the field of view.
            main={"size": self.FULL_RES},
            lores={"size": preview_resolution, "format": lores_format},
            raw={},
            display="lores"
        )
        self.picam2.configure(cfg)

    def _configure_video(self, resolution=None, lores_format = "YUV420"):
        resolution = resolution or self._video_size
        cfg = self.picam2.create_video_configuration(
            main={"size": resolution},
            lores={"size": self.SD_RES, "format": lores_format},
            display="lores"
        )
        self.picam2.configure(cfg)

    def _configure_preview(self, resolution=None, lores_format = "YUV420"):
        resolution = resolution or self._preview_size
        cfg = self.picam2.create_preview_configuration(
            main={"size": resolution},
            lores={"size": self.SD_RES, "format": lores_format},
            display="main"
        )
        self.picam2.configure(cfg)

    # ── Camera settings ───────────────────────────────────────────────────────
    def apply_settings(
        self,
        exposure_time=None,
        analogue_gain=None,
        brightness=None,
        contrast=None,
        saturation=None,
        sharpness=None,
        awb_mode=None,
    ):
        """
        Apply image controls to the camera.

        Args:
            exposure_time:  Shutter speed in microseconds (None = auto).
            analogue_gain:  Analogue ISO gain (1.0 – 16.0).
            brightness:     -1.0 to +1.0 (default 0.0).
            contrast:       0.0 to 32.0  (default 1.0).
            saturation:     0.0 to 32.0  (default 1.0).
            sharpness:      0.0 to 16.0  (default 1.0).
            awb_mode:       auto | daylight | cloudy | indoor | fluorescent | tungsten
        """
        ctls = {}

        if exposure_time is not None:
            ctls["ExposureTime"] = int(exposure_time)
            ctls["AeEnable"]     = False
        else:
            ctls["AeEnable"]     = True

        if analogue_gain is not None:
            ctls["AnalogueGain"] = float(analogue_gain)
        if brightness  is not None: ctls["Brightness"]  = float(brightness)
        if contrast    is not None: ctls["Contrast"]    = float(contrast)
        if saturation  is not None: ctls["Saturation"]  = float(saturation)
        if sharpness   is not None: ctls["Sharpness"]   = float(sharpness)

        AWB_MAP = {
            "auto":        libcontrols.AwbModeEnum.Auto,
            "daylight":    libcontrols.AwbModeEnum.Daylight,
            "cloudy":      libcontrols.AwbModeEnum.Cloudy,
            "indoor":      libcontrols.AwbModeEnum.Indoor,
            "fluorescent": libcontrols.AwbModeEnum.Fluorescent,
            "tungsten":    libcontrols.AwbModeEnum.Tungsten,
        }
        if awb_mode is not None:
            awb_mode = awb_mode.lower()
            mode = AWB_MAP.get(awb_mode)
            if mode is not None:
                ctls["AwbMode"] = mode
                ctls["AwbEnable"] = awb_mode == "auto"
            else:
                log.warning("Unknown AWB mode '%s'. Choices: %s", awb_mode, list(AWB_MAP))

        if ctls:
            self._pending_controls.update(ctls)
            if getattr(self.picam2, "started", False):
                self.picam2.set_controls(ctls)
            log.info("Controls applied: %s", list(ctls))

    def lock_auto_features(self, settle_time: float = 2.0) -> dict:
        """Lock the current automatic exposure and white-balance results.

        The camera must already be streaming. AE and AWB are allowed to settle
        first, then their current metadata values are written back with both
        automatic algorithms disabled. Call this before a scan sequence so
        subsequent frames use the same exposure and white balance.

        Returns:
            The controls applied to lock the current image settings.

        Raises:
            RuntimeError: If the camera is not streaming.
            ValueError: If ``settle_time`` is negative.
        """
        if settle_time < 0:
            raise ValueError("settle_time must be non-negative")
        if not getattr(self.picam2, "started", False):
            raise RuntimeError("camera must be started before locking auto features")

        time.sleep(settle_time)
        metadata = self.picam2.capture_metadata()
        controls = {
            "AeEnable": False,
            "AwbEnable": False,
        }

        for name in ("ExposureTime", "AnalogueGain", "ColourGains"):
            if name in metadata:
                controls[name] = metadata[name]

        self.picam2.set_controls(controls)
        self._pending_controls.update(controls)
        log.info(
            "AE/AWB locked: exposure=%s gain=%s colour_gains=%s",
            controls.get("ExposureTime"),
            controls.get("AnalogueGain"),
            controls.get("ColourGains"),
        )
        return controls
    
    

    def prepare_scan(
        self,
        photo_resolution: tuple[int, int] | None = None,
        preview_resolution: tuple[int, int] | None = None,
        quality: int | None = None,
        focus: int | str | None = None,
        rotation: int | None = None,
        show_preview: bool = False,
        settle_time: float = 2.0,
        exposure_time: int | None = None,
        analogue_gain: float | None = None,
        colour_gains: tuple[float, float] | None = None,
        awb_mode: str | None = "auto",
        brightness: float | None = None,
        contrast: float | None = None,
        saturation: float | None = None,
        sharpness: float | None = None,
        autofocus_step: int = AF_STEP,
        autofocus_roi: tuple = AF_ROI,
        keep_running: bool = False
    ) -> dict:
        """Prepare and lock the camera for a repeatable scanning sequence.

        ``focus`` may be ``None`` to keep the saved/current position, an
        integer VCM position, or ``"auto"`` for a software sharpness sweep.
        Manual exposure/gain/colour-gain values disable the corresponding
        automatic feature. Unspecified exposure and white balance are allowed
        to settle and are then locked from live metadata.

        The method starts a non-interactive preview while preparing, then
        stops it. The next :meth:`capture_photo` uses the prepared focus and
        controls without restoring an older focus state.
        """
        if settle_time < 0:
            raise ValueError("settle_time must be non-negative")
        if not isinstance(focus, (type(None), int, str)):
            raise TypeError("focus must be None, an integer, or 'auto'")
        if isinstance(focus, str) and focus.lower() != "auto":
            raise ValueError("focus string must be 'auto'")
        if quality is not None:
            self.quality = quality
        if photo_resolution is not None:
            self.size = photo_resolution
        if preview_resolution is not None:
            self.preview_size = preview_resolution
        if rotation is not None:
            self.rotation = rotation

        preview_resolution = preview_resolution or self._preview_size
        # self._configure_preview(preview_resolution)
        self._configure_still(preview_resolution or self._preview_size)
        if show_preview:
            self.picam2.start_preview(Preview.QT)
        self.picam2.start()
        try:
            if focus is not None:
                if isinstance(focus, str):
                    self.focus_sweep_autofocus(
                        step=autofocus_step, roi=autofocus_roi
                    )
                elif self.focuser.position != focus:
                    self.focus_set(focus)
            elif self.focuser.position:
                log.info("Keeping saved focus position %d", self.focuser.position)

            manual_controls = {}
            if exposure_time is not None:
                manual_controls.update({
                    "ExposureTime": int(exposure_time),
                    "AeEnable": False,
                })
            if analogue_gain is not None:
                manual_controls["AnalogueGain"] = float(analogue_gain)
            if colour_gains is not None:
                manual_controls.update({
                    "ColourGains": tuple(float(value) for value in colour_gains),
                    "AwbEnable": False,
                })
            if awb_mode is not None:
                self.apply_settings(
                    awb_mode=awb_mode,
                    brightness=brightness,
                    contrast=contrast,
                    saturation=saturation,
                    sharpness=sharpness,
                )
            manual_controls.update({
                name: float(value)
                for name, value in (
                    ("Brightness", brightness),
                    ("Contrast", contrast),
                    ("Saturation", saturation),
                    ("Sharpness", sharpness),
                )
                if value is not None
            })
            if manual_controls:
                self._pending_controls.update(manual_controls)
                self.picam2.set_controls(manual_controls)

            if exposure_time is None and colour_gains is None:
                locked = self.lock_auto_features(settle_time=settle_time)
            else:
                if settle_time:
                    time.sleep(settle_time)
                locked = dict(manual_controls)
                self.picam2.set_controls(locked)
                self._pending_controls.update(locked)
        finally:
            if not keep_running:
                self.picam2.stop()
                
        self._scan_prepared = True

        return {
            "photo_resolution": self._size,
            "preview_resolution": self._preview_size,
            "quality": self._quality,
            "focus": self.focuser.position,
            "rotation": self._rotation,
            "controls": locked,
            "show_preview": show_preview,
        }

    # ── Focus (VCM) ───────────────────────────────────────────────────────────
    def focus_set(self, position: int):
        """Set absolute focus position [0=infinity … 1023=macro]."""
        self.focuser.set_position(position)
        log.info("Focus -> %d", position)

    def focus_step(self, delta: int):
        """Step focus by delta (positive=near, negative=far)."""
        self.focuser.step(delta)
        log.info("Focus step %+d -> %d", delta, self.focuser.position)

    def focus_reset(self):
        """Reset focus to infinity (position 0)."""
        self.focuser.reset()

    # def focus_libcamera_auto(self) -> bool:
    #     """
    #     Trigger libcamera hardware autofocus (AfMode=Auto + AfTrigger=Start).
    #     Returns True if the control is available on this camera/driver.
    #     """
    #     try:
    #         self.picam2.set_controls({
    #             "AfMode":    libcontrols.AfModeEnum.Auto,
    #             "AfTrigger": libcontrols.AfTriggerEnum.Start,
    #         })
    #         log.info("libcamera HW autofocus triggered")
    #         return True
    #     except Exception as exc:
    #         log.warning("libcamera AF not available: %s", exc)
    #         return False

    # def focus_libcamera_continuous(self, enable: bool = True):
    #     """Enable or disable libcamera continuous autofocus."""
    #     mode = libcontrols.AfModeEnum.Continuous if enable else libcontrols.AfModeEnum.Manual
    #     try:
    #         self.picam2.set_controls({"AfMode": mode})
    #         log.info("Continuous AF %s", "enabled" if enable else "disabled")
    #     except Exception as exc:
    #         log.warning("Could not set AF mode: %s", exc)

    # ── Software autofocus (Laplacian sharpness sweep) ────────────────────────
    def focus_sweep_autofocus(self, step: int = AF_STEP, roi: tuple = AF_ROI, window_name: Optional[str] = None) -> int:
        """
        Software autofocus: sweeps the VCM through its full range and picks
        the position with the highest Laplacian sharpness within the ROI.

        Args:
            step:  VCM step size between measurements (default 30).
            roi:   Region of interest as (x, y, w, h) fractions (default centre).
            window_name: Optional OpenCV window name to show the sweep live.

        Returns:
            Best focus position found.
        """
        if not OPENCV_AVAILABLE:
            log.error("OpenCV required for sweep autofocus. Install python3-opencv.")
            return self.focuser.position

        total_steps = (VCM_MAX_POS - VCM_MIN_POS) // step + 1
        log.info("Sweep autofocus: %d positions, step=%d", total_steps, step)

        best_pos   = VCM_MIN_POS
        best_score = -1.0

        self.focuser.reset()
        time.sleep(0.3)

        pos = VCM_MIN_POS
        step_num = 0
        try:
            while pos <= VCM_MAX_POS:
                self.focuser.set_position(pos, settle=True)
                # Discard stale frame, then get fresh one
                self.picam2.capture_array("lores")
                frame = self.picam2.capture_array("lores")
                
                # Show live preview if a window name was provided
                if window_name:
                    real_h = frame.shape[0] * 2 // 3
                    yuv = frame[:real_h + real_h//2, :]
                    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                    
                    if self._rotation == 90:
                        bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
                    elif self._rotation == 180:
                        bgr = cv2.rotate(bgr, cv2.ROTATE_180)
                    elif self._rotation == 270:
                        bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)

                    cv2.putText(bgr, f"AF Sweep: {pos:4d}/1023", (10, 30), 
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 165, 255), 2)
                        
                    cv2.imshow(window_name, bgr)
                    cv2.waitKey(1)
                
                score = self._sharpness(frame, roi)
                step_num += 1
                print(
                    f"\r  AF: [{step_num:2d}/{total_steps}] "
                    f"pos={pos:4d}  sharpness={score:8.1f}  best={best_pos:4d}",
                    end="", flush=True,
                )
                if score > best_score:
                    best_score = score
                    best_pos   = pos
                pos += step
        finally:
            print()
            self.focuser.set_position(best_pos)
            log.info("Autofocus done — best pos=%d (score=%.1f)", best_pos, best_score)

        return best_pos

    @staticmethod
    def _sharpness(frame, roi: tuple) -> float:
        """
        Laplacian variance sharpness metric computed on the ROI.

        picamera2 lores YUV420 frames have shape (H*3//2, W):
          rows 0 … H-1        → Y luma plane  (what we want)
          rows H … H*3//2-1   → packed UV chroma (must be discarded)

        Without stripping the UV rows, the ROI calculation bleeds into
        chroma data and all sharpness scores come out nearly identical.
        """
        if frame.ndim == 2:
            # YUV420 planar — keep only the Y plane (first 2/3 of rows)
            real_h = frame.shape[0] * 2 // 3
            gray = frame[:real_h, :]
        else:
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

        h, w = gray.shape
        x0 = int(w * roi[0]);  y0 = int(h * roi[1])
        x1 = x0 + int(w * roi[2]); y1 = y0 + int(h * roi[3])
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            return 0.0
        return float(cv2.Laplacian(crop, cv2.CV_64F).var())

    def _process_captured_image(self, output: str, resolution: tuple[int, int], quality: int):
        """Resizes, rotates, and guarantees correct EXIF metadata for Meshroom."""
        # 1. Load EXIF from the original file captured by picamera2
        try:
            exif_dict = piexif.load(output)
        except Exception:
            # If for some reason there's no EXIF, create a blank dictionary structure
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        # 2. Force the correct Make, Model, and Focal Length for Meshroom
        # Using the exact strings from your cameraSensors.db discovery
        exif_dict['0th'][piexif.ImageIFD.Make] = b"RaspberryPi"
        exif_dict['0th'][piexif.ImageIFD.Model] = b"RP_imx477"
        exif_dict['Exif'][piexif.ExifIFD.FocalLength] = (39, 10) # 3.9mm
        
        # Inject unique Serial Number to force separate intrinsic profiles in Meshroom
        serial = f"cam{self.camera_id}".encode('utf-8')
        exif_dict['Exif'][piexif.ExifIFD.BodySerialNumber] = serial

        # 3. Check if we need to resize or rotate using OpenCV
        needs_cv2 = (resolution != self.FULL_RES) or (self._rotation != 0)
        
        if needs_cv2 and OPENCV_AVAILABLE:
            try:
                img = cv2.imread(output, cv2.IMREAD_UNCHANGED)
                if img is not None:
                    if resolution != self.FULL_RES:
                        img = cv2.resize(img, resolution, interpolation=cv2.INTER_AREA)
                        # Update EXIF dimensions to match the downscaled image
                        exif_dict['0th'][piexif.ImageIFD.ImageWidth] = resolution[0]
                        exif_dict['0th'][piexif.ImageIFD.ImageLength] = resolution[1]
                        exif_dict['Exif'][piexif.ExifIFD.PixelXDimension] = resolution[0]
                        exif_dict['Exif'][piexif.ExifIFD.PixelYDimension] = resolution[1]

                    if self._rotation == 90:
                        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
                    elif self._rotation == 180:
                        img = cv2.rotate(img, cv2.ROTATE_180)
                    elif self._rotation == 270:
                        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)

                    # Overwrite file with OpenCV (This destroys the EXIF data on the hard drive)
                    cv2.imwrite(output, img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            except Exception as exc:
                log.warning("Failed to process captured image with OpenCV: %s", exc)

        # 4. Inject the corrected EXIF data back into the final JPEG file
        try:
            exif_bytes = piexif.dump(exif_dict)
            piexif.insert(exif_bytes, output)
        except Exception as exc:
            log.warning("Failed to write EXIF data: %s", exc)

    # ── Still capture ─────────────────────────────────────────────────────────
    def capture_photo(
        self,
        output: Optional[str] = None,
        resolution=None,
        quality: Optional[int] = None,
        raw: bool = False,
        show_preview: bool = False,
        preview_duration: float = 2.0,
    ) -> str:
        """
        Capture a JPEG still (and optionally a DNG raw file).

        Args:
            output:           Output file path (auto-generated if None).
            resolution:       (width, height) — default uses instance size.
            quality:          JPEG quality 0–100 (default uses instance quality).
            raw:              Also save a DNG raw alongside the JPEG.
            show_preview:     Show a preview before shutter.
            preview_duration: Seconds to display preview.

        Returns:
            Path to the JPEG file.
        """
        jpeg_quality = self._validate_quality(quality) if quality is not None else self._quality
        capture_resolution = resolution or self._size
        output = output or f"photo_{self._ts()}.jpg"    
            
        # check if camera is already running
        already_running = getattr(self.picam2, "started", False)
        
        if not already_running:
            self._configure_still(capture_resolution)
            self.picam2.options["quality"] = jpeg_quality
            self.picam2.start(show_preview=show_preview)
        
        if not self._scan_prepared:
            self.focuser.restore_state()
            
        try:
            if self._pending_controls:
                self.picam2.set_controls(self._pending_controls)
            
            if not already_running:
                time.sleep(max(preview_duration if show_preview else 2.0, 2.0))

            if raw:
                req = self.picam2.capture_request()
                try:
                    req.save("main", output)
                    raw_path = Path(output).with_suffix(".dng").as_posix()
                    req.save_dng(raw_path)
                    log.info("Saved DNG:  %s", raw_path)
                finally:
                    req.release()
            else:
                self.picam2.capture_file(output)

            log.info("Saved JPEG: %s (quality=%d)", output, jpeg_quality)
        finally:
            if not already_running:
                self.picam2.stop()

        self._process_captured_image(output, capture_resolution, jpeg_quality)
        return output

    # ── Video recording ───────────────────────────────────────────────────────
    def record_video(
        self,
        output: Optional[str] = None,
        duration: float = 10.0,
        resolution=None,
        quality: int = 25,
    ) -> str:
        """
        Record H.264 video wrapped in an MP4 container.

        Args:
            output:     Output file path (.mp4).
            duration:   Recording duration in seconds.
            resolution: (width, height) — default HD_RES (1920x1080).
            quality:    H.264 quantiser parameter (10=best … 40=worst).

        Returns:
            Path to the saved video file.
        """
        output = output or f"video_{self._ts()}.mp4"
        self._configure_video(resolution)

        encoder  = H264Encoder(qp=quality)
        file_out = FfmpegOutput(output)

        self.picam2.start_recording(encoder, file_out)
        self.focuser.restore_state()
        log.info("Recording %s for %.1f s …", output, duration)
        time.sleep(duration)
        self.picam2.stop_recording()
        log.info("Saved video: %s", output)
        return output

    # Inside ArducamIMX477 class in hardware/camera/camera.py
    def start_stream(self, rtsp_url: str = "rtsp://localhost:8554/", bitrate: int = 2_000_000):
        """Pushes an H.264 RTSP live stream from 'lores' to MediaMTX."""
        if not getattr(self.picam2, "started", False):
            raise RuntimeError("Camera must be started (e.g. via prepare_scan) before streaming.")

        if getattr(self.picam2, "recording", False):
            log.warning("Camera %d is already streaming/recording.", self.camera_id)
            return

        encoder = H264Encoder(bitrate=bitrate)

        # Handle rotation directly in FFmpeg so Python doesn't waste CPU
        ffmpeg_opts = "-f rtsp -rtsp_transport tcp"

        output = FfmpegOutput(f"{ffmpeg_opts} {rtsp_url}cam{self.camera_id}")
        # Stream from 'lores' so 'main' remains available for full-resolution photo captures!
        self.picam2.start_recording(encoder, output, name="lores")
        log.info("Live stream started on %s", rtsp_url)

        return rtsp_url

    def stop_stream(self):
        """Stops the H.264 RTSP stream."""
        if getattr(self.picam2, "recording", False):
            self.picam2.stop_recording()
            log.info("Live stream stopped for camera %d", self.camera_id)


    # ── Interactive preview ───────────────────────────────────────────────────
    def interactive_preview(self, resolution=None):
        """
        Open a live preview window with single-keystroke focus control.

        Keys (NO Enter needed — press once and it reacts immediately):
          w / s   — focus near / far  (small step ±10)
          e / d   — focus near / far  (large step ±50)
          + / -   — same as w / s
          0-9     — jump to focus zone (0=infinity … 9=macro, maps to 0…900)
          a       — software sweep autofocus
          c       — capture JPEG to current directory
          r       — reset focus to infinity (pos=0)
          i       — print camera properties to terminal
          q / ESC — quit
        """
        if not OPENCV_AVAILABLE:
            log.error("OpenCV is required for the preview. Install python3-opencv.")
            return

        self._configure_preview(resolution)
        
        # Start without the native picamera2 preview window
        self.picam2.start_preview(Preview.NULL)
        self.picam2.start()
        self.focuser.restore_state()

        self._print_preview_help()
        window_name = "Arducam IMX477 Preview"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        
        log.info("Preview running. CLICK ON THE PREVIEW WINDOW to use keyboard shortcuts.")

        try:
            while True:
                # Capture YUV420 lores frame for fast preview
                frame = self.picam2.capture_array("lores")
                
                # Convert YUV420 to BGR for OpenCV display
                # frame shape is (H*1.5, W)
                real_h = frame.shape[0] * 2 // 3
                yuv = frame[:real_h + real_h//2, :]
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                
                # Apply software rotation because Raspberry Pi 5 ISP hardware
                # does not support transpose operations for 90/270 degrees.
                if self._rotation == 90:
                    bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
                elif self._rotation == 180:
                    bgr = cv2.rotate(bgr, cv2.ROTATE_180)
                elif self._rotation == 270:
                    bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)

                # Overlay focus status on the video
                pos = self.focuser.position
                cv2.putText(bgr, f"Focus: {pos:4d}/1023", (10, 30), 
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)

                cv2.imshow(window_name, bgr)
                key = cv2.waitKey(1) & 0xFF

                if key in (27, ord('q')):  # ESC or q
                    break
                elif key in (ord('w'), ord('+'), ord('=')):
                    self.focus_step(+VCM_STEP_SMALL)
                elif key in (ord('s'), ord('-')):
                    self.focus_step(-VCM_STEP_SMALL)
                elif key in (ord('e'), ord('W')):
                    self.focus_step(+VCM_STEP_LARGE)
                elif key in (ord('d'), ord('S')):
                    self.focus_step(-VCM_STEP_LARGE)
                elif key == ord('r'):
                    self.focus_reset()
                elif key == ord('a'):
                    print("\nRunning sweep autofocus …")
                    self.focus_sweep_autofocus(window_name=window_name)
                    self._print_preview_help()
                elif key == ord('c'):
                    fname = f"capture_{self._ts()}.jpg"
                    self.picam2.capture_file(fname)
                    self._process_captured_image(fname, resolution or self._size, self._quality)
                    print(f"\nCaptured -> {fname}")
                elif key == ord('i'):
                    print()
                    self._print_info()
                    self._print_preview_help()
                elif ord('0') <= key <= ord('9'):
                    self.focus_set(int(chr(key)) * 100)
        finally:
            cv2.destroyAllWindows()
            self.picam2.stop()

    def _interactive_preview_linebuf(self, resolution=None):
        """Fallback interactive preview using line-buffered input (requires Enter)."""
        self._configure_preview(resolution)
        self.picam2.start(show_preview=True)
        self.focuser.restore_state()
        log.info("Preview started (line-buffered — press Enter after each command):")
        self._print_preview_help()

        try:
            while True:
                try:
                    key = input("> ").strip()
                except EOFError:
                    break

                if key == "q":
                    break
                elif key == "w":
                    self.focus_step(+VCM_STEP_SMALL)
                elif key == "s":
                    self.focus_step(-VCM_STEP_SMALL)
                elif key == "W":
                    self.focus_step(+VCM_STEP_LARGE)
                elif key == "S":
                    self.focus_step(-VCM_STEP_LARGE)
                elif key == "r":
                    self.focus_reset()
                elif key == "a":
                    self.focus_sweep_autofocus()
                # elif key == "h":
                #     self.focus_libcamera_auto()
                elif key == "c":
                    fname = f"capture_{self._ts()}.jpg"
                    self.picam2.capture_file(fname)
                    log.info("Captured: %s", fname)
                elif key == "i":
                    self._print_info()
                elif key.startswith("f") and key[1:].lstrip("-").isdigit():
                    self.focus_set(int(key[1:]))
                else:
                    log.info("Unknown command '%s'. Press q to quit.", key)
        finally:
            self.picam2.stop()

    def _print_preview_help(self):
        print(
            "\n+-- Arducam IMX477 Interactive Preview ----------------------------+\n"
            "|  w/+  focus near (step +10)    e  focus near (step +50)         |\n"
            "|  s/-  focus far  (step -10)    d  focus far  (step -50)         |\n"
            "|  0-9  jump to zone (x100)      r  reset to infinity             |\n"
            "|  a    sweep autofocus                                           |\n"
            "|  c    capture JPEG             i  camera info   q/ESC quit      |\n"
            "+------------------------------------------------------------------+"
        )

    def _print_status(self):
        bar_len = 30
        pos     = self.focuser.position
        filled  = int(bar_len * pos / VCM_MAX_POS)
        bar     = "#" * filled + "." * (bar_len - filled)
        print(
            f"\r  Focus: [{bar}] {pos:4d}/{VCM_MAX_POS}  (far<----->near) ",
            end="",
            flush=True,
        )

    # ── Info ──────────────────────────────────────────────────────────────────
    def _print_info(self):
        props = self.picam2.camera_properties
        meta  = self.picam2.capture_metadata()
        print("\n=== Camera Properties ===")
        for k, v in sorted(props.items()):
            print(f"  {k}: {v}")
        print("\n=== Current Frame Metadata ===")
        for k, v in sorted(meta.items()):
            print(f"  {k}: {v}")
        print(f"\n  VCM Focus Position : {self.focuser.position} / {VCM_MAX_POS}")
        print()

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def close(self):
        try:
            self.picam2.stop()
        except Exception:
            pass
        self.focuser.close()
        log.info("Camera closed.")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
