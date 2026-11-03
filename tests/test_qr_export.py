"""LIB-QR: system/qr_export.py with a fake R2 (S3) client; the endpoints in a subprocess with the real FastAPI."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, buckets=("scanner-exports",), can_create=True):
        self.buckets = set(buckets)
        self.can_create = can_create
        self.objects = {}
        self.uploads = 0

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise FakeError("404")

    def create_bucket(self, Bucket):
        if not self.can_create:
            raise FakeError("AccessDenied")
        self.buckets.add(Bucket)

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise FakeError("404")
        return {"Metadata": self.objects[(Bucket, Key)]["meta"]}

    def upload_file(self, path, bucket, key, Callback=None, ExtraArgs=None):
        data = Path(path).read_bytes()
        self.objects[(bucket, key)] = {"data": data, "meta": dict((ExtraArgs or {}).get("Metadata") or {})}
        self.uploads += 1
        if Callback:
            Callback(len(data))

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://r2.example/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class QrExportTest(unittest.TestCase):
    def setUp(self):
        self.qr = load("qr_export_under_test", "system/qr_export.py")
        self.usb = load("usb_for_qr_test", "system/usb.py")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "20261101-120000-abcdef"
        for rel in ("glb/low_model.glb", "stl/high_model.stl", "object.json", "photos/rig/0/f_0.jpg"):
            (self.folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / rel).write_bytes(b"x" * 100)

    def run_export(self, s3):
        self.qr.start_export(self.folder, client_factory=lambda: s3, files_fn=self.usb.export_files)
        for _ in range(200):
            status = self.qr.export_status()
            if status["state"] not in self.qr.RUNNING_STATES:
                return status
            time.sleep(0.01)
        self.fail("export did not finish")

    def test_uploads_only_the_models_and_signs_a_link(self):
        s3 = FakeS3()
        status = self.run_export(s3)
        self.assertEqual(status["state"], "done")
        self.assertTrue(status["url"].startswith("https://r2.example/scanner-exports/20261101-120000-abcdef.zip"))
        self.assertGreater(status["expires_at"], time.time() + 3500)
        data = s3.objects[("scanner-exports", "20261101-120000-abcdef.zip")]["data"]
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(names, ["20261101-120000-abcdef/glb/low_model.glb", "20261101-120000-abcdef/object.json",
                                 "20261101-120000-abcdef/stl/high_model.stl"])
        self.assertFalse(any(p.name.endswith(".part") for p in self.folder.parent.iterdir()))

    def test_same_models_reuse_the_archive_and_a_change_uploads_again(self):
        s3 = FakeS3()
        self.run_export(s3)
        self.assertTrue(self.run_export(s3)["reused"])
        self.assertEqual(s3.uploads, 1)
        (self.folder / "glb/low_model.glb").write_bytes(b"y" * 120)
        self.assertFalse(self.run_export(s3)["reused"])
        self.assertEqual(s3.uploads, 2)

    def test_missing_bucket_is_created_or_explained(self):
        s3 = FakeS3(buckets=())
        self.assertEqual(self.run_export(s3)["state"], "done")
        self.assertIn("scanner-exports", s3.buckets)
        status = self.run_export(FakeS3(buckets=(), can_create=False))
        self.assertEqual(status["state"], "error")
        self.assertIn("Create bucket", status["error"])

    def test_bucket_name_from_the_environment(self):
        os.environ["R2_EXPORT_BUCKET"] = "my-exports"
        self.addCleanup(os.environ.pop, "R2_EXPORT_BUCKET")
        s3 = FakeS3(buckets=("my-exports",))
        self.assertEqual(self.run_export(s3)["state"], "done")
        self.assertIn(("my-exports", "20261101-120000-abcdef.zip"), s3.objects)

    def test_refusals(self):
        empty = Path(self._tmp.name) / "20261101-120000-000000"
        empty.mkdir()
        with self.assertRaises(self.qr.QrExportError):
            self.qr.start_export(empty, client_factory=FakeS3, files_fn=self.usb.export_files)

        def no_r2():
            raise RuntimeError("missing R2_ENDPOINT_URL")
        with self.assertRaises(self.qr.QrExportUnavailableError):
            self.qr.start_export(self.folder, client_factory=no_r2, files_fn=self.usb.export_files)
        self.qr._status.update(state="uploading", object_id="x")
        with self.assertRaises(self.qr.QrExportBusyError):
            self.qr.start_export(self.folder, client_factory=FakeS3, files_fn=self.usb.export_files)

    def test_head_bucket_403_proceeds_to_object_operations(self):
        class NonAdminS3(FakeS3):
            def head_bucket(self, Bucket):
                raise FakeError("403")

        s3 = NonAdminS3()
        status = self.run_export(s3)
        self.assertEqual(status["state"], "done")
        self.assertIn(("scanner-exports", "20261101-120000-abcdef.zip"), s3.objects)

    def test_auto_delete_removes_archive(self):
        s3 = FakeS3()
        s3.objects[("scanner-exports", "test.zip")] = {"data": b"123", "meta": {}}
        self.qr._schedule_auto_delete(s3, "scanner-exports", "test.zip", 0.05)
        time.sleep(0.12)
        self.assertNotIn(("scanner-exports", "test.zip"), s3.objects)

    def test_expired_link_is_not_offered(self):
        self.qr._status.update(state="done", url="https://x", expires_at=time.time() - 1)
        self.assertEqual((self.qr.export_status()["state"], self.qr.export_status()["url"]), ("expired", None))

    def test_qr_svg(self):
        svg = self.qr.qr_svg("https://r2.example/a.zip")
        self.assertIn("<svg", svg)


API_PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from fastapi import FastAPI
from fastapi.testclient import TestClient
import router.library as library

app = FastAPI()
app.include_router(library.router)
client = TestClient(app)
oid = "20261027-202750-21d2de"
out = {"status": client.get("/library/export/qr/status").json()["state"],
       "image_none": client.get("/library/export/qr/image.svg").status_code,
       "missing": client.post("/library/objects/20261027-202750-000000/export/qr").status_code}
r = client.post(f"/library/objects/{oid}/export/qr")
out["no_r2"] = [r.status_code, "R2" in r.json()["detail"]]
library.qr_export._status.update(state="done", url="https://r2.example/x.zip", expires_at=9e12)
img = client.get("/library/export/qr/image.svg")
out["image"] = [img.status_code, img.headers["content-type"], "<svg" in img.text]
print(json.dumps(out))
"""


class QrExportApiTest(unittest.TestCase):
    def test_api(self):
        from tests.test_library import make_result
        with tempfile.TemporaryDirectory() as tmp:
            make_result(Path(tmp), "20261027-202750-21d2de")
            # Empty R2 settings: load_dotenv() never overrides a variable that is set, so the real .env keys are not
            # used and no request reaches Cloudflare
            env = {**os.environ, "RESULTS_DIR": tmp, "R2_ENDPOINT_URL": "", "R2_ACCESS_KEY_ID": "",
                   "R2_SECRET_ACCESS_KEY": ""}
            out = subprocess.run([sys.executable, "-c", API_PROGRAM % ROOT], capture_output=True, text=True, env=env,
                                 timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        r = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(r["status"], "idle")
        self.assertEqual(r["image_none"], 404)
        self.assertEqual(r["missing"], 404)
        self.assertEqual(r["no_r2"], [503, True])
        self.assertEqual(r["image"], [200, "image/svg+xml", True])


if __name__ == "__main__":
    unittest.main()
