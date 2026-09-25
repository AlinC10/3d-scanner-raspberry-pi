from fastapi import APIRouter, HTTPException, BackgroundTasks
import logging
from schemas.scanner import *

from hardware.scanner import Scanner, ScannerState
import cloud.runpod as runpod
import cloud.cloudflare_r2 as r2

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scanner",
    tags=["Scanner"],
)

# Step 13: Singleton Hardware State
# The scanner is initialized once and kept in memory as a singleton across the FastAPI application lifecycle.
scanner = Scanner()

@router.post("/prepare")
def prepare_scan(req: PrepareRequest):
    """
    Safely triggers the homing, lighting, and camera initializations for the frontend framing.
    """
    try:
        url = scanner.prepare_scan(
            enable_stream=req.enable_stream,
            bitrate=req.bitrate
        )
        return {"status": "success", "stream_url": url, "state": scanner.state}
    except RuntimeError as e:
        # State validation failures
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Hardware errors, S3 bucket cleanup errors, etc.
        raise HTTPException(status_code=500, detail=str(e))

def _orchestrate_pipeline(req: StartRequest):
    """
    Background worker that runs the mechanical scan and cloud pipeline synchronously.
    Updates the global scanner.state natively so the frontend polling loop is always accurate.
    """
    try:
        # 1. Mechanical Scan
        scanner.scan(
            angle=req.mechanical.angle,
            step_type=req.mechanical.turntable.step_type,
            delay_turntable=req.mechanical.turntable.delay,
            z_move_mm=req.mechanical.z_move_mm,
            z_step_type=req.mechanical.z_axis.step_type,
            delay_z_motor=req.mechanical.z_axis.delay
        )
        
        # Check if it was cancelled during the scan
        if scanner.state == ScannerState.CANCELLED:
            log.info("Scan was cancelled, aborting cloud pipeline.")
            return

        # Check if the upload worker failed or stalled
        if scanner.state == ScannerState.ERROR:
            log.error("Scan ended in error state (e.g. stalled uploads). Aborting cloud pipeline.")
            return

        # 2. Cloud Processing
        scanner.state = ScannerState.PROCESSING
        
        job = {
            "job_id": req.job_id,
            "photo_count": scanner.total_photos,
            "resolution_mp": req.cloud.resolution_mp,
            "mode": req.cloud.mode,
            "depthmap_downscale": req.cloud.depthmap_downscale,
            "max_input_points": req.cloud.max_input_points
        }
        
        success = runpod.launch_job(job, cancel_event=scanner._cancel_event)
        
        if scanner._cancel_event.is_set():
            scanner.state = ScannerState.CANCELLED
            log.info("Job cancelled during cloud processing.")
            return
            
        if not success:
            raise RuntimeError("RunPod processing failed.")
            
        # 3. Downloading Assets
        scanner.state = ScannerState.DOWNLOADING
        # R2 download_generated_obj defaults to dest_dir=os.getcwd() if not provided, but let's be explicit
        success = r2.download_generated_obj(object_name="output.zip", dest_dir=r2.OUTPUT_DIR)
        
        if not success:
            raise RuntimeError("Failed to download or extract output.zip from R2.")
            
        # 4. Completed!
        scanner.state = ScannerState.COMPLETED
        log.info("Full pipeline completed successfully.")
        
    except Exception as e:
        log.error("Pipeline failed: %s", e)
        if scanner.state != ScannerState.CANCELLED:
            scanner.state = ScannerState.ERROR

@router.post("/start", status_code=202)
def start_scan(req: StartRequest, background_tasks: BackgroundTasks):
    """
    Delegates the mechanical scan and cloud execution to a background pipeline.
    """
    if scanner.state != ScannerState.PREPARED:
        raise HTTPException(status_code=400, detail=f"Cannot start scan from state {scanner.state}. Must be PREPARED.")
        
    background_tasks.add_task(_orchestrate_pipeline, req)
    return {"status": "accepted", "message": "Pipeline started."}

@router.post("/cancel")
def cancel_scan():
    """
    Instantly stops all mechanical movement and terminates any running cloud pods.
    """
    try:
        scanner.emergency_stop()
        return {"status": "success", "state": scanner.state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/status")
def get_status():
    """
    Poll this endpoint to observe the active pipeline state.
    """
    return {
        "state": scanner.state,
        "total_photos": getattr(scanner, "total_photos", 0)
    }