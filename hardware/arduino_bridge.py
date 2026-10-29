import serial
import serial.tools.list_ports
import threading
import time
import logging
from enum import IntEnum
from typing import Optional

log = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Protocol Mapping Enums
# These map exactly to the C++ enums defined in hardware/arduino_control/lib/Protocol/Protocol.h
# -------------------------------------------------------------------------
class ArduinoCommand(IntEnum):
    PING            = 0x01
    CONFIG          = 0x02
    ENABLE          = 0x03
    DISABLE         = 0x04
    ROTATE          = 0x05
    STOP            = 0x06
    # Future features (reserved)
    ACCEL_ROTATE    = 0x10
    SET_ACCEL       = 0x11
    GET_STATUS      = 0x20
    # Universal out-of-band immediate abort
    EMERGENCY_STOP  = 0xFF

class ArduinoStatus(IntEnum):
    OK                 = 0  # Success / target reached / board ready
    ERR_UNKNOWN_CMD    = 1  # Opcode or command unrecognized
    ERR_INVALID_ARG    = 2  # Missing or invalid arguments
    ERR_NOT_CONFIGURED = 3  # Motor not initialized via CONFIG
    ERR_ABORTED        = 4  # Aborted mid-motion by emergency stop
    ERR_BUSY           = 5  # Controller busy

