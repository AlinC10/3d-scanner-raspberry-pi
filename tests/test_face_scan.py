import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SCHEMA_PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import (FaceCaptureRequest, FaceCaptureResponse, FaceCaptureResult, FacePrepareRequest,
                             StatusResponse)
from hardware.rig_limits import Z_TRAVEL_MAX_MM

def ok(fn):
    try:
        fn()
        return True
    except Exception:
        return False

req = FaceCaptureRequest()
prep = FacePrepareRequest()
result_default = FaceCaptureResult()

result = {
    "capture_defaults": req.model_dump(),
    "prepare_defaults": {k: v for k, v in prep.model_dump().items() if k != "camera_config"},
    "prepare_focus": prep.camera_config.focus,

    "strategy_none": ok(lambda: FaceCaptureRequest(strategy="none")),
    "strategy_bad": ok(lambda: FaceCaptureRequest(strategy="fast")),
    "manual_z_200": ok(lambda: FaceCaptureRequest(manual_z=200.0)),
    "manual_z_199": ok(lambda: FaceCaptureRequest(manual_z=199.9)),
    "manual_z_max": ok(lambda: FaceCaptureRequest(manual_z=float(Z_TRAVEL_MAX_MM))),
    "manual_z_over_max": ok(lambda: FaceCaptureRequest(manual_z=float(Z_TRAVEL_MAX_MM) + 1.0)),
    "step_mm_0": ok(lambda: FaceCaptureRequest(step_mm=0.0)),
    "manual_z_not_whole_steps": ok(lambda: FaceCaptureRequest(manual_z=200.005)),
    "search_range_not_whole_steps": ok(lambda: FaceCaptureRequest(search_range_mm=100.001)),
    "step_not_whole_steps": ok(lambda: FaceCaptureRequest(step_mm=8.005)),
    # Moved to the face prepare (or removed): refused by the capture
    "capture_home": ok(lambda: FaceCaptureRequest(home=True)),
    "capture_start_z": ok(lambda: FaceCaptureRequest(start_z_mm=180.0)),
    "capture_upload": ok(lambda: FaceCaptureRequest(upload_r2=False)),
    "capture_brightness": ok(lambda: FaceCaptureRequest(illumination_brightness=0.5)),

    "prepare_start_low": ok(lambda: FacePrepareRequest(start_z_mm=99.0)),
    "prepare_start_high": ok(lambda: FacePrepareRequest(start_z_mm=351.0)),
    "prepare_start_not_whole_steps": ok(lambda: FacePrepareRequest(start_z_mm=180.005)),
    "prepare_brightness_zero": ok(lambda: FacePrepareRequest(illumination_brightness=0.0)),
    "prepare_home": ok(lambda: FacePrepareRequest(home=True)),
    "prepare_fixed_focus": FacePrepareRequest(camera_config={"focus": 400}).camera_config.focus,

    "response_state": FaceCaptureResponse().state,
    "result_default": result_default.model_dump(),
    "status_face_result": StatusResponse(
        state="completed", mode="face", face_result={"optimal_z_mm": 240.0, "face_detected": False}
    ).model_dump(include={"mode", "face_result"}),
    "status_default_mode": StatusResponse(state="idle").mode,
}
print(json.dumps(result))
""" % ROOT


class TestFaceScanSchemas(unittest.TestCase):
    """Request and response schemas of the face prepare and capture (real pydantic, subprocess)."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", SCHEMA_PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_defaults(self):
        self.assertEqual(self.r["capture_defaults"], {
            "strategy": "sweep_best_score", "search_range_mm": 150.0, "step_mm": 8.0, "manual_z": None,
            "settle_s": 0.150, "z_delay": None, "reconstruct": True,
        })
        self.assertEqual(self.r["prepare_defaults"], {
            "enable_stream": True, "illumination_brightness": 0.5, "start_z_mm": 180.0, "upload_r2": False,
            "z_delay": 0.005,
        })
        self.assertEqual(self.r["prepare_focus"], "auto")

    def test_capture_validation(self):
        self.assertTrue(self.r["strategy_none"])
        self.assertFalse(self.r["strategy_bad"])
        self.assertTrue(self.r["manual_z_200"])
        self.assertFalse(self.r["manual_z_199"])
        self.assertTrue(self.r["manual_z_max"])
        self.assertFalse(self.r["manual_z_over_max"])
        self.assertFalse(self.r["step_mm_0"])
        self.assertFalse(self.r["manual_z_not_whole_steps"])
        self.assertFalse(self.r["search_range_not_whole_steps"])
        self.assertFalse(self.r["step_not_whole_steps"])

    def test_the_capture_no_longer_takes_the_prepare_fields(self):
        # FACE-12: no `home` (the face prepare always homes); the height, light and upload belong to the prepare
        for key in ("capture_home", "capture_start_z", "capture_upload", "capture_brightness"):
            self.assertFalse(self.r[key], key)

    def test_prepare_validation(self):
        self.assertFalse(self.r["prepare_start_low"])
        self.assertFalse(self.r["prepare_start_high"])
        self.assertFalse(self.r["prepare_start_not_whole_steps"])
        self.assertFalse(self.r["prepare_brightness_zero"])
        self.assertFalse(self.r["prepare_home"])
        self.assertEqual(self.r["prepare_fixed_focus"], 400)

    def test_result_and_status(self):
        self.assertEqual(self.r["response_state"], "running")
        self.assertEqual(self.r["result_default"], {
            "optimal_z_mm": None, "best_score": None, "face_detected": None, "warning": None, "photos": [],
            "r2_uploaded": False, "result_id": None, "metrics": None, "reconstruction_error": None,
        })
        self.assertEqual(self.r["status_face_result"]["mode"], "face")
        self.assertFalse(self.r["status_face_result"]["face_result"]["face_detected"])
        self.assertIsNone(self.r["status_default_mode"])


