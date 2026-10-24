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
from schemas.scanner import FaceCaptureRequest, FaceCaptureResponse
from hardware.rig_limits import Z_TRAVEL_MAX_MM

def ok(fn):
    try:
        fn()
        return True
    except Exception:
        return False

req_default = FaceCaptureRequest()
resp_default = FaceCaptureResponse()

result = {
    "default_strategy": req_default.strategy,
    "default_start_z_mm": req_default.start_z_mm,
    "default_search_range_mm": req_default.search_range_mm,
    "default_step_mm": req_default.step_mm,
    "default_manual_z": req_default.manual_z,
    "default_home": req_default.home,
    "default_upload_r2": req_default.upload_r2,
    "default_illumination": req_default.illumination_brightness,
    "default_settle_s": req_default.settle_s,

    "manual_z_200": ok(lambda: FaceCaptureRequest(manual_z=200.0)),
    "manual_z_199": ok(lambda: FaceCaptureRequest(manual_z=199.9)),
    "manual_z_max": ok(lambda: FaceCaptureRequest(manual_z=float(Z_TRAVEL_MAX_MM))),
    "manual_z_over_max": ok(lambda: FaceCaptureRequest(manual_z=float(Z_TRAVEL_MAX_MM) + 1.0)),

    "travel_bounds_ok": ok(lambda: FaceCaptureRequest(start_z_mm=180.0, search_range_mm=150.0)),
    "travel_bounds_exceeded": ok(lambda: FaceCaptureRequest(start_z_mm=350.0, search_range_mm=200.0)),

    "step_mm_8": ok(lambda: FaceCaptureRequest(step_mm=8.0)),
    "step_mm_0": ok(lambda: FaceCaptureRequest(step_mm=0.0)),

    "response_status": resp_default.status,
    "response_photos": resp_default.photos,
    "response_r2": resp_default.r2_uploaded,
}
print(json.dumps(result))
""" % ROOT


class TestFaceScanSchemas(unittest.TestCase):
    """Tests Pydantic request & response schemas for facial capture via subprocess."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", SCHEMA_PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_face_capture_request_defaults(self):
        self.assertEqual(self.r["default_strategy"], "sweep_best_score")
        self.assertEqual(self.r["default_start_z_mm"], 180.0)
        self.assertEqual(self.r["default_search_range_mm"], 150.0)
        self.assertEqual(self.r["default_step_mm"], 8.0)
        self.assertIsNone(self.r["default_manual_z"])
        self.assertTrue(self.r["default_home"])
        self.assertTrue(self.r["default_upload_r2"])
        self.assertEqual(self.r["default_illumination"], 0.5)
        self.assertEqual(self.r["default_settle_s"], 0.150)

    def test_manual_z_validation(self):
        self.assertTrue(self.r["manual_z_200"])
        self.assertFalse(self.r["manual_z_199"])
        self.assertTrue(self.r["manual_z_max"])
        self.assertFalse(self.r["manual_z_over_max"])

    def test_travel_bounds_validation(self):
        self.assertTrue(self.r["travel_bounds_ok"])
        self.assertFalse(self.r["travel_bounds_exceeded"])

    def test_step_mm_validation(self):
        self.assertTrue(self.r["step_mm_8"])
        self.assertFalse(self.r["step_mm_0"])

    def test_response_defaults(self):
        self.assertEqual(self.r["response_status"], "success")
        self.assertEqual(self.r["response_photos"], [])
        self.assertFalse(self.r["response_r2"])


class TestFaceScanScannerMock(unittest.TestCase):
    """Tests ScannerMock simulated face scanning execution."""

    def setUp(self):
        from hardware.scanner_mock import ScannerMock
        self.scanner = ScannerMock()

    def tearDown(self):
        self.scanner.close_hardware()

    def test_face_scan_sweep_best_score(self):
        from hardware.scanner_mock import ScanPhase
        result = self.scanner.face_scan(
            strategy="sweep_best_score",
            start_z_mm=180.0,
            search_range_mm=150.0,
            step_mm=8.0,
            manual_z=None,
            home=True,
            upload_r2=True,
            illumination_brightness=0.5,
        )
        self.assertEqual(result["status"], "success")
        self.assertIsNotNone(result["optimal_z_mm"])
        # Apex is simulated at 240 mm (closest 8mm step: 244mm or 236mm)
        self.assertAlmostEqual(result["optimal_z_mm"], 244.0, delta=8.0)
        self.assertIsNotNone(result["best_score"])
        self.assertEqual(len(result["photos"]), 2)
        self.assertTrue(result["r2_uploaded"])
        self.assertEqual(self.scanner.photos_captured, 2)
        self.assertEqual(self.scanner.phase, ScanPhase.DONE)

    def test_face_scan_direct_stop(self):
        result = self.scanner.face_scan(
            strategy="direct_stop",
            start_z_mm=180.0,
            search_range_mm=150.0,
            step_mm=8.0,
            home=True,
        )
        self.assertEqual(result["status"], "success")
        self.assertIsNotNone(result["optimal_z_mm"])
        self.assertAlmostEqual(result["optimal_z_mm"], 240.0, delta=8.0)

    def test_face_scan_manual_z(self):
        result = self.scanner.face_scan(
            manual_z=220.0,
            home=True,
            upload_r2=False,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["optimal_z_mm"], 220.0)
        self.assertIsNone(result["best_score"])
        self.assertFalse(result["r2_uploaded"])
        for p in result["photos"]:
            self.assertTrue(p.endswith("f_220.jpg"))

    def test_face_scan_busy_rejection(self):
        from hardware.scanner_mock import ScannerState, ScannerBusyError
        self.scanner.state = ScannerState.RUNNING
        with self.assertRaises(ScannerBusyError):
            self.scanner.face_scan()

    def test_face_scan_cancellation(self):
        from hardware.scanner_mock import ScanPhase
        # Simulate cancel triggering during vertical motion
        def trigger_cancel(*args, **kwargs):
            self.scanner._cancel_event.set()
            return True

        self.scanner.move_z_up_distance = trigger_cancel
        result = self.scanner.face_scan(home=False)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.scanner.phase, ScanPhase.CANCELLED)


