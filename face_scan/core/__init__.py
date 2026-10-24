"""
Core Vision & Facial Analysis Package
=====================================
"""

import sys
from pathlib import Path

# Add project root and face_scan directory to sys.path so modules resolve in any environment
_FACE_SCAN_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _FACE_SCAN_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_FACE_SCAN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from face_scan.face_scanner import FaceScanner
except (ImportError, ValueError):
    from face_scanner import FaceScanner

from .framing_analyzer import StereoFramingAnalyzer

__all__ = ["FaceScanner", "StereoFramingAnalyzer"]
