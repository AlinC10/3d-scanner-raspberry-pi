#!/usr/bin/env python3
"""
Test script for the DualCamera class.
This script demonstrates how to initialize two cameras, configure them concurrently,
adjust focus, and take a synchronized photo.
"""

import time
import logging
from dual_camera import DualCamera

# Setup basic logging to see the output in the console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

def run_test():
    log.info("Starting DualCamera test script...")

    # The 'with' block ensures that dual_cam.close() is called automatically at the end.
    # By default, this looks for cameras on CSI ports 0 and 1.
    with DualCamera(camera_id_1=0, camera_id_2=1, quality=95) as dual_cam:
        
        # 1. Prepare Scan (Launch Preview & Lock AE/AWB)
        # show_preview=True launches the preview window for each camera.
        # keep_running=True ensures the camera hardware and preview stay active after preparation.
        log.info("Preparing cameras, launching preview, and locking AE/AWB...")
        prep_results = dual_cam.prepare_scan(show_preview=True, keep_running=True, settle_time=2.0)
        log.info("Preparation complete. Locked controls for both cameras:")
        for i, res in enumerate(prep_results):
            log.info(f"  Camera {i}: {res.get('controls')}")

        # Let the user see the preview for a few seconds before capturing
        log.info("Preview is now running. Waiting 3 seconds before taking a photo...")
        time.sleep(3.0)

        # 2. Test Focus Movement
        log.info("Testing concurrent focus adjustment (step +50)...")
        dual_cam.focus_step(50)
        time.sleep(1) # Let the lens settle physically while previewing

        # 3. Capture Synchronized Photos WHILE Preview is active
        # Because we used keep_running=True in prepare_scan, the capture_photo 
        # method will reuse the already-running camera without stopping the preview!
        log.info("Capturing synchronized photos (Preview should remain open!)...")
        photo_paths = dual_cam.capture_photo(
            output_prefix="test_dual",
            raw=False
        )
        log.info(f"Photos successfully saved to: {photo_paths}")

        # Let the user see that the preview is still alive after the capture
        log.info("Waiting 3 more seconds to show that the preview is still active...")
        time.sleep(3.0)

        # 4. Optional: Reset focus before exiting
        log.info("Resetting focus to infinity...")
        dual_cam.focus_reset()
        
    log.info("Test completed successfully! The cameras have been safely closed.")

if __name__ == "__main__":
    run_test()
