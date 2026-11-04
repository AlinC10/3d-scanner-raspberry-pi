"""
test_stream_pod_router.py — The pod started together with the object scan (cloud.start_pod_with_scan, streaming
Turntable Studio v10+), in router/scanner.py::_orchestrate_pipeline, with a fake cloud.job and a fake scanner:
- the cloud stage starts BEFORE the photos and only logs while the scan runs;
- capture_done.json gets exactly the photos the scan verified, then the state goes PROCESSING and the result is used;
- a failed or cancelled scan writes cancel.json and stops the pod;
- a streaming stage that failed falls back to the usual cloud stage after the scan;
- a flip scan, launch_runpod false or the option off: the flow of before.
"""

import os
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from tests.test_medium_fixes import _HTTPException, _Response, _State, _load, _router_package, _web_stubs  # noqa: F401


def _req(stream=True, flip=False, launch=True, job_id=None):
    return types.SimpleNamespace(
        job_id=job_id,
        mechanical=types.SimpleNamespace(
            turntable=types.SimpleNamespace(angle=18.0, delay=0.001, acceleration=True, start_delay=None,
                                            ramp_percent=0.2, decel_percent=None, end_delay=None),
            z_axis=types.SimpleNamespace(distance_mm=80.0, delay=0.001, acceleration=False, start_delay=None,
                                         ramp_percent=0.2, decel_percent=None, end_delay=None),
            flip=flip),
        cloud=types.SimpleNamespace(launch_runpod=launch, resolution_level=1, title=None, start_pod_with_scan=stream),
    )


class FakeCloudJob(types.ModuleType):
    """cloud.job: run_cloud_job blocks (like the pod) until capture_done.json or a cancel."""

    def __init__(self, fail_stream=False, result="ok"):
        super().__init__("cloud.job")
        self.calls, self.events = [], []
        self.capture = threading.Event()
        self.fail_stream, self.result = fail_stream, result
        self.r2 = types.SimpleNamespace(new_job_id=lambda: "20261102-150000-abcdef")

    @staticmethod
    def stream_n_images(step_deg, max_levels, cameras=2):
        return int(cameras * round(360 / step_deg) * max_levels)

    class AnyEvent:
        def __init__(self, *events):
            self.events = [e for e in events if e is not None]

        def is_set(self):
            return any(e.is_set() for e in self.events)

    def run_cloud_job(self, **kw):
        self.calls.append(kw)
        self.events.append("run_cloud_job" + (" stream" if kw.get("stream") else ""))
        if kw.get("stream"):
            if self.fail_stream:
                raise RuntimeError("RunPod refused the pod")
            while not self.capture.is_set():
                if kw["cancel_event"].is_set():
                    return {"result": "failed", "status": {"state": "cancelled"}}
                self.capture.wait(0.01)
        return {"result": self.result, "status": {"state": "ok" if self.result == "ok" else "pipeline_error"},
                "results_dir": "/tmp/x"}

    def write_capture_done(self, job_id, keys, step_deg=None, bucket=None, client=None):
        self.events.append(("capture_done", job_id, list(keys)))
        self.capture.set()

    def write_cancel(self, job_id, bucket=None, client=None):
        self.events.append(("cancel", job_id))
        return True


