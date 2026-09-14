# Meshroom Rig Implementation Walkthrough

All proposed changes for the multi-camera rig integration have been successfully applied to your codebase! Here is a summary of what was accomplished:

### 1. Unique EXIF Serial Numbers (`camera.py`)
* Modified `_process_captured_image` to inject a fake `BodySerialNumber` into the EXIF data of every saved JPEG.
* Left camera gets `SerialNumber: cam0`, right camera gets `SerialNumber: cam1`.
* **Result:** Meshroom will now safely split the two physical lenses into independent optical profiles during Bundle Adjustment, eliminating the need for you to manually synchronize their focal planes perfectly.

### 2. Rig Directory Structure (`dual_camera.py` & `dual_camera_xvs.py`)
* Updated the `capture_photo()` signature in both dual camera classes to include `meshroom_rig=True` (enabled by default) and `output_dir`.
* When `meshroom_rig` is true, the script automatically creates two subdirectories: `output_dir/0/` and `output_dir/1/`.
* The captured JPEGs are placed inside their respective directories using the **exact same filename** (e.g., `0/scan01.jpg` and `1/scan01.jpg`), which is required by Meshroom to link them as a rigid stereo pair.
* If you set `meshroom_rig=False`, it falls back to the old behavior (e.g., `scan01_cam0.jpg` in the root folder).

### Next Steps for You
You are now ready to capture a scan! Just make sure you follow the physical calibration steps outlined in our plan:
1. Reset the VCMs to `0` and manually twist the physical lenses until they are both sharp at infinity.
2. In your capture script, call `dual_cam.capture_photo(output_prefix="scan_01", output_dir="my_new_dataset")`.
3. Drag the `my_new_dataset` folder directly into Meshroom and watch the magic happen!
