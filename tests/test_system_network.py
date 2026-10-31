import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The real `system` package in a subprocess (tests/__init__.py replaces it with a mock in this process). Every
# nmcli / bluetoothctl call goes through a scripted fake of subprocess.run.
PROGRAM = r"""
import json, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, %r)
from unittest.mock import patch
from subprocess import CompletedProcess, TimeoutExpired
from system import nm, wifi, ethernet, bluetooth, status, network_state
from schemas.system.network import NetworkCredentials

calls = []

def ok(out="", code=0, err=""):
    return CompletedProcess([], code, stdout=out, stderr=err)

class Rig:
    # The fake system: profiles, devices, and what nmcli answers
    def __init__(self):
        self.profiles = {"Home": "Home"}            # profile name -> ssid
        self.psk = {"Home": "old-secret"}
        self.accept = {"Home": "old-secret", "Cafe": "latte"}
        self.devices = "eth0:ethernet:connected:Wired connection 1\nusb0:ethernet:disconnected:\nwlan0:wifi:connected:Home\n"
        self.bt_scan = "[NEW] Device AA:AA:AA:AA:AA:01 Phone\n[CHG] Device AA:AA:AA:AA:AA:02 RSSI: -70\n[NEW] Device AA:AA:AA:AA:AA:03 AA-AA-AA-AA-AA-03\n"
        self.bt_devices = ("Device AA:AA:AA:AA:AA:01 Phone\nDevice AA:AA:AA:AA:AA:02 Laptop\n"
                           "Device AA:AA:AA:AA:AA:03 AA-AA-AA-AA-AA-03\nDevice AA:AA:AA:AA:AA:04 OldHeadset\n"
                           "Device AA:AA:AA:AA:AA:05 Speaker\n")
        self.bt_info = {"AA:AA:AA:AA:AA:01": "Icon: phone\nRSSI: 0xffffffc4 (-60)\nPaired: no",
                        "AA:AA:AA:AA:AA:02": "Icon: computer\nPaired: yes",
                        "AA:AA:AA:AA:AA:04": "Icon: audio-headset\nPaired: yes",
                        "AA:AA:AA:AA:AA:05": "Icon: audio-card\nPaired: yes\nConnected: yes"}

    def __call__(self, cmd, **kw):
        calls.append({"cmd": cmd, "timeout": kw.get("timeout"), "lc": (kw.get("env") or {}).get("LC_ALL")})
        a = cmd[1:]
        if cmd[0] == "bluetoothctl":
            if a[:1] == ["--timeout"] and "scan" in a: return ok(self.bt_scan)
            if a == ["devices"]: return ok(self.bt_devices)
            if a == ["devices", "Connected"]: return ok("Device AA:AA:AA:AA:AA:05 Speaker\n")
            if a[0] == "info": return ok(self.bt_info.get(a[1], ""))
            if a == ["show"]: return ok("Powered: yes")
            return ok()
        if a[:4] == ["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection"]:
            return ok("".join(f"{n}:802-11-wireless:yes\n" for n in self.profiles) + "Wired connection 1:802-3-ethernet:yes\n")
        if a[:3] == ["-t", "-f", "NAME"]:
            return ok("".join(f"{n}\n" for n in self.profiles))
        if a[:2] == ["-g", "802-11-wireless.ssid"]: return ok(self.profiles.get(a[-1], "") + "\n")
        if a[:2] == ["-s", "-g"]: return ok(self.psk.get(a[-1], "") + "\n")
        if a[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY"]:
            return ok("*:Home:70:WPA2\n:My\\:Net:40:WPA2\n:My\\:Net:65:WPA2\n::90:WPA2\n:Cafe:50:WPA2\n")
        if a[:3] == ["-t", "-f", "ACTIVE,SSID,SIGNAL"]: return ok("no:Cafe:50\nyes:Home:70\n")
        if a[:3] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]: return ok(self.devices)
        if a[:2] == ["-g", "IP4.ADDRESS"]: return ok("192.168.1.20/24 | 10.0.0.2/8\n")
        if a == ["radio", "wifi"]: return ok("enabled\n")
        if a[:2] == ["connection", "modify"]:
            name = a[3]
            if "wifi-sec.psk" in a: self.psk[name] = a[a.index("wifi-sec.psk") + 1]
            if "802-11-wireless-security.psk" in a: self.psk[name] = a[a.index("802-11-wireless-security.psk") + 1]
            return ok()
        if a[:2] == ["connection", "delete"]:
            self.profiles.pop(a[-1], None); return ok()
        if "connection" in a and "up" in a:
            name = a[-1]
            if self.accept.get(self.profiles.get(name)) == self.psk.get(name): return ok("Connection successfully activated")
            return ok(code=4, err="Error: Connection activation failed: Secrets were required, but not provided.")
        if "wifi" in a and "connect" in a:
            ssid = a[a.index("connect") + 1]
            pwd = a[a.index("password") + 1] if "password" in a else ""
            self.profiles[ssid] = ssid; self.psk[ssid] = pwd
            if self.accept.get(ssid) == pwd: return ok("Device 'wlan0' successfully activated")
            return ok(code=4, err="Error: Connection activation failed: Secrets were required, but not provided.")
        return ok()

def attempt(fn):
    try:
        return {"value": fn(), "error": None, "code": None}
    except Exception as e:
        return {"value": None, "error": type(e).__name__, "code": getattr(e, "status_code", None), "msg": str(e)}

out = {}
out["split"] = [nm.split_terse("My\\:Net:80:WPA2"), nm.split_terse("a\\\\b::c")]

def raise_(e):
    def f(*a, **k): raise e
    return f
with patch.object(nm.subprocess, "run", raise_(TimeoutExpired(["nmcli"], 1))):
    out["timeout"] = attempt(lambda: wifi.get_status())
with patch.object(nm.subprocess, "run", raise_(FileNotFoundError())):
    out["missing"] = attempt(lambda: wifi.get_status())
    out["bt_missing"] = attempt(lambda: bluetooth.get_power_status())

rig = Rig()
with patch.object(subprocess, "run", rig):
    calls.clear()
    nets = wifi.scan_wifi()
    out["scan"] = {k: v.model_dump() for k, v in nets.items()}
    out["scan_rescan_flags"] = [c["cmd"][-2:] for c in calls if "IN-USE,SSID,SIGNAL,SECURITY" in c["cmd"]]
    out["scan_locale"] = {c["lc"] for c in calls if c["cmd"][0] == "nmcli"} == {"C"}
    wifi.scan_wifi(rescan=True)
    out["rescan"] = [(c["cmd"][-1], c["timeout"]) for c in calls if "IN-USE,SSID,SIGNAL,SECURITY" in c["cmd"]][-1]
    out["saved"] = [s.model_dump() for s in wifi.list_saved_networks()]

    # saved network, no password: the profile is activated, nothing created
    calls.clear()
    out["connect_saved"] = attempt(lambda: wifi.connect_wifi(NetworkCredentials(ssid="Home", password="")))
    out["connect_saved_cmds"] = [c["cmd"][1:] for c in calls if "up" in c["cmd"] or "connect" in c["cmd"]]
    # saved network, wrong new password: 401 and the old password is put back
    out["connect_saved_wrong"] = attempt(lambda: wifi.connect_wifi(NetworkCredentials(ssid="Home", password="typo")))
    out["psk_after_wrong"] = rig.psk["Home"]
    # new network, wrong password: 401 and the created profile is removed again
    out["connect_new_wrong"] = attempt(lambda: wifi.connect_wifi(NetworkCredentials(ssid="Cafe", password="tea")))
    out["profiles_after_wrong"] = sorted(rig.profiles)
    out["connect_new_ok"] = attempt(lambda: wifi.connect_wifi(NetworkCredentials(ssid="Cafe", password="latte")))
    out["forget_missing"] = attempt(lambda: wifi.delete_saved_network("Nope"))

    out["eth"] = ethernet.get_status()
    rig.devices = "usb0:ethernet:disconnected:\nwlan0:wifi:disconnected:\n"
    out["eth_usb_only"] = ethernet.get_status()["interface"]
    rig.devices = "wlan0:wifi:disconnected:\n"
    out["eth_none"] = ethernet.get_status()
    rig.devices = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:connected:Home\n"

    out["bt_default"] = [(d.name, d.rssi, d.connected) for d in bluetooth.scan_devices(duration=3)]
    out["bt_unnamed"] = [d.mac_address[-2:] for d in bluetooth.scan_devices(duration=3, include_unnamed=True)]
    out["bt_all"] = sorted(d.mac_address[-2:] for d in bluetooth.scan_devices(duration=3, include_out_of_range=True))
    out["bt_duration_capped"] = [c["cmd"][2] for c in calls if "scan" in c["cmd"]][-1:]
    bluetooth.scan_devices(duration=99)
    out["bt_duration_capped"] = [c["cmd"][2] for c in calls if "scan" in c["cmd"]][-1]

    out["status"] = status.get_network_status()
    with patch.object(status.usb, "list_drives", return_value=[{"device": "/dev/sda1", "label": "STICK"}]):
        out["status_usb"] = status.get_network_status()["usb"]
    with patch.object(status.usb, "list_drives", return_value=[]):
        out["status_no_usb"] = status.get_network_status()["usb"]["connected"]

    # one tool failing gives an "error" part, not a failed status
    def broken_bt(cmd, **kw):
        if cmd[0] == "bluetoothctl": raise FileNotFoundError()
        return rig(cmd, **kw)
    with patch.object(subprocess, "run", broken_bt):
        out["status_bt_missing"] = status.get_network_status()["bluetooth"]

    # network state: remember, then restore
    tmp = Path(tempfile.mkdtemp()) / "state" / "network.json"
    with patch.object(network_state, "NETWORK_STATE_FILE", tmp):
        network_state.update(lambda s: s["wifi"].update(enabled=True, profile="Home"), path=tmp)
        network_state.update(lambda s: s["ethernet"].update(profile="enterprise-wired"), path=tmp)
        network_state.update(lambda s: s["bluetooth"].update(powered=True, devices=["AA:AA:AA:AA:AA:05", "AA:AA:AA:AA:AA:02"]), path=tmp)
        out["state_saved"] = network_state.load(tmp)
        out["state_has_no_password"] = "secret" not in tmp.read_text()
        calls.clear()
        rig.devices = "eth0:ethernet:disconnected:\nwlan0:wifi:disconnected:\n"
        rig.profiles["enterprise-wired"] = "x"
        out["restore"] = network_state.restore(tmp)
        out["restore_cmds"] = [c["cmd"][1:] for c in calls if "up" in c["cmd"] or c["cmd"][1:2] == ["connect"]]
        out["last_restore"] = network_state.last_restore
        out["start_without_file"] = network_state.start_restore(Path(tmp.parent / "missing.json")) is None
    (tmp.parent / "junk.json").write_text("{not json")
    out["load_junk"] = network_state.load(tmp.parent / "junk.json")

print(json.dumps(out, default=str))
""" % ROOT


class SystemNetworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-3000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_terse_output_respects_the_escaping(self):
        # SYS-3: an SSID with ':' no longer breaks the scan
        self.assertEqual(self.r["split"], [["My:Net", "80", "WPA2"], ["a\\b", "", "c"]])

    def test_errors_carry_their_http_code(self):
        self.assertEqual((self.r["timeout"]["error"], self.r["timeout"]["code"]), ("CommandTimeoutError", 504))
        self.assertEqual((self.r["missing"]["error"], self.r["missing"]["code"]), ("ToolUnavailableError", 503))
        self.assertEqual(self.r["bt_missing"]["code"], 503)

    def test_wifi_scan(self):
        scan = self.r["scan"]
        self.assertEqual(sorted(scan), ["Cafe", "Home", "My:Net"])         # hidden network left out
        self.assertEqual(scan["My:Net"]["signal"], 65)                      # strongest access point of the SSID
        self.assertTrue(scan["Home"]["saved"])
        self.assertFalse(scan["Cafe"]["saved"])
        self.assertEqual(scan["Home"]["in_use"], "*")
        self.assertEqual(self.r["scan_rescan_flags"], [["--rescan", "no"]])  # no radio scan by default (SYS-21)
        self.assertTrue(self.r["scan_locale"])                               # SYS-6
        self.assertEqual(self.r["rescan"][0], "yes")
        self.assertGreater(self.r["rescan"][1], 15)
        self.assertEqual(self.r["saved"], [{"name": "Home", "ssid": "Home", "autoconnect": True}])

    def test_a_saved_network_connects_without_a_password(self):
        self.assertEqual(self.r["connect_saved"]["value"], "Home")
        self.assertEqual(len(self.r["connect_saved_cmds"]), 1)
        self.assertEqual(self.r["connect_saved_cmds"][0][-3:], ["up", "id", "Home"])

    def test_a_wrong_password_is_a_401_and_never_destroys_a_working_profile(self):
        self.assertEqual(self.r["connect_saved_wrong"]["code"], 401)
        self.assertEqual(self.r["psk_after_wrong"], "old-secret")
        self.assertEqual(self.r["connect_new_wrong"]["code"], 401)
        self.assertEqual(self.r["profiles_after_wrong"], ["Home"])          # the failed new profile was removed
        self.assertEqual(self.r["connect_new_ok"]["value"], "Cafe")
        self.assertEqual(self.r["forget_missing"]["code"], 404)

    def test_ethernet_status(self):
        eth = self.r["eth"]
        self.assertEqual(eth["interface"], "eth0")                          # not usb0
        self.assertEqual(eth["state"], "connected")
        self.assertEqual(eth["ip"], "192.168.1.20/24")
        self.assertEqual(self.r["eth_usb_only"], "usb0")
        self.assertIsNone(self.r["eth_none"]["interface"])
        self.assertEqual(self.r["eth_none"]["state"], "unavailable")

    def test_bluetooth_lists_only_named_devices_in_range(self):
        # S4: the phone (seen, hex RSSI), the laptop (seen), the connected speaker; not the old headset, not the
        # address-only device
        self.assertEqual(self.r["bt_default"], [["Phone", -60, False], ["Laptop", None, False], ["Speaker", None, True]])
        self.assertIn("03", self.r["bt_unnamed"])
        self.assertEqual(self.r["bt_all"], ["01", "02", "04", "05"])
        self.assertEqual(self.r["bt_duration_capped"], "20")

    def test_aggregated_status(self):
        s = self.r["status"]
        self.assertEqual(s["ethernet"]["ip"], "192.168.1.20/24")
        self.assertTrue(s["ethernet"]["connected"])
        self.assertEqual((s["wifi"]["ssid"], s["wifi"]["signal"], s["wifi"]["connected"]), ("Home", 70, True))
        self.assertEqual(s["wifi"]["ip"], "192.168.1.20/24")
        self.assertEqual(s["bluetooth"]["devices"], [{"mac_address": "AA:AA:AA:AA:AA:05", "name": "Speaker"}])
        self.assertIn("error", self.r["status_bt_missing"])
        self.assertEqual(self.r["status_usb"], {"connected": True, "drives": [{"device": "/dev/sda1", "label": "STICK"}],
                                                "export_state": "idle", "safe_to_remove": True})
        self.assertIs(self.r["status_no_usb"], False)

    def test_network_state_and_restore(self):
        saved = self.r["state_saved"]
        self.assertEqual(saved["wifi"], {"enabled": True, "profile": "Home"})
        self.assertTrue(self.r["state_has_no_password"])
        restore = self.r["restore"]
        self.assertEqual(restore["state"], "done")
        self.assertEqual(restore["wifi"], "connected 'Home'")
        self.assertEqual(restore["ethernet"], "connected 'enterprise-wired'")
        self.assertEqual(restore["bluetooth AA:AA:AA:AA:AA:05"], "already connected")
        self.assertEqual(restore["bluetooth AA:AA:AA:AA:AA:02"], "connected")
        self.assertEqual(self.r["last_restore"], restore)
        self.assertTrue(self.r["start_without_file"])
        self.assertEqual(self.r["load_junk"], {"wifi": {}, "ethernet": {}, "bluetooth": {}})

    def test_a_failing_restore_step_does_not_stop_the_others(self):
        # the enterprise profile is accepted here; a refused Wi-Fi password would be "failed: ..." and the rest runs
        self.assertNotIn("failed", json.dumps(self.r["restore"]))


