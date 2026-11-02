"""
suite_stream.py — the pod started with the scan (cloud.job stream=True, Turntable Studio image v10+): the plan made
before the photos exist, capture_done.json / cancel.json for the pod, the job spec, a whole streamed job against an
in-memory S3 and a fake RunPod run. Run in a clean interpreter by tests/test_cloud_module.py.
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cloud import cloudflare_r2 as r2
from cloud import job as cj
from cloud import runpod as rp
from suite_cloud import BUCKET, FakeS3


class TestStreamPlan(unittest.TestCase):
    def test_photo_count_and_plan(self):
        self.assertEqual(cj.stream_n_images(18.0, 6), 240)
        self.assertEqual(cj.stream_n_images(24.0, 2), 60)
        plan = cj.plan_stream_job(240)
        self.assertEqual(plan["n_images"], 240)
        self.assertEqual(plan["files"], {})
        self.assertTrue(plan["stream"])
        # the same RAM formula as plan_job on that many photos
        self.assertEqual(plan["min_ram_gb"], int(__import__("math").ceil(
            cj.estimate_ram_gb(240, 4056, 3040, 1) * cj.RAM_MARGIN)))
        self.assertGreaterEqual(plan["min_ram_gb"], cj.plan_stream_job(80)["min_ram_gb"])

    def test_pod_step_label_while_waiting(self):
        self.assertIn("waiting for the photos", cj.pod_step_label({"step": "0. Waiting for photos", "photos": 12}))


class TestStreamMarkers(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        patcher = mock.patch.dict(os.environ, {"R2_BUCKET": BUCKET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_capture_done_lists_the_verified_photos(self):
        keys = [r2.input_key("j1", "rig/1/f_2000.jpg"), r2.input_key("j1", "rig/0/f_1000.jpg"), "rig/0/f_1001.jpg"]
        body = cj.write_capture_done("j1", keys, 18.0, client=self.s3)
        stored = json.loads(self.s3.objects[(BUCKET, r2.input_key("j1", "capture_done.json"))])
        self.assertEqual(stored["files"], ["rig/0/f_1000.jpg", "rig/0/f_1001.jpg", "rig/1/f_2000.jpg"])
        self.assertEqual(stored["levels"], [1, 2])
        self.assertEqual(stored["n_images"], 3)
        self.assertEqual(body["step_deg"], 18.0)

    def test_cancel_marker_never_raises(self):
        self.assertTrue(cj.write_cancel("j2", client=self.s3))
        self.assertIn((BUCKET, r2.input_key("j2", "cancel.json")), self.s3.objects)

        class Broken(FakeS3):
            def put_object(self, **kw):
                raise RuntimeError("network")
        self.assertFalse(cj.write_cancel("j3", client=Broken()))

    def test_any_event(self):
        a, b = threading.Event(), threading.Event()
        ev = cj.AnyEvent(a, b, None)
        self.assertFalse(ev.wait(timeout=0.05))
        threading.Timer(0.1, b.set).start()
        t0 = time.time()
        self.assertTrue(ev.wait(timeout=5))
        self.assertLess(time.time() - t0, 2)


class TestStreamedJob(unittest.TestCase):
    """run_cloud_job(stream=True): no photo needed at the start, "stream" in the job, the usual end."""

    def setUp(self):
        self.s3 = FakeS3()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"R2_BUCKET": BUCKET, "TURNTABLE_IMAGE": "img:v10", "RUNPOD_API_KEY": "x"}
        for p in (mock.patch.dict(os.environ, env),
                  mock.patch.object(r2, "client_from_env", lambda prefix="R2_": self.s3),
                  mock.patch.object(r2, "pod_credentials_env", lambda job_id, ttl: {"R2_IN_ACCESS_KEY_ID": "t"}),
                  mock.patch.object(rp, "RunPodClient", lambda *a, **k: object()),
                  mock.patch.object(rp, "terminate_stale_pods", lambda client: [])):
            p.start()
            self.addCleanup(p.stop)

    def test_dry_run_job_spec(self):
        rep = cj.run_cloud_job(input_dir=str(Path(self.tmp.name) / "not_there"), job_id="j5", dry_run=True,
                               pipeline_options={"step_deg": 18.0, "rig_mode": True}, stream=True,
                               stream_n_photos=120)
        body = json.dumps(rep["pod_request"])
        self.assertIn('\\"stream\\": true', body)
        self.assertEqual(rep["plan"]["n_images"], 120)

    def test_streamed_job_end_to_end(self):
        seen = {}

        def fake_run_job_pods(client, make_body, min_ram_gb, read_status, cancel_event=None, **kw):
            body = make_body(min_ram_gb)
            env = body.get("env")
            env = {e["key"]: e["value"] for e in env} if isinstance(env, list) else env
            seen["job"] = json.loads(env["JOB_JSON"])
            seen["min_ram_gb"] = min_ram_gb
            # the pod: waits for capture_done.json (written by the scanner meanwhile), then delivers
            deadline = time.time() + 10
            while (BUCKET, r2.input_key("j6", "capture_done.json")) not in self.s3.objects:
                self.assertLess(time.time(), deadline)
                time.sleep(0.02)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("object.json", "{}")
            self.s3.put_object(Bucket=BUCKET, Key=r2.output_key("j6", "output.zip"), Body=buf.getvalue())
            self.s3.put_object(Bucket=BUCKET, Key=r2.output_key("j6", "status.json"),
                               Body=json.dumps({"state": "ok", "timings_s": {"wait_after_capture_s": 100}}))
            run = rp.PodRun(pod_id="p1", started_at=time.time(), outcome="finished", status=read_status(),
                            terminated=True)
            return [run]

        with mock.patch.object(rp, "run_job_pods", fake_run_job_pods):
            result = {}
            t = threading.Thread(target=lambda: result.update(cj.run_cloud_job(
                input_dir=str(Path(self.tmp.name) / "photos"), results_dir=str(Path(self.tmp.name) / "results"),
                job_id="j6", pipeline_options={"step_deg": 18.0, "rig_mode": True}, stream=True,
                stream_n_photos=cj.stream_n_images(18.0, 6))))
            t.start()
            # the scan: photos while the pod runs, then the marker
            for k in range(3):
                self.s3.put_object(Bucket=BUCKET, Key=r2.input_key("j6", f"rig/0/f_100{k}.jpg"), Body=b"jpg")
            cj.write_capture_done("j6", [f"rig/0/f_100{k}.jpg" for k in range(3)], 18.0, client=self.s3)
            t.join(timeout=20)
        self.assertEqual(result.get("result"), "ok", result)
        self.assertTrue(seen["job"]["stream"])
        self.assertEqual(result["plan"]["n_images"], 240)
        self.assertTrue((Path(result["results_dir"]) / "object.json").is_file())
        self.assertFalse([k for (b, k) in self.s3.objects if k.startswith("jobs/j6/")])   # R2 cleaned


if __name__ == "__main__":
    unittest.main()
