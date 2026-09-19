from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
import router.system
import router.ai
import router.library
import router.scanning

from system.exception import SystemConnectionError

app = FastAPI()

@app.exception_handler(SystemConnectionError)
async def system_connection_error_handler(request: Request, exc: SystemConnectionError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)}
    )


routers = [router.system, router.ai, router.library, router.scanning]

for router in routers:
    app.include_router(router.router)

app.frontend("/", directory="../3d-scanner-front-end")