from enum import Enum
from typing import Literal, Annotated
import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from router.scanner import scanner
from hardware.endstop import TopEndstopTriggered, BottomEndstopTriggered
from hardware.scanner import ScannerError
from hardware.lights import LightLockedError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/test",
    tags=["Hardware Diagnostics & Tests"],
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
    Reads the Time-of-Flight (VL53L0X) laser distance sensor.
    Returns the distance to the subject in mm.
    """
    try:
        distance = scanner.tof_sensor.get_distance()
        return {
            "status": "success",
            "distance_mm": distance
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ToF Sensor Error: {str(e)}")


# ── 2. Lighting & Relays ─────────────────────────────────────────────────────

@router.post("/lights/enable")
def test_lights_enable(req: BrightnessRequest = BrightnessRequest(brightness=1.0)):
    """
    Turns on the LED illumination panels (via GPIO relay).
    """
    try:
        scanner.lights.enable(brightness=req.brightness)
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
        scanner.lights.disable()
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
    def is_enabled(motor):
        if motor.en_device is None:
            return True
        return motor.en_device.value == 0

    return {
        "turntable": "enabled" if is_enabled(scanner.turntable_motor) else "disabled",
        "z_axis": "enabled" if is_enabled(scanner.z_axis_motor) else "disabled"
    }

@router.post("/motor/enable")
def test_motor_enable(target: MotorTarget = MotorTarget.ALL):
    """
    Manually energizes the motor coils (holding torque ON).
    Useful to verify wiring and check if the motor locks up.
    """
    if target in (MotorTarget.TURNTABLE, MotorTarget.ALL):
        scanner.turntable_motor.enable()
    if target in (MotorTarget.Z_AXIS, MotorTarget.ALL):
        scanner.z_axis_motor.enable()
        
    return {"status": "success", "target": target, "state": "enabled"}

@router.post("/motor/disable")
def test_motor_disable(target: MotorTarget = MotorTarget.ALL):
    """
    Manually releases motor torque (holding torque OFF / free wheel).
    """
    if target in (MotorTarget.TURNTABLE, MotorTarget.ALL):
        scanner.turntable_motor.disable()
    if target in (MotorTarget.Z_AXIS, MotorTarget.ALL):
        scanner.z_axis_motor.disable()
        
    return {"status": "success", "target": target, "state": "disabled"}


# ── 4. Motor Homing & Limit Seek ─────────────────────────────────────────────

@router.post("/motor/home-bottom")
def test_home_bottom():
    """
    Moves the Z-axis carriage DOWN until the bottom (home) limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    try:
        test_motor_enable(MotorTarget.Z_AXIS)
        scanner.home_z_axis()
        return {
            "status": "success",
            "message": "Carriage successfully homed to bottom endstop.",
            "bottom_endstop": bool(scanner.down_endstop.is_active)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Homing failed: {str(e)}")


@router.post("/motor/move-to-top")
def test_move_to_top(delay: float = Query(0.0010, ge=0.0002, le=0.01)):
    """
    Moves the Z-axis carriage UP until the top limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    try:
        test_motor_enable(MotorTarget.Z_AXIS)
        scanner.move_z_to_top(delay=delay)
        return {
            "status": "success",
            "message": "Carriage successfully reached top endstop.",
            "top_endstop": bool(scanner.up_endstop.is_active)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Move to top failed: {str(e)}")


# ── 5. Manual Incremental Movement ───────────────────────────────────────────

@router.post("/motor/z-move")
def test_z_move(req: ZMoveRequest):
    """
    Moves the Z-axis carriage by a specific distance in millimeters.
    Automatically blocked if the target limit switch is active.
    """
    scanner.z_axis_motor.enable()
    try:
        if req.direction == "up":
            scanner.move_z_up_distance(
                distance=req.distance_mm, 
                delay=req.delay,
                acceleration=req.acceleration,
                start_delay=req.start_delay,
                ramp_percent=req.ramp_percent,
                decel_percent=req.decel_percent
            )
        else:
            scanner.move_z_down_distance(
                distance=req.distance_mm, 
                delay=req.delay,
                acceleration=req.acceleration,
                start_delay=req.start_delay,
                ramp_percent=req.ramp_percent,
                decel_percent=req.decel_percent
            )

        return {
            "status": "success",
            "direction": req.direction,
            "distance_mm": req.distance_mm
        }
    except TopEndstopTriggered:
        raise HTTPException(status_code=400, detail="Cannot move UP: Top limit switch reached!")
    except BottomEndstopTriggered:
        raise HTTPException(status_code=400, detail="Cannot move DOWN: Bottom limit switch reached!")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Z-axis movement error: {str(e)}")
    finally:
        scanner.z_axis_motor.stop()
        scanner.z_axis_motor.disable()


@router.post("/turntable/rotate")
def test_turntable_rotate(req: TurntableRotateRequest):
    """
    Rotates the turntable by a specific angle in degrees (e.g. 18°).
    """
    scanner.turntable_motor.enable()
    try:
        scanner.turntable_motor.rotate_angle(
            clockwise=req.clockwise,
            angle=req.angle,
            delay=req.delay,
            acceleration=req.acceleration,
            start_delay=req.start_delay,
            ramp_percent=req.ramp_percent,
            decel_percent=req.decel_percent
        )
        return {
            "status": "success",
            "angle": req.angle,
            "clockwise": req.clockwise
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Turntable rotation error: {str(e)}")
    finally:
        scanner.turntable_motor.stop()
        scanner.turntable_motor.disable()


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
