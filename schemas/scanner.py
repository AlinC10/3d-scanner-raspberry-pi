from pydantic import BaseModel, Field
from typing import Annotated, Literal

class PrepareRequest(BaseModel):
    enable_stream: Annotated[bool, Field(
        description="Whether to generate and return a livestream URL during preparation."
    )] = True
    bitrate: Annotated[int, Field(
        ge=500_000,
        le=10_000_000,
        description="The bitrate for the camera livestream in bps (e.g., 2000000 for 2 Mbps)."
    )] = 2_000_000


class MotorConfig(BaseModel):
    step_type: Annotated[Literal["Full", "Half", "1/4", "1/8", "1/16"], Field(
        description="Stepper motor microstepping resolution (e.g., 'Full', 'Half', '1/4')."
    )] = "Full"
    delay: Annotated[float, Field(
        ge=0.00005,
        description="Delay in seconds between step pulses for the motor."
    )] = 0.0005


class MechanicalConfig(BaseModel):
    angle: Annotated[float | int, Field(
        gt=2.0,
        le=40.0,
        description="The rotation angle per capture step in degrees."
    )] = 18.0
    z_move_mm: Annotated[float, Field(
        gt=0.0,
        lt=150.0,
        description="The distance in millimeters the Z-axis should move UP between rotational slices."
    )] = 100.0
    
    turntable: MotorConfig = Field(default_factory=MotorConfig)
    z_axis: MotorConfig = Field(default_factory=lambda: MotorConfig(delay=0.0001))


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