from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

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


class ModelRequest(BaseModel):
    """Query of `GET /library/objects/{id}/model`: which one of the object's models to send."""
    format: Annotated[ModelFormat, Field(description='".glb" or ".stl" (also "glb", "stl", "gltf", ".gltf").')] = ".glb"
    lod: Annotated[ModelLod, Field(description='"low" (light, the default) or "high" (full detail).')] = "low"


class LibraryObject(BaseModel):
    """One finished object: `id` is its folder name, `object` its `object.json`, `models` the files present."""
    id: str
    object: dict[str, Any]
    models: dict[str, list[ModelLod]] = Field(
        default_factory=dict, description='Formats present and their levels of detail, e.g. {".glb": ["low", "high"]}.'
    )


class LibraryPage(BaseModel):
    """`GET /library/objects`: one page of the finished objects, newest first."""
    items: list[LibraryObject]
    total: int
    offset: int
    limit: int
    next_offset: int | None = None   # offset of the next page; None when this is the last one
