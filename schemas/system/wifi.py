from typing import Optional, Annotated
from pydantic import BaseModel, Field

class WIFICharacteristics(BaseModel):
    in_use: Optional[str] = '' # '*' or ''
    ssid: str
    signal: int
    security: str