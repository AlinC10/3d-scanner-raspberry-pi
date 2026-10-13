# Dual Camera Architectures (`BaseDualCamera`, `DualCameraRPISync`, `DualCameraXVS`, `DualCamera`)

The 3D scanner utilizes a vertical stereoscopic camera array consisting of two **Arducam IMX477 12.3MP High Quality** cameras mounted on the motorized Z-axis carriage.

The codebase implements an object-oriented architecture built on an abstract base class ([`BaseDualCamera`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/base_dual_camera.py)), providing three swappable, polymorphic implementations:
1. **`DualCameraRPISync`** ([`../dual_camera_rpi_sync.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_rpi_sync.py)) — **[Default & Production]** Continuous pipeline with Raspberry Pi software synchronization (`rpi.sync`), native MJPEG streaming, spectator-driven on-demand encoding, DMA triple-buffering, and Two-Tier Latching for photogrammetry.
2. **`DualCamera`** ([`../dual_camera.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera.py)) — **[Fallback / Development]** Software multi-threading with 0.15s staggered thread dispatch.
3. **`DualCameraXVS`** ([`../dual_camera_xvs.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_xvs.py)) — **[Hardware Pin Sync]** Hardware master/slave cross-camera frame triggering via sensor XVS solder pads.

All three drivers adhere to the `BaseDualCamera` interface contract, allowing [`../scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/scanner.py) to toggle between them dynamically via `Scanner(camera_mode="rpi_sync"|"xvs"|"staggered")` or the `CAMERA_MODE` environment variable.

---

## ⚖️ Architecture Comparison Matrix

| Feature | `DualCameraRPISync` (libcamera `rpi.sync`) | `DualCameraXVS` (Hardware XVS Pin) | `DualCamera` (Software Multi-Threading) |
|---|---|---|---|
| **Primary Status** | **Production Standard** | Alternative Hardware Mode | Legacy / Development Fallback |
| **Sync Accuracy** | **Sub-frame software sync (~0.028 ms / 28 µs)** | Sub-microsecond (< 1 µs) | OS thread dispatch (~5–20 ms) |
| **Physical Wiring** | Two standard Raspberry Pi CSI ribbon cables | Standard CSI ribbons + **soldered XVS jumper wire** | Two standard Raspberry Pi CSI ribbon cables |
| **Kernel / OS Config** | `cma=512M` in `/boot/firmware/cmdline.txt` | Custom `dtoverlay` lines in `/boot/firmware/config.txt` | Standard Raspberry Pi OS setup |
| **Threading Model** | Single hardware ownership + background grab loops | Asymmetric Slave-First dispatch | Symmetric 0.15s staggered thread dispatch |
| **Live Stream Engine** | Native FastAPI MJPEG (`/test/stream/mjpeg`) | H.264 RTSP via MediaMTX | H.264 RTSP via MediaMTX |
| **Streaming Overhead** | **0% CPU when idle** (On-demand spectator tracking) | Continuous background RTSP (~25-35% CPU) | Continuous background RTSP (~25-35% CPU) |
| **Preview Quality Control** | Dedicated `stream_quality = 80` (decoupled from photo) | Shared `quality` parameter | Shared `quality` parameter |
| **Photogrammetry Capture** | **Two-Tier Latching** (Zero Dropped Photos) | One-shot still grab | One-shot still grab |
| **DMA Buffer Management** | **Triple-buffering (`buffer_count=3`)** + instant 3-5 ms release | Standard libcamera buffering | Standard libcamera buffering |
| **Failure Mode** | Best-effort Tier 2 fallback guarantees both photos | If Master fails, Slave deadlocks forever | One failing does not block the other |

---

## 🏛️ Abstract Base Class: `BaseDualCamera`

The abstract base class ([`hardware/base_dual_camera.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/base_dual_camera.py)) enforces interface consistency and eliminates code duplication across all dual camera drivers.

```
                              ┌──────────────────────────────────────────────┐
                              │         BaseDualCamera (abc.ABC)             │
                              │  - Unified Lifecycle (__enter__, close)      │
                              │  - Common Properties (quality, stream_quality)│
                              │  - Shared Transforms (rotate, apply_settings)│
                              │  - Shared VCM Focus Subsystem                │
                              │  - Helper Methods (_resolve_capture_paths)   │
                              └──────────────────────┬───────────────────────┘
                                                     │
         ┌───────────────────────────────────────────┼───────────────────────────────────────────┐
         ▼                                           ▼                                           ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────────┐
│      DualCameraRPISync       │   │        DualCameraXVS         │   │          DualCamera          │
│ - Continuous rpi.sync loops  │   │ - Hardware XVS pad timing    │   │ - 0.15s staggered dispatch   │
│ - Native MJPEG streaming     │   │ - Slave-first sequence       │   │ - RTSP H.264 streaming       │
│ - On-demand spectator track  │   │ - RTSP H.264 streaming       │   │ - Development fallback       │
│ - Two-Tier photo latching    │   └──────────────────────────────┘   └──────────────────────────────┘
└──────────────────────────────┘
```

### 1. Unified Constructor Signature
All subclasses inherit and adhere to standard parameter naming:
```python
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
):
```
* Backward compatibility: Keyword arguments `camera_id_1`, `camera_id_2`, `master_id`, `slave_id`, `rotation_1`, and `rotation_2` are automatically sanitized and mapped to `camera_id1` / `camera_id2`.

### 2. Concrete Shared Functionality (Zero Duplication)
* **Lifecycle & Teardown**: `__enter__()`, `__exit__()`, and idempotent `close()`. Closes the internal `ThreadPoolExecutor(max_workers=2)` with `cancel_futures=True` and safely terminates each camera handle within independent `try/except` blocks to prevent unreleased CSI kernel locks.
* **Backward-Compatibility Aliases**: 
  * `@property master -> self.cam1`
  * `@property slave -> self.cam2`
* **Streaming State**: `@property is_streaming` checks whether any camera is actively streaming.
* **Decoupled Quality Properties**:
  * `@property quality`: Sets still photo JPEG compression quality (default `95`).
  * `@property stream_quality`: Sets live preview stream compression quality (default `80`, bounded between 1 and 100).
* **Motorized VCM Focus Subsystem**:
  * `focus_set(position: int | Tuple[int, int])`: Sets absolute focal positions (0 = infinity, 1023 = macro). Accepts integer for symmetric focus or 2-tuple for independent lens tuning.
  * `focus_step(delta: int | Tuple[int, int])`: Incremental relative adjustments.
  * `focus_reset()`: Relaxes springs back to infinity (`0`).
  * `focus_sweep_autofocus()`: Concurrent contrast-detection autofocus sweep using Laplacian variance scoring.
* **Image Controls & Transformations**:
  * `rotate(direction: str)`: Software rotation dispatched concurrently.
  * `apply_settings(**kwargs)`: Dynamic controls (`brightness`, `contrast`, `exposure_time`, etc.) forwarded concurrently.
  * `lock_auto_features(settle_time: float)`: Settles and locks AE/AWB algorithms.
* **Standardized Path Generators**:
  * `_resolve_capture_paths(prefix, output_dir, meshroom_rig)`: Formats 1-indexed file paths (`0/{prefix}.jpg` and `1/{prefix}.jpg` for Meshroom rigs, or `{prefix}_cam1.jpg` and `{prefix}_cam2.jpg` for flat layouts).

### 3. Abstract Method Contracts
Subclasses must implement:
* `prepare_scan(**kwargs) -> List[dict]`
* `start_stream(bitrate: int) -> List[str]`
* `stop_stream() -> None`
* `capture_photo(output_prefix, output_dir, meshroom_rig, **kwargs) -> List[str]`
* `record_video(output_prefix, duration, **kwargs) -> List[str]`

---

## 🚀 Production Driver: `DualCameraRPISync`

[`DualCameraRPISync`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_rpi_sync.py) is the primary production driver for Raspberry Pi 5. It integrates low-level `libcamera` software synchronization, DMA memory safeguards, and native FastAPI streaming.

### 1. Single Hardware Ownership (The SyncHub Pattern)
Under Linux, Raspberry Pi 5 RP1 CSI-2 DMA channels only permit **one active pipeline handler per CSI port**. Spawning independent `Picamera2` handles across multiple classes causes fatal `Pipeline handler in use` kernel crashes.
* `DualCameraRPISync` acts as the exclusive owner of two `ArducamIMX477` instances (`self.cam1` on CSI-0, `self.cam2` on CSI-1).
* It coordinates background grab loops, sequence locking, and MJPEG publishing while delegating focus and image post-processing to the camera instances.

### 2. Software Shutter Synchronization (`rpi.sync`)
The driver configures physical frame timing limits on both cameras:
* **Camera 1 (CSI-0)**: Configured as `SyncMode = 1` (`SYNC_SERVER`). Emits synchronization timing pulses across IPC sockets and sets `SyncFrames = 30`.
* **Camera 2 (CSI-1)**: Configured as `SyncMode = 2` (`SYNC_CLIENT`). Listens for sync pulses and locks frame boundaries to Server.
* **Startup Ordering**: Client (`cam2`) starts first to bind its listening IPC socket; Server (`cam1`) starts 50 ms later to broadcast sync pulses.
* **SyncReady Await**: Both cameras report `SyncReady: true` in request metadata once phase lock is established.

### 3. Triple-Buffering DMA Management (`buffer_count = 3`)
* High-resolution 12 MP (4056×3040) YUV420 buffers consume ~18.5 MB each.
* Setting `buffer_count = 3` limits camera DMA memory allocation to $3 \times 2 \times 18.5\text{ MB} = 111\text{ MB}$.
* **Fast Buffer Release**: Inside `_grab_worker`, the hardware request buffer is held for only **3–5 ms** before `req.release()` returns it to the kernel DMA pool.
* **Kernel CMA Configuration**: With `cma=512M` in `/boot/firmware/cmdline.txt`, the system maintains >350 MB free CMA memory, eliminating kernel compaction stalls (`kswapd`).

### 4. On-Demand Encoding & Spectator Tracking
The driver tracks active viewers per camera link (`_spectators = [cam1_viewers, cam2_viewers]`):
* **Zero Loading Latency**: Underlying sensor capture loops and hardware synchronization run continuously in the background. Switching between Camera 1 and Camera 2 in the browser is **instantaneous ($\le 100\text{ ms}$)** because the sensor is already warm and aligned.
* **Concurrent Dual Streaming**: Users can open both cameras simultaneously in separate browser tabs (`/test/stream/mjpeg?camera=1` and `/test/stream/mjpeg?camera=2`). Both feeds encode and stream concurrently at 10 fps.
* **0% CPU When Idle**: If no browser tabs are open (0 spectators), JPEG compression completely halts, keeping the Raspberry Pi 5 cool and freeing CPU resources for scanning.

```
Browser Tab (Camera 1) ──► GET /test/stream/mjpeg?camera=1 ──► add_spectator(0) ──┐
                                                                                  ├──► Encode & Stream
Browser Tab (Camera 2) ──► GET /test/stream/mjpeg?camera=2 ──► add_spectator(1) ──┤
                                                                                  │
Browser Disconnect     ──► Stream Generator Terminates     ──► remove_spectator() ┴──► 0 Spectators = 0% CPU
```

### 5. Sub-Frame Modulo Telemetry
Because cameras start asynchronously, their frame counters may differ by 1 period (100 ms at 10 fps). The telemetry method `get_sync_status()` computes shutter offset using modulo arithmetic around `self.period_ns`:
$$\text{aligned\_diff} = \left((\text{raw\_diff} + \text{half\_period}) \pmod{\text{period}}\right) - \text{half\_period}$$
* Accurately measures physical hardware shutter synchronization: **+0.028 ms (28 microseconds)**.
* Exposes live diagnostics: `ready`, `cam1_ready`, `cam2_ready`, `stream_fps`, `offset_ms`, `drops`, `spectators`, `stream_quality`.

### 6. Two-Tier Latching Photogrammetry Policy
During `capture_photo()`, images are retrieved with guaranteed delivery:
* **Tier 1 (Strict Sequence Lock)**: Arms target sequence matching (`target_seq = max(seq1, seq2) + 2`) with a 250 ms deadline window.
* **Tier 2 (Resilient Fallback - Zero Dropped Photos)**: If timing jitter exceeds 250 ms, the driver immediately latches the latest available frame from each camera. **Photos are never dropped and scans never abort.**
* **EXIF Tag Injection**: Latched frames are saved to disk, then passed to `_process_captured_image()` to inject calibrated focal length and unique body serial numbers (`b"cam1"` and `b"cam2"`) into Meshroom stereoscopic folders (`0/` and `1/`).

---

## ⚡ Hardware Pin Driver: `DualCameraXVS`

[`DualCameraXVS`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_xvs.py) synchronizes sensors using physical electrical pulses via the **XVS (eXternal Vertical Sync)** solder pads.

```
┌──────────────────────────────────────┐       ┌──────────────────────────────────────┐
│       IMX477 Camera 0 (Master)       │       │       IMX477 Camera 1 (Slave)        │
│  [ CSI-2 Cable ] ──► Pi CAM Port 0   │       │  [ CSI-2 Cable ] ──► Pi CAM Port 1   │
│  [ XVS Pad ] ────────────────────────┼───────┼─────► [ XVS Pad ]                    │
└──────────────────────────────────────┘       └──────────────────────────────────────┘
                     (Hardware timing pulses emitted from Master)
```

### Hardware Setup
1. Solder a jumper wire between the XVS pads on both Arducam IMX477 boards.
2. Add device tree overlays to `/boot/firmware/config.txt`:
   ```ini
   dtoverlay=imx477,camera0,xvs_source   # Master emits clock
   dtoverlay=imx477,camera1,xvs_sink     # Slave awaits clock
   ```
3. **The Slave-First Dispatch Rule**: The Slave sensor blocks its pipeline until it receives pulses from the Master. `DualCameraXVS` starts the Slave thread first, sleeps 0.1s, and then starts the Master thread.

---

## 🧵 Software Fallback: `DualCamera`

[`DualCamera`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera.py) provides a software-only fallback when hardware sync overlays or sockets are unavailable.
* Dispatches camera operations with an intentional **0.15s timing stagger** (`time.sleep(0.15)`) between Camera 1 and Camera 2 to prevent CSI-2 bus contention and thread scheduling collisions.
* Inherits all shared methods from `BaseDualCamera`.

---

## 🔄 Dynamic Selection in `hardware/scanner.py`

In [`hardware/scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/scanner.py), the `Scanner` class dynamically selects and instantiates the camera driver based on user configuration:

```python
class Scanner:
    def __init__(self, xvs: Optional[bool] = None, camera_mode: Optional[str] = None):
        # Config priority: explicit camera_mode -> legacy xvs flag -> CAMERA_MODE env var -> default "rpi_sync"
        if camera_mode:
            self.camera_mode = camera_mode.lower()
        elif xvs is not None:
            self.camera_mode = "xvs" if xvs else "staggered"
        else:
            self.camera_mode = os.environ.get("CAMERA_MODE", "rpi_sync").lower()

    def setup_and_stream(self, enable_stream=True, camera_mode=None, **kwargs):
        mode = (camera_mode or self.camera_mode).lower()
        if mode == "rpi_sync":
            self.dual_cameras = DualCameraRPISync(**camera_kwargs)
        elif mode == "xvs":
            self.dual_cameras = DualCameraXVS(**camera_kwargs)
        elif mode == "staggered":
            self.dual_cameras = DualCamera(**camera_kwargs)
        else:
            self.dual_cameras = DualCameraRPISync(**camera_kwargs)  # Safe fallback
```

---

## 🌐 FastAPI Streaming Endpoints

The camera subsystem is exposed to the frontend via native FastAPI endpoints in [`router/test.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/router/test.py) and [`router/scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/router/scanner.py):

* **`GET /test/stream/mjpeg?camera=1|2`**: Multipart MJPEG video stream with automatic spectator tracking.
* **`POST /test/stream/active?camera_id=1|2`**: Instant active preview camera switching.
* **`POST /test/stream/quality?quality=80`**: Dynamic runtime adjustment of preview JPEG compression quality (1–100).
* **`GET /test/stream/sync-status`**: Real-time diagnostic telemetry (SyncReady, offset_ms, stream_fps, drops, spectators).
* **`POST /test/camera/snap`**: Captures a synchronized test pair at full 12 MP resolution with EXIF tags.
