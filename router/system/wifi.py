from fastapi import APIRouter, status, HTTPException
from fastapi.responses import Response
from schemas.system.wifi import *
from system import wifi
from system.wifi import WIFIConnectionError
from typing import Annotated, Literal
from schemas.system.network import *

router = APIRouter(
    prefix="/wifi",
    tags=["System: WI-FI"],
)

@router.get("/status")
def get_wifi_status() -> dict[str, bool]:
    """Return whether WI-FI is enabled or disabled."""
    return {"enabled": wifi.get_status()}

@router.get("/power/{state}")
def set_wifi_power(state: Literal["on", "off"]) -> dict[str, Literal["on", "off"]]:
    wifi.set_wifi_power(state)
    return {"state": state}

@router.get("/scan")
def scan_wifi() -> list[WIFICharacteristics]:
    return list(wifi.scan_wifi().values())

@router.post("/connect")
def connect_wifi(details: NetworkCredentials):
    """Check network credential to connect to the Ethernet."""
    try:
        wifi.connect_wifi(details)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    except WIFIConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e)
        )