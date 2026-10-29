from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from schemas.system.bluetooth import *
from system import bluetooth, network_state
from system.bluetooth import BluetoothConnectionError
from .guard import network_change_allowed

router = APIRouter(
    prefix="/bluetooth",
    tags=["System: Bluetooth"]
)

@router.get("/status")
def get_status() -> dict[str, bool]:
    return {"powered": bluetooth.get_power_status()}


# POST is the right method for a change (SYS-11); GET is kept for the current front-end
@router.api_route("/power/{state}", methods=["POST", "GET"], dependencies=[Depends(network_change_allowed)])
def set_power(state: Literal["on", "off"]) -> dict[str, Literal["on", "off"]]:
    success = bluetooth.power_on() if state == "on" else bluetooth.power_off()

    if success:
        network_state.remember_bluetooth_power(state == "on")
        return {"powered": state}
    else:
        raise BluetoothConnectionError(
            "Failed to change power state (if the adapter is blocked: `rfkill unblock bluetooth`)"
        )


@router.get("/scan", dependencies=[Depends(network_change_allowed)])
def scan_bt(
    duration: Annotated[int, Query(ge=1, le=bluetooth.SCAN_DURATION_MAX_S, description="Discovery time in seconds.")] = bluetooth.SCAN_DURATION_DEFAULT_S,
    include_unnamed: Annotated[bool, Query(description="Also list devices that only have an address.")] = False,
    include_out_of_range: Annotated[bool, Query(description="Also list remembered devices not seen now.")] = False,
) -> list[BluetoothDevice]:
    """The devices seen during this discovery, with a real name, strongest first (and the connected ones)."""
    return bluetooth.scan_devices(duration=duration, include_unnamed=include_unnamed,
                                  include_out_of_range=include_out_of_range)


@router.post("/connect", dependencies=[Depends(network_change_allowed)])
def connect_bluetooth(device: BluetoothDevice) -> BluetoothDevice:
    """Pairs (only when not paired yet: SYS-8) and connects; the device is reconnected when the server starts."""
    mac_address = device.mac_address
    if not bluetooth.is_paired(mac_address):
        bluetooth.pair_device(mac_address)
    bluetooth.connect_device(mac_address)
    network_state.remember_bluetooth_device(mac_address)
    return device

@router.post("/send-file", dependencies=[Depends(network_change_allowed)])
def send_file(payload: SendFileRequest):
    try:
        mac_address = payload.device.mac_address
        file_path = payload.file_path
        bluetooth.send_file(mac_address, file_path)

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    except BluetoothConnectionError as e:
        raise BluetoothConnectionError(f"Transfer failed: {str(e)}")
