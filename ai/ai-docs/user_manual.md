# Automated 3D Scanner - Comprehensive User Manual

## 1. System Overview
The automated 3D scanner is an advanced, edge-to-cloud photogrammetric device designed to acquire geometrically complete and photometrically consistent image sets without operator intervention. It utilizes an elastic cloud computing model, meaning the heavy photogrammetric reconstruction algorithms are executed on remote, dynamically provisioned GPU instances rather than requiring an expensive local workstation. The entire system is operated autonomously via an integrated 11.6" capacitive touchscreen Human-Machine Interface (HMI) with an HDMI and USB touch interface.

## 2. Hardware Environment and Object Placement
The physical structure of the scanner is built using 2020 aluminum extrusions and closed panels. 
*   **Enclosure**: The internal surface is white to provide a uniform, feature-poor background. This is critical; a textured static background would produce features that contradict the rotating-object motion model during the 3D reconstruction phase.
*   **Turntable**: The object to be scanned must be placed on the Ø260 mm turntable. 
*   **Safety and Clearances**: Ensure the background wall is clear of obstructions. The system uses a 940 nm Class 1 laser Time-of-Flight (ToF) sensor to detect the object. This sensor looks across the turntable toward the back wall located at 535 mm, with a sensing footprint of approximately 237 mm in diameter at the back wall. The device operates safely with no moving parts outside the closed enclosure.

## 3. Preparation and Acquisition Phase
Once the scan is initiated from the touchscreen, the system runs through a fully autonomous sequence.
1.  **Thermal-Photometric Stabilization**: LED lights lose luminous flux as their junction warms up, and low-cost cameras experience internal drift. The scanner actively measures this drift using the camera's exposure-gain product. It automatically waits until the relative drift stabilizes below 3% over a 10-second window (with a maximum timeout of 90 seconds) before proceeding.
2.  **Autofocus & Photometric Locking**: Both cameras perform a contrast-based Autofocus (AF) sweep, testing 35 focal points to maximize sharpness in the central 40% of the image. Following this, Auto Exposure (AE) and Auto White Balance (AWB) converge for 2 seconds and are locked. To prevent color and luminance seams, the parameters from the master camera are forcefully inherited by the slave camera.
3.  **Adaptive Hardware Acquisition**: The turntable rotates using a smooth trapezoidal velocity profile. Instead of blindly scanning the entire vertical axis, the ToF sensor constantly checks the object's height. If two consecutive readings indicate the sensor is looking past the object at the back wall, the scan terminates immediately to save time. For example, a 200 mm tall object typically requires 3 vertical levels (120 images) and takes roughly 93 seconds of mechanical acquisition.
4.  **Concurrent Upload**: Images are uploaded to cloud storage in the background while the scanner is still moving, effectively hiding network transfer times.

## 4. Cloud Processing and Asset Delivery
After acquisition, the edge device provisions an ephemeral cloud GPU pod. The user can track the progress and download the resulting 3D models via the "Library" interface. 

The system provides multi-format delivery tailored to different use cases:
*   **OBJ Format**: Delivered in two versions. A high-polygon mesh with a 4096x4096 lossless PNG texture atlas, and a low-polygon decimated mesh (capped at 50,000 vertices) with a 2048x2048 JPEG texture.
*   **STL & 3MF Formats**: Optimized for 3D printing. The system automatically repairs non-manifold geometry, filling holes to ensure the exported mesh is watertight.
*   **GLB Format**: Optimized for web and AR applications. The geometry is heavily compressed using the Draco algorithm, reducing file size by up to 86.6% (e.g., from 35.1 MB to 4.7 MB) while maintaining a visually and metrically lossless maximum position error of ≤ 7.9 µm.