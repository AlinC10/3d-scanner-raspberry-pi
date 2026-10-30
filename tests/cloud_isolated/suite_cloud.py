"""
suite_cloud.py — the cloud/ module (copied from Turntable Studio with its tests/test_cloud.py), with an in-memory
S3 and a fake RunPod API (no network). Run in a clean interpreter by tests/test_cloud_module.py, because
tests/__init__.py replaces `cloud` with mocks.
"""

import io
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
from botocore.exceptions import ClientError

from cloud import cloudflare_r2 as r2
from cloud import runpod as rp


class FakeS3:
    """The subset of the boto3 S3 client the module uses, backed by a dict."""

    def __init__(self):
        self.objects = {}
        self.mtimes = {}          # (bucket, key) -> LastModified; objects without one count as written "now"

    def upload_file(self, file_name, bucket, key, **_):
        self.objects[(bucket, key)] = Path(file_name).read_bytes()

    def download_file(self, bucket, key, file_name):
        if (bucket, key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "GetObject")
        Path(file_name).write_bytes(self.objects[(bucket, key)])

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop((Bucket, o["Key"]), None)
        return {}

    def get_paginator(self, _name):
        store = self.objects

        mtimes = self.mtimes

        class Pager:
            def paginate(self, Bucket, Prefix=""):
                now = datetime.now(timezone.utc)
                yield {"Contents": [{"Key": k, "LastModified": mtimes.get((b, k), now)}
                                    for (b, k) in sorted(store) if b == Bucket and k.startswith(Prefix)]}
        return Pager()


class FakeRunPod:
    """create / get / delete pod; get_pod reports RUNNING until `exit_after` polls."""

    def __init__(self, fail_creates=0, exit_after=None, delete_fails=False):
        self.created, self.deleted, self.bodies = [], [], []
        self.fail_creates = fail_creates
        self.exit_after = exit_after
        self.delete_fails = delete_fails
        self.polls = 0

    def create_pod(self, body):
        self.bodies.append(body)
        if self.fail_creates > 0:
            self.fail_creates -= 1
            raise rp.RunPodError("no capacity")
        pod_id = f"pod{len(self.created) + 1}"
        self.created.append(pod_id)
        return {"id": pod_id, "gpu": {"displayName": "RTX A4000"}, "memoryInGb": body["minRAMPerGPU"],
                "vcpuCount": body["minVCPUPerGPU"], "costPerHr": 0.36}

    def get_pod(self, pod_id):
        self.polls += 1
        state = "EXITED" if self.exit_after is not None and self.polls >= self.exit_after else "RUNNING"
        return {"id": pod_id, "desiredStatus": state}

    def delete_pod(self, pod_id):
        if self.delete_fails:
            raise rp.RunPodError("network")
        self.deleted.append(pod_id)


BUCKET = "test-bucket"


