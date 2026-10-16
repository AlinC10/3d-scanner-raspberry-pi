"""
First-scan readiness, scanner side (FIRST_SCAN_PLAN P1, P2, P4, P6, P7, P8). All hardware, R2, ToF and cameras are
fakes; the retry / drain constants are shrunk so the whole file runs in a few seconds.

  A upload policy        B drain + verification   C manifest          D local integrity
  E ToF truth table      F preflight              G cameras/lifecycle H status data
"""
import json
import os
import shutil
import threading
import time
import types
import unittest
from collections import namedtuple
from unittest.mock import MagicMock, patch

# Importing this module installs the module stubs and the stand-in hardware.endstop used by make_scanner()
from tests.test_scanner_motion_safety import make_scanner, scanner_module, _photo_set

ScannerState = scanner_module.ScannerState
ScannerError = scanner_module.ScannerError
ScannerUploadError = scanner_module.ScannerUploadError
ScannerPreflightError = scanner_module.ScannerPreflightError
TopEndstopTriggered = scanner_module.TopEndstopTriggered

DiskUsage = namedtuple("DiskUsage", "total used free")


class FakeR2:
    """In-memory stand-in for cloud.cloudflare_r2 following the agreed contract."""

    def __init__(self):
        self.bucket = {}            # key -> size
        self.calls = []             # (key, ok)
        self.fail_plan = {}         # key -> number of attempts that still fail
        self.fail_all = False       # the network is down
        self.lost = set()           # keys reported as uploaded that never reach the bucket
        self.delay = 0.0            # seconds each upload takes
        self.preflight_error = None
        self.preflight_calls = 0
        self.deleted = 0
        self.delete_error = None
        self.lock = threading.Lock()

    def upload_file(self, file_name, bucket=None, object_name=None):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if not os.path.isfile(file_name) or os.path.getsize(file_name) == 0:
                self.calls.append((object_name, False))
                return False
            if self.fail_all or self.fail_plan.get(object_name, 0) > 0:
                if self.fail_plan.get(object_name, 0) > 0:
                    self.fail_plan[object_name] -= 1
                self.calls.append((object_name, False))
                return False
            if object_name not in self.lost:
                self.bucket[object_name] = os.path.getsize(file_name)
            self.calls.append((object_name, True))
            return True

    def delete_all_files_from_bucket(self, bucket=None):
        if self.delete_error:
            raise RuntimeError(self.delete_error)
        with self.lock:
            self.deleted = len(self.bucket)
            self.bucket.clear()
        return self.deleted

    def list_keys(self, prefix=""):
        with self.lock:
            return {k for k in self.bucket if k.startswith(prefix)}

    def verify_keys(self, expected):
        present = self.list_keys()
        return sorted(k for k in expected if k not in present)

    def preflight(self):
        self.preflight_calls += 1
        if self.preflight_error:
            raise RuntimeError(self.preflight_error)

    def attempts(self, key):
        return sum(1 for k, _ in self.calls if k == key)


def record_history(scanner):
    """Makes `state` and `phase` of one scanner instance remember every value they were assigned."""
    history = {"state": [], "phase": []}

    def prop(name):
        slot = f"_rec_{name}"

        def get(self):
            return self.__dict__[slot]

        def set_(self, value):
            self.__dict__[slot] = value
            history[name].append(value)

        return property(get, set_)

    cls = type("RecordingScanner", (type(scanner),), {"state": prop("state"), "phase": prop("phase")})
    scanner.__dict__["_rec_state"] = scanner.__dict__.pop("state")
    scanner.__dict__["_rec_phase"] = scanner.__dict__.pop("phase")
    scanner.__class__ = cls
    return history


