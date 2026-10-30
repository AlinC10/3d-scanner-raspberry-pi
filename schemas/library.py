from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# Aliases accepted for a model format: the extension with or without the dot, any case. A glTF request gets the
# binary .glb (the only glTF file the results contain).
_FORMAT_ALIASES = {
    "glb": ".glb", "gltf": ".glb",
    "stl": ".stl",
}


def normalize_model_format(value: Any) -> Any:
    """``"stl"`` / ``"STL"`` / ``".stl"`` -> ``".stl"``; ``"glb"`` / ``"gltf"`` / ``".gltf"`` -> ``".glb"``.

    Anything else is returned unchanged so the Literal check refuses it (422).
    """
    if isinstance(value, str):
        key = value.strip().lower().lstrip(".")
        return _FORMAT_ALIASES.get(key, value)
    return value


ModelFormat = Annotated[Literal[".glb", ".stl"], BeforeValidator(normalize_model_format)]
ModelLod = Literal["low", "high"]
# Kind of scan an object comes from (storage.results.CATEGORIES); "all" in the list filter
ObjectCategory = Literal["object", "face"]
CategoryFilter = Literal["all", "object", "face"]


class ModelRequest(BaseModel):
    """Query of `GET /library/objects/{id}/model`: which one of the object's models to send."""
    format: Annotated[ModelFormat, Field(description='".glb" or ".stl" (also "glb", "stl", "gltf", ".gltf").')] = ".glb"
    lod: Annotated[ModelLod, Field(description='"low" (light, the default) or "high" (full detail).')] = "low"


class LibraryObject(BaseModel):
    """One finished object: `id` is its folder name, `object` its `object.json`, `models` the files present."""
    id: str
    category: ObjectCategory = Field(
        "object", description='"object" (turntable scan) or "face" (face scan); inferred for older objects.'
    )
    object: dict[str, Any]
    models: dict[str, list[ModelLod]] = Field(
        default_factory=dict, description='Formats present and their levels of detail, e.g. {".glb": ["low", "high"]}.'
    )


class LibraryPage(BaseModel):
    """`GET /library/objects`: one page of the finished objects, newest first."""
    items: list[LibraryObject]
    total: int   # objects matching the category / search filter (all of them without a filter)
    offset: int
    limit: int
    next_offset: int | None = None   # offset of the next page; None when this is the last one


class ObjectDimensions(BaseModel):
    """Real size of the object in millimetres; only the sides sent are changed."""
    model_config = ConfigDict(extra="forbid")
    length: Annotated[float | None, Field(gt=0, le=10_000)] = None
    width: Annotated[float | None, Field(gt=0, le=10_000)] = None
    height: Annotated[float | None, Field(gt=0, le=10_000)] = None


class UpdateObjectRequest(BaseModel):
    """
    Body of `PATCH /library/objects/{id}`: only the fields sent are changed in `object.json` (null or missing = keep).
    The values computed by the reconstruction that are not listed here (created_at, job_id, faces, ...) never change.
    """
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    dimensions_mm: ObjectDimensions | None = None
    volume_cm3: Annotated[float | None, Field(gt=0, le=1_000_000)] = None


class UsbExportRequest(BaseModel):
    """Body of `POST /library/objects/{id}/export/usb` (optional): which stick, when more than one is plugged in."""
    model_config = ConfigDict(extra="forbid")
    device: Annotated[str | None, Field(
        pattern=r"^/dev/[A-Za-z0-9]+$", description="Partition from GET /library/usb (e.g. /dev/sda1); null = the first stick."
    )] = None


class UsbDrive(BaseModel):
    device: str
    label: str | None = None
    size_bytes: int = 0
    fstype: str | None = None
    mountpoint: str | None = None
    free_bytes: int | None = None


class UsbExportStatus(BaseModel):
    """
    Progress of the USB export. `state`: idle | mounting | archiving | syncing | done | error. Pull the stick out only
    when `safe_to_remove` is true.
    """
    state: str
    object_id: str | None = None
    device: str | None = None
    archive: str | None = None      # file name on the stick: <object id>.zip
    bytes_total: int = 0
    bytes_done: int = 0
    error: str | None = None
    safe_to_remove: bool = True
    started_at: float | None = None
    finished_at: float | None = None


class BluetoothExportRequest(BaseModel):
    """Body of `POST /library/objects/{id}/export/bluetooth` (optional): which connected device, when several are."""
    model_config = ConfigDict(extra="forbid")
    mac_address: Annotated[str | None, Field(
        pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", description="Connected device; null = the first connected one."
    )] = None


class BluetoothExportStatus(BaseModel):
    """Progress of the Bluetooth export. `state`: idle | archiving | sending | done | error | cancelled."""
    state: str
    object_id: str | None = None
    device: str | None = None
    device_name: str | None = None
    archive: str | None = None
    bytes_total: int = 0
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