class _FakeDetector:
    """Stands for FaceScanner: one face whose 478 landmarks are all inside the frame (or `inside` of them)."""
    instances = []

    def __init__(self, num_faces=1, inside=478, nose_y=0.5):
        self.inside = inside
        self.nose_y = nose_y
        self.closed = False
        _FakeDetector.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def close(self):
        self.closed = True

    def scan_image(self, frame):
        lms = [{"x": 0.5, "y": 0.5} if i < self.inside else {"x": 1.2, "y": 0.5} for i in range(478)]
        return {"faces": [{
            "key_points": {"nose_tip": {"normalized": [0.5, self.nose_y, 0.0]}},
            "bounding_box": {"xmin": 20.0, "ymin": 10.0, "xmax": 60.0, "ymax": 50.0},
            "normalized_landmarks": lms,
        }]}


class _Frame:
    shape = (100, 200, 3)


class TestFramingAnalyzer(unittest.TestCase):
    def test_the_share_of_landmarks_inside_the_frame_raises_the_score(self):
        from face_scan.core.framing_analyzer import StereoFramingAnalyzer, VISIBILITY_WEIGHT
        full = StereoFramingAnalyzer(_FakeDetector(inside=478)).evaluate_frame(_Frame(), 0)
        cut = StereoFramingAnalyzer(_FakeDetector(inside=239)).evaluate_frame(_Frame(), 0)
        self.assertEqual(full["visible_ratio"], 1.0)
        self.assertAlmostEqual(cut["visible_ratio"], 0.5, places=2)
        self.assertAlmostEqual(full["score"] - cut["score"], VISIBILITY_WEIGHT * 0.5, places=2)
        self.assertEqual(full["bbox_norm"], (0.1, 0.1, 0.2, 0.4))

    def test_stereo_pair_reports_any_detection(self):
        from face_scan.core.framing_analyzer import StereoFramingAnalyzer
        res = StereoFramingAnalyzer(_FakeDetector()).evaluate_stereo_pair(_Frame(), None)
        self.assertFalse(res["both_detected"])
        self.assertTrue(res["any_detected"])


def _prepared_mock_scanner(upload_r2=True):
    from hardware.scanner_mock import ScannerMock
    scanner = ScannerMock()
    scanner.setup_and_stream(scan_mode="face", check_cloud=upload_r2)
    scanner.finish_preparation(z_start_mm=180.0, upload_r2=upload_r2)
    return scanner


