"""
The generated 3D models on disk (`results/`), read for the `/library` endpoints.

One folder per object, named ``YYYYMMDD-HHMMSS-<6 hex>`` (the id of the object), as Turntable Studio writes it:
``object.json`` (title, dimensions, ...), ``status.json`` (``state: "ok"`` when the reconstruction finished),
``glb/{low,high}_model.glb``, ``stl/{low,high}_model.stl`` and other files that are not served.

Nothing here imports FastAPI or pydantic: the router maps the errors to HTTP codes.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# The id is the folder name; anything else (a path, "..") is refused before the disk is touched
RESULT_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

# format -> (sub-folder, file extension); the file name is always built here, never taken from a request
MODEL_FORMATS = {".glb": "glb", ".stl": "stl"}
MODEL_LODS = ("low", "high")
MEDIA_TYPES = {".glb": "model/gltf-binary", ".stl": "model/stl"}


class ResultNotFoundError(LookupError):
    """The object, or the model asked for, does not exist (or is not a finished result)."""


@dataclass
class ResultEntry:
    """One finished object: its id, its `object.json` and the models present on disk."""
    id: str
    object: dict
    models: dict = field(default_factory=dict)   # {".glb": ["low", "high"], ".stl": [...]}

    def as_dict(self) -> dict:
        return {"id": self.id, "object": self.object, "models": self.models}


@dataclass
class ResultPage:
    items: list
    total: int
    offset: int
    limit: int

    @property
    def next_offset(self) -> Optional[int]:
        following = self.offset + len(self.items)
        return following if following < self.total else None


def is_valid_id(result_id: str) -> bool:
    return isinstance(result_id, str) and RESULT_ID_PATTERN.fullmatch(result_id) is not None


def model_path(root: Path, result_id: str, fmt: str, lod: str) -> Path:
    """
    Path of one model file. Raises ResultNotFoundError for an unknown id, format or lod, or a missing file.

    :param root: the results folder.
    :param result_id: folder name of the object.
    :param fmt: ``".glb"`` or ``".stl"`` (already normalized by the schema).
    :param lod: ``"low"`` or ``"high"``.
    :raises ResultNotFoundError: no such object or model.
    """
    folder = _result_folder(root, result_id)
    if fmt not in MODEL_FORMATS or lod not in MODEL_LODS:
        raise ResultNotFoundError(f"Unknown model {lod}{fmt}.")
    path = folder / MODEL_FORMATS[fmt] / f"{lod}_model{fmt}"
    if not path.is_file():
        raise ResultNotFoundError(f"Object {result_id} has no {lod} {fmt} model.")
    return path


def get_result(root: Path, result_id: str) -> ResultEntry:
    """
    :raises ResultNotFoundError: the id is invalid, the folder is missing or it is not a finished result.
    """
    entry = _read_entry(_result_folder(root, result_id))
    if entry is None:
        raise ResultNotFoundError(f"Object {result_id} is not a finished result.")
    return entry


def list_results(root: Path, offset: int = 0, limit: int = 20) -> ResultPage:
    """
    The finished objects, newest first (the folder name starts with the date), one page of them.

    A folder that is not a finished result (no valid `object.json`, `status.json` not ``ok``, still being written) is
    skipped and logged: it never fails the whole list. A missing results folder is an empty list.
    """
    entries = []
    if root.is_dir():
        for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not folder.is_dir() or not is_valid_id(folder.name):
                continue
            entry = _read_entry(folder)
            if entry is not None:
                entries.append(entry)
    offset = max(0, offset)
    return ResultPage(items=entries[offset:offset + limit], total=len(entries), offset=offset, limit=limit)


def _result_folder(root: Path, result_id: str) -> Path:
    if not is_valid_id(result_id):
        raise ResultNotFoundError(f"Unknown object {result_id!r}.")
    folder = (root / result_id).resolve()
    # The pattern already excludes separators and dots; the containment check is a second line of defence
    if folder.parent != root.resolve() or not folder.is_dir():
        raise ResultNotFoundError(f"Unknown object {result_id!r}.")
    return folder


def _read_json(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_entry(folder: Path) -> Optional[ResultEntry]:
    status = _read_json(folder / "status.json")
    if status is None or status.get("state") != "ok":
        log.debug("Skipping %s: no status.json with state 'ok'.", folder.name)
        return None
    obj = _read_json(folder / "object.json")
    if obj is None:
        log.warning("Skipping result %s: object.json is missing or invalid.", folder.name)
        return None
    models = {}
    for fmt, sub in MODEL_FORMATS.items():
        lods = [lod for lod in MODEL_LODS if (folder / sub / f"{lod}_model{fmt}").is_file()]
        if lods:
            models[fmt] = lods
    return ResultEntry(id=folder.name, object=obj, models=models)
