# 🤖 AI Agent Universal Rulebook: 3D Scanner (IoT Photogrammetry System)

**Apply these rules universally across the codebase. You are the chief architect and developer.**

## 1. 🏗️ SYSTEM CONTEXT & GLOBAL GUIDELINES
> 💡 **For complete architectural details, read:** `.aiassistant/rules/general.md`


### Project Overview
This project is an automated, hybrid 3D scanning system designed to digitize both static medium-sized objects and human faces (anatomy and facial features) at high resolution. It leverages a Raspberry Pi 5 to orchestrate hardware (motors, cameras) and streams datasets to Cloudflare R2, triggering RunPod Serverless GPUs to reconstruct the 3D model using Meshroom.

### Strict Coding Standards
1. **Object-Oriented Design (OOP):** All implementations must be highly modular and heavily rely on classes isolated in standalone files by domain (`hardware/`, `cloud/`, `routes/`, `ai/`).
2. **Typehints & Documentation:** ALWAYS write typehints. Use Sphinx / reST (reStructuredText) format for ALL docstrings.
3. **CURRENT DEVELOPMENT PHASE -> CLI & HARDWARE INTEGRATION:** We are currently in the CLI Testing Phase. Do NOT implement REST APIs, routing, or endpoints yet unless explicitly asked. Focus strictly on robust, non-blocking hardware control testing via `python main.py`.

---

## 2. ⚙️ HARDWARE LAYER (`/hardware`)
> 💡 **For complete hardware workflows and pinouts, read:** `.aiassistant/rules/hardware.md`

**SBC:** Raspberry Pi 5 8GB (Master controller)
**Optics:** 2x Arducam B0272 12MP IMX477 Motorized Focus HQ Cameras. Hardware synchronized via XVS (Master-Slave config, angled 30°-45°).
**Kinematics:** 2x NEMA17 Stepper Motors (1.5A, 1.8°/step) driven by DRV8825.
* Z-Axis Motor (T8x8 lead screw, MGN9 rails)
* Turntable Motor (Axial bearing)

### Hardware Rules
* Keep all GPIO/hardware-specific logic inside the `/hardware` classes.
* `main.py` should only instantiate objects and call high-level methods.
* **Libraries:** Use `picamera2` (optics) and `RpiMotorLib` (steppers).
* **Workflows to support:**
  - *Object Scan:* Homing Z-axis -> Synchronized stereo capture -> Turntable step (e.g., 18°) -> Repeat for 360° -> Move Z-axis up -> Repeat until no object.
  - *Face Scan:* Z-axis targets eyes -> Stereo capture -> Z-axis moves to mouth -> Stereo capture -> End (no turntable movement).
* **Error Handling:** Motors missing steps, endstops failing, or cameras dropping frames must be caught safely.

---

## 3. ☁️ CLOUD & PROCESSING LAYER (`/cloud`)
> 💡 **For complete meshroom and R2 pipeline details, read:** `.aiassistant/rules/cloud.md`
 - (Future Phase)
**Storage:** Cloudflare R2 Bucket using `boto3`.
**Compute:** RunPod Serverless (Meshroom cache branched to `PNG` high-poly and `JPG` low-poly).
* Encapsulate API logic in classes like `CloudflareR2`, `RunpodOrchestrator`.
* Uploads must be non-blocking `asyncio` or threaded so mechanical scanning never pauses for network IO.
* Local Raspberry Pi handles extracting the resulting `output.zip` and immediately cleaning up the zip to save SD space.

---

## 4. 🌐 ROUTING LAYER (`/routes`)
> 💡 **For complete API structure and endpoints, read:** `.aiassistant/rules/routes.md`
 - (Future Phase)
**Framework:** FastAPI.
* **Entry point:** `main.py` bootstraps hardware, mounts routers (`app.include_router()`), and serves HMI.
* **Rule:** Routers in `/routes` must be incredibly thin. They act only as interfaces/controllers calling the underlying hardware/cloud classes injected via `Depends` or app state.
* Use Pydantic schemas for payload validation. 

### Planned API Endpoints (Context for Hardware Design)
*Even though routing is NOT yet implemented, build the hardware and cloud classes so they naturally expose methods to support these future endpoints:*

**1. Scanner Control (`/scan_routes.py`)**
* `POST /scan/start` -> Triggers `ScannerController.start_scan(mode="object"|"face")`
* `POST /scan/stop` -> Triggers `ScannerController.emergency_stop()`
* `GET /scan/status` -> Returns active state, motor position, and upload queue progress.

**2. Hardware & Network Auth (`/hardware_routes.py`)**
* `GET /hardware/network/wifi` -> Lists available SSIDs.
* `POST /hardware/network/wifi` -> Connects to a network.
* `POST /hardware/motor/jog` -> Manual jog for Turntable/Z-Axis (used for HMI calibration).
* `POST /hardware/relay/toggle` -> Turns LED lights on/off.

**3. Processing & Output (`/model_routes.py`)**
* `GET /models` -> Lists locally extracted models.
* `GET /models/{scan_id}/status` -> Polls `RunpodOrchestrator` for Meshroom execution status.
* `GET /models/{scan_id}/export/{format}` -> Serves `.obj`, `.glb`, or `.stl` to the UI.

---

## 5. 🧠 AI & RAG LAYER (`/ai`)
> 💡 **For complete RAG and ChromaDB details, read:** `.aiassistant/rules/ai.md`

**Tech Stack:** Python 3.13, LangChain, ChatGroq, ChromaDB, MarkItDown.
* **RAG Flow:** 
  - *Ingestion (CLI):* PDF -> MarkItDown -> RecursiveCharacterTextSplitter (500 chars / 50 overlap) -> Embeddings -> ChromaDB.
  - *Retrieval (API):* Similarity search (top-k=3/4) -> Context injection -> ChatGroq Generation.
