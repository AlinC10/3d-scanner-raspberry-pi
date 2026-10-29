from fastapi import APIRouter

from system import status as system_status
from . import bluetooth, wifi, ethernet
from .guard import network_change_allowed  # noqa: F401  (re-exported: the one rule for every route that changes the network)

router = APIRouter(
    prefix="/system")


@router.get("/status", tags=["System"])
def get_network_status():
    """
    Every connection in one call: Ethernet (cable, state, profile, IP), Wi-Fi (radio, state, SSID, signal, IP),
    Bluetooth (power, connected devices) and the outcome of the restore at startup. Reads only what the system already
    knows (no radio scan, nothing changed): safe to poll every few seconds, also during a scan.
    """
    return system_status.get_network_status()


routers = [bluetooth.router, wifi.router, ethernet.router]

for imported_router in routers:
    router.include_router(imported_router)
