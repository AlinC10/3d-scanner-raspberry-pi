#!/usr/bin/env python3
"""
Rig Capture Module: Autonomous Auto-Framing & Stereo Photo Capture
===================================================================
Independent module running on Raspberry Pi 5 (or mock mode on desktop)
that controls the vertical Z stepper motor, performs auto-framing,
and captures synchronized 12.3MP stereo pairs directly into input_images/rig/0 and 1.

Does NOT depend on 3D reconstruction libraries (Trimesh, Poisson, PyMeshLab, etc.).
Can run standalone on Edge RPi 5.

Usage Examples:
  # 1. Standard production capture on Raspberry Pi 5 (with homing and best sweep):
  python rig_capture.py --home --strategy sweep_best_score

  # 2. Fast capture with direct stop on face center:
  python rig_capture.py --strategy direct_stop

  # 3. Capture and trigger Cloudflare R2 upload:
  python rig_capture.py --upload-r2

  # 4. Simulation test on desktop/laptop without hardware:
  python rig_capture.py --mock --test-image dual_photo.jpg
"""

import sys
import argparse
import logging
import os
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

# Add project root to sys.path so modules can import correctly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("RigCapture")


def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Rig Capture: Auto-Framing & 12.3MP Stereo Photo Capture for 3D Face Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Directories:
  Photos are automatically saved directly into:
    input_images/rig/0/  (Camera 0 - Left physical, right cheek)
    input_images/rig/1/  (Camera 1 - Right physical, left cheek)
Ready to be processed by photogrammetry (locally on Windows PC or on RunPod Cloud).
        """
    )
    parser.add_argument(
        "--strategy",
        choices=["sweep_best_score", "direct_stop"],
        default="sweep_best_score",
        help="Auto-framing strategy: 'sweep_best_score' (full sweep and return to apex) or 'direct_stop' (stop on center)",
    )
    parser.add_argument(
        "--start-z-mm",
        type=float,
        default=180.0,
        help="Initial carriage climb height in mm before starting search (default: 180.0 mm)",
    )
    parser.add_argument(
        "--search-range-mm",
        type=float,
        default=150.0,
        help="Vertical search range in mm above start-z-mm (default: 150.0 mm)",
    )
    parser.add_argument(
        "--home",
        action="store_true",
        default=True,
        help="Perform initial homing sequence to bottom limit switch (GPIO 4) before scanning (default: True)",
    )
    parser.add_argument(
        "--no-home",
        dest="home",
        action="store_false",
        help="Skip initial homing sequence",
    )
    parser.add_argument(
        "--manual-z",
        type=float,
        default=None,
        help="Skip auto-framing and move carriage directly to a specified Z height in mm (>= 200.0 mm)",
    )
    parser.add_argument(
        "--step-mm",
        type=float,
        default=8.0,
        help="Incremental climb step in mm during auto-framing sweep (default: 8.0 mm)",
    )
    parser.add_argument(
        "--illumination",
        type=float,
        default=0.5,
        help="Chamber LED illumination PWM brightness (0.0 to 1.0, default: 0.5)",
    )
    parser.add_argument(
        "--upload-r2",
        action="store_true",
        default=True,
        help="Trigger Cloudflare R2 upload and verification (default: True)",
    )
    parser.add_argument(
        "--no-upload-r2",
        dest="upload_r2",
        action="store_false",
        help="Disable Cloudflare R2 upload",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force MOCK hardware mode for desktop simulation without GPIO/hardware",
    )

    args = parser.parse_args()

    log.info("=" * 60)
    log.info("INITIALIZING AUTONOMOUS RIG CAPTURE MODULE")
    log.info("  • Strategy: %s", args.strategy)
    log.info("  • Hardware Mode: %s", "MOCK (Desktop Simulation)" if args.mock else "REAL HARDWARE (RPi 5)")
    log.info("  • Homing: %s", args.home)
    log.info("  • Upload R2: %s", args.upload_r2)
    log.info("=" * 60)

    if args.mock or os.environ.get("MOCK_HARDWARE") == "1":
        from hardware.scanner_mock import ScannerMock as Scanner
    else:
        try:
            from hardware.scanner import Scanner
        except ImportError:
            log.warning("Hardware drivers not found. Falling back to ScannerMock.")
            from hardware.scanner_mock import ScannerMock as Scanner

    scanner = Scanner()
    try:
        result = scanner.face_scan(
            strategy=args.strategy,
            start_z_mm=args.start_z_mm,
            search_range_mm=args.search_range_mm,
            step_mm=args.step_mm,
            manual_z=args.manual_z,
            home=args.home,
            upload_r2=args.upload_r2,
            illumination_brightness=args.illumination,
        )

        log.info("=" * 60)
        log.info("FACE CAPTURE RESULT: %s", result.get("status"))
        log.info("  • Optimal Z: %s mm", result.get("optimal_z_mm"))
        log.info("  • Best Score: %s", result.get("best_score"))
        log.info("  • Photos: %s", result.get("photos"))
        log.info("  • R2 Uploaded: %s", result.get("r2_uploaded"))
        log.info("=" * 60)
    finally:
        scanner.close_hardware()


if __name__ == "__main__":
    main()
