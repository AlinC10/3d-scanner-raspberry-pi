import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from storage import results  # noqa: E402  (no pydantic / FastAPI: loads under the test mocks)


def make_result(root: Path, result_id: str, state: str = "ok", title: str = "Obj", obj=True,
                models=((".glb", "low"), (".glb", "high"), (".stl", "low"), (".stl", "high"))) -> Path:
    folder = root / result_id
    folder.mkdir(parents=True)
    (folder / "status.json").write_text(json.dumps({"state": state}))
    if obj is True:
        (folder / "object.json").write_text(json.dumps({"title": title}))
    elif obj is not None:
        (folder / "object.json").write_text(obj)
    for fmt, lod in models:
        sub = folder / fmt.lstrip(".")
        sub.mkdir(exist_ok=True)
        (sub / f"{lod}_model{fmt}").write_bytes(f"{result_id} {lod}{fmt}".encode())
    return folder


class ResultsStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_finished_results_newest_first_and_skips_the_others(self):
        make_result(self.root, "20261027-202750-21d2de", title="old")
        make_result(self.root, "20261028-101010-aaaaaa", title="new", models=((".glb", "low"),))
        make_result(self.root, "20261029-000000-bbbbbb", state="running")
        make_result(self.root, "20261029-000001-cccccc", obj="{not json")
        make_result(self.root, "20261029-000002-dddddd", obj=None)
        make_result(self.root, "not-an-id", title="ignored")
        (self.root / "20261029-000003-eeeeee").write_text("a file, not a folder")

        page = results.list_results(self.root)
        self.assertEqual([e.id for e in page.items], ["20261028-101010-aaaaaa", "20261027-202750-21d2de"])
        self.assertEqual(page.total, 2)
        self.assertEqual(page.items[0].models, {".glb": ["low"]})
        self.assertEqual(page.items[1].models, {".glb": ["low", "high"], ".stl": ["low", "high"]})
        self.assertEqual(page.items[1].object, {"title": "old"})
        self.assertIsNone(page.next_offset)

    def test_pagination(self):
        for i in range(5):
            make_result(self.root, f"2026102{i}-000000-00000{i}")
        first = results.list_results(self.root, offset=0, limit=2)
        self.assertEqual([e.id[:8] for e in first.items], ["20261024", "20261023"])
        self.assertEqual(first.next_offset, 2)
        last = results.list_results(self.root, offset=4, limit=2)
        self.assertEqual(len(last.items), 1)
        self.assertIsNone(last.next_offset)
        beyond = results.list_results(self.root, offset=10, limit=2)
        self.assertEqual((beyond.items, beyond.total, beyond.next_offset), ([], 5, None))

    def test_missing_results_folder_is_an_empty_list(self):
        page = results.list_results(self.root / "missing")
        self.assertEqual((page.items, page.total), ([], 0))

    def test_model_path_and_errors(self):
        make_result(self.root, "20261027-202750-21d2de", models=((".glb", "low"),))
        path = results.model_path(self.root, "20261027-202750-21d2de", ".glb", "low")
        self.assertEqual(path.name, "low_model.glb")
        for args in [
            ("20261027-202750-21d2de", ".glb", "high"),     # model not on disk
            ("20261027-202750-21d2de", ".obj", "low"),      # format not served
            ("20261027-202750-21d2de", ".glb", "mid"),
            ("20261027-202750-000000", ".glb", "low"),      # no such object
            ("../20261027-202750-21d2de", ".glb", "low"),   # traversal
            ("..", ".glb", "low"),
            ("20261027-202750-21d2de/../x", ".glb", "low"),
        ]:
            with self.subTest(args=args), self.assertRaises(results.ResultNotFoundError):
                results.model_path(self.root, *args)

    def test_get_result_refuses_an_unfinished_one(self):
        make_result(self.root, "20261029-000000-bbbbbb", state="failed")
        with self.assertRaises(results.ResultNotFoundError):
            results.get_result(self.root, "20261029-000000-bbbbbb")


