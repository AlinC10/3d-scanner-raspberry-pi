from pydantic import BaseModel, Field
from typing import Annotated, Literal
from hardware.camera.config import AF_ROI, AF_STEP, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_VIDEO_RESOLUTION, DEFAULT_PHOTO_RESOLUTION, DEFAULT_QUALITY


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

    focus: Annotated[int | str | tuple[int, int] | list[int] | None, Field(description="Focus value, 'auto', or tuple/list for dual independent focus.")] = None

    show_preview: Annotated[bool, Field(description="Whether to show the native camera preview window.")] = False
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
        ge=0.0,
        le=1.0,
        description="Desired LED brightness during the scan (0.0 to 1.0). Defaults to 100%."
    )] = 1.0

    camera_config: Annotated[DualCameraConfig, Field(description="Configuration block for the dual camera array.")]


class TurntableRotateRequest(BaseModel):
    angle: Annotated[float, Field(
        gt=0.0,
        le=360.0,
        description="Angle in degrees to rotate turntable."
    )] = 18.0
    clockwise: Annotated[bool, Field(
        description="Rotation direction (True = clockwise, False = counter-clockwise)."
    )] = True
    delay: Annotated[float, Field(
        ge=0.00005,
        description="Delay in seconds between step pulses for the motor."
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
        ge=0.00005,
        description="Starting delay in seconds for the ramp."
    )] = None


class ZMoveRequest(BaseModel):
    distance_mm: Annotated[float, Field(
        gt=0.0,
        le=200.0,
        description="Distance in millimeters to travel."
    )] = 100.0
    direction: Annotated[Literal["up", "down"], Field(
        description="Direction of carriage travel."
    )] = "up"
    delay: Annotated[float, Field(
        ge=0.00005,
        description="Delay in seconds between step pulses for the motor."
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
        ge=0.00005,
        description="Starting delay in seconds for the ramp."
    )] = None


class MechanicalConfig(BaseModel):
    turntable: Annotated[TurntableRotateRequest, Field(description="Configuration for the rotating turntable.")] = Field(default_factory=TurntableRotateRequest)
    z_axis: Annotated[ZMoveRequest, Field(description="Configuration for the vertical Z-axis.")] = Field(default_factory=ZMoveRequest)


class CloudConfig(BaseModel):
    resolution_mp: Annotated[float, Field(
        gt=0.0,
        lt=12.0,
        description="Camera capture resolution in megapixels. Used to calculate required cloud GPU RAM."
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

class CancelResponse(BaseModel):
    status: str = "success"
    state: str = "cancelled"

class StartResponse(BaseModel):
    status: str = "accepted"
    message: str = "Scan started"
