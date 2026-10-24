"""
Scanner Orchestrator CLI Entry-Point
====================================
Parses command-line arguments and coordinates autonomous scanning.
"""

import sys
import argparse
import logging
from pathlib import Path

# Add project root and face_scan directory to sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FACE_SCAN_DIR = Path(__file__).resolve().parent.parent
for _p in (str(_PROJECT_ROOT), str(_FACE_SCAN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from face_scan.orchestrator.orchestrator_engine import FaceScannerOrchestrator
except (ImportError, ValueError):
    from orchestrator_engine import FaceScannerOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("3DScanner.CLI")


def main():
    parser = argparse.ArgumentParser(
        description="3D Scanner Orchestrator: Dual-Camera Auto-Framing & Stereo 3D Reconstruction"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force MOCK mode for desktop/laptop simulation without GPIO/hardware",
    )
    parser.add_argument(
        "--strategy",
        choices=["sweep_best_score", "direct_stop"],
        default="sweep_best_score",
        help="Framing algorithm: 'sweep_best_score' (sweep and return to apex) or 'direct_stop' (stop on center)",
    )
    parser.add_argument(
        "--test-image",
        default="dual_photo.jpg",
        help="Path to reference test image to use in mock camera preview mode",
    )
    parser.add_argument(
        "--step-mm",
        type=float,
        default=8.0,
        help="Incremental climb step in mm (default: 8.0 mm = 800 steps)",
    )
    parser.add_argument(
        "--max-travel-mm",
        type=float,
        default=120.0,
        help="Maximum upward travel distance in mm before stopping (default: 120.0 mm)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="./output_scan",
        help="Directory to save stereo photos and resulting 3D models",
    )
    parser.add_argument(
        "--skip-3d",
        action="store_true",
        help="Skip 3D reconstruction step (photo capture & framing only)",
    )
    parser.add_argument(
        "--home",
        action="store_true",
        help="Perform initial homing sequence to bottom endstop before scanning",
    )
    parser.add_argument(
        "--upload-r2",
        action="store_true",
        help="Trigger Cloudflare R2 upload hook immediately after stereo photo capture",
    )

    args = parser.parse_args()

    orchestrator = FaceScannerOrchestrator(
        use_mock=args.mock,
        test_image_path=args.test_image,
        strategy=args.strategy,
        step_distance_mm=args.step_mm,
        max_travel_mm=args.max_travel_mm,
        output_dir=args.output_dir
    )

    try:
        if args.home:
            orchestrator.home_to_bottom()

        optimal_z = orchestrator.run_auto_framing()
        log.info("Auto-framing completed. Optimal capture Z = %.1f mm", optimal_z)

        left_img, right_img = orchestrator.capture_stereo_photo()

        # Upload stereo captures to Cloudflare R2 if requested
        if args.upload_r2:
            orchestrator.upload_to_cloudflare_r2(left_img, right_img)

        if not args.skip_3d:
            orchestrator.reconstruct_3d_face(left_img, right_img)

        log.info("\nALL OPERATIONS COMPLETED SUCCESSFULLY!")

    finally:
        orchestrator.close()


if __name__ == "__main__":
    main()