class TestFaceScanScannerIntegration(unittest.TestCase):
    """Tests Scanner.face_scan logic on real Scanner class with mocked components."""

    def test_scanner_face_scan_with_mock_analyzer(self):
        from hardware.scanner import Scanner, ScanPhase

        scanner = Scanner(xvs=False)

        # Mock cameras
        mock_cams = MagicMock()
        mock_cams.get_latest_lores_frames.return_value = (MagicMock(), MagicMock())
        mock_cams.capture_photo.return_value = ["/tmp/input_images/rig/0/f_240.jpg", "/tmp/input_images/rig/1/f_240.jpg"]
        mock_cams.last_capture_info = {"paths": mock_cams.capture_photo.return_value, "cameras": [{}, {}]}
        scanner.dual_cameras = mock_cams

        class MockAnalyzer:
            def __init__(self):
                self.calls = 0

            def evaluate_stereo_pair(self, left, right):
                self.calls += 1
                score = 1.8 if self.calls == 5 else 0.5
                return {"total_score": score, "both_detected": True, "avg_nose_y": 0.50}

        analyzer = MockAnalyzer()

        with patch.object(scanner, "home_z_axis") as mock_home, \
             patch.object(scanner, "move_z_up_distance", return_value=True) as mock_up, \
             patch.object(scanner, "move_z_down_distance", return_value=True) as mock_down, \
             patch.object(scanner, "_register_photos") as mock_reg, \
             patch.object(scanner, "_drain_and_verify") as mock_drain:

            res = scanner.face_scan(
                strategy="sweep_best_score",
                start_z_mm=180.0,
                search_range_mm=80.0,
                step_mm=8.0,
                home=True,
                upload_r2=True,
                illumination_brightness=0.5,
                analyzer=analyzer,
            )

            mock_home.assert_called_once()
            self.assertTrue(mock_up.called)
            self.assertEqual(res["status"], "success")
            self.assertEqual(scanner.phase, ScanPhase.DONE)
            mock_cams.capture_photo.assert_called_once()
            mock_drain.assert_called_once()

    def test_scanner_face_scan_no_face_detected_fallback(self):
        from hardware.scanner import Scanner, ScanPhase

        scanner = Scanner(xvs=False)
        mock_cams = MagicMock()
        mock_cams.get_latest_lores_frames.return_value = (MagicMock(), MagicMock())
        mock_cams.capture_photo.return_value = ["/tmp/rig/0/f_180.jpg", "/tmp/rig/1/f_180.jpg"]
        mock_cams.last_capture_info = {"paths": mock_cams.capture_photo.return_value, "cameras": [{}, {}]}
        scanner.dual_cameras = mock_cams

        class NoFaceAnalyzer:
            def evaluate_stereo_pair(self, left, right):
                return {"total_score": 0.0, "both_detected": False, "avg_nose_y": None}

        with patch.object(scanner, "home_z_axis") as mock_home, \
             patch.object(scanner, "move_z_up_distance", return_value=True), \
             patch.object(scanner, "move_z_down_distance", return_value=True), \
             patch.object(scanner, "_register_photos"), \
             patch.object(scanner, "_drain_and_verify"):

            res = scanner.face_scan(
                strategy="sweep_best_score",
                start_z_mm=180.0,
                search_range_mm=40.0,
                step_mm=8.0,
                home=True,
                upload_r2=True,
                analyzer=NoFaceAnalyzer(),
            )
            mock_home.assert_called_once()
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["optimal_z_mm"], 180.0)
            self.assertEqual(scanner.phase, ScanPhase.DONE)

    def test_scanner_face_scan_top_endstop_trigger(self):
        from hardware.scanner import Scanner, ScanPhase
        from hardware.endstop import TopEndstopTriggered

        scanner = Scanner(xvs=False)
        mock_cams = MagicMock()
        mock_cams.get_latest_lores_frames.return_value = (MagicMock(), MagicMock())
        mock_cams.capture_photo.return_value = ["/tmp/rig/0/f_180.jpg", "/tmp/rig/1/f_180.jpg"]
        mock_cams.last_capture_info = {"paths": mock_cams.capture_photo.return_value, "cameras": [{}, {}]}
        scanner.dual_cameras = mock_cams

        calls = 0
        def climb_mock(dist):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise TopEndstopTriggered("Top limit switch reached")
            return True

        class DummyAnalyzer:
            def evaluate_stereo_pair(self, left, right):
                return {"total_score": 1.0, "both_detected": True, "avg_nose_y": 0.50}

        with patch.object(scanner, "home_z_axis"), \
             patch.object(scanner, "move_z_up_distance", side_effect=climb_mock), \
             patch.object(scanner, "move_z_down_distance", return_value=True), \
             patch.object(scanner, "_register_photos"), \
             patch.object(scanner, "_drain_and_verify"):

            res = scanner.face_scan(
                start_z_mm=180.0,
                search_range_mm=100.0,
                step_mm=8.0,
                home=True,
                upload_r2=True,
                analyzer=DummyAnalyzer(),
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(scanner.phase, ScanPhase.DONE)

    def test_scanner_face_scan_home_false_and_no_upload(self):
        from hardware.scanner import Scanner, ScanPhase

        scanner = Scanner(xvs=False)
        mock_cams = MagicMock()
        mock_cams.capture_photo.return_value = ["/tmp/rig/0/f_220.jpg", "/tmp/rig/1/f_220.jpg"]
        mock_cams.last_capture_info = {"paths": mock_cams.capture_photo.return_value, "cameras": [{}, {}]}
        scanner.dual_cameras = mock_cams

        with patch.object(scanner, "home_z_axis") as mock_home, \
             patch.object(scanner, "move_z_up_distance", return_value=True) as mock_up, \
             patch.object(scanner, "move_z_down_distance", return_value=True), \
             patch.object(scanner, "_register_photos"), \
             patch.object(scanner, "_drain_and_verify") as mock_drain:

            res = scanner.face_scan(
                manual_z=220.0,
                home=False,
                upload_r2=False,
            )
            mock_home.assert_not_called()
            mock_drain.assert_not_called()
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["optimal_z_mm"], 220.0)
            self.assertFalse(res["r2_uploaded"])
            self.assertEqual(scanner.phase, ScanPhase.DONE)



