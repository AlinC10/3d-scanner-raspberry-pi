from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
import router.system
import router.ai
import router.library
import router.scanner
import router.test

from system.exception import SystemConnectionError
from fastapi.staticfiles import StaticFiles
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    # Open the Arduino serial port and claim the GPIO pins once, here, and not as a side effect of importing the
    # router module. A failure aborts the startup, as it did when this ran at import time.
    from router.scanner import scanner
    scanner.get()
    yield
    # --- SHUTDOWN ---
    log.info("FastAPI server shutting down. Initiating hardware cleanup...")
    try:
        from router.scanner import scanner
        if not scanner.initialized:
            return  # the hardware was never opened: nothing to power down
        scanner.emergency_stop()  # Instantly halt any running background loops/motors
        scanner.cleanup()         # Power down the lights and release motor torque
        scanner.close_hardware()  # Release the GPIO pins & I2C to the OS
        log.info("Hardware cleanup complete.")
    except Exception as e:
        log.error(f"Error during hardware cleanup: {e}")

app = FastAPI(lifespan=lifespan)

@app.exception_handler(SystemConnectionError)
async def system_connection_error_handler(request: Request, exc: SystemConnectionError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)}
    )


routers = [router.system, router.ai, router.library, router.scanner, router.test]

for router in routers:
    app.include_router(router.router)

# The front-end is located from this file (or FRONTEND_DIR), not from the current directory. A missing folder is a
# warning: the API still works, only the UI at "/" is absent.
_frontend_dir = frontend_directory()
if _frontend_dir.is_dir():
    app.frontend("/", directory=str(_frontend_dir))
else:
    log.warning("Front-end folder %s does not exist: serving the API only (set FRONTEND_DIR to its location).",
                _frontend_dir)

app.mount("/docs", StaticFiles(directory="ai/ai-docs"), name="docs")

if __name__ == "__main__":
    # `python main_prod.py`: the scanner is meant to be reached only from the Pi itself, so the server listens on the
    # loopback address and is not visible in the network. (`fastapi dev` does the same; `fastapi run` listens on
    # 0.0.0.0 unless `--host 127.0.0.1` is given.)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
