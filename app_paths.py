"""Where the application finds its files, independent of the directory the server is started from."""
import os
from pathlib import Path
from typing import Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ENV = "FRONTEND_DIR"
DEFAULT_FRONTEND_DIR = PROJECT_ROOT.parent / "3d-scanner-front-end"
# Photos taken by POST /test/camera/snap, kept apart from the scan photos (input_images/)
TEST_SNAPS_DIR = PROJECT_ROOT / "test_snaps"


def frontend_directory(environ: Optional[Mapping[str, str]] = None) -> Path:
    """
    The front-end build/sources served at "/": the FRONTEND_DIR environment variable when set, otherwise the
    `3d-scanner-front-end` folder next to the project folder. The path never depends on the current directory
    (it used to be "../3d-scanner-front-end", which broke when the server was started from another folder).
    """
    environ = os.environ if environ is None else environ
    configured = (environ.get(FRONTEND_ENV) or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_FRONTEND_DIR
