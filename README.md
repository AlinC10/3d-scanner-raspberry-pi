# 3D Scanner - Raspberry Pi 5 & Arduino Uno

A photogrammetry scanner with a turntable: a Raspberry Pi 5 runs the FastAPI server, the two Arducam IMX477 cameras (libcamera `rpi.sync`), the ToF sensor, the lights and the endstops, and an Arduino Uno generates the step pulses of the two NEMA 17 motors (turntable and vertical Z axis). The photos are uploaded to Cloudflare R2 and verified there. The 3D reconstruction is done by **Turntable Studio** (`../template_meshroom/turntable_studio`, a separate project: OpenMVS on camera poses locked from the AprilTag disc); with `cloud.launch_runpod: true` the scanner starts it on RunPod itself (`cloud/job.py`, photos under `jobs/<job_id>/` in R2, result extracted into `results/<job_id>/`); off by default. A face scan is reconstructed on the Pi (`rpi_face_mesh/`). A face scan (`POST /scanner/face/prepare`, then `POST /scanner/face/capture`) frames a face with MediaPipe and takes one stereo pair. The finished 3D objects in `results/` are served by `/library`.

---

## Running it

The scanner is meant to be used **from the Raspberry Pi itself**: the server listens on `127.0.0.1` only, so the API and the web page are not reachable from the network. There is no authentication and no CORS configuration, on purpose.

| What | Command | Listens on |
|---|---|---|
| Scanner (real hardware) | `fastapi dev` (uses `main_prod:app`, see `pyproject.toml`) or `python main_prod.py` | `127.0.0.1:8000` |
| Development on a PC (simulated rig) | `python main_dev.py` or `fastapi dev main_dev.py` | `127.0.0.1:8000` |

* `fastapi run` (production mode of the FastAPI CLI) listens on `0.0.0.0` by default, which makes the API visible to the whole network. If you use it, add `--host 127.0.0.1`.
* Check what the server listens on: `ss -ltn | grep 8000` shows `127.0.0.1:8000` when it is local only.
* Open `http://127.0.0.1:8000/docs` for the interactive API documentation.
* On the Pi the server runs as a systemd service and the UI in a `cage` kiosk without the desktop: setup in `kiosk/README.md`.
* The hardware can only be owned by one process: run a single worker.
* The server starts without the **Arduino**: every request that moves a motor answers `503` until the Arduino answers again (it is reconnected at every such request). It also starts without the **ToF sensor** (only the object scan needs it; it is reinitialised at `/scanner/prepare`) and without the **AI assistant** (`GET /api/ai/status` says why it is off).

The web page at `/` is the `3d-scanner-front-end` folder next to this project (or the folder in the `FRONTEND_DIR` environment variable). When the folder does not exist the server still starts, with a warning, and only the API is served.

### Development mode (`main_dev.py`)

`MOCK_HARDWARE=1` (set by `main_dev.py`) replaces the real `Scanner` with `hardware.scanner_mock.ScannerMock`: a simulated rig with the same states, status data and errors, in which nothing touches a motor, a camera, the cloud or the disk (except the placeholder photos of `POST /test/camera/snap`). `/scanner/prepare`, `/scanner/start`, `/scanner/cancel`, the live preview (placeholder frames) and the `/test` endpoints all work, and a simulated scan ends in `completed` after a few seconds. See the docstring of `hardware/scanner_mock.py`.

### Environment variables

