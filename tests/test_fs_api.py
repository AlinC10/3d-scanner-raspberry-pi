"""
First-scan API packages (P0, P8, P10; audit API-1, API-9, API-10, API-11, API-13, API-14, CLOUD-11, PERI-8):
  - request schemas, checked with the REAL pydantic in a subprocess (tests/__init__.py replaces pydantic with a mock)
  - router/scanner.py, loaded by file path with fake modules (no hardware, no network, no real RunPod)
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ----------------------------------------------------------------------------------------------------------
# Schemas (real pydantic, subprocess)
# ----------------------------------------------------------------------------------------------------------
PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import (CloudConfig, StartRequest, MechanicalConfig, TurntableScanConfig, ZAxisScanConfig,
                             TurntableRotateRequest, ZMoveRequest, DualCameraConfig, PrepareRequest, StatusResponse)

def ok(factory):
    try:
        factory()
        return True
    except Exception:
        return False

legacy = StartRequest(mechanical={"turntable": {"angle": 18.0, "clockwise": False},
                                  "z_axis": {"distance_mm": 50.0, "direction": "down"}})
status = StatusResponse(state="idle")

print(json.dumps({
    "launch_default": CloudConfig().launch_runpod,
    "launch_true": CloudConfig(launch_runpod=True).launch_runpod,
    "launch_false": CloudConfig(launch_runpod=False).launch_runpod,
    "start_default_launch": StartRequest().cloud.launch_runpod,

    "unknown_mechanical_angle": ok(lambda: StartRequest(mechanical={"angle": 18.0})),
    "unknown_mechanical_z_move": ok(lambda: MechanicalConfig(z_move_mm=50.0)),
    "unknown_top_level": ok(lambda: StartRequest(foo=1)),
    "unknown_cloud": ok(lambda: CloudConfig(bogus=1)),
    "unknown_turntable": ok(lambda: TurntableScanConfig(angel=18.0)),
    "unknown_z_axis": ok(lambda: ZAxisScanConfig(distnce_mm=10.0)),
    "known_keys_ok": ok(lambda: StartRequest(
        job_id="20261030-101500-a1b2c3", mechanical={"turntable": {"angle": 18.0}, "z_axis": {"distance_mm": 50.0}},
        cloud={"launch_runpod": True, "resolution_level": 2, "title": "vase"})),
    "job_id_library_format": ok(lambda: StartRequest(job_id="20261030-101500-a1b2c3")),
    "job_id_other_format": ok(lambda: StartRequest(job_id="job-1")),
    "job_id_default": StartRequest().job_id,
    "meshroom_field_refused": ok(lambda: CloudConfig(resolution_mp=12.0)),

    "legacy_clockwise": ok(lambda: TurntableScanConfig(clockwise=True)),
    "legacy_direction": ok(lambda: ZAxisScanConfig(direction="up")),
    "legacy_nested": legacy.mechanical.turntable.angle == 18.0 and legacy.mechanical.z_axis.distance_mm == 50.0,
    "legacy_not_stored": not hasattr(legacy.mechanical.turntable, "clockwise")
                         and not hasattr(legacy.mechanical.z_axis, "direction"),

    "test_rotate_unchanged": ok(lambda: TurntableRotateRequest(angle=18.0, clockwise=False)),
    "test_rotate_not_forbidden": ok(lambda: TurntableRotateRequest(angle=18.0, whatever=1)),
    "test_z_unchanged": ok(lambda: ZMoveRequest(distance_mm=10.0, direction="down")),
    "camera_extra_keys_ok": ok(lambda: DualCameraConfig(some_frontend_key=1)),

    "level_0": ok(lambda: CloudConfig(resolution_level=0)),
    "level_3": ok(lambda: CloudConfig(resolution_level=3)),
    "level_4": ok(lambda: CloudConfig(resolution_level=4)),
    "level_default": CloudConfig().resolution_level,

    "brightness_0": ok(lambda: PrepareRequest(illumination_brightness=0.0, camera_config={})),
    "brightness_small": ok(lambda: PrepareRequest(illumination_brightness=0.05, camera_config={})),
    "brightness_1": ok(lambda: PrepareRequest(illumination_brightness=1.0, camera_config={})),
    "brightness_over": ok(lambda: PrepareRequest(illumination_brightness=1.5, camera_config={})),

    "status": status.model_dump(),
    "status_full": StatusResponse(state="error", phase="verifying", phase_message="m", level=3,
                                  photos_captured=40, photos_uploaded=38, missing=["rig/a.jpg"],
                                  error="boom", tof_warnings=["w"]).model_dump(),
}))
""" % ROOT


class TestRequestSchemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_launch_runpod_defaults_to_true_and_accepts_both_values(self):
        self.assertIs(self.r["launch_default"], True)
        self.assertIs(self.r["start_default_launch"], True)
        self.assertIs(self.r["launch_true"], True)
        self.assertIs(self.r["launch_false"], False)

    def test_unknown_keys_are_rejected(self):
        for key in ("unknown_mechanical_angle", "unknown_mechanical_z_move", "unknown_top_level", "unknown_cloud",
                    "unknown_turntable", "unknown_z_axis"):
            self.assertFalse(self.r[key], key)
        self.assertTrue(self.r["known_keys_ok"])

    def test_legacy_keys_are_tolerated_and_dropped(self):
        self.assertTrue(self.r["legacy_clockwise"])
        self.assertTrue(self.r["legacy_direction"])
        self.assertTrue(self.r["legacy_nested"])
        self.assertTrue(self.r["legacy_not_stored"])

    def test_manual_test_requests_and_camera_config_keep_their_behaviour(self):
        self.assertTrue(self.r["test_rotate_unchanged"])
        self.assertTrue(self.r["test_rotate_not_forbidden"])
        self.assertTrue(self.r["test_z_unchanged"])
        self.assertTrue(self.r["camera_extra_keys_ok"])

    def test_cloud_resolution_level_and_job_id(self):
        self.assertTrue(self.r["level_0"])
        self.assertTrue(self.r["level_3"])
        self.assertFalse(self.r["level_4"])
        self.assertEqual(self.r["level_default"], 1)
        self.assertFalse(self.r["meshroom_field_refused"])
        self.assertTrue(self.r["job_id_library_format"])
        self.assertFalse(self.r["job_id_other_format"])
        self.assertIsNone(self.r["job_id_default"])

    def test_zero_brightness_is_rejected(self):
        self.assertFalse(self.r["brightness_0"])
        self.assertTrue(self.r["brightness_small"])
        self.assertTrue(self.r["brightness_1"])
        self.assertFalse(self.r["brightness_over"])

    def test_status_response_defaults_and_fields(self):
        self.assertEqual(self.r["status"], {
            "state": "idle", "mode": None, "total_photos": 0, "stream_urls": None, "phase": None, "phase_message": None,
            "cloud_message": None, "level": 0, "photos_captured": 0, "photos_uploaded": 0, "missing": [], "error": None,
            "tof_warnings": [], "face_result": None, "job_id": None, "result_id": None, "seconds_since_last_upload": None,
            "upload_drain_timeout_s": None,
        })
        full = self.r["status_full"]
        self.assertEqual((full["phase"], full["level"], full["photos_uploaded"], full["missing"], full["error"]),
                         ("verifying", 3, 38, ["rig/a.jpg"], "boom"))


# ----------------------------------------------------------------------------------------------------------
# Router (fake modules)
# ----------------------------------------------------------------------------------------------------------
class _HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _APIRouter:
    def __init__(self, *args, **kwargs):
        pass

    def _decorator(self, *args, **kwargs):
        return lambda fn: fn

    get = post = put = delete = _decorator


class _State:
    IDLE = "idle"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PREPARE_ALLOWED = ("idle", "completed", "cancelled", "error")


class _ScannerError(Exception):
    pass


class _ScannerUploadError(_ScannerError):
    def __init__(self, message, missing=()):
        super().__init__(message)
        self.missing = list(missing)


class _Kwargs:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeLights:
    def __init__(self):
        self.calls = []

    def set_brightness(self, value):
        self.calls.append(value)


class _FakeScanner:
    """Stands in for hardware.scanner.Scanner; `on_scan` is run inside scan() to script a scenario."""

    def __init__(self):
        self.state = _State.PREPARED
        self.job_id = "20261030-101500-a1b2c3"
        self.images_dir = "/tmp/input_images/rig"
        self.phase_message = None
        self.error = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._stopping = False
        self.total_photos = 40
        self.lights = _FakeLights()
        self.scan_calls = []
        self.setup_calls = []
        self.on_scan = None
        self.setup_error = None

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        self.state = _State.PROCESSING  # what scan(finish_state=PROCESSING) leaves behind
        if self.on_scan:
            self.on_scan(self)

    def setup_and_stream(self, **kwargs):
        self.setup_calls.append(kwargs)
        if self.setup_error:
            raise self.setup_error
        return ["rtsp://x/cam0"]

    def finish_preparation(self, **kwargs):
        pass

    def cancel(self):
        """What emergency_stop() does: publish CANCELLED and raise the cancel flag."""
        with self._lock:
            self.state = _State.CANCELLED
            self._cancel_event.set()