class TestFaceScanScannerMock(unittest.TestCase):
    """ScannerMock follows the same prepare-then-capture flow."""

    def tearDown(self):
        if getattr(self, "scanner", None):
            self.scanner.close_hardware()

    def test_face_prepare_then_sweep(self):
        from hardware.scanner_mock import ScanPhase
        self.scanner = _prepared_mock_scanner()
        self.assertEqual((self.scanner.state, self.scanner.mode, self.scanner.z_position_mm), ("prepared", "face", 180.0))
        result = self.scanner.face_scan(strategy="sweep_best_score", search_range_mm=150.0, step_mm=8.0)
        self.assertEqual(result["status"], "success")
        self.assertAlmostEqual(result["optimal_z_mm"], 240.0, delta=8.0)
        self.assertTrue(result["face_detected"])
        self.assertEqual(len(result["photos"]), 2)
        self.assertTrue(result["r2_uploaded"])
        self.assertEqual(self.scanner.phase, ScanPhase.DONE)
        self.assertEqual(self.scanner.state, "completed")
        self.assertEqual(self.scanner.mode, "face")

    def test_manual_z_and_none(self):
        self.scanner = _prepared_mock_scanner(upload_r2=False)
        result = self.scanner.face_scan(manual_z=220.0)
        self.assertEqual(result["optimal_z_mm"], 220.0)
        self.assertIsNone(result["best_score"])
        self.assertFalse(result["r2_uploaded"])
        self.scanner.close_hardware()
        self.scanner = _prepared_mock_scanner()
        result = self.scanner.face_scan(strategy="none")
        self.assertEqual(result["optimal_z_mm"], 180.0)
        self.assertIsNone(result["face_detected"])

    def test_capture_needs_a_face_prepare(self):
        from hardware.scanner_mock import ScannerMock, ScannerModeError
        self.scanner = ScannerMock()
        with self.assertRaises(ScannerModeError):
            self.scanner.face_scan()
        self.scanner.setup_and_stream()               # object prepare
        self.scanner.finish_preparation(z_start_mm=30.0)
        self.assertEqual(self.scanner.mode, "object")
        with self.assertRaises(ScannerModeError):
            self.scanner.face_scan()

    def test_object_scan_refused_after_a_face_prepare(self):
        from hardware.scanner_mock import ScannerModeError
        self.scanner = _prepared_mock_scanner()
        with self.assertRaises(ScannerModeError):
            self.scanner.scan()


