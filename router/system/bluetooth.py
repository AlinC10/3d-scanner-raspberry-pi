from fastapi import APIRouter, status, HTTPException, Response

from schemas.system.bluetooth import *
from system.bluetooth import BluetoothConnectionError
from system import bluetooth
from typing import Literal

router = APIRouter(
    prefix="/bluetooth",
    tags=["System: Bluetooth"]
)

@router.get("/status")
def get_status() -> dict[str, bool]:
    return {"powered": bluetooth.get_power_status()}


@router.get("/power/{state}")
def set_power(state: Literal["on", "off"]) -> dict[str, Literal["on", "off"]]:
    success = bluetooth.power_on() if state == "on" else bluetooth.power_off()

    if success:
        return {"powered": {state}}
    else:
        raise BluetoothConnectionError("Failed to change power state")


@router.get("/scan")
def scan_bt() -> list[BluetoothDevice]:
    return bluetooth.scan_devices()


@router.post("/connect")
def connect_bluetooth(device: BluetoothDevice) -> BluetoothDevice:
    mac_address = device.mac_address
    bluetooth.pair_device(mac_address)
    bluetooth.connect_device(mac_address)
    return device

@router.post("/send-file")
def send_file(payload: SendFileRequest):
    try:
        mac_address = payload.device.mac_address
        file_path = payload.file_path
        bluetooth.send_file(mac_address, file_path)

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    except BluetoothConnectionError as e:
        raise BluetoothConnectionError(f"Transfer failed: {str(e)}")
