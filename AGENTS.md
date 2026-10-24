# AGENTS.md

Instructions for AI coding agents working in this repository. Read this first; it points to the documentation that is the source of truth instead of repeating it.

## What this is

A photogrammetry scanner with a turntable. A **Raspberry Pi 5 (Trixie OS)** runs a FastAPI server, two Arducam IMX477 cameras (libcamera `rpi.sync`, motorized focus), a VL53L1X ToF sensor, relay + PWM lights and two endstops. An **Arduino Uno** generates the step pulses of two NEMA 17 motors (turntable, vertical Z axis) through TB6600 drivers, over serial. Photos are uploaded to Cloudflare R2 and verified there; 3D reconstruction (Meshroom on RunPod) is optional and **off by default**.

The owner works in Romanian. `PROBLEME_RAMASE.md` is written in Romanian without diacritics; the documentation under `*/docs/` and the code are in English.

## Rules for this repository

1. **Do not commit or push.** The owner makes every commit. Leave changes in the working tree.
2. **Hardware safety comes first.** The Arduino universal STOP is the byte `0xFF`; it cuts every motor. Never send it while a move you do not want to stop is running, and never add a motor command that is not inside `Scanner.motion_guard()`. Endstop callbacks only act while a Z move is in flight. See `hardware/docs/SCANNER.md` (safety) and `hardware/docs/ARDUINO_PROTOCOL.md`.
3. **The server is local only.** It listens on `127.0.0.1` and has no authentication or CORS on purpose (it is used from the Pi itself). Do not make it listen on `0.0.0.0` and do not add CORS.
4. **RunPod bills by the minute.** In `cloud/runpod.py` every pod must be terminated in a `finally`, and the number of attempts stays bounded. Do not run anything that creates a pod.
5. **Never print or commit secrets** (`.env`, R2 and RunPod keys, `HF_TOKEN`).
6. **`requirements.txt` is not pinned on purpose** and is a `pip freeze` of the PC; do not pin it or clean it without being asked.
7. **Keep the documentation true.** A change to behaviour, an endpoint or a schema field updates the matching document in the same change (see the map below). When you fix or find a problem, update `PROBLEME_RAMASE.md` (move it to section 5 or add it at its level).
8. Say what you verified and what you did not. Nothing here has been run against real hardware by an agent: say "covered by tests, not confirmed on the rig".

## Commands

| What | Command |
|---|---|
| Run on the Pi (real hardware) | `fastapi dev` (entrypoint `main_prod:app`, listens on `127.0.0.1:8000`) or `python main_prod.py`. Avoid `fastapi run` (listens on `0.0.0.0`) unless `--host 127.0.0.1` |
| Develop on a PC (simulated rig) | `python main_dev.py` or `fastapi dev main_dev.py` (`MOCK_HARDWARE=1`, `hardware/scanner_mock.py`) |
| Tests (no hardware needed) | `python -m unittest discover -s tests -t .` |
| API documentation while running | `http://127.0.0.1:8000/docs` |

Python 3.13. All tests must pass before you finish; the suite takes about 30 seconds. `tests/__init__.py` replaces the Pi-only libraries (and `pydantic`, `fastapi`, `hardware.motor`, ...) with mocks, so tests that need the real thing either load the file by path (`importlib`) or run a subprocess (see `tests/test_prepare_request_schema.py`, `tests/test_dev_app.py`, `tests/test_runpod_launch.py`).

## Where the truth lives

| Topic | Read |
|---|---|
| Scanner state machine, prepare / scan phases, upload and verification, cancel and emergency stop | `hardware/docs/SCANNER.md` |
| HTTP API of the scan (`/scanner/*`) and of the manual tests (`/test/*`) | `router/docs/SCANNER_API.md`, `router/docs/TEST_API.md` |
| Request / response models and their validation | `schemas/README.md`, `schemas/scanner.py` |
| Dual camera drivers (`rpi_sync` is the production one), the `BaseDualCamera` interface and its capability flags | `hardware/docs/DUAL_CAMERAS.md`, `hardware/docs/CAMERA.md` |
| Motors, Arduino bridge and serial protocol, firmware | `hardware/docs/MOTOR.md`, `hardware/docs/ARDUINO_BRIDGE.md`, `hardware/docs/ARDUINO_PROTOCOL.md`, `hardware/arduino_control/README.md` |
| ToF sensor and the object / backdrop threshold, lights | `hardware/docs/TOF.md`, `hardware/docs/LIGHTS.md` |
| Wi-Fi / Bluetooth / Ethernet | `system/README.md`, `router/docs/SYSTEM_API.md` |
| Mechanical limits of the rig | `hardware/rig_limits.py` |
| Open problems by priority, decisions already taken, what was fixed and what is unconfirmed | `PROBLEME_RAMASE.md` |
| Procedure before testing the motors on the rig | `PRE_TEST_CHECKLIST.md` |
| Plans (done and future) | `plans/` |

The files `.cursorrules`, `.github/copilot-instructions.md`, `.agents/skills/rules.md` and `.aiassistant/rules/*` describe an older architecture and are **obsolete**; ignore them.

## Architecture in five lines

* `router/*.py` are thin: validate with the schemas, call `Scanner`, map errors to HTTP codes (409 = busy or stopping, 400 = refused state or configuration, 422 = invalid body). The preview endpoints exist once (`router/scanner.py`) and are also registered under `/test/stream/*`.
* `hardware/scanner.py` (`Scanner`) owns the rig: state machine, exclusive motion (`motion_guard`), cooperative cancel (`_cancel_event`), the background upload pipeline (retry, parking, drain, verification against a manifest).
* A request is validated by the same functions the hardware uses (`angle_to_steps`, `distance_to_steps`), so a bad value is a 422 before anything moves.
* New request fields go through four places: the schema, the router, `Scanner` (and the camera interface if it concerns the cameras), and the docs; add tests for each.
* Drivers other than `rpi_sync` are secondary: a new camera feature is declared in `BaseDualCamera` first and implemented in `rpi_sync`; note in `PROBLEME_RAMASE.md` what the other drivers still lack.

## Style

* Type hints everywhere; docstrings in reST (`:param x:`, `:raises E:`). Match the style of the surrounding code (comment density, naming).
* Hardware-specific code stays in `hardware/`; no GPIO or serial access from routers.
* Prefer small, tested changes. Tests use `unittest`; for hardware behaviour use fakes that script the device, not `MagicMock` returning whatever.