class CategoryAndFilterTest(unittest.TestCase):
    """LIB-CAT / LIB-FILTER: the category of an object and the filters applied before the page is cut."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_result(self.root, "20261020-000000-000001", obj=json.dumps({"title": "Mug", "description": "blue cup"}))
        make_result(self.root, "20261021-000000-000002",
                    obj=json.dumps({"title": "Ana", "mode": "mediapipe_face_mesh"}))
        make_result(self.root, "20261022-000000-000003", obj=json.dumps({"title": "Bob", "category": "face"}))
        make_result(self.root, "20261023-000000-000004", obj=json.dumps({"title": "Screwdriver", "category": "x"}))
        face_status = self.root / "20261024-000000-000005"
        make_result(self.root, face_status.name, title="Old face")
        (face_status / "status.json").write_text(json.dumps({"state": "ok", "mode": "mediapipe_face_mesh"}))

    def tearDown(self):
        self._tmp.cleanup()

    def ids(self, **kwargs):
        return [e.id[-1] for e in results.list_results(self.root, **kwargs).items]

    def test_category_is_read_or_inferred(self):
        categories = {e.id[-1]: e.category for e in results.list_results(self.root).items}
        self.assertEqual(categories, {"1": "object", "2": "face", "3": "face", "4": "object", "5": "face"})
        self.assertEqual(results.get_result(self.root, "20261021-000000-000002").as_dict()["category"], "face")

    def test_category_filter(self):
        self.assertEqual(self.ids(category="face"), ["5", "3", "2"])
        self.assertEqual(self.ids(category="object"), ["4", "1"])
        self.assertEqual(self.ids(category="all"), ["5", "4", "3", "2", "1"])

    def test_search_in_title_description_and_id_any_case(self):
        self.assertEqual(self.ids(search="MUG"), ["1"])
        self.assertEqual(self.ids(search="cup"), ["1"])
        self.assertEqual(self.ids(search="-000004"), ["4"])
        self.assertEqual(self.ids(search="   "), ["5", "4", "3", "2", "1"])
        self.assertEqual(self.ids(search="o", category="face"), ["5", "3"])

    def test_filter_is_applied_before_the_page(self):
        page = results.list_results(self.root, offset=1, limit=1, category="face")
        self.assertEqual(page.total, 3)
        self.assertEqual([e.id[-1] for e in page.items], ["3"])
        self.assertEqual(page.next_offset, 2)

    def test_set_category_writes_object_json_and_keeps_the_rest(self):
        oid = "20261020-000000-000001"
        self.assertTrue(results.set_category(self.root, oid, "object"))
        obj = json.loads((self.root / oid / "object.json").read_text())
        self.assertEqual(obj, {"title": "Mug", "description": "blue cup", "category": "object"})
        self.assertFalse(results.set_category(self.root, oid, "car"))
        self.assertFalse(results.set_category(self.root, "20261020-000000-999999", "object"))


# Real FastAPI and pydantic in a subprocess (tests/__init__.py replaces them with mocks in this process)
API_PROGRAM = r"""
import json, os, sys
sys.path.insert(0, %r)
from fastapi import FastAPI
from fastapi.testclient import TestClient
import router.library as library
from schemas.library import ModelRequest

app = FastAPI()
app.include_router(library.router)
client = TestClient(app)

def fmt(value):
    try:
        return ModelRequest(format=value).format
    except Exception:
        return "refused"

def call(url):
    r = client.get(url)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.content.decode()
    return {"status": r.status_code, "body": body, "type": r.headers.get("content-type")}

