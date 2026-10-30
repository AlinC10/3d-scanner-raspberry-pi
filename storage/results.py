"""
The generated 3D models on disk (`results/`), read for the `/library` endpoints.

One folder per object, named ``YYYYMMDD-HHMMSS-<6 hex>`` (the id of the object), as Turntable Studio writes it:
``object.json`` (title, dimensions, ...), ``status.json`` (``state: "ok"`` when the reconstruction finished),
``glb/{low,high}_model.glb``, ``stl/{low,high}_model.stl`` and other files that are not served.

Nothing here imports FastAPI or pydantic: the router maps the errors to HTTP codes.
"""
import json
import logging
import os
import re
import threading
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

# Kind of scan an object comes from: "category" in object.json. Older objects have none; it is inferred from the
# "mode" the face reconstruction writes (`infer_category`).
CATEGORIES = ("object", "face")
FACE_MODE = "mediapipe_face_mesh"


class ResultNotFoundError(LookupError):
    """The object, or the model asked for, does not exist (or is not a finished result)."""


@dataclass
class ResultEntry:
    """One finished object: its id, its `object.json` and the models present on disk."""
    id: str
    object: dict
    models: dict = field(default_factory=dict)   # {".glb": ["low", "high"], ".stl": [...]}
    category: str = "object"                      # one of CATEGORIES

    def as_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "object": self.object, "models": self.models}


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


def infer_category(obj: dict, status: Optional[dict] = None) -> str:
    """
    The category of an object: its ``category`` when it is a known one, else ``"face"`` when the face reconstruction
    made it (``mode`` ``mediapipe_face_mesh`` in object.json or status.json), else ``"object"``.
    """
    category = obj.get("category")
    if category in CATEGORIES:
        return category
    if obj.get("mode") == FACE_MODE or (status or {}).get("mode") == FACE_MODE:
        return "face"
    return "object"


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


def finished_folder(root: Path, result_id: str) -> Path:
    """
    The folder of a finished object (for the USB export).

    :raises ResultNotFoundError: the id is invalid, the folder is missing or it is not a finished result.
    """
    folder = _result_folder(root, result_id)
    if _read_entry(folder) is None:
        raise ResultNotFoundError(f"Object {result_id} is not a finished result.")
    return folder


# Fields of object.json the user may edit; everything else (created_at, job_id, units, faces_*, ...) is never touched
EDITABLE_FIELDS = ("title", "description", "dimensions_mm", "volume_cm3")
_object_write_lock = threading.Lock()


def update_object(root: Path, result_id: str, changes: dict) -> ResultEntry:
    """
    Merges `changes` (only EDITABLE_FIELDS; ``dimensions_mm`` is merged key by key) into ``object.json`` and saves it
    atomically (temporary file + rename). Returns the updated entry.

    :raises ResultNotFoundError: the id is invalid, the folder is missing or it is not a finished result.
    :raises ValueError: a field that may not be edited.
    """
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(f"These fields cannot be edited: {', '.join(unknown)}")
    folder = _result_folder(root, result_id)
    with _object_write_lock:
        get_result(root, result_id)   # a finished result only
        path = folder / "object.json"
        obj = _read_json(path) or {}
        for key, value in changes.items():
            if key == "dimensions_mm" and isinstance(value, dict):
                dims = dict(obj.get("dimensions_mm") or {})
                dims.update(value)
                obj["dimensions_mm"] = dims
            else:
                obj[key] = value
        _write_object_json(folder, obj)
    return get_result(root, result_id)


def set_category(root: Path, result_id: str, category: str) -> bool:
    """
    Writes ``category`` into the ``object.json`` of a finished object (the scanner calls it once the model is saved).
    Returns True when written; never raises (an object without it still gets one from `infer_category`).
    """
    if category not in CATEGORIES:
        log.warning("Not writing unknown category %r for %s.", category, result_id)
        return False
    try:
        folder = _result_folder(root, result_id)
        with _object_write_lock:
            obj = _read_json(folder / "object.json")
            if obj is None:
                return False
            obj["category"] = category
            _write_object_json(folder, obj)
        return True
    except (ResultNotFoundError, OSError) as e:
        log.warning("Could not write the category of %s: %s", result_id, e)
        return False


def _write_object_json(folder: Path, obj: dict) -> None:
    """Saves object.json atomically (temporary file + rename); the caller holds _object_write_lock."""
    tmp = folder / "object.json.tmp"
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / "object.json")


# Sub-folder of a result that keeps the scan photos it was built from (not served, not in the USB export)
PHOTOS_DIR = "photos"


def copy_photos(images_dir: Path, folder: Path) -> int:
    """
    Copies the scan photos (``rig/0|1/*.jpg`` and ``manifest.json``) into ``folder/photos/rig/``: the layout Turntable
    Studio reads, so the folder can be reconstructed again. Returns the number of files copied; never raises (the model is already saved: a failure is logged).
    """
    import shutil
    images_dir = Path(images_dir)
    dest = Path(folder) / PHOTOS_DIR / images_dir.name
    copied = 0
    try:
        for path in sorted(p for p in images_dir.rglob("*") if p.is_file()):
            if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".json"):
                continue
            target = dest / path.relative_to(images_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    except OSError as e:
        log.warning("Could not copy the photos of %s into %s: %s", images_dir, dest, e)
    return copied


def list_results(root: Path, offset: int = 0, limit: int = 20, category: str = "all",
                 search: Optional[str] = None) -> ResultPage:
    """
    The finished objects, newest first (the folder name starts with the date), one page of them.

    `category` (``"all"`` or one of CATEGORIES) and `search` (case-insensitive text in the title, the description or
    the id) are applied before the page is cut, so ``total`` is the number of matching objects.

    A folder that is not a finished result (no valid `object.json`, `status.json` not ``ok``, still being written) is
    skipped and logged: it never fails the whole list. A missing results folder is an empty list.
    """
    entries = []
    if root.is_dir():
        for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not folder.is_dir() or not is_valid_id(folder.name):
                continue
            entry = _read_entry(folder)
            if entry is not None and _matches(entry, category, search):
                entries.append(entry)
    offset = max(0, offset)
    return ResultPage(items=entries[offset:offset + limit], total=len(entries), offset=offset, limit=limit)


def _matches(entry: ResultEntry, category: str, search: Optional[str]) -> bool:
    if category != "all" and entry.category != category:
        return False
    needle = (search or "").strip().casefold()
    if not needle:
        return True
    haystack = (entry.id, entry.object.get("title"), entry.object.get("description"))
    return any(isinstance(text, str) and needle in text.casefold() for text in haystack)


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
    return ResultEntry(id=folder.name, object=obj, models=models, category=infer_category(obj, status))
