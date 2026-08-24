---
apply: by file patterns
instructions: Hardware, Raspberry Pi, GPIO, Stepper Motors, Camera, Libcamera, Sensors, Endstops Apply these rules whenever the prompt mentions physical components, pinouts, motors, video capture, or low-level control.
patterns: hardware/**
---

# AI AGENT CONTEXT: 3D Scanner - Hardware & Control Layer
**Project:** IoT Photogrammetry 3D Scanner (Edge Device: Raspberry Pi 5)

**Focus:** Hardware abstraction, motor kinematics, camera synchronization, and local CLI control.

## 1. SYSTEM OVERVIEW
This system is an automated, hybrid 3D scanner designed to capture image datasets for photogrammetry. It operates in two physical modes: **Object Scanning** (turntable + Z-axis) and **Face Scanning** (Z-axis only). The brain of the hardware is a Raspberry Pi 5 (8GB) that concurrently orchestrates stepper motors, dual synchronized cameras, and lighting relays.

Currently, the development is focused strictly on the **CLI/Hardware layer**. APIs, endpoints, and cloud sync are out of scope for the hardware classes and will be implemented later.

## 2. HARDWARE SPECIFICATIONS
*   **SBC:** Raspberry Pi 5 8GB (Master controller).
*   **Optics:** 2x Arducam B0272 12MP IMX477 Motorized Focus HQ Cameras. 
    *   Mounted on a horizontal bracket at a 30° to 45° angle to each other.
    *   Hardware synchronized via XVS (Master-Slave configuration) to ensure simultaneous capture from both angles.
    *   Features
        *   12MP IMX477 High Quality Camera - The same image sensor used in Raspberry Pi High Quality Camera. Natively works with existing commands, codes and examples.
        *   I2C Focus Control - The focusing process is controlled via software instead of your bare hands. Use keyboard arrows keys to adjust the focus to the best or OpenCV autofocus examples to automate it.
        *   Focus Distance: 80mm to infinity

*   **Kinematics (Motors & Mechanics):** 2x NEMA17 Stepper Motors (1.5A, 1.8°/step) driven by **DRV8825** drivers.
    *   **Z-Axis Motor:** Moves the dual-camera bracket vertically. Uses a T8x8 (P2) trapezoidal lead screw with an MGN9 linear guide rail system.
    *   **Turntable Motor:** Rotates the central scanning platform. Supported by a heavy-duty unidirectional axial bearing (120/155mm).
*   **Peripherals:** 
    *   Mechanical Endstops (for Z-axis homing).
    *   LED Strips (diffuse lighting) controlled via a 5V Relay module.
    *   Buzzer (for status notifications).

## 3. SOFTWARE ARCHITECTURE & MODULES
The software follows a strict Object-Oriented design. Each hardware component has its own class, isolated in specific files (e.g., `/hardware/camera.py`, `/hardware/motor.py`). 

### Existing Base Classes (DO NOT reinvent, extend these):
*   **`Camera` Class:** 
    *   Wrapper for individual camera control.
    *   **Core Library:** `picamera2` (`Picamera2`, `H264Encoder`, `FileOutput`).
*   **`DualCamera` Class:** 
    *   Composes two `Camera` objects.
    *   Responsible for hardware-synced dual triggering (Master-Slave logic).
*   **`Motor` Class:** 
    *   Wrapper for stepper motor control (Z-axis and Turntable).
    *   **Core Library:** `RpiMotorLib` (specifically `RpiMotorLib` for DRV8825).
    *   **Types used:** `typing.Tuple`.

### Classes to be developed/refined by AI:
*   **`RelayControl` / `LightControl`:** For the 5V LED relay.
*   **`Endstop` / `LimitSwitch`:** For Z-axis homing logic via GPIO.
*   **`ScannerController` (Main Logic):** A state machine or controller class that orchestrates the Motors, DualCamera, and Lights based on the selected mode.

## 4. HARDWARE WORKFLOWS (CLI LEVEL)
The system supports two hardcoded sequences that the AI must help implement and optimize:

### Mode A: Object Scan
1.  **Initialize:** Turn on LED relay.
2.  **Home:** Move Z-axis motor down until the mechanical endstop is triggered (Zero position).
3.  **Scan Loop:** 
    *   Move Z-axis to calculated vertical position.
    *   Trigger `DualCamera` to capture synchronized stereoscopic images.
    *   Rotate Turntable motor by a predefined step (e.g., 18 degrees).
    *   Repeat camera trigger and rotation until a full 360° is completed.
    *   *Condition:* Continue moving Z-axis up and repeating 360° slices until no object is detected in frame (or a predefined height limit is reached).
4.  **Finalize:** Turn off LEDs, trigger buzzer, return Z-axis to home.

### Mode B: Face Scan (Human Subject)
1.  **Initialize:** Turn on LED relay.
2.  **Targeting:** Z-axis moves to find the subject's eyes (eye-level alignment).
3.  **Scan Loop:**
    *   Turntable remains strictly stationary.
    *   Trigger `DualCamera` at eye level.
    *   Move Z-axis down to the second/final position (mouth level).
    *   Trigger `DualCamera` again.
4.  **Finalize:** Turn off LEDs, trigger buzzer.

## 5. AI CODING GUIDELINES & CONSTRAINTS
*   **Hardware Isolation:** Keep all GPIO and hardware-specific logic inside the `/hardware` directory classes. `main.py` should only instantiate these objects and call high-level methods like `scanner.start_object_scan()`.
*   **Libraries:** Rely on `picamera2` for optics and `RpiMotorLib` for steppers. Assume standard Python built-ins (`time`, `os`, `threading`) are available; do not list them as requirements unless explaining a specific concurrency model. Check every library used to be compatible with the `Raspberry PI 5` (can check library documentation, GitHub repository), and if is not compatible, do not use it and search for alternatives.
*   **Non-blocking Execution:** The hardware sequences (especially camera capture) must be designed to eventually allow concurrent operations (e.g., adding pictures to a queue for background cloud upload). Use threading or asyncio where appropriate, but keep the initial CLI implementations simple and robust.
*   **Error Handling:** Motors missing steps, endstops failing, or cameras dropping frames must be caught. Provide safe fallbacks (e.g., immediately stop motors if a limit switch is bypassed).
