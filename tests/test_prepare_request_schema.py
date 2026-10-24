import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Real pydantic in a subprocess (tests/__init__.py replaces it with a mock in this process)
PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import DualCameraConfig, PrepareRequest, StartRequest, ZMoveRequest, TurntableRotateRequest, StreamActiveRequest

def focus(value):
    try:
        return {"ok": True, "value": DualCameraConfig(focus=value).focus}
    except Exception:
        return {"ok": False, "value": None}

def accepted(**kwargs):
    try:
        DualCameraConfig(**kwargs)
        return True
    except Exception:
        return False

def z_start(value):
    try:
        return PrepareRequest(z_start_mm=value).z_start_mm
    except Exception:
        return "refused"

def example_of(model):
    schema = model.model_json_schema()
    example = (schema.get("examples") or [None])[0]
    result = {"has_examples": example is not None, "has_example": "example" in schema}
    if example is not None:
        model(**example)                     # the example is a valid body: "Execute" without editing works
        result["example"] = example
    return result

from fastapi import FastAPI
_app = FastAPI()
@_app.post("/prepare")
def _prepare(req: PrepareRequest):
    return 1
_openapi = _app.openapi()["components"]["schemas"]["PrepareRequest"]

out = {
    "swagger": {
        "prepare": example_of(PrepareRequest), "start": example_of(StartRequest), "z_move": example_of(ZMoveRequest),
        "rotate": example_of(TurntableRotateRequest), "stream_active": example_of(StreamActiveRequest),
        "in_openapi": _openapi.get("examples") == [PrepareRequest().model_dump(mode="json", exclude_none=True)],
    },
    "default_z_start": PrepareRequest().z_start_mm,
    "z_start": {k: z_start(v) for k, v in {
        "zero": 0, "ten": 10, "max": 100, "above_max": 100.01, "negative": -1, "half_step": 10.005,
        "two_decimals": 12.34, "nan": float("nan"),
    }.items()},
    "settle_time": {k: accepted(settle_time=v) for k, v in {"zero": 0, "two": 2, "ten": 10, "above": 10.01, "huge": 1e9, "negative": -1}.items()},
    "default_copy_colour_gains": DualCameraConfig().copy_colour_gains,
    "copy_colour_gains_true": DualCameraConfig(copy_colour_gains=True).copy_colour_gains,
    "z_start_not_in_camera_config": "z_start_mm" in PrepareRequest().camera_config.model_dump(),
    "default_focus": DualCameraConfig().focus,
    "focus": {k: focus(v) for k, v in {
        "int": 300, "low": -5, "high": 5000, "zero": 0, "max": 1023,
        "pair": [10, 20], "pair_tuple": (30, 40), "pair_clamped": [-1, 2000],
        "null": None, "null_pair": [None, None], "auto": "auto",
        "word": "near", "one": [1], "three": [1, 2, 3], "empty": [], "half_null": [300, None], "bool": True,
        "whole_float": 300.0, "fraction": 300.5,
    }.items()},
    "prepare_without_camera_config": PrepareRequest().camera_config.model_dump(),
    "prepare_only_led": PrepareRequest(illumination_brightness=0.5).camera_config.focus,
    "partial_camera_config": PrepareRequest(camera_config={"quality": 80}).camera_config.model_dump(),
    "small_photo_default_preview": PrepareRequest(
        camera_config={"photo_resolution": [2028, 1520]}).camera_config.preview_resolution,
    "small_photo_big_preview": accepted(photo_resolution=[2028, 1520], preview_resolution=[3000, 2000]),
    "photo_640": accepted(photo_resolution=[640, 480]),
    "photo_as_text": accepted(photo_resolution="4056x3040"),
    "photo_swapped": accepted(photo_resolution=[3040, 4056]),
    "photo_tuple_literal_matches_defaults": list(__import__("hardware.camera.config", fromlist=["x"]).SUPPORTED_PHOTO_RESOLUTIONS),
    "photo_odd": accepted(photo_resolution=[641, 480]),
    "photo_tiny": accepted(photo_resolution=[100, 100]),
    "photo_too_big": accepted(photo_resolution=[5000, 3040]),
    "photo_full": accepted(photo_resolution=[4056, 3040]),
    "photo_half": accepted(photo_resolution=[2028, 1520]),
    "led_zero": None,
}
try:
    PrepareRequest(illumination_brightness=0.0)
    out["led_zero"] = True
except Exception:
    out["led_zero"] = False
