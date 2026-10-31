import time
from enum import Enum
from typing import Literal, Annotated, Optional
import logging
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app_paths import TEST_SNAPS_DIR
from router import scanner as scanner_api
from router.scanner import scanner, ScannerState
from hardware.endstop import TopEndstopTriggered, BottomEndstopTriggered
from hardware.scanner import ScannerError
from hardware.lights import LightLockedError
from hardware.buzzer import PATTERNS as BUZZER_PATTERNS, MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ, MAX_TONE_S

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


def _require_lights_free() -> None:
    """
    The lights belong to the scan from /scanner/prepare until the scan ends (states preparing / prepared / running):
    a manual switch-off or dimming would ruin the photos. A manual /test stream session does not count.
    """
    if getattr(scanner, "scan_owns_rig", False) is True:
        raise HTTPException(
            status_code=409,
            detail=f"The lights are controlled by the scan (state: {scanner.state}). Cancel it with POST /scanner/cancel first."
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

# The sounds the scanner plays (hardware/buzzer.py PATTERNS; tests check that both lists are the same)
BuzzerPattern = Literal["ready", "start", "hold_still", "photo_countdown", "success", "error"]


class BuzzerPlayRequest(BaseModel):
    pattern: Annotated[BuzzerPattern, Field(description="Sound to play: ready, start, hold_still, photo_countdown, success, error.")]


class BuzzerToneRequest(BaseModel):
    frequency_hz: Annotated[float, Field(ge=MIN_FREQUENCY_HZ, le=MAX_FREQUENCY_HZ,
                                         description="Tone frequency (110-1760 Hz).")] = 880.0
    duration_s: Annotated[float, Field(gt=0.0, le=MAX_TONE_S, description="Tone length (at most 5 s).")] = 0.5


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
    _require_lights_free()
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
    _require_lights_free()
    try:
        scanner.lights.off(force=False)
        return {
            "status": "success",
            "state": "disabled",
            "message": "Lights disabled."
        }
    except LightLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to turn off lights: {str(e)}")


# ── 2b. Buzzer ───────────────────────────────────────────────────────────────
# Allowed in any state (no motor, no light): a test sound only interrupts the sound playing.

def _buzzer():
    """The scanner's buzzer, or 503 when it is missing or could not be opened."""
    buzzer = getattr(scanner, "buzzer", None)
    if buzzer is None or not getattr(buzzer, "available", False):
        raise HTTPException(
            status_code=503,
            detail="The buzzer is not available (BUZZER_GPIO_PIN off, gpiozero missing or the pin could not be opened)."
        )
    return buzzer


@router.get("/buzzer")
def get_buzzer_status():
    """Whether the buzzer works (`available`), its GPIO pin and the names of the sounds the scanner plays."""
    buzzer = getattr(scanner, "buzzer", None)
    return {
        "available": bool(getattr(buzzer, "available", False)),
        "pin": getattr(buzzer, "pin", None),
        "patterns": list(BUZZER_PATTERNS),
    }


@router.post("/buzzer/play")
def test_buzzer_play(req: BuzzerPlayRequest):
    """Plays one of the scanner's sounds now (in the background: the answer does not wait for it). 503 without buzzer."""
    buzzer = _buzzer()
    buzzer.play(req.pattern)
    return {"status": "success", "pattern": req.pattern}


@router.post("/buzzer/tone")
def test_buzzer_tone(req: BuzzerToneRequest = BuzzerToneRequest()):
    """Plays one tone (default 880 Hz for 0.5 s) to check the wiring and the volume. 503 without buzzer."""
    buzzer = _buzzer()
    try:
        buzzer.tone(req.frequency_hz, req.duration_s)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"status": "success", "frequency_hz": req.frequency_hz, "duration_s": req.duration_s}


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
    scanner_api.require_arduino()   # 503 without the Arduino
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
    scanner_api.require_arduino()   # 503 without the Arduino
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
    delay: float = Query(0.0010, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Step delay in seconds (constant speed unless acceleration=true)"),
    start_delay: float = Query(0.004, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Starting ramp delay"),
    ramp_percent: float = Query(0.1, ge=0.0, le=1.0, description="Percentage of movement used for acceleration"),
    acceleration: bool = Query(False, description="true: ramp the speed up from start_delay. false (default): constant speed at `delay` (start_delay and ramp_percent are ignored)")
):
    """
    Moves the Z-axis carriage DOWN until the bottom (home) limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    _require_motors_free()
    scanner_api.require_arduino()   # 503 without the Arduino
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
    delay: float = Query(0.0010, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Step delay in seconds (constant speed unless acceleration=true)"),
    start_delay: float = Query(0.004, ge=STEP_DELAY_MIN, le=STEP_DELAY_MAX, description="Starting ramp delay"),
    ramp_percent: float = Query(0.1, ge=0.0, le=1.0, description="Percentage of movement used for acceleration"),
    acceleration: bool = Query(False, description="true: ramp the speed up from start_delay. false (default): constant speed at `delay` (start_delay and ramp_percent are ignored)")
):
    """
    Moves the Z-axis carriage UP until the top limit switch is triggered.
    Stops and releases torque upon reaching the switch.
    """
    _require_motors_free()
    scanner_api.require_arduino()   # 503 without the Arduino
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
    scanner_api.require_arduino()   # 503 without the Arduino
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
    scanner_api.require_arduino()   # 503 without the Arduino
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
def test_stream_start():
    """
    Initializes the dual cameras and the lights and starts the MJPEG live preview.
    Returns the preview URLs (`/scanner/stream/mjpeg?camera=1|2`), which can be used as `<img src>`.

    A pure hardware test: it does not touch Cloudflare R2, the ToF sensor or the disk-space check that
    `POST /scanner/prepare` runs first.
    """
    try:
        if scanner.is_streaming and scanner.stream_urls:
            return {
                "status": "already_streaming",
                "stream_urls": scanner.stream_urls,
                "is_streaming": True
            }

        urls = scanner.setup_and_stream(enable_stream=True, preflight=False)
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
    Stops the live preview and closes the cameras.
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


# The preview endpoints are the ones of /scanner (router/scanner.py), registered here too under /test/stream/*, so
# there is one implementation to fix. They also take the camera / quality as a query parameter or a JSON body.
for _path, _methods, _endpoint in (
    ("/stream/mjpeg", ["GET"], scanner_api.scanner_stream_mjpeg),
    ("/stream/active", ["POST"], scanner_api.scanner_stream_switch_active),
    ("/stream/quality", ["POST"], scanner_api.scanner_stream_set_quality),
    ("/stream/sync-status", ["GET"], scanner_api.scanner_stream_sync_status),
):
    router.add_api_route(_path, _endpoint, methods=_methods)


@router.post("/camera/snap")
def test_camera_snap():
    """
    Captures a single synchronized photo pair from both cameras to verify capture functionality. The two photos are
    saved in the `test_snaps/` folder of the project (`snap_<date>_<time>_cam1.jpg` and `_cam2.jpg`).
    Initializes cameras first if not currently running, and releases them again afterwards when it started them
    (the scanner returns to IDLE). Refused while a scan owns the cameras.
    """
    if getattr(scanner, "scan_owns_rig", False) is True:
        raise HTTPException(
            status_code=409,
            detail=f"The cameras belong to the scan (state: {scanner.state}). Cancel it with POST /scanner/cancel first."
        )
    started_here = scanner.dual_cameras is None
    try:
        if started_here:
            scanner.setup_and_stream(enable_stream=False, preflight=False)

        # test_snaps/snap_<date>_<time>_cam1.jpg and _cam2.jpg: easy to tell from scan photos, never overwritten
        photo_paths = scanner.dual_cameras.capture_photo(
            output_prefix=time.strftime("snap_%Y%m%d_%H%M%S"),
            output_dir=str(TEST_SNAPS_DIR),
            meshroom_rig=False,
        )
        return {
            "status": "success",
            "photo_paths": photo_paths,
            "count": len(photo_paths) if photo_paths else 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Camera snap failed: {str(e)}")
    finally:
        if started_here:
            scanner.stop_stream()


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


@router.post("/cloud/runpod-dry-run")
def test_cloud_runpod_dry_run(
    resolution_level: Annotated[int, Query(ge=0, le=3, description="Turntable Studio resolution level")] = 1,
):
    """
    Checks the cloud chain WITHOUT creating a pod (nothing is billed): the configuration (`problem`), the R2 write test
    (preflight), the RunPod API key (lists the account's pods, read only) and the pod request that a scan with
    `cloud.launch_runpod: true` would send for the photos of the last scan (`input_images/rig/`), secrets redacted.
    No temporary R2 credentials are minted. 400 when there are no photos on disk.
    """
    from pathlib import Path
    import cloud.cloudflare_r2 as r2
    from cloud import job as cloud_job, runpod as rp

    problem = scanner_api._runpod_config_problem()
    images_dir = Path(getattr(scanner, "images_dir", None) or "input_images/rig")
    result = {"problem": problem, "images_dir": str(images_dir), "runpod_api": None, "turntable_pods": None}
    try:
        pods = rp.RunPodClient().list_pods()
        result["runpod_api"] = "ok"
        result["turntable_pods"] = [{"id": p.get("id"), "name": p.get("name"), "status": p.get("desiredStatus")}
                                    for p in pods if str(p.get("name") or "").startswith("turntable-")]  # the scanner's ("turntable-pi-*") and the laptop's
    except Exception as e:
        result["runpod_api"] = f"error: {e}"
    try:
        report = cloud_job.run_cloud_job(
            input_dir=str(images_dir.parent), image_prefix=images_dir.name + "/", job_id=r2.new_job_id(),
            pipeline_options={"rig_mode": True, "resolution_level": resolution_level}, dry_run=True,
        )
    except (r2.R2Error, rp.RunPodError) as e:
        raise HTTPException(status_code=400, detail=f"Dry run failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dry run failed: {e}")
    result.update({"plan": report.get("plan"), "pod_request": report.get("pod_request"),
                   "r2_preflight_s": report.get("timings_s", {}).get("preflight")})
    return result
