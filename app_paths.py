"""Where the application finds its files, independent of the directory the server is started from."""
import os
from pathlib import Path
from typing import Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ENV = "FRONTEND_DIR"
DEFAULT_FRONTEND_DIR = PROJECT_ROOT.parent / "3d-scanner-front-end"
# Photos taken by POST /test/camera/snap, kept apart from the scan photos (input_images/)
TEST_SNAPS_DIR = PROJECT_ROOT / "test_snaps"
# Documents the AI assistant answers from (served read-only at /ai-docs)
AI_DOCS_DIR = PROJECT_ROOT / "ai" / "ai-docs"
# Connections made by the user, made again when the server starts (system/network_state.py); no passwords in it
NETWORK_STATE_FILE = PROJECT_ROOT / "state" / "network.json"
# 3D models produced by Turntable Studio, one folder per object (listed by /library)
RESULTS_ENV = "RESULTS_DIR"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "results"


def results_directory(environ: Optional[Mapping[str, str]] = None) -> Path:
    """
    The folder of the generated models served by `/library`: the RESULTS_DIR environment variable when set, otherwise
    `results/` in the project folder.
    """
    environ = os.environ if environ is None else environ
    configured = (environ.get(RESULTS_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_RESULTS_DIR


def frontend_directory(environ: Optional[Mapping[str, str]] = None) -> Path:
    """
    The front-end build/sources served at "/": the FRONTEND_DIR environment variable when set, otherwise the
    `3d-scanner-front-end` folder next to the project folder. The path never depends on the current directory
    (it used to be "../3d-scanner-front-end", which broke when the server was started from another folder).
    """
    environ = os.environ if environ is None else environ
    configured = (environ.get(FRONTEND_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_FRONTEND_DIR