class ScannerTestCase(unittest.TestCase):
    """A prepared scanner with fake R2, shrunk upload constants and a ToF that always sees the object."""

    def setUp(self):
        self.r2 = FakeR2()
        patches = [
            patch.object(scanner_module, "r2", self.r2),
            patch.multiple(
                scanner_module,
                UPLOAD_BACKOFF_S=(0.01,),
                UPLOAD_BACKOFF_JITTER=0.0,
                UPLOAD_ATTEMPTS_PER_FILE=3,
                UPLOAD_PARK_RETRY_S=0.05,
                UPLOAD_DRAIN_TIMEOUT_S=1.0,
                UPLOAD_DRAIN_POLL_S=0.01,
                TURNTABLE_SETTLE_S=0.0,
                MIN_FREE_DISK_MB=1,
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.s = make_scanner()
        self.s.state = ScannerState.PREPARED
        self.cams = self.s.dual_cameras      # scan() releases s.dual_cameras at the end
        self.s.lights.brightness = 1.0
        self.s.lights.is_on = True
        # Verdict of the ToF probe per ring (level); a missing level answers `default_verdict`
        self.verdicts = {}
        self.default_verdict = True
        self.s.tof.available = True
        self.s.tof.probe_object.side_effect = lambda threshold, samples=2: self.verdicts.get(
            self.s._level, self.default_verdict
        )
        self.addCleanup(self.s.stop_upload_worker, False)

    def manifest(self):
        with open(os.path.join(self.s.images_dir, "manifest.json"), encoding="utf-8") as f:
            return json.load(f)

    def ring_levels(self):
        """The set of levels that were photographed (from the capture prefixes)."""
        prefixes = {c.kwargs["output_prefix"] for c in self.cams.capture_photo.call_args_list}
        return {int(p[2:-3]) for p in prefixes}   # "f_<level><shot on 3 digits>"

    def start_worker(self):
        self.s.start_upload_worker()

    def add_file(self, name="f_1000.jpg", content=b"jpeg", cam=0):
        cam_dir = os.path.join(self.s.images_dir, str(cam))
        os.makedirs(cam_dir, exist_ok=True)
        path = os.path.join(cam_dir, name)
        with open(path, "wb") as f:
            f.write(content)
        return path, self.s._cloud_key(path)

    def enqueue(self, path, key):
        self.s._enqueue_upload(path, key)


# ───────────────────────────────────────── A. upload policy ─────────────────────────────────────────

class TestUploadPolicy(ScannerTestCase):
    def test_cloud_key_is_rig_plus_the_relative_path(self):
        path = os.path.join(self.s.images_dir, "1", "f_2005.jpg")
        self.assertEqual(self.s._cloud_key(path), "rig/1/f_2005.jpg")
        with self.assertRaises(ValueError):
            self.s._cloud_key("/somewhere/else/f_1000.jpg")

    def test_intermittent_failures_eventually_upload_everything(self):
        self.start_worker()
        files = [self.add_file(f"f_1{i:03d}.jpg") for i in range(6)]
        for _, key in files:
            self.r2.fail_plan[key] = 2          # two transient failures, below the 3 attempts of a file
        for path, key in files:
            self.enqueue(path, key)
        self.assertEqual(self.s._drain_uploads(5.0), "done")
        self.assertEqual(set(self.r2.bucket), {k for _, k in files})
        self.assertFalse(self.s.upload_failed)
        self.assertEqual(self.s.photos_uploaded, 6)
        for _, key in files:
            self.assertEqual(self.r2.attempts(key), 3)

    def test_missing_file_is_failed_at_once_and_does_not_block_the_queue(self):
        self.start_worker()
        gone, gone_key = self.add_file("f_1000.jpg")
        os.remove(gone)
        good, good_key = self.add_file("f_1001.jpg")
        self.enqueue(gone, gone_key)
        self.enqueue(good, good_key)
        self.assertEqual(self.s._drain_uploads(5.0), "done")
        self.assertIn(good_key, self.r2.bucket)
        self.assertIn(gone, self.s._upload_failed)
        self.assertEqual(self.r2.attempts(gone_key), 0)   # never even tried: failed permanently on disk check
        self.assertTrue(self.s.upload_failed)

    def test_empty_file_is_failed_at_once(self):
        self.start_worker()
        empty, key = self.add_file("f_1000.jpg", content=b"")
        self.enqueue(empty, key)
        self.assertEqual(self.s._drain_uploads(5.0), "done")
        self.assertIn(empty, self.s._upload_failed)
        self.assertEqual(self.r2.attempts(key), 0)

    def test_failing_file_does_not_block_the_files_behind_it(self):
        self.start_worker()
        bad, bad_key = self.add_file("f_1000.jpg")
        self.r2.fail_plan[bad_key] = 10_000
        self.enqueue(bad, bad_key)
        others = [self.add_file(f"f_1{i:03d}.jpg") for i in range(1, 5)]
        for path, key in others:
            self.enqueue(path, key)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not all(k in self.r2.bucket for _, k in others):
            time.sleep(0.01)
        self.assertTrue(all(k in self.r2.bucket for _, k in others))
        self.assertNotIn(bad_key, self.r2.bucket)

    def test_file_is_parked_after_the_attempts_and_retried_later(self):
        self.start_worker()
        path, key = self.add_file()
        self.r2.fail_plan[key] = 4        # 3 attempts park it, then one parked retry fails, the next one succeeds
        self.enqueue(path, key)
        self.assertEqual(self.s._drain_uploads(5.0), "done")
        self.assertIn(key, self.r2.bucket)
        self.assertEqual(self.r2.attempts(key), 5)
        self.assertFalse(self.s.upload_failed)   # the success clears the failure

    def test_parked_file_counts_as_failed_while_it_is_parked(self):
        self.start_worker()
        path, key = self.add_file()
        self.r2.fail_all = True
        self.enqueue(path, key)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and path not in self.s._upload_failed:
            time.sleep(0.01)
        self.assertIn(path, self.s._upload_failed)
        self.assertIn(path, self.s._pending)     # still owed: the drain keeps waiting for it

    def test_the_capture_never_waits_for_a_blocked_upload(self):
        release = threading.Event()
        self.r2.upload_file = lambda *a, **k: (release.wait(5.0), True)[1]
        self.start_worker()
        started = time.monotonic()
        for shot in range(5):
            self.s._register_photos(
                self.s.images_dir, _photo_set(self.s.images_dir, shot=shot), 1, shot
            )
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()

    def test_backoff_schedule_and_constants(self):
        with patch.object(scanner_module, "UPLOAD_BACKOFF_S", (2.0, 5.0, 10.0, 20.0, 30.0)):
            delays = [self.s._backoff_delay(n) for n in range(1, 8)]
        self.assertEqual(delays, [2.0, 5.0, 10.0, 20.0, 30.0, 30.0, 30.0])

    def test_defaults_and_env_overrides(self):
        self.assertEqual(scanner_module._env_float("SCAN_NOPE", 7.0), 7.0)
        with patch.dict(os.environ, {"SCAN_X": "12.5", "SCAN_BAD": "abc", "SCAN_B": "1, 2,3"}):
            self.assertEqual(scanner_module._env_float("SCAN_X", 1.0), 12.5)
            self.assertEqual(scanner_module._env_float("SCAN_BAD", 1.0), 1.0)
            self.assertEqual(scanner_module._env_backoff("SCAN_B", (9.0,)), (1.0, 2.0, 3.0))


class TestModuleDefaults(unittest.TestCase):
    def test_documented_defaults(self):
        if any(k.startswith("SCAN_UPLOAD_") or k == "SCAN_MIN_FREE_DISK_MB" for k in os.environ):
            self.skipTest("SCAN_* overrides present in the environment")
        self.assertEqual(scanner_module.UPLOAD_ATTEMPTS_PER_FILE, 8)
        self.assertEqual(scanner_module.UPLOAD_PARK_RETRY_S, 60.0)
        self.assertEqual(scanner_module.UPLOAD_DRAIN_TIMEOUT_S, 1800.0)
        self.assertEqual(scanner_module.UPLOAD_BACKOFF_S, (2.0, 5.0, 10.0, 20.0, 30.0))
        self.assertEqual(scanner_module.MIN_FREE_DISK_MB, 2048.0)


class TestPhotoNames(ScannerTestCase):
    """Photo names are the Meshroom format f_<level><shot on 3 digits>."""

    def test_stem_format(self):
        self.assertEqual(scanner_module.photo_file_stem(1, 0), "f_1000")
        self.assertEqual(scanner_module.photo_file_stem(1, 19), "f_1019")
        self.assertEqual(scanner_module.photo_file_stem(2, 5), "f_2005")
        self.assertEqual(scanner_module.photo_file_stem(12, 7), "f_12007")
        self.assertEqual(scanner_module.photo_file_stem(3, 359), "f_3359")

    def test_names_of_different_rings_never_collide(self):
        names = {
            scanner_module.photo_file_stem(level, shot)
            for level in range(1, 30) for shot in range(scanner_module.MAX_SHOTS_PER_RING)
        }
        self.assertEqual(len(names), 29 * scanner_module.MAX_SHOTS_PER_RING)

    def test_a_ring_with_too_many_shots_is_refused_before_any_photo(self):
        # 18 deg = 160 steps -> 20 shots per revolution; with a limit of 10 the ring must be refused
        with patch.object(scanner_module, "MAX_SHOTS_PER_RING", 10):
            with self.assertRaises(ScannerError) as ctx:
                self.s._capture_turntable_slice(self.s.images_dir, 18.0, 160, 0.001, level_label=1)
        self.assertIn("too many", str(ctx.exception))


# ──────────────────────────────────── B. drain deadline and cancel ─────────────────────────────────────

class TestDrain(ScannerTestCase):
    def test_deadline_expires_without_progress(self):
        self.start_worker()
        path, key = self.add_file()
        self.r2.fail_all = True
        self.enqueue(path, key)
        started = time.monotonic()
        self.assertEqual(self.s._drain_uploads(0.3), "timeout")
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_deadline_restarts_on_every_successful_upload(self):
        # 6 files x 0.1 s = 0.6 s in total, far above the 0.35 s limit, but a file succeeds every 0.1 s
        self.r2.delay = 0.1
        self.start_worker()
        for i in range(6):
            path, key = self.add_file(f"f_1{i:03d}.jpg")
            self.enqueue(path, key)
        self.assertEqual(self.s._drain_uploads(0.35), "done")
        self.assertEqual(len(self.r2.bucket), 6)

    def test_drain_without_a_worker_reports_it(self):
        path, key = self.add_file()
        self.s._enqueue_upload(path, key)
        self.assertEqual(self.s._drain_uploads(0.3), "worker_dead")

    def test_cancel_during_the_drain_aborts_it_promptly(self):
        self.verdicts = {1: False}
        self.r2.fail_all = True
        outcome = {}

        def run():
            try:
                self.s.scan()
                outcome["returned"] = True
            except Exception as e:  # pragma: no cover - the assertion below reports it
                outcome["error"] = e

        with patch.object(scanner_module, "UPLOAD_DRAIN_TIMEOUT_S", 60.0):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and self.s.phase != "uploading":
                time.sleep(0.005)
            self.assertEqual(self.s.phase, "uploading")
            self.assertEqual(self.s.state, ScannerState.RUNNING)
            cancelled_at = time.monotonic()
            self.s.emergency_stop()
            t.join(3.0)
        self.assertFalse(t.is_alive(), "scan() did not return after the cancel")
        self.assertLess(time.monotonic() - cancelled_at, 2.0)
        self.assertEqual(outcome, {"returned": True})
        self.assertEqual(self.s.phase, "cancelled")
        self.assertEqual(self.s.state, ScannerState.CANCELLED)


# ───────────────────────────────────── B. scan: verification ─────────────────────────────────────────

class TestScanVerification(ScannerTestCase):
    def test_complete_upload_is_verified(self):
        self.verdicts = {1: True, 2: False}
        self.s.scan()
        expected = self.manifest()["expected_keys"]
        self.assertEqual(len(expected), 80)           # 2 rings x 20 shots x 2 cameras
        self.assertEqual(self.s.total_photos, 80)
        self.assertTrue(self.s.upload_verified)
        self.assertEqual(self.s.missing, [])
        self.assertEqual(sorted(self.s.uploaded_keys), sorted(expected))
        self.assertEqual(self.s.photos_uploaded, 80)
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.phase, "done")
        self.assertEqual(self.s.level, 0)

    def test_key_absent_from_the_bucket_raises_with_the_missing_list(self):
        self.verdicts = {1: False}
        lost = "rig/1/f_1007.jpg"
        self.r2.lost.add(lost)                        # uploaded "successfully" but not in the bucket
        with self.assertRaises(ScannerUploadError) as ctx:
            self.s.scan()
        self.assertEqual(ctx.exception.missing, [lost])
        self.assertIsInstance(ctx.exception, ScannerError)
        self.assertEqual(self.s.missing, [lost])
        self.assertFalse(self.s.upload_verified)
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.s.phase, "error")
        self.assertIn("1 of 40", self.s.error)
        # the local files and the manifest stay
        manifest = self.manifest()
        self.assertEqual(manifest["missing"], [lost])
        self.assertFalse(manifest["complete"])
        photos = [p["path"] for p in manifest["photos"]]
        self.assertEqual(len(photos), 40)
        self.assertTrue(all(os.path.getsize(p) > 0 for p in photos))

    def test_file_that_vanishes_before_its_upload_ends_in_missing_without_blocking(self):
        self.verdicts = {1: False}
        original = self.s._enqueue_upload

        def vanishing(path, key):
            if key == "rig/0/f_1003.jpg":
                os.remove(path)
            original(path, key)

        self.s._enqueue_upload = vanishing
        with self.assertRaises(ScannerUploadError) as ctx:
            self.s.scan()
        self.assertEqual(ctx.exception.missing, ["rig/0/f_1003.jpg"])
        self.assertEqual(len(self.r2.bucket) - 1, 39)      # +1: the manifest, 39 photos

    def test_r2_down_for_the_whole_drain_ends_in_error_with_every_photo_missing(self):
        self.verdicts = {1: False}
        self.r2.fail_all = True
        started = time.monotonic()
        with self.assertRaises(ScannerUploadError) as ctx:
            self.s.scan()
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(len(ctx.exception.missing), 40)
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.s.total_photos, 40)             # the physical scan did finish
        photos = self.manifest()["photos"]
        self.assertTrue(all(os.path.isfile(p["path"]) for p in photos))   # the photos stay on disk

    def test_missing_attribute_is_capped_but_the_manifest_keeps_everything(self):
        self.verdicts = {1: True, 2: False}
        self.r2.fail_all = True
        with self.assertRaises(ScannerUploadError) as ctx:
            self.s.scan()
        self.assertEqual(len(ctx.exception.missing), 80)
        self.assertEqual(len(self.s.missing), 50)
        self.assertEqual(self.s.missing, sorted(ctx.exception.missing)[:50])
        self.assertEqual(len(self.manifest()["missing"]), 80)

    def test_bucket_listing_failure_is_not_reported_as_verified(self):
        self.verdicts = {1: False}

        def boom(expected):
            raise RuntimeError("listing denied")

        self.r2.verify_keys = boom
        with self.assertRaises(ScannerUploadError) as ctx:
            self.s.scan()
        self.assertIn("listing denied", str(ctx.exception))
        self.assertFalse(self.s.upload_verified)

    def test_failed_scan_does_not_wait_for_the_upload_backlog(self):
        self.r2.fail_all = True
        self.verdicts = {1: True}
        calls = {"n": 0}
        real = self.s.dual_cameras.capture_photo.side_effect

        def capture(**kwargs):
            calls["n"] += 1
            if calls["n"] > 25:
                raise RuntimeError("camera exploded")
            return real(**kwargs)

        self.s.dual_cameras.capture_photo.side_effect = capture
        with patch.object(scanner_module, "UPLOAD_DRAIN_TIMEOUT_S", 60.0):
            started = time.monotonic()
            with self.assertRaises(ScannerError):
                self.s.scan()
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.s.phase, "error")
        self.assertIn("camera exploded", self.s.error)

    def test_hardware_is_released_before_the_drain_and_state_is_never_idle_early(self):
        self.verdicts = {1: True, 2: False}
        history = record_history(self.s)
        seen = {}
        original = self.r2.verify_keys

        def spying_verify(expected):
            seen["state"] = self.s.state
            seen["phase"] = self.s.phase
            seen["lights_off"] = self.s.lights.off.called
            seen["cameras_released"] = self.s.dual_cameras is None
            seen["motors_stopped"] = self.s.turntable_motor.stop.called
            return original(expected)

        self.r2.verify_keys = spying_verify
        self.r2.delay = 0.002
        self.s.scan(finish_state=ScannerState.PROCESSING)

        self.assertEqual(seen, {"state": ScannerState.RUNNING, "phase": "verifying", "lights_off": True,
                                "cameras_released": True, "motors_stopped": True})
        # RUNNING until the very end, then straight to the requested state, never through IDLE
        self.assertEqual(history["state"], [ScannerState.RUNNING, ScannerState.PROCESSING])

    def test_lights_and_cameras_are_off_while_the_upload_is_still_running(self):
        self.verdicts = {1: False}
        seen = []
        real_upload = self.r2.upload_file

        def spy(file_name, bucket=None, object_name=None):
            if object_name and object_name.endswith("f_1019.jpg"):   # a late file of the only ring
                seen.append((self.s.lights.off.called, self.s.dual_cameras is None, self.s.phase))
            return real_upload(file_name, bucket, object_name)

        self.r2.upload_file = spy
        self.r2.delay = 0.005
        self.s.scan()
        self.assertTrue(seen)

    def test_phase_sequence(self):
        self.verdicts = {1: True, 2: False}
        history = record_history(self.s)
        self.s.scan()
        phases = [p for i, p in enumerate(history["phase"]) if i == 0 or p != history["phase"][i - 1]]
        self.assertEqual(phases, ["scanning", "uploading", "verifying", "done"])

    def test_status_attributes_are_reset_by_the_next_prepare(self):
        self.verdicts = {1: False}
        self.r2.lost.add("rig/0/f_1000.jpg")
        with self.assertRaises(ScannerUploadError):
            self.s.scan()
        self.assertTrue(self.s.missing)
        with patch.object(scanner_module, "DualCamera"):
            self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.s.phase, "preparing")
        self.assertEqual((self.s.missing, self.s.error, self.s.photos_uploaded, self.s.total_photos),
                         ([], None, 0, 0))
        self.assertEqual(self.s.photos_captured, 0)


