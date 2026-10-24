# 3D Scanner - Raspberry Pi 5 & Arduino Uno

A photogrammetry scanner with a turntable: a Raspberry Pi 5 runs the FastAPI server, the two Arducam IMX477 cameras (libcamera `rpi.sync`), the ToF sensor, the lights and the endstops, and an Arduino Uno generates the step pulses of the two NEMA 17 motors (turntable and vertical Z axis). The photos are uploaded to Cloudflare R2 and verified there; the 3D reconstruction (Meshroom on RunPod) is optional and off by default.

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
* The hardware can only be owned by one process: run a single worker.

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
| `RUNPOD_API_KEY` | Only for scans with `cloud.launch_runpod = true` |
| `SCAN_PREFLIGHT=0` | Skips the preflight of `/scanner/prepare` (development only) |
| `SCAN_Z_HOME_SETTLE_S`, `SCAN_CAPTURE_ATTEMPTS`, `SCAN_MIN_FREE_DISK_MB`, `SCAN_UPLOAD_*`, `SCAN_BLACK_FRAME_*` | Timings and limits of the scan, see [`hardware/docs/SCANNER.md`](hardware/docs/SCANNER.md) |
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
| Wi-Fi / Bluetooth / Ethernet API | [`router/docs/SYSTEM_API.md`](router/docs/SYSTEM_API.md), [`system/README.md`](system/README.md) |
| Request / response schemas | [`schemas/README.md`](schemas/README.md) |
| Dual camera drivers (`rpi_sync`, `xvs`, `staggered`) | [`hardware/docs/DUAL_CAMERAS.md`](hardware/docs/DUAL_CAMERAS.md) |
| Single camera, focus (VCM) | [`hardware/docs/CAMERA.md`](hardware/docs/CAMERA.md), [`hardware/camera/docs/`](hardware/camera/docs/) |
| Motors, Arduino bridge and protocol, firmware | [`hardware/docs/MOTOR.md`](hardware/docs/MOTOR.md), [`hardware/docs/ARDUINO_BRIDGE.md`](hardware/docs/ARDUINO_BRIDGE.md), [`hardware/docs/ARDUINO_PROTOCOL.md`](hardware/docs/ARDUINO_PROTOCOL.md), [`hardware/arduino_control/README.md`](hardware/arduino_control/README.md) |
| ToF sensor, lights | [`hardware/docs/TOF.md`](hardware/docs/TOF.md), [`hardware/docs/LIGHTS.md`](hardware/docs/LIGHTS.md) |
| Open problems, by priority | [`PROBLEME_RAMASE.md`](PROBLEME_RAMASE.md) |
| Checklist before testing the motors | [`PRE_TEST_CHECKLIST.md`](PRE_TEST_CHECKLIST.md) |
| Plans (done and future) | [`plans/`](plans/) |

The files in `.aiassistant/`, `.agents/`, `.cursorrules` and `.github/copilot-instructions.md` describe an older architecture; the `docs` folders above are the current ones.
