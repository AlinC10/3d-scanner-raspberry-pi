from fastapi import APIRouter, Depends
from schemas.system.network import NetworkCredentials
from system import ethernet, network_state
from .guard import network_change_allowed

router = APIRouter(prefix="/ethernet", tags=["System: Ethernet"])

@router.get("/status")
def ethernet_status():
    """Whether the cable is plugged in, the NetworkManager state, the profile and the IPv4 address."""
    return ethernet.get_status()

@router.post("/connect", dependencies=[Depends(network_change_allowed)])
def secure_connect(details: NetworkCredentials):
    """Provides credentials for strict 802.1X wired networks."""
    profile = ethernet.connect_secure_ethernet(details)
    network_state.remember_ethernet_profile(profile)
    return {"status": "success", "message": "Enterprise Ethernet connected"}