_CLOUD_ENV = {name: "x" for name in ("RUNPOD_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_R2_API_TOKEN",
                                       "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                                       "TURNTABLE_IMAGE")}


def _fake_cloud_job(run=None):
    """Stands in for cloud.job: run_cloud_job records its arguments; `run(kwargs)` scripts the report."""
    mod = types.ModuleType("cloud.job")
    mod.DEFAULT_IMAGE_ENV = "TURNTABLE_IMAGE"
    mod.calls = []

    def run_cloud_job(**kwargs):
        mod.calls.append(kwargs)
        return run(kwargs) if run else {"result": "ok", "results_dir": "results/x", "cost_estimate": 0.1}

    mod.run_cloud_job = run_cloud_job
    return mod


def _load_router(scanner, r2=None):
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _APIRouter
    fastapi.HTTPException = _HTTPException
    fastapi.BackgroundTasks = object
    fastapi.Query = lambda *args, **kwargs: None
    responses = types.ModuleType("fastapi.responses")
    responses.StreamingResponse = object
    fastapi.responses = responses
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type("BaseModel", (), {})
    pydantic.Field = lambda *args, **kwargs: None

    fake_mock_mod = types.ModuleType("hardware.scanner_mock")
    fake_mock_mod.ScannerMock = lambda **kwargs: scanner
    fake_mock_mod.ScannerState = _State
    fake_mock_mod.ScannerUploadError = _ScannerUploadError

    fake_schemas = types.ModuleType("schemas.scanner")
    for name in ("PrepareRequest", "PrepareResponse", "StartRequest", "StartResponse", "CancelResponse",
                 "StatusResponse", "StreamActiveRequest", "StreamQualityRequest"):
        setattr(fake_schemas, name, type(name, (_Kwargs,), {}))

    router_pkg = types.ModuleType("router")
    router_pkg.__path__ = []
    r2 = r2 or MagicMock()
    cloud_pkg = MagicMock()
    cloud_pkg.cloudflare_r2 = r2  # `import cloud.cloudflare_r2 as r2` reads the attribute of the package

    modules = {
        "fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic, "router": router_pkg,
        "hardware.scanner_mock": fake_mock_mod, "schemas.scanner": fake_schemas,
        "cloud": cloud_pkg, "cloud.cloudflare_r2": r2,
    }
    with patch.dict(os.environ, {"MOCK_HARDWARE": "1"}), patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("router_scanner_fs_api", os.path.join(ROOT, "router/scanner.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _start_req(launch_runpod=False):
    turntable = SimpleNamespace(angle=18.0, delay=0.001, acceleration=True, start_delay=None, ramp_percent=0.2,
                                decel_percent=None, end_delay=0.015)
    z_axis = SimpleNamespace(distance_mm=100.0, delay=0.001, acceleration=False, start_delay=None, ramp_percent=0.2,
                             decel_percent=None, end_delay=None)
    cloud = SimpleNamespace(launch_runpod=launch_runpod, resolution_level=1, title=None)
    return SimpleNamespace(job_id=None, mechanical=SimpleNamespace(turntable=turntable, z_axis=z_axis),
                           cloud=cloud)


class _RouterCase(unittest.TestCase):
    def setUp(self):
        self.scanner = _FakeScanner()
        self.r2 = MagicMock()
        self.mod = _load_router(self.scanner, self.r2)

    def run_pipeline(self, req, cloud_job=None):
        """Runs the orchestrator; `cloud_job=None` makes `import cloud.job` fail like a missing package would."""
        with patch.dict(sys.modules, {"cloud.job": cloud_job}):
            self.mod._orchestrate_pipeline(req)


class TestCloudRetry(_RouterCase):
    """UX-3: POST /scanner/cloud/retry runs only the cloud stage again, same job and options, after a failure."""

    def setUp(self):
        super().setUp()
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        rig = os.path.join(self._tmp.name, "rig", "0")
        os.makedirs(rig)
        open(os.path.join(rig, "f_0.jpg"), "wb").write(b"jpg")
        self.scanner.images_dir = os.path.join(self._tmp.name, "rig")
        self.mod._cloud_retry.clear()
        p = patch.object(self.mod, "_runpod_config_problem", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def fail_then(self, *reports):
        it = iter(reports)
        return _fake_cloud_job(run=lambda kw: next(it))

    def retry(self, job):
        tasks = MagicMock()
        with patch.dict(sys.modules, {"cloud.job": job}):
            self.mod.retry_cloud_reconstruction(tasks)
            tasks.add_task.call_args.args[0]()

    def test_failed_job_can_be_retried_with_the_same_job_and_options(self):
        job = self.fail_then({"result": "failed", "status": {"state": "pipeline_error"}}, {"result": "ok"})
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        self.assertEqual(self.scanner.state, _State.ERROR)
        self.retry(job)
        self.assertEqual(self.scanner.state, _State.COMPLETED)
        self.assertEqual(len(job.calls), 2)
        self.assertEqual(job.calls[0]["job_id"], job.calls[1]["job_id"])
        self.assertEqual(job.calls[0]["pipeline_options"], job.calls[1]["pipeline_options"])
        self.assertIs(job.calls[1]["skip_upload"], True)
        self.assertEqual(self.mod._cloud_retry, {})          # nothing left to retry
        with self.assertRaises(_HTTPException) as ctx:
            self.retry(job)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_refused_without_a_failure_after_a_new_scan_or_without_photos(self):
        job = self.fail_then({"result": "failed"})
        with self.assertRaises(_HTTPException) as ctx:
            self.retry(job)
        self.assertEqual(ctx.exception.status_code, 400)
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        self.scanner.job_id = "20261031-000000-ffffff"     # a newer photo set
        with self.assertRaises(_HTTPException) as ctx:
            self.retry(job)
        self.assertIn("newer scan", ctx.exception.detail)
        self.scanner.job_id = "20261030-101500-a1b2c3"
        os.remove(os.path.join(self.scanner.images_dir, "0", "f_0.jpg"))
        with self.assertRaises(_HTTPException) as ctx:
            self.retry(job)
        self.assertIn("no longer on the Pi", ctx.exception.detail)

    def test_busy_is_409_and_a_second_failure_stays_retryable(self):
        job = self.fail_then({"result": "failed"}, {"result": "failed"})
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        self.mod._start_state["pending"] = True
        with self.assertRaises(_HTTPException) as ctx:
            self.retry(job)
        self.assertEqual(ctx.exception.status_code, 409)
        self.mod._start_state["pending"] = False
        self.retry(job)
        self.assertEqual(self.scanner.state, _State.ERROR)
        self.assertTrue(self.mod._cloud_retry)
        self.assertFalse(self.mod._start_state["pending"])


class TestNullPairs(unittest.TestCase):
    def test_all_null_pairs_mean_unset_and_half_filled_pairs_are_rejected(self):
        code = (
            "import json\n"
            "from schemas.scanner import DualCameraConfig as C\n"
            "out = {}\n"
            "def ok(**kw):\n"
            "    try:\n"
            "        return C(**kw)\n"
            "    except Exception:\n"
            "        return None\n"
            "a = ok(colour_gains=[None, None], focus=[None, None])\n"
            "out['null_pairs_ok'] = a is not None and a.colour_gains is None and a.focus is None\n"
            "out['half_filled_rejected'] = ok(colour_gains=[1.5, None]) is None\n"
            "b = ok(colour_gains=[1.5, 2.0])\n"
            "out['numbers_kept'] = b is not None and tuple(b.colour_gains) == (1.5, 2.0)\n"
            "print(json.dumps(out))\n"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-1500:])
        r = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(r["null_pairs_ok"])
        self.assertTrue(r["half_filled_rejected"])
        self.assertTrue(r["numbers_kept"])


class TestCameraConfigContract(unittest.TestCase):
    """DualCameraConfig only carries settings the dual rpi.sync driver really uses."""

    @classmethod
    def setUpClass(cls):
        code = (
            "import json\n"
            "from schemas.scanner import DualCameraConfig as C, PrepareRequest as P\n"
            "def ok(**kw):\n"
            "    try:\n"
            "        C(**kw); return True\n"
            "    except Exception:\n"
            "        return False\n"
            "out = {\n"
            "  'defaults': ok(),\n"
            "  'exposure_zero': ok(exposure_time=0),\n"
            "  'exposure_ok': ok(exposure_time=8000),\n"
            "  'gain_zero': ok(analogue_gain=0),\n"
            "  'gain_ok': ok(analogue_gain=1.5),\n"
            "  'gains_zero': ok(colour_gains=[0, 0]),\n"
            "  'gains_ok': ok(colour_gains=[1.8, 1.4]),\n"
            "  'contrast_zero': ok(contrast=0),\n"
            "  'saturation_zero': ok(saturation=0),\n"
            "  'removed_keys_tolerated': ok(show_preview=True, keep_running=True, autofocus_step=10,\n"
            "      autofocus_roi=[0, 0, 1, 1], video_resolution=[1920, 1080]),\n"
            "  'focus_auto': ok(focus='auto'),\n"
            "  'focus_word': ok(focus='near'),\n"
            "  'preview_ok': ok(preview_resolution=[640, 480]),\n"
            "  'preview_odd': ok(preview_resolution=[641, 480]),\n"
            "  'preview_tiny': ok(preview_resolution=[80, 60]),\n"
            "  'preview_bigger_than_photo': ok(photo_resolution=[1920, 1080], preview_resolution=[2000, 1080]),\n"
            "  'fields': sorted(C.model_fields),\n"
            "}\n"
            "p = P(camera_config={})\n"
            "out['prepare_fields'] = sorted(P.model_fields)\n"
            "out['prepare_ignores_bitrate'] = P(bitrate=1, camera_config={}).enable_stream is True\n"
            "print(json.dumps(out))\n"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_zero_manual_exposure_and_gain_are_rejected(self):
        self.assertFalse(self.r["exposure_zero"])
        self.assertFalse(self.r["gain_zero"])
        self.assertFalse(self.r["gains_zero"])
        self.assertFalse(self.r["contrast_zero"])

    def test_valid_manual_values_and_neutral_zero_values_are_accepted(self):
        for key in ("defaults", "exposure_ok", "gain_ok", "gains_ok", "saturation_zero"):
            self.assertTrue(self.r[key], key)

    def test_removed_keys_are_tolerated_but_not_part_of_the_schema(self):
        self.assertTrue(self.r["removed_keys_tolerated"])
        for key in ("show_preview", "keep_running", "autofocus_step", "autofocus_roi", "video_resolution"):
            self.assertNotIn(key, self.r["fields"])

    def test_focus_auto_is_accepted_other_words_are_not(self):
        self.assertTrue(self.r["focus_auto"])
        self.assertFalse(self.r["focus_word"])

    def test_preview_resolution_is_validated(self):
        self.assertIn("preview_resolution", self.r["fields"])
        self.assertTrue(self.r["preview_ok"])
        self.assertFalse(self.r["preview_odd"])
        self.assertFalse(self.r["preview_tiny"])
        self.assertFalse(self.r["preview_bigger_than_photo"])

    def test_prepare_request_has_no_bitrate(self):
        self.assertNotIn("bitrate", self.r["prepare_fields"])
        self.assertTrue(self.r["prepare_ignores_bitrate"])


class TestFocusValidation(unittest.TestCase):
    def test_focus_auto_and_numbers_are_accepted_other_words_are_rejected(self):
        code = (
            "import json\n"
            "from schemas.scanner import DualCameraConfig as C\n"
            "out = {}\n"
            "for name, v in (('auto', 'auto'), ('word', 'near'), ('int', 500), ('pair', [100, 200]), ('none', None)):\n"
            "    try:\n"
            "        C(focus=v); out[name] = True\n"
            "    except Exception:\n"
            "        out[name] = False\n"
            "print(json.dumps(out))\n"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-1500:])
        r = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(r["auto"])
        self.assertFalse(r["word"])
        self.assertTrue(r["int"] and r["pair"] and r["none"])


class TestPipelineWithoutRunPod(_RouterCase):
    def test_flag_false_completes_without_touching_runpod(self):
        self.run_pipeline(_start_req(launch_runpod=False), cloud_job=None)  # importing cloud.job would raise
        self.assertEqual(self.scanner.state, _State.COMPLETED)
        self.assertEqual(len(self.scanner.scan_calls), 1)
        self.assertFalse(self.mod._start_state["pending"])

    def test_flag_false_ignores_a_broken_runpod_module(self):
        broken = types.ModuleType("cloud.job")   # no run_cloud_job at all
        self.run_pipeline(_start_req(launch_runpod=False), cloud_job=broken)
        self.assertEqual(self.scanner.state, _State.COMPLETED)

    def test_cancel_during_the_scan_stays_cancelled(self):
        self.scanner.on_scan = lambda s: s.cancel()
        self.run_pipeline(_start_req(launch_runpod=False))
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_cancel_flag_without_published_state_is_cancelled(self):
        self.scanner.on_scan = lambda s: (setattr(s, "state", _State.IDLE), s._cancel_event.set())
        self.run_pipeline(_start_req(launch_runpod=False))
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_scan_that_ended_in_error_is_not_completed(self):
        self.scanner.on_scan = lambda s: setattr(s, "state", _State.ERROR)
        self.run_pipeline(_start_req(launch_runpod=False))
        self.assertEqual(self.scanner.state, _State.ERROR)


class TestUploadVerificationFailure(_RouterCase):
    def _raise(self, error):
        def hook(scanner):
            raise error
        self.scanner.on_scan = hook

    def test_upload_error_ends_in_error(self):
        self._raise(_ScannerUploadError("2 photos missing", missing=["rig/a.jpg", "rig/b.jpg"]))
        with self.assertLogs(self.mod.log, level="ERROR") as logs:
            self.run_pipeline(_start_req(launch_runpod=True), cloud_job=_fake_cloud_job())
        self.assertEqual(self.scanner.state, _State.ERROR)
        self.assertIn("rig/a.jpg", "\n".join(logs.output))
        self.assertFalse(self.mod._start_state["pending"])

    def test_only_the_first_20_missing_keys_are_logged(self):
        missing = [f"rig/{i:03d}.jpg" for i in range(30)]
        self._raise(_ScannerUploadError("30 missing", missing=missing))
        with self.assertLogs(self.mod.log, level="ERROR") as logs:
            self.run_pipeline(_start_req())
        text = "\n".join(logs.output)
        self.assertIn("rig/019.jpg", text)
        self.assertNotIn("rig/020.jpg", text)
        self.assertIn("30 photo(s) missing", text)

    def test_upload_error_is_matched_by_name_when_the_class_is_not_importable(self):
        foreign = type("ScannerUploadError", (Exception,), {"missing": ["rig/x.jpg"]})("missing")
        self._raise(foreign)
        self.run_pipeline(_start_req())
        self.assertEqual(self.scanner.state, _State.ERROR)

    def test_upload_error_after_cancel_keeps_cancelled(self):
        def hook(scanner):
            scanner.cancel()
            raise _ScannerUploadError("missing", missing=["rig/a.jpg"])
        self.scanner.on_scan = hook
        self.run_pipeline(_start_req())
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_other_scanner_errors_end_in_error(self):
        self._raise(_ScannerError("The Z-axis climb was aborted"))
        self.run_pipeline(_start_req())
        self.assertEqual(self.scanner.state, _State.ERROR)

    def test_other_scanner_errors_after_cancel_keep_cancelled(self):
        def hook(scanner):
            scanner.cancel()
            raise _ScannerError("aborted")
        self.scanner.on_scan = hook
        self.run_pipeline(_start_req())
        self.assertEqual(self.scanner.state, _State.CANCELLED)


class TestPipelineWithRunPod(_RouterCase):
    def test_flag_true_runs_turntable_studio_on_the_uploaded_photos(self):
        seen = []
        job = _fake_cloud_job(run=lambda kw: (seen.append(self.scanner.state), {"result": "ok"})[1])
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        kwargs = job.calls[0]
        self.assertEqual(seen, [_State.PROCESSING])
        self.assertEqual(kwargs["job_id"], "20261030-101500-a1b2c3")
        self.assertIs(kwargs["skip_upload"], True)       # the scan already uploaded and verified them
        self.assertIs(kwargs["dry_run"], False)          # no confirmation
        self.assertEqual((kwargs["input_dir"], kwargs["image_prefix"]), ("/tmp/input_images", "rig/"))
        self.assertIs(kwargs["cancel_event"], self.scanner._cancel_event)
        self.assertEqual(kwargs["pipeline_options"]["resolution_level"], 1)
        self.assertEqual(kwargs["pipeline_options"]["lift_mm"], 100.0)
        self.assertEqual(kwargs["pipeline_options"]["step_deg"], 18.0)
        self.assertIs(kwargs["pipeline_options"]["rig_mode"], True)
        self.assertEqual(self.scanner.state, _State.COMPLETED)
        self.assertEqual(self.scanner.result_id, "20261030-101500-a1b2c3")

    def test_pod_messages_go_to_cloud_message_and_only_milestones_to_phase_message(self):
        seen = []

        def run(kw):
            kw["on_progress"]("Pod RUNNING | 2.5 min | ...")
            seen.append((self.scanner.phase_message, self.scanner.cloud_message))
            kw["on_phase"]("3D reconstruction: structure from motion (SfM) done")
            kw["on_progress"]("Pod RUNNING | 3.0 min | ...")
            seen.append((self.scanner.phase_message, self.scanner.cloud_message))
            return {"result": "ok"}

        job = _fake_cloud_job(run=run)
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        self.assertEqual(seen, [
            ("Preparing the 3D reconstruction in the cloud", "Pod RUNNING | 2.5 min | ..."),
            ("3D reconstruction: structure from motion (SfM) done", "Pod RUNNING | 3.0 min | ..."),
        ])
        self.assertEqual(self.scanner.state, _State.COMPLETED)

    def test_flag_false_never_touches_the_cloud_job(self):
        job = _fake_cloud_job()
        self.run_pipeline(_start_req(launch_runpod=False), cloud_job=job)
        self.assertEqual(job.calls, [])
        self.assertEqual(self.scanner.state, _State.COMPLETED)

    def test_failed_job_ends_in_error_with_the_reason(self):
        job = _fake_cloud_job(run=lambda kw: {"result": "failed", "status": {"state": "pipeline_error",
                                                                              "error": "no poses"}})
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=job)
        self.assertEqual(self.scanner.state, _State.ERROR)
        self.assertIn("pipeline_error", self.scanner.error)

    def test_cancel_during_the_job_stays_cancelled(self):
        def run(kw):
            self.scanner.cancel()
            return {"result": "failed", "status": {}}
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=_fake_cloud_job(run=run))
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_error_after_cancel_is_not_written(self):
        def run(kw):
            self.scanner.cancel()
            raise RuntimeError("pod vanished")
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=_fake_cloud_job(run=run))
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_cloud_package_missing_with_flag_true_ends_in_error(self):
        self.run_pipeline(_start_req(launch_runpod=True), cloud_job=None)
        self.assertEqual(self.scanner.state, _State.ERROR)


class TestCompareAndSet(_RouterCase):
    def test_cancelled_state_is_never_overwritten(self):
        self.scanner.state = _State.CANCELLED
        for target in (_State.COMPLETED, _State.ERROR, _State.PROCESSING, _State.DOWNLOADING):
            self.assertFalse(self.mod._set_state_unless_cancelled(target))
            self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_state_is_written_when_not_cancelled(self):
        self.assertTrue(self.mod._set_state_unless_cancelled(_State.COMPLETED))
        self.assertEqual(self.scanner.state, _State.COMPLETED)

    def test_cancel_flag_forces_cancelled(self):
        self.scanner._cancel_event.set()
        self.assertFalse(self.mod._set_state_unless_cancelled(_State.COMPLETED))
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_write_is_done_under_the_scanner_lock(self):
        held = []

        class Probe:
            def __init__(inner):
                inner.lock = threading.Lock()

            def __enter__(inner):
                inner.lock.acquire()
                held.append(True)
                return inner

            def __exit__(inner, *exc):
                inner.lock.release()

        self.scanner._lock = Probe()
        self.mod._set_state_unless_cancelled(_State.COMPLETED)
        self.assertEqual(held, [True])

    def test_works_when_the_scanner_has_no_lock(self):
        del self.scanner._lock
        self.assertTrue(self.mod._set_state_unless_cancelled(_State.COMPLETED))


class TestStatusFields(_RouterCase):
    def test_fields_come_from_the_scanner(self):
        s = self.scanner
        s.state = _State.RUNNING
        s.phase, s.phase_message = "uploading", "38/40 uploaded"
        s.photos_captured, s.photos_uploaded, s.level = 40, 38, 2
        s.missing = ["rig/a.jpg"]
        s.error = None
        s.tof_warnings = ["invalid reading at level 1"]
        s.stream_urls = ["rtsp://x/cam0"]
        out = self.mod.get_status()
        self.assertEqual(out.state, "running")
        self.assertEqual(out.total_photos, 40)
        self.assertEqual((out.phase, out.phase_message), ("uploading", "38/40 uploaded"))
        self.assertEqual((out.photos_captured, out.photos_uploaded, out.level), (40, 38, 2))
        self.assertEqual(out.missing, ["rig/a.jpg"])
        self.assertIsNone(out.error)
        self.assertEqual(out.tof_warnings, ["invalid reading at level 1"])
        self.assertEqual(out.stream_urls, ["rtsp://x/cam0"])

    def test_error_state_carries_the_reason(self):
        self.scanner.state = _State.ERROR
        self.scanner.error = "2 photos missing in the bucket"
        self.scanner.missing = ["rig/a.jpg", "rig/b.jpg"]
        out = self.mod.get_status()
        self.assertEqual(out.error, "2 photos missing in the bucket")
        self.assertEqual(out.missing, ["rig/a.jpg", "rig/b.jpg"])

    def test_missing_list_is_capped(self):
        self.scanner.missing = [f"rig/{i}.jpg" for i in range(500)]
        self.assertEqual(len(self.mod.get_status().missing), 50)

    def test_bare_object_gets_safe_defaults(self):
        bare = SimpleNamespace(state="idle")
        mod = _load_router(bare)
        out = mod.get_status()
        self.assertEqual(out.state, "idle")
        self.assertEqual(out.total_photos, 0)
        self.assertIsNone(out.phase)
        self.assertIsNone(out.phase_message)
        self.assertIsNone(out.error)
        self.assertEqual((out.level, out.photos_captured, out.photos_uploaded), (0, 0, 0))
        self.assertEqual((out.missing, out.tof_warnings), ([], []))

    def test_unexpected_value_types_fall_back_to_defaults(self):
        weird = SimpleNamespace(state="idle", phase=MagicMock(), photos_captured=MagicMock(), missing=MagicMock(),
                                level="x", error=5)
        out = _load_router(weird).get_status()
        self.assertIsNone(out.phase)
        self.assertEqual((out.photos_captured, out.level, out.missing, out.error), (0, 0, [], None))


class TestPrepare(_RouterCase):
    def _req(self, brightness=0.5, z_start_mm=30.0):
        camera = SimpleNamespace(model_dump=lambda: {"quality": 95})
        return SimpleNamespace(enable_stream=True, bitrate=2_000_000, illumination_brightness=brightness,
                               z_start_mm=z_start_mm, z_delay=0.001, camera_config=camera)

    def test_refused_state_does_not_touch_the_brightness(self):
        for state in (_State.RUNNING, _State.PREPARED, _State.PREPARING, _State.PROCESSING):
            self.scanner.state = state
            with self.assertRaises(_HTTPException) as ctx:
                self.mod.prepare_scan(self._req(0.2), MagicMock())
            self.assertEqual(ctx.exception.status_code, 400, state)
        self.assertEqual(self.scanner.lights.calls, [])
        self.assertEqual(self.scanner.setup_calls, [])

    def test_emergency_stop_in_progress_is_409_without_touching_the_brightness(self):
        self.scanner.state = _State.CANCELLED
        self.scanner._stopping = True
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.scanner.lights.calls, [])

    def test_allowed_state_sets_the_brightness_then_prepares(self):
        self.scanner.state = _State.IDLE
        tasks = MagicMock()
        out = self.mod.prepare_scan(self._req(0.7), tasks)
        self.assertEqual(self.scanner.lights.calls, [0.7])
        self.assertEqual(len(self.scanner.setup_calls), 1)
        tasks.add_task.assert_called_once()
        self.assertEqual(out.status, "accepted")

    def test_z_start_goes_to_finish_preparation_beside_the_camera_settings(self):
        self.scanner.state = _State.IDLE
        tasks = MagicMock()
        self.mod.prepare_scan(self._req(z_start_mm=12.5), tasks)
        worker_kwargs = tasks.add_task.call_args.args[1]
        self.assertEqual(worker_kwargs, {"quality": 95, "z_start_mm": 12.5, "z_delay": 0.001})
        # the camera settings given to setup_and_stream() do not carry it
        self.assertNotIn("z_start_mm", self.scanner.setup_calls[0])

    def test_conflict_error_from_the_scanner_is_409_not_500(self):
        self.scanner.state = _State.IDLE
        busy = type("ScannerBusyError", (Exception,), {"conflict": True})("Another motion is in progress.")
        self.scanner.setup_error = busy
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_preflight_refusal_maps_to_400_or_409(self):
        self.scanner.state = _State.IDLE
        for conflict, expected in ((False, 400), (True, 409)):
            error = type("ScannerPreflightError", (RuntimeError,), {"conflict": conflict})("R2 is not reachable")
            self.scanner.setup_error = error
            with self.assertRaises(_HTTPException) as ctx:
                self.mod.prepare_scan(self._req(), MagicMock())
            self.assertEqual(ctx.exception.status_code, expected)

    def test_hardware_failure_is_still_500(self):
        self.scanner.state = _State.IDLE
        self.scanner.setup_error = OSError("i2c")
        with self.assertRaises(_HTTPException) as ctx:
            self.mod.prepare_scan(self._req(), MagicMock())
        self.assertEqual(ctx.exception.status_code, 500)