oid = "20261027-202750-21d2de"
print(json.dumps({
    "formats": {v: fmt(v) for v in ["stl", "STL", ".stl", "glb", " .GLB ", "gltf", ".gltf", "obj", "", ".3mf"]},
    "list": call("/library/objects?limit=1"),
    "list_bad_limit": call("/library/objects?limit=101")["status"],
    "list_bad_offset": call("/library/objects?offset=-1")["status"],
    "list_face": call("/library/objects?category=face"),
    "list_search": call("/library/objects?search=PARK&category=object"),
    "list_bad_category": call("/library/objects?category=car")["status"],
    "one": call(f"/library/objects/{oid}"),
    "missing": call("/library/objects/20261027-202750-000000")["status"],
    "model": call(f"/library/objects/{oid}/model?format=stl&lod=high"),
    "model_default": call(f"/library/objects/{oid}/model"),
    "model_bad_format": call(f"/library/objects/{oid}/model?format=obj")["status"],
    "model_bad_lod": call(f"/library/objects/{oid}/model?lod=mid")["status"],
    "model_traversal": call("/library/objects/..%%2F..%%2Fetc/model")["status"],
}))
"""


class LibraryApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        make_result(root, "20261027-202750-21d2de", title="Parkside")
        make_result(root, "20261026-000000-000001")
        make_result(root, "20261025-000000-000002", obj=json.dumps({"title": "Ana", "category": "face"}))
        env = {**os.environ, "RESULTS_DIR": str(root)}
        out = subprocess.run([sys.executable, "-c", API_PROGRAM % ROOT], capture_output=True, text=True, env=env,
                             timeout=120)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.r = json.loads(out.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_format_is_normalized_and_limited(self):
        self.assertEqual(self.r["formats"], {
            "stl": ".stl", "STL": ".stl", ".stl": ".stl", "glb": ".glb", " .GLB ": ".glb", "gltf": ".glb",
            ".gltf": ".glb", "obj": "refused", "": "refused", ".3mf": "refused",
        })

    def test_list_pages_without_model_files(self):
        listed = self.r["list"]
        self.assertEqual(listed["status"], 200)
        self.assertEqual(listed["body"]["total"], 3)
        self.assertEqual(listed["body"]["next_offset"], 1)
        item = listed["body"]["items"][0]
        self.assertEqual(item["id"], "20261027-202750-21d2de")
        self.assertEqual(item["object"], {"title": "Parkside"})
        self.assertEqual(item["category"], "object")
        self.assertEqual(item["models"], {".glb": ["low", "high"], ".stl": ["low", "high"]})
        self.assertEqual(self.r["list_bad_limit"], 422)
        self.assertEqual(self.r["list_bad_offset"], 422)

    def test_list_filters(self):
        face = self.r["list_face"]["body"]
        self.assertEqual((face["total"], [i["id"] for i in face["items"]]), (1, ["20261025-000000-000002"]))
        self.assertEqual(face["items"][0]["category"], "face")
        found = self.r["list_search"]["body"]
        self.assertEqual((found["total"], [i["id"] for i in found["items"]]), (1, ["20261027-202750-21d2de"]))
        self.assertEqual(self.r["list_bad_category"], 422)

    def test_one_object(self):
        self.assertEqual(self.r["one"]["status"], 200)
        self.assertEqual(self.r["one"]["body"]["id"], "20261027-202750-21d2de")
        self.assertEqual(self.r["missing"], 404)

    def test_model_file(self):
        self.assertEqual(self.r["model"]["status"], 200)
        self.assertEqual(self.r["model"]["type"], "model/stl")
        self.assertEqual(self.r["model"]["body"], "20261027-202750-21d2de high.stl")
        self.assertEqual(self.r["model_default"]["type"], "model/gltf-binary")
        self.assertEqual(self.r["model_default"]["body"], "20261027-202750-21d2de low.glb")
        self.assertEqual(self.r["model_bad_format"], 422)
        self.assertEqual(self.r["model_bad_lod"], 422)
        self.assertIn(self.r["model_traversal"], (404, 422))


if __name__ == "__main__":
    unittest.main()
