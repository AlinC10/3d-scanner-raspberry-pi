from typing import Optional, Annotated
from pydantic import BaseModel, Field

class WIFICharacteristics(BaseModel):
    in_use: Optional[str] = '' # '*' or ''
    ssid: str
    signal: int
    security: str
    saved: Annotated[bool, Field(description="A profile for this network is saved: connecting needs no password.")] = False


class SavedNetwork(BaseModel):
    """A Wi-Fi profile kept by NetworkManager (`GET /system/wifi/saved`). Its password is never returned."""
    name: str           # profile name (used by DELETE /system/wifi/saved/{name})
    ssid: str
    autoconnect: bool = True
