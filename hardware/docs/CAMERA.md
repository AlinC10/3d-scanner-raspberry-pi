# Camera Subsystem Overview (`hardware/camera`)

The `hardware/camera` package provides the complete optical imaging and motorized focus pipeline for the 3D scanner. Built on Raspberry Pi's `picamera2` and Linux `libcamera`, it manages individual camera sensor hardware, automated Voice Coil Motor (VCM) lens positioning, live H.264 RTSP video streaming, and stereoscopic capture synchronization.

This document serves as the high-level architecture overview and navigation index for the camera subsystem. For specific low-level component references, see the specialized guides in [`hardware/camera/docs/`](../camera/docs/).

---

## 🗂️ Module Architecture & File Map

```
hardware/camera/
├── camera.py           # ArducamIMX477: Core sensor driver & picamera2 pipeline
├── focuser.py          # Focuser: I2C hardware driver for VCM motorized focus
├── config/             # System constants and shared optical defaults
│   ├── camera_default.py
│   └── vcm.py
└── docs/               # In-depth component guides
    ├── ArducamIMX477.md            # Detailed single-camera API reference
    ├── Focuser.md                  # Low-level VCM I2C DAC register guide
    ├── Focus_Guide.md              # Depth of field, hyperfocal & stacking guide
    ├── DualCamera.md               # Software multi-threaded dual camera guide
    ├── DualCameraXVS.md            # Hardware XVS sync setup & timing guide
    └── Meshroom_Rig_Walkthrough.md # Multi-camera stereoscopic rig guide
```

---

## 🏗️ Core Architectural Features

### 1. Dual-Stream Concurrent Pipeline (`lores` + `main`)
The camera driver leverages `picamera2` multi-stream output capabilities:
* **`lores` Stream** ($1280 \times 720$ / $1920 \times 1080$): Feeds an `H264Encoder` streaming over TCP via FFmpeg directly to MediaMTX (`rtsp://localhost:8554/cam{id}`).
* **`main` Stream** ($4056 \times 3040$): Kept uncompressed and available. When a scan captures a photo, it grabs the full 12.3MP still directly from the `main` stream without interrupting, reconfiguring, or dropping frames on the live RTSP stream.

### 2. Re-entrant Preparation & Runtime Auto-Tuning
In the 4-phase scanning architecture, `prepare_scan()` is called twice:
1. **Top Position**: Starts the sensor and boots the RTSP livestream for the frontend framing view.
2. **In Position (Home + 30mm)**: Recalibrates focus and settles AE/AWB in front of the illuminated object.

By guarding stream initialization with `if not getattr(self.picam2, "started", False):`, the second call bypasses destructive graph re-configuration. The video stream stays alive while the sensor dynamically updates exposure, meters the scene, and locks settings.

### 3. Strict "No Digital Zoom" Policy
Digital zoom or Region of Interest (ROI) cropping alters the effective focal length and shifts the lens's optical principal point $(c_x, c_y)$. This breaks Meshroom's mathematical distortion solver. The driver enforces zero-crop capture; framing adjustments must be made physically via the Z-axis lead screw or turntable distance.

### 4. EXIF Serial Number Injection
To prevent Meshroom's Bundle Adjustment node from averaging out subtle manufacturing differences between the two physical lenses (e.g. slight focal length or optical center variations), `camera.py` injects unique EXIF serial numbers (`cam0` and `cam1`). This causes Meshroom to automatically calibrate them as separate optical intrinsic groups.

---

## 📚 Component Documentation Index

Rather than duplicating low-level specifications, consult the dedicated guides in [`hardware/camera/docs/`](../camera/docs/) and [`hardware/docs/`](../docs/):

| Component / Topic | Primary Guide | Key Topics Covered |
|---|---|---|
| **Single Camera API** | [ArducamIMX477.md](../camera/docs/ArducamIMX477.md) | Constructor, still captures, RTSP streaming, rotation, resolution configs. |
| **VCM Motor & I2C** | [Focuser.md](../camera/docs/Focuser.md) | DW9714 10-bit DAC format, I2C bus auto-detection, smooth ramping, state persistence. |
| **Optics & Focus Guide** | [Focus_Guide.md](../camera/docs/Focus_Guide.md) | Circle of confusion, hyperfocal distance calculations, focus bracketing. |
| **Stereo Dual Camera** | [DUAL_CAMERAS.md](DUAL_CAMERAS.md) | Software multi-threading (`DualCamera`) vs Hardware XVS Sync (`DualCameraXVS`). |
| **Meshroom Rig Setup** | [Meshroom_Rig_Walkthrough.md](../camera/docs/Meshroom_Rig_Walkthrough.md) | Stereo baseline, `/0/` and `/1/` folder segregation, identical timestamp naming. |

---

## ⚙️ Configuration Reference (`config/`)

Hardware constants and operational presets are centralized in [`hardware/camera/config/`](../camera/config/):

### VCM & Motorized Lens Constants (`vcm.py`)
* `VCM_I2C_ADDR = 0x0C`: Standard 7-bit I2C address for DW9714 driver.
* `VCM_I2C_BUS = 10`: Default CSI I2C bus on Raspberry Pi 5 (`i2c-10` for CAM 0, `i2c-11` for CAM 1).
* `VCM_MIN_POS = 0`: Infinity ($\infty$) lens position (relaxed spring).
* `VCM_MAX_POS = 1023`: Extreme macro / close-up position (maximum coil extension).
* `VCM_MOVE_DELAY_S = 0.06`: 60 ms mechanical settling time after movement.

### Capture & Autofocus Defaults (`camera_default.py`)
* `DEFAULT_PHOTO_RESOLUTION = (4056, 3040)`: Full 12.3MP sensor capture resolution.
* `DEFAULT_VIDEO_RESOLUTION = (1920, 1080)`: 1080p full HD stream resolution.
* `DEFAULT_PREVIEW_RESOLUTION = (1280, 720)`: 720p HD live preview resolution.
* `DEFAULT_QUALITY = 95`: High-quality JPEG compression level.
* `AF_STEP = 30`: DAC step size during contrast-detection autofocus sweep.
* `AF_ROI = (0.3, 0.3, 0.4, 0.4)`: Center 40% region of interest for Laplacian variance sharpness scoring.

---

## 🚀 Quick Start Example

```python
from hardware.camera.camera import ArducamIMX477

# Initialize camera 0 (CSI port 0, VCM on i2c-10)
with ArducamIMX477(camera_id=0) as cam:
    # 1. Prepare and start sensor with keep_running=True for live streaming
    cam.prepare_scan(keep_running=True)
    
    # 2. Start RTSP live stream to MediaMTX
    stream_url = cam.start_stream(bitrate=2_000_000)
    print(f"RTSP stream active at: {stream_url}")
    
    # 3. Perform contrast autofocus on object
    sharpest_pos = cam.focus_sweep_autofocus()
    print(f"Optimal focus locked at DAC position: {sharpest_pos}")
    
    # 4. Lock auto exposure and white balance for consistent photogrammetry
    cam.lock_auto_features(settle_time=2.0)
    
    # 5. Capture calibrated 12.3MP photo
    photo_file = cam.capture_photo(output="calibration_shot.jpg")
    print(f"Photo saved: {photo_file}")
```
