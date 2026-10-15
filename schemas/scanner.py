from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Annotated, Literal
from hardware.camera.config import AF_ROI, AF_STEP, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_VIDEO_RESOLUTION, DEFAULT_PHOTO_RESOLUTION, DEFAULT_QUALITY
from hardware.motor import (
    END_DELAY_MIN, END_DELAY_MAX, TURNTABLE_END_DELAY_DEFAULT,
    MotorError, angle_to_steps, distance_to_steps, TURNTABLE_STEP_TYPE, Z_STEP_TYPE,
)
from hardware.motor import STEP_DELAY_MIN, STEP_DELAY_MAX

# Longest Z travel of the rig (decision D3); matches Z_TRAVEL_MAX_MM in hardware/scanner.py
Z_DISTANCE_MAX_MM = 150.0


class DualCameraConfig(BaseModel):
    master_id: Annotated[Literal[0, 1], Field(description="Camera ID for the master camera.")] = 0
    slave_id: Annotated[Literal[0, 1], Field(description="Camera ID for the slave camera.")] = 1

    quality: Annotated[int, Field(ge=10, le=100, description="JPEG quality for captured photos.")] = DEFAULT_QUALITY
    photo_resolution: Annotated[tuple[int, int], Field(description="Resolution for still captures (W, H).")] = DEFAULT_PHOTO_RESOLUTION
    video_resolution: Annotated[tuple[int, int], Field(description="Resolution for video streaming (W, H).")] = DEFAULT_VIDEO_RESOLUTION
    preview_resolution: Annotated[tuple[int, int], Field(description="Resolution for the live preview (W, H).")] = DEFAULT_PREVIEW_RESOLUTION

    rotation: Annotated[
        Literal[0, 90, 180, 270] | tuple[Literal[0, 90, 180, 270], Literal[0, 90, 180, 270]] | list[Literal[0, 90, 180, 270]] | None,
        Field(description="Rotation degrees for the cameras.")
    ] = 0

    focus: Annotated[int | str | tuple[int, int] | list[int] | None, Field(description="Focus position, or tuple/list for dual independent focus.")] = None

    show_preview: Annotated[bool, Field(description="Whether to show the native camera preview window.")] = False

    @field_validator("focus")
    @classmethod
    def _focus_must_be_numeric(cls, v):
        # 'auto' is not implemented by the camera driver (it used to end in a 500 after the lights were on)
        if isinstance(v, str):
            raise ValueError("focus must be an integer position (0-1023) or a pair of them; 'auto' is not supported")
        return v
    settle_time: Annotated[float, Field(ge=0.0, description="Time in seconds to wait for auto-exposure/auto-white-balance to settle.")] = 2.0
    
    exposure_time: Annotated[int | None, Field(description="Manual exposure time in microseconds.")] = None
    analogue_gain: Annotated[float | None, Field(description="Manual analogue gain.")] = None
    colour_gains: Annotated[tuple[float, float] | None, Field(description="Manual AWB gains (red, blue).")] = None
    awb_mode: Annotated[str | None, Field(description="Auto white balance mode.")] = "auto"
    
    brightness: Annotated[float | None, Field(ge=-1.0, le=1.0, description="Brightness adjustment.")] = None
    contrast: Annotated[float | None, Field(description="Contrast adjustment.")] = None
    saturation: Annotated[float | None, Field(description="Saturation adjustment.")] = None
    sharpness: Annotated[float | None, Field(description="Sharpness adjustment.")] = None
    
    autofocus_step: Annotated[int, Field(description="Step size for contrast-based autofocus routines.")] = AF_STEP
    autofocus_roi: Annotated[tuple[float, float, float, float], Field(description="Region of interest for autofocus (x, y, w, h).")] = AF_ROI
    keep_running: Annotated[bool, Field(description="Keep the camera event loop running. (Required True for streams).")] = False


class PrepareRequest(BaseModel):
    enable_stream: Annotated[bool, Field(
        description="Whether to generate and return a livestream URL during preparation."
    )] = True
    bitrate: Annotated[int, Field(
        ge=500_000,
        le=10_000_000,
        description="The bitrate for the camera livestream in bps (e.g., 2000000 for 2 Mbps)."
    )] = 2_000_000
    
    illumination_brightness: Annotated[float, Field(
        gt=0.0,
        le=1.0,
        description=(
            "Desired LED brightness during the scan (above 0.0, up to 1.0). Defaults to 100%. "
            "0 is refused: the scanner cannot scan in the dark."
        )
    )] = 1.0

    camera_config: Annotated[DualCameraConfig, Field(description="Configuration block for the dual camera array.")]


