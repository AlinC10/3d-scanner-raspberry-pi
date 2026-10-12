#!/usr/bin/env python3
"""
CLI Tool for Testing Software-Synchronized Dual Cameras (DualCameraRPISync) on Raspberry Pi 5.

Usage:
  python3 test_sync_cam.py              # Monitor continuous soft sync telemetry in real time
  python3 test_sync_cam.py --snap       # Take a synchronized stereo photo pair and exit
  python3 test_sync_cam.py --raw        # Take synchronized photos with RAW DNG files
  python3 test_sync_cam.py --duration 10 # Run live telemetry for 10 seconds and stop
"""

import argparse
import logging
import os
import sys
import time

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Safe mock fallback for running on dev machines or outside venv
for mod in ["piexif", "cv2", "smbus2"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            from unittest.mock import MagicMock
            sys.modules[mod] = MagicMock()

from hardware.dual_camera_rpi_sync import DualCameraRPISync


def main():
    parser = argparse.ArgumentParser(description="Test Software-Synchronized Dual Cameras (DualCameraRPISync)")
    parser.add_argument("--snap", action="store_true", help="Capture a synchronized photo pair")
    parser.add_argument("--raw", action="store_true", help="Also capture raw DNG files during snap")
    parser.add_argument("--duration", type=int, default=0, help="Run live monitoring for N seconds (0 = until Ctrl+C)")
    parser.add_argument("--output-dir", type=str, default="photos", help="Directory where photos will be saved")
    args = parser.parse_args()

    print("\n" + "=" * 65)
    print(" 🚀  Pornire DualCameraRPISync (Sincronizare Software Pi 5)")
    print("=" * 65)

    try:
        # Initialize DualCameraRPISync
        print("  [1/3] Inițializare camere CSI 0 și CSI 1...")
        cam = DualCameraRPISync(auto_start=True)

        print("  [2/3] Se așteaptă starea SyncReady...")
        res = cam.prepare_scan()
        print(f"        Status pregătire: Cam1 (ready={res[0]['ready']}), Cam2 (ready={res[1]['ready']})")

        if args.snap:
            print("\n  [3/3] Declanșare captură foto sincronizată...")
            os.makedirs(args.output_dir, exist_ok=True)
            t0 = time.time()
            paths = cam.capture_photo(
                output_prefix="sync_test",
                output_dir=args.output_dir,
                meshroom_rig=True,
                raw=args.raw,
            )
            elapsed = time.time() - t0
            print(f"        ✅ Fotografii salvate în {elapsed:.2f}s:")
            for p in paths:
                print(f"           - {p} (Mărime: {os.path.getsize(p) / 1024:.1f} KB)")
        else:
            print("\n  [3/3] Monitorizare telemetrie sincronizare în timp real...")
            print("        (Apasă Ctrl+C pentru a opri)\n")
            print(f"{'TIMP':<9} | {'CAM 1 SEQ':<10} | {'CAM 2 SEQ':<10} | {'OFFSET (ms)':<12} | {'FPS':<6} | {'DROPS (C1,C2)':<13}")
            print("-" * 72)

            start_time = time.time()
            while True:
                status = cam.get_sync_status()
                offset_str = f"{status['offset_ms']:+.2f}" if status["offset_ms"] is not None else "N/A"
                print(
                    f"{time.strftime('%H:%M:%S'):<9} | "
                    f"{status['cam1_seq']:<10} | "
                    f"{status['cam2_seq']:<10} | "
                    f"{offset_str:<12} | "
                    f"{status['stream_fps']:<6.1f} | "
                    f"{str(status['drops']):<13}"
                )

                if args.duration > 0 and (time.time() - start_time) >= args.duration:
                    print(f"\nDurata de {args.duration}s a expirat.")
                    break

                time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n\nOprire solicitată de utilizator (Ctrl+C)...")
    except Exception as e:
        print(f"\n❌ Eroare: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Eliberare resurse hardware și închidere pipeline...")
        if "cam" in locals() and cam is not None:
            cam.stop_stream()
            cam.close()
        print("✅ Încheiat curat.\n")


if __name__ == "__main__":
    main()
