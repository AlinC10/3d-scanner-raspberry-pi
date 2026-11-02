"""
Unit tests for the unified Jobs API, JobManager, and parallel scan decoupling.
Runs in a subprocess to use the real FastAPI and pydantic without mocks from tests/__init__.py.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PROGRAM = r"""
import json, sys, os
from unittest.mock import MagicMock, patch

sys.path.insert(0, %r)
os.environ["MOCK_HARDWARE"] = "1"

from cloud.jobs_manager import jobs_manager, JobManager, JobRecord
from hardware.scanner import ScannerState
from fastapi.testclient import TestClient
from main_dev import app

out = {}

# 1. Direct JobManager tests
jm = JobManager()
rec = jm.register_job("job-1", kind="object", status="running", created_at=100.0)
out["register_ok"] = (rec.job_id == "job-1" and rec.kind == "object" and rec.status == "running")

jm.register_job("job-2", kind="face", status="running", created_at=200.0)
jm.register_job("job-3", kind="object", status="completed", created_at=150.0)

ordered_ids = [j.job_id for j in jm.list_jobs()]
out["list_order"] = (ordered_ids == ["job-2", "job-3", "job-1"])

# Update job
updated = jm.update_job("job-1", status="processing", pod_id="pod-test-1")
out["update_processing"] = (updated.status == "processing" and updated.pod_id == "pod-test-1" and updated.completed_at is None)
done = jm.update_job("job-1", status="completed")
out["update_completed"] = (done.status == "completed" and done.completed_at is not None)

# Active pods and jobs
jm.register_job("active-pod-job", status="processing", pod_id="pod-live")
jm.register_job("done-pod-job", status="completed", pod_id="pod-dead")
out["active_pods"] = jm.get_active_pod_ids()
out["active_jobs"] = jm.get_active_job_ids()

# Cancel job
with patch("cloud.runpod.RunPodClient") as mock_rpc:
    client_inst = MagicMock()
    mock_rpc.return_value = client_inst
    jm.register_job("cancel-me", status="processing", pod_id="pod-to-kill")
    cancel_ok = jm.cancel_job("cancel-me")
    rec_cancelled = jm.get_job("cancel-me")
    out["cancel_job"] = [cancel_ok, rec_cancelled.status, rec_cancelled.cancel_event.is_set()]
    client_inst.delete_pod.assert_called_once_with("pod-to-kill")
    out["cancel_pod_called"] = True

# 2. HTTP Jobs API tests via FastAPI TestClient
jobs_manager.clear()
client = TestClient(app)

# GET /jobs empty
res_empty = client.get("/jobs")
out["http_get_empty"] = [res_empty.status_code, res_empty.json()]

# Register jobs
jobs_manager.register_job("obj-job-100", kind="object", status="processing")
jobs_manager.register_job("face-job-200", kind="face", status="running")

# GET /jobs all
res_all = client.get("/jobs")
all_data = res_all.json()
out["http_get_all"] = [res_all.status_code, len(all_data), sorted([j["job_id"] for j in all_data])]

# GET /jobs?id=obj-job-100
res_filtered = client.get("/jobs?id=obj-job-100")
out["http_get_filtered"] = [res_filtered.status_code, res_filtered.json()["job_id"], res_filtered.json()["kind"]]

# GET /jobs?id=not-found
res_not_found = client.get("/jobs?id=nonexistent")
out["http_filtered_404"] = res_not_found.status_code

# GET /jobs/{job_id}
res_by_path = client.get("/jobs/face-job-200")
out["http_get_path"] = [res_by_path.status_code, res_by_path.json()["job_id"], res_by_path.json()["kind"]]

# GET /jobs/{job_id} 404
res_path_404 = client.get("/jobs/no-such-id")
out["http_path_404"] = res_path_404.status_code

# POST /jobs/{job_id}/cancel
res_cancel = client.post("/jobs/face-job-200/cancel")
out["http_cancel"] = [res_cancel.status_code, res_cancel.json()]
out["http_cancel_status_in_mgr"] = jobs_manager.get_job("face-job-200").status

# POST /jobs/{job_id}/cancel 404
res_cancel_404 = client.post("/jobs/unknown-job/cancel")
out["http_cancel_404"] = res_cancel_404.status_code

# 3. Parallel scan decoupling checks
out["processing_in_prepare_allowed"] = ScannerState.PROCESSING in ScannerState.PREPARE_ALLOWED

# 4. Stale pod preservation check
from cloud.runpod import terminate_stale_pods
mock_rp = MagicMock()
mock_rp.list_pods.return_value = [
    {"id": "pod-keep-1", "name": "turntable-pi-1"},
    {"id": "pod-stale", "name": "turntable-pi-stale"},
]
out["stale_pods_kept"] = terminate_stale_pods(mock_rp, keep=("pod-keep-1",))
mock_rp.delete_pod.assert_called_once_with("pod-stale")

print(json.dumps(out))
""" % ROOT


class TestJobsManagerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=120)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-3000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_direct_job_manager_operations(self):
        self.assertTrue(self.r["register_ok"])
        self.assertTrue(self.r["list_order"])
        self.assertTrue(self.r["update_processing"])
        self.assertTrue(self.r["update_completed"])
        self.assertIn("pod-live", self.r["active_pods"])
        self.assertNotIn("pod-dead", self.r["active_pods"])
        self.assertIn("active-pod-job", self.r["active_jobs"])
        self.assertNotIn("done-pod-job", self.r["active_jobs"])
        self.assertEqual(self.r["cancel_job"], [True, "cancelled", True])
        self.assertTrue(self.r["cancel_pod_called"])

    def test_jobs_router_endpoints(self):
        self.assertEqual(self.r["http_get_empty"], [200, []])
        self.assertEqual(self.r["http_get_all"], [200, 2, ["face-job-200", "obj-job-100"]])
        self.assertEqual(self.r["http_get_filtered"], [200, "obj-job-100", "object"])
        self.assertEqual(self.r["http_filtered_404"], 404)
        self.assertEqual(self.r["http_get_path"], [200, "face-job-200", "face"])
        self.assertEqual(self.r["http_path_404"], 404)
        self.assertEqual(self.r["http_cancel"], [200, {"status": "cancelled", "job_id": "face-job-200"}])
        self.assertEqual(self.r["http_cancel_status_in_mgr"], "cancelled")
        self.assertEqual(self.r["http_cancel_404"], 404)

    def test_parallel_scan_decoupling_and_pod_preservation(self):
        self.assertTrue(self.r["processing_in_prepare_allowed"])
        self.assertEqual(self.r["stale_pods_kept"], ["pod-stale"])


if __name__ == "__main__":
    unittest.main()