print(json.dumps(out))
""" % ROOT


class PrepareRequestSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])
        cls.focus = cls.r["focus"]

    def test_swagger_example_is_the_defaults_and_a_valid_body(self):
        sw = self.r["swagger"]
        for name in ("prepare", "start", "z_move", "rotate"):
            self.assertTrue(sw[name]["has_examples"] and sw[name]["has_example"], name)
        self.assertTrue(sw["in_openapi"])
        cam = sw["prepare"]["example"]["camera_config"]
        # defaults of the schema are shown; the fields that default to null are left out, not shown as 0
        self.assertEqual((cam["quality"], cam["focus"], cam["photo_resolution"], cam["settle_time"]), (95, "auto", [4056, 3040], 2.0))
        self.assertEqual(sw["prepare"]["example"]["z_start_mm"], 30.0)
        for null_default in ("exposure_time", "analogue_gain", "colour_gains", "brightness", "contrast", "saturation", "sharpness"):
            self.assertNotIn(null_default, cam)
        self.assertEqual(sw["z_move"]["example"], {"acceleration": False, "delay": 0.001, "direction": "up", "distance_mm": 100.0, "ramp_percent": 0.2})
        turntable = sw["start"]["example"]["mechanical"]["turntable"]
        self.assertEqual((turntable["angle"], turntable["end_delay"]), (18.0, 0.015))
        self.assertNotIn("start_delay", turntable)

    def test_a_body_with_a_required_field_gets_no_generated_example(self):
        self.assertFalse(self.r["swagger"]["stream_active"]["has_examples"])

    def test_settle_time_is_between_0_and_10_seconds(self):
        st = self.r["settle_time"]
        self.assertTrue(st["zero"] and st["two"] and st["ten"])
        for name in ("above", "huge", "negative"):
            self.assertFalse(st[name], name)

    def test_z_start_defaults_to_30_mm_and_is_not_a_camera_setting(self):
        self.assertEqual(self.r["default_z_start"], 30.0)
        self.assertFalse(self.r["z_start_not_in_camera_config"])

    def test_z_start_accepts_0_to_100_mm_in_whole_steps(self):
        zs = self.r["z_start"]
        self.assertEqual((zs["zero"], zs["ten"], zs["max"], zs["two_decimals"]), (0, 10, 100, 12.34))

    def test_z_start_outside_the_range_or_not_a_whole_number_of_steps_is_refused(self):
        for name in ("above_max", "negative", "half_step", "nan"):
            self.assertEqual(self.r["z_start"][name], "refused", name)

    def test_colour_gains_are_not_copied_to_camera_2_unless_asked(self):
        self.assertIs(self.r["default_copy_colour_gains"], False)
        self.assertIs(self.r["copy_colour_gains_true"], True)

    def test_focus_defaults_to_auto(self):
        self.assertEqual(self.r["default_focus"], "auto")
        self.assertEqual(self.r["prepare_only_led"], "auto")

    def test_integer_focus_is_kept_and_clamped_to_the_vcm_range(self):
        self.assertEqual(self.focus["int"]["value"], 300)
        self.assertEqual(self.focus["low"]["value"], 0)
        self.assertEqual(self.focus["high"]["value"], 1023)
        self.assertEqual(self.focus["zero"]["value"], 0)
        self.assertEqual(self.focus["max"]["value"], 1023)
        self.assertEqual(self.focus["whole_float"]["value"], 300)

    def test_a_pair_is_two_clamped_integers(self):
        self.assertEqual(self.focus["pair"]["value"], [10, 20])
        self.assertEqual(self.focus["pair_tuple"]["value"], [30, 40])
        self.assertEqual(self.focus["pair_clamped"]["value"], [0, 1023])

    def test_pairs_of_the_wrong_length_or_with_holes_are_refused(self):
        for name in ("one", "three", "empty", "half_null"):
            self.assertFalse(self.focus[name]["ok"], name)

    def test_null_and_a_null_pair_mean_the_saved_positions(self):
        self.assertTrue(self.focus["null"]["ok"])
        self.assertIsNone(self.focus["null"]["value"])
        self.assertTrue(self.focus["null_pair"]["ok"])
        self.assertIsNone(self.focus["null_pair"]["value"])

    def test_auto_is_accepted_other_words_booleans_and_fractions_are_not(self):
        self.assertEqual(self.focus["auto"]["value"], "auto")
        for name in ("word", "bool", "fraction"):
            self.assertFalse(self.focus[name]["ok"], name)

    def test_camera_config_is_optional_and_partial_blocks_get_the_defaults(self):
        defaults = self.r["prepare_without_camera_config"]
        self.assertEqual(defaults["quality"], 95)
        self.assertEqual(defaults["photo_resolution"], [4056, 3040])
        self.assertEqual(defaults["focus"], "auto")
        self.assertEqual(defaults["awb_mode"], "auto")
        self.assertIsNone(defaults["exposure_time"])
        partial = self.r["partial_camera_config"]
        self.assertEqual(partial["quality"], 80)
        self.assertEqual(partial["photo_resolution"], [4056, 3040])

    def test_photo_resolution_is_one_of_the_two_supported_sizes(self):
        self.assertTrue(self.r["photo_full"])
        self.assertTrue(self.r["photo_half"])
        for name in ("photo_odd", "photo_tiny", "photo_too_big", "photo_640", "photo_as_text", "photo_swapped"):
            self.assertFalse(self.r[name], name)

    def test_the_schema_accepts_exactly_the_sizes_the_sync_driver_supports(self):
        self.assertEqual(self.r["photo_tuple_literal_matches_defaults"], [[4056, 3040], [2028, 1520]])

    def test_the_default_preview_is_kept_for_the_binned_photo_size_and_a_bigger_one_is_refused(self):
        self.assertEqual(self.r["small_photo_default_preview"], [960, 720])
        self.assertFalse(self.r["small_photo_big_preview"])

    def test_led_brightness_zero_is_still_refused(self):
        self.assertFalse(self.r["led_zero"])


if __name__ == "__main__":
    unittest.main()
