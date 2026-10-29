# Automated 3D Scanner - Comprehensive User Manual


## Table of contents

1. [System overview](#1-system-overview)
2. [Safety and ground rules](#2-safety-and-ground-rules)
3. [Hardware environment and object placement](#3-hardware-environment-and-object-placement)
4. [Quick start: your first object scan](#4-quick-start-your-first-object-scan)
5. [Preparation and acquisition phase in detail](#5-preparation-and-acquisition-phase-in-detail)
6. [Scan settings explained](#6-scan-settings-explained)
7. [Face scan](#7-face-scan)
8. [Cloud processing and asset delivery](#8-cloud-processing-and-asset-delivery)
9. [The Library](#9-the-library)
10. [Network, Wi-Fi and Bluetooth](#10-network-wi-fi-and-bluetooth)
11. [AI assistant](#11-ai-assistant)
12. [Service and calibration tools](#12-service-and-calibration-tools)
13. [Understanding the status screen](#13-understanding-the-status-screen)
14. [Troubleshooting](#14-troubleshooting)
15. [Tips for better scans](#15-tips-for-better-scans)
16. [Technical specifications at a glance](#16-technical-specifications-at-a-glance)
17. [Glossary](#17-glossary)
18. [Known limitations and open points](#18-known-limitations-and-open-points)

---

## 1. System Overview

The automated 3D scanner is an advanced, edge-to-cloud photogrammetric device designed to acquire geometrically complete and photometrically consistent image sets without operator intervention. It uses an elastic cloud computing model: the heavy photogrammetric reconstruction runs on remote, dynamically provisioned GPU instances instead of an expensive local workstation. The entire system is operated autonomously through an integrated **11.6" capacitive touchscreen** Human-Machine Interface (HMI) with an HDMI and USB touch interface.

### 1.1 What the scanner does

| Mode | What you get | Typical use |
|---|---|---|
| **Object scan** | A complete 3D model of an object, built from many photo pairs taken while the object rotates and the cameras move up in levels | Parts, figurines, tools, products |
| **Face scan** | A single synchronized stereo photo pair taken at the best height for a face | Portraits, facial capture |

### 1.2 How it works in one minute

1. You place the object on the turntable and close the enclosure.
2. The scanner switches on the lights, wakes the cameras, homes the carriage and waits until the lights and cameras are stable.
3. Two synchronized cameras photograph the object at fixed angular steps while the turntable rotates, then climb to the next level and repeat.
4. Photos are uploaded to cloud storage while scanning continues.
5. A cloud GPU reconstructs the 3D model, which then appears in the Library in several formats.

---

## 2. Safety and Ground Rules

- The device operates safely with **no moving parts outside the closed enclosure**. Keep the panels closed during a scan.
- The distance sensor is a **940 nm Class 1 laser** Time-of-Flight (ToF) sensor, which is the lowest laser hazard class.
- A **Cancel** (emergency stop) function is available at any time. It stops the motors, the lights, the cameras and the uploads. Use it whenever something looks wrong. The motion controller reacts to an abort within **4.1 ms** or less.
- While a scan is preparing, running or uploading, **network changes are blocked** (see section 10) so that a connection change cannot interrupt your uploads.
- The service tools in section 12 move the motors directly. Use them only when no scan is active and only if you are trained to service the machine.

---

## 3. Hardware Environment and Object Placement

The physical structure of the scanner is built using 2020 aluminum extrusions and closed panels.

### 3.1 Enclosure

The internal surface is **white** to provide a uniform, feature-poor background. This is critical: a textured static background would produce features that contradict the rotating-object motion model during the 3D reconstruction phase. **Do not add posters, stickers, cloths or marks inside the enclosure.**

### 3.2 Turntable

The object to be scanned must be placed on the **Ø260 mm turntable**.

| Item | Value |
|---|---|
| Turntable diameter | 260 mm |
| Carriage (height) travel | up to 475 mm |
| Cameras | 2, at 300 mm from the axis, 20° apart |
| Default start height | 30 mm above the bottom (adjustable 0 to 100 mm) |

The maximum object height is not stated in the sources; the carriage travel is 475 mm **(open)**.

### 3.3 Safety and clearances

Ensure the background wall is clear of obstructions. The ToF sensor looks across the turntable toward the back wall located at **535 mm**, with a sensing footprint of approximately **237 mm in diameter** at the back wall. If anything sits between the turntable and the back wall (cables, tools, a second object), the sensor cannot tell that the object has ended and the scan may run longer than needed or report a sensor warning.

### 3.4 Placing the object

1. Switch off any manual lights you use around the machine; the scanner uses its own LED lighting.
2. Place the object roughly at the **centre** of the turntable **(recommendation)**.
3. Make sure the object is stable and cannot slide or tip while the table turns. Use a small piece of putty or a non-textured support if needed **(recommendation)**.
4. Keep the object inside the Ø260 mm table; do not let it overhang the edge.
5. Check that nothing is placed between the turntable and the back wall.
6. Close the enclosure.

---

## 4. Quick Start: Your First Object Scan

1. **Prepare the machine.** Make sure the machine is powered, the touchscreen shows the scanner screen and the status is **idle**.
2. **Place the object** (section 3.4) and close the enclosure.
3. **Press Prepare.** Choose the lighting brightness, start height and camera options, or keep the defaults. The live preview from both cameras appears while the scanner prepares. This takes 15 to 30 seconds, and up to about 2 minutes when the lights need extra time to stabilize.
4. **Check the preview.** The object should be visible, sharp and well lit in both camera views. If not, press Cancel, adjust the object and prepare again.
5. **When the status is Prepared**, review the scan settings (angle, height step, speed) or keep the defaults, then **press Start**.
6. **Wait.** Watch the progress: current level, photos captured and photos uploaded. Do not open the enclosure.
7. **When the status is Completed**, open the **Library** to see and download your model once processing is finished.

---

## 5. Preparation and Acquisition Phase in Detail

Once the scan is initiated from the touchscreen, the system runs through a fully autonomous sequence.

### 5.1 Thermal-photometric stabilization

LED lights lose luminous flux as their junction warms up, and low-cost cameras experience internal drift. The scanner actively measures this drift using the camera's **exposure-gain product** (when the light fades, the camera compensates with longer exposure or higher gain, so this product tells how much the light has changed without any extra sensor). It automatically waits until the relative drift stabilizes **below 3% over a 10-second window** (with a **maximum timeout of 90 seconds**) before proceeding.

> **What you will notice:** the screen stays in the "preparing" state for a while after you press Prepare. This is normal. The whole preparation (homing, climb to the start height, camera calibration and stabilization) takes 15 to 30 seconds, and up to about 2 minutes with light stabilization.

### 5.2 Autofocus and photometric locking

1. Both cameras perform a contrast-based **autofocus (AF) sweep**, testing **35 focal points** to maximize sharpness in the **central 40%** of the image. **Place the important part of your object in the middle of the view.**
2. **Auto exposure (AE)** and **auto white balance (AWB)** then converge for **2 seconds** and are locked.
3. To prevent color and luminance seams, the parameters from the **master camera are inherited by the slave camera**.

After this step, exposure and colors stay constant for the whole scan. This is what keeps the final texture free of visible seams.

### 5.3 Adaptive hardware acquisition

The turntable rotates using a smooth **trapezoidal velocity profile** (it speeds up and slows down gently so the object is not jolted and the photos are sharp).

Instead of blindly scanning the entire vertical axis, the ToF sensor constantly checks the object's height. If **two consecutive readings** indicate the sensor is looking **past the object at the back wall**, the scan terminates immediately to save time.

**Example:** a 200 mm tall object typically requires **3 vertical levels (120 images)** and takes roughly **93 seconds** of mechanical acquisition.

How the numbers relate **(derived)**: with the default 18° step, one level has 20 stops (360° ÷ 18°). Each stop captures a synchronized pair of photos, so one level is 40 images and three levels are 120 images.

### 5.4 Concurrent upload

Images are uploaded to cloud storage in the background while the scanner is still moving, effectively hiding network transfer times. The screen shows two counters: **photos captured** and **photos uploaded**. They should stay close to each other.

### 5.5 Verification

At the end of acquisition, the scanner checks that every photo reached the cloud. If some are missing, the scan ends with an error and shows which photos are missing (up to 50 are listed).

### 5.6 What happens if the upload stalls

If no photo is uploaded successfully for **5 minutes (300 seconds)**, the scan fails. The status shows how long it has been since the last successful upload. Check the network connection (section 10).

---

## 6. Scan Settings Explained

All settings are optional; the defaults suit most objects.

### 6.1 Settings at Prepare

| Setting | Default | Allowed values | When to change it |
|---|---|---|---|
| Live preview | On | On / Off | Leave on to check the framing |
| Lighting brightness | 100% (1.0) | above 0 up to 100% | Lower for shiny objects that glare; keep high for dark objects |
| Start height | 30 mm | 0 to 100 mm (0.01 mm steps) | Raise if the bottom of the object is hidden or low features are not important |
| Photo resolution | 4056 × 3040 | 4056 × 3040 or 2028 × 1520 | Use the lower one for faster, lighter tests |
| JPEG quality | 95 | 10 to 100 | Lower to reduce upload size |
| Focus | Automatic | Automatic, a fixed value 0 to 1023, one value per camera, or the last saved position | Use a fixed focus only for repeat work on similar objects |
| Rotation of image | 0° | 0, 90, 180, 270 | Rarely needed |
| Settle time | 2 s | 0 to 10 s | Increase if exposure is unstable |
| Manual exposure, gain, white balance | Automatic | Exposure time in µs, gain 1 to 16, red/blue gains | For difficult lighting only |
| Copy white balance from camera 1 to 2 | Off | On / Off | Turn on if the two cameras show different colors |

### 6.2 Settings at Start

| Setting | Default | Allowed values | Effect |
|---|---|---|---|
| Angle per photo stop | 18° | up to 180°, a multiple of 0.1125°, must divide 360° exactly (for example 18°, 22.5°, 11.25°) | Smaller angle = more photos per level = more detail and a longer scan |
| Height step between levels | 100 mm | above 0 up to 475 mm (0.01 mm steps) | Smaller step = more overlap between levels, more photos |
| Turntable and carriage speed | 0.001 s per step | 0.0001 to 0.05 s (larger = slower) | Slower is gentler for delicate or top-heavy objects |
| Smooth start / stop (turntable) | On | On / Off | Keep on |
| Smooth start / stop (carriage) | Off | Off | Keep off |
| Job name | automatic | free text (for example `scan-vase-001`) | Helps you find the scan later |

**Photo count rule of thumb (derived):** images = (360 ÷ angle) × 2 cameras × number of levels. At 11.25° the same object needs twice as many photos as at 22.5°.

**Cloud reconstruction option:** the settings include a "launch cloud reconstruction" switch which should stay **off** (it belongs to a legacy path). See section 18.

---

## 7. Face Scan

Face scanning is a **two-step** process: prepare, then capture. Each capture needs its own prepare.

### 7.1 Before you begin

- Seat the subject so that the face is in the camera view. The carriage will search for the best height automatically.
- Face detection software must be installed on the scanner. If it is missing, the scanner refuses to prepare and tells you so.

### 7.2 Step 1: Prepare

| Setting | Default | Allowed values |
|---|---|---|
| Live preview | On | On / Off |
| Lighting brightness | 50% (0.5) | above 0 up to 100% |
| Start height (where the carriage waits and the search starts) | 180 mm | 100 to 350 mm |
| Upload the photo pair to the cloud | On | On / Off |
| Focus | Automatic (focuses on the face at capture) | Automatic or a fixed value |

Wait until the status shows **Prepared** (face mode). Check the preview: the subject should be seated and the face should be inside the view.

### 7.3 Step 2: Capture

Press **Take photo** (capture). The carriage searches for the face and photographs it.

| Setting | Default | Meaning |
|---|---|---|
| Strategy | Sweep for best score | **Sweep for best score:** scan the whole range, then return to the best height. **Stop when centred:** stop as soon as the face is centred. **None:** photograph at the current height |
| Search range | 150 mm | 10 to 250 mm. Start height + range must not exceed 475 mm |
| Search step | 8 mm | 1 to 25 mm |
| Manual height | none | 200 to 475 mm: go straight to this height, no search |
| Settle time | 0.15 s | 0.05 to 2 s |

**Important:** while the screen shows a "hold still" message, the subject must **stay still for about 10 seconds** after the search. Autofocus and exposure lock during this time.

### 7.4 Reading the result

After completion, the result shows the optimal height, the quality score, whether a face was detected and any warning, plus the two photos.

| Result | Meaning | What to do |
|---|---|---|
| Face detected | Normal | Done |
| No face detected, with a warning | No face was found in the range. The photo was taken at the start height. **This is not an error** | Prepare again with a different start height |
| Face detection not run | You used a manual height or the "none" strategy | Normal |
| Warning that only one camera saw the face | One camera had a poor view | Reposition the subject and repeat |

---

## 8. Cloud Processing and Asset Delivery

After acquisition, the edge device provisions an ephemeral cloud GPU pod. The user can track the progress and download the resulting 3D models via the **Library** interface.

### 8.1 What happens in the cloud

For the curious **(summary of the reconstruction pipeline)**:

1. The photo pairs are matched to find the same points in different views.
2. The camera positions are solved, using the fact that the two cameras are a fixed rig. This makes the result more stable on objects with little texture.
3. Depth maps are computed and merged into a point cloud.
4. A closed surface is built and smoothed, then textured.
5. The cloud job size is chosen automatically from the predicted memory need: jobs up to 64 GB run on consumer GPUs, larger jobs on high-memory hosts. This avoids silent out-of-memory failures.

### 8.2 Delivery formats

The system provides multi-format delivery tailored to different use cases:

| Format | Versions | Best for |
|---|---|---|
| **OBJ** | **High:** high-polygon mesh with a 4096 × 4096 lossless PNG texture atlas. **Low:** low-polygon decimated mesh (capped at 50,000 vertices) with a 2048 × 2048 JPEG texture | Editing in 3D software; lighter sharing |
| **STL and 3MF** | Mesh for printing | **3D printing.** The system automatically repairs non-manifold geometry, filling holes, to ensure the exported mesh is watertight |
| **GLB** | Draco-compressed | **Web and AR.** File size reduced by up to 86.6% (for example from 35.1 MB to 4.7 MB) while staying visually and metrically lossless, with a maximum position error of ≤ 7.9 µm |

The size reduction figure checks out **(derived)**: (35.1 − 4.7) ÷ 35.1 ≈ 86.6%.

### 8.3 Choosing the right file

| I want to... | Use |
|---|---|
| Print the object | STL or 3MF (check "watertight" in the Library) |
| Show it on a website or in AR | GLB (low first) |
| Edit or texture it in 3D software | OBJ high, or OBJ low for a lighter file |
| Preview quickly on the scanner | The built-in viewer with the low version |

---

## 9. The Library

The Library lists the finished models stored on the scanner.

### 9.1 Browsing

- Objects are listed **newest first**, up to 20 per page by default (1 to 100 can be requested). Use the next-page control until there are no more pages.
- Each entry shows a **title**, **creation date**, **dimensions** (length, width, height in mm), **volume** (cm³), **surface area** (cm²) and whether the mesh is **watertight**.

Example entry: *Parkside*, 71.0 × 54.3 × 226.9 mm, 363.74 cm³, watertight.

### 9.2 Opening and downloading a model

1. Open an object.
2. The viewer loads the **low** version first because it is light and quick.
3. Use the **high** version or **STL** buttons when you need full detail or a print file.
4. A button is disabled when that file does not exist for the object.

### 9.3 Formats and versions listed

The Library API lists **GLB** and **STL**, each in **low** and **high** versions. OBJ and 3MF delivery is described in section 8.2 **(open: confirm where OBJ and 3MF appear in the Library)**.

### 9.4 Planned features

Not yet available: export of selected models as a **QR download link** or to a **USB stick**, **thumbnails**, and **deleting** models.

---

## 10. Network, Wi-Fi and Bluetooth

The scanner needs a network connection to upload photos. A single status panel shows Ethernet, Wi-Fi and Bluetooth together and refreshes every few seconds.

### 10.1 Rules during a scan

While the scanner is preparing, running, processing, downloading or still uploading, these actions are **blocked** and report that the scanner is busy: Wi-Fi scan, switch on/off, connect and forget; Bluetooth scan, switch on/off, connect and send file; Ethernet connect. Reading the status is always allowed.

### 10.2 Ethernet

A normal cable connects by itself. The panel shows the cable state and IP address. Only enterprise (802.1X) wired networks need a manual connection with a username and password.

### 10.3 Wi-Fi

| Task | How |
|---|---|
| See the current connection | Status panel: network name, signal strength, IP address |
| Turn Wi-Fi on or off | Wi-Fi switch |
| See available networks | Wi-Fi list. It refreshes instantly from the last scan; use the **Refresh** button for a new radio scan (up to about 20 seconds) |
| Connect to a saved network | Select it: **no password needed** |
| Connect to a new network | Select it and enter the password |
| Connect to an enterprise network | Enter the username (identity) and password |
| Forget a saved network | Open the saved networks and choose Forget |

- A network with no security shows as open.
- If the password is wrong or missing, the scanner asks for it again. This also happens when a saved password stopped working. **A wrong password never deletes a working saved network.**
- Other problems (timeout, busy) show their own message.

### 10.4 After a restart

At start-up the scanner reconnects automatically to the connections used in the previous session. The panel shows "reconnecting" while this happens, then the result for each connection.

### 10.5 Bluetooth

| Task | How |
|---|---|
| Switch Bluetooth on or off | Bluetooth switch |
| Find devices | Scan (1 to 20 seconds, default 8). The list shows **named devices in range**, strongest first, plus connected ones |
| Connect | Select a device. The scanner pairs only when needed, so reconnecting a paired device works directly |
| Send a file to a device | Select a connected device and choose a file |

---

## 11. AI Assistant

An integrated assistant answers questions using the scanner's own documents.

- Open the assistant. If it is **starting**, wait a moment; if it is **off**, the screen shows the reason and the chat stays disabled.
- Type your question. The assistant keeps the conversation history during the session.
- If the assistant reports it is unavailable or has an error, try again later; scanning is not affected.

---

## 12. Service and Calibration Tools

This section is for service technicians. These tools are unavailable (they report "busy") while a scan is preparing, prepared or running, and they report that the motion controller is missing if the Arduino is not connected.

### 12.1 What they offer

| Tool | Purpose |
|---|---|
| Endstops | Show the state of the bottom and top limit switches |
| Distance sensor | Show the live ToF distance in mm and whether the reading is valid |
| Lights | Switch the LEDs on or off, set brightness |
| Motors | Enable or disable the turntable, the carriage or both |
| Home / move to top | Move the carriage to the bottom or top limit |
| Carriage move | Move up or down by an exact distance (above 0 and up to 475 mm; stops with an error at a limit switch) |
| Turntable rotate | Rotate clockwise or counter-clockwise by an angle up to 360° (multiples of 0.1125°) |
| Camera preview and snapshot | Start or stop the preview, switch camera, change preview quality, check synchronization, take a test photo pair |
| Cleanup | Return the machine to idle: stream stopped, lights off |

### 12.2 Suggested check routine (derived)

1. Read the endstops: both show their expected idle state.
2. Enable the motors and home to the bottom; the bottom endstop turns active.
3. Move to the top; the top endstop turns active.
4. Rotate the turntable 360° and confirm that it returns to the same mark (missed-step check).
5. Read the distance sensor: the reading should be valid and plausible.
6. Switch on the lights and start the preview; check for banding and for the camera synchronization numbers.
7. Take a test photo pair (two photos expected).
8. Run Cleanup.

---

## 13. Understanding the Status Screen

### 13.1 Scanner states

| Status | What it means | What you do |
|---|---|---|
| Idle | Waiting | Prepare a scan |
| Preparing | Lights, cameras and homing in progress (15-30 s; up to about 2 min) | Wait |
| Prepared | Ready. The mode (object or face) says what for | Start (object) or Take photo (face) |
| Running | Scanning, capturing or uploading | Wait, or Cancel |
| Processing / Downloading | Only with the legacy cloud reconstruction | Wait |
| Completed | Done. For a face scan the result is shown | Prepare again |
| Cancelled | Stopped by you | Prepare again |
| Error | Failed. The reason is displayed | Read the message, then prepare again |

### 13.2 Phases and progress

The detailed phase can be: idle, preparing, prepared, scanning, uploading, verifying, done, error or cancelled. During an object scan you also see the **current level**, the **photos captured**, the **photos uploaded**, and a short message such as "Ring 2: capturing". Warnings from the distance sensor appear here too.

---

## 14. Troubleshooting

| Symptom or message | Likely cause | What to do |
|---|---|---|
| Prepare is refused, naming the **cloud**, the **distance sensor** or **disk space** | A pre-scan check failed | Check the internet connection; check that nothing blocks the distance sensor; free disk space on the scanner |
| "Motion controller (Arduino) not answering" | The motor controller is disconnected or off | Check the USB cable and power, restart the scanner. The scanner stays running and other functions remain usable |
| "Busy" (another scan or motion running, or emergency stop finishing) | A command was sent while the machine is occupied | Wait a few seconds and try again |
| Start is refused because the scanner is not prepared, or was prepared for a face scan | Wrong sequence | Press Prepare for an object scan first |
| Face capture refused because the scanner is not prepared for a face scan | Each face capture needs its own face prepare | Run Face prepare again |
| Face prepare refused: face detection missing | Face detection software or model not installed | Contact service |
| No face found (warning) | Face outside the search range | Prepare again with another start height |
| Preview does not show | Stream not started, or you opened the page before prepare finished | Wait for Prepared and reload the preview |
| Scan ends early | The distance sensor saw the back wall twice in a row | Normal if the object is shorter than expected. If not, check that dark or glossy surfaces are not confusing the sensor, and that the clearance (section 3.3) is respected |
| Distance sensor warning | The sensor reading was unreliable | Check the clearance; reflective or transparent objects can confuse it **(recommendation)** |
| Error: photos missing | Some photos did not reach the cloud | Check the connection; repeat the scan |
| Error: no upload progress | Upload stalled for 5 minutes | Check the network (section 10), then prepare again |
| Wi-Fi asks for the password again | Wrong or changed password | Enter it again; the saved network is not deleted |
| Network buttons are disabled | A scan is running | Wait until the scan has finished |
| Assistant disabled | Off or still starting | Read the displayed reason or wait |
| Model missing in the Library | Processing not finished, or that file was not produced | Wait, or open the object and check which versions exist |

If a message is not listed here, the machine always displays the exact reason for an error: note it down when contacting support.

---

## 15. Tips for Better Scans

These are general photogrammetry recommendations, not taken from the sources.

- **Texture matters.** Plain, uniform objects are hard for any photogrammetry system. Objects with some visible surface detail work best. The stereo rig constraint helps on weakly textured surfaces, but it does not replace detail.
- **Avoid shiny, mirror-like and transparent surfaces,** because reflections move with the camera and confuse the reconstruction. Lower the brightness for glossy parts.
- **Centre the object** and keep it fully inside the Ø260 mm table.
- **Keep the enclosure clean and plain.** Remove dust, marks and loose items from the white walls and the turntable.
- **Do not touch or move the object after Prepare.** Exposure and white balance are locked at that point.
- **Do a quick test at low resolution** (2028 × 1520, larger angle) to check framing, then a full-quality scan.
- **For tall objects,** use a smaller height step if the model shows gaps between levels.

---

## 16. Technical Specifications at a Glance

| Item | Specification |
|---|---|
| Display | 11.6" capacitive touchscreen (HDMI + USB touch) |
| Structure | 2020 aluminum extrusions, closed panels, white interior |
| Turntable | Ø260 mm, 0.1125° per microstep, 18° stop = 160 microsteps |
| Carriage travel | up to 475 mm, 0.01 mm resolution |
| Cameras | 2 × 12.3 MP (4056 × 3040), motorized focus, hardware-synchronized |
| Ground sampling distance | ≈ 0.12 mm per pixel |
| Depth precision | ≈ 0.17 mm |
| Distance sensor | 940 nm Class 1 laser ToF, back wall at 535 mm, footprint ≈ 237 mm diameter |
| Lighting | 12 V LED strips, 10 kHz PWM dimming |
| Warm-up gate | drift < 3% over 10 s, maximum 90 s |
| Autofocus | 35 focus points, central 40% area |
| Auto exposure / white balance | 2 s settle, then locked; master camera values copied to slave |
| Example scan | 200 mm object: 3 levels, 120 images, about 93 s of acquisition |
| Cloud | Ephemeral GPU pods; jobs ≤ 64 GB on consumer GPUs |
| Output | OBJ (high and low), STL, 3MF, GLB (Draco, up to 86.6% smaller) |
| Geometric accuracy of GLB compression | ≤ 7.9 µm |
| Connectivity | Ethernet, Wi-Fi, Bluetooth |

The sensor footprint figure matches a field of view of about 25° at 535 mm **(derived)**.

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **AE / AWB** | Auto exposure / auto white balance |
| **AF** | Autofocus |
| **Atlas (texture)** | One image that holds all the textures of the model |
| **Drift** | Slow change of light or camera output as parts warm up |
| **Edge device** | The Raspberry Pi inside the scanner |
| **GLB** | A compact 3D file format for web and AR |
| **GPU pod** | A temporary remote computer with a graphics card |
| **HMI** | Human-Machine Interface, here the touchscreen |
| **Level / ring** | One full turntable revolution at one height |
| **Master / slave camera** | The camera that sets exposure and colors, and the camera that copies them |
| **Non-manifold** | Broken mesh geometry that cannot be printed |
| **Photogrammetry** | Building 3D models from overlapping photos |
| **ToF** | Time-of-Flight distance sensor |
| **Trapezoidal velocity profile** | Smooth start, constant speed, smooth stop |
| **Watertight** | A closed mesh without holes, suitable for printing |

---

## 18. Known Limitations and Open Points

1. **Not yet confirmed on the rig:** the most recent software changes (face scan in two steps, Library, network panel) are covered by automated tests only.
2. **Cloud reconstruction path (open):** this manual describes provisioning an ephemeral cloud GPU after acquisition. The scanner's software interface, however, marks cloud reconstruction as a legacy option that is off by default, and lists models in a local Library. Confirm which path is the default.
3. **Formats (open):** OBJ and 3MF are described as deliverables, while the Library lists GLB and STL (low and high).
4. **Maximum object size (open):** the carriage travel is 475 mm and the table is Ø260 mm; a maximum object height is not specified.
5. **Planned, not yet available:** export via QR link or USB stick, thumbnails, deleting models.
6. **No user accounts:** the scanner's interface has no authentication; it is meant to be used on the device itself.
7. **Face scan:** it needs the face-detection software installed on the scanner, and each capture needs its own prepare.