# ─────────────────────────────────────── C. manifest ───────────────────────────────────────────

class TestManifest(ScannerTestCase):
    def test_manifest_content(self):
        self.verdicts = {1: False}
        cams = self.s.dual_cameras
        real = cams.capture_photo.side_effect

        def capture(**kwargs):
            paths = real(**kwargs)
            cams.last_capture_info = {
                "paths": list(paths),
                "cameras": [
                    {"exposure_time": 10000 + i, "analogue_gain": 1.5, "colour_gains": [1.8, 1.6],
                     "sensor_timestamp": 111 + i, "width": 4056, "height": 3040}
                    for i in range(2)
                ],
            }
            return paths

        cams.capture_photo.side_effect = capture
        self.s.scan()

        m = self.manifest()
        self.assertEqual(set(m), {"scan_id", "started_at", "expected_keys", "photos", "uploaded", "missing", "complete"})
        self.assertTrue(m["scan_id"])
        self.assertEqual(len(m["photos"]), 40)
        self.assertEqual(m["expected_keys"], [p["key"] for p in m["photos"]])
        self.assertEqual(m["uploaded"], sorted(m["expected_keys"]))
        self.assertEqual(m["missing"], [])
        self.assertTrue(m["complete"])
        first = m["photos"][0]
        self.assertEqual(first["key"], "rig/0/f_1000.jpg")
        self.assertEqual((first["level"], first["shot"], first["camera"], first["size"]), (1, 0, 0, 4))
        self.assertEqual(first["exposure_time"], 10000)
        self.assertEqual(first["analogue_gain"], 1.5)
        self.assertEqual(first["colour_gains"], [1.8, 1.6])
        self.assertEqual(first["sensor_timestamp"], 111)
        self.assertEqual(m["photos"][1]["camera"], 1)
        self.assertEqual(m["photos"][1]["exposure_time"], 10001)

    def test_manifest_is_uploaded_after_verification_outside_the_photo_set(self):
        self.verdicts = {1: False}
        order = []
        original_verify = self.r2.verify_keys
        original_upload = self.r2.upload_file
        self.r2.verify_keys = lambda expected: (order.append("verify"), original_verify(expected))[1]

        def upload(file_name, bucket=None, object_name=None):
            if object_name == "rig/_manifest.json":
                order.append("manifest")
            return original_upload(file_name, bucket, object_name)

        self.r2.upload_file = upload
        self.s.scan()
        self.assertEqual(order, ["verify", "manifest"])
        self.assertIn("rig/_manifest.json", self.r2.bucket)
        self.assertNotIn("rig/_manifest.json", self.manifest()["expected_keys"])

    def test_manifest_upload_failure_is_best_effort(self):
        self.verdicts = {1: False}
        original = self.r2.upload_file

        def upload(file_name, bucket=None, object_name=None):
            if object_name == "rig/_manifest.json":
                raise RuntimeError("denied")
            return original(file_name, bucket, object_name)

        self.r2.upload_file = upload
        self.s.scan()  # must not raise
        self.assertTrue(self.s.upload_verified)

    def test_manifest_without_exposure_info_has_nulls(self):
        self.verdicts = {1: False}
        self.s.scan()
        first = self.manifest()["photos"][0]
        self.assertIsNone(first["exposure_time"])
        self.assertIsNone(first["colour_gains"])

    def test_stale_capture_info_of_another_capture_is_ignored(self):
        self.verdicts = {1: False}
        self.s.dual_cameras.last_capture_info = {
            "paths": ["/not/this/capture.jpg"], "cameras": [{"exposure_time": 1}, {"exposure_time": 2}]
        }
        self.s.scan()
        self.assertIsNone(self.manifest()["photos"][0]["exposure_time"])

    def test_manifest_write_is_atomic(self):
        self.s._begin_manifest(self.s.images_dir)
        self.s._register_photos(self.s.images_dir, _photo_set(self.s.images_dir), 1, 0)
        before = self.manifest()
        self.assertEqual(len(before["photos"]), 2)

        seen_tmp = []
        real_replace = os.replace

        def failing_replace(src, dst):
            seen_tmp.append(os.path.basename(src))
            # the temp file holds the complete new content while the real one is still the old, valid JSON
            with open(src, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["photos"]), 4)
            with open(dst, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["photos"]), 2)
            raise OSError("disk error")

        with patch.object(scanner_module.os, "replace", failing_replace):
            with self.assertRaises(ScannerError):
                self.s._register_photos(self.s.images_dir, _photo_set(self.s.images_dir, shot=1), 1, 1)
        self.assertEqual(seen_tmp, ["manifest.json.tmp"])
        self.assertEqual(self.manifest(), before)       # the old manifest is intact
        self.assertIs(scanner_module.os.replace, real_replace)

    def test_manifest_is_written_after_every_photo_pair(self):
        self.s._begin_manifest(self.s.images_dir)
        for shot in range(3):
            self.s._register_photos(self.s.images_dir, _photo_set(self.s.images_dir, shot=shot), 1, shot)
            self.assertEqual(len(self.manifest()["photos"]), 2 * (shot + 1))

    def test_scan_starts_with_a_clean_directory_and_an_empty_manifest(self):
        self.verdicts = {1: False}
        self.s.scan()
        scanner_module.fc.create_clean_dir.assert_called_with(self.s.images_dir)

    def test_directory_cleanup_failure_is_a_clear_error(self):
        scanner_module.fc.create_clean_dir.side_effect = OSError("read-only file system")
        try:
            with self.assertRaises(ScannerError) as ctx:
                self.s.scan()
            self.assertIn("local photo directory", str(ctx.exception))
            self.assertEqual(self.s.state, ScannerState.ERROR)
        finally:
            scanner_module.fc.create_clean_dir.side_effect = None
        self.s.dual_cameras = None
        self.assertEqual(self.s.total_photos, 0)

    def test_leftover_files_after_the_cleanup_are_an_error(self):
        self.add_file("old.jpg")        # a cleanup that did not remove everything
        with self.assertRaises(ScannerError):
            self.s.scan()
        self.assertEqual(self.s.total_photos, 0)


