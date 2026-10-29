"""
R-4 / API-21: the Arduino bridge when the USB serial link drops or was never there.

  * a SerialException while the port is being reconfigured (the timeout setter) reaches the reconnect;
  * after the reconnect an idempotent command (PING, CONFIG, ENABLE, DISABLE) is retried once, a move never;
  * restoring the timeout on a dead port does not raise;
  * send_stop() that cannot write the 0xFF resets the Arduino by reopening the port;
  * ensure_connected() reconnects a bridge that started without a port and answers with a PING.

The bridge module is loaded by path with a fake `serial` (as in test_ws_b_motor_bridge.py); no hardware.
"""
import threading
import unittest

from tests.test_ws_b_motor_bridge import _FakeSerial, _load_bridge_module, _real_bridge


class _UnpluggedSerial(_FakeSerial):
    """A port whose USB device disappeared: reconfiguring it (setting the timeout) and writing raise."""

    def __init__(self, exc_type):
        self._exc_type = None     # the base constructor sets the timeout: let that one through
        super().__init__()
        self._exc_type = exc_type

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        if self._exc_type is None:
            self._timeout = value
            return
        raise self._exc_type("Could not configure port: (5, 'Input/output error')")

    def write(self, data):
        raise self._exc_type("write failed: device disconnected")


class BridgeReconnectTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bridge_module()
        self.SerialException = self.mod.serial.SerialException
        self.dead = _UnpluggedSerial(self.SerialException)
        self.bridge = _real_bridge(self.mod, self.dead)
        self.fresh = _FakeSerial()
        self.connects = 0

        def fake_connect():
            self.connects += 1
            self.bridge.serial = self.fresh
            self.bridge.connection_epoch += 1

        self.bridge._connect = fake_connect

    def test_a_failing_timeout_setter_reconnects_and_retries_an_idempotent_command_once(self):
        reply = self.bridge.send_command("3 1", timeout=2.0)          # ENABLE motor 1
        self.assertEqual(reply, "0")
        self.assertEqual(self.connects, 1)
        self.assertEqual(self.fresh.written, [b"3 1\n"])               # sent again, once, on the new port
        self.assertEqual(self.bridge.connection_epoch, 1)

    def test_a_move_is_not_repeated_after_the_reconnect(self):
        reply = self.bridge.send_command("5 1 1 200 1000 1000 0 0 0", timeout=2.0)
        self.assertEqual(reply, str(self.mod.ArduinoStatus.ERR_ABORTED.value))
        self.assertEqual(self.connects, 1)
        self.assertEqual(self.fresh.written, [])

    def test_the_reconnect_is_attempted_only_once_per_command(self):
        def connect_still_dead():
            self.connects += 1
            self.bridge.serial = _UnpluggedSerial(self.SerialException)

        self.bridge._connect = connect_still_dead
        reply = self.bridge.send_command("4 0")
        self.assertEqual(reply, str(self.mod.ArduinoStatus.ERR_ABORTED.value))
        self.assertEqual(self.connects, 2)   # the failed command, then its single retry

    def test_send_stop_on_a_dead_port_resets_the_arduino_by_reopening_it(self):
        reopened = []
        self.bridge._reset_by_reopen = lambda: reopened.append(True)
        self.bridge.send_stop(repeat=2)
        self.assertEqual(reopened, [True])
        self.assertEqual(self.bridge.stop_epoch, 1)


class EnsureConnectedTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bridge_module()

    def test_a_bridge_without_a_port_reconnects_and_pings(self):
        bridge = _real_bridge(self.mod, None)
        port = _FakeSerial()

        def connect():
            bridge.serial = port

        bridge._connect = connect
        self.assertFalse(bridge.is_connected)
        self.assertTrue(bridge.ensure_connected())
        self.assertEqual(port.written, [b"1\n"])   # PING

    def test_no_arduino_at_all_is_false_and_does_not_raise(self):
        bridge = _real_bridge(self.mod, None)
        bridge._connect = lambda: None
        self.assertFalse(bridge.ensure_connected())

    def test_a_wrong_answer_to_the_ping_is_false(self):
        bridge = _real_bridge(self.mod, _FakeSerial(reply=b"1\n"))
        self.assertFalse(bridge.ensure_connected())

    def test_a_transaction_in_flight_counts_as_alive_and_sends_nothing(self):
        port = _FakeSerial()
        bridge = _real_bridge(self.mod, port)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with bridge._response_lock:
                held.set()
                release.wait(5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        held.wait(5)
        try:
            self.assertTrue(bridge.ensure_connected())
            self.assertEqual(port.written, [])
        finally:
            release.set()
            t.join(5)

    def test_config_commands_are_remembered_while_disconnected(self):
        bridge = _real_bridge(self.mod, None)
        with self.assertRaises(RuntimeError):
            bridge.send_command("2 0 6 7 8")
        self.assertEqual(bridge._configs, ["2 0 6 7 8"])   # replayed by _connect once the Arduino is there


if __name__ == "__main__":
    unittest.main()
