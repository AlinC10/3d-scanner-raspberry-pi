"""PATCH /library/objects/{id} (object.json edits) and the USB export (system/usb.py with scripted lsblk/udisksctl)."""
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
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from storage import results  # noqa: E402
from tests.test_library import make_result  # noqa: E402


def load_usb():
    """system/ is mocked by tests/__init__.py: load the real module by path."""
    spec = importlib.util.spec_from_file_location("usb_under_test", os.path.join(ROOT, "system", "usb.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OID = "20261027-202750-21d2de"


class UpdateObjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        folder = make_result(self.root, OID)
        (folder / "object.json").write_text(json.dumps({
            "title": "Old", "description": "", "dimensions_mm": {"length": 71.0, "width": 54.3, "height": 226.9},
            "volume_cm3": 363.74, "job_id": "j", "faces_high": 598224}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_sent_fields_change(self):
        entry = results.update_object(self.root, OID, {"title": "Vase", "dimensions_mm": {"height": 230.0},
                                                       "volume_cm3": 400.0})
        self.assertEqual(entry.object["title"], "Vase")
        self.assertEqual(entry.object["dimensions_mm"], {"length": 71.0, "width": 54.3, "height": 230.0})
        self.assertEqual(entry.object["volume_cm3"], 400.0)
        self.assertEqual((entry.object["job_id"], entry.object["faces_high"]), ("j", 598224))
        self.assertFalse((self.root / OID / "object.json.tmp").exists())

    def test_computed_fields_cannot_be_edited(self):
        with self.assertRaises(ValueError):
            results.update_object(self.root, OID, {"faces_high": 1})

    def test_unknown_object(self):
        with self.assertRaises(results.ResultNotFoundError):
            results.update_object(self.root, "20261027-202750-000000", {"title": "x"})


class CopyPhotosTest(unittest.TestCase):
    def test_photos_are_kept_beside_the_model_and_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "input_images" / "rig"
            for cam in ("0", "1"):
                (images / cam).mkdir(parents=True)
                (images / cam / "f_1000.jpg").write_bytes(b"jpg")
            (images / "manifest.json").write_text("{}")
            folder = make_result(root / "results", OID)
            self.assertEqual(results.copy_photos(images, folder), 3)
            self.assertTrue((folder / "photos" / "rig" / "0" / "f_1000.jpg").is_file())
            self.assertTrue((folder / "photos" / "rig" / "1" / "f_1000.jpg").is_file())
            self.assertTrue((folder / "photos" / "rig" / "manifest.json").is_file())
            names = [n for _, n in load_usb().export_files(folder)]
            self.assertFalse(any("/photos/" in n for n in names))
            self.assertIsNotNone(results.get_result(root / "results", OID))   # still a valid library entry


LSBLK = {"blockdevices": [
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "tran": None, "type": "disk", "fstype": None,
     "children": [{"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "type": "part", "fstype": "ext4",
                   "mountpoints": ["/"]}]},
    {"name": "sda", "path": "/dev/sda", "tran": "usb", "type": "disk", "fstype": None,
     "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "vfat", "label": "STICK",
                   "size": 1000, "mountpoints": [None]}]},
]}


class UsbExportTest(unittest.TestCase):
    def setUp(self):
        self.usb = load_usb()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stick = self.root / "stick"
        self.stick.mkdir()
        self.folder = make_result(self.root / "results", OID)
        for extra in ("obj/high/high_model.obj", "3mf/low_model.3mf", "logs/x.log", "output.zip"):
            (self.folder / extra).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / extra).write_bytes(b"data")
        self.mounted = False
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, cmd):
        self.calls.append(cmd[:2])
        if cmd[0] == "lsblk":
            data = json.loads(json.dumps(LSBLK))
            data["blockdevices"][1]["children"][0]["mountpoints"] = [str(self.stick) if self.mounted else None]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), "")
        if cmd[:2] == ["udisksctl", "mount"]:
            self.mounted = True
        if cmd[:2] == ["udisksctl", "unmount"]:
            self.mounted = False
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def wait(self):
        for _ in range(200):
            status = self.usb.export_status()
            if status["state"] not in self.usb.RUNNING_STATES:
                return status
            time.sleep(0.02)
        self.fail("export did not finish")

    def test_only_usb_partitions_are_listed(self):
        with patch.object(self.usb, "_run", self.fake_run):
            drives = self.usb.list_drives()
        self.assertEqual([(d["device"], d["label"], d["mountpoint"]) for d in drives], [("/dev/sda1", "STICK", None)])

    def test_eject_unmounts_and_powers_off_the_disk(self):
        self.mounted = True
        with patch.object(self.usb, "_run", self.fake_run):
            result = self.usb.eject()
        self.assertEqual(result, {"device": "/dev/sda1", "disk": "/dev/sda", "powered_off": True})
        self.assertFalse(self.mounted)
        self.assertIn(["udisksctl", "unmount"], self.calls)
        self.assertEqual(self.calls[-1], ["udisksctl", "power-off"])

    def test_eject_is_refused_during_an_export(self):
        self.usb._status.update(state="archiving", object_id=OID)
        self.addCleanup(self.usb._status.update, state="idle", object_id=None)
        with self.assertRaises(self.usb.UsbBusyError):
            self.usb.eject()

    def test_no_stick_is_an_error(self):
        empty = {"blockdevices": [LSBLK["blockdevices"][0]]}
        with patch.object(self.usb, "_run",
                          lambda cmd: subprocess.CompletedProcess(cmd, 0, json.dumps(empty), "")):
            with self.assertRaises(self.usb.UsbNotFoundError):
                self.usb.start_export(self.folder)

    def test_export_mounts_writes_every_format_and_unmounts(self):
        with patch.object(self.usb, "_run", self.fake_run):
            started = self.usb.start_export(self.folder)
            self.assertFalse(started["safe_to_remove"])
            status = self.wait()
        self.assertEqual(status["state"], "done", status)
        self.assertTrue(status["safe_to_remove"])
        self.assertFalse(self.mounted)
        archive = self.stick / f"{OID}.zip"
        names = set(zipfile.ZipFile(archive).namelist())
        self.assertIn(f"{OID}/object.json", names)
        self.assertIn(f"{OID}/glb/low_model.glb", names)
        self.assertIn(f"{OID}/stl/high_model.stl", names)
        self.assertIn(f"{OID}/obj/high/high_model.obj", names)
        self.assertIn(f"{OID}/3mf/low_model.3mf", names)
        self.assertFalse(any("logs/" in n or n.endswith("output.zip") for n in names))
        self.assertEqual(sorted(p.name for p in self.stick.iterdir()), [f"{OID}.zip"])

    def test_a_second_export_replaces_the_archive(self):
        with patch.object(self.usb, "_run", self.fake_run):
            self.usb.start_export(self.folder)
            self.wait()
            self.usb.start_export(self.folder)
            self.wait()
        self.assertEqual(sorted(p.name for p in self.stick.iterdir()), [f"{OID}.zip"])

    def test_not_enough_space_is_an_error_and_the_stick_is_released(self):
        with patch.object(self.usb, "_run", self.fake_run), \
                patch.object(self.usb.shutil, "disk_usage", return_value=unittest.mock.Mock(free=1)):
            self.usb.start_export(self.folder)
            status = self.wait()
        self.assertEqual(status["state"], "error")
        self.assertIn("space", status["error"])
        self.assertTrue(status["safe_to_remove"])
        self.assertEqual(list(self.stick.iterdir()), [])


