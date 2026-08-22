---
apply: always
---

# SYSTEM CONTEXT: 3D Scanner – IoT Photogrammetry System

## 1. Project Overview
This project is an automated, hybrid 3D scanning system designed to digitize both static medium-sized objects and human faces (anatomy and facial features) at high resolution. 

It utilizes an IoT-oriented modular architecture, combining the mechanical precision of stepper motors with advanced Computer Vision. To bypass local processing limitations, the system uses an edge-to-cloud data pipeline: images are captured and hardware-synchronized by a Raspberry Pi, asynchronously streamed to a cloud storage bucket, and processed into 3D models via photogrammetry algorithms (Meshroom) on dedicated cloud GPUs.

**Primary Use Cases:** E-commerce (3D/AR product models), MedTech (non-invasive facial topography for custom prosthetics/planning), GameDev/VFX (photorealistic assets), Digital Archiving, and Reverse Engineering.

---

## 2. Hardware Architecture
The physical system is orchestrated entirely by the edge device, functioning as the central controller and image buffer.

*   **Main Controller:** Raspberry Pi 5 (8GB RAM) – manages mechanical coordination, camera triggering, and runs complex Python scripts asynchronously to prevent blocking.
*   **HMI (Human-Machine Interface):** 11.6-inch Capacitive Touchscreen (1366x768). Runs a Web UI (JavaScript) allowing users to select scanning profiles, start processes, and preview status without a connected laptop.
*   **Vision System:** 2x Arducam B0272 12MP IMX477 (Motorized Focus).
    *   Mounted horizontally on the Z-axis with a 30° or 45° converging angle.
    *   Operates in a Master-Slave configuration synchronized via XVS to capture extended geometry in a single trigger.
*   **Kinematics (Motors & Drivers):** 2x NEMA17 stepper motors (1.5A, 1.8°) driven by DRV8825 drivers.
    *   **Z-Axis Motor:** Moves the horizontal camera mount vertically.
    *   **Rotary Motor:** Spins the central platform.
*   **Mechanical Structure:** 
    *   Base platform uses a large unidirectional axial bearing (120/155mm) for smooth rotation.
    *   Vertical Z-axis uses a T8x8 P2 trapezoidal right-hand threaded rod with a nut, guided by two linear rails with MGN9 carriages.
*   **Lighting & Feedback:** LED strips controlled via a 5V relay for optimal diffuse lighting, plus an active buzzer for status notifications.

---

## 3. Cloud & Processing Stack
*   **Storage:** Cloudflare R2 Bucket.
*   **Compute:** RunPod Serverless (GPU instances).
*   **Pipeline:** 
    1. Images are buffered in memory/locally on the RPi.
    2. Uploaded sequentially and concurrently via Wi-Fi to Cloudflare R2 while the scanner continues hardware operations.
    3. RunPod server retrieves images, runs Meshroom (point cloud reconstruction and texturing).
    4. Finished 3D models are uploaded back to R2 and downloaded locally to the Raspberry Pi.
*   **Export Formats:** `.obj` (+ texture images), `.glb`, `.stl` (for 3D printing). Both high-poly and low-poly versions are generated.

---

## 4. Core Workflows

### Phase 1: Initialization & Calibration
User selects the scanning profile (Object or Face) via the HMI.
*   **Object Mode:** Motors auto-calibrate to mechanical endstops (home position). LED relay is activated for diffuse lighting.
*   **Face Mode:** Cameras utilize facial recognition to locate the subject's eyes and automatically adjust the Z-axis to eye level. LED relay is activated.

### Phase 2: Scanning Loop
The Raspberry Pi coordinates subsystems simultaneously. 
*   **Object Mode:**
    *   Z-axis moves to the target vertical position.
    *   Rotary platform turns by a predefined step (e.g., 18 degrees).
    *   Cameras trigger synchronously.
    *   Background thread uploads captured pairs to Cloudflare R2.
    *   *Termination:* The loop repeats until the cameras no longer detect an object in the frame.
*   **Face Mode:**
    *   Rotary platform remains stationary.
    *   Z-axis moves downward to the subject's mouth.
    *   Cameras trigger synchronously.
    *   Background thread uploads captured pairs to Cloudflare R2.
    *   *Termination:* The loop ends.

### Phase 3: Processing & Output
*   RunPod takes over processing via Meshroom.
*   Upon completion, the RPi downloads the model.
*   HMI triggers a visual notification and buzzer sound. User can preview or export the files.

---

## 5. Codebase Architecture & Agent Guidelines

### 5.1 Object-Oriented Design (OOP)
All implementations must be highly modular and heavily rely on classes. Code must be organized in standalone files and specific directories representing their domain.
*   `hardware/`: Must contain classes for physical interactions (e.g., `Motor`, `Camera`, `Relay`, `DualCamera`, `WiFiBluetoothManager`).
*   `cloud/`: (Future phase) Must handle API interactions, asynchronous R2 uploads, and RunPod polling.
*   `routes/`: (Future phase) Will contain routing and endpoints connecting the UI to hardware classes.

### 5.2 Application State & Server
*   The application will eventually spin up a local web server (via a `main.py` entry point) binding the routes to the hardware control logic.
*   The system must expose Wi-Fi and Bluetooth configuration capabilities directly through the UI endpoints.
* Write typehints, docstring with the Sphinx / reST (reStructuredText) format and add comments where is necessary for better understanding.

### 5.3 CURRENT DEVELOPMENT PHASE: CLI & Hardware Integration
*   **IMPORTANT FOR AI AGENTS:** We are currently in the **CLI Testing Phase**.
*   Do NOT implement REST APIs, routing, or endpoints yet.
*   Focus strictly on writing robust, non-blocking hardware control classes (motors, cameras, relays) and testing them via `python main.py`.