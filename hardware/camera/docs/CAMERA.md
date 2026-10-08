# Arducam IMX477 Camera Driver (`../camera.py`)

The `ArducamIMX477` class in `../camera.py` is the low-level hardware driver for the **Arducam IMX477 (B0272) 12.3MP Motorized Focus Camera**, built on top of the Raspberry Pi `picamera2` and `libcamera` subsystem.

It powers individual camera instances across the dual-camera rig, managing the dual-stream pipeline (concurrent H.264 RTSP streaming alongside full-resolution still captures), motorized Voice Coil Motor (VCM) focus, runtime auto-exposure/white-balance locking, and DNG raw asset acquisition.

---

## 📷 Hardware Specifications

| Parameter | Specification | Description |
|---|---|---|
| **Sensor** | Sony IMX477R | 12.33 Megapixels, 1/2.3" Diagonal (7.564 mm) |
| **Pixel Size** | $1.55\ \mu\text{m} \times 1.55\ \mu\text{m}$ | High low-light sensitivity and dynamic range |
| **Max Resolution** | $4056 \times 3040$ pixels | Full unbinned 12.3MP still image capture |
| **Lens Mount & Focus** | Motorized VCM Focus | I2C controlled Voice Coil Motor (`0` = $\infty$, `1023` = macro) |
| **CSI Bus Mapping** | Dual CSI Ports | `CSI 0` $\to$ VCM on `i2c-10`, `CSI 1` $\to$ VCM on `i2c-11` |

---

## 🏗️ Architectural Design Patterns

### 1. Dual-Stream Concurrent Pipeline (`lores` vs `main`)
To allow the live RTSP stream to run continuously without bottlenecking or freezing during photo captures, the driver configures two separate hardware streams inside `picamera2`:
* **`lores` Stream** ($960 \times 720$ native 4:3 @ 24 FPS): Dedicated to the H.264 video encoder (`start_stream()`), streaming over TCP via FFmpeg to MediaMTX with a 4 Mbps bitrate. It matches the full sensor aspect ratio without cropping while keeping CPU usage around ~15–20% on Raspberry Pi 5.
* **`main` Stream** ($4056 \times 3040$): Kept free and uncompressed. When `capture_photo()` fires, it captures a full 12.3MP still directly from the `main` stream without interrupting or dropping frames on the `lores` RTSP feed.

```
                  ┌───────────────────────────────┐
                  │      Sony IMX477 Sensor       │
                  └───────────────┬───────────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  │      picamera2 Pipeline       │
                  └───────┬───────────────┬───────┘
                          │               │
            lores Stream  │               │  main Stream
         (960x720 H.264)  │               │  (4056x3040 Raw/JPEG)
                          ▼               ▼
                  ┌───────────────┐ ┌───────────────┐
                  │ H264Encoder   │ │ High-Res JPEG │
                  │ + Ffmpeg RTSP │ │ or DNG Raw    │
                  └───────────────┘ └───────────────┘
```

---

### 2. Re-entrant Preparation & Runtime Auto-Tuning
In earlier implementations, calling `prepare_scan()` while an RTSP stream was active would attempt to reconstruct the sensor stream graph via `_configure_still()`, triggering a fatal `RuntimeError: cannot configure streams while recording`.

The driver features a **re-entrancy guard**:
```python
if not getattr(self.picam2, "started", False):
    self._configure_still(preview_resolution or self._preview_size)
    self.picam2.start()
```
* **First Call (Stream Launch)**: Configures the streams, starts `picamera2`, and boots the video encoder.
* **Second Call (Home Position)**: Detects that `picam2.started` is already `True`. It bypasses stream reconstruction completely, leaving the video feed alive, and only updates camera controls, settles AE/AWB on the target object, and locks the settings.

---

### 3. Strict "No Digital Zoom" Rule
In photogrammetry, digital zooming or Region of Interest (ROI) cropping shifts the camera's optical principal point $(c_x, c_y)$ and distorts the focal length $f$. 
* The driver **strictly forbids digital zoom**.
* Framing adjustments must be made physically by repositioning the camera carriage on the Z-axis or adjusting distance to the turntable.

### 4. Photogrammetry Sensor Defaults
During `prepare_scan()`, the driver automatically injects two optimized `libcamera` controls:
* **`AeMeteringMode: CentreWeighted`**: Concentrates auto-exposure metering on the physical object resting in the center of the turntable, preventing surrounding dark backgrounds from overexposing and clipping object highlights.
* **`NoiseReductionMode: Off`**: Disables spatial smoothing and edge-blurring algorithms to deliver raw, un-softened sensor pixels to Meshroom for dense SIFT feature extraction.

---

## 🎯 Focus & VCM Subsystem (`../focuser.py`)

The lens focus is driven by a Voice Coil Motor (VCM) controlled over I2C. The camera driver auto-detects the correct I2C bus based on the camera port (`camera_id=0` $\to$ `/dev/i2c-10`, `camera_id=1` $\to$ `/dev/i2c-11`). (See [FOCUSER.md](FOCUSER.md) for full protocol and VCM kinematics documentation).

