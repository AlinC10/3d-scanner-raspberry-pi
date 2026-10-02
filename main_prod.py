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

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    yield
    # --- SHUTDOWN ---
    log.info("FastAPI server shutting down. Initiating hardware cleanup...")
    try:
        from router.scanner import scanner
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

app.frontend("/", directory="../3d-scanner-front-end")