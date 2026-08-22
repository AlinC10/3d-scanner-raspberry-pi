---
apply: by file patterns
instructions: # SCOPE: REST API, FastAPI, Flask, Endpoints, HTTP Routes, JSON Schemas, Request Validation Apply these rules whenever the prompt touches web routing, controllers, API responses, or client requests.
patterns: routes/**
---

# SYSTEM INSTRUCTIONS: API & Routing Architecture (FastAPI)

## 1. System Overview
This module governs the software architecture, API design, and routing for a Hybrid 3D IoT Scanner based on Photogrammetry. The system captures high-resolution datasets for medium-sized static objects and human faces. 

While the system relies on physical components (Raspberry Pi 5, NEMA17 motors, Arducam cameras, LED relays) and cloud services (RunPod/Meshroom, Cloudflare R2), **this document focuses strictly on the software application layer, specifically the FastAPI implementation, route structuring, and orchestration.**

## 2. Architectural Guidelines

### 2.1. Object-Oriented Design & Separation of Concerns
- The codebase strictly follows an Object-Oriented Programming (OOP) paradigm.
- Hardware implementations (e.g., `Camera`, `Motor`, `Relay`, `WiFi`, `Bluetooth`) must reside in dedicated files and directories (e.g., `/hardware`).
- The API layer (`/routes`) must remain thin. Routes should only act as interfaces that call methods from the instantiated hardware/logic classes.

### 2.2. The Entry Point: `main.py`
`main.py` is the absolute core of the application. Its responsibilities include:
1. **Hardware Initialization:** Bootstrapping and instantiating all physical components (cameras, motors, lights) upon startup.
2. **Server Initialization:** Creating the FastAPI application instance (`app = FastAPI()`) to serve as the local server.
3. **Router Mounting:** Importing all route modules from the `routes/` directory and including them into the main FastAPI app (using `app.include_router()`).
4. **HMI Serving:** Serving the Web HMI (JavaScript Touchscreen Interface) alongside the API.
5. **Global Context:** Managing the global state of the scanner and passing necessary hardware instances to the route handlers.

### 2.3. Asynchronous Operations
- **Non-Blocking Architecture:** Hardware movements (motors) and optical captures (cameras) must run efficiently without blocking the event loop.
- **Concurrent Uploads:** Captured images must be pushed to an asynchronous queue immediately after capture. A background worker will handle pushing these images to the Cloudflare R2 bucket concurrently, ensuring the mechanical scanning loop is never delayed by network latency.

## 3. Directory & Route Structure

### Expected Structure
```text
/
├── main.py                 # App entry point, FastAPI setup, hardware init
├── hardware/               # Hardware abstraction classes
│   ├── motor.py
│   ├── camera.py
│   ├── network.py          # Wi-Fi / Bluetooth management
│   └── ...
└── routes/                 # FastAPI routers
    ├── scan_routes.py      # /scan endpoints (start, stop, status)
    ├── hardware_routes.py  # /hardware endpoints (wifi, bt, manual control)
    ├── model_routes.py     # /models endpoints (download, list, export)
    └── ...
```

### 3.1. Scan Workflow Routes (`/scan`)
These routes control the primary logic loops of the scanner.
- **Profiles:** Must handle both `Object` and `Face` scan profiles.
- **Workflow Steps (handled by backend logic triggered via API):**
  1. **Calibration/Homing:** 
     - *Object Mode:* Motors find origin via mechanical endstops.
     - *Face Mode:* Cameras use Computer Vision to detect the subject's eyes and adjust Z-axis accordingly.
  2. **Lighting:** Activate LED strips via relay for diffuse lighting.
  3. **Scan Loop:** 
     - Coordinate Z-axis (camera arm) and rotational (platter) movements.
     - Note: Face mode requires no platter rotation; Object mode loops until the object is no longer detected.
     - Trigger synchronous Master-Slave camera captures.

### 3.2. Network & Hardware Configuration Routes (`/hardware`)
- **Accessibility:** Must expose endpoints to scan and connect to Wi-Fi and Bluetooth directly from the HMI.
- **Manual Overrides:** Endpoints to manually jog motors or test LEDs/cameras for calibration and maintenance.

### 3.3. Post-Processing & Model Routes (`/models`)
- **Cloud Communication:** Although processing happens externally on RunPod (Meshroom GPU), the API must track the job status.
- **Delivery:** Once the Cloudflare R2 bucket has the processed 3D model, the local server downloads it automatically.
- **Notification:** Trigger a hardware buzzer and an HMI WebSocket/SSE notification upon completion.
- **Export Formats:** Endpoints to serve the models to the HMI in various formats (High/Low poly, `.obj` + textures, `.glb`, `.stl` for 3D printing).

## 4. Development Instructions for AI Agents
1. **Focus on FastAPI:** Use `APIRouter` for all files in the `/routes` directory.
2. **Dependency Injection:** Use FastAPI's `Depends` or application state to pass initialized hardware objects from `main.py` to the routes.
3. **Pydantic Models:** Use Pydantic schemas for all incoming payloads (e.g., scan configuration, Wi-Fi credentials) and outgoing responses.
4. **Error Handling:** Ensure hardware failures (e.g., motor stall, camera disconnect) are caught and gracefully returned to the HMI via appropriate HTTP status codes and error messages.
