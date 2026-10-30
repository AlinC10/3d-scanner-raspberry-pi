"""Bluetooth export of an object (system/bluetooth_export.py) with a scripted bt-obex, and its cancel."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_library import make_result
from tests.test_library_edit_usb import load_usb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OID = "20261027-202750-21d2de"
PHONE = SimpleNamespace(mac_address="AA:BB:CC:DD:EE:FF", name="Phone")


def load_bt_export():
    """system/ is mocked by tests/__init__.py: load the real modules by path."""
    with patch.dict(sys.modules, {"system.usb": load_usb()}):
        spec = importlib.util.spec_from_file_location("bt_export_under_test",
                                                      os.path.join(ROOT, "system", "bluetooth_export.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeObex:
    """Stands in for `bt-obex -p`: finishes at once (`code`) or hangs until killed (`hang`)."""

    def __init__(self, code=0, hang=False):
        self.code, self.hang, self.cmds = code, hang, []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        args = ["sleep 30"] if self.hang else [f"exit {self.code}"]
        return subprocess.Popen(["sh", "-c", args[0]], **kwargs)


class BluetoothExportTest(unittest.TestCase):
    def setUp(self):
        self.bt = load_bt_export()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = make_result(Path(self.tmp.name), OID)

    def tearDown(self):
        self.bt.cancel()
        self.tmp.cleanup()

    def wait(self, states=("archiving", "sending")):
        for _ in range(300):
            status = self.bt.export_status()
            if status["state"] not in states:
                return status
            time.sleep(0.02)
        self.fail("transfer did not finish")

    def start(self, obex):
        original = self.bt._send
        with patch.object(self.bt, "_send", lambda *a: original(*a, popen=obex)):
            return self.bt.start_export(self.folder, [PHONE])

    def test_no_connected_device_is_an_error(self):
        with self.assertRaises(self.bt.NoConnectedDeviceError):
            self.bt.start_export(self.folder, [])
        with self.assertRaises(self.bt.NoConnectedDeviceError):
            self.bt.start_export(self.folder, [PHONE], mac="11:22:33:44:55:66")

    def test_the_archive_is_pushed_to_the_connected_device(self):
        obex = FakeObex()
        self.start(obex)
        status = self.wait()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(obex.cmds[0][:3], ["bt-obex", "-p", "AA:BB:CC:DD:EE:FF"])
        self.assertTrue(obex.cmds[0][3].endswith(f"{OID}.zip"))

    def test_a_refused_transfer_is_an_error(self):
        self.start(FakeObex(code=1))
        self.assertEqual(self.wait()["state"], "error")

    def test_disconnect_cancels_the_running_transfer(self):
        self.start(FakeObex(hang=True))
        self.wait(states=("archiving",))
        with self.assertRaises(self.bt.BluetoothBusyError):
            self.bt.start_export(self.folder, [PHONE])
        self.assertFalse(self.bt.cancel("11:22:33:44:55:66"))   # another device: untouched
        self.assertTrue(self.bt.cancel("aa:bb:cc:dd:ee:ff"))
        self.assertEqual(self.wait()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
