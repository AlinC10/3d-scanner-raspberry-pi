# Face Scan Module

This directory contains the facial detection, stereo landmark tracking, and autonomous auto-framing subsystem for the 3D photogrammetry scanner.

## Components

- **`face_scanner.py`**: High-performance MediaPipe FaceLandmarker wrapper extracting 478 face landmarks, blendshapes, bounding boxes, and canonical 3D facial mesh (.OBJ). `face_detection_problem()` tells, without building a detector, whether MediaPipe, OpenCV and the model are there (checked before the carriage moves). The detector holds the model in memory: use `with FaceScanner(...)`; the scanner builds one per height search and closes it after.
- **`core/`**:
  - `framing_analyzer.py` (`StereoFramingAnalyzer`): Evaluates dual-camera stereo framing at 27° convergence and 15° pitch down to compute centering scores and apex height. The score of one camera adds centering, face size, the cheek profile and the share of the 478 landmarks inside the frame (`visible_ratio`, weight `VISIBILITY_WEIGHT`); it also returns the face box as frame fractions (`bbox_norm`, used for the autofocus).
- **`rig_capture.py`**: Standalone command-line runner using `hardware.scanner.Scanner` (real hardware) or `ScannerMock`: the face prepare, then the capture, like the API.
- **`run_capture.sh`**: Helper shell script that resolves the workspace virtual environment (`.venv`) and runs `rig_capture.py`.
- **`face_landmarker.task`**: Pre-trained Google MediaPipe face landmarker model.

For operating instructions and requirements on Raspberry Pi 5, see [GHID_SI_REQUIREMENTS_RPI_CAPTURE.md](../plans/implemented/GHID_SI_REQUIREMENTS_RPI_CAPTURE.md).

## How the scanner uses it

The orchestration is in `hardware/scanner.py` (`Scanner`), behind `POST /scanner/face/prepare` and `POST /scanner/face/capture` (see `router/docs/SCANNER_API.md`). There is no other motion code here: the former `orchestrator/` package and `hardware_bridge.py` (a mock-up that moved Z outside `Scanner.motion_guard()`) were removed.
