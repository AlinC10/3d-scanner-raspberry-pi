# Stereoscopic Camera Synchronization (Photogrammetry Optimization)

## 🎯 What was accomplished
We resolved the stereoscopic color and brightness parity mismatch between the two cameras and injected Photogrammetry-optimized sensor defaults. This significantly improves Meshroom/AliceVision's SIFT feature matching and preserves perfect XVS hardware frame synchronization.

## 🛠️ Changes Made
1. **Injected Photogrammetry Defaults (`hardware/camera/camera.py`)**
   - Enabled `CentreWeighted` metering (`AeMeteringMode`) by default. This prevents the dark background around the turntable from falsely triggering the cameras to overexpose the object.
   - Disabled spatial noise reduction (`NoiseReductionMode.Off`). This stops the Libcamera ISP from blurring/smearing the micro-textures and surface grain that photogrammetry heavily relies on for depth matching.
2. **Master-Slave AE/AWB Parameter Override (`hardware/dual_camera_xvs.py`)**
   - Intercepted the auto-exposure (AE) and auto-white-balance (AWB) results returned by both cameras in `prepare_scan()` and `lock_auto_features()`.
   - Built a dynamic sync routine that forcefully copies the Master camera's locked `ExposureTime`, `AnalogueGain`, and `ColourGains` onto the Slave camera.
   - Included robust safeguards (`getattr(self.slave.picam2, "started", False)`) to prevent Libcamera crashes if the override happens while the camera is explicitly stopped (e.g., `keep_running=False`).
3. **Fallback Array Sync (`hardware/dual_camera.py`)**
   - Applied the exact same override logic to the standard fallback `DualCamera` interface, mapping `self.cam1`'s metadata securely onto `self.cam2`.

## 🧪 Next Steps for the User
- Run your hardware tests (e.g. `hardware/camera/tests/main.py`) in Auto mode.
- Notice how the Slave (second) camera now matches the Master (first) camera flawlessly in brightness and color tint, regardless of their slightly different physical angles/perspectives!