# ───────────────────────────────────── D. local integrity ──────────────────────────────────────────

class TestLocalIntegrity(ScannerTestCase):
    def test_zero_byte_capture_raises(self):
        def capture(output_prefix="x", output_dir=".", **kwargs):
            paths = _photo_set(output_dir, shot=0, content=b"")
            return paths

        self.s.dual_cameras.capture_photo.side_effect = capture
        with self.assertRaises(ScannerError) as ctx:
            self.s.scan()
        self.assertIn("empty", str(ctx.exception))
        self.assertEqual(self.s.total_photos, 0)
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.r2.calls, [])      # the empty file never reached the uploader

    def test_missing_file_after_a_capture_raises(self):
        self.s.dual_cameras.capture_photo.side_effect = lambda **kw: ["/nonexistent/0/a.jpg", "/nonexistent/1/a.jpg"]
        with self.assertRaises(ScannerError):
            self.s.scan()

    def test_empty_result_raises(self):
        self.s.dual_cameras.capture_photo.side_effect = lambda **kw: []
        with self.assertRaises(ScannerError):
            self.s.scan()

    def test_driver_capture_error_is_wrapped(self):
        self.s.dual_cameras.capture_photo.side_effect = scanner_module.CaptureError("no frame received")
        with self.assertRaises(ScannerError) as ctx:
            self.s.scan()
        self.assertIn("no frame received", str(ctx.exception))
        self.assertIn("level 1", str(ctx.exception))

    def test_black_first_photo_stops_the_scan_with_a_clear_message(self):
        self.s._mean_luma = lambda path: 0.4
        with self.assertRaises(ScannerError) as ctx:
            self.s.scan()
        self.assertIn("black", str(ctx.exception))
        self.assertIn("lens cap", str(ctx.exception))
        self.assertEqual(self.s.total_photos, 0)
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.r2.calls, [])      # nothing black was uploaded

    def test_only_the_first_pair_is_checked(self):
        calls = []

        def luma(path):
            calls.append(path)
            return 80.0

        self.s._mean_luma = luma
        self.verdicts = {1: True, 2: False}
        self.s.scan()
        self.assertEqual(len(calls), 2)          # one pair, not every photo

    def test_black_frame_check_can_be_disabled(self):
        self.s._mean_luma = lambda path: 0.0
        self.verdicts = {1: False}
        with patch.dict(os.environ, {"SCAN_BLACK_FRAME_CHECK": "0"}):
            self.s.scan()
        self.assertGreater(self.s.total_photos, 0)

    def test_unreadable_photo_is_not_called_black(self):
        self.s._mean_luma = lambda path: None
        self.verdicts = {1: False}
        self.s.scan()
        self.assertGreater(self.s.total_photos, 0)

    def test_photo_outside_the_photo_tree_is_rejected(self):
        with self.assertRaises(ScannerError):
            self.s._register_photos(self.s.images_dir, ["/etc/hostname"], 1, 0)
        self.assertEqual(self.s.total_photos, 0)

    def test_manifest_count_equals_total_photos(self):
        self.verdicts = {1: True, 2: False}
        self.s.scan()
        self.assertEqual(len(self.manifest()["photos"]), self.s.total_photos)

    def test_count_mismatch_is_an_error(self):
        self.verdicts = {1: False}
        real = self.s._register_photos

        def register_and_forget(*args, **kwargs):
            real(*args, **kwargs)
            self.s.total_photos += 1       # a photo counted but not listed

        self.s._register_photos = register_and_forget
        with self.assertRaises(ScannerError) as ctx:
            self.s.scan()
        self.assertIn("manifest", str(ctx.exception))