### Focus Operations
1. **Absolute Positioning (`focus_set`)**: Sets the DAC value from `0` (infinity) to `1023` (macro).
2. **Incremental Stepping (`focus_step`)**: Steps the lens by $\pm\Delta$ DAC ticks.
3. **Reset (`focus_reset`)**: Resets the lens to infinity (`0`).
4. **Contrast-Detection Sweep Autofocus (`focus_sweep_autofocus`)**:
   - Sweeps the VCM across its operating range in steps of `autofocus_step` (default: 30).
   - Extracts a center Region of Interest (`roi`, default: 40% center box).
   - Computes image sharpness using the **Laplacian Variance**:
     $$\text{Score} = \text{Var}(\nabla^2 I_{\text{ROI}})$$
   - Identifies the peak sharpness score and drives the VCM directly back to that optimal position.

---

## 🔒 Optical Consistency & AE/AWB Locking

For 3D photogrammetry with Meshroom, lighting must not flicker or shift color between photos:

### `lock_auto_features(settle_time: float = 2.0, lock_ae: bool = True, lock_awb: bool = True) -> dict`
1. Waits `settle_time` seconds with scanner LEDs illuminated for the automatic metering algorithms to converge on the object.
2. Captures live frame metadata: `ExposureTime`, `AnalogueGain`, and `ColourGains` (red/blue balance).
3. Selectively locks Auto Exposure (`AeEnable=False`) and/or Auto White Balance (`AwbEnable=False`), writing the live metadata back to the sensor.
4. Freezes exposure and white balance for all subsequent 360° captures.

*Note: The `prepare_scan` method intelligently orchestrates this. If you supply a manual `exposure_time`, it correctly skips locking AE while still locking AWB, ensuring a manual shutter speed does not cause color drift.*

---

## 📚 Class API Reference: `ArducamIMX477`

### Constructor
```python
ArducamIMX477(
    camera_id: int = 0,
    quality: int = 95,
    size: Optional[Tuple[int, int]] = (4056, 3040),
    video_size: Optional[Tuple[int, int]] = (1920, 1080),
    preview_size: Optional[Tuple[int, int]] = (960, 720),
    rotation: int = 0
)
```

### Core Methods

#### `prepare_scan(photo_resolution=None, quality=None, focus=None, settle_time=2.0, exposure_time=None, awb_mode="auto", keep_running=False, ...) -> dict`
Prepares, calibrates, and locks the camera for scanning. Automatically injects Photogrammetry Defaults (`CentreWeighted` AE + `NoiseReductionMode.Off`). Supports `focus="auto"` for sweep autofocus or an integer position.

#### `start_stream(rtsp_url="rtsp://localhost:8554/", bitrate=4_000_000) -> str`
Starts pushing an H.264 video stream ($960 \times 720$ native 4:3 @ 24 FPS) from the `lores` stream to MediaMTX via RTSP over TCP.
* **Returns**: Full RTSP stream URL (e.g. `rtsp://localhost:8554/cam0`).

#### `stop_stream() -> None`
Stops the live RTSP stream recording.

#### `capture_photo(output=None, resolution=None, quality=None, raw=False, show_preview=False) -> str`
Captures a high-resolution still image from the `main` stream.
* If `raw=True`, captures a full 12-bit RAW `.dng` file alongside the `.jpg`.
* Applies single-pass software rotation via OpenCV if required.
* **Returns**: Path to the saved JPEG file.

#### `record_video(output=None, duration=10.0, resolution=None, quality=25) -> str`
Records an MP4 video clip locally.

#### `apply_settings(exposure_time=None, analogue_gain=None, awb_mode=None, brightness=None, contrast=None, saturation=None, sharpness=None) -> None`
Applies runtime image adjustments without restarting the stream.

#### `lock_auto_features(settle_time=2.0, lock_ae=True, lock_awb=True) -> dict`
Extracts converged exposure and color balance metadata and locks the specified auto features.

#### `focus_set(position: int) -> None`
Sets lens focus position (`0`–`1023`).

#### `focus_sweep_autofocus(step=30, roi=(0.3, 0.3, 0.4, 0.4)) -> int`
Runs contrast-detection sweep autofocus on the specified ROI and returns the winning position.

#### `close() -> None`
Stops `picamera2` and safely terminates I2C VCM connection. Also supported via context manager (`with ArducamIMX477(...) as cam:`).

---

## 💻 Python Usage Examples

### 1. Standalone Live RTSP Streaming
```python
from hardware.camera.camera import ArducamIMX477

with ArducamIMX477(camera_id=0, preview_size=(1280, 720)) as cam:
    # Prepare camera with keep_running=True
    cam.prepare_scan(keep_running=True)
    
    # Launch RTSP stream to MediaMTX
    stream_url = cam.start_stream(bitrate=2_000_000)
    print(f"Viewing stream at: {stream_url}")
    
    input("Press Enter to stop stream...")
    cam.stop_stream()
```

### 2. High-Resolution Capture with Locked AE/AWB
```python
from hardware.camera.camera import ArducamIMX477

with ArducamIMX477(camera_id=0) as cam:
    # 1. Sweep autofocus on object and lock exposure
    cam.prepare_scan(focus="auto", settle_time=2.0, keep_running=True)
    
    # 2. Capture calibrated 12MP JPEG
    photo_path = cam.capture_photo(output="test_object.jpg", quality=95)
    print(f"Saved: {photo_path}")
```
