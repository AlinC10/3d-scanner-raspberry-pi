"""
SCAN-FLIP: a scan of both sides of the object. The first side climbs as usual, the scanner waits in `awaiting_flip`,
then the second side is photographed on the way down into input_images/session_1/rig (keys session_1/rig/...).
Fakes only (see tests/test_fs_scanner.py); the schema is checked with the real pydantic in a subprocess. The
simulated rig and the HTTP endpoints are covered in tests/test_dev_app.py.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.test_fs_scanner import ScannerTestCase, ScannerState, scanner_module

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@unittest.skip("SCAN-FLIP disabled temporarily")
class TestFlipScan(ScannerTestCase):
    def run_scan(self, **kwargs):
        """Starts scan() in a thread; returns the thread and a dict that receives its exception, if any."""
        outcome = {}

        def body():
            try:
                self.s.scan(**kwargs)
            except Exception as e:   # noqa: BLE001 - reported to the test
                outcome["error"] = e

        thread = threading.Thread(target=body, daemon=True)
        thread.start()
        return thread, outcome

    def wait_for_state(self, state, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.s.state == state:
                return
            time.sleep(0.01)
        self.fail(f"state {state} not reached (state {self.s.state})")

    def captured_prefixes(self):
        return [(c.kwargs["output_dir"], c.kwargs["output_prefix"]) for c in self.cams.capture_photo.call_args_list]

    def test_continue_scans_the_second_side_on_the_way_down(self):
        self.verdicts = {1: True, 2: False}
        thread, outcome = self.run_scan(flip=True)
        self.wait_for_state(ScannerState.AWAITING_FLIP)
        self.assertEqual(self.s.phase, "awaiting_flip")
        self.assertGreater(self.s.flip_seconds_left, 0)
        self.assertEqual(self.s.total_photos, 80)            # 2 rings x 20 shots x 2 cameras
        self.s.flip_decision(True)
        thread.join(15)
        self.assertNotIn("error", outcome)
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.total_photos, 160)
        self.assertIsNone(self.s.flip_seconds_left)

        # Second side: top ring first, same names, its own folder
        second = [prefix for folder, prefix in self.captured_prefixes() if folder == self.s.flip_images_dir]
        self.assertEqual(len(second), 40)
        self.assertEqual(second[0], "f_2000")
        self.assertEqual(second[-1], "f_1019")
        self.assertTrue(os.path.isfile(os.path.join(self.s.flip_images_dir, "1", "f_1000.jpg")))
        self.assertEqual(Path(self.s.flip_images_dir).parent.name, "session_1")

        # One descent between the two rings of the second side, at the climb distance
        self.assertEqual(self.s.z_axis_motor.rotate.call_args_list[-1].kwargs["clockwise"], False)
        downs = [c for c in self.s.z_axis_motor.rotate.call_args_list if c.kwargs.get("clockwise") is False]
        ups = [c for c in self.s.z_axis_motor.rotate.call_args_list if c.kwargs.get("clockwise") is True]
        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0].kwargs["steps"], ups[0].kwargs["steps"])
        self.s.tof.probe_object.assert_called()   # only on the first side
        self.assertEqual(self.s.tof.probe_object.call_count, 21)   # ring 1 stops at the first object reading + ring 2

        # Both sides are in the manifest and verified in the bucket
        manifest = self.manifest()
        sessions = [p["session"] for p in manifest["photos"]]
        self.assertEqual((sessions.count(0), sessions.count(1)), (80, 80))
        self.assertIn("session_1/rig/0/f_2000.jpg", manifest["expected_keys"])
        self.assertIn("rig/0/f_2000.jpg", manifest["expected_keys"])
        self.assertTrue(self.s.upload_verified)
        self.assertIn("session_1/rig/1/f_1019.jpg", self.r2.bucket)

    def test_skip_finishes_with_the_first_side(self):
        self.verdicts = {1: False}
        thread, outcome = self.run_scan(flip=True)
        self.wait_for_state(ScannerState.AWAITING_FLIP)
        self.s.flip_decision(False)
        thread.join(15)
        self.assertNotIn("error", outcome)
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.total_photos, 40)
        self.assertFalse(any(k.startswith("session_1/") for k in self.r2.bucket))
        self.assertFalse(os.path.exists(self.s.flip_images_dir))
        self.assertTrue(self.s.upload_verified)

    def test_no_answer_finishes_with_one_side_and_says_so(self):
        self.verdicts = {1: False}
        thread, outcome = self.run_scan(flip=True, flip_timeout_s=0.2)
        thread.join(15)
        self.assertNotIn("error", outcome)
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.total_photos, 40)
        self.assertIn("flip wait timed out", self.s.phase_message)

    def test_cancel_during_the_wait_cancels_the_scan(self):
        self.verdicts = {1: False}
        thread, outcome = self.run_scan(flip=True)
        self.wait_for_state(ScannerState.AWAITING_FLIP)
        self.assertTrue(self.s.scan_owns_rig)
        self.s.emergency_stop()
        thread.join(15)
        self.assertEqual(self.s.state, ScannerState.CANCELLED)
        self.assertEqual(self.s.phase, "cancelled")
        self.assertEqual(self.s.total_photos, 40)

    def test_without_flip_there_is_no_wait(self):
        self.verdicts = {1: False}
        self.s.scan()
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.session, 0)
        self.assertEqual(self.manifest()["photos"][0]["session"], 0)

    def test_a_decision_outside_the_wait_is_refused(self):
        with self.assertRaises(scanner_module.ScannerModeError):
            self.s.flip_decision(True)

    def test_a_new_scan_deletes_the_second_side_of_an_older_one(self):
        stale = os.path.join(self.s.flip_images_dir, "0", "f_1000.jpg")
        os.makedirs(os.path.dirname(stale))
        Path(stale).write_bytes(b"old")
        self.verdicts = {1: False}
        self.s.scan()
        self.assertFalse(os.path.exists(Path(self.s.flip_images_dir).parent))

    def test_a_bottom_endstop_on_the_way_down_takes_one_last_ring(self):
        self.verdicts = {1: True, 2: True, 3: False}
        thread, outcome = self.run_scan(flip=True)
        self.wait_for_state(ScannerState.AWAITING_FLIP)
        original = self.s.move_z_down

        def hit_bottom(*args, **kwargs):
            raise scanner_module.BottomEndstopTriggered("bottom")

        self.s.move_z_down = hit_bottom
        self.s.flip_decision(True)
        thread.join(15)
        self.s.move_z_down = original
        self.assertNotIn("error", outcome)
        second = [prefix for folder, prefix in self.captured_prefixes() if folder == self.s.flip_images_dir]
        self.assertEqual((second[0], second[20]), ("f_3000", "f_2000"))   # ring 3, then the last ring
        self.assertEqual(len(second), 40)
        self.assertTrue(any("bottom endstop" in w for w in self.s.tof_warnings))


@unittest.skip("SCAN-FLIP disabled temporarily")
class TestCopyPhotosSecondSide(unittest.TestCase):
    def test_the_second_side_is_kept_beside_the_first(self):
        from storage.results import copy_photos
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "input_images", "rig")
            second = Path(tmp, "input_images", "session_1", "rig")
            for side in (first, second):
                (side / "0").mkdir(parents=True)
                (side / "0" / "f_1000.jpg").write_bytes(b"x")
            folder = Path(tmp, "results", "job")
            self.assertEqual(copy_photos(first, folder, second_side=second), 2)
            self.assertTrue((folder / "photos" / "rig" / "0" / "f_1000.jpg").is_file())
            self.assertTrue((folder / "photos" / "session_1" / "rig" / "0" / "f_1000.jpg").is_file())
            # Without a second side only the first is copied
            self.assertEqual(copy_photos(first, Path(tmp, "other")), 1)


SCHEMA_PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import MechanicalConfig, StartRequest, StatusResponse, FlipResponse
def ok(fn):
    try:
        fn()
        return True
    except Exception:
        return False
print(json.dumps({
    "default": MechanicalConfig().flip,
    "on": StartRequest(mechanical={"flip": True}).mechanical.flip,
    "bad": ok(lambda: MechanicalConfig(flip="maybe")),
    "status": StatusResponse(state="awaiting_flip", session=1, flip_seconds_left=12.5).model_dump(
        include={"state", "session", "flip_seconds_left"}),
    "response": FlipResponse(state="running").model_dump(),
}))
""" % ROOT


@unittest.skip("SCAN-FLIP disabled temporarily")
class TestFlipSchema(unittest.TestCase):
    def test_flip_field_and_status(self):
        done = subprocess.run([sys.executable, "-c", SCHEMA_PROGRAM], capture_output=True, text=True, cwd=ROOT,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        r = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIs(r["default"], False)
        self.assertIs(r["on"], True)
        self.assertFalse(r["bad"])
        self.assertEqual(r["status"], {"state": "awaiting_flip", "session": 1, "flip_seconds_left": 12.5})
        self.assertEqual(r["response"], {"status": "accepted", "state": "running", "message": ""})


if __name__ == "__main__":
    unittest.main()