ROUTER_PROGRAM = r"""
import json, os, sys
sys.path.insert(0, %r)
os.environ["MOCK_HARDWARE"] = "1"
from hardware.mock_hardware import MockHardware
MockHardware.apply()
import main_dev
import router.scanner
from fastapi.testclient import TestClient

app = main_dev.app
results = {}

with TestClient(app) as client:
    paths = app.openapi().get("paths", {})
    results["route_registered"] = "/scanner/face/capture" in paths

    r_manual = client.post("/scanner/face/capture", json={"manual_z": 240.0, "upload_r2": False})
    results["manual_code"] = r_manual.status_code
    results["manual_body"] = r_manual.json()

    r_bad_z = client.post("/scanner/face/capture", json={"manual_z": 150.0})
    results["bad_z_code"] = r_bad_z.status_code

    r_bad_bounds = client.post("/scanner/face/capture", json={"start_z_mm": 350.0, "search_range_mm": 200.0})
    results["bad_bounds_code"] = r_bad_bounds.status_code

    scanner = router.scanner.scanner
    from hardware.scanner import ScannerState
    scanner.state = ScannerState.RUNNING
    try:
        r_busy = client.post("/scanner/face/capture", json={"manual_z": 240.0})
        results["busy_code"] = r_busy.status_code
    finally:
        scanner.state = ScannerState.IDLE

print(json.dumps(results))
""" % ROOT


class TestFaceScanRouterEndpoint(unittest.TestCase):
    """Tests POST /scanner/face/capture endpoint in a real FastAPI TestClient environment."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", ROUTER_PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=30)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_route_registration(self):
        self.assertTrue(self.r["route_registered"])

    def test_manual_z_post_success(self):
        self.assertEqual(self.r["manual_code"], 200)
        body = self.r["manual_body"]
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["optimal_z_mm"], 240.0)
        self.assertEqual(len(body["photos"]), 2)
        self.assertFalse(body["r2_uploaded"])

    def test_validation_errors(self):
        self.assertEqual(self.r["bad_z_code"], 422)
        self.assertEqual(self.r["bad_bounds_code"], 422)

    def test_busy_conflict_code(self):
        self.assertEqual(self.r["busy_code"], 409)


if __name__ == "__main__":
    unittest.main()
