from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from typing import Optional, Annotated
import logging
from schemas.scanner import *

import os
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE") == "1"

if MOCK_HARDWARE:
    from hardware.scanner_mock import ScannerMock as Scanner
    from hardware.scanner_mock import ScannerState
else:
    from hardware.scanner import Scanner, ScannerState
import cloud.runpod as runpod
import cloud.cloudflare_r2 as r2

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/scanner",
    tags=["Scanner"],
)

# The scanner is initialized once and kept in memory as a singleton across the FastAPI application lifecycle.
import os
MOCK_HARDWARE = os.environ.get("MOCK_HARDWARE") == "1"

if MOCK_HARDWARE:
    from hardware.scanner_mock import ScannerMock
    scanner = ScannerMock(xvs=False)
else:
    scanner = Scanner(xvs=False) if not MOCK_HARDWARE else Scanner(xvs=False)

def _finish_preparation_worker(kwargs: dict):
    scanner.finish_preparation(**kwargs)

@router.post("/prepare", response_model=PrepareResponse, status_code=202)
def prepare_scan(req: PrepareRequest, background_tasks: BackgroundTasks):
    """
    Safely triggers the homing, lighting, and camera initializations for the frontend framing.
    """
    try:
        scanner.lights.set_brightness(req.illumination_brightness)
        
        urls = scanner.setup_and_stream(
            enable_stream=req.enable_stream,
            bitrate=req.bitrate,
            **req.camera_config.model_dump()
        )
        
        background_tasks.add_task(_finish_preparation_worker, req.camera_config.model_dump())
        
        return PrepareResponse(
            status="accepted", 
            message="Live stream started. Rig homing and camera tuning in progress.", 
            state=scanner.state, 
            stream_urls=urls
        )
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
            angle=req.mechanical.turntable.angle,
            delay_turntable=req.mechanical.turntable.delay,
            z_move_mm=req.mechanical.z_axis.distance_mm,
            delay_z_motor=req.mechanical.z_axis.delay,
            turntable_acceleration=req.mechanical.turntable.acceleration,
            turntable_start_delay=req.mechanical.turntable.start_delay,
            turntable_ramp_percent=req.mechanical.turntable.ramp_percent,
            turntable_decel_percent=req.mechanical.turntable.decel_percent,
            z_acceleration=req.mechanical.z_axis.acceleration,
            z_start_delay=req.mechanical.z_axis.start_delay,
            z_ramp_percent=req.mechanical.z_axis.ramp_percent,
            z_decel_percent=req.mechanical.z_axis.decel_percent
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

@router.post("/start", response_model=StartResponse, status_code=202)
def start_scan(req: StartRequest, background_tasks: BackgroundTasks):
    """
    Delegates the mechanical scan and cloud execution to a background pipeline.
    """
    if scanner.state != ScannerState.PREPARED:
        raise HTTPException(status_code=400, detail=f"Cannot start scan from state {scanner.state}. Must be PREPARED.")
        
    background_tasks.add_task(_orchestrate_pipeline, req)
    return StartResponse(status="accepted", message="Pipeline started.")

@router.post("/cancel", response_model=CancelResponse)
def cancel_scan():
    """
    Instantly stops all mechanical movement and terminates any running cloud pods.
    """
    try:
        scanner.emergency_stop()
        return CancelResponse(status="success", state=scanner.state)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/status", response_model=StatusResponse)
def get_status():
    """
    Poll this endpoint to observe the active pipeline state.
    """
    return StatusResponse(
        state=scanner.state,
        total_photos=getattr(scanner, "total_photos", 0),
        stream_urls=getattr(scanner, "stream_urls", None)
    )


@router.get("/stream/mjpeg")
def scanner_stream_mjpeg(
    camera: Annotated[Optional[int], Query(ge=1, le=2, description="Optional camera index (1 or 2) to activate")] = None
):
    """
    Streams multipart MJPEG video directly from the active camera to browser <img> elements.
    Accepts optional camera query parameter (1 or 2) to switch active view on connection.
    Only encodes JPEGs on demand while viewers are connected.
    """
    if scanner.dual_cameras is None or not getattr(scanner, "is_streaming", False):
        raise HTTPException(
            status_code=400,
            detail="Cameras are not streaming. Call /scanner/prepare first."
        )

    if camera is not None and hasattr(scanner.dual_cameras, "active_camera"):
        scanner.dual_cameras.active_camera = camera

    cam_idx = (camera - 1) if camera in (1, 2) else getattr(scanner.dual_cameras, "active", 0)

    def frame_generator():
        if hasattr(scanner.dual_cameras, "add_spectator"):
            scanner.dual_cameras.add_spectator(cam_idx)
        try:
            last_id = -1
            while getattr(scanner, "is_streaming", False):
                if not hasattr(scanner.dual_cameras, "wait_jpeg"):
                    break
                frame_id, frame_bytes = scanner.dual_cameras.wait_jpeg(cam_idx, last_id, timeout=1.0)
                if frame_bytes and frame_id != last_id:
                    last_id = frame_id
                    yield (
                        b"--FRAME\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n\r\n"
                        + frame_bytes + b"\r\n"
                    )
        finally:
            if hasattr(scanner.dual_cameras, "remove_spectator"):
                scanner.dual_cameras.remove_spectator(cam_idx)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=FRAME"
    )


@router.post("/stream/active")
def scanner_stream_switch_active(
    camera_id: Annotated[int, Query(ge=1, le=2, description="Camera ID (1 or 2)")]
):
    """
    Switches active preview camera (1 for cam1, 2 for cam2) with zero latency.
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if not hasattr(scanner.dual_cameras, "active_camera"):
        raise HTTPException(status_code=400, detail="Active camera switching not supported by current camera driver.")

    scanner.dual_cameras.active_camera = camera_id
    return {
        "status": "success",
        "active_camera": scanner.dual_cameras.active_camera,
        "message": f"Switched preview to camera {camera_id}."
    }


@router.post("/stream/quality")
def scanner_stream_set_quality(
    quality: Annotated[int, Query(ge=1, le=100, description="Quality 1-100")]
):
    """
    Dynamically updates live JPEG compression quality (1-100) on active stream.
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    try:
        from hardware.dual_camera_rpi_sync import DualCameraRPISync
        is_rpi_sync = isinstance(scanner.dual_cameras, DualCameraRPISync)
    except Exception:
        is_rpi_sync = False

    if is_rpi_sync:
        scanner.dual_cameras.stream_quality = quality
        current_q = scanner.dual_cameras.stream_quality
    else:
        scanner.dual_cameras.quality = quality
        current_q = scanner.dual_cameras.quality

    return {
        "status": "success",
        "quality": current_q,
        "message": f"Updated stream quality to {quality}."
    }


@router.get("/stream/sync-status")
def scanner_stream_sync_status():
    """
    Returns hardware/software synchronization diagnostics (SyncReady, offsets, framerates, drops).
    """
    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if hasattr(scanner.dual_cameras, "get_sync_status"):
        return scanner.dual_cameras.get_sync_status()

    return {
        "ready": True,
        "is_streaming": scanner.is_streaming,
        "mode": scanner.dual_cameras.__class__.__name__
    }