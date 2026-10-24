# Face Scan Module

This directory contains the facial detection, stereo landmark tracking, and autonomous auto-framing subsystem for the 3D photogrammetry scanner.

## Components

- **`face_scanner.py`**: High-performance MediaPipe FaceLandmarker wrapper extracting 478 face landmarks, blendshapes, bounding boxes, and canonical 3D facial mesh (.OBJ).
- **`core/`**:
  - `framing_analyzer.py` (`StereoFramingAnalyzer`): Evaluates dual-camera stereo framing at 27° convergence and 15° pitch down to compute centering scores and apex height.
- **`orchestrator/`**:
  - `orchestrator_engine.py` (`FaceScannerOrchestrator`): Coordinates carriage motion, vibration settling, synchronized 12.3MP photo capture, and optional R2 sync.
  - `cli.py`: Standalone CLI runner for the orchestrator.
- **`hardware_bridge.py`**: Safe hardware bridge mapping to `hardware.motor`, `hardware.endstop`, `hardware.dual_camera_rpi_sync`, and mock simulations for desktop testing.
- **`rig_capture.py`**: Standalone command-line runner using `hardware.scanner.Scanner` (real hardware) or `ScannerMock`.
- **`run_capture.sh`**: Helper shell script that resolves the workspace virtual environment (`.venv`) and runs `rig_capture.py`.
- **`face_landmarker.task`**: Pre-trained Google MediaPipe face landmarker model.

For operating instructions and requirements on Raspberry Pi 5, see [GHID_SI_REQUIREMENTS_RPI_CAPTURE.md](../plans/implemented/GHID_SI_REQUIREMENTS_RPI_CAPTURE.md).
