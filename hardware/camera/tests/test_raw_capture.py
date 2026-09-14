#!/usr/bin/env python3
"""Compare normal JPEG and JPEG+DNG captures under identical controls.

Run on the Raspberry Pi:
    python3 test_raw_capture.py --camera 0
    python3 test_raw_capture.py --camera 1 --output /tmp/raw-check

The script captures both files through ArducamIMX477, using the same locked
AE/AWB values. It reports image dimensions, file sizes, pixel statistics, and
DNG metadata when rawpy is installed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from camera import ArducamIMX477


ROOT = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare normal JPEG and JPEG+DNG captures"
    )
    parser.add_argument("--camera", type=int, choices=(0, 1), required=True)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "test-output" / "raw-comparison",
    )
    parser.add_argument("--width", type=int, default=2800)
    parser.add_argument("--height", type=int, default=2100)
    parser.add_argument("--quality", type=int, default=95)
    parser.add_argument("--settle-time", type=float, default=3.0)
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0:
        parser.error("width and height must be positive")
    if not 0 <= args.quality <= 100:
        parser.error("quality must be between 0 and 100")
    if args.settle_time < 0:
        parser.error("settle-time cannot be negative")
    return args


def image_report(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"OpenCV could not decode {path}")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "shape": list(image.shape),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "mean": float(image.mean()),
        "minimum": int(image.min()),
        "maximum": int(image.max()),
        "saturated_fraction": float(np.mean(image >= 250)),
    }


def raw_report(path: Path):
    report = {
        "path": str(path),
        "bytes": path.stat().st_size,
    }
    try:
        import rawpy
    except ImportError:
        report["rawpy"] = "not installed; DNG decode not attempted"
        return report

    with rawpy.imread(str(path)) as raw:
        raw_data = raw.raw_image_visible
        report.update({
            "rawpy": "decoded",
            "raw_shape": list(raw_data.shape),
            "raw_dtype": str(raw_data.dtype),
            "raw_minimum": int(raw_data.min()),
            "raw_maximum": int(raw_data.max()),
            "raw_mean": float(raw_data.mean()),
        })
        rgb = raw.postprocess(use_camera_wb=True, output_bps=16)
        report.update({
            "developed_shape": list(rgb.shape),
            "developed_mean": float(rgb.mean()),
            "developed_minimum": int(rgb.min()),
            "developed_maximum": int(rgb.max()),
        })
        developed_path = path.with_name(f"{path.stem}_developed.jpg")
        developed = np.clip(rgb / 256, 0, 255).astype(np.uint8)
        cv2.imwrite(str(developed_path), cv2.cvtColor(developed, cv2.COLOR_RGB2BGR))
        report["developed_jpeg"] = str(developed_path)
    return report


def main():
    args = parse_args()
    run_dir = (
        args.output
        / f"camera{args.camera}"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    resolution = (args.width, args.height)
    normal_path = run_dir / "normal.jpg"
    raw_jpeg_path = run_dir / "raw.jpg"

    print(f"Camera: {args.camera}")
    print(f"Output: {run_dir}")
    print(f"Resolution: {resolution}")

    with ArducamIMX477(camera_id=args.camera, quality=args.quality) as camera:
        camera._configure_preview((1280, 720))
        camera.picam2.start()
        try:
            time.sleep(args.settle_time)
            locked = camera.lock_auto_features(settle_time=0.0)
            before_capture = camera.picam2.capture_metadata()
            print(f"Locked controls: {locked}")
            print(f"Capture metadata: {before_capture}")
        finally:
            camera.picam2.stop()

        print("Capturing normal JPEG...")
        camera.capture_photo(
            output=str(normal_path),
            resolution=resolution,
            quality=args.quality,
            raw=False,
        )

        print("Capturing JPEG+DNG...")
        camera.capture_photo(
            output=str(raw_jpeg_path),
            resolution=resolution,
            quality=args.quality,
            raw=True,
        )

    dng_path = raw_jpeg_path.with_suffix(".dng")
    result = {
        "camera": args.camera,
        "resolution_requested": list(resolution),
        "normal_jpeg": image_report(normal_path),
        "raw_jpeg": image_report(raw_jpeg_path),
        "dng": raw_report(dng_path),
    }
    report_path = run_dir / "comparison.json"
    report_path.write_text(json.dumps(result, indent=2) + "\n")

    print("\nNormal JPEG:")
    print(json.dumps(result["normal_jpeg"], indent=2))
    print("\nJPEG from raw request:")
    print(json.dumps(result["raw_jpeg"], indent=2))
    print("\nDNG:")
    print(json.dumps(result["dng"], indent=2))
    print(f"\nReport: {report_path}")

    mean_difference = abs(
        result["normal_jpeg"]["mean"] - result["raw_jpeg"]["mean"]
    )
    print(f"Mean brightness difference: {mean_difference:.2f}")
    if mean_difference > 20:
        print("WARNING: the JPEG outputs differ significantly in brightness.")
    else:
        print("JPEG brightness comparison is within the diagnostic threshold.")


if __name__ == "__main__":
    main()