class TestRunPodPreflight(_RouterCase):
    def start(self, launch_runpod, cloud_job, env=None):
        tasks = MagicMock()
        with patch.dict(sys.modules, {"cloud.job": cloud_job}), patch.dict(os.environ, env or {}, clear=False):
            result = self.mod.start_scan(_start_req(launch_runpod), tasks)
        return result, tasks

    def test_each_missing_variable_with_flag_true_is_400_and_nothing_is_queued(self):
        for name in _CLOUD_ENV:
            env = dict(_CLOUD_ENV, **{name: "  "})
            tasks = MagicMock()
            with self.subTest(name=name), patch.dict(sys.modules, {"cloud.job": _fake_cloud_job()}), \
                    patch.dict(os.environ, env):
                with self.assertRaises(_HTTPException) as ctx:
                    self.mod.start_scan(_start_req(True), tasks)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn(name, ctx.exception.detail)
                tasks.add_task.assert_not_called()
                self.assertFalse(self.mod._start_state["pending"])

    def test_cloud_package_import_failure_is_400(self):
        with patch.dict(sys.modules, {"cloud.job": None}), patch.dict(os.environ, _CLOUD_ENV):
            with self.assertRaises(_HTTPException) as ctx:
                self.mod.start_scan(_start_req(True), MagicMock())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_flag_false_needs_no_cloud_configuration(self):
        result, tasks = self.start(False, cloud_job=None)
        self.assertEqual(result.status, "accepted")
        tasks.add_task.assert_called_once()

    def test_flag_true_with_valid_configuration_is_accepted(self):
        result, tasks = self.start(True, _fake_cloud_job(), env=_CLOUD_ENV)
        self.assertEqual(result.status, "accepted")
        tasks.add_task.assert_called_once()

    def test_wrong_state_is_still_400(self):
        self.scanner.state = _State.IDLE
        with self.assertRaises(_HTTPException) as ctx:
            self.start(False, cloud_job=None)
        self.assertEqual(ctx.exception.status_code, 400)


class TestRequirements(unittest.TestCase):
    def test_runtime_packages_are_listed(self):
        with open(os.path.join(ROOT, "requirements.txt")) as f:
            names = {line.strip().split("=")[0].split(">")[0].split("<")[0].lower()
                     for line in f if line.strip() and not line.startswith("#")}
        self.assertIn("rpi-hardware-pwm", names)
        self.assertIn("adafruit-circuitpython-vl53l1x", names)
        self.assertNotIn("adafruit-circuitpython-vl53l0x", names)


if __name__ == "__main__":
    unittest.main()
