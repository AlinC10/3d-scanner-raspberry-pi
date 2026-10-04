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
        
        # We use a two-lock architecture to allow emergency stops to preempt active commands.
        
        # 1. Write Lock: Prevents two threads from interleaving bytes during a write.
        self._write_lock = threading.Lock()
        
        # 2. Response (Transaction) Lock: Ensures a strict Request->Response transaction.
        # If Thread A sends a ROTATE command, no other thread can send a normal command 
        # until Thread A receives its '0' or '4' response. This prevents Thread B from 
        # stealing Thread A's response buffer.
        self._response_lock = threading.Lock()
        
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
            search_names = ["ARDUINO", "CH340", "CH341", "CP210", "FTDI", "USB-SERIAL"]
            
            for p in ports:
                # p[1] is the OS-provided hardware description field.
                description = (p.description or p[1] or "").upper()
                for name in search_names:
                    if name in description:
                        target_port = p.device
                        break
                if target_port:
                    break
            
            # Fallback if no matching descriptors are found
            target_port = target_port or "/dev/ttyACM0"

        log.info(f"[ArduinoBridge] Connecting to {target_port} at {self.baudrate} baud...")
        
        # IMPORTANT: Opening the serial port on an Arduino Uno triggers the DTR pin, 
        # causing a hard hardware reset. The Arduino will spend ~1.5s in the Optiboot bootloader.
        try:
            if self.serial and self.serial.is_open:
                self.serial.close()
            self.serial = serial.Serial(target_port, self.baudrate, timeout=3.0)
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
            # Fallback: if we connected to an already-open port without DTR reset, 
            # ping the board to ensure it is alive.
            try:
                self.send_command(f"{ArduinoCommand.PING.value}")
            except Exception:
                pass

    def send_command(self, cmd: str, timeout: Optional[float] = 30.0) -> str:
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
        
        # Acquire transaction lock so no other normal command can interleave
        with self._response_lock:
            # Acquire write lock just for the physical bytes transfer
            with self._write_lock:
                self.serial.write(clean_cmd.encode("utf-8"))
                self.serial.flush()

            # Temporarily mutate the underlying pyserial timeout for this specific transaction
            old_timeout = self.serial.timeout
            if timeout is not None:
                self.serial.timeout = timeout
                
            try:
                # Block until the Arduino executes the command and returns its Status Code (e.g., '0' or '4')
                line = self.serial.readline().decode("utf-8", errors="ignore").strip()
                return line
            except serial.SerialException as e:
                log.error(f"[ArduinoBridge] Serial connection lost during send_command: {e}. Attempting to reconnect...")
                self._connect()
                return str(ArduinoStatus.ERR_ABORTED.value) # Pretend it aborted so caller can handle safely
            finally:
                # Restore the default timeout for the next command
                if self.serial and self.serial.is_open:
                    self.serial.timeout = old_timeout

    def send_stop(self):
        """
        Out-of-band immediate universal EMERGENCY STOP.
        
        Notice that this method ONLY takes `_write_lock`, and intentionally BYPASSES `_response_lock`.
        If Thread A is currently blocked inside `send_command()` waiting for a 30-second rotation 
        to finish, Thread B can call `send_stop()`. Thread B will inject the `0xFF` byte directly 
        onto the wire. 
        
        The C++ firmware checks for `0xFF` in between every single microsecond step. When it sees it,
        the Arduino aborts the rotation, releases coil torque, and replies '4' (ERR_ABORTED). 
        Thread A then wakes up, receives '4', and the system halts cleanly.
        """
        with self._write_lock:
            if self.serial and self.serial.is_open:
                try:
                    self.serial.write(bytes([ArduinoCommand.EMERGENCY_STOP.value])) # 0xFF
                    self.serial.flush()
                except serial.SerialException as e:
                    log.error(f"[ArduinoBridge] Serial connection lost during send_stop: {e}")
        log.warning("[ArduinoBridge] Out-of-band universal EMERGENCY_STOP (0xFF) sent (cuts power to both motors)")

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