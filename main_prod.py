from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
import router.system
import router.ai
import router.library
import router.scanner
import router.test

from app_paths import AI_DOCS_DIR, frontend_directory
from system.exception import SystemConnectionError
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    # Open the Arduino serial port and claim the GPIO pins once, here, and not as a side effect of importing the
    # router module. A missing Arduino does not stop the server: the motor requests answer 503 until it answers.
    from router.scanner import scanner
    try:
        scanner.get()
    except Exception as e:
        # The rest of the server (UI, network, AI, the 3D models) does not need the rig: keep running. The scanner is
        # built again on the next request that uses it, and that request reports the error (API-21).
        log.error("Scanner hardware could not be opened at startup: %s. Scanning is unavailable until it is fixed.", e)
    # The AI assistant loads in the background and never stops the server: without GROQ_API_KEY (or a library) it stays
    # off and GET /api/ai/status says why
    from router.ai import service as ai_service
    ai_service.start()
    # The Wi-Fi / Ethernet / Bluetooth connections of the last run are made again in the background (S6); a failure is
    # only logged (and shown by GET /system/status): the server works as before
    from system import network_state
    try:
        network_state.start_restore()
    except Exception as e:
        log.warning("The network restore could not start: %s", e)
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
    # A network error says which code fits (504 timeout, 503 tool missing, 401 refused password, 409 a scan runs);
    # anything else is a 400
    code = getattr(exc, "status_code", None)
    return JSONResponse(
        status_code=code if isinstance(code, int) else status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)}
    )


routers = [router.system, router.ai, router.library, router.scanner, router.test]

for router in routers:
    app.include_router(router.router)

# The documents the AI assistant answers from. Not under /docs: that is the Swagger UI. Mounted before the
# front-end, which serves everything under "/".
if AI_DOCS_DIR.is_dir():
    app.mount("/ai-docs", StaticFiles(directory=str(AI_DOCS_DIR)), name="ai-docs")

# The front-end is located from this file (or FRONTEND_DIR), not from the current directory. A missing folder is a
# warning: the API still works, only the UI at "/" is absent.
_frontend_dir = frontend_directory()
if _frontend_dir.is_dir():
    app.frontend("/", directory=str(_frontend_dir))
else:
    log.warning("Front-end folder %s does not exist: serving the API only (set FRONTEND_DIR to its location).",
                _frontend_dir)


if __name__ == "__main__":
    # `python main_prod.py`: the scanner is meant to be reached only from the Pi itself, so the server listens on the
    # loopback address and is not visible in the network. (`fastapi dev` does the same; `fastapi run` listens on
    # 0.0.0.0 unless `--host 127.0.0.1` is given.)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