class TestFaceScanScanner(unittest.TestCase):
    """Scanner.face_scan on the real class with scripted parts."""

    def _scanner(self, z_name=180, upload_r2=True, focus="auto"):
        from hardware.scanner import Scanner, ScannerState
        scanner = Scanner(xvs=False)
        # Own mocks: the mocked hardware classes hand every Scanner the same objects, which other tests share.
        # A bare MagicMock endstop would read as "pressed".
        scanner.up_endstop = MagicMock(is_active=False)
        scanner.down_endstop = MagicMock(is_active=False)
        scanner.tof = MagicMock()
        scanner.lights = MagicMock(is_on=True, brightness=0.5)
        cams = MagicMock()
        cams.get_latest_lores_frames.return_value = (_Frame(), _Frame())
        cams.capture_photo.return_value = [f"/tmp/rig/0/f_{z_name}.jpg", f"/tmp/rig/1/f_{z_name}.jpg"]
        cams.last_capture_info = {"paths": cams.capture_photo.return_value, "cameras": [{}, {}]}
        cams.prepare_scan.return_value = [{"locked": True, "ready": True}, {"locked": True, "ready": True}]
        scanner.dual_cameras = cams
        scanner.cams = cams   # the scanner drops its reference when the run ends
        scanner._lock_auto_supported = True
        # What a face prepare leaves behind
        scanner.state = ScannerState.PREPARED
        scanner.mode = "face"
        scanner.z_position_mm = 180.0
        scanner._face_camera_settings = {"focus": focus, "copy_colour_gains": False, "quality": 95}
        scanner._face_upload_r2 = upload_r2
        return scanner

    def _run(self, scanner, **kwargs):
        with patch.object(scanner, "_register_photos"), patch.object(scanner, "_drain_and_verify") as drain, \
                patch.object(scanner, "start_upload_worker"), patch.object(scanner, "stop_upload_worker"), \
                patch.object(scanner, "_face_detection_problem", return_value=None):
            kwargs.setdefault("reconstruct", False)   # the 3D reconstruction has its own tests (TestFaceReconstruction)
            result = scanner.face_scan(**kwargs)
        return result, drain

    def test_z_moves_use_the_prepare_delay_and_a_capture_may_override_it(self):
        from hardware.scanner import FACE_Z_DELAY_DEFAULT_S
        scanner = self._scanner()
        self.assertEqual(scanner._face_z_delay, FACE_Z_DELAY_DEFAULT_S)
        delays = []
        with patch.object(scanner, "move_z_up_distance",
                          side_effect=lambda distance, delay, acceleration: (delays.append(delay), True)[1]):
            self._run(scanner, strategy="none", manual_z=200.0, z_delay=0.01)
        self.assertTrue(delays)
        self.assertEqual(set(delays), {0.01})

    def test_reconstruction_runs_on_the_pair_in_processing_and_its_result_is_published(self):
        scanner = self._scanner()
        info = {"dimensions_mm": {"width": 150.0}, "interpupillary_distance_mm": 63.0, "fit_rms_mm": {}}
        seen = []

        def reconstruct(*args, **kwargs):
            seen.append((scanner.state, scanner.phase))
            return info

        with patch("hardware.face_reconstruction.reconstruct_face", side_effect=reconstruct) as rec:
            result, _ = self._run(scanner, strategy="none", reconstruct=True)
        self.assertEqual(seen, [("processing", "reconstructing_face")])
        left, right = rec.call_args.args[:2]
        self.assertEqual((left, right), ("/tmp/rig/0/f_180.jpg", "/tmp/rig/1/f_180.jpg"))
        self.assertEqual(rec.call_args.args[3], scanner.job_id)
        self.assertEqual(result["result_id"], scanner.job_id)
        self.assertEqual(scanner.result_id, scanner.job_id)
        self.assertEqual(scanner.face_result["metrics"]["interpupillary_distance_mm"], 63.0)
        self.assertEqual(scanner.state, "completed")

    def test_a_failed_reconstruction_ends_in_error_with_the_reason(self):
        from hardware.face_reconstruction import FaceReconstructionError
        from hardware.scanner import ScannerError
        scanner = self._scanner()
        with patch("hardware.face_reconstruction.reconstruct_face", side_effect=FaceReconstructionError("no face")):
            with self.assertRaises(ScannerError):
                self._run(scanner, strategy="none", reconstruct=True)
        self.assertEqual(scanner.state, "error")
        self.assertEqual(scanner.phase, "error")
        self.assertIn("no face", scanner.error)
        self.assertEqual(scanner.face_result["reconstruction_error"], "no face")
        self.assertEqual(len(scanner.face_result["photos"]), 2)   # the photos are kept
        self.assertIsNone(scanner.result_id)

    def test_sweep_starts_from_the_prepared_height_without_homing_and_locks_on_the_face(self):
        from face_scan.core.framing_analyzer import StereoFramingAnalyzer
        scanner = self._scanner()
        scores = iter([0.5, 0.9, 1.8, 0.9, 0.5])

        class Analyzer(StereoFramingAnalyzer):
            def evaluate_stereo_pair(self, left, right):
                res = super().evaluate_stereo_pair(left, right)
                res["total_score"] = next(scores)
                return res

        with patch.object(scanner, "home_z_axis") as home, \
                patch.object(scanner, "move_z_up_distance", return_value=True) as up, \
                patch.object(scanner, "move_z_down_distance", return_value=True) as down:
            result, drain = self._run(scanner, search_range_mm=32.0, step_mm=8.0,
                                      analyzer=Analyzer(_FakeDetector()))
        home.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["optimal_z_mm"], 196.0)
        self.assertEqual(result["best_score"], 1.8)
        self.assertTrue(result["face_detected"])
        self.assertIsNone(result["warning"])
        self.assertEqual(scanner.z_position_mm, 196.0)
        self.assertTrue(down.called)              # went past the best height: arrives from below
        drain.assert_called_once()
        self.assertEqual(scanner.state, "completed")
        self.assertEqual(scanner.face_result["face_detected"], True)
        # FACE-7b: autofocus sweep + AE/AWB lock on the face box of each camera, at that height
        lock = scanner.cams.prepare_scan.call_args.kwargs
        self.assertIs(lock["lock_auto"], True)
        self.assertEqual(lock["focus"], "auto")
        roi_left, roi_right = lock["autofocus_roi"]
        self.assertAlmostEqual(roi_left[0], 0.1 - 0.02)
        self.assertAlmostEqual(roi_left[2], 0.2 + 0.04)
        # Every frame the search used was a fresh, strict one (FACE-16)
        for call in scanner.cams.get_latest_lores_frames.call_args_list:
            self.assertIs(call.kwargs.get("fresh"), True)
            self.assertIs(call.kwargs.get("strict"), True)
        for call in up.call_args_list + down.call_args_list:
            self.assertIs(call.kwargs.get("acceleration"), False, call)

    def test_no_face_is_a_warning_and_the_photo_is_taken_at_the_start_height(self):
        # FACE-11: not an error; best_score null instead of 0.0
        scanner = self._scanner()
        analyzer = MagicMock()
        analyzer.evaluate_stereo_pair.return_value = {"total_score": 0.0, "both_detected": False,
                                                      "any_detected": False, "avg_nose_y": None}
        with patch.object(scanner, "move_z_up_distance", return_value=True), \
                patch.object(scanner, "move_z_down_distance", return_value=True):
            result, _ = self._run(scanner, search_range_mm=24.0, step_mm=8.0, analyzer=analyzer)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["face_detected"])
        self.assertIsNone(result["best_score"])
        self.assertIn("No face", result["warning"])
        self.assertEqual(result["optimal_z_mm"], 180.0)
        self.assertEqual(scanner.state, "completed")
        self.assertIn("No face", scanner.phase_message)
        self.assertNotIn("autofocus_roi", scanner.cams.prepare_scan.call_args.kwargs)

    def test_stale_preview_frames_end_in_error(self):
        from hardware.scanner import ScannerError
        scanner = self._scanner()
        scanner.dual_cameras.get_latest_lores_frames.return_value = (None, None)
        with patch.object(scanner, "move_z_up_distance", return_value=True):
            with self.assertRaises(ScannerError):
                self._run(scanner, analyzer=MagicMock())
        self.assertEqual(scanner.state, "error")
        self.assertIn("no fresh preview frame", scanner.error)
        self.assertEqual(scanner.cams.get_latest_lores_frames.call_count, scanner.FACE_FRAME_ATTEMPTS)

    def test_the_detector_is_built_for_the_search_and_closed_after_it(self):
        # FACE-14
        _FakeDetector.instances.clear()
        scanner = self._scanner()
        with patch("face_scan.face_scanner.FaceScanner", _FakeDetector), \
                patch.object(scanner, "move_z_up_distance", return_value=True), \
                patch.object(scanner, "move_z_down_distance", return_value=True):
            self._run(scanner, search_range_mm=16.0, step_mm=8.0)
        self.assertEqual(len(_FakeDetector.instances), 1)
        self.assertTrue(_FakeDetector.instances[0].closed)

    def test_top_endstop_ends_the_search_but_is_an_error_for_a_direct_move(self):
        # FACE-18
        import hardware.scanner as scanner_module
        from hardware.scanner import ScannerError
        analyzer = MagicMock()
        analyzer.evaluate_stereo_pair.return_value = {"total_score": 1.0, "both_detected": True, "avg_nose_y": 0.3}
        scanner = self._scanner()
        calls = []

        def climb(distance, **kwargs):
            calls.append(distance)
            if len(calls) >= 2:
                raise scanner_module.TopEndstopTriggered("top")
            return True

        with patch.object(scanner, "move_z_up_distance", side_effect=climb), \
                patch.object(scanner, "move_z_down_distance", return_value=True):
            with self.assertRaises(ScannerError):
                self._run(scanner, search_range_mm=40.0, step_mm=8.0, analyzer=analyzer)
        # the position is lost at the endstop: the scan cannot go back to a known height
        self.assertIn("position was lost", scanner.error)

        scanner = self._scanner()
        with patch.object(scanner, "move_z_up_distance", side_effect=scanner_module.TopEndstopTriggered("top")):
            with self.assertRaises(ScannerError):
                self._run(scanner, manual_z=300.0)
        self.assertIn("top endstop was reached", scanner.error)

    def test_manual_z_and_none_need_no_face_detection(self):
        scanner = self._scanner(z_name=220, upload_r2=False, focus=400)
        with patch.object(scanner, "move_z_up_distance", return_value=True) as up:
            result, drain = self._run(scanner, manual_z=220.0)
        self.assertEqual(up.call_args.kwargs["distance"], 40.0)
        self.assertEqual(result["optimal_z_mm"], 220.0)
        self.assertIsNone(result["face_detected"])
        self.assertFalse(result["r2_uploaded"])
        drain.assert_not_called()
        self.assertEqual(scanner.cams.prepare_scan.call_args.kwargs["focus"], 400)   # fixed focus kept

        scanner = self._scanner()
        with patch.object(scanner, "move_z_up_distance") as up:
            result, _ = self._run(scanner, strategy="none")
        up.assert_not_called()
        self.assertEqual(result["optimal_z_mm"], 180.0)

    def test_a_z_move_that_does_not_complete_ends_in_error(self):
        from hardware.scanner import ScannerError, ScanPhase
        scanner = self._scanner()
        with patch.object(scanner, "move_z_up_distance", return_value=False):
            with self.assertRaises(ScannerError):
                self._run(scanner, manual_z=220.0)
        self.assertEqual(scanner.state, "error")
        self.assertEqual(scanner.phase, ScanPhase.ERROR)
        self.assertIn("did not complete", scanner.error)

    def test_the_face_scan_does_not_read_the_tof_sensor(self):
        scanner = self._scanner()
        scanner.tof.read_distance_mm.side_effect = AssertionError("the face scan must not read the ToF sensor")
        with patch.object(scanner, "move_z_up_distance", return_value=True):
            result, _ = self._run(scanner, manual_z=220.0)
        self.assertEqual(result["status"], "success")

    def test_begin_face_scan_refuses_before_anything_moves(self):
        from hardware.scanner import ScannerBusyError, ScannerModeError, ScannerPreflightError, ScannerState
        scanner = self._scanner()
        with self.assertRaises(ValueError):
            scanner.begin_face_scan(manual_z=150.0)
        with self.assertRaises(ValueError):
            scanner.begin_face_scan(search_range_mm=300.0)     # 180 + 300 > travel
        with patch.object(scanner, "_face_detection_problem", return_value="MediaPipe is not installed"):
            with self.assertRaises(ScannerPreflightError):     # FACE-13: before the carriage moves
                scanner.begin_face_scan()
            scanner.begin_face_scan(strategy="none")           # no search: MediaPipe not needed
        self.assertEqual(scanner.state, ScannerState.RUNNING)

        scanner = self._scanner()
        scanner.state = ScannerState.PREPARING
        with self.assertRaises(ScannerBusyError):
            scanner.begin_face_scan(manual_z=220.0)
        for state, mode in ((ScannerState.IDLE, None), (ScannerState.COMPLETED, "face"),
                            (ScannerState.PREPARED, "object")):
            scanner.state, scanner.mode = state, mode
            with self.assertRaises(ScannerModeError):
                scanner.begin_face_scan(manual_z=220.0)

    def test_object_scan_refused_after_a_face_prepare(self):
        from hardware.scanner import ScannerModeError
        scanner = self._scanner()
        with self.assertRaises(ScannerModeError):
            scanner.scan()
        self.assertEqual(scanner.state, "prepared")

    def test_face_prepare_keeps_the_cameras_unlocked_and_remembers_the_settings(self):
        import hardware.scanner as scanner_module
        scanner = self._scanner()
        scanner.state = "preparing"
        scanner.z_position_mm = None
        with patch.object(scanner, "home_z_axis"), \
                patch.object(scanner, "move_z_up_distance", return_value=True) as up, \
                patch.object(scanner, "wait_for_light_stable", return_value=True), \
                patch.object(scanner_module.r2, "delete_all_files_from_bucket") as wipe:
            scanner.finish_preparation(z_start_mm=200.0, upload_r2=False, focus="auto", exposure_time=5000,
                                       copy_colour_gains=True)
        self.assertEqual(scanner.state, "prepared")
        self.assertEqual(scanner.z_position_mm, 200.0)
        self.assertEqual(up.call_args.kwargs["distance"], 200.0)
        wipe.assert_not_called()                                # no upload: the bucket is left alone
        kwargs = scanner.cams.prepare_scan.call_args.kwargs
        self.assertNotIn("lock_auto", kwargs)                   # AE/AWB keep following the subject
        self.assertNotIn("exposure_time", kwargs)
        self.assertEqual(scanner._face_camera_settings["exposure_time"], 5000)
        self.assertTrue(scanner._face_camera_settings["copy_colour_gains"])
        self.assertFalse(scanner._face_upload_r2)


ROUTER_PROGRAM = r"""
import json, os, sys, time
sys.path.insert(0, %r)
os.environ["MOCK_HARDWARE"] = "1"
os.environ["GROQ_API_KEY"] = ""
from hardware.mock_hardware import MockHardware
MockHardware.apply()
import main_dev
import router.scanner
from fastapi.testclient import TestClient

app = main_dev.app
results = {}

def wait(client, states):
    deadline = time.time() + 30
    status = client.get("/scanner/status").json()
    while status["state"] not in states and time.time() < deadline:
        time.sleep(0.05)
        status = client.get("/scanner/status").json()
    return status

with TestClient(app) as client:
    paths = app.openapi().get("paths", {})
    results["routes"] = ["/scanner/face/prepare" in paths, "/scanner/face/capture" in paths]

    results["capture_unprepared"] = client.post("/scanner/face/capture", json={"manual_z": 240.0}).status_code
    results["prepare_bad_key"] = client.post("/scanner/face/prepare", json={"home": True}).status_code

    r = client.post("/scanner/face/prepare", json={"start_z_mm": 300.0, "upload_r2": False})
    results["prepare_code"] = r.status_code
    results["prepared"] = wait(client, ("prepared", "error"))
    results["start_after_face_prepare"] = client.post("/scanner/start", json={}).status_code
    results["capture_bad_key"] = client.post("/scanner/face/capture", json={"home": True}).status_code
    results["capture_bad_range"] = client.post("/scanner/face/capture", json={"search_range_mm": 250.0, "step_mm": 25.0}).status_code

    r = client.post("/scanner/face/capture", json={"manual_z": 240.0})
    results["capture_code"] = r.status_code
    results["capture_body"] = r.json()
    results["final"] = wait(client, ("completed", "error", "cancelled"))
    results["capture_again"] = client.post("/scanner/face/capture", json={"manual_z": 240.0}).status_code

    client.post("/scanner/prepare", json={})
    results["object_mode"] = wait(client, ("prepared", "error"))["mode"]
    results["capture_after_object_prepare"] = client.post("/scanner/face/capture", json={}).status_code
    client.post("/scanner/cancel")

    router.scanner._start_state["pending"] = True
    try:
        results["pending_code"] = client.post("/scanner/face/capture", json={"manual_z": 240.0}).status_code
    finally:
        router.scanner._start_state["pending"] = False

print(json.dumps(results))
""" % ROOT