class _TurntableMotion(BaseModel):
    """Motion parameters shared by the manual turntable test and the scan configuration (no direction)."""
    angle: Annotated[float, Field(
        gt=0.0,
        le=360.0,
        description="Angle in degrees to rotate turntable."
    )] = 18.0
    delay: Annotated[float, Field(
        ge=STEP_DELAY_MIN,
        le=STEP_DELAY_MAX,
        description=f"Delay in seconds between step pulses for the motor ({STEP_DELAY_MIN}-{STEP_DELAY_MAX} s)."
    )] = 0.0010
    acceleration: Annotated[bool, Field(
        description="Whether to use trapezoidal acceleration ramping."
    )] = True
    ramp_percent: Annotated[float, Field(
        ge=0.0,
        le=1.0,
        description="Percentage of total steps to use for the acceleration phase."
    )] = 0.2
    decel_percent: Annotated[float | None, Field(
        ge=0.0,
        le=1.0,
        description="Optional separate percentage for the deceleration phase. Defaults to ramp_percent if null."
    )] = None
    start_delay: Annotated[float | None, Field(
        ge=STEP_DELAY_MIN,
        le=STEP_DELAY_MAX,
        description=f"Starting delay in seconds for the ramp ({STEP_DELAY_MIN}-{STEP_DELAY_MAX} s)."
    )] = None
    end_delay: Annotated[float | None, Field(
        ge=END_DELAY_MIN,
        le=END_DELAY_MAX,
        description=(
            "Step delay in seconds at the end of the deceleration ramp, so the turntable stops slowly "
            f"({END_DELAY_MIN}-{END_DELAY_MAX} s). null disables the slow stop. "
            "Ignored when acceleration is false or there is no deceleration phase."
        )
    )] = TURNTABLE_END_DELAY_DEFAULT

    @field_validator("angle")
    @classmethod
    def _angle_must_be_whole_steps(cls, value: float) -> float:
        # Same rule the motor applies when the move is executed, so a bad angle is a 422 now and not a failed scan later
        try:
            angle_to_steps(value, TURNTABLE_STEP_TYPE)
        except MotorError as e:
            raise ValueError(str(e))
        return value


class TurntableRotateRequest(_TurntableMotion):
    """Manual rotation (`/test/turntable/rotate`): up to 360° in either direction."""
    clockwise: Annotated[bool, Field(
        description="Rotation direction (True = clockwise, False = counter-clockwise)."
    )] = True


class TurntableScanConfig(_TurntableMotion):
    """
    Turntable settings of a scan (MechanicalConfig.turntable). Stricter than the manual test request: one ring of
    photos must cover exactly one revolution, so the angle must divide a full turn in whole steps and is at most 180°.
    A scan always turns the platter in one fixed direction, so there is no `clockwise` field here (a client that
    still sends one is ignored).

    Unknown keys are rejected (422) so a misspelled setting is not silently replaced by its default.
    """
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data):
        # `clockwise` was part of the old scan request: still accepted, but ignored
        if isinstance(data, dict) and "clockwise" in data:
            data = {key: value for key, value in data.items() if key != "clockwise"}
        return data

    angle: Annotated[float, Field(
        gt=0.0,
        le=180.0,
        description="Angle in degrees between two photos. At most 180 and must divide 360 in whole steps."
    )] = 18.0

    @field_validator("angle")
    @classmethod
    def _angle_must_divide_a_full_turn(cls, value: float) -> float:
        try:
            steps = angle_to_steps(value, TURNTABLE_STEP_TYPE)
            full_turn = angle_to_steps(360.0, TURNTABLE_STEP_TYPE)
        except MotorError as e:
            raise ValueError(str(e))
        if full_turn % steps != 0:
            raise ValueError(
                f"angle {value}° ({steps} steps) does not divide a full revolution ({full_turn} steps) evenly; "
                "the scan would not close the circle"
            )
        return value


