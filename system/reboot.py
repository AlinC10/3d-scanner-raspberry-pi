import os
import subprocess
import threading

def reboot():
    os.sync()

    try:
        subprocess.run(["systemctl", "reboot"], check=True)

    except Exception:
        raise SystemError("Can't reboot the system!")