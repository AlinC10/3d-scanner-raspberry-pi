"""
One way to run the network tools (`nmcli`, and the generic runner used by Bluetooth): a deadline on every call, the C
locale (the success checks compare English text), a clear error when the tool is missing, and a parser for the terse
`nmcli -t` output that respects its escaping (an SSID may contain ':').

Errors carry an HTTP `status_code` (read by the exception handler of the app): 504 when a command overran its
deadline, 503 when the tool or NetworkManager is missing, 400 otherwise.
"""
import os
import subprocess
from subprocess import CompletedProcess
from typing import Optional, Type

from system.exception import SystemConnectionError

# Deadlines (seconds) of the nmcli calls
NMCLI_TIMEOUT_S = 15.0          # status, radio, connection show / up of a known profile
WIFI_SCAN_TIMEOUT_S = 25.0      # a real radio scan (`--rescan yes`) can take ~10-20 s
WIFI_CONNECT_WAIT_S = 40        # `nmcli --wait`: how long NetworkManager may take to activate
WIFI_CONNECT_TIMEOUT_S = WIFI_CONNECT_WAIT_S + 10.0


class CommandTimeoutError(SystemConnectionError):
    """A network command did not finish within its deadline."""
    status_code = 504


class ToolUnavailableError(SystemConnectionError):
    """The tool (nmcli, bluetoothctl, bt-obex) is not installed or its daemon is not running."""
    status_code = 503


def _environment() -> dict:
    env = dict(os.environ)
    env["LC_ALL"] = "C"     # English messages and "enabled"/"yes" whatever the system language is (SYS-6)
    env["LANG"] = "C"
    return env


def run(cmd: list[str], timeout: float, error_type: Type[SystemConnectionError] = SystemConnectionError,
        **kwargs) -> CompletedProcess:
    """
    Runs `cmd` with a deadline and the C locale; captures text output unless the caller redirects it.

    :raises CommandTimeoutError: the command did not finish in time (the child is killed).
    :raises ToolUnavailableError: the program is not installed.
    """
    kwargs.setdefault("text", True)
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    try:
        return subprocess.run(cmd, timeout=timeout, env=_environment(), **kwargs)
    except subprocess.TimeoutExpired:
        raise CommandTimeoutError(f"`{' '.join(cmd[:4])}` did not finish within {timeout:g} s")
    except FileNotFoundError:
        raise ToolUnavailableError(f"`{cmd[0]}` is not installed on this system (SYS-4).")


def nmcli(args: list[str], timeout: float = NMCLI_TIMEOUT_S,
          error_type: Type[SystemConnectionError] = SystemConnectionError,
          check: Optional[str] = None) -> CompletedProcess:
    """
    Runs `nmcli <args>`. With `check`, a non-zero exit becomes `error_type(check + ": " + message)`; a NetworkManager
    that is not running becomes ToolUnavailableError.
    """
    res = run(["nmcli", *args], timeout=timeout, error_type=error_type)
    if check is not None and res.returncode != 0:
        message = (res.stderr or res.stdout or "").strip()
        if "NetworkManager is not running" in message:
            raise ToolUnavailableError(f"{check}: NetworkManager is not running.")
        raise error_type(f"{check}: {message}")
    return res


def split_terse(line: str) -> list[str]:
    """
    Splits one line of `nmcli -t` output on the unescaped ':' and removes the escaping (`\\:` -> ':', `\\\\` -> '\\').
    `My:Net:80:WPA2` printed as `My\\:Net:80:WPA2` gives ["My:Net", "80", "WPA2"] (SYS-3).
    """
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields
