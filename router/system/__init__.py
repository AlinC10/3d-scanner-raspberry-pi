from fastapi import APIRouter
from . import bluetooth, wifi, ethernet

router = APIRouter(
    prefix="/system")

routers = [bluetooth.router, wifi.router, ethernet.router]

for imported_router in routers:
    router.include_router(imported_router)