# ───────────────────────────────────────── E. ToF ────────────────────────────────────────────────

class TestToFPolicy(ScannerTestCase):
    def slice(self, check_tof=True):
        return self.s._capture_turntable_slice(
            images_dir=self.s.images_dir, angle=18.0, steps=160, delay_turntable=0.001, check_tof=check_tof
        )

    def probe_results(self, results):
        it = iter(results)
        self.s.tof.probe_object.side_effect = lambda threshold, samples=2: next(it)

    def test_valid_backdrop_readings_clear_the_ring(self):
        self.probe_results([False] * 20)
        self.assertIs(self.slice(), False)

    def test_object_seen_at_any_stop_wins_and_stops_probing(self):
        self.probe_results([False, None, True] + [False] * 17)
        self.assertIs(self.slice(), True)
        self.assertEqual(self.s.tof.probe_object.call_count, 3)

    def test_invalid_readings_alone_are_unknown(self):
        self.probe_results([None] * 20)
        self.assertIsNone(self.slice())

    def test_one_valid_backdrop_among_many_invalid_readings_stays_unknown(self):
        self.probe_results([None] * 7 + [False] + [None] * 12)
        self.assertIsNone(self.slice())

    def test_valid_backdrop_majority_with_a_few_invalid_clears_it(self):
        self.probe_results([None] * 3 + [False] * 17)
        self.assertIs(self.slice(), False)

    def test_non_boolean_answer_is_unknown(self):
        self.probe_results([MagicMock()] * 20)
        self.assertIsNone(self.slice())

    def test_probe_exception_is_unknown_not_a_crash(self):
        def boom(threshold, samples=2):
            raise OSError("i2c")

        self.s.tof.probe_object.side_effect = boom
        self.assertIsNone(self.slice())

    def test_threshold_is_the_backdrop_minus_the_margin(self):
        self.probe_results([True])
        self.slice()
        threshold = scanner_module.TOF_BACKGROUND_DISTANCE_MM - scanner_module.TOF_CLEARANCE_MARGIN_MM
        self.assertEqual(self.s.tof.probe_object.call_args.args[0], threshold)

    def test_ring_without_tof_check_reports_false(self):
        self.assertIs(self.slice(check_tof=False), False)
        self.s.tof.probe_object.assert_not_called()

    def test_valid_backdrop_ends_the_scan_early_and_reports_it_at_the_first_ring(self):
        self.verdicts = {1: False}
        self.s.scan()
        self.assertEqual(self.ring_levels(), {1})
        self.assertIn("first ring", self.s.phase_message)
        self.assertEqual(self.s.tof_warnings, [])

    def test_object_present_continues_the_climb(self):
        self.verdicts = {1: True, 2: True, 3: False}
        self.s.scan()
        self.assertEqual(self.ring_levels(), {1, 2, 3})
        self.assertEqual(self.s.tof_warnings, [])
        self.assertEqual(self.s.z_axis_motor.rotate.call_count, 2)

    def test_invalid_readings_continue_and_warn(self):
        self.verdicts = {1: None, 2: False}
        self.s.scan()
        self.assertEqual(self.ring_levels(), {1, 2})            # did not end after ring 1
        self.assertEqual(len(self.s.tof_warnings), 1)
        self.assertIn("Level 1", self.s.tof_warnings[0])
        self.assertIn("first ring", self.s.phase_message)       # unknown at the first ring is reported too

    def test_all_invalid_ring_warns_and_continues_up_to_the_endstop(self):
        self.default_verdict = None
        climbs = {"n": 0}

        def rotate(**kwargs):
            climbs["n"] += 1
            if climbs["n"] == 2:
                self.s.up_endstop.is_active = True      # the second climb ends on the top switch
            return True

        self.s.z_axis_motor.rotate.side_effect = rotate
        self.s.scan()
        # rings 1 and 2 (unknown) plus the final ring at the ceiling
        self.assertEqual(self.ring_levels(), {1, 2, 3})
        self.assertEqual(len(self.s.tof_warnings), 2)
        self.assertEqual(self.s.total_photos, 120)
        self.assertTrue(self.s.upload_verified)

    def test_tof_warnings_are_cleared_by_the_next_scan(self):
        self.verdicts = {1: None, 2: False}
        self.s.scan()
        self.assertTrue(self.s.tof_warnings)
        self.s.state = ScannerState.PREPARED
        self.s.dual_cameras = self.cams
        for name in os.listdir(self.s.images_dir):      # what the real create_clean_dir does between two scans
            full = os.path.join(self.s.images_dir, name)
            shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
        self.verdicts = {1: False}
        self.s.scan()
        self.assertEqual(self.s.tof_warnings, [])


# ───────────────────────────────────────── F. preflight ──────────────────────────────────────────

class TestPreflight(ScannerTestCase):
    def setUp(self):
        super().setUp()
        self.s.state = ScannerState.IDLE
        self.camera_class = patch.object(scanner_module, "DualCamera").start()
        self.addCleanup(patch.stopall)

    def assert_refused(self, fragment):
        with self.assertRaises(ScannerPreflightError) as ctx:
            self.s.setup_and_stream(enable_stream=False)
        self.assertIn(fragment, str(ctx.exception))
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertFalse(getattr(ctx.exception, "conflict", False))
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.phase, "error")
        self.assertIn(fragment, self.s.error)
        self.camera_class.assert_not_called()
        self.s.lights.on.assert_not_called()
        return ctx.exception

    def test_cloud_failure_leaves_the_state_idle(self):
        self.r2.preflight_error = "write rights missing"
        self.assert_refused("write rights missing")

    def test_missing_tof_sensor_leaves_the_state_idle(self):
        self.s.tof.available = False
        self.assert_refused("ToF")

    def test_tof_without_a_valid_test_reading_leaves_the_state_idle(self):
        self.s.tof.read_distance_mm.return_value = None
        self.assert_refused("no valid test reading")

    def test_tof_test_reading_exception_leaves_the_state_idle(self):
        self.s.tof.read_distance_mm.side_effect = OSError("i2c")
        self.assert_refused("test reading")

    def test_low_disk_space_leaves_the_state_idle(self):
        with patch.object(scanner_module, "MIN_FREE_DISK_MB", 2048.0), \
                patch.object(scanner_module.shutil, "disk_usage", return_value=DiskUsage(10**12, 10**12, 500 * 1024 ** 2)):
            self.assert_refused("500 MB")

    def test_first_failure_short_circuits(self):
        reads = []

        class Tof:
            @property
            def available(self):
                reads.append(1)
                return False

        self.s.tof = Tof()
        self.r2.preflight_error = "bad credentials"
        with patch.object(scanner_module.shutil, "disk_usage") as disk:
            self.assert_refused("bad credentials")
        self.assertEqual(reads, [])
        disk.assert_not_called()

    def test_passing_preflight_prepares_normally(self):
        self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.r2.preflight_calls, 1)
        self.assertEqual(self.s.state, ScannerState.PREPARING)

    def test_switch_skips_the_checks(self):
        self.r2.preflight_error = "would fail"
        self.s.tof.available = False
        with patch.dict(os.environ, {"SCAN_PREFLIGHT": "0"}):
            self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.r2.preflight_calls, 0)
        self.assertEqual(self.s.state, ScannerState.PREPARING)

    def test_wrong_state_is_refused_before_any_preflight_work(self):
        self.s.state = ScannerState.RUNNING
        with self.assertRaises(RuntimeError):
            self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.r2.preflight_calls, 0)
        self.assertEqual(self.s.state, ScannerState.RUNNING)

    def test_manual_test_endpoints_can_skip_the_preflight(self):
        self.r2.preflight_error = "cloud is down"
        self.s.tof.available = False
        self.s.setup_and_stream(enable_stream=False, preflight=False)
        self.assertEqual(self.r2.preflight_calls, 0)
        self.assertEqual(self.s.state, ScannerState.PREPARING)

    def test_preflight_can_be_called_on_its_own(self):
        self.s.preflight()
        self.r2.preflight_error = "x"
        with self.assertRaises(ScannerPreflightError):
            self.s.preflight()


# ───────────────────────────────── G. cameras and lifecycle ──────────────────────────────────────