| Variable | Meaning |
|---|---|
| `CAMERA_MODE` | `rpi_sync` (default), `xvs` or `staggered` |
| `FRONTEND_DIR` | Folder of the web page served at `/` |
| `PWM_CHIP` | Index of the PWM chip of the lights, when it cannot be found by itself |
| `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Cloudflare R2 credentials |
| `RUNPOD_API_KEY`, `TURNTABLE_IMAGE`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_R2_API_TOKEN` | Only for scans with `cloud.launch_runpod = true` (see `.env.example`; check with `POST /test/cloud/runpod-dry-run`) |
| `RESULTS_DIR` | Folder of the finished 3D objects served by `/library` (default `results/` in the project) |
| `GROQ_API_KEY` | AI assistant (`/api/ai/*`). Without it the assistant is off and the rest of the server works |
| `SCAN_PREFLIGHT=0` | Skips the preflight of `/scanner/prepare` (development only) |
| `SCAN_Z_HOME_SETTLE_S`, `SCAN_CAPTURE_ATTEMPTS`, `SCAN_MIN_FREE_DISK_MB`, `SCAN_UPLOAD_*`, `SCAN_BLACK_FRAME_*` | Timings and limits of the scan, see [`hardware/docs/SCANNER.md`](hardware/docs/SCANNER.md). Among them `SCAN_UPLOAD_ATTEMPT_TIMEOUT_S` (one upload attempt, 300 s) and `SCAN_UPLOAD_DRAIN_TIMEOUT_S` (no successful upload for this long after the scan ends the run in `error`, 300 s) |
| `CAMERA_CALL_TIMEOUT_S` | Deadline of a blocking libcamera call (default 60 s) |

### Tests

```
python -m unittest discover -s tests -t .
```
The tests run without hardware (the Pi-only libraries are replaced by mocks).

---

## Documentation

| Topic | File |
|---|---|
| Scanner state machine, scan loop, upload pipeline, safety | [`hardware/docs/SCANNER.md`](hardware/docs/SCANNER.md) |
| Scanner API (`/scanner/*`) | [`router/docs/SCANNER_API.md`](router/docs/SCANNER_API.md) |
| Hardware test API (`/test/*`) | [`router/docs/TEST_API.md`](router/docs/TEST_API.md) |
| Every endpoint, for the front-end team | [`router/docs/FRONTEND_API_REFERENCE.md`](router/docs/FRONTEND_API_REFERENCE.md) |
| API changes for the front-end team (2026-10-28 / 29) | [`router/docs/FRONTEND_CHANGES.md`](router/docs/FRONTEND_CHANGES.md) |
| Library of finished 3D objects (`/library/*`) | [`router/docs/LIBRARY_API.md`](router/docs/LIBRARY_API.md) |
| AI assistant API (`/api/ai/*`) | [`router/docs/AI_API.md`](router/docs/AI_API.md) |
| Wi-Fi / Bluetooth / Ethernet API | [`router/docs/SYSTEM_API.md`](router/docs/SYSTEM_API.md), [`system/README.md`](system/README.md) |
| Request / response schemas | [`schemas/README.md`](schemas/README.md) |
| Dual camera drivers (`rpi_sync`, `xvs`, `staggered`) | [`hardware/docs/DUAL_CAMERAS.md`](hardware/docs/DUAL_CAMERAS.md) |
| Single camera, focus (VCM) | [`hardware/docs/CAMERA.md`](hardware/docs/CAMERA.md), [`hardware/camera/docs/`](hardware/camera/docs/) |
| Motors, Arduino bridge and protocol, firmware | [`hardware/docs/MOTOR.md`](hardware/docs/MOTOR.md), [`hardware/docs/ARDUINO_BRIDGE.md`](hardware/docs/ARDUINO_BRIDGE.md), [`hardware/docs/ARDUINO_PROTOCOL.md`](hardware/docs/ARDUINO_PROTOCOL.md), [`hardware/arduino_control/README.md`](hardware/arduino_control/README.md) |
| ToF sensor, lights | [`hardware/docs/TOF.md`](hardware/docs/TOF.md), [`hardware/docs/LIGHTS.md`](hardware/docs/LIGHTS.md) |
| Open problems, by priority | [`PROBLEME_RAMASE.md`](PROBLEME_RAMASE.md) |
| Checklist before testing the motors | [`PRE_TEST_CHECKLIST.md`](PRE_TEST_CHECKLIST.md) |
| Plans (done and future) | [`plans/`](plans/) |

The `docs` folders above are the current documentation (the old agent instruction files `.aiassistant/`, `.agents/`, `.cursorrules`, `.github/copilot-instructions.md` were removed; see `AGENTS.md`).
