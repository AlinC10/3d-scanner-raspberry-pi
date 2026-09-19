from typing import Optional, Annotated
from pydantic import BaseModel, Field

class NetworkCredentials(BaseModel):
    """Network credentials required for connection"""
    ssid: Annotated[str, Field()]
    password: Annotated[Optional[str], Field()] = ""
    identity: Annotated[Optional[str], Field()] = None