class RecordingCameras:
    """A camera system that records prepare_scan() kwargs; accepts lock_auto like the sync driver."""
    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.prepare_calls = []
        self.is_streaming = False
        self.closed = False
        RecordingCameras.instances.append(self)

    def prepare_scan(self, lock_auto=False, **kwargs):
        self.prepare_calls.append({"lock_auto": lock_auto, **kwargs})
        return []

    def start_stream(self, bitrate=0):
        return ["/s/1", "/s/2"]

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class TestLifecycle(ScannerTestCase):
    def setUp(self):
        super().setUp()
        self.s.state = ScannerState.IDLE
        RecordingCameras.instances = []

    def finish(self):
        """Runs finish_preparation with the motor part short-circuited the way the existing tests do."""
        self.s.down_endstop.is_active = True
        self.s.wait_for_light_stable = lambda **kwargs: True
        self.s.finish_preparation()

    def test_stale_cameras_and_brightness_lock_are_released_first(self):
        stale = MagicMock()
        self.s.dual_cameras = stale
        order = MagicMock()
        order.attach_mock(stale.close, "stale_close")
        order.attach_mock(self.s.lights.unlock_brightness, "unlock")
        order.attach_mock(self.s.lights.on, "lights_on")
        with patch.object(scanner_module, "DualCamera", side_effect=lambda **kw: order.new_camera() or MagicMock()):
            self.s.setup_and_stream(enable_stream=False)
        stale.stop_stream.assert_called_once()
        stale.close.assert_called_once()
        names = [c[0] for c in order.mock_calls]
        self.assertLess(names.index("stale_close"), names.index("new_camera"))
        self.assertLess(names.index("unlock"), names.index("lights_on"))
        self.assertIsNot(self.s.dual_cameras, stale)

    def test_cancel_during_camera_construction_releases_everything(self):
        built = []

        def construct(**kwargs):
            cam = MagicMock()
            built.append(cam)
            self.s._cancel_event.set()          # emergency_stop() landed while the cameras were being built
            self.s.state = ScannerState.CANCELLED
            return cam

        with patch.object(scanner_module, "DualCamera", side_effect=construct):
            result = self.s.setup_and_stream(enable_stream=False)
        self.assertIsNone(result)
        built[0].close.assert_called()
        self.assertIsNone(self.s.dual_cameras)
        self.s.lights.off.assert_called()
        self.assertEqual(self.s.phase, "cancelled")
        built[0].prepare_scan.assert_not_called()

    def test_cancel_during_prepare_scan_releases_everything(self):
        cam = MagicMock()
        cam.prepare_scan.side_effect = lambda **kw: self.s._cancel_event.set()
        with patch.object(scanner_module, "DualCamera", return_value=cam):
            result = self.s.setup_and_stream(enable_stream=True)
        self.assertIsNone(result)
        cam.close.assert_called()
        cam.start_stream.assert_not_called()
        self.assertIsNone(self.s.dual_cameras)

    def test_prepare_after_a_cancelled_prepare_works(self):
        cam = MagicMock()
        cam.prepare_scan.side_effect = lambda **kw: self.s._cancel_event.set()
        with patch.object(scanner_module, "DualCamera", return_value=cam):
            self.s.setup_and_stream(enable_stream=False)
        self.s.state = ScannerState.CANCELLED
        with patch.object(scanner_module, "DualCamera", return_value=MagicMock()):
            self.s.setup_and_stream(enable_stream=False)
        self.assertEqual(self.s.state, ScannerState.PREPARING)
        self.assertFalse(self.s._cancel_event.is_set())

    def test_lock_auto_is_passed_only_in_the_recalibration_call(self):
        with patch.object(scanner_module, "DualCameraRPISync", RecordingCameras):
            self.s.setup_and_stream(enable_stream=False, camera_mode="rpi_sync")
            cams = self.s.dual_cameras
            self.assertIsInstance(cams, RecordingCameras)
            self.assertEqual(len(cams.prepare_calls), 1)
            self.assertFalse(cams.prepare_calls[0]["lock_auto"])
            self.finish()
        self.assertEqual(self.s.state, ScannerState.PREPARED)
        self.assertEqual(len(cams.prepare_calls), 2)
        self.assertTrue(cams.prepare_calls[1]["lock_auto"])
        self.assertEqual(self.s.phase, "prepared")

    def test_lock_auto_is_not_sent_to_drivers_that_do_not_support_it(self):
        class Legacy(RecordingCameras):
            def prepare_scan(self, **kwargs):
                if "lock_auto" in kwargs:
                    raise AssertionError("lock_auto must not reach this driver")
                self.prepare_calls.append(kwargs)
                return []

        with patch.object(scanner_module, "DualCamera", Legacy):
            self.s.setup_and_stream(enable_stream=False, camera_mode="staggered")
            self.finish()
        self.assertEqual(self.s.state, ScannerState.PREPARED)

    def test_zero_brightness_is_refused_before_lights_and_bucket(self):
        self.s.lights.brightness = 0
        self.r2.bucket["rig/0/old.jpg"] = 1
        with patch.object(scanner_module, "DualCamera") as camera_class:
            with self.assertRaises(ScannerError) as ctx:
                self.s.setup_and_stream(enable_stream=False)
        self.assertIn("Brightness is 0%", str(ctx.exception))
        self.s.lights.on.assert_not_called()
        camera_class.assert_not_called()
        self.assertEqual(self.r2.bucket, {"rig/0/old.jpg": 1})   # nothing was wiped
        self.assertEqual(self.s.state, ScannerState.IDLE)
        self.assertEqual(self.s.phase, "error")

    def test_zero_brightness_after_prepare_is_refused_before_the_bucket_wipe(self):
        self.s.state = ScannerState.PREPARING
        self.s.dual_cameras = RecordingCameras()
        self.s.lights.brightness = 0
        self.r2.bucket["rig/0/old.jpg"] = 1
        self.finish()
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.r2.bucket, {"rig/0/old.jpg": 1})
        self.assertEqual(self.s.dual_cameras, None)

    def test_bucket_wipe_failure_is_a_clear_error_before_the_recalibration(self):
        cams = RecordingCameras()
        self.s.dual_cameras = cams
        self.s.state = ScannerState.PREPARING
        self.r2.delete_error = "Errors in delete_objects"
        self.finish()
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.s.phase, "error")
        self.assertIn("Cloud sanitization failed", self.s.error)
        self.assertIn("Errors in delete_objects", self.s.error)
        self.assertEqual(cams.prepare_calls, [])

    def test_bitrate_is_ignored_and_the_stream_starts_without_one(self):
        started = []

        class Cams(RecordingCameras):
            def start_stream(self, *args, **kwargs):
                started.append((args, kwargs))
                return ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"]

        with patch.object(scanner_module, "DualCameraRPISync", Cams):
            urls = self.s.setup_and_stream(enable_stream=True, camera_mode="rpi_sync", bitrate=9_000_000)
        self.assertEqual(urls, ["/test/stream/mjpeg?camera=1", "/test/stream/mjpeg?camera=2"])
        self.assertEqual(started, [((), {})])
        self.assertNotIn("bitrate", self.s.dual_cameras.prepare_calls[0])

    def test_rotation_goes_to_the_camera_constructor_not_to_prepare_scan(self):
        with patch.object(scanner_module, "DualCameraRPISync", RecordingCameras):
            self.s.setup_and_stream(enable_stream=False, camera_mode="rpi_sync", rotation=[90, 270])
        cams = self.s.dual_cameras
        self.assertEqual(cams.init_kwargs["rotation1"], 90)
        self.assertEqual(cams.init_kwargs["rotation2"], 270)
        self.assertNotIn("rotation", cams.prepare_calls[0])

    def test_single_rotation_value_applies_to_both_cameras(self):
        with patch.object(scanner_module, "DualCameraRPISync", RecordingCameras):
            self.s.setup_and_stream(enable_stream=False, camera_mode="rpi_sync", rotation=180)
        self.assertEqual(self.s.dual_cameras.init_kwargs["rotation1"], 180)
        self.assertEqual(self.s.dual_cameras.init_kwargs["rotation2"], 180)

    def test_preview_resolution_sizes_the_stream_of_the_sync_driver(self):
        with patch.object(scanner_module, "DualCameraRPISync", RecordingCameras):
            self.s.setup_and_stream(enable_stream=False, camera_mode="rpi_sync", preview_resolution=(640, 480))
        self.assertEqual(self.s.dual_cameras.init_kwargs["stream_size"], (640, 480))

    def test_focus_auto_reaches_both_prepare_calls_and_only_the_second_locks(self):
        with patch.object(scanner_module, "DualCameraRPISync", RecordingCameras):
            self.s.setup_and_stream(enable_stream=False, camera_mode="rpi_sync", focus="auto")
            cams = self.s.dual_cameras
            # the router hands the same camera_config to finish_preparation
            self.s.down_endstop.is_active = True
            self.s.wait_for_light_stable = lambda **kwargs: True
            self.s.finish_preparation(focus="auto")
        self.assertEqual([c["focus"] for c in cams.prepare_calls], ["auto", "auto"])
        self.assertEqual([c["lock_auto"] for c in cams.prepare_calls], [False, True])

    def test_photo_root_is_the_rig_folder(self):
        root = os.path.normpath(scanner_module.INPUT_IMAGES_DIR)
        self.assertEqual(os.path.basename(root), "rig")
        self.assertEqual(os.path.basename(os.path.dirname(root)), "input_images")

    def test_failed_exposure_lock_is_an_error(self):
        cams = RecordingCameras()
        cams.prepare_scan = lambda lock_auto=False, **kw: [
            {"locked": True, "ready": True}, {"locked": False, "ready": True}]
        self.s.dual_cameras = cams
        self.s._lock_auto_supported = True
        self.s.state = ScannerState.PREPARING
        self.finish()
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertIn("AE/AWB lock failed", self.s.error)

    def test_successful_exposure_lock_prepares(self):
        cams = RecordingCameras()
        cams.prepare_scan = lambda lock_auto=False, **kw: [
            {"locked": True, "ready": True}, {"locked": True, "ready": True}]
        self.s.dual_cameras = cams
        self.s._lock_auto_supported = True
        self.s.state = ScannerState.PREPARING
        self.finish()
        self.assertEqual(self.s.state, ScannerState.PREPARED)

    def test_bucket_is_wiped_before_the_recalibration(self):
        self.r2.bucket["rig/0/old.jpg"] = 1
        cams = RecordingCameras()
        self.s.dual_cameras = cams
        self.s.state = ScannerState.PREPARING
        cams.prepare_scan = lambda **kw: cams.prepare_calls.append((dict(kw), dict(self.r2.bucket)))
        self.finish()
        self.assertEqual(cams.prepare_calls[0][1], {})
        self.assertEqual(self.s.state, ScannerState.PREPARED)

    def test_two_consecutive_scans_in_one_process(self):
        self.s.state = ScannerState.PREPARED
        self.verdicts = {1: False}
        self.s.scan()
        self.assertEqual(self.s.state, ScannerState.IDLE)
        first_gen = self.s._upload_gen
        self.s.dual_cameras = MagicMock()        # prepared again
        self.s.state = ScannerState.PREPARED
        with patch.object(scanner_module.fc, "create_clean_dir") as clean:
            # the real cleanup is a mock here: wipe the directory by hand like the real one would
            def real_clean(path):
                shutil.rmtree(path, ignore_errors=True)
                os.makedirs(path, exist_ok=True)

            clean.side_effect = real_clean
            cams = self.s.dual_cameras

            def capture(output_prefix="x", output_dir=".", **kwargs):
                return _photo_set(output_dir, level=1, shot=int(output_prefix.rsplit("_", 1)[1]))

            cams.capture_photo.side_effect = capture
            self.s.scan()
        self.assertGreater(self.s._upload_gen, first_gen)
        self.assertTrue(self.s.upload_verified)
        self.assertEqual(self.s.state, ScannerState.IDLE)


