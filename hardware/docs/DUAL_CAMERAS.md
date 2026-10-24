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
| **Sync Accuracy** | **Sub-frame software sync** (live value: `offset_ms` in `get_sync_status()`) | Sub-microsecond (< 1 µs) | OS thread dispatch (~5–20 ms) |
| **Physical Wiring** | Two standard Raspberry Pi CSI ribbon cables | Standard CSI ribbons + **soldered XVS jumper wire** | Two standard Raspberry Pi CSI ribbon cables |
| **Kernel / OS Config** | `cma=512M` in `/boot/firmware/cmdline.txt` | Custom `dtoverlay` lines in `/boot/firmware/config.txt` | Standard Raspberry Pi OS setup |
| **Threading Model** | Single hardware ownership + background grab loops | Asymmetric Slave-First dispatch | Symmetric 0.15s staggered thread dispatch |
| **Live Stream Engine** | Native FastAPI MJPEG (`/scanner/stream/mjpeg`, also under `/test/stream/mjpeg`) | None (`supports_preview_stream = False`; `start_stream` is the single-camera H.264/RTSP path, which needs an H.264 encoder the Pi 5 does not have) | Same as XVS |
| **Streaming Overhead** | **Encodes only watched cameras** (spectator tracking; with no viewer at all the active camera is still encoded) | No preview | No preview |
| **Preview Quality Control** | Dedicated `stream_quality = 80` (decoupled from photo) | Stored, unused (no preview) | Stored, unused (no preview) |
| **Photogrammetry Capture** | **Two-Tier Latching** (Zero Dropped Photos) | One-shot still grab | One-shot still grab |
| **DMA Buffer Management** | **Triple-buffering (`buffer_count=3`)** + instant 3-5 ms release | Standard libcamera buffering | Standard libcamera buffering |
| **Failure Mode** | Tier 2 fallback latches the newest frames; if a frame or file is still missing `capture_photo` raises `CaptureError` | If Master fails, Slave deadlocks forever | One failing does not block the other |

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
│ - Native MJPEG streaming     │   │ - Slave-first sequence       │   │ - No live preview            │
│ - On-demand spectator track  │   │ - No live preview            │   │ - Development fallback       │
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
    stream_size: Optional[Tuple[int, int]] = None,   # live preview size, default (1024, 768)
    stream_quality: int = 80,                        # live preview JPEG quality (1-100)
):
```
* `DualCameraRPISync` adds its own sync parameters (`fps`, `sync_frames`, `buffer_count`, `ready_timeout`, `auto_start`); `DualCamera` and `DualCameraXVS` accept `stream_size` / `stream_quality` too (they only store them).
* Backward compatibility: Keyword arguments `camera_id_1`, `camera_id_2`, `master_id`, `slave_id`, `rotation_1`, and `rotation_2` are automatically sanitized and mapped to `camera_id1` / `camera_id2`.

### 2. The interface rule and the capability flags
`BaseDualCamera` is **the** interface of `scanner.dual_cameras`: the scanner and the routers use only members declared in it (no `hasattr(...)` and no `isinstance(..., DualCameraRPISync)`). A member that only some drivers really implement still exists in the base with a safe default, and the driver says what it supports through two class attributes:

| Capability | Meaning | `DualCameraRPISync` | `DualCamera` / `DualCameraXVS` |
|---|---|---|---|
| `supports_preview_stream` | live MJPEG preview: `wait_jpeg()`, spectators, `active_camera` switching | `True` | `False` (the routers answer 400 for the switch and send an empty stream) |
| `supports_lock_auto` | `prepare_scan(lock_auto=True)` freezes AE/AWB after `SyncReady` and returns `locked`/`ready` per camera | `True` | `False`: they always lock AE/AWB inside `prepare_scan` and ignore `lock_auto` |
| `supported_photo_sizes` | the still sizes the driver can capture without cropping the field of view (`None` = no restriction); a driver that sets it raises `ValueError` for any other `size` in the base constructor | `((4056, 3040), (2028, 1520))` | `None` (not restricted by the driver; the API accepts only the two sizes above, see "Still sizes") |

`prepare_scan` has two interface arguments besides the camera settings: `lock_auto` and `copy_colour_gains` (default `False`: after the AE/AWB lock cam2 keeps the white-balance gains of its own AWB, exposure time and analogue gain are still copied from cam1). Every driver accepts both; `DualCamera` and `DualCameraXVS` ignore them (they always copy cam1's values, colour gains included, see `PROBLEME_RAMASE.md`).

**Still sizes.** The scanner accepts only `4056x3040` and `2028x1520` (`SUPPORTED_PHOTO_RESOLUTIONS` in `camera_defaults.py`, `Literal` in `schemas/scanner.py`). `DualCameraRPISync` configures the sensor directly at the requested size: `2028x1520` is the 2x2 binned mode of the IMX477, which has the same field of view as the full readout (no crop, 4x fewer pixels, up to 40 fps instead of 10), so the photo is not captured at full size and shrunk afterwards.

When a driver gains a feature that the scanner or a router uses, declare it in `base_dual_camera.py` first (default / `pass` in the base, capability flag if behaviour differs) and note what remains to be ported in `../../plans/FUTURE_WORK.md`. `tests/test_base_dual_camera_interface.py` checks that every driver has every interface member.

### 3. Concrete Shared Functionality (Zero Duplication)
* **Lifecycle & Teardown**: `__enter__()`, `__exit__()`, and idempotent `close()`. Closes the internal `ThreadPoolExecutor(max_workers=2)` with `cancel_futures=True` and safely terminates each camera handle within independent `try/except` blocks to prevent unreleased CSI kernel locks.
* **Backward-Compatibility Aliases**: 
  * `@property master -> self.cam1`
  * `@property slave -> self.cam2`
* **Streaming State**: `@property is_streaming` checks whether any camera is actively streaming.
* **`CaptureError(RuntimeError)`**: raised by a driver's `capture_photo` when a frame/file is missing or the driver is closed (see `DualCameraRPISync`).
* **`last_capture_info`**: `None` until the first successful capture; afterwards `{"paths": [p1, p2], "cameras": [{...}, {...}]}` (see below). Only `DualCameraRPISync` fills it.
* **Decoupled Quality Properties**:
  * `@property quality`: Sets still photo JPEG compression quality (default `95`).
  * `@property stream_quality`: Sets live preview stream compression quality (default `80`, bounded between 1 and 100). It never changes the quality of the scan photos, on any driver.
* **Live-preview state** (shared by all drivers, fed only by those with `supports_preview_stream`): `stream_size`, `active_camera` (1-indexed, 1 or 2) and `active` (0-indexed) with validation, `add_spectator(idx)` / `remove_spectator(idx)` / `has_spectators(idx=None)` (idx 0/1 or 1/2), `wait_jpeg(camera_idx, last_id, timeout)` (default: returns `(last_id, None)` at once) and `get_sync_status()` (default: `{"ready": True, "is_streaming": ..., "mode": <class name>}`).
* **`autofocus_positions`**: result of the last autofocus sweep (one position per lens), `None` until one ran.
* **Motorized VCM Focus Subsystem**:
  * `focus_set(position: int | Tuple[int, int])`: Sets absolute focal positions (0 = infinity, 1023 = macro). Accepts integer for symmetric focus or 2-tuple for independent lens tuning.
  * `focus_step(delta: int | Tuple[int, int])`: Incremental relative adjustments.
  * `focus_reset()`: Relaxes springs back to infinity (`0`).
  * `focus_sweep_autofocus()`: Concurrent contrast-detection autofocus sweep using Laplacian variance scoring.
* **Image Controls & Transformations**:
  * `rotate(direction: str)`: Software rotation dispatched concurrently.
  * `apply_settings(**kwargs)`: Dynamic controls (`brightness`, `contrast`, `exposure_time`, etc.) forwarded concurrently.
  * `lock_auto_features(settle_time: float, copy_colour_gains: bool = True)`: Settles and locks AE/AWB on both cameras, then copies cam1's `ExposureTime` / `AnalogueGain` to cam2 (AE/AWB disabled on both). cam1's `ColourGains` are copied too only with `copy_colour_gains=True` (the base default); `DualCameraRPISync.prepare_scan` passes `False` unless the request asks otherwise, so cam2 keeps the gains of its own AWB.
* **Standardized Path Generators**:
  * `_resolve_capture_paths(prefix, output_dir, meshroom_rig)`: Formats 1-indexed file paths (`0/{prefix}.jpg` and `1/{prefix}.jpg` for Meshroom rigs, or `{prefix}_cam1.jpg` and `{prefix}_cam2.jpg` for flat layouts).

### 4. Abstract Method Contracts
Subclasses must implement:
* `prepare_scan(*, lock_auto: bool = False, copy_colour_gains: bool = False, **kwargs) -> List[dict]` (`focus` kwarg: `"auto"` = sweep, an int = both lenses, a pair = (cam1, cam2))
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
* **Concurrent Dual Streaming**: Users can open both cameras simultaneously in separate browser tabs (`/scanner/stream/mjpeg?camera=1` and `/scanner/stream/mjpeg?camera=2`). Both feeds encode and stream concurrently at 10 fps.
* **Encoding follows viewers**: JPEG compression runs only for cameras with a connected browser tab. With no viewer at all, the *active* camera is still encoded as a fallback (so the first viewer gets a fresh frame); the preview encode is a small lores (1024x768) frame and does not touch the full-resolution stills.

```
Browser Tab (Camera 1) ──► GET /scanner/stream/mjpeg?camera=1 ──► add_spectator(0) ──┐
                                                                                  ├──► Encode & Stream
