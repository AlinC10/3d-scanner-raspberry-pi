import sys
import os

file_path = "../hardware/arduino_bridge.py"
with open(file_path, "r") as f:
    text = f.read()

# 1. Update Locks to RLock
text = text.replace("self._write_lock = threading.Lock()", "self._write_lock = threading.RLock()")
text = text.replace("self._response_lock = threading.Lock()", "self._response_lock = threading.RLock()")

# 2. Add config storage to init
init_find = "self.serial: Optional[serial.Serial] = None"
init_replace = "self.serial: Optional[serial.Serial] = None\n        self._configs = []"
text = text.replace(init_find, init_replace)

# 3. Update _connect to replay configs
connect_find = """        if not ready_received:
            # Fallback: if we connected to an already-open port without DTR reset, 
            # ping the board to ensure it is alive.
            try:
                self.send_command(f"{ArduinoCommand.PING.value}")
            except Exception:
                pass"""
connect_replace = """        if not ready_received:
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
                    pass"""
text = text.replace(connect_find, connect_replace)

# 4. Update send_command to store configs and wrap write in try/except
send_find = """        clean_cmd = cmd.strip() + "\\n"
        
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
                    self.serial.timeout = old_timeout"""

send_replace = """        clean_cmd = cmd.strip() + "\\n"
        
        # Intercept and store CONFIG commands for auto-healing
        if clean_cmd.startswith(str(ArduinoCommand.CONFIG.value) + " "):
            cmd_no_nl = cmd.strip()
            if cmd_no_nl not in self._configs:
                self._configs.append(cmd_no_nl)
        
        # Acquire transaction lock so no other normal command can interleave
        with self._response_lock:
            # Temporarily mutate the underlying pyserial timeout for this specific transaction
            old_timeout = self.serial.timeout
            if timeout is not None:
                self.serial.timeout = timeout
                
            try:
                # Acquire write lock just for the physical bytes transfer
                with self._write_lock:
                    self.serial.write(clean_cmd.encode("utf-8"))
                    self.serial.flush()

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
                    self.serial.timeout = old_timeout"""

text = text.replace(send_find, send_replace)

with open(file_path, "w") as f:
    f.write(text)

print("ArduinoBridge patched successfully!")
