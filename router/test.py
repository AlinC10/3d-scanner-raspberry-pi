import time
from enum import Enum
from typing import Literal, Annotated, Optional
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from router.scanner import scanner, ScannerState
from hardware.endstop import TopEndstopTriggered, BottomEndstopTriggered
from hardware.scanner import ScannerError
from hardware.lights import LightLockedError

from hardware.motor import STEP_DELAY_MIN, STEP_DELAY_MAX
from hardware.scanner import ScannerBusyError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/test",
    tags=["Hardware Diagnostics & Tests"],
)

def _require_motors_free() -> None:
    """
    Manual motor tests must not run while a prepare/scan owns the rig: they would cut torque or move the
    platter/carriage in the middle of a scan. Only idle / finished states are allowed.
    """
    allowed = getattr(ScannerState, "PREPARE_ALLOWED", ("idle", "completed", "cancelled", "error"))
    if scanner.state not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Scanner is busy (state: {scanner.state}). Cancel it with POST /scanner/cancel first."
        )


def _motion_busy() -> HTTPException:
    """409 for a motion request that arrives while another motion (Z move, rotation, homing) is still running."""
    return HTTPException(
        status_code=409,
        detail="Another motion is in progress. Wait for it to finish or stop it with POST /scanner/cancel."
    )


# ── Schemas ──────────────────────────────────────────────────────────────────

class MotorTarget(str, Enum):
    TURNTABLE = "turntable"
    Z_AXIS = "z_axis"
    ALL = "all"


from schemas.scanner import ZMoveRequest, TurntableRotateRequest

class StreamTestRequest(BaseModel):
    bitrate: Annotated[int, Field(
        ge=500_000,
        le=10_000_000,
        description="RTSP stream bitrate in bps (e.g. 2000000 = 2 Mbps)."
    )] = 4_000_000


class StreamActiveRequest(BaseModel):
    camera_id: Annotated[int, Field(
        ge=1,
        le=2,
        description="Camera ID to display in preview (1 for cam1, 2 for cam2)."
    )]


class StreamQualityRequest(BaseModel):
    quality: Annotated[int, Field(
        ge=1,
        le=100,
        description="JPEG compression quality (1 to 100)."
    )]


class BrightnessRequest(BaseModel):
    brightness: Annotated[float, Field(
        ge=0.0,
        le=1.0,
        description="Light brightness level (0.0 to 1.0)."
    )]


# ── 1. Sensor Diagnostics (Endstops & ToF) ───────────────────────────────────

@router.get("/endstops")
def get_endstops_status():
    """
    Reads the real-time status of both Z-axis physical limit switches.
    Returns:
      bottom_endstop: True if the carriage is at the absolute bottom (home).
      top_endstop: True if the carriage hit the upper ceiling limit.
    """
    return {
        "bottom_endstop": bool(scanner.down_endstop.is_active),
        "top_endstop": bool(scanner.up_endstop.is_active)
    }


@router.get("/tof")
def get_tof_distance():
    """
    Reads the Time-of-Flight (VL53L1X) laser distance sensor.
    Always answers 200:
      status: "success" for a valid reading, "invalid" otherwise.
      distance_mm: the distance in mm, or null when the reading is invalid.
      valid: True when distance_mm is a real measurement.
      available: False when the sensor failed to initialize (not connected, library missing).
    An invalid reading (no target, timeout, I2C error) is not "no object": the empty backdrop reads valid.
    """
    tof = scanner.tof
    available = bool(getattr(tof, "available", False))
    distance = None
    if available:
        try:
            distance = tof.read_distance_mm()
        except Exception as e:
            log.error(f"ToF test read failed: {e}")
    valid = distance is not None
    return {
        "status": "success" if valid else "invalid",
        "distance_mm": distance,
        "valid": valid,
        "available": available,
    }


# ── 2. Lighting & Relays ─────────────────────────────────────────────────────

