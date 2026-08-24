# 🤖 3D Scanner - Master Implementation Blueprint

## 📋 Task Breakdown 

### 🔧 HARDWARE (Real-World Actions)
* [ ] [Line the MDF Enclosure & Turntable with Black Velvet](#hw-velvet)
* [ ] [Surface the Turntable with Matte Black Rubber / Silicone](#hw-rubber)
* [ ] [Implement Hardware Cross-Polarization (Kill Glare)](#hw-polarization)
* [ ] [Prep environment for flipped scanning (Undercuts)](#hw-flipped)

### 💻 SOFTWARE (Code, Cloud, Automation)
* [ ] [Configure turntable for 18° jumps](#sw-turntable)
* [ ] [Implement Z-Axis top detection (OpenCV)](#sw-zaxis)
* [ ] [Add "Backlash Compensation" for Lens Motor](#sw-backlash)
* [ ] [Lock AE, AWB, and Focus before scanning](#sw-ae-awb)
* [ ] [Enforce strict "No Digital Zoom" policy](#sw-zoom)
* [ ] [Perform OpenCV Pre-calculated Lens Calibration](#sw-calibration)
* [ ] [Set output format to High-Quality JPEG](#sw-jpeg)
* [ ] [Optional: Downscale resolution to 6MP for faster processing](#sw-downscale)
* [ ] [Setup Concurrent Streams and "Stop-Settle-Shoot" logic](#sw-stop-settle)
* [ ] [Implement Mode 1: Universal Mode (24cm Fixed Focus)](#sw-mode1)
* [ ] [Implement Mode 2: High-Fidelity Mode (Focus Stacking)](#sw-mode2)
* [ ] [Implement Hybrid Enfuse Stacking Pipeline](#sw-enfuse)
* [ ] [Implement 3-Step Process Throttling for `enfuse`](#sw-throttle)
* [ ] [Implement "Blur Rejector" Safety Check](#sw-blur)
* [ ] [Build an Asynchronous Upload Queue](#sw-upload)
* [ ] [Configure a Custom Bounding Box in Meshroom](#sw-bbox)
* [ ] [Implement "Empty Box" Automatic Masking](#sw-emptybox)

---

## 1. 🏗️ Physical Build & MDF Enclosure
*Status: Foundation* | *Depends on: Raw materials (MDF, Velvet, Polarizers, Rubber)*

<a id="hw-velvet"></a>
* [ ] **Line the MDF Enclosure & Turntable with Black Velvet**
* **Details:** Meshroom will get confused if it tracks the stationary wood grain of the walls while the object spins. Treat the inside of the MDF box, walls, and turntable top with **Black Velvet** cloth (or ultra-matte black paint). Velvet absorbs 99% of light, turning the background into absolute pitch black. Meshroom will instantly ignore the background and focus 100% on the lit object, speeding up processing and preventing tracking errors.

<a id="hw-rubber"></a>
* [ ] **Surface the Turntable with Matte Black Rubber / Silicone**
* **Details:** To prevent objects from sliding when the stepper motor snaps into position, use a high-friction surface.
  * **Critical Requirement:** The rubber must be **Pitch Black, Matte, and completely featureless** (no textures, grids, or logos). A flat black neoprene or silicone mat is perfect. This provides structural grip while still acting as a visual "void" for Meshroom, replacing the need for velvet on the turntable itself.

<a id="hw-polarization"></a>
* [ ] **Implement Hardware Cross-Polarization (Kill Glare)**
* **Details:** Because the object is trapped in a box with intense LEDs, shiny plastics or metals will create massive glare that breaks photogrammetry.
  * Tape **Linear Polarizer Film** over all LED lighting strips.
  * Mount a small **Circular Polarizer (CPL) filter** in front of Both Arducam lenses.
  * Rotate the CPL filter by hand until the shiny reflections on an object completely vanish. All surfaces will now look perfectly matte.

---

## 2. 🔌 Hardware & Mechanics
*Status: Core Mechanics* | *Depends on: MDF Box Completed, Rapberry Pi Wired*

<a id="sw-turntable"></a>
* [ ] **Configure turntable for 18° jumps**
* **Details:** Set your NEMA17 motor (1.8° per step) to move exactly 10 full steps per jump. This creates 20 stops per 360° rotation, giving you 40 images per ring and ensuring the required 60-80% overlap for Meshroom's SIFT feature-matching.

<a id="sw-zaxis"></a>
* [ ] **Implement Z-Axis top detection**
* **Details:** Stop the scan when it reaches the top of the object using **Stereoscopic Depth Mapping** via OpenCV (`cv2.StereoBM` or `cv2.StereoSGBM`). Stop the loop when the center depth map only detects the background. Alternatively, mount a cheap Time-of-Flight (ToF) sensor to physically detect when the object ends. Avoid 2D background subtraction to prevent perspective warping issues.

<a id="sw-backlash"></a>
* [ ] **Add "Backlash Compensation" for Lens Motor**
* **Details:** Essential CNC/robotics trick to fix mechanical gear slop when returning the lens to original positions. 
  * Example: To move from 30cm back down to 20cm, do NOT go `30 -> 20`. 
  * **Over-travel approach:** Tell motor to go `30 -> 10` (overshoot), then climb `10 -> 20`. Doing this guarantees the gears mesh from the exact same physical direction every time, maintaining micro-millimeter focus accuracy.

---

## 3. 📷 Optics & Camera Configuration
*Status: Baseline Calibration* | *Depends on: Hardware & Mechanics powered on*

<a id="sw-ae-awb"></a>
* [ ] **Lock AE, AWB, and Focus before scanning**
* **Details:** Meshroom fails if lighting or focus changes between photos. Turn on your LEDs, wait 2-3 seconds for auto-exposure to adjust, then lock the settings via `picamera2` before starting the motor loop.
```python
# After letting the camera run for 2 seconds to calculate the room light:
cam.set_controls({
 "AeEnable": False,       # Turn off Auto Exposure
 "AwbEnable": False,      # Turn off Auto White Balance
 "ExposureValue": 0.0     # Lock the exposure to the current calculated value
})
```

<a id="sw-zoom"></a>
* [ ] **Enforce strict "No Digital Zoom" policy**
* **Details:** The Arducam B0272 has a fixed focal length. Never apply digital zoom or Region of Interest (ROI) cropping via `picamera2`. Digital cropping destroys the Principal Point of the lens and throws away 12MP resolution, causing Meshroom to miscalculate the optical distortion model. 

<a id="sw-calibration"></a>
* [ ] **Perform OpenCV Pre-calculated Lens Calibration (1-Time Setup)**
* **Details:** Don't force Meshroom to guess your Arducam's lens distortion on every scan. Print a "ChArUco" black-and-white checkerboard. Put it in the scanner and take 20 photos at different angles. Run a one-time OpenCV script to calculate the mathematical curvature of your lens (`.json`). Feed this into Meshroom's `CameraInit` node to massively decrease the RTX 5090's solving time.

<a id="sw-jpeg"></a>
* [ ] **Set output format to High-Quality JPEG**
* **Details:** Use maximum-quality JPEGs (Quality 95-100) instead of PNG or RAW formats. High-quality JPEG compression is invisible to Meshroom's algorithms, but shrinks an 80-image scan from ~2GB down to ~400MB.

<a id="sw-downscale"></a>
* [ ] **Optional: Downscale resolution to 6MP for faster processing**
* **Details:** If you need Meshroom to process the final 3D model 2-3x faster, lower the capture resolution from 12MP to roughly 6MP (approx. 2800 × 2100). You preserve the necessary overlapping geometry while feeding the GPU 50% less pixel data.

---

## 4. 🧠 Software: Capture Logic & Working Modes
*Status: Python CLI Scripting* | *Depends on: Cameras and Motors calibrated*

<a id="sw-stop-settle"></a>
* [ ] **Setup Concurrent Streams and "Stop-Settle-Shoot" logic**
* **Details:** Configure `picamera2` to output a low-res preview stream for monitoring and instantly capture 12MP high-res frames without pausing the video. Avoid continuous rotation to prevent motion blur; instead, use a loop that pauses briefly to let vibrations settle.
```python
# 1. Start the Live Stream
dual_camera.start_continuous_stream()

# 2. Adjust focus (Manual via CLI or Auto)
dual_camera.set_focus(150) # Example I2C motor position

# 3. The Fast Scan Loop
for step in range(20):
    motor.rotate_turntable_18_degrees()
    # Let the object stop vibrating
    time.sleep(0.2)
    # Grab the high-res photos INSTANTLY without stopping live video
    dual_camera.take_photo(filename=f"scan_img_{step}")

# 4. Stop Live Stream when scan is totally finished
dual_camera.stop_stream()
```

<a id="sw-mode1"></a>
* [ ] **Implement Mode 1: Universal Mode (24cm Fixed Focus)**
* **Details:** Fastest and most efficient mode. Uses a static hyperfocal distance of 24cm. 
  * The natural depth of field covers roughly `19.7cm` to `30.6cm`, creating an ~11cm "bubble of clarity" that perfectly covers the turntable radius. 
  * Bypasses heavy Raspberry Pi CPU loads. Best for matte objects, 3D prints, and standard objects. Lens motor stays stationary during the entire scan.

<a id="sw-mode2"></a>
* [ ] **Implement Mode 2: High-Fidelity Mode (Focus Stacking at 22cm & 27cm)**
* **Details:** For portfolio-grade/high-precision models. 
  * The 22cm photo covers `18cm to 27cm`.
  * The 27cm photo covers `21cm to 35cm`.
  * They robustly overlap in the middle. The stacking software blends them to create a single "razor-sharp" image spanning `18cm to 35cm`. Scan time will double, but clarity is maximum.

<a id="sw-enfuse"></a>
* [ ] **Implement Hybrid Enfuse Stacking Pipeline (for Mode 2)**
* **Details:** Instead of writing raw OpenCV stacking, use industry-standard subprocess tools to negate lens breathing.
  * Install requirement: `sudo apt-get install hugin-tools enfuse`
  * **Step 1:** Python runs `align_image_stack` to micro-correct optical scaling/breathing.
  * **Step 2:** Python runs `enfuse` to seamlessly blend the aligned images with zero ghosting. The final image is sent to Meshroom.

<a id="sw-throttle"></a>
* [ ] **Implement 3-Step Process Throttling for `enfuse` (Mode 2)**
* **Details:** `enfuse` uses 100% of CPU/RAM by default, which can freeze the Raspberry Pi and stutter the hardware scan loop. Throttle the background subprocess using native Linux sysadmin tools so Core 0 stays completely free for Python/Hardware control.
  * **Command Structure:** `taskset -c 1,2,3 nice -n 19 ionice -c 3 enfuse -o output.jpg img1.jpg img2.jpg`
  * **`taskset -c 1,2,3`**: Isolates `enfuse` to Cores 1, 2, and 3 only (Core 0 safely runs the Pi OS and Python scanner).
  * **`nice -n 19`**: Drops CPU priority to the absolute lowest setting.
  * **`ionice -c 3`**: Drops SD card disk read/write priority to "Idle" to prevent I/O blocking.

<a id="sw-blur"></a>
* [ ] **Implement "Blur Rejector" Safety Check (OpenCV)**
* **Details:** Prevent ruined Meshroom pipelines caused by physical mechanical vibrations. Before uploading/saving, run `cv2.Laplacian(image, cv2.CV_64F).var()` on the preview frame. If the focal score drops below a set threshold, the camera took a blurry photo. The script will automatically pause, wait 1 second for vibrations to settle, and retake the photo.

---

## 5. ☁️ Cloud Architecture & Meshroom Pipeline
*Status: RunPod Processing* | *Depends on: Python CLI outputting valid datasets*

<a id="sw-upload"></a>
* [ ] **Build an Asynchronous Upload Queue**
* **Details:** Implement a background thread using Python's `queue.Queue` so the Pi can upload JPEGs to Cloudflare R2 silently. This prevents the turntable from stalling while waiting for the Wi-Fi upload to finish.
```python
import threading
import queue
import cloud
from hardware import Motor
from hardware import DualCamera

# The background uploader thread
def upload_worker(q):
    while True:
        img_path = q.get()
        if img_path is None:  # Stop signal
            break
        # Mock upload to cloudflare R2
        cloud.upload_to_r2(img_path) 
        q.task_done()

# Start the uploader thread
upload_queue = queue.Queue()
uploader_thread = threading.Thread(target=upload_worker, args=(upload_queue,))
uploader_thread.start()

# --- The Fast Hardare Scan Loop ---
for step in range(20):
    motor.rotate_18_degrees()
    # Save photos as high-quality JPEGs to the SD card
    img1, img2 = dual_camera.capture() 
    # Hand the file paths to the background thread immediately
    upload_queue.put(img1)
    upload_queue.put(img2)
    # Loop continues instantly without waiting for Wi-Fi!

# End of scan, wait for remaining uploads
upload_queue.put(None)
uploader_thread.join()
```

<a id="sw-bbox"></a>
* [ ] **Configure a Custom Bounding Box in Meshroom (`Meshing` Node)**
* **Details:** Meshroom is "scale agnostic" and cannot natively ignore things "past 35cm". Because your hardware is fixed inside an MDF box, apply a **Custom Bounding Box** inside the Meshroom pipeline.
  * *How it works:* The `Meshing` node will delete any tracked points (like walls or background dust) outside an invisible 3D box centered over the turntable.

**1-Time Calibration:**
1. Run one standard scan and open the result in the Meshroom GUI on a PC.
2. Select the `Meshing` node and check **`customBoundingBox`**.
3. A 3D white box will appear. Scale and move it to perfectly surround the turntable area only.
4. Copy the exact `bboxTranslation`, `bboxRotation`, and `bboxScale` values generated in the properties panel.

**RunPod Pipeline Update:**
Open your `.mg` JSON template and inject those parameters into the `Meshing` node block:
```json
"Meshing_1": {
    "nodeType": "Meshing",
    "inputs": {
        "customBoundingBox": true,
        "boundingBox": {
            "bboxTranslation": [0.12, -0.45, 1.2],
            "bboxRotation": [0.0, 0.0, 0.0],
            "bboxScale": [2.5, 2.5, 3.0]
        }
    }
}
```

<a id="sw-emptybox"></a>
* [ ] **Implement "Empty Box" Automatic Masking**
* **Details:** Even with Black Velvet, dust or seams might be visible due to the 15° camera tilt. Once the scan is complete and the object is removed, take one final "Empty Box" photo of the bare turntable. Upload this empty frame alongside the dataset. Meshroom uses this as a direct comparative mask to automatically erase any static background noise instantly on the RTX 5090.

<a id="hw-flipped"></a>
* [ ] **Prep environment for flipped scanning (Undercuts)**
* **Details:** To scan undercuts, you will need to scan the object, flip it, and scan it again. When processing both halves of an object (right-side up and upside down), dump all photos into Meshroom simultaneously as a single batch. It will automatically detect overlapping textures and seal the mesh.
