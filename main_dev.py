"""
Development server: the whole API with a simulated rig, on a PC, without the Raspberry Pi hardware.

    python main_dev.py                 # http://127.0.0.1:8000 (reloads on code changes)
    fastapi dev main_dev.py            # the same through the FastAPI CLI

`MOCK_HARDWARE=1` makes `router.scanner` build `hardware.scanner_mock.ScannerMock` instead of the real `Scanner`
(no motors, cameras, lights, ToF or cloud are touched), and `MockHardware` stands in for the Pi-only libraries so that
the modules that import them load. Everything else is `main_prod.app`.

Like the production app it listens on 127.0.0.1 only and sets no CORS headers: the front-end is served by the app
itself (see `main_prod.py`), so it is the same origin.
"""
import logging
import os

log = logging.getLogger(__name__)

# 1. The flag first: router.scanner reads it at import time
os.environ["MOCK_HARDWARE"] = "1"

# 2. Stand-ins for the Pi-only libraries, BEFORE anything imports them
from hardware.mock_hardware import MockHardware

MockHardware.apply()
print("Running in DEV MODE: simulated rig, no hardware is touched.")

# 3. The production app (routers, lifespan, front-end)
from main_prod import app  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main_dev:app", host="127.0.0.1", port=8000, reload=True)
