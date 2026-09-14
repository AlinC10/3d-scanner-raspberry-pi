# Arducam IMX477 3D Scanner Project Documentation

Welcome to the documentation for the dual-camera 3D scanner project based on the **Raspberry Pi 5 (8GB)** and **Arducam B0272 12MP IMX477 Motorized Focus** cameras.

## Project Overview

The brain of the hardware is a Raspberry Pi 5 that orchestrates stepper motors and dual synchronized cameras for 3D scanning. The cameras are mounted on a horizontal bracket at a 30° to 45° angle to each other. Future iterations will utilize hardware synchronization via XVS (Master-Slave configuration) to ensure simultaneous captures. 

The objectives of the codebase are to provide a well-structured interface to:
* Auto-select the VCM (Voice Coil Motor) driver I2C bus upon instantiation.
* Manage properties for photo resolution, JPEG quality, video resolution, preview resolution, and image rotation.
* Provide interactive previews and fixed-focus photo capturing.
* Handle auto-exposure, white balancing, and settings locking.
* Rotate camera images by 90-degree increments.
* Capture high-quality JPEG images (default quality 95).

## Directory Structure & Modules

The documentation is split by classes to maintain modularity:
* [ArducamIMX477 Camera Class](ArducamIMX477.md) - The high-level interface for a single camera.
* [DualCamera Class](DualCamera.md) - The multi-threaded interface for controlling two cameras concurrently (software sync).
* [DualCameraXVS Class](DualCameraXVS.md) - The specialized interface for true hardware synchronization (Master-Slave) via the XVS cable.
* [Focuser Class](Focuser.md) - The hardware driver interface for the I2C motorized focus (VCM).
* [Focus Calibration & Stacking Guide](Focus_Guide.md) - A guide on calculating depth of field, calibrating the VCM, and handling multiple focal planes in photogrammetry.

### Common Configuration (`config/`)

The project uses shared configurations located in the `config/` directory. These define system-wide defaults and hardware constants:

**`config/vcm.py`** (Voice Coil Motor Constants)
* `VCM_I2C_BUS = 10` - Default CSI camera I2C bus on RPi 5.
* `VCM_I2C_ADDR = 0x0C` - I2C address for the Arducam B0272 VCM (DW9714-compatible).
* `VCM_MIN_POS = 0` - Focus position for infinity / far.
* `VCM_MAX_POS = 1023` - Focus position for macro / near.
* `VCM_STEP_SMALL = 10`, `VCM_STEP_LARGE = 50` - Manual stepping increments.
* `VCM_MOVE_DELAY_S = 0.06` - Mechanical settling time (60ms) for movements.

**`config/camera_default.py`** (Camera Defaults)
* `AF_STEP = 30` - VCM step size during auto-focus sweep.
* `AF_ROI = (0.3, 0.3, 0.4, 0.4)` - Central region of interest for auto-focus sharpness calculations.
* `DEFAULT_PHOTO_RESOLUTION = (4056, 3040)` - 12.3MP full sensor resolution.
* `DEFAULT_VIDEO_RESOLUTION = (1920, 1080)` - 1080p video.
* `DEFAULT_PREVIEW_RESOLUTION = (1920, 1080)` - 1080p preview.
* `DEFAULT_QUALITY = 95` - High-quality JPEG setting.

## Best Practices & Guidelines for Scanning (Meshroom)

To ensure the best photogrammetry results in Meshroom, the following rules are strictly applied:

1. **Lock Settings Before Scanning**: Auto Exposure (AE) and Auto White Balance (AWB) must be allowed to settle and then explicitly locked. Meshroom fails if lighting or focus changes between photos.
2. **No Digital Zoom**: The Arducam B0272 has a fixed focal length. Never apply digital zoom or Region of Interest (ROI) cropping via `picamera2`. Cropping ruins the Principal Point and the optical distortion model required by Meshroom.
3. **High-Quality JPEGs**: Always use quality 95-100 instead of PNG or RAW. High-quality compression is unnoticeable to Meshroom algorithms and significantly reduces dataset sizes (e.g., from 2GB down to ~400MB for 80 images).
4. **Resolution Downscaling (Optional)**: For 2-3x faster photogrammetry processing, captures can be optically scaled down to ~6MP.
5. **Multi-Camera Rig Support**: The `DualCamera` classes default to `meshroom_rig=True`, which saves synchronized images into `0/` and `1/` subdirectories with identical filenames. Meshroom uses this structure to recognize the cameras as a rigid stereo pair, dramatically improving tracking stability and reducing compute time.
6. **Intrinsic Separation via EXIF**: The `ArducamIMX477` class automatically injects a fake `SerialNumber` (`cam0` and `cam1`) into the EXIF data. This tricks Meshroom's Bundle Adjustment into splitting the cameras into separate intrinsic optical groups. This is critical for preventing Meshroom from averaging out minor focus differences (focus breathing) between the two physical lenses.
7. **RAW (DNG) captures**: If capturing RAW (DNG) images alongside JPEGs, note that software rotation is NOT applied to the DNGs, only to the JPEGs. DNG files will remain in their original hardware orientation.