API_PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from fastapi import FastAPI
from fastapi.testclient import TestClient
import router.library as library
from system import usb

app = FastAPI()
app.include_router(library.router)
client = TestClient(app)
oid = "20261027-202750-21d2de"
usb.list_drives = lambda: []
def r(resp):
    return {"status": resp.status_code, "body": resp.json()}
print(json.dumps({
    "patch": r(client.patch(f"/library/objects/{oid}", json={"title": "Vase", "dimensions_mm": {"width": 50}})),
    "patch_bad_field": client.patch(f"/library/objects/{oid}", json={"job_id": "x"}).status_code,
    "patch_bad_value": client.patch(f"/library/objects/{oid}", json={"volume_cm3": -1}).status_code,
    "patch_missing": client.patch("/library/objects/20261027-202750-000000", json={"title": "x"}).status_code,
    "export_no_stick": r(client.post(f"/library/objects/{oid}/export/usb")),
    "export_missing": client.post("/library/objects/20261027-202750-000000/export/usb").status_code,
    "status": r(client.get("/library/export/usb/status")),
    "eject_no_stick": client.post("/library/usb/eject").status_code,
}))
"""


class LibraryEditApiTest(unittest.TestCase):
    def test_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_result(Path(tmp), OID, title="Old")
            env = {**os.environ, "RESULTS_DIR": tmp}
            out = subprocess.run([sys.executable, "-c", API_PROGRAM % ROOT], capture_output=True, text=True, env=env,
                                 timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        res = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(res["patch"]["status"], 200)
        self.assertEqual(res["patch"]["body"]["object"]["title"], "Vase")
        self.assertEqual(res["patch"]["body"]["object"]["dimensions_mm"], {"width": 50.0})
        self.assertEqual(res["patch_bad_field"], 422)
        self.assertEqual(res["patch_bad_value"], 422)
        self.assertEqual(res["patch_missing"], 404)
        self.assertEqual(res["export_no_stick"]["status"], 503)
        self.assertIn("No USB stick", res["export_no_stick"]["body"]["detail"])
        self.assertEqual(res["export_missing"], 404)
        self.assertEqual(res["status"]["body"]["state"], "idle")
        self.assertEqual(res["eject_no_stick"], 503)


if __name__ == "__main__":
    unittest.main()
