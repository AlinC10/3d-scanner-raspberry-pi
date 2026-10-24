import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The real `system.bluetooth` in a subprocess (tests/__init__.py replaces the `system` package with a mock in this one)
PROGRAM = r"""
import json, subprocess, sys
sys.path.insert(0, %r)
from unittest.mock import patch
from subprocess import CompletedProcess, TimeoutExpired
from system import bluetooth as bt

def ok(stdout=""):
    return CompletedProcess([], 0, stdout=stdout, stderr="")

def run_with(func, side_effect):
    calls = []
    def fake(cmd, **kwargs):
        calls.append({"cmd": cmd, "timeout": kwargs.get("timeout")})
        result = side_effect(cmd)
        if isinstance(result, Exception):
            raise result
        return result
    error = None
    value = None
    with patch.object(bt.subprocess, "run", fake):
        try:
            value = func()
        except Exception as e:
            error = type(e).__name__
    return {"calls": calls, "error": error, "value": value}

def hang(cmd):
    return TimeoutExpired(cmd, 1)

out = {
    "power_on": run_with(bt.power_on, lambda cmd: ok("Changing power on succeeded")),
    "power_off": run_with(bt.power_off, lambda cmd: ok("Changing power off succeeded")),
    "status": run_with(bt.get_power_status, lambda cmd: ok("Powered: yes")),
    "pair": run_with(lambda: bt.pair_device("AA:BB:CC:DD:EE:FF"), lambda cmd: ok()),
    "connect": run_with(lambda: bt.connect_device("AA:BB:CC:DD:EE:FF"), lambda cmd: ok()),
    "power_on_hangs": run_with(bt.power_on, hang),
    "pair_hangs": run_with(lambda: bt.pair_device("AA:BB:CC:DD:EE:FF"), hang),
    "connect_hangs": run_with(lambda: bt.connect_device("AA:BB:CC:DD:EE:FF"), hang),
    "devices_hangs": run_with(lambda: bt.scan_devices(duration=5), hang),
    "scan": run_with(
        lambda: [d.mac_address for d in bt.scan_devices(duration=5)],
        lambda cmd: ok("Device AA:BB:CC:DD:EE:FF Phone") if cmd[1] == "devices" else ok("Icon: phone\nPaired: yes"),
    ),
    # discovery overruns and `info` hangs, `devices` answers: the device is still listed
    "scan_discovery_and_info_hang": run_with(
        lambda: [d.mac_address for d in bt.scan_devices(duration=5)],
        lambda cmd: ok("Device AA:BB:CC:DD:EE:FF Phone") if cmd[1] == "devices" else hang(cmd),
    ),
    "constants": [bt.BLUETOOTHCTL_TIMEOUT_S, bt.PAIR_TIMEOUT_S, bt.CONNECT_TIMEOUT_S, bt.SCAN_GRACE_S],
}
print(json.dumps(out))
""" % ROOT


class BluetoothTimeoutsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-2000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])
        cls.short, cls.pair, cls.connect, cls.grace = cls.r["constants"]

    def timeouts(self, name):
        return [c["timeout"] for c in self.r[name]["calls"]]

    def test_every_simple_command_has_the_short_deadline(self):
        for name in ("power_on", "power_off", "status"):
            self.assertEqual(self.timeouts(name), [self.short], name)

    def test_pairing_and_connecting_get_a_longer_deadline_and_trust_the_short_one(self):
        self.assertEqual(self.timeouts("pair"), [self.pair, self.short])
        self.assertEqual(self.timeouts("connect"), [self.connect])
        self.assertGreater(self.pair, self.short)
        self.assertGreater(self.connect, self.short)

    def test_discovery_deadline_is_the_duration_plus_a_grace_period(self):
        scan = self.r["scan"]["calls"]
        self.assertEqual(scan[0]["cmd"], ["bluetoothctl", "--timeout", "5", "scan", "on"])
        self.assertEqual(scan[0]["timeout"], 5 + self.grace)
        self.assertEqual([c["timeout"] for c in scan[1:]], [self.short] * (len(scan) - 1))
        self.assertEqual(self.r["scan"]["value"], ["AA:BB:CC:DD:EE:FF"])

    def test_a_command_that_hangs_becomes_a_bluetooth_error(self):
        for name in ("power_on_hangs", "pair_hangs", "connect_hangs", "devices_hangs"):
            self.assertEqual(self.r[name]["error"], "BluetoothConnectionError", name)

    def test_a_hanging_pair_stops_before_trusting_the_device(self):
        self.assertEqual(len(self.r["pair_hangs"]["calls"]), 1)

    def test_an_overrun_discovery_or_a_hanging_info_does_not_lose_the_device_list(self):
        result = self.r["scan_discovery_and_info_hang"]
        self.assertIsNone(result["error"])
        self.assertEqual(result["value"], ["AA:BB:CC:DD:EE:FF"])


if __name__ == "__main__":
    unittest.main()
