import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PROGRAM = r"""
import json, sys
sys.path.insert(0, %r)
from schemas.scanner import (TurntableRotateRequest, TurntableScanConfig, ZMoveRequest, ZAxisScanConfig,
                             MechanicalConfig)

def ok(factory):
    try:
        factory()
        return True
    except Exception:
        return False

print(json.dumps({
    "angle_18": ok(lambda: TurntableRotateRequest(angle=18.0)),
    "angle_one_step": ok(lambda: TurntableRotateRequest(angle=0.1125)),
    "angle_10": ok(lambda: TurntableRotateRequest(angle=10.0)),
    "angle_360": ok(lambda: TurntableRotateRequest(angle=360.0)),
    "z_100": ok(lambda: ZMoveRequest(distance_mm=100.0)),
    "z_12_34": ok(lambda: ZMoveRequest(distance_mm=12.34)),
    "z_33_333": ok(lambda: ZMoveRequest(distance_mm=33.333)),
    "nested_bad_angle": ok(lambda: MechanicalConfig(turntable={"angle": 10.0})),
    "defaults": ok(lambda: MechanicalConfig()),
    "scan_turntable_has_no_clockwise": "clockwise" not in TurntableScanConfig.model_fields,
    "scan_z_has_no_direction": "direction" not in ZAxisScanConfig.model_fields,
    "test_rotate_keeps_clockwise": "clockwise" in TurntableRotateRequest.model_fields,
    "test_z_keeps_direction": "direction" in ZMoveRequest.model_fields,
    "old_clients_are_tolerated": ok(lambda: MechanicalConfig(
        turntable={"angle": 18.0, "clockwise": False}, z_axis={"distance_mm": 50.0, "direction": "down"})),
}))
""" % ROOT


class TestRequestSchemasRejectPartialSteps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.result = json.loads(done.stdout.strip().splitlines()[-1])

    def test_whole_step_angles_are_accepted(self):
        self.assertTrue(self.result["angle_18"])
        self.assertTrue(self.result["angle_one_step"])
        self.assertTrue(self.result["angle_360"])

    def test_partial_step_angle_is_rejected(self):
        self.assertFalse(self.result["angle_10"])

    def test_whole_step_distances_are_accepted(self):
        self.assertTrue(self.result["z_100"])
        self.assertTrue(self.result["z_12_34"])

    def test_partial_step_distance_is_rejected(self):
        self.assertFalse(self.result["z_33_333"])

    def test_scan_config_has_no_direction_fields(self):
        self.assertTrue(self.result["scan_turntable_has_no_clockwise"])
        self.assertTrue(self.result["scan_z_has_no_direction"])

    def test_manual_test_requests_keep_their_direction_fields(self):
        self.assertTrue(self.result["test_rotate_keeps_clockwise"])
        self.assertTrue(self.result["test_z_keeps_direction"])

    def test_clients_that_still_send_direction_are_not_rejected(self):
        self.assertTrue(self.result["old_clients_are_tolerated"])

    def test_validation_applies_to_the_nested_start_request(self):
        self.assertFalse(self.result["nested_bad_angle"])
        self.assertTrue(self.result["defaults"])


if __name__ == "__main__":
    unittest.main()