class TestStreamPodRouter(unittest.TestCase):
    def setUp(self):
        self.scanner = MagicMock()
        self.scanner.state = _State.PREPARED
        self.scanner._cancel_event = threading.Event()
        self.scanner.images_dir = "/tmp/input_images/rig"
        self.scanner.uploaded_keys = ["jobs/J/input/rig/0/f_1000.jpg", "jobs/J/input/rig/1/f_1000.jpg"]
        fake_mock_mod = types.ModuleType("hardware.scanner_mock")
        fake_mock_mod.ScannerMock = lambda **kwargs: self.scanner
        fake_mock_mod.ScannerState = _State
        fake_schemas = types.ModuleType("schemas.scanner")
        for name in ("PrepareRequest", "PrepareResponse", "StartResponse", "CancelResponse", "StatusResponse",
                     "StreamActiveRequest", "StreamQualityRequest", "StartRequest"):
            setattr(fake_schemas, name, type(name, (_Response,), {}))
        with patch.dict(os.environ, {"MOCK_HARDWARE": "1"}):
            self.mod = _load("router/scanner.py", "router_scanner_stream_test", {
                **_web_stubs(), "router": _router_package(), "hardware.scanner_mock": fake_mock_mod,
                "schemas.scanner": fake_schemas, "cloud": MagicMock(), "cloud.runpod": MagicMock(),
                "cloud.cloudflare_r2": MagicMock(),
            })
        self.mod.copy_photos = MagicMock()
        self.mod.set_category = MagicMock()
        self.mod.results_directory = lambda: __import__("pathlib").Path("/tmp/results")

    def run_pipeline(self, cloud, req, scan=None):
        self.mod._load_cloud_job = lambda: cloud
        order = []

        def default_scan(**kw):
            order.append(("scan", kw["job_id"], [c["stream"] if "stream" in c else None for c in cloud.calls]))
            self.scanner.state = _State.PROCESSING
            self.scanner.job_id = kw["job_id"] or "20261102-160000-000000"   # scan() makes one when None
            if kw.get("on_potential_last_level"):
                kw["on_potential_last_level"](1)

        self.scanner.scan.side_effect = scan or default_scan
        t = threading.Thread(target=self.mod._orchestrate_pipeline, args=(req,))
        t.start()
        t.join(timeout=20)
        self.assertFalse(t.is_alive(), "the pipeline must end")
        return order

    def test_pod_starts_with_the_scan_and_gets_capture_done(self):
        cloud = FakeCloudJob()
        started_during_scan = threading.Event()

        def scan(**kw):
            # Verify the streaming stage has NOT been started yet at the beginning of scan()
            self.assertEqual(len(cloud.calls), 0)
            self.assertEqual(self.scanner.state, _State.PREPARED)   # nothing changed the scan's state meanwhile
            # Trigger potential last level callback (e.g. shot 0 ToF does not see object)
            if kw.get("on_potential_last_level"):
                kw["on_potential_last_level"](1)
            for _ in range(200):
                if cloud.calls:
                    break
                threading.Event().wait(0.01)
            if cloud.calls and cloud.calls[0].get("stream"):
                started_during_scan.set()
            self.scanner.state = _State.PROCESSING
            self.scanner.job_id = kw["job_id"] or "20261102-160000-000000"   # scan() makes one when None

        self.run_pipeline(cloud, _req(), scan)
        self.assertTrue(started_during_scan.is_set())
        call = cloud.calls[0]
        self.assertEqual(call["job_id"], "20261102-150000-abcdef")
        self.assertEqual(self.scanner.scan.call_args.kwargs["job_id"], "20261102-150000-abcdef")
        self.assertEqual(call["stream_n_photos"], 2 * 20 * 6)        # (475 - 30) // 80 + 1 = 6 rings at most
        self.assertTrue(call["pipeline_options"]["rig_mode"])
        self.assertEqual(call["pipeline_options"]["step_deg"], 18.0)
        done = [e for e in cloud.events if isinstance(e, tuple) and e[0] == "capture_done"]
        self.assertEqual(done, [("capture_done", "20261102-150000-abcdef", self.scanner.uploaded_keys)])
        self.assertEqual(len(cloud.calls), 1)                         # no second pod
        self.assertEqual(self.scanner.state, _State.COMPLETED)
        self.mod.copy_photos.assert_called_once()

    def test_pod_starts_early_with_env(self):
        cloud = FakeCloudJob()
        started_before_scan = threading.Event()

        def scan(**kw):
            for _ in range(200):
                if cloud.calls:
                    break
                threading.Event().wait(0.01)
            if cloud.calls and cloud.calls[0].get("stream"):
                started_before_scan.set()
            self.scanner.state = _State.PROCESSING
            self.scanner.job_id = kw["job_id"] or "20261102-160000-000000"

        with patch.dict(os.environ, {"SCAN_POD_EARLY": "1"}):
            self.run_pipeline(cloud, _req(), scan)
        self.assertTrue(started_before_scan.is_set())

    def test_failed_scan_cancels_the_pod(self):
        cloud = FakeCloudJob()

        def scan(**kw):
            if kw.get("on_potential_last_level"):
                kw["on_potential_last_level"](1)
            raise ValueError("camera died")

        self.run_pipeline(cloud, _req(), scan)
        self.assertIn(("cancel", "20261102-150000-abcdef"), cloud.events)
        self.assertFalse([e for e in cloud.events if isinstance(e, tuple) and e[0] == "capture_done"])
        self.assertEqual(self.scanner.state, _State.ERROR)

    def test_cancelled_scan_cancels_the_pod(self):
        cloud = FakeCloudJob()

        def scan(**kw):
            if kw.get("on_potential_last_level"):
                kw["on_potential_last_level"](1)
            self.scanner._cancel_event.set()
            self.scanner.state = _State.CANCELLED

        self.run_pipeline(cloud, _req(), scan)
        self.assertIn(("cancel", "20261102-150000-abcdef"), cloud.events)
        self.assertEqual(self.scanner.state, _State.CANCELLED)

    def test_streaming_failure_falls_back_to_the_usual_stage(self):
        cloud = FakeCloudJob(fail_stream=True)
        self.run_pipeline(cloud, _req())
        self.assertEqual(cloud.events[0], "run_cloud_job stream")
        self.assertEqual(cloud.events[-1], "run_cloud_job")             # after the scan, as without streaming
        self.assertEqual(self.scanner.state, _State.COMPLETED)

    def test_failed_reconstruction_is_an_error_with_a_retry(self):
        cloud = FakeCloudJob(result="failed")
        self.run_pipeline(cloud, _req())
        self.assertEqual(self.scanner.state, _State.ERROR)
        self.assertEqual(self.mod._cloud_retry.get("job_id"), "20261102-150000-abcdef")

    def test_without_the_option_or_with_flip_the_pod_starts_after_the_scan(self):
        for req in (_req(stream=False), _req(flip=True), _req(stream=None)):
            cloud = FakeCloudJob()
            with patch.dict(os.environ, {"SCAN_CLOUD_STREAM": "0"}):
                order = self.run_pipeline(cloud, req)
            self.assertEqual(order[0][2], [])                            # no cloud call before the photos
            self.assertEqual(cloud.events, ["run_cloud_job"])
            self.assertFalse(cloud.calls[0].get("stream"))
            self.assertEqual(self.scanner.state, _State.COMPLETED)
            self.scanner.state = _State.PREPARED

    def test_env_turns_it_on_by_default(self):
        cloud = FakeCloudJob()
        with patch.dict(os.environ, {"SCAN_CLOUD_STREAM": "1"}):
            self.run_pipeline(cloud, _req(stream=None))
        self.assertTrue(cloud.calls[0].get("stream"))

    def test_no_runpod_no_pod(self):
        cloud = FakeCloudJob()
        self.run_pipeline(cloud, _req(launch=False))
        self.assertEqual(cloud.calls, [])

    def test_max_levels_override(self):
        with patch.dict(os.environ, {"SCAN_STREAM_MAX_LEVELS": "3"}):
            self.assertEqual(self.mod._stream_max_levels(_req()), 3)
        self.assertEqual(self.mod._stream_max_levels(_req()), 6)


if __name__ == "__main__":
    unittest.main()