@router.post("/lights/enable")
def test_lights_enable(req: BrightnessRequest = BrightnessRequest(brightness=1.0)):
    """
    Turns on the LED illumination panels (via GPIO relay).
    """
    try:
        scanner.lights.set_brightness(req.brightness)
        scanner.lights.on()
        return {
            "status": "success",
            "state": "enabled",
            "brightness": req.brightness,
            "message": "Lights enabled."
        }
    except LightLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to turn on lights: {str(e)}")


@router.post("/lights/disable")
def test_lights_disable():
    """
    Turns off the LED illumination panels.
    """
    try:
        scanner.lights.off()
        return {
            "status": "success",
            "state": "disabled",
            "message": "Lights disabled."
        }
    except LightLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to turn off lights: {str(e)}")


# ── 3. Motor State (Torque) ──────────────────────────────────────────────────

@router.get("/motor/status")
def get_motor_status():
    """
    Returns the current electrical holding state of both steppers.
    "enabled" = Coil is energized, motor holding torque is ON.
    "disabled" = Coil is off, motor free wheels.
    """
    return {
        "turntable": "enabled" if getattr(scanner.turntable_motor, "is_enabled", False) else "disabled",
        "z_axis": "enabled" if getattr(scanner.z_axis_motor, "is_enabled", False) else "disabled"
    }

@router.post("/motor/enable")
def test_motor_enable(target: MotorTarget = MotorTarget.ALL):
    """
    Manually energizes the motor coils (holding torque ON).
    Useful to verify wiring and check if the motor locks up.
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            if target in (MotorTarget.TURNTABLE, MotorTarget.ALL):
                scanner.turntable_motor.enable()
            if target in (MotorTarget.Z_AXIS, MotorTarget.ALL):
                scanner.z_axis_motor.enable()
    except ScannerBusyError:
        raise _motion_busy()

    return {"status": "success", "target": target, "state": "enabled"}

@router.post("/motor/disable")
def test_motor_disable(target: MotorTarget = MotorTarget.ALL):
    """
    Manually releases motor torque (holding torque OFF / free wheel).
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            if target in (MotorTarget.TURNTABLE, MotorTarget.ALL):
                scanner.turntable_motor.disable()
            if target in (MotorTarget.Z_AXIS, MotorTarget.ALL):
                scanner.z_axis_motor.disable()
    except ScannerBusyError:
        raise _motion_busy()

    return {"status": "success", "target": target, "state": "disabled"}


# ── 4. Motor Homing & Limit Seek ─────────────────────────────────────────────

