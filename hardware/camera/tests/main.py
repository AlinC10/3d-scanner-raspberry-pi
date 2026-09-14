#!/usr/bin/env python3
"""
Arducam IMX477 B0272 Motorized Focus Camera Controller
Raspberry Pi 5 — Trixie OS (picamera2 + smbus2)

Hardware setup in /boot/firmware/config.txt:
    camera_auto_detect=0
    dtoverlay=imx477,vcm

Install dependencies:
    sudo apt install -y python3-picamera2 python3-smbus2 python3-opencv

Usage:
    python3 arducam_imx477_controller.py --help
"""

import argparse
import time
import logging

from camera import ArducamIMX477
from config import DEFAULT_QUALITY
from config.vcm import VCM_MAX_POS


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Arducam IMX477 B0272 Motorized Camera — Raspberry Pi 5",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # shared args helper
    def add_common(sp):
        sp.add_argument("--camera",  type=int, default=0,          help="Camera index (default: 0)")
        sp.add_argument("--verbose", action="store_true",           help="Enable debug logging")
        sp.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                        help=f"JPEG/Video quality 0–100 (default: {DEFAULT_QUALITY})")
        sp.add_argument("--rotation", type=int, default=0,
                        help="Image rotation: 0, 90, 180, or 270 degrees (default: 0)")

    # ── photo ──────────────────────────────────────────────────────────────
    ph = sub.add_parser("photo", help="Capture a JPEG still photo")
    ph.add_argument("-o", "--output",    default=None,  help="Output file (default: auto-named)")
    ph.add_argument("--raw",             action="store_true", help="Also save DNG raw file")
    ph.add_argument("--width",  type=int, default=4056, help="Capture width  (default: 4056)")
    ph.add_argument("--height", type=int, default=3040, help="Capture height (default: 3040)")
    ph.add_argument("--focus",  type=int, default=None, help="VCM position 0-1023 (skip=current)")
    ph.add_argument("--autofocus",    action="store_true", help="Software sweep autofocus before capture")
    # ph.add_argument("--hw-autofocus", action="store_true", help="libcamera HW autofocus before capture")
    ph.add_argument("--preview",      action="store_true", help="Show 2-second preview before capture")
    ph.add_argument("--exposure",   type=int,   default=None, help="Exposure time in microseconds")
    ph.add_argument("--gain",       type=float, default=None, help="Analogue gain (1.0–16.0)")
    ph.add_argument("--brightness", type=float, default=0.0,  help="Brightness (-1.0 to 1.0)")
    ph.add_argument("--contrast",   type=float, default=1.0,  help="Contrast (0.0 to 2.0)")
    ph.add_argument("--awb",        type=str,   default="auto", help="AWB mode (auto, incandescent, daylight, etc)")
    add_common(ph)

    # ── video ──────────────────────────────────────────────────────────────
    vi = sub.add_parser("video", help="Record an H.264/MP4 video")
    vi.add_argument("-o", "--output",    default=None, help="Output .mp4 file (default: auto-named)")
    vi.add_argument("-d", "--duration",  type=float, default=10.0, help="Duration in seconds (default: 10)")
    vi.add_argument("--width",  type=int, default=1920, help="Width  (default: 1920)")
    vi.add_argument("--height", type=int, default=1080, help="Height (default: 1080)")
    vi.add_argument("--focus",   type=int, default=None, help="VCM position before recording")
    add_common(vi)

    # ── preview ────────────────────────────────────────────────────────────
    pv = sub.add_parser("preview", help="Interactive live preview with focus control")
    pv.add_argument("--width",  type=int, default=1920)
    pv.add_argument("--height", type=int, default=1080)
    add_common(pv)

    # ── focus ──────────────────────────────────────────────────────────────
    fo = sub.add_parser("focus", help="Control or inspect focus position")
    fo.add_argument("--set",   type=int, default=None, help="Set VCM to absolute position (0-1023)")
    fo.add_argument("--step",  type=int, default=None, help="Step VCM by this delta")
    fo.add_argument("--reset", action="store_true",    help="Reset focus to infinity (pos=0)")
    fo.add_argument("--auto",  action="store_true",    help="Run software sweep autofocus")
    # fo.add_argument("--hw",    action="store_true",    help="Run libcamera HW autofocus")
    add_common(fo)

    # ── info ───────────────────────────────────────────────────────────────
    info = sub.add_parser("info", help="Print camera properties and current state")
    add_common(info)

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)

    camera_idx = getattr(args, "camera",  0)
    quality    = getattr(args, "quality", DEFAULT_QUALITY)
    rotation   = getattr(args, "rotation", 0)

    with ArducamIMX477(
        camera_id=camera_idx,
        quality=quality,
        rotation=rotation,
    ) as cam:

        # ── photo ─────────────────────────────────────────────────────────
        if args.command == "photo":
            cam.apply_settings(
                exposure_time=args.exposure,
                analogue_gain=args.gain,
                brightness=args.brightness,
                contrast=args.contrast,
                awb_mode=args.awb,
            )
            if args.focus is not None:
                cam.focus_set(args.focus)
            elif args.autofocus:
                cam._configure_preview((1280, 720))
                cam.picam2.start()
                cam.focus_sweep_autofocus()
                cam.picam2.stop()
            cam.capture_photo(
                output=args.output,
                resolution=(args.width, args.height),
                quality=args.quality,
                raw=args.raw,
                show_preview=args.preview,
            )

        # ── video ─────────────────────────────────────────────────────────
        elif args.command == "video":
            if args.focus is not None:
                cam.focus_set(args.focus)
            cam.record_video(
                output=args.output,
                duration=args.duration,
                resolution=(args.width, args.height),
                quality=args.quality,
            )

        # ── preview ───────────────────────────────────────────────────────
        elif args.command == "preview":
            cam.interactive_preview(resolution=(args.width, args.height))

        # ── focus ─────────────────────────────────────────────────────────
        elif args.command == "focus":
            if args.reset:
                cam.focus_reset()
            elif args.set is not None:
                cam.focus_set(args.set)
            elif args.step is not None:
                cam.focus_step(args.step)
            elif args.auto:
                cam._configure_preview((1280, 720))
                cam.picam2.start()
                best = cam.focus_sweep_autofocus()
                cam.picam2.stop()
                print(f"Best focus position: {best}")
            else:
                print(f"Current VCM position: {cam.focuser.position} / {VCM_MAX_POS}")

        # ── info ──────────────────────────────────────────────────────────
        elif args.command == "info":
            cam._configure_preview()
            cam.picam2.start()
            time.sleep(1)
            cam._print_info()
            cam.picam2.stop()


if __name__ == "__main__":
    main()