Browser Tab (Camera 2) ──► GET /scanner/stream/mjpeg?camera=2 ──► add_spectator(1) ──┤
                                                                                  │
Browser Disconnect     ──► Stream Generator Terminates     ──► remove_spectator() ┴──► 0 Spectators = active-camera fallback only
```

### 5. Sub-Frame Modulo Telemetry
Because cameras start asynchronously, their frame counters may differ by 1 period (100 ms at 10 fps). The telemetry method `get_sync_status()` computes shutter offset using modulo arithmetic around `self.period_ns`:
$$\text{aligned\_diff} = \left((\text{raw\_diff} + \text{half\_period}) \pmod{\text{period}}\right) - \text{half\_period}$$
* Measures the shutter phase between the cameras (reported as `offset_ms`; confirm the real value on the rig).
* Exposes live diagnostics: `ready`, `cam1_ready`, `cam2_ready`, `stream_fps`, `offset_ms`, `drops`, `spectators`, `stream_quality`.

### 6. Two-Tier Latching Photogrammetry Policy
During `capture_photo()`, images are retrieved with guaranteed delivery:
* **Tier 1 (Strict Sequence Lock)**: Arms target sequence matching (`target_seq = max(seq1, seq2) + 2`) with a 250 ms deadline window.
* **Tier 2 (Resilient Fallback)**: If timing jitter exceeds 250 ms, the driver immediately latches the latest available frame from each camera (2 s deadline).
* **No silent bad images**: in real hardware mode `capture_photo` raises **`CaptureError`** when a frame for either camera never arrived, when an output file is missing or 0 bytes after encoding/post-processing, or when the driver is closed. A pair is therefore either two real JPEGs or an exception; the caller must treat the exception as a failed shot. Stale files at the target path are removed before encoding so they cannot hide a failed encode. Stub mode (no `picamera2`, development) keeps writing placeholder files and never raises.
* **Stride crop**: the ISP pads YUV420 rows to the buffer stride, so the decoded array can be wider than the configured width. The still path crops to the configured main width (`bgr[:, :w]`) before JPEG encoding; this is a no-op when stride equals width.
* **EXIF Tag Injection**: Latched frames are saved to disk, then passed to `_process_captured_image()` to inject calibrated focal length, unique body serial numbers (`b"cam1"` and `b"cam2"`) and a per-image `ImageUniqueID` (`cam<camera_id>_<file name without extension>`, e.g. `cam0_f_1000` for `rig/0/f_1000.jpg`) into Meshroom stereoscopic folders (`0/` and `1/`).
* **`last_capture_info`** (after every successful capture; `None` before the first):
  ```python
  {"paths": [p1, p2],
   "cameras": [{"exposure_time": int|None,      # microseconds, from request metadata
                "analogue_gain": float|None,
                "colour_gains": [r, b]|None,
                "sensor_timestamp": int|None,   # ns
                "width": int|None, "height": int|None},  # final image size
               {...}]}
  ```
  Values are taken from the request metadata of the latched frames, so they show the exposure each photo really used (the Scanner writes them to its manifest).

### 7. Exposure & White-Balance Lock (`prepare_scan(lock_auto=True)`)
`prepare_scan(..., lock_auto: bool = False)`:
* `lock_auto=False` (default, also the first/cold call): AE/AWB keep running; the returned dicts have `locked: False`.
* `lock_auto=True`: after both cameras report `SyncReady`, AE/AWB are re-armed (a second scan does not inherit the previous scan's values), settle for `settle_time` (kwarg, default 2.0 s), then `BaseDualCamera.lock_auto_features()` locks each camera on its own result (`AeEnable=False` / `AwbEnable=False`) and copies cam1's `ExposureTime` and `AnalogueGain` to cam2. cam1's `ColourGains` are copied as well only with the `copy_colour_gains=True` keyword of `prepare_scan` (default `False`, request field `camera_config.copy_colour_gains`): the two sensors do not answer alike to the same gains, and copying them gave cam2 a bluish cast. Manually supplied `exposure_time`/`analogue_gain`/`colour_gains` are not re-armed. Call it only when the lighting is stable.
* The returned per-camera dicts carry `locked` plus the locked `exposure_time`, `analogue_gain`, `colour_gains` (`None` when not locked). A failure is logged and reported as `locked: False`; it does not raise. If the cameras are not `SyncReady` the lock is skipped.
* `lock_auto` is a keyword-only argument of `prepare_scan`; it is never forwarded to the cameras.
* Without `focus=`, the saved focus position is re-applied on both lenses (the first write after each camera start is forced, see [FOCUSER.md](../camera/docs/FOCUSER.md)). A focus I2C failure raises from `focus_set`.
* The SyncReady waits for both cameras share one `ready_timeout` deadline.
* Later `apply_settings(brightness=..., contrast=...)` calls do not re-enable AE; AWB presets keep AWB enabled but never switch it off.

### 7b. Grab-worker failures and camera timeouts
* A grab worker survives an unexpected error while handling one frame (for instance a `MemoryError` copying a 12 MP frame): the request is released, the error is logged and the loop goes on. After `GRAB_MAX_CONSECUTIVE_ERRORS` (100) failures in a row, or when `capture_request(wait=CAMERA_CALL_TIMEOUT_S)` gets no frame, the worker gives up and the camera is reported dead (`_worker_errors`).
* From then on `capture_photo` fails at once with `CaptureError("a camera stopped delivering frames (camera N: ...)")` instead of waiting for frames that never come; `_start_pipeline` clears the marks.
* `CAMERA_CALL_TIMEOUT_S` (60 s, environment variable of the same name) bounds every blocking metadata/request call (`ArducamIMX477._capture_metadata`, `lock_auto_features`, `wait_for_light_stable`): a stalled sensor raises `CameraTimeoutError`, the preparation ends in `ERROR` and the scanner does not go on.

### 8. Lifecycle & Second Scan
* `_start_pipeline()` rolls back when a camera fails to start: whatever was started is stopped, `_running`/`is_streaming` become `False`, focusers are invalidated and the original exception is re-raised, so the next call can retry cleanly.
* `close()` is idempotent: it blocks restarts, calls `stop_stream()` first, then shuts down the executor and closes each camera (`stop_recording`, `stop_preview`, `stop`, `Picamera2.close()`, focuser bus - each step guarded so one failure does not leak the others). Afterwards `_start_pipeline()`/`start_stream()` raise `RuntimeError` and `capture_photo()` raises `CaptureError`: create a new instance for the next scan (the `Scanner` does this in `setup_and_stream`).
* `stop_stream()` marks the focusers as unpowered: the CSI bus (and the VCM) is power-gated while a camera is stopped.

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
        elif xvs is True:
            self.camera_mode = "xvs"
        else:  # xvs False or None
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

The camera subsystem is exposed to the frontend via native FastAPI endpoints. The four preview endpoints are implemented once, in [`router/scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/router/scanner.py) (`/scanner/stream/*`); [`router/test.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/router/test.py) registers the same functions under `/test/stream/*`, so they behave identically and there is one place to fix. The camera id / quality can be given as a query parameter or as a JSON body.

* **`GET /scanner/stream/mjpeg?camera=1|2`** (also `/test/stream/mjpeg`): Multipart MJPEG video stream with automatic spectator tracking. The `stream_urls` returned by `POST /scanner/prepare` point here.
* **`POST /scanner/stream/active?camera_id=1|2`**: Instant active preview camera switching.
* **`POST /scanner/stream/quality?quality=80`**: Dynamic runtime adjustment of preview JPEG compression quality (1–100).
* **`GET /scanner/stream/sync-status`**: Real-time diagnostic telemetry (SyncReady, offset_ms, stream_fps, drops, spectators).
* **`POST /test/camera/snap`**: Captures a synchronized test pair (`test_snaps/`) at the configured photo size with EXIF tags.