@router.post("/motor/home-bottom")
def test_home_bottom(
    delay: float = Query(0.0015, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Cruise step delay in seconds"),
    start_delay: float = Query(0.004, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Starting ramp delay"),
    ramp_percent: float = Query(0.1, ge=0.0, le=1.0, description="Percentage of movement used for acceleration"),
    acceleration: bool = Query(True, description="Ramp the speed up from start_delay. false: constant speed at `delay` (start_delay and ramp_percent are ignored)")
):
    """
    Moves the Z-axis carriage DOWN until the bottom (home) limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            test_motor_enable(MotorTarget.Z_AXIS)
            scanner.home_z_axis(delay=delay, start_delay=start_delay, ramp_percent=ramp_percent, acceleration=acceleration)
        return {
            "status": "success",
            "message": "Carriage successfully homed to bottom endstop.",
            "bottom_endstop": bool(scanner.down_endstop.is_active)
        }
    except ScannerBusyError:
        raise _motion_busy()
    except HTTPException:
        raise
    except Exception as e:
        text = str(e)
        raise HTTPException(status_code=500, detail=text if text.startswith("Homing failed") else f"Homing failed: {text}")


@router.post("/motor/move-to-top")
def test_move_to_top(
    delay: float = Query(0.0015, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Cruise step delay in seconds"),
    start_delay: float = Query(0.004, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Starting ramp delay"),
    ramp_percent: float = Query(0.1, ge=0.0, le=1.0, description="Percentage of movement used for acceleration"),
    acceleration: bool = Query(True, description="Ramp the speed up from start_delay. false: constant speed at `delay` (start_delay and ramp_percent are ignored)")
):
    """
    Moves the Z-axis carriage UP until the top limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            test_motor_enable(MotorTarget.Z_AXIS)
            scanner.move_z_to_top(delay=delay, start_delay=start_delay, ramp_percent=ramp_percent, acceleration=acceleration)
        return {
            "status": "success",
            "message": "Carriage successfully reached top endstop.",
            "top_endstop": bool(scanner.up_endstop.is_active)
        }
    except ScannerBusyError:
        raise _motion_busy()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Move to top failed: {str(e)}")


# ── 5. Manual Incremental Movement ───────────────────────────────────────────

@router.post("/motor/z-move")
def test_z_move(req: ZMoveRequest):
    """
    Moves the Z-axis carriage by a specific distance in millimeters.
    Automatically blocked if the target limit switch is active.
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            scanner.z_axis_motor.enable()
            try:
                if req.direction == "up":
                    moved = scanner.move_z_up_distance(
                        distance=req.distance_mm,
                        delay=req.delay,
                        acceleration=req.acceleration,
                        start_delay=req.start_delay,
                        ramp_percent=req.ramp_percent,
                        decel_percent=req.decel_percent,
                        end_delay=req.end_delay
                    )
                else:
                    moved = scanner.move_z_down_distance(
                        distance=req.distance_mm,
                        delay=req.delay,
                        acceleration=req.acceleration,
                        start_delay=req.start_delay,
                        ramp_percent=req.ramp_percent,
                        decel_percent=req.decel_percent,
                        end_delay=req.end_delay
                    )
            finally:
                scanner.z_axis_motor.disable()

        if not moved:
            raise RuntimeError("the movement was aborted before it completed")

        return {
            "status": "success",
            "direction": req.direction,
            "distance_mm": req.distance_mm
        }
    except ScannerBusyError:
        raise _motion_busy()
    except HTTPException:
        raise
    except TopEndstopTriggered:
        raise HTTPException(status_code=400, detail="Cannot move UP: Top limit switch reached!")
    except BottomEndstopTriggered:
        raise HTTPException(status_code=400, detail="Cannot move DOWN: Bottom limit switch reached!")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Z-axis movement error: {str(e)}")


@router.post("/turntable/rotate")
def test_turntable_rotate(req: TurntableRotateRequest):
    """
    Rotates the turntable by a specific angle in degrees (e.g. 18°).
    """
    _require_motors_free()
    try:
        with scanner.motion_guard():
            scanner.turntable_motor.enable()
            try:
                completed = scanner.turntable_motor.rotate_angle(
                    clockwise=req.clockwise,
                    angle=req.angle,
                    delay=req.delay,
                    acceleration=req.acceleration,
                    start_delay=req.start_delay,
                    ramp_percent=req.ramp_percent,
                    decel_percent=req.decel_percent,
                    end_delay=req.end_delay
                )
            finally:
                scanner.turntable_motor.disable()

        if not completed:
            raise RuntimeError("the rotation was aborted before it completed")

        return {
            "status": "success",
            "angle": req.angle,
            "clockwise": req.clockwise
        }
    except ScannerBusyError:
        raise _motion_busy()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Turntable rotation error: {str(e)}")


# ── 6. Livestream & Camera Testing ───────────────────────────────────────────

@router.post("/stream/start")
def test_stream_start(req: StreamTestRequest = StreamTestRequest()):
    """
    Initializes dual cameras and starts the H.264 RTSP livestream.
    Returns the RTSP stream URLs.
    """
    try:
        if scanner.is_streaming and scanner.stream_urls:
            return {
                "status": "already_streaming",
                "stream_urls": scanner.stream_urls,
                "is_streaming": True
            }

        urls = scanner.setup_and_stream(enable_stream=True, bitrate=req.bitrate)
        return {
            "status": "success",
            "stream_urls": urls,
            "is_streaming": scanner.is_streaming
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Livestream initialization failed: {str(e)}")


@router.post("/stream/stop")
def test_stream_stop():
    """
    Stops the active RTSP live stream and closes the camera hardware locks.
    """
    try:
        scanner.stop_stream()
        return {
            "status": "success",
            "is_streaming": False,
            "message": "Livestream stopped and cameras closed."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop stream: {str(e)}")


@router.get("/stream/status")
def test_stream_status():
    """
    Returns whether the camera livestream is active and its current stream URLs.
    """
    return {
        "is_streaming": scanner.is_streaming,
        "stream_urls": scanner.stream_urls
    }


@router.get("/stream/mjpeg")
def test_stream_mjpeg(
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
            detail="Cameras are not streaming. Call /test/stream/start first."
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
def test_stream_switch_active(
    camera_id: Annotated[Optional[int], Query(ge=1, le=2, description="Camera ID (1 or 2)")] = None,
    req: Optional[StreamActiveRequest] = None,
):
    """
    Switches active preview camera (1 for cam1, 2 for cam2) with zero latency.
    """
    target_id = camera_id if camera_id is not None else (req.camera_id if req is not None else None)
    if target_id is None:
        raise HTTPException(status_code=400, detail="Must provide camera_id via query param or JSON body.")

    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    if not hasattr(scanner.dual_cameras, "active_camera"):
        raise HTTPException(status_code=400, detail="Active camera switching not supported by current camera driver.")

    scanner.dual_cameras.active_camera = target_id
    return {
        "status": "success",
        "active_camera": scanner.dual_cameras.active_camera,
        "message": f"Switched preview to camera {target_id}."
    }


@router.post("/stream/quality")
def test_stream_set_quality(
    quality: Annotated[Optional[int], Query(ge=1, le=100, description="Quality 1-100")] = None,
    req: Optional[StreamQualityRequest] = None,
):
    """
    Dynamically updates live JPEG compression quality (1-100) on active stream.
    """
    target_quality = quality if quality is not None else (req.quality if req is not None else None)
    if target_quality is None:
        raise HTTPException(status_code=400, detail="Must provide quality via query param or JSON body.")

    if scanner.dual_cameras is None:
        raise HTTPException(status_code=400, detail="Cameras are not initialized.")

    try:
        from hardware.dual_camera_rpi_sync import DualCameraRPISync
        is_rpi_sync = isinstance(scanner.dual_cameras, DualCameraRPISync)
    except Exception:
        is_rpi_sync = False

    if is_rpi_sync:
        scanner.dual_cameras.stream_quality = target_quality
        current_q = scanner.dual_cameras.stream_quality
    else:
        scanner.dual_cameras.quality = target_quality
        current_q = scanner.dual_cameras.quality

    return {
        "status": "success",
        "quality": current_q,
        "message": f"Updated stream quality to {target_quality}."
    }


@router.get("/stream/sync-status")
def test_stream_sync_status():
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


@router.post("/camera/snap")
def test_camera_snap():
    """
    Captures a single synchronized photo pair from both cameras to verify capture functionality.
    Initializes cameras first if not currently running.
    """
    try:
        if scanner.dual_cameras is None:
            scanner.setup_and_stream(enable_stream=False)

        photo_paths = scanner.dual_cameras.capture_photo()
        return {
            "status": "success",
            "photo_paths": photo_paths,
            "count": len(photo_paths) if photo_paths else 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Camera snap failed: {str(e)}")


# ── 7. Full Hardware Reset & Cleanup ─────────────────────────────────────────

@router.post("/cleanup")
def test_cleanup():
    """
    Powers down all hardware safely:
    - Cuts stepper motor torque.
    - Turns off LED lighting relay.
    - Closes cameras and terminates live stream.
    - Resets hardware state to IDLE.
    """
    _require_motors_free()
    try:
        scanner.cleanup()
        return {
            "status": "success",
            "message": "All hardware safely stopped, torques cut, and cameras closed.",
            "state": scanner.state,
            "is_streaming": scanner.is_streaming,
            "lights_on": bool(scanner.lights.is_active)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleanup error: {str(e)}")
