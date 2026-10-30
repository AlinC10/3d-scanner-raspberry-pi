"""
Local 3D reconstruction of a face stereo pair on the Pi (FACE-20), with ``rpi_face_mesh/reconstruct_3d.py``.

The reconstruction runs in a child process (its own memory, its own MediaPipe instance, killed on a cancel or after
FACE_RECONSTRUCT_TIMEOUT_S). It writes into ``results/.<result_id>.partial/``, which the library ignores; only a
finished model is renamed to ``results/<result_id>/`` with ``status.json`` ``{"state": "ok"}``, so ``/library``
never lists a half-written face.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

FACE_MESH_DIR = Path(__file__).resolve().parent.parent / "rpi_face_mesh"
FACE_RECONSTRUCT_TIMEOUT_S = float(os.environ.get("FACE_RECONSTRUCT_TIMEOUT_S", "300"))
# Interpreter of the child process (default: the server's own). The bundle is documented for Python 3.9-3.12.
FACE_RECONSTRUCT_PYTHON = os.environ.get("FACE_RECONSTRUCT_PYTHON") or sys.executable

# Lines printed by reconstruct_3d.py -> the phase message shown in /scanner/status
_PROGRESS = (
    ("Scanning stereo face pair", "Detecting face landmarks..."),
    ("Deforming 3D facial geometry", "Computing 3D geometry & depth..."),
    ("Baking", "Baking texture..."),
    ("Preparing clean PBR textures", "Exporting models (GLB, STL, OBJ)..."),
)


class FaceReconstructionError(Exception):
    """The reconstruction failed, timed out or was cancelled (the message says which)."""


def reconstruct_face(
    left: str,
    right: str,
    results_root: Path,
    result_id: str,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    title: Optional[str] = None,
) -> dict:
    """
    Builds the face model of one stereo pair into ``results_root/<result_id>/`` (obj/, glb/, stl/, 3mf/,
    object.json, status.json, reconstruct_log.txt).

    :param left: Photo of camera 0 (``rig/0/<name>.jpg``).
    :param right: Photo of camera 1 (``rig/1/<name>.jpg``).
    :param results_root: The library folder (``app_paths.results_directory()``).
    :param result_id: Folder name; must match the library id pattern to be listed.
    :param cancel_event: Set -> the child process is killed and FaceReconstructionError raised.
    :param on_progress: Called with a human readable step ("Detecting face landmarks...", ...).
    :param title: Written to object.json (default "Face <date time>").
    :return: The content of object.json (dimensions_mm, interpupillary_distance_mm, ...).
    :raises FaceReconstructionError: the reconstruction did not produce a model.
    """
    say = on_progress or (lambda _msg: None)
    script = FACE_MESH_DIR / "reconstruct_3d.py"
    if not script.is_file():
        raise FaceReconstructionError(f"The face mesh bundle is missing: {script}")
    # The child runs in rpi_face_mesh/ (its imports are relative to it): every path it gets must be absolute
    left, right = str(Path(left).resolve()), str(Path(right).resolve())
    results_root = Path(results_root).resolve()
    final_dir = results_root / result_id
    work_dir = results_root / f".{result_id}.partial"
    if final_dir.exists():
        raise FaceReconstructionError(f"The result folder {final_dir} already exists.")
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    cmd = [FACE_RECONSTRUCT_PYTHON, "-u", str(script), "-l", str(left), "-r", str(right),
           "-o", str(work_dir), "--name", "model"]
    log.info("[FaceReconstruct] %s", " ".join(cmd))
    say("Starting the face reconstruction...")
    started = time.monotonic()
    log_lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=str(FACE_MESH_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except OSError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise FaceReconstructionError(f"Could not start the face reconstruction: {e}") from e

    def _read_output() -> None:
        for line in proc.stdout:
            line = line.rstrip()
            log_lines.append(line)
            for marker, message in _PROGRESS:
                if marker in line:
                    say(message)
                    break

    reader = threading.Thread(target=_read_output, name="face-reconstruct-log", daemon=True)
    reader.start()
    failure = None
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            failure = "The face reconstruction was cancelled."
        elif time.monotonic() - started > FACE_RECONSTRUCT_TIMEOUT_S:
            failure = f"The face reconstruction took longer than {FACE_RECONSTRUCT_TIMEOUT_S:.0f} s."
        if failure:
            proc.kill()
            break
        time.sleep(0.2)
    proc.wait()
    reader.join(timeout=5.0)
    try:
        (work_dir / "reconstruct_log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    except OSError:
        pass

    info = None
    if failure is None:
        if proc.returncode != 0:
            tail = " | ".join(log_lines[-3:])
            failure = f"The face reconstruction failed (exit code {proc.returncode}): {tail}"
        else:
            try:
                info = json.loads((work_dir / "object.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                failure = f"The face reconstruction wrote no valid object.json: {e}"
    if failure:
        log.error("[FaceReconstruct] %s (log kept in %s)", failure, work_dir)
        raise FaceReconstructionError(failure)

    info["title"] = title or time.strftime("Face %Y-%m-%d %H:%M")
    (work_dir / "object.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (work_dir / "status.json").write_text(json.dumps({
        "state": "ok", "mode": "mediapipe_face_mesh", "elapsed_s": round(time.monotonic() - started, 1),
    }, indent=2), encoding="utf-8")
    os.replace(work_dir, final_dir)
    log.info("[FaceReconstruct] Face model saved in %s (%.1f s).", final_dir, time.monotonic() - started)
    return info
