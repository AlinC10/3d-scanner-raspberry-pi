"""
The development app (main_dev.py: the real routers on top of the simulated `ScannerMock`) and the preview endpoints
shared by /scanner and /test. Runs in a subprocess with the real FastAPI: tests/__init__.py replaces it with mocks in
this process.
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PROGRAM = r"""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, %r)
os.environ["MOCK_HARDWARE"] = "1"
os.environ["GROQ_API_KEY"] = ""      # the AI assistant stays off: the server must start anyway
from hardware.mock_hardware import MockHardware
MockHardware.apply()
import main_dev                       # applies the mocks and builds the app
from fastapi.testclient import TestClient
import router.scanner
import router.test

router.test.TEST_SNAPS_DIR = tempfile.mkdtemp()      # the snap endpoint must not write into the project

app = main_dev.app
out = {}

# one implementation behind both prefixes (the routers' own route lists: the app wraps them)
routes = {}
for module_router in (router.scanner.router, router.test.router):
    for r in module_router.routes:
        routes[(r.path, tuple(sorted(r.methods)))] = r.endpoint
out["shared"] = {}
for suffix, method in (("mjpeg", "GET"), ("active", "POST"), ("quality", "POST"), ("sync-status", "GET")):
    a = routes.get(("/scanner/stream/" + suffix, (method,)))
    b = routes.get(("/test/stream/" + suffix, (method,)))
    out["shared"][suffix] = a is not None and a is b
out["openapi_ok"] = "/test/stream/mjpeg" in app.openapi()["paths"]