class ArduinoBridge:
    """
    Hardware Abstraction Layer for the Arduino Uno.
    
    This class manages the lifecycle and thread-safety of the physical USB serial port.
    Because both the Turntable Motor and Z-Axis Motor must communicate over the *same* 
    physical USB cable, this class is implemented as a Singleton.
    """
    _instance: Optional["ArduinoBridge"] = None
    _singleton_lock = threading.Lock()
    # Class-level default so that instances created without __init__ (tests) still have the counter.
    stop_epoch = 0

    def __init__(self, port: Optional[str] = None, baudrate: int = 115200):
        """
        Initializes the ArduinoBridge instance with lock mechanisms and attempts to connect.

        :param port: The target serial port, e.g. '/dev/ttyACM0'. If None, auto-discovery is used.
        :type port: Optional[str]
        :param baudrate: The baud rate for the serial connection.
        :type baudrate: int
        """
        self.port = port
        self.baudrate = baudrate
        self.serial: Optional[serial.Serial] = None
        self._configs = []
        # Incremented on every (re)connection: opening the port resets the Arduino, so every motor driver is
        # disabled again and any state cached by the callers (Motor.is_enabled) is stale.
        self.connection_epoch = 0
        # Incremented on every universal stop (send_stop): a 0xFF received while the Arduino is idle disables ALL
        # drivers, so the "energized" state cached by every Motor sharing this bridge is stale after it.
        self.stop_epoch = 0
        
        # We use a two-lock architecture to allow emergency stops to preempt active commands.
        
        # 1. Write Lock: Prevents two threads from interleaving bytes during a write.
        self._write_lock = threading.RLock()
        
        # 2. Response (Transaction) Lock: Ensures a strict Request->Response transaction.
        # If Thread A sends a ROTATE command, no other thread can send a normal command 
        # until Thread A receives its '0' or '4' response. This prevents Thread B from 
        # stealing Thread A's response buffer.
        self._response_lock = threading.RLock()
        
        self._connect()
        
        # Guarantee hardware safe-state on unexpected script death
        import atexit
        atexit.register(self.close)

    @classmethod
    def get_instance(cls, port: Optional[str] = None, baudrate: int = 115200) -> "ArduinoBridge":
        """
        Singleton accessor. Ensures that even if scanner.py instantiates two Motor objects,
        they both share the exact same ArduinoBridge and serial port connection, avoiding 
        `OSError: [Errno 16] Device or resource busy`.

        :param port: The target serial port, e.g. '/dev/ttyACM0'. If None, auto-discovery is used.
        :type port: Optional[str]
        :param baudrate: The baud rate for the serial connection.
        :type baudrate: int
        :return: The singleton instance of the ArduinoBridge.
        :rtype: ArduinoBridge
        """
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls(port=port, baudrate=baudrate)
            return cls._instance

    def _connect(self):
        """
        Handles OS port discovery and the critical Arduino DTR bootloader reset handshake.
        """
        target_port = self.port
        if not target_port:
            # Auto-discovery: Search for known Arduino or generic serial chip USB descriptors.
            # This is safer than hardcoding '/dev/ttyACM0' which can drift to ACM1 upon replugging.
            ports = list(serial.tools.list_ports.comports())
            # 1. Try to match specific known names
            search_names = ["ARDUINO", "CH340", "CH341", "CP210", "FTDI", "USB-SERIAL", "USB", "SERIAL", "UART"]
            
            for p in ports:
                # p[1] is the OS-provided hardware description field.
                description = (p.description or p[1] or "").upper()
                hwid = (p.hwid or "").upper()
                for name in search_names:
                    if name in description or name in hwid:
                        target_port = p.device
                        break
                if target_port:
                    break
            
            # 2. Fallback: just find any ttyUSB or ttyACM connected
            if not target_port:
                for p in ports:
                    if "ttyUSB" in p.device or "ttyACM" in p.device:
                        target_port = p.device
                        break
                        
            # 3. Absolute fallback
            target_port = target_port or "/dev/ttyACM0"

        log.info(f"[ArduinoBridge] Connecting to {target_port} at {self.baudrate} baud...")
        
        # IMPORTANT: Opening the serial port on an Arduino Uno triggers the DTR pin, 
        # causing a hard hardware reset. The Arduino will spend ~1.5s in the Optiboot bootloader.
        try:
            if self.serial and self.serial.is_open:
                self.serial.close()
            self.serial = serial.Serial(target_port, self.baudrate, timeout=3.0)
            self.connection_epoch += 1
        except serial.SerialException as e:
            log.error(f"[ArduinoBridge] Failed to open port {target_port}: {e}")
            return

        # Event-driven bootloader handshake: 
        # We must wait for the C++ setup() function to execute `Serial.println(STATUS_OK);`.
        # If we send commands before receiving this '0', the bootloader will simply swallow them.
        start_time = time.time()
        ready_received = False
        while time.time() - start_time < 3.0:
            if self.serial.in_waiting > 0:
                line = self.serial.readline().decode("utf-8", errors="ignore").strip()
                if line == str(ArduinoStatus.OK.value) or "READY" in line:
                    ready_received = True
                    log.info(f"[ArduinoBridge] Arduino booted and ready: '{line}'")
                    break
            time.sleep(0.05)

        if not ready_received:
            try:
                self.send_command(f"{ArduinoCommand.PING.value}")
            except Exception:
                pass

        # Auto-heal: Restore any motor configuration states
        if hasattr(self, '_configs') and self._configs:
            log.info("[ArduinoBridge] Auto-restoring motor configurations after connection...")
            for cfg in self._configs:
                try:
                    self.send_command(cfg, timeout=2.0)
                except Exception:
                    pass

    def send_command(self, cmd: str, timeout: Optional[float] = 30.0, _after_replay: bool = False) -> str:
        """
        Sends a standard command and waits for the response synchronously.
        
        :param cmd: The ASCII command payload (e.g., "5 1 200 1 2000").
        :type cmd: str
        :param timeout: Dynamically adjusted timeout based on physical motor movement duration. A 100-step move needs <1s timeout, while a 25,000-step homing move needs a 50s timeout.
        :type timeout: Optional[float]
        :return: The status code or response returned by the Arduino.
        :rtype: str
        """
        clean_cmd = cmd.strip() + "\n"
        is_config = clean_cmd.startswith(str(ArduinoCommand.CONFIG.value) + " ")
        # Moves are the only commands that can still be running on the Arduino after a timeout
        is_move = clean_cmd.split()[0] in ("5", "16", "r", "R", "a", "A")

        # Intercept and store CONFIG commands for auto-healing (also while disconnected: they are replayed on connect)
        if is_config:
            cmd_no_nl = cmd.strip()
            if cmd_no_nl not in self._configs:
                self._configs.append(cmd_no_nl)

        if not self.serial:
            raise RuntimeError("Arduino is not connected. Cannot send command.")

        # Acquire transaction lock so no other normal command can interleave
        with self._response_lock:
            old_timeout = None
            try:
                # Temporarily mutate the underlying pyserial timeout for this specific transaction. Inside the try:
                # setting it reconfigures the port, which is the first thing to fail when the USB device is gone
                # (R-4), and that failure must reach the reconnect below.
                old_timeout = self.serial.timeout
                if timeout is not None:
                    self.serial.timeout = timeout

                # Whatever is still in the input buffer is stale (a late answer of a timed-out command, the boot
                # banner of an Arduino that reset on its own): drop it, otherwise it would be taken for the answer
                # to this command and every later one would be off by one.
                self.serial.reset_input_buffer()

                # Acquire write lock just for the physical bytes transfer
                with self._write_lock:
                    self.serial.write(clean_cmd.encode("utf-8"))
                    self.serial.flush()

                # Block until the Arduino executes the command and returns its Status Code (e.g., '0' or '4')
                line = self._read_status_line()
                if not line:
                    log.error(f"[ArduinoBridge] No answer to '{cmd.strip()}' within {timeout}s. Resynchronising.")
                    self._resync_after_timeout(was_move=is_move)
                elif (line == str(ArduinoStatus.ERR_NOT_CONFIGURED.value) and not is_config
                      and not _after_replay and self._configs):
                    # The Arduino rebooted on its own (brownout) and forgot the motor pins. The command was refused
                    # before doing anything, so it is safe to configure the motors again and run it once more.
                    log.warning("[ArduinoBridge] Arduino reports 'not configured' (it was reset?). Restoring motor "
                                "configuration and retrying once.")
                    self.connection_epoch += 1  # every driver is disabled after a reset
                    for cfg in list(self._configs):
                        self.send_command(cfg, timeout=2.0, _after_replay=True)
                    return self.send_command(cmd, timeout=timeout, _after_replay=True)
                return line
            except serial.SerialException as e:
                log.error(f"[ArduinoBridge] Serial connection lost during send_command: {e}. Attempting to reconnect...")
                self._connect()
                # Reopening the port resets the Arduino (every driver disabled, configuration replayed by _connect).
                # A command that does the same thing twice is retried once; a move is never repeated blindly.
                if (not _after_replay and self._is_idempotent(clean_cmd)
                        and self.serial is not None and self.serial.is_open):
                    log.warning(f"[ArduinoBridge] Retrying '{cmd.strip()}' once after the reconnect.")
                    return self.send_command(cmd, timeout=timeout, _after_replay=True)
                return str(ArduinoStatus.ERR_ABORTED.value) # Pretend it aborted so caller can handle safely
            finally:
                # Restore the default timeout for the next command (the port may be gone or replaced meanwhile)
                if old_timeout is not None:
                    try:
                        if self.serial and self.serial.is_open:
                            self.serial.timeout = old_timeout
                    except (serial.SerialException, OSError, ValueError) as e:
                        log.warning(f"[ArduinoBridge] Could not restore the serial timeout: {e}")

    @staticmethod
    def _is_idempotent(clean_cmd: str) -> bool:
        """PING, CONFIG, ENABLE and DISABLE leave the Arduino in the same state when sent twice."""
        opcode = clean_cmd.split()[0] if clean_cmd.split() else ""
        return opcode in {str(c.value) for c in (ArduinoCommand.PING, ArduinoCommand.CONFIG,
                                                  ArduinoCommand.ENABLE, ArduinoCommand.DISABLE)}

    @property
    def is_connected(self) -> bool:
        """True while the serial port is open (says nothing about the Arduino answering: see `ensure_connected`)."""
        return self.serial is not None and bool(getattr(self.serial, "is_open", False))

    def ensure_connected(self) -> bool:
        """
        Checks that the Arduino answers, reconnecting first when the port is closed or was never opened (the server
        may have started without the Arduino, API-21). A reconnect resets the board and replays the motor
        configuration.

        A command already in flight (a move of another thread) proves the link is alive: then nothing is sent.

        :return: True when the Arduino answered a PING (or a transaction is in flight), False otherwise. Never raises.
        """
        if not self._response_lock.acquire(blocking=False):
            return self.is_connected   # another thread is mid-transaction: do not queue a PING behind a long move
        try:
            if not self.is_connected:
                self._connect()
                if not self.is_connected:
                    return False
            try:
                reply = self.send_command(f"{ArduinoCommand.PING.value}", timeout=2.0)
            except Exception as e:
                log.warning(f"[ArduinoBridge] PING failed: {e}")
                return False
            return reply == str(ArduinoStatus.OK.value)
        finally:
            self._response_lock.release()

    def _reset_by_reopen(self) -> None:
        """
        Closes and reopens the port without the boot handshake. Opening the port toggles DTR, which resets the Arduino
        and stops whatever it was stepping: the last resort when a 0xFF cannot be written.
        """
        try:
            target = self.serial.port if self.serial is not None else self.port
            if self.serial is not None:
                try:
                    self.serial.close()
                except Exception:
                    pass
            self.serial = serial.Serial(target, self.baudrate, timeout=3.0)
            self.connection_epoch += 1
            log.warning(f"[ArduinoBridge] Port {target} reopened: the Arduino was reset to stop the motors.")
        except Exception as e:
            log.error(f"[ArduinoBridge] Could not reopen the serial port to reset the Arduino: {e}")

    def _read_status_line(self, max_lines: int = 5) -> str:
        """
        Reads the numeric status line of the current transaction. Non-numeric lines (debug output, garbage after a
        reset) are skipped. Returns '' on timeout.
        """
        for _ in range(max_lines):
            line = self.serial.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                return ""
            if line.isdigit():
                return line
            if "READY" in line.upper():
                # The boot banner in the middle of a transaction: the board reset and the command was lost
                self.connection_epoch += 1
                log.error("[ArduinoBridge] Arduino rebooted while a command was in flight; the command was lost.")
                return str(ArduinoStatus.ERR_ABORTED.value)
            log.warning(f"[ArduinoBridge] Ignoring unexpected line from Arduino: {line!r}")
        return ""

    def _resync_after_timeout(self, was_move: bool = True) -> None:
        """
        After a timeout the Arduino may still be stepping and will answer later. For a move, abort it (0xFF),
        swallow that late answer and empty the buffer so the next command starts from a clean stream.
        For any other command nothing can be running, and a 0xFF sent to an idle Arduino would cut the torque of
        every motor, so only the buffer is emptied.
        """
        try:
            if was_move:
                with self._write_lock:
                    # If the move had just finished, this 0xFF reaches an idle Arduino and disables every driver
                    self.stop_epoch += 1
                    self.serial.write(bytes([ArduinoCommand.EMERGENCY_STOP.value]))
                    self.serial.flush()
                self.serial.timeout = 0.5
                self.serial.readline()  # '4' if the move was still running, nothing if it had just finished
            self.serial.reset_input_buffer()
        except Exception as e:
            log.warning(f"[ArduinoBridge] Resync after timeout failed: {e}")

    def send_stop(self, repeat: int = 1, gap_s: float = 0.02):
        """
        Out-of-band immediate universal EMERGENCY STOP.
        
        Notice that this method ONLY takes `_write_lock`, and intentionally BYPASSES `_response_lock`.
        If Thread A is currently blocked inside `send_command()` waiting for a 30-second rotation 
        to finish, Thread B can call `send_stop()`. Thread B will inject the `0xFF` byte directly 
        onto the wire. 
        
        The C++ firmware checks for `0xFF` in between every single microsecond step. When it sees it,
        the Arduino aborts the rotation, releases coil torque, and replies '4' (ERR_ABORTED). 
        Thread A then wakes up, receives '4', and the system halts cleanly.

        A `0xFF` that arrives while the Arduino is idle disables ALL motors (and gets no reply), so every call
        increments `stop_epoch`; `Motor.is_enabled` compares it to know that its cached state is stale.

        :param repeat: How many 0xFF bytes to write (at least 1). Repeats go out back to back, without waiting for
                       any reply, so the second byte catches a move that starts right after the first abort.
        :param gap_s: Pause in seconds between two consecutive bytes (not after the last one).
        """
        repeat = max(1, int(repeat))
        with self._write_lock:
            self.stop_epoch += 1
            if self.serial and self.serial.is_open:
                for i in range(repeat):
                    try:
                        self.serial.write(bytes([ArduinoCommand.EMERGENCY_STOP.value])) # 0xFF
                        self.serial.flush()
                    except serial.SerialException as e:
                        # The 0xFF cannot be delivered: reset the board instead (reopening the port toggles DTR), so a
                        # move it may still be stepping ends anyway (R-4)
                        log.error(f"[ArduinoBridge] Serial connection lost during send_stop: {e}")
                        self._reset_by_reopen()
                        break
                    if i < repeat - 1 and gap_s > 0:
                        time.sleep(gap_s)
        log.warning("[ArduinoBridge] Out-of-band universal EMERGENCY_STOP (0xFF) sent x%d (cuts power to both motors)",
                    repeat)

    def close(self):
        """
        Safely disarms the hardware before releasing the OS serial port.
        """
        with self._write_lock:
            if self.serial and self.serial.is_open:
                try:
                    # Fire one last emergency stop to ensure all coils are un-powered
                    # so the motors don't overheat while the Python script is dead.
                    self.serial.write(bytes([ArduinoCommand.EMERGENCY_STOP.value]))
                    self.serial.flush()
                    self.serial.close()
                except Exception as e:
                    log.warning(f"[ArduinoBridge] Error closing serial: {e}")