# ──────────────────────────────────── H. status data ──────────────────────────────────────────────

class TestStatusData(ScannerTestCase):
    def test_initial_values(self):
        s = make_scanner()
        self.assertEqual(s.phase, "idle")
        self.assertIsNone(s.phase_message)
        self.assertEqual((s.photos_captured, s.photos_uploaded, s.missing, s.tof_warnings), (0, 0, [], []))
        self.assertIsNone(s.error)
        self.assertEqual(s.level, 0)

    def test_photos_captured_follows_total_photos(self):
        self.s.total_photos = 7
        self.assertEqual(self.s.photos_captured, 7)

    def test_emergency_stop_sets_the_cancelled_phase(self):
        self.s.emergency_stop()
        self.assertEqual(self.s.phase, "cancelled")
        self.assertEqual(self.s.state, ScannerState.CANCELLED)

    def test_failed_preparation_publishes_error_text(self):
        self.s.state = ScannerState.PREPARING
        self.s.dual_cameras = MagicMock()
        self.s.z_axis_motor.rotate.return_value = True      # homing never reaches the switch
        self.s.finish_preparation()
        self.assertEqual(self.s.state, ScannerState.ERROR)
        self.assertEqual(self.s.phase, "error")
        self.assertIn("Homing failed", self.s.error)

    def test_level_is_visible_while_scanning(self):
        levels = []
        real = self.s.dual_cameras.capture_photo.side_effect

        def capture(**kwargs):
            levels.append((self.s.level, self.s.phase, self.s.photos_captured))
            return real(**kwargs)

        self.s.dual_cameras.capture_photo.side_effect = capture
        self.verdicts = {1: True, 2: False}
        self.s.scan()
        self.assertEqual({lv for lv, _, _ in levels}, {1, 2})
        self.assertEqual({ph for _, ph, _ in levels}, {"scanning"})
        self.assertEqual([n for _, _, n in levels[:3]], [0, 2, 4])


if __name__ == "__main__":
    unittest.main()
