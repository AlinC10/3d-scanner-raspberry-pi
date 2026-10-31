from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app_paths import results_directory
from schemas.library import (CategoryFilter, LibraryObject, LibraryPage, ModelRequest, UpdateObjectRequest, UsbDrive,
                             UsbExportRequest, UsbExportStatus, BluetoothExportRequest, BluetoothExportStatus)
from storage import results
from system import bluetooth as bt, bluetooth_export, usb

router = APIRouter(
    prefix="/library",
    tags=["library"],
)

# Most objects one page may ask for
MAX_PAGE_SIZE = 100
# Export states during which the object's files are read (a delete waits for them)
_EXPORT_RUNNING = {"USB": usb.RUNNING_STATES, "Bluetooth": bluetooth_export.RUNNING_STATES}


@router.get("/objects", response_model=LibraryPage)
def list_objects(
    offset: Annotated[int, Query(ge=0, description="Index of the first object to return.")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="How many objects to return.")] = 20,
    category: Annotated[CategoryFilter, Query(description='"all", "object" or "face".')] = "all",
    search: Annotated[str | None, Query(max_length=120, description="Text in the title, description or id (any case).")] = None,
):
    """
    The finished 3D objects, newest first: their id, category and `object.json`, and which models exist. No model file
    is sent here; ask for one with `GET /library/objects/{id}/model`. Continue with `offset=next_offset` until it is
    null. `category` and `search` filter before the page is cut: `total` counts the matching objects.
    """
    page = results.list_results(results_directory(), offset=offset, limit=limit, category=category, search=search)
    return LibraryPage(
        items=[LibraryObject(**entry.as_dict()) for entry in page.items],
        total=page.total, offset=page.offset, limit=page.limit, next_offset=page.next_offset,
    )


@router.get("/objects/{object_id}", response_model=LibraryObject)
def get_object(object_id: str):
    """One finished object (404 when it does not exist)."""
    try:
        entry = results.get_result(results_directory(), object_id)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return LibraryObject(**entry.as_dict())


@router.delete("/objects/{object_id}", status_code=204)
def delete_object(object_id: str):
    """
    Deletes an object for good: its models, `object.json` and the scan photos kept with it. `204` done, `404` unknown
    object, `409` while a USB or Bluetooth export of this object is running.
    """
    for name, status in (("USB", usb.export_status()), ("Bluetooth", bluetooth_export.export_status())):
        if status.get("object_id") == object_id and status.get("state") in _EXPORT_RUNNING[name]:
            raise HTTPException(status_code=409, detail=f"A {name} export of {object_id} is running; wait or cancel it.")
    try:
        results.delete_result(results_directory(), object_id)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete {object_id}: {e}")
    return Response(status_code=204)


@router.get("/objects/{object_id}/model")
def get_model(object_id: str, query: Annotated[ModelRequest, Query()]):
    """
    One model of an object, as a file: `format` `.glb` or `.stl` (`glb`, `stl`, `gltf` are accepted too), `lod`
    `low` or `high`. 404 when the object or that model does not exist, 422 for another format.
    """
    try:
        path = results.model_path(results_directory(), object_id, query.format, query.lod)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return FileResponse(
        path,
        media_type=results.MEDIA_TYPES[query.format],
        filename=f"{object_id}_{query.lod}{query.format}",
    )


@router.patch("/objects/{object_id}", response_model=LibraryObject)
def update_object(object_id: str, req: UpdateObjectRequest):
    """
    Edits `title`, `description`, `dimensions_mm` (any of length / width / height) or `volume_cm3` in the object's
    `object.json`; only the fields sent (and not null) change. 404 unknown object, 422 invalid value or other field.
    """
    changes = req.model_dump(exclude_none=True)
    if "dimensions_mm" in changes:
        changes["dimensions_mm"] = {k: v for k, v in changes["dimensions_mm"].items() if v is not None}
    try:
        entry = results.update_object(results_directory(), object_id, changes)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return LibraryObject(**entry.as_dict())


@router.get("/usb", response_model=list[UsbDrive])
def list_usb_drives():
    """The USB sticks plugged in (one entry per partition with a file system). 500 when they cannot be listed."""
    try:
        return usb.list_drives()
    except usb.UsbError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/usb/eject")
def eject_usb_drive(req: UsbExportRequest | None = None):
    """
    Safe removal: unmounts the stick (all its partitions) and powers it off, so it can be pulled out without damage.
    Body optional `{"device": "/dev/sda1"}` (default: the first stick). `200` `{"device", "disk", "powered_off",
    "safe_to_remove": true}`; `503` no stick, `409` an export is running, `500` it could not be unmounted.
    """
    try:
        result = usb.eject(req.device if req else None)
    except usb.UsbNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except usb.UsbBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except usb.UsbError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {**result, "safe_to_remove": True}


@router.post("/objects/{object_id}/export/usb", response_model=UsbExportStatus, status_code=202)
def export_object_to_usb(object_id: str, req: UsbExportRequest | None = None):
    """
    Exports the object (every model format: obj, glb, stl, 3mf, and object.json) as `<object id>.zip` in the root of
    the USB stick; an older archive of the same object is replaced. Runs in the background: poll
    `GET /library/export/usb/status` until `state` is `done` (or `error`); the stick is unmounted at the end and can be
    pulled out when `safe_to_remove` is true. 404 unknown object, 503 no USB stick, 409 an export is running.
    """
    try:
        folder = results.finished_folder(results_directory(), object_id)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return usb.start_export(folder, req.device if req else None)
    except usb.UsbNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except usb.UsbBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except usb.UsbError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export/usb/status", response_model=UsbExportStatus)
def usb_export_status():
    """Progress of the last USB export (see `POST /library/objects/{id}/export/usb`)."""
    return usb.export_status()


@router.post("/objects/{object_id}/export/bluetooth", response_model=BluetoothExportStatus, status_code=202)
def export_object_to_bluetooth(object_id: str, req: BluetoothExportRequest | None = None):
    """
    Sends the object (the same `<object id>.zip` as the USB export) to the connected Bluetooth device (OBEX push; the
    phone asks to accept the file). Runs in the background: poll `GET /library/export/bluetooth/status`.
    `POST /system/bluetooth/disconnect` cancels it. 404 unknown object, 503 no device connected, 409 a transfer runs.
    """
    try:
        folder = results.finished_folder(results_directory(), object_id)
    except results.ResultNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return bluetooth_export.start_export(folder, bt.connected_devices(), req.mac_address if req else None)
    except bluetooth_export.NoConnectedDeviceError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except bluetooth_export.BluetoothBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except bluetooth_export.BluetoothExportError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export/bluetooth/status", response_model=BluetoothExportStatus)
def bluetooth_export_status():
    """Progress of the last Bluetooth export: idle | archiving | sending | done | error | cancelled."""
    return bluetooth_export.export_status()


@router.post("/export/bluetooth/cancel")
def cancel_bluetooth_export():
    """Stops the running Bluetooth export (the device stays connected). `cancelled`: whether one was running."""
    return {"cancelled": bluetooth_export.cancel()}