def wait_for(client, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/scanner/status").json()
        if st["state"] in states:
            return st
        time.sleep(0.1)
    return client.get("/scanner/status").json()

with TestClient(app) as client:
    out["status_idle"] = client.get("/scanner/status").json()["state"]

    # the AI assistant is off without a key, and says so; the server still runs
    import router.ai
    router.ai.service.wait_until_loaded(10)
    out["ai_status"] = client.get("/api/ai/status").json()
    chat = client.post("/api/ai/chat", json={"question": "hi"})
    out["ai_chat"] = [chat.status_code, "GROQ_API_KEY" in chat.json().get("detail", "")]

    # no CORS headers (local only), Swagger still at /docs, the AI documents at /ai-docs
    cors = client.get("/scanner/status", headers={"Origin": "http://evil.example"})
    out["cors_header"] = cors.headers.get("access-control-allow-origin")
    out["swagger"] = [client.get("/docs").status_code, "swagger" in client.get("/docs").text.lower()]
    out["ai_docs"] = client.get("/ai-docs/user_manual.md").status_code

    # preview refused before anything is started: the same message on both prefixes
    a = client.get("/scanner/stream/mjpeg")
    b = client.get("/test/stream/mjpeg")
    out["not_streaming"] = [a.status_code, b.status_code, a.json() == b.json()]
    out["active_without_cameras"] = client.post("/test/stream/active", params={"camera_id": 2}).status_code

    # prepare -> prepared
    prep = client.post("/scanner/prepare", json={"z_start_mm": 20})
    out["prepare"] = [prep.status_code, prep.json().get("stream_urls")]
    out["prepared_state"] = wait_for(client, ("prepared", "error"))["state"]
    out["z_after_prepare"] = router.test.scanner.z_mm

    # preview: first frame, camera switch, quality; query and JSON body
    # (the test client would wait for the end of an endless stream: take the first chunk from the response object)
    resp = router.scanner.scanner_stream_mjpeg(camera=2)
    chunk = asyncio.run(resp.body_iterator.__anext__())
    out["mjpeg"] = [resp.media_type, b"--FRAME" in chunk, b"image/jpeg" in chunk]
    out["active_query"] = client.post("/test/stream/active", params={"camera_id": 1}).json()["active_camera"]
    out["active_body"] = client.post("/scanner/stream/active", json={"camera_id": 2}).json()["active_camera"]
    out["active_none"] = client.post("/scanner/stream/active").status_code
    out["active_bad"] = client.post("/scanner/stream/active", params={"camera_id": 3}).status_code
    out["quality_query"] = client.post("/scanner/stream/quality", params={"quality": 40}).json()["quality"]
    out["quality_body"] = client.post("/test/stream/quality", json={"quality": 70}).json()["quality"]
    out["quality_none"] = client.post("/test/stream/quality").status_code
    out["sync"] = [client.get("/scanner/stream/sync-status").json()["ready"], client.get("/test/stream/sync-status").json()["ready"]]

    # the manual /test endpoints are refused while the scan owns the rig, the sensors answer
    out["lights_during_scan"] = client.post("/test/lights/enable").status_code
    out["tof"] = client.get("/test/tof").json()["valid"]
    out["endstops"] = client.get("/test/endstops").json()
    out["buzzer_status"] = client.get("/test/buzzer").json()
    out["buzzer_play"] = client.post("/test/buzzer/play", json={"pattern": "success"}).status_code
    out["buzzer_bad_pattern"] = client.post("/test/buzzer/play", json={"pattern": "siren"}).status_code
    out["buzzer_tone"] = client.post("/test/buzzer/tone").json()
    out["buzzer_bad_tone"] = client.post("/test/buzzer/tone", json={"frequency_hz": 5000}).status_code
    out["buzzer_played"] = [p if isinstance(p, str) else list(p) for p in router.scanner.scanner.buzzer.played]
    from hardware.buzzer import PATTERNS
    from router.test import BuzzerPattern
    import typing
    out["buzzer_patterns_match"] = sorted(PATTERNS) == sorted(typing.get_args(BuzzerPattern))
    router.scanner.scanner.buzzer.available = False
    out["buzzer_missing"] = [client.post("/test/buzzer/play", json={"pattern": "ready"}).status_code,
                             client.get("/test/buzzer").json()["available"]]
    router.scanner.scanner.buzzer.available = True

    # scan -> completed
    start = client.post("/scanner/start", json={"job_id": None, "cloud": {"launch_runpod": False}})
    out["start"] = start.status_code
    final = wait_for(client, ("completed", "error", "cancelled"), timeout=60)
    out["final"] = [final["state"], final["photos_captured"] > 0, final["photos_uploaded"] == final["photos_captured"], final["phase"]]

    # idle again: the manual endpoints work
    out["lights_after_scan"] = client.post("/test/lights/enable", json={"brightness": 0.5}).status_code
    out["lights_off"] = client.post("/test/lights/disable").status_code
    out["motor_status"] = client.get("/test/motor/status").json()
    out["z_move"] = client.post("/test/motor/z-move", json={"direction": "up", "distance_mm": 10}).status_code
    out["z_move_bad_steps"] = client.post("/test/motor/z-move", json={"direction": "up", "distance_mm": 10.005}).status_code
    out["rotate"] = client.post("/test/turntable/rotate", json={"angle": 18}).status_code
    out["home"] = client.post("/test/motor/home-bottom").json()["bottom_endstop"]
    out["to_top"] = client.post("/test/motor/move-to-top").json()["top_endstop"]
    snap = client.post("/test/camera/snap")
    out["snap"] = [snap.status_code, snap.json().get("count"), all(os.path.exists(p) for p in snap.json().get("photo_paths", []))]
    out["cleanup"] = client.post("/test/cleanup").status_code

    # a second run can be cancelled
    client.post("/scanner/prepare", json={})
    wait_for(client, ("prepared", "error"))
    out["cancel"] = [client.post("/scanner/cancel").json()["state"], client.get("/scanner/status").json()["state"]]
    out["prepare_again"] = client.post("/scanner/prepare", json={}).status_code
    wait_for(client, ("prepared", "error"))
    client.post("/scanner/cancel")

    # without the Arduino: every motor request is a 503 that names it, the rest of the server works
    router.scanner.scanner.arduino_available = False
    prep = client.post("/scanner/prepare", json={})
    out["no_arduino"] = [
        prep.status_code, "Arduino" in prep.json().get("detail", ""),
        client.post("/test/motor/z-move", json={"direction": "up", "distance_mm": 10}).status_code,
        client.post("/test/turntable/rotate", json={"angle": 18}).status_code,
        client.post("/scanner/face/capture", json={"manual_z": 240.0}).status_code,
        client.get("/scanner/status").json()["state"],
        client.post("/test/cleanup").status_code,
        client.get("/test/endstops").status_code,
    ]
    router.scanner.scanner.arduino_available = True
    out["arduino_back"] = client.post("/test/motor/z-move", json={"direction": "up", "distance_mm": 10}).status_code

print(json.dumps(out))
""" % ROOT


class DevAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=180)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-3000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_preview_endpoints_have_one_implementation_under_both_prefixes(self):
        self.assertEqual(self.r["shared"], {"mjpeg": True, "active": True, "quality": True, "sync-status": True})
        self.assertTrue(self.r["openapi_ok"])

    def test_the_preview_is_refused_the_same_way_before_the_cameras_run(self):
        self.assertEqual(self.r["not_streaming"], [400, 400, True])
        self.assertEqual(self.r["active_without_cameras"], 400)

    def test_prepare_returns_the_scanner_stream_urls_and_reaches_prepared(self):
        status, urls = self.r["prepare"]
        self.assertEqual(status, 202)
        self.assertEqual(urls, ["/scanner/stream/mjpeg?camera=1", "/scanner/stream/mjpeg?camera=2"])
        self.assertEqual(self.r["prepared_state"], "prepared")
        self.assertEqual(self.r["z_after_prepare"], 20.0)   # z_start_mm of the request

    def test_the_preview_serves_frames_and_takes_the_camera_and_quality_from_query_or_body(self):
        self.assertEqual(self.r["mjpeg"], ["multipart/x-mixed-replace; boundary=FRAME", True, True])
        self.assertEqual((self.r["active_query"], self.r["active_body"]), (1, 2))
        self.assertEqual(self.r["active_none"], 400)
        self.assertEqual(self.r["active_bad"], 422)
        self.assertEqual((self.r["quality_query"], self.r["quality_body"]), (40, 70))
        self.assertEqual(self.r["quality_none"], 400)
        self.assertEqual(self.r["sync"], [True, True])

    def test_buzzer_endpoints(self):
        self.assertEqual(self.r["buzzer_status"],
                         {"available": True, "pin": None, "patterns": ["ready", "start", "hold_still", "photo_countdown", "success", "error"]})
        self.assertEqual(self.r["buzzer_play"], 200)
        self.assertEqual(self.r["buzzer_bad_pattern"], 422)
        self.assertEqual(self.r["buzzer_tone"], {"status": "success", "frequency_hz": 880.0, "duration_s": 0.5})
        self.assertEqual(self.r["buzzer_bad_tone"], 422)
        self.assertEqual(self.r["buzzer_played"], ["success", ["tone", 880.0, 0.5]])
        self.assertTrue(self.r["buzzer_patterns_match"])
        self.assertEqual(self.r["buzzer_missing"], [503, False])

    def test_the_manual_light_endpoint_is_refused_while_the_scan_owns_the_rig(self):
        self.assertEqual(self.r["lights_during_scan"], 409)

    def test_a_simulated_scan_runs_to_completed(self):
        self.assertEqual(self.r["start"], 202)
        self.assertEqual(self.r["final"], ["completed", True, True, "done"])

    def test_the_manual_endpoints_work_against_the_simulated_rig(self):
        self.assertTrue(self.r["tof"])
        self.assertEqual(set(self.r["endstops"]), {"bottom_endstop", "top_endstop"})
        self.assertEqual(self.r["lights_after_scan"], 200)
        self.assertEqual(self.r["lights_off"], 200)
        self.assertEqual(self.r["motor_status"], {"turntable": "disabled", "z_axis": "disabled"})
        self.assertEqual(self.r["z_move"], 200)
        self.assertEqual(self.r["z_move_bad_steps"], 422)
        self.assertEqual(self.r["rotate"], 200)
        self.assertIs(self.r["home"], True)
        self.assertIs(self.r["to_top"], True)
        self.assertEqual(self.r["snap"], [200, 2, True])
        self.assertEqual(self.r["cleanup"], 200)

    def test_the_server_starts_with_the_ai_assistant_off_and_reports_why(self):
        st = self.r["ai_status"]
        self.assertEqual((st["on"], st["loading"]), (False, False))
        self.assertIn("GROQ_API_KEY", st["reason"])
        self.assertEqual(self.r["ai_chat"], [503, True])

    def test_no_cors_headers_and_the_docs_paths_do_not_collide(self):
        self.assertIsNone(self.r["cors_header"])
        self.assertEqual(self.r["swagger"], [200, True])
        self.assertEqual(self.r["ai_docs"], 200)

    def test_without_the_arduino_motor_requests_are_503_and_the_rest_works(self):
        self.assertEqual(self.r["no_arduino"], [503, True, 503, 503, 503, "cancelled", 200, 200])
        self.assertEqual(self.r["arduino_back"], 200)

    def test_a_run_can_be_cancelled_and_prepared_again(self):
        self.assertEqual(self.r["cancel"], ["cancelled", "cancelled"])
        self.assertEqual(self.r["prepare_again"], 202)


if __name__ == "__main__":
    unittest.main()