class TestFaceScanRouter(unittest.TestCase):
    """POST /scanner/face/prepare and /scanner/face/capture in a real FastAPI TestClient (simulated rig)."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", ROUTER_PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_routes(self):
        self.assertEqual(self.r["routes"], [True, True])

    def test_capture_needs_a_face_prepare(self):
        self.assertEqual(self.r["capture_unprepared"], 400)
        self.assertEqual(self.r["capture_after_object_prepare"], 400)
        self.assertEqual(self.r["capture_again"], 400)      # a new prepare is needed after a capture
        self.assertEqual(self.r["object_mode"], "object")

    def test_face_prepare_then_capture(self):
        self.assertEqual(self.r["prepare_code"], 202)
        self.assertEqual(self.r["prepared"]["state"], "prepared")
        self.assertEqual(self.r["prepared"]["mode"], "face")
        self.assertEqual(self.r["start_after_face_prepare"], 400)
        self.assertEqual(self.r["capture_code"], 202)
        self.assertEqual(self.r["capture_body"]["state"], "running")
        final = self.r["final"]
        self.assertEqual((final["state"], final["phase"], final["mode"]), ("completed", "done", "face"))
        self.assertEqual(final["face_result"]["optimal_z_mm"], 240.0)
        self.assertEqual(len(final["face_result"]["photos"]), 2)
        self.assertFalse(final["face_result"]["r2_uploaded"])

    def test_validation_errors(self):
        self.assertEqual(self.r["prepare_bad_key"], 422)
        self.assertEqual(self.r["capture_bad_key"], 422)
        self.assertEqual(self.r["capture_bad_range"], 422)  # 300 + 250 mm > travel: refused by the scanner

    def test_pending_conflict(self):
        self.assertEqual(self.r["pending_code"], 409)


if __name__ == "__main__":
    unittest.main()


class TestFaceReconstruction(unittest.TestCase):
    """hardware.face_reconstruction with a scripted reconstruct_3d.py (no MediaPipe needed)."""

    SCRIPT = (
        "import argparse, json, os, sys, time\n"
        "p = argparse.ArgumentParser(); p.add_argument('-l'); p.add_argument('-r'); p.add_argument('-o')\n"
        "p.add_argument('--name'); a = p.parse_args()\n"
        "mode = os.environ.get('FAKE_MODE', 'ok')\n"
        "print('Scanning stereo face pair: x and y...', flush=True)\n"
        "if mode == 'fail': sys.exit(3)\n"
        "if mode == 'hang': time.sleep(30)\n"
        "print('Deforming 3D facial geometry from stereo landmarks...', flush=True)\n"
        "os.makedirs(os.path.join(a.o, 'glb'), exist_ok=True)\n"
        "open(os.path.join(a.o, 'glb', 'low_' + a.name + '.glb'), 'wb').write(b'g')\n"
        "json.dump({'title': a.name, 'interpupillary_distance_mm': 63.0}, open(os.path.join(a.o, 'object.json'), 'w'))\n"
    )

    def setUp(self):
        import tempfile
        from pathlib import Path
        import hardware.face_reconstruction as fr
        self.fr = fr
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "mesh").mkdir()
        (self.tmp / "mesh" / "reconstruct_3d.py").write_text(self.SCRIPT)
        self.results = self.tmp / "results"
        self.patch = patch.object(fr, "FACE_MESH_DIR", self.tmp / "mesh")
        self.patch.start()

    def tearDown(self):
        import shutil
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_it(self, mode, **kwargs):
        with patch.dict(os.environ, {"FAKE_MODE": mode}):
            return self.fr.reconstruct_face("l.jpg", "r.jpg", self.results, "20261030-101500-abcdef", **kwargs)

    def test_success_is_a_library_entry_and_the_steps_are_reported(self):
        steps = []
        info = self.run_it("ok", on_progress=steps.append)
        folder = self.results / "20261030-101500-abcdef"
        self.assertEqual(json.loads((folder / "status.json").read_text())["state"], "ok")
        self.assertTrue((folder / "glb" / "low_model.glb").is_file())
        self.assertEqual(info["interpupillary_distance_mm"], 63.0)
        self.assertIn("Detecting face landmarks...", steps)
        self.assertIn("Computing 3D geometry & depth...", steps)
        self.assertFalse((self.results / ".20261030-101500-abcdef.partial").exists())

    def test_failure_leaves_no_library_entry(self):
        with self.assertRaises(self.fr.FaceReconstructionError):
            self.run_it("fail")
        self.assertFalse((self.results / "20261030-101500-abcdef").exists())

    def test_cancel_kills_the_process(self):
        import threading
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        with self.assertRaises(self.fr.FaceReconstructionError) as ctx:
            self.run_it("hang", cancel_event=cancel)
        self.assertIn("cancelled", str(ctx.exception))
