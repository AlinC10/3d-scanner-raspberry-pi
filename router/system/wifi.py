from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from schemas.system.network import *
from schemas.system.wifi import *
from system import network_state, wifi
from .guard import network_change_allowed

router = APIRouter(
    prefix="/wifi",
    tags=["System: WI-FI"],
)

@router.get("/status")
def get_wifi_status() -> dict[str, bool]:
    """Return whether WI-FI is enabled or disabled."""
    return {"enabled": wifi.get_status()}

# POST is the right method for a change (SYS-11); GET is kept for the current front-end
@router.api_route("/power/{state}", methods=["POST", "GET"], dependencies=[Depends(network_change_allowed)])
def set_wifi_power(state: Literal["on", "off"]) -> dict[str, Literal["on", "off"]]:
    wifi.set_wifi_power(state)
    network_state.remember_wifi_power(state == "on")
    return {"state": state}

@router.get("/scan", dependencies=[Depends(network_change_allowed)])
def scan_wifi(
    rescan: Annotated[bool, Query(description="Force a radio scan (up to ~20 s). False: the list the system already has.")] = False,
) -> list[WIFICharacteristics]:
    """The networks in range, one per SSID, strongest first; `saved` = connecting needs no password."""
    networks = sorted(wifi.scan_wifi(rescan=rescan).values(), key=lambda n: (n.in_use != "*", -n.signal))
    return networks

@router.get("/saved")
def saved_networks() -> list[SavedNetwork]:
    """The saved Wi-Fi profiles (their passwords stay in NetworkManager)."""
    return wifi.list_saved_networks()

@router.delete("/saved/{name}", status_code=204, dependencies=[Depends(network_change_allowed)])
def forget_network(name: str):
    """Forgets a saved network (by profile name or SSID): the next connection asks for the password again."""
    wifi.delete_saved_network(name)
    network_state.forget_wifi_profile(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.post("/connect", dependencies=[Depends(network_change_allowed)])
def connect_wifi(details: NetworkCredentials):
    """
    Connects to a network. A saved network needs no password (send an empty one); 401 when the password is wrong or a
    saved one is no longer accepted. The connection is remembered and made again when the server starts.
    """
    profile = wifi.connect_wifi(details)
    network_state.remember_wifi_profile(profile)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