class _ZMotion(BaseModel):
    """Motion parameters shared by the manual Z test and the scan configuration (no direction)."""
    distance_mm: Annotated[float, Field(
        gt=0.0,
        le=Z_DISTANCE_MAX_MM,
        description=f"Distance in millimeters to travel (at most {Z_DISTANCE_MAX_MM:g} mm, the Z travel)."
    )] = 100.0
    delay: Annotated[float, Field(
        ge=STEP_DELAY_MIN,
        le=STEP_DELAY_MAX,
        description=f"Delay in seconds between step pulses for the motor ({STEP_DELAY_MIN}-{STEP_DELAY_MAX} s)."
    )] = 0.0010
    acceleration: Annotated[bool, Field(
        description="Whether to use trapezoidal acceleration ramping."
    )] = False
    ramp_percent: Annotated[float, Field(
        ge=0.0,
        le=1.0,
        description="Percentage of total steps to use for the acceleration phase."
    )] = 0.2
    decel_percent: Annotated[float | None, Field(
        ge=0.0,
        le=1.0,
        description="Optional separate percentage for the deceleration phase. Defaults to ramp_percent if null."
    )] = None
    start_delay: Annotated[float | None, Field(
        ge=STEP_DELAY_MIN,
        le=STEP_DELAY_MAX,
        description=f"Starting delay in seconds for the ramp ({STEP_DELAY_MIN}-{STEP_DELAY_MAX} s)."
    )] = None
    end_delay: Annotated[float | None, Field(
        ge=END_DELAY_MIN,
        le=END_DELAY_MAX,
        description=(
            "Optional step delay in seconds at the end of the deceleration ramp for a slow stop "
            f"({END_DELAY_MIN}-{END_DELAY_MAX} s). null (default) disables it."
        )
    )] = None

    @field_validator("distance_mm")
    @classmethod
    def _distance_must_be_whole_steps(cls, value: float) -> float:
        try:
            distance_to_steps(value, Z_STEP_TYPE)
        except MotorError as e:
            raise ValueError(str(e))
        return value


class ZMoveRequest(_ZMotion):
    """Manual Z move (`/test/motor/z-move`): up or down."""
    direction: Annotated[Literal["up", "down"], Field(
        description="Direction of carriage travel."
    )] = "up"


class ZAxisScanConfig(_ZMotion):
    """
    Z-axis settings of a scan (MechanicalConfig.z_axis): `distance_mm` is the climb between two rings. A scan only
    ever climbs, so there is no `direction` field here (a client that still sends one is ignored).

    Unknown keys are rejected (422) so a misspelled setting is not silently replaced by its default.
    """
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data):
        # `direction` was part of the old scan request: still accepted, but ignored
        if isinstance(data, dict) and "direction" in data:
            data = {key: value for key, value in data.items() if key != "direction"}
        return data


class MechanicalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turntable: Annotated[TurntableScanConfig, Field(description="Configuration for the rotating turntable.")] = Field(default_factory=TurntableScanConfig)
    z_axis: Annotated[ZAxisScanConfig, Field(description="Configuration for the vertical Z-axis.")] = Field(default_factory=ZAxisScanConfig)


class CloudConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch_runpod: Annotated[bool, Field(
        description=(
            "When false (default) the scan stops once the photos are verified in the R2 bucket and the state "
            "becomes `completed`. When true the RunPod pipeline (Meshroom job, output download) runs afterwards."
        )
    )] = False

    resolution_mp: Annotated[float, Field(
        gt=0.0,
        le=13.0,
        description=(
            "Camera capture resolution in megapixels (12.0 nominal, 12.33 real sensor value). "
            "Used to calculate required cloud GPU RAM."
        )
    )] = 12.0

    mode: Annotated[Literal["single", "rig", "two-sides"], Field(
        description="Scanning mode layout. Options typically include 'single', 'rig', or 'two-sides'."
    )] = "rig"
    depthmap_downscale: Annotated[int, Field(
        ge=1,
        description="Meshroom DepthMap downscale factor. Higher values use less RAM but produce lower detail."
    )] = 2
    max_input_points: Annotated[int, Field(
        ge=1000,
        description="Meshroom Meshing maximum input points constraint."
    )] = 10000000


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: Annotated[str, Field(
        description="Unique identifier for this scanning and processing job, used to track RunPod execution."
    )] = "meshroom-job"
    mechanical: MechanicalConfig = Field(default_factory=MechanicalConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)


class PrepareResponse(BaseModel):
    status: str = "accepted"
    message: str = "Live stream started. Rig homing and camera tuning in progress."
    state: str = "preparing"
    stream_urls: list[str] | None = None

class StatusResponse(BaseModel):
    state: str
    total_photos: int = 0
    stream_urls: list[str] | None = None

    # Progress and failure detail (API-11). All optional so an older scanner object still produces a valid response.
    phase: str | None = None            # preparing / scanning / uploading / verifying / done ...
    phase_message: str | None = None    # human readable detail of the phase
    level: int = 0                      # current ring (0 = bottom)
    photos_captured: int = 0
    photos_uploaded: int = 0
    missing: list[str] = []             # R2 keys that could not be verified in the bucket
    error: str | None = None            # why the scan ended in `error`
    tof_warnings: list[str] = []

class CancelResponse(BaseModel):
    status: str = "success"
    state: str = "cancelled"

class StartResponse(BaseModel):
    status: str = "accepted"
    message: str = "Scan started"
