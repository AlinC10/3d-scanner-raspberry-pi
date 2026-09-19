from fastapi import APIRouter
from schemas.system.network import NetworkCredentials
from system import ethernet

router = APIRouter(prefix="/ethernet", tags=["System: Ethernet"])

@router.get("/status")
def ethernet_status():
    """Returns whether the cable is plugged in and if it holds an IP."""
    return ethernet.get_status()

@router.post("/connect")
def secure_connect(details: NetworkCredentials):
    """Provides credentials for strict 802.1X wired networks."""
    ethernet.connect_secure_ethernet(details)
    return {"status": "success", "message": "Enterprise Ethernet connected"}