GUARD_PROGRAM = r"""
import json, os, sys
sys.path.insert(0, %r)
os.environ["MOCK_HARDWARE"] = "1"
os.environ["GROQ_API_KEY"] = ""
from hardware.mock_hardware import MockHardware
MockHardware.apply()
import main_dev
import router.scanner
from unittest.mock import patch
from fastapi.testclient import TestClient
from system import wifi, status as system_status, nm

out = {}
with TestClient(main_dev.app) as client:
    scanner = router.scanner.scanner.get()
    with patch.object(wifi, "scan_wifi", return_value={}), \
            patch.object(system_status, "get_network_status", return_value={"wifi": {}}), \
            patch.object(wifi, "set_wifi_power"), \
            patch.object(wifi, "get_status", side_effect=nm.CommandTimeoutError("slow")):
        out["idle_scan"] = client.get("/system/wifi/scan").status_code
        scanner.state = "running"
        out["running_scan"] = client.get("/system/wifi/scan").status_code
        out["running_bt_scan"] = client.get("/system/bluetooth/scan").status_code
        out["running_power_off"] = client.post("/system/wifi/power/off").status_code
        out["running_connect"] = client.post("/system/wifi/connect", json={"ssid": "x"}).status_code
        out["running_status"] = client.get("/system/status").status_code
        scanner.state = "idle"
        out["power_post"] = client.post("/system/wifi/power/on").status_code
        out["power_get"] = client.get("/system/wifi/power/on").status_code
        out["timeout_code"] = client.get("/system/wifi/status").status_code
print(json.dumps(out))
""" % ROOT


class SystemGuardTest(unittest.TestCase):
    """S5: network scans and connection changes are refused while a scan runs; the status is not."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([sys.executable, "-c", GUARD_PROGRAM], capture_output=True, text=True, cwd=ROOT, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(done.stderr[-3000:])
        cls.r = json.loads(done.stdout.strip().splitlines()[-1])

    def test_guard(self):
        self.assertEqual(self.r["idle_scan"], 200)
        for key in ("running_scan", "running_bt_scan", "running_power_off", "running_connect"):
            self.assertEqual(self.r[key], 409, key)
        self.assertEqual(self.r["running_status"], 200)

    def test_power_accepts_post_and_get(self):
        self.assertEqual((self.r["power_post"], self.r["power_get"]), (200, 200))

    def test_the_app_maps_a_timeout_to_504(self):
        self.assertEqual(self.r["timeout_code"], 504)


if __name__ == "__main__":
    unittest.main()
