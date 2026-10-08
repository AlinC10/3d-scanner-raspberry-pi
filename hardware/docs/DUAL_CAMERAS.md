# Dual Camera Architectures (`DualCamera` vs `DualCameraXVS`)

The 3D scanner utilizes a vertical stereoscopic camera array consisting of two **Arducam IMX477 12.3MP High Quality** cameras mounted on the Z-axis carriage.

The codebase provides two swappable, polymorphic implementations:
1. **`DualCamera`** ([`../dual_camera.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera.py)): Concurrent software multi-threading.
2. **`DualCameraXVS`** ([`../dual_camera_xvs.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_xvs.py)): Hardware-synchronized master/slave triggering via sensor XVS pins.

Both classes expose an **identical API interface**, allowing [`../scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/scanner.py) to toggle between them effortlessly using the `Scanner(xvs=True/False)` flag.

---

## ⚖️ Comparison Matrix

| Feature | `DualCamera` (Software Multi-Threading) | `DualCameraXVS` (Hardware Cross-View Sync) |
|---|---|---|
| **Primary Use Case** | Baseline development & testing without jumper cables | Production photogrammetry & precision capture |
| **Sync Accuracy** | Software level (~5–20 ms shutter offset via OS thread dispatch) | Sub-microsecond (pixel-perfect hardware frame sync) |
| **Physical Wiring** | Two standard Raspberry Pi CSI ribbon cables | Standard CSI cables + **1-pin jumper wire** connecting sensor XVS pads |
| **OS Configuration** | Standard Raspberry Pi OS camera setup | Requires custom `dtoverlay` lines in `/boot/firmware/config.txt` |
| **Threading Model** | Symmetric concurrent execution (`ThreadPoolExecutor(2)`) | **Asymmetric Slave-First Sequence** (Slave starts $\to$ 0.1s pause $\to$ Master starts) |
| **Failure Mode** | Both cameras operate independently; one failing does not freeze the other | If Master fails to generate pulses, Slave blocks waiting forever |

---

## ⚡ Hardware XVS Synchronization Guide

Sony IMX477 sensors support hardware cross-camera synchronization using the **XVS (eXternal Vertical Sync)** pin. 

```
┌──────────────────────────────────────┐       ┌──────────────────────────────────────┐
│       IMX477 Camera 0 (Master)       │       │       IMX477 Camera 1 (Slave)        │
│                                      │       │                                      │
│  [ CSI-2 Cable ] ──► Pi CAM Port 0   │       │  [ CSI-2 Cable ] ──► Pi CAM Port 1   │
│  [ XVS Pad ] ────────────────────────┼───────┼─────► [ XVS Pad ]                    │
└──────────────────────────────────────┘       └──────────────────────────────────────┘
                     (Hardware timing pulses emitted from Master)
```

### 1. Physical Hardware Wiring
Solder or attach a female-to-female jumper wire between the **XVS solder pads** on both Arducam IMX477 camera modules.

### 2. Raspberry Pi Kernel Configuration
Edit `/boot/firmware/config.txt` to assign the Master and Slave roles to the respective CSI camera ports:

```ini
# /boot/firmware/config.txt
dtoverlay=imx477,camera0,xvs_source   # Master: Generates XVS timing clock
dtoverlay=imx477,camera1,xvs_sink     # Slave: Waits for external XVS clock
```

### 3. The "Slave-First" Threading Rule
In hardware XVS mode, the **Slave sensor completely freezes its pipeline until it detects timing pulses from the Master**. 
* If Master is started first, it emits pulses before Slave is listening.
* If started sequentially in a single thread, Python deadlocks when initializing the Slave.

To resolve this, `DualCameraXVS` enforces an asymmetric thread dispatch inside [`prepare_scan`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_xvs.py#L104-L114) and [`start_stream`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/dual_camera_xvs.py#L116-L127):

```python
# 1. Start the Slave camera thread FIRST (it blocks waiting for XVS pulses)
f_slave = self._executor.submit(self.slave.prepare_scan, **kwargs_slave)

# 2. Give the OS thread scheduler 100ms to spin up the Slave thread
time.sleep(0.1)

# 3. Start the Master camera. Generating pulses unblocks the Slave!
f_master = self._executor.submit(self.master.prepare_scan, **kwargs_master)

# 4. Await both threads
return [f_master.result(), f_slave.result()]
```

---

## 🛠️ Shared Polymorphic API Reference

Both `DualCamera` and `DualCameraXVS` implement the following unified methods:

### 1. Initialization
```python
DualCamera(
    camera_id_1: int = 0,
    camera_id_2: int = 1,
    quality: int = 95,
    size: Optional[Tuple[int, int]] = None,
    video_size: Optional[Tuple[int, int]] = None,
    preview_size: Optional[Tuple[int, int]] = None,
    rotation_1: int = 0,
    rotation_2: int = 0
)

DualCameraXVS(
    master_id: int = 0,
    slave_id: int = 1,
    quality: int = 95,
    size: Optional[Tuple[int, int]] = None,
    video_size: Optional[Tuple[int, int]] = None,
    preview_size: Optional[Tuple[int, int]] = None,
    master_rotation: int = 0,
    slave_rotation: int = 0
)
```

---

### 2. Preparation & Streaming

#### `prepare_scan(**kwargs) -> List[dict]`
Configures both cameras, applies exposure/gain parameters, runs autofocus (if requested), and locks AE/AWB. Supports independent dual parameters:
* `focus=(300, 450)` sets Camera 0 to 300 and Camera 1 to 450.
* `rotation=(0, 180)` sets independent sensor rotations.
* **Master-to-Slave Stereoscopic Sync**: When auto-features are used (i.e. `exposure_time` or `colour_gains` are `None`), the Slave camera automatically inherits and is locked to the Master camera's exact `ExposureTime`, `AnalogueGain`, and `ColourGains`. This guarantees 1:1 color temperature and luminance parity between the stereoscopic camera pair, preventing texture seam discoloration in Meshroom. If manual values are explicitly passed in `kwargs`, this override is safely bypassed.

#### `start_stream(bitrate: int = 4_000_000) -> List[str]`
Pushes dual H.264 RTSP live video streams ($960 \times 720$ native 4:3 @ 24 FPS) to the local MediaMTX streaming server.
* **Returns**: `["rtsp://localhost:8554/cam0", "rtsp://localhost:8554/cam1"]`

#### `stop_stream() -> None`
Stops the RTSP encoder pipeline on both cameras.

---

### 3. Image Controls & Auto Features

#### `apply_settings(**kwargs) -> None`
Applies runtime image controls (`brightness`, `contrast`, `saturation`, `sharpness`, `exposure_time`, `awb_mode`) to both cameras simultaneously without stopping the stream.

#### `lock_auto_features(settle_time: float = 2.0) -> List[dict]`
Waits `settle_time` seconds for the sensors to meter the illuminated object on the turntable, reads `ExposureTime`, `AnalogueGain`, and `ColourGains` from the Master camera metadata, forcefully applies them to the Slave camera, and locks the algorithms (`AeEnable=False`, `AwbEnable=False`). Note: when called via `prepare_scan`, partial manual overrides intelligently toggle these locks.

---

### 4. Precision Focus Control (VCM)

Both classes support the motorized Voice Coil Motor (VCM) focusers on the Arducam lenses:

#### `focus_set(position: int | Tuple[int, int]) -> None`
Sets absolute lens position (`0` = infinity, `1023` = macro close-up).
* Pass `int` to set both lenses equally: `focus_set(400)`
* Pass `tuple` to set independently: `focus_set((350, 480))`

#### `focus_step(delta: int | Tuple[int, int]) -> None`
Adjusts focus incrementally by $\pm\Delta$.

#### `focus_sweep_autofocus(step: int = 30, roi: tuple = (0.3, 0.3, 0.4, 0.4)) -> List[int]`
Performs contrast-detection sweep autofocus on both lenses concurrently and locks to the sharpest position.

#### `focus_reset() -> None`
Resets both focusers to infinity (`0`).

---

### 5. Synchronized Capture & Meshroom Rig Format

#### `capture_photo(output_prefix="photo", output_dir=".", meshroom_rig=True, quality=None) -> List[str]`
Captures synchronized photos from both sensors.

#### Meshroom Multi-Camera Rig Directory Structure
When `meshroom_rig=True` (default), photos are automatically segregated into numbered subdirectories with **identical base filenames**:

```
output_dir/
├── 0/
│   ├── slice_0001.jpg   <-- Captured by Camera 0
│   └── slice_0002.jpg
└── 1/
    ├── slice_0001.jpg   <-- Captured by Camera 1 (Identical timestamp/name!)
    └── slice_0002.jpg
```

> [!TIP]
> **Why Identical Filenames Matter**:
> Meshroom's `CameraInit` node uses matching filenames in subdirectory `0/` and `1/` to automatically detect a fixed multi-camera rig. It solves the relative baseline transformation between the two cameras once, drastically speeding up 3D reconstruction.

---

## 🔄 Dynamic Selection in `../scanner.py`

In [`../scanner.py`](file:///home/alin/Desktop/3d-scanner-raspberry-pi/hardware/scanner.py), the `Scanner` class dynamically initializes the correct driver:

```python
class Scanner:
    def __init__(self, xvs: bool = True):
        self.xvs = xvs
        self.dual_cameras: Optional[DualCamera | DualCameraXVS] = None

    def setup_and_stream(self, enable_stream=True, bitrate=2_000_000, **kwargs):
        camera_kwargs = { ... }

        if self.xvs:
            # Production: Hardware XVS Master/Slave
            self.dual_cameras = DualCameraXVS(master_id=0, slave_id=1, **camera_kwargs)
        else:
            # Fallback/Dev: Software ThreadPoolExecutor
            self.dual_cameras = DualCamera(camera_id_1=0, camera_id_2=1, **camera_kwargs)

        kwargs["keep_running"] = True
        self.dual_cameras.prepare_scan(**kwargs)
        ...
```
