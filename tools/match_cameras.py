#!/usr/bin/env python3
"""
Calibrates camera 1 so that its image of a neutral target looks like camera 0's.

Camera 0 is the reference: it keeps the settings you give (shutter, gain, colour gains). Camera 1 is shot with the
same shutter, and its analogue gain and colour gains (red, blue) are adjusted over a few iterations until the mean
linear R, G, B of a patch of the target match camera 0's.

Setup: stop the scanner server (the cameras must be free), switch the LEDs on at the brightness used for scanning, and
lay a MATTE white or grey sheet flat on the turntable so that it fills the centre of BOTH views (a matte surface has
the same radiance from every direction, so the two cameras see the same colour and brightness).

    python3 tools/match_cameras.py --shutter 22991 --gain 1.5 --awbgains 3.15,1.49

The result is printed as the settings for camera 1 and as the correction factors relative to camera 0
(gain_factor, red_factor, blue_factor), which is what the scanner would multiply onto the values it locks on camera 0.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

GAIN_MIN, GAIN_MAX = 1.0, 16.0
COLOUR_GAIN_MIN, COLOUR_GAIN_MAX = 0.5, 8.0


def to_linear(srgb: np.ndarray) -> np.ndarray:
    """JPEG values (0-255) to approximately linear light (0-1). The ISP tone curve is not exactly sRGB; the
    iteration absorbs the remaining difference."""
    v = srgb / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def patch_means(path: str, roi: float) -> np.ndarray:
    """Mean linear (R, G, B) of the centre patch covering `roi` (0-1) of each side."""
    a = np.asarray(Image.open(path).convert("RGB"), dtype=float)
    h, w, _ = a.shape
    y0, y1 = int(h * (0.5 - roi / 2)), int(h * (0.5 + roi / 2))
    x0, x1 = int(w * (0.5 - roi / 2)), int(w * (0.5 + roi / 2))
    return to_linear(a[y0:y1, x0:x1].reshape(-1, 3)).mean(axis=0)


def next_settings(ref: np.ndarray, cur: np.ndarray, gain: float, red: float, blue: float):
    """One correction step: scales the gain so G matches, then the red/blue gains so R/G and B/G match."""
    gain_new = float(np.clip(gain * ref[1] / cur[1], GAIN_MIN, GAIN_MAX))
    red_new = float(np.clip(red * (ref[0] / ref[1]) / (cur[0] / cur[1]), COLOUR_GAIN_MIN, COLOUR_GAIN_MAX))
    blue_new = float(np.clip(blue * (ref[2] / ref[1]) / (cur[2] / cur[1]), COLOUR_GAIN_MIN, COLOUR_GAIN_MAX))
    return gain_new, red_new, blue_new


def shoot(camera: int, shutter: int, gain: float, red: float, blue: float, out: str, tuning: str | None) -> None:
    cmd = [
        "rpicam-still", "--camera", str(camera), "--shutter", str(shutter), "--gain", f"{gain:.4f}",
        "--awbgains", f"{red:.4f},{blue:.4f}", "--denoise", "off", "-t", "1500", "-n", "-o", out,
    ]
    if tuning:
        cmd += ["--tuning-file", tuning]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shutter", type=int, required=True, help="exposure time in microseconds (both cameras)")
    ap.add_argument("--gain", type=float, required=True, help="analogue gain of camera 0 (the reference)")
    ap.add_argument("--awbgains", required=True, help="colour gains of camera 0 as red,blue")
    ap.add_argument("--roi", type=float, default=0.2, help="side of the centre patch as a fraction of the image (0.2)")
    ap.add_argument("--iterations", type=int, default=4)
    ap.add_argument("--tolerance", type=float, default=0.01, help="stop when G, R/G and B/G match within this fraction")
    ap.add_argument("--tuning-file", default=None, help="tuning file for both cameras (default: libcamera's)")
    ap.add_argument("--ref", type=int, default=0, choices=(0, 1), help="reference camera (default 0)")
    args = ap.parse_args()

    ref_cam, cam = args.ref, 1 - args.ref
    red0, blue0 = (float(v) for v in args.awbgains.split(","))
    tmp = Path(tempfile.mkdtemp(prefix="match_cameras_"))

    ref_path = str(tmp / "ref.jpg")
    shoot(ref_cam, args.shutter, args.gain, red0, blue0, ref_path, args.tuning_file)
    ref = patch_means(ref_path, args.roi)
    print(f"camera {ref_cam} (reference): linear R={ref[0]:.4f} G={ref[1]:.4f} B={ref[2]:.4f}  "
          f"R/G={ref[0] / ref[1]:.3f} B/G={ref[2] / ref[1]:.3f}")
    if ref[1] > 0.9 or ref[1] < 0.05:
        print("WARNING: the reference patch is nearly clipped or very dark; change shutter/gain or the target.")

    gain, red, blue = args.gain, red0, blue0
    for i in range(1, args.iterations + 1):
        path = str(tmp / f"cam{cam}_{i}.jpg")
        shoot(cam, args.shutter, gain, red, blue, path, args.tuning_file)
        cur = patch_means(path, args.roi)
        err = max(abs(cur[1] / ref[1] - 1), abs((cur[0] / cur[1]) / (ref[0] / ref[1]) - 1),
                  abs((cur[2] / cur[1]) / (ref[2] / ref[1]) - 1))
        print(f"  iteration {i}: gain={gain:.3f} awbgains={red:.3f},{blue:.3f}  "
              f"G={cur[1]:.4f} R/G={cur[0] / cur[1]:.3f} B/G={cur[2] / cur[1]:.3f}  error={err * 100:.1f}%")
        if err <= args.tolerance:
            break
        gain, red, blue = next_settings(ref, cur, gain, red, blue)

    if gain in (GAIN_MIN, GAIN_MAX):
        print(f"WARNING: camera {cam} reached the analogue gain limit ({gain}); use a longer shutter.")
    result = {
        "reference_camera": ref_cam,
        "camera": cam,
        "shutter_us": args.shutter,
        "analogue_gain": round(gain, 4),
        "colour_gains": [round(red, 4), round(blue, 4)],
        "gain_factor": round(gain / args.gain, 4),
        "red_factor": round(red / red0, 4),
        "blue_factor": round(blue / blue0, 4),
    }
    print("\nSettings for camera", cam, "so that it matches camera", ref_cam, ":")
    print(json.dumps(result, indent=2))
    print(f"\nImages kept in {tmp} (check them by eye, then re-run once with these values to confirm).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