class TestR2Layout(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_delete_stale_jobs_only_touches_old_job_folders(self):
        """Old failed jobs go; a job written recently (may be running), the current job and other prefixes stay."""
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        for key in ("jobs/old/input/rig/0/f_1000.jpg", "jobs/old/output/status.json"):
            self.s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
            self.s3.mtimes[(BUCKET, key)] = old
        # old photos but a fresh started.json: still running
        self.s3.put_object(Bucket=BUCKET, Key="jobs/running/input/f.jpg", Body=b"x")
        self.s3.mtimes[(BUCKET, "jobs/running/input/f.jpg")] = old
        self.s3.put_object(Bucket=BUCKET, Key="jobs/running/output/started.json", Body=b"{}")
        self.s3.put_object(Bucket=BUCKET, Key="jobs/mine/input/f.jpg", Body=b"x")
        self.s3.mtimes[(BUCKET, "jobs/mine/input/f.jpg")] = old
        self.s3.put_object(Bucket=BUCKET, Key="scans/f.jpg", Body=b"x")      # the scanner's own folder
        self.s3.mtimes[(BUCKET, "scans/f.jpg")] = old

        deleted = r2.delete_stale_jobs(2 * 3600, BUCKET, client=self.s3, keep=["mine"])
        self.assertEqual(deleted, ["old"])
        keys = {k for (_, k) in self.s3.objects}
        self.assertFalse(any(k.startswith("jobs/old/") for k in keys))
        self.assertIn("jobs/running/input/f.jpg", keys)
        self.assertIn("jobs/mine/input/f.jpg", keys)
        self.assertIn("scans/f.jpg", keys)

        # cloud_scan's default: every earlier job goes, the current one and other prefixes stay
        self.assertEqual(r2.delete_stale_jobs(0, BUCKET, client=self.s3, keep=["mine"], now=1e12), ["running"])
        keys = {k for (_, k) in self.s3.objects}
        self.assertEqual(keys, {"jobs/mine/input/f.jpg", "scans/f.jpg"})

    def test_keys(self):
        self.assertEqual(r2.input_key("j1", "rig\\0/f_1000.jpg"), "jobs/j1/input/rig/0/f_1000.jpg")
        self.assertEqual(r2.output_key("j1", "status.json"), "jobs/j1/output/status.json")
        for bad in ("", "a/b", ".."):
            with self.assertRaises(ValueError):
                r2.job_prefix(bad)

    def test_upload_download_roundtrip_keeps_rig_layout(self):
        src = self.tmp / "in"
        for cam in ("0", "1"):
            (src / "rig" / cam).mkdir(parents=True)
            (src / "rig" / cam / "f_1000.jpg").write_bytes(b"jpeg" + cam.encode())
        (src / "rig" / "0" / "empty.jpg").write_bytes(b"")          # failed capture: not uploaded
        res = r2.upload_job_images("j1", str(src), BUCKET, client=self.s3, workers=2)
        self.assertEqual(sorted(res["uploaded"]), ["jobs/j1/input/rig/0/f_1000.jpg", "jobs/j1/input/rig/1/f_1000.jpg"])
        self.assertEqual(res["failed"], ["jobs/j1/input/rig/0/empty.jpg"])

        expected = [r2.input_key("j1", "rig/0/f_1000.jpg"), r2.input_key("j1", "rig/1/f_1000.jpg")]
        self.assertEqual(r2.verify_keys(expected, BUCKET, prefix=r2.input_prefix("j1"), client=self.s3), [])

        dst = self.tmp / "pod_in"
        r2.download_job_images("j1", str(dst), BUCKET, client=self.s3)
        self.assertEqual((dst / "rig" / "1" / "f_1000.jpg").read_bytes(), b"jpeg1")

    def test_delete_prefix_only_inside_jobs(self):
        self.s3.put_object(Bucket=BUCKET, Key="jobs/j1/input/a.jpg", Body=b"x")
        self.s3.put_object(Bucket=BUCKET, Key="jobs/j2/input/a.jpg", Body=b"x")
        self.assertEqual(r2.delete_prefix(r2.job_prefix("j1"), BUCKET, client=self.s3), 1)
        self.assertIn((BUCKET, "jobs/j2/input/a.jpg"), self.s3.objects)
        with self.assertRaises(r2.R2Error):
            r2.delete_prefix("", BUCKET, client=self.s3)

    def test_download_output_extracts_zip_and_rejects_escape(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("glb/high_model.glb", b"glb")
            zf.writestr("logs/run_log.txt", b"log")
        self.s3.put_object(Bucket=BUCKET, Key=r2.output_key("j1", "output.zip"), Body=buf.getvalue())
        r2.put_json({"state": "ok"}, r2.output_key("j1", "status.json"), BUCKET, client=self.s3)
        dest = r2.download_job_output("j1", str(self.tmp / "res" / "j1"), BUCKET, client=self.s3)
        self.assertEqual((dest / "glb" / "high_model.glb").read_bytes(), b"glb")
        self.assertEqual(json.loads((dest / "status.json").read_text())["state"], "ok")

        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", b"x")
        self.s3.put_object(Bucket=BUCKET, Key=r2.output_key("j2", "output.zip"), Body=bad.getvalue())
        with self.assertRaises(r2.R2Error):
            r2.download_job_output("j2", str(self.tmp / "res" / "j2"), BUCKET, client=self.s3)
        self.assertFalse((self.tmp / "res" / "evil.txt").exists())

    def test_get_json_missing_is_none(self):
        self.assertIsNone(r2.get_json("jobs/x/output/status.json", BUCKET, client=self.s3))


class TestTempCredentials(unittest.TestCase):
    def test_request_is_object_level_and_prefix_scoped(self):
        body = r2.temp_credentials_request(BUCKET, "parent", "object-read-only", ["jobs/j1/input/"], 3600)
        self.assertEqual(body, {"bucket": BUCKET, "parentAccessKeyId": "parent", "permission": "object-read-only",
                                "prefixes": ["jobs/j1/input/"], "ttlSeconds": 3600})
        with self.assertRaises(ValueError):
            r2.temp_credentials_request(BUCKET, "parent", "admin-read-write", ["jobs/j1/"], 3600)
        with self.assertRaises(ValueError):
            r2.temp_credentials_request(BUCKET, "parent", "object-read-write", ["photos/"], 3600)
        with self.assertRaises(ValueError):
            r2.temp_credentials_request(BUCKET, "parent", "object-read-write", [], 3600)

    def test_pod_env_has_read_only_input_and_write_output(self):
        calls = []

        class Session:
            def post(self, url, headers, json, timeout):
                calls.append(json)
                resp = mock.Mock(status_code=200)
                n = len(calls)
                resp.json.return_value = {"success": True, "result": {
                    "accessKeyId": f"id{n}", "secretAccessKey": f"s{n}", "sessionToken": f"t{n}"}}
                return resp

        with mock.patch.object(r2, "requests", Session()), mock.patch.dict(os.environ, {
                "CLOUDFLARE_ACCOUNT_ID": "acc", "CLOUDFLARE_R2_API_TOKEN": "tok", "R2_ACCESS_KEY_ID": "parent",
                "R2_ENDPOINT_URL": "https://acc.r2.cloudflarestorage.com", "R2_BUCKET": BUCKET}):
            env = r2.pod_credentials_env("j1", 3600)
        self.assertEqual([c["permission"] for c in calls], ["object-read-only", "object-read-write"])
        self.assertEqual([c["prefixes"] for c in calls], [["jobs/j1/input/"], ["jobs/j1/output/"]])
        self.assertEqual(env["R2_IN_ACCESS_KEY_ID"], "id1")
        self.assertEqual(env["R2_OUT_SESSION_TOKEN"], "t2")
        self.assertNotIn("R2_SECRET_ACCESS_KEY", env)   # the account key never goes to the pod


class TestRunPod(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(rp.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, ram=16):
        return rp.build_pod_request("ghcr.io/u/img:v1", {"JOB_ID": "j1", "R2_OUT_SECRET_ACCESS_KEY": "x"},
                                    ram, 8, 30, ["NVIDIA RTX A4000", "NVIDIA GeForce RTX 3090"])

    def test_request(self):
        b = self.body()
        self.assertEqual(b["gpuTypeIds"], ["NVIDIA RTX A4000", "NVIDIA GeForce RTX 3090"])
        self.assertEqual(b["gpuTypePriority"], "custom")
        self.assertEqual((b["minRAMPerGPU"], b["minVCPUPerGPU"], b["gpuCount"]), (16, 8, 1))
        self.assertEqual(rp.redact(b)["env"]["R2_OUT_SECRET_ACCESS_KEY"], "***")
        self.assertEqual(rp.disk_gb_for(120, 0.6, 1), 22)

    def test_pod_terminated_after_status(self):
        api = FakeRunPod()
        statuses = iter([None, None, {"state": "ok"}])
        run = rp.run_pod(api, self.body(), lambda: next(statuses), poll_s=0)
        self.assertEqual((run.outcome, run.status["state"], run.terminated), ("finished", "ok", True))
        self.assertEqual(api.deleted, ["pod1"])

    def test_timeout_and_failed_terminate(self):
        api = FakeRunPod(delete_fails=True)
        run = rp.run_pod(api, self.body(), lambda: None, poll_s=0, max_runtime_s=-1)
        self.assertEqual(run.outcome, "timeout")
        self.assertFalse(run.terminated)

    def test_pod_that_never_starts_is_replaced(self):
        # Host stuck pulling the image: the pod stays RUNNING but never writes started.json
        api = FakeRunPod()
        started = iter([False, True])          # second pod starts at its first check
        statuses = iter([None, {"state": "ok"}])
        with mock.patch.object(rp, "BOOT_TIMEOUT_S", -1):
            runs = rp.run_job_pods(api, self.body, 16, lambda: next(statuses, None), read_started=lambda: next(started))
        self.assertEqual([r.outcome for r in runs], ["boot_timeout", "finished"])
        self.assertEqual(api.bodies[1]["minRAMPerGPU"], 16)   # same request, new pod
        self.assertEqual(api.deleted, ["pod1", "pod2"])

    def test_started_pod_is_not_boot_timed_out(self):
        api = FakeRunPod()
        statuses = iter([None, None, {"state": "ok"}])
        run = rp.run_pod(api, self.body(), lambda: next(statuses), poll_s=0, read_started=lambda: True,
                         boot_timeout_s=-1)
        self.assertEqual(run.outcome, "finished")

    def test_create_retries_then_gives_up(self):
        api = FakeRunPod(fail_creates=99)
        self.assertIsNone(rp.run_pod(api, self.body(), lambda: None, poll_s=0))
        self.assertEqual(len(api.bodies), rp.MAX_CREATE_ATTEMPTS)
        self.assertEqual(api.created, [])

    def test_ram_insufficient_retries_with_more_ram(self):
        api = FakeRunPod()
        statuses = iter([{"state": "ram_insufficient", "required_gb": 30}, {"state": "ok"}])
        cleared = []
        runs = rp.run_job_pods(api, self.body, 16, lambda: next(statuses), before_retry=lambda: cleared.append(1))
        self.assertEqual([r.status["state"] for r in runs], ["ram_insufficient", "ok"])
        self.assertEqual(api.bodies[1]["minRAMPerGPU"], 36)  # 30 GB measured * 1.2
        self.assertEqual(cleared, [1])
        self.assertEqual(api.deleted, ["pod1", "pod2"])

    def test_pipeline_error_is_not_retried(self):
        api = FakeRunPod()
        runs = rp.run_job_pods(api, self.body, 16, lambda: {"state": "pipeline_error"})
        self.assertEqual(len(runs), 1)

    def test_exit_without_status_retries_with_more_ram(self):
        api = FakeRunPod(exit_after=1)
        runs = rp.run_job_pods(api, self.body, 16, lambda: None)
        self.assertEqual([r.outcome for r in runs], ["exited", "exited"])
        self.assertEqual(api.bodies[1]["minRAMPerGPU"], 24)


class TestPlanJob(unittest.TestCase):
    def test_plan_from_photos(self):
        from PIL import Image
        from cloud.job import plan_job
        with tempfile.TemporaryDirectory() as tmp:
            for cam in ("0", "1"):
                d = Path(tmp) / "rig" / cam
                d.mkdir(parents=True)
                for k in range(3):
                    Image.fromarray(np.zeros((304, 405, 3), np.uint8)).save(d / f"f_100{k}.jpg")
            plan = plan_job(tmp)
        self.assertEqual((plan["n_images"], plan["width"], plan["height"]), (6, 405, 304))
        self.assertGreaterEqual(plan["min_ram_gb"], plan["ram_estimate_gb"])



    def test_image_prefix_keeps_only_the_scanner_photo_tree(self):
        from PIL import Image
        from cloud.job import plan_job
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("rig/0", "rig/1", "other"):
                d = Path(tmp) / sub
                d.mkdir(parents=True)
                Image.fromarray(np.zeros((30, 40, 3), np.uint8)).save(d / "f_1000.jpg")
            plan = plan_job(tmp, image_prefix="rig/")
        self.assertEqual(plan["n_images"], 2)

    def test_ram_estimate_is_local(self):
        from cloud import resources
        self.assertGreaterEqual(resources.estimate_ram_gb(1, 10, 10), resources.RAM_FLOOR_GB)
        self.assertAlmostEqual(resources.estimate_ram_gb(100, 4056, 3040, 1), 6.8 + 12.0 * 100 * 4056 * 3040 / 4 / 1e9)


class TestScannerAdditions(unittest.TestCase):
    def test_extraction_is_refused_without_disk_space(self):
        s3 = FakeS3()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("glb/low_model.glb", b"x" * 1000)
        s3.objects[(BUCKET, r2.output_key("j1", "output.zip"))] = buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(r2.shutil, "disk_usage", return_value=mock.Mock(free=100)):
            with self.assertRaises(r2.R2Error) as ctx:
                r2.download_job_output("j1", str(Path(tmp) / "j1"), BUCKET, client=s3)
        self.assertIn("disk space", str(ctx.exception))

    def test_stale_turntable_pods_are_terminated_and_others_kept(self):
        class Client(FakeRunPod):
            def list_pods(self):
                return [{"id": "a", "name": "turntable-old"}, {"id": "b", "name": "my-notebook"},
                        {"id": "c", "name": "turntable-older"}]
        client = Client()
        self.assertEqual(rp.terminate_stale_pods(client), ["a", "c"])
        self.assertEqual(client.deleted, ["a", "c"])

    def test_a_pod_that_cannot_be_terminated_is_an_error(self):
        class Client(FakeRunPod):
            def list_pods(self):
                return [{"id": "a", "name": "turntable-old"}]
        with mock.patch.object(rp, "TERMINATE_RETRY_S", 0):
            with self.assertRaises(rp.RunPodError):
                rp.terminate_stale_pods(Client(delete_fails=True))

    def test_upload_attempt_time_limit(self):
        class SlowS3(FakeS3):
            def upload_file(self, file_name, bucket, key, Callback=None):
                Callback(10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jpg"
            path.write_bytes(b"x")
            self.assertFalse(r2.upload_file(str(path), BUCKET, "k", client=SlowS3(), timeout_s=1e-9))
            self.assertTrue(r2.upload_file(str(path), BUCKET, "k", client=SlowS3(), timeout_s=60))
