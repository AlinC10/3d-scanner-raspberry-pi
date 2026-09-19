from pydantic import BaseModel, Field
from typing import Annotated, Optional
from pathlib import Path

class BluetoothDevice(BaseModel):
    mac_address: Annotated[str, Field(
        description="Bluetooth MAC address (XX:XX:XX:XX:XX:XX)",
        pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
    )]
    name: Annotated[str, Field(
        description="Friendly name or alias of the device"
    )] = "Unknown"
    icon: Annotated[Optional[str], Field(
        description="Freedesktop icon category: phone, computer, audio-headset, etc.")
    ] = None
    paired: Annotated[bool, Field(description="Whether the device is paired with the adapter")] = False
    connected: Annotated[bool, Field(description="Whether there is an active connection")] = False
    rssi: Annotated[Optional[int], Field(description="Received Signal Strength Indicator in dBm (signed int)")] = None
    device_class: Annotated[
        Optional[int], Field(description="24-bit unsigned integer representing Bluetooth Class of Device (CoD)")] = None

class SendFileRequest(BaseModel):
    device: BluetoothDevice
    file_path: Annotated[str | Path, Field(description="File Path of the requested files.")]