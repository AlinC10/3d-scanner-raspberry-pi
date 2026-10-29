from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from typing import Annotated, Literal
from hardware.camera.config import (
    DEFAULT_PHOTO_RESOLUTION, DEFAULT_PREVIEW_RESOLUTION, DEFAULT_QUALITY, VCM_MAX_POS, VCM_MIN_POS,
)
from hardware.motor import (
    END_DELAY_MIN, END_DELAY_MAX, TURNTABLE_END_DELAY_DEFAULT,
    MotorError, angle_to_steps, distance_to_steps, TURNTABLE_STEP_TYPE, Z_STEP_TYPE,
)
from hardware.motor import STEP_DELAY_MIN, STEP_DELAY_MAX
from hardware.rig_limits import Z_TRAVEL_MAX_MM

# Longest Z move a request may ask for: the whole travel of the rod (hardware/rig_limits.py)
Z_DISTANCE_MAX_MM = float(Z_TRAVEL_MAX_MM)
# Scan start position above the bottom endstop (PrepareRequest.z_start_mm); the default matches Z_START_DEFAULT_MM
# in hardware/scanner.py
Z_START_DEFAULT_MM = 30.0
Z_START_MAX_MM = 100.0


def _defaults_as_example(schema: dict, model: type) -> None:
    """
    `json_schema_extra` hook of the request bodies: puts the model's own defaults in the schema as its example.

    Without it Swagger (/docs) builds the "Example Value" field by field and shows `0` for every optional number that
    defaults to null (exposure_time, analogue_gain, ...): "Execute" then sends `exposure_time: 0`, which is refused.
    With it the example is exactly what a client that sends `{}` gets, minus the fields that default to null.
    A model with a required field has no such example and is left alone.
    """
    try:
        example = model().model_dump(mode="json", exclude_none=True)
    except Exception:
        return
    schema["examples"] = [example]
    schema["example"] = example   # older Swagger UI builds read this key


class DualCameraConfig(BaseModel):
    """
    Camera settings of `POST /scanner/prepare` (all of them are used by the dual rpi.sync camera driver).

    Unknown keys are tolerated and ignored, so a client that still sends a removed key (`show_preview`,
    `keep_running`, `autofocus_*`, `video_resolution`, `preview_resolution`) keeps working.
    """
    master_id: Annotated[Literal[0, 1], Field(description="Camera ID for the master camera.")] = 0
    slave_id: Annotated[Literal[0, 1], Field(description="Camera ID for the slave camera.")] = 1

    quality: Annotated[int, Field(ge=10, le=100, description="JPEG quality for captured photos.")] = DEFAULT_QUALITY
    photo_resolution: Annotated[
        Literal[(4056, 3040), (2028, 1520)],
        # a JSON array arrives as a list, which a Literal of tuples would not match
        BeforeValidator(lambda v: tuple(v) if isinstance(v, list) else v),
        Field(description=(
            "Resolution for still captures (W, H): [4056, 3040] (full sensor) or [2028, 1520] (2x2 binned, the same "
            "field of view). No other size is accepted."
        )),
    ] = DEFAULT_PHOTO_RESOLUTION
    preview_resolution: Annotated[tuple[int, int], Field(description=(
        "Resolution (W, H) of the live MJPEG preview; both values even, 160x120 up to the photo resolution. "
        "A larger preview costs CPU on the Pi."
    ))] = DEFAULT_PREVIEW_RESOLUTION

    rotation: Annotated[
        Literal[0, 90, 180, 270] | tuple[Literal[0, 90, 180, 270], Literal[0, 90, 180, 270]] | list[Literal[0, 90, 180, 270]] | None,
        Field(description=(
            "Rotation in degrees of the saved photos: one value for both cameras or a (cam1, cam2) pair. "
            "The live preview is not rotated."
        ))
    ] = 0

    focus: Annotated[int | Literal["auto"] | tuple[int, int] | None, Field(
        description=(
            "\"auto\" (default): a sharpness sweep in front of the lit object (in the recalibration step of the "
            "preparation) finds the best position of each lens and keeps it locked for the scan. "
            f"An integer sets both lenses to that position ({VCM_MIN_POS}-{VCM_MAX_POS}; a value outside the range is "
            "moved to the nearest end). A pair [cam1, cam2] sets each lens separately (exactly two integers, "
            "clamped the same way). null uses the last focus positions saved on the Pi (the ones the previous "
            "scan, sweep or manual focus left)."
        )
    )] = "auto"

    settle_time: Annotated[float, Field(ge=0.0, le=10.0, description="Time in seconds (0 to 10) to wait for auto-exposure/auto-white-balance to settle.")] = 2.0

    # Manual controls: null (or omitted) leaves the control to the camera. 0 is never a valid exposure or gain
    # (it used to produce completely black photos), so it is rejected instead of being sent to the sensor.
    exposure_time: Annotated[int | None, Field(gt=0, description="Manual exposure time in microseconds (> 0). Disables auto-exposure.")] = None
    analogue_gain: Annotated[float | None, Field(ge=1.0, le=16.0, description="Manual analogue gain (1.0-16.0).")] = None
    colour_gains: Annotated[
        tuple[Annotated[float, Field(gt=0.0, le=32.0)], Annotated[float, Field(gt=0.0, le=32.0)]] | None,
        Field(description="Manual AWB gains (red, blue), each > 0. Disables auto white balance.")
    ] = None
    awb_mode: Annotated[str | None, Field(description="Auto white balance mode.")] = "auto"
    copy_colour_gains: Annotated[bool, Field(description=(
        "false (default): when the auto white balance is locked, each camera keeps the colour gains its own AWB "
        "settled on (exposure and gain are still shared). true: camera 1's colour gains are copied onto camera 2, "
        "which gives camera 2 a colour cast when the two sensors do not respond alike. Applies to the default "
        "rpi_sync camera mode only."
    ))] = False

    brightness: Annotated[float | None, Field(ge=-1.0, le=1.0, description="Brightness adjustment.")] = None
    contrast: Annotated[float | None, Field(gt=0.0, le=32.0, description="Contrast (> 0, 1.0 = neutral). 0 would flatten the image.")] = None
    saturation: Annotated[float | None, Field(ge=0.0, le=32.0, description="Saturation (0 = monochrome, 1.0 = neutral).")] = None
    sharpness: Annotated[float | None, Field(ge=0.0, le=16.0, description="Sharpness (0 = off, 1.0 = neutral).")] = None

    @field_validator("preview_resolution")
    @classmethod
    def _preview_size_is_sane(cls, v):
        width, height = v
        if width % 2 or height % 2 or not (160 <= width <= 4056) or not (120 <= height <= 3040):
            raise ValueError("preview_resolution must be even numbers between 160x120 and 4056x3040")
        return v

    @model_validator(mode="after")
    def _preview_not_larger_than_photo(self):
        photo_w, photo_h = self.photo_resolution
        if "preview_resolution" not in self.model_fields_set:
            # A client that lowers only the photo resolution must not be refused because of the default preview size
            self.preview_resolution = (min(self.preview_resolution[0], photo_w) // 2 * 2,
                                       min(self.preview_resolution[1], photo_h) // 2 * 2)
        if self.preview_resolution[0] > photo_w or self.preview_resolution[1] > photo_h:
            raise ValueError("preview_resolution cannot be larger than photo_resolution")
        return self

    @field_validator("colour_gains", mode="before")
    @classmethod
    def _all_null_pair_means_unset(cls, v):
        # Some clients send an empty pair as [null, null]; that means "not set", like null itself.
        # A half-filled pair ([1.5, null]) is still rejected by the type check.
        if isinstance(v, (list, tuple)) and len(v) > 0 and all(item is None for item in v):
            return None
        return v

    @staticmethod
    def _clamp_focus_position(value):
        """A lens position outside the VCM range is moved to the nearest end (numbers only; the type check follows)."""
        if isinstance(value, bool):
            raise ValueError("focus positions must be integers, not booleans")
        if isinstance(value, (int, float)):
            return max(VCM_MIN_POS, min(VCM_MAX_POS, value))
        return value

    @field_validator("focus", mode="before")
    @classmethod
    def _normalise_focus(cls, v):
        # [null, null] means "not set", like null itself (the last saved positions are used)
        if isinstance(v, (list, tuple)):
            if len(v) > 0 and all(item is None for item in v):
                return None
            if len(v) != 2:
                raise ValueError("focus must be \"auto\", an integer, null or a pair [cam1, cam2] of exactly two integers")
            return tuple(cls._clamp_focus_position(item) for item in v)
        return cls._clamp_focus_position(v)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra=_defaults_as_example)

    enable_stream: Annotated[bool, Field(
        description="Whether to start the MJPEG live preview during preparation and return its URLs."
    )] = True
    illumination_brightness: Annotated[float, Field(
        gt=0.0,
        le=1.0,
        description=(
            "Desired LED brightness (PWM) during the scan (above 0.0, up to 1.0). Defaults to 100%. "
            "0 is refused: the scanner cannot scan in the dark. Not to be confused with camera_config.brightness, "
            "which adjusts the image."
        )
    )] = 1.0
    z_start_mm: Annotated[float, Field(
        ge=0.0,
        le=Z_START_MAX_MM,
        description=(
            "Height in millimeters of the scan start position above the bottom endstop: after the homing the "
            f"carriage climbs this far (0 to {Z_START_MAX_MM:g}, default {Z_START_DEFAULT_MM:g}; 0 stays on the "
            "endstop). Must be a whole number of motor steps (multiples of 0.01 mm at 1/4 microstepping)."
        )
    )] = Z_START_DEFAULT_MM

    @field_validator("z_start_mm")
    @classmethod
    def _z_start_must_be_whole_steps(cls, value: float) -> float:
        try:
            distance_to_steps(value, Z_STEP_TYPE)
        except MotorError as e:
            raise ValueError(str(e))
        return value

    # Optional: a client that sends nothing (or only some fields) gets the defaults declared in DualCameraConfig. The
    # front-end may send its basic fields (photo quality / resolution) and leave the advanced ones out.
    camera_config: Annotated[DualCameraConfig, Field(
        description="Configuration block for the dual camera array (every field has a default)."
    )] = Field(default_factory=DualCameraConfig)


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
    model_config = ConfigDict(json_schema_extra=_defaults_as_example)

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
    model_config = ConfigDict(json_schema_extra=_defaults_as_example)

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
    model_config = ConfigDict(extra="forbid", json_schema_extra=_defaults_as_example)

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

class FaceCaptureResult(BaseModel):
    """Outcome of the last face scan, in `GET /scanner/status` (`face_result`) once it completed."""
    optimal_z_mm: float | None = None
    best_score: float | None = None
    # Whether the height search found a face (None: no search, manual_z or strategy "none"). False is a warning, not
    # an error: the photo was taken at the start height
    face_detected: bool | None = None
    warning: str | None = None
    photos: list[str] = []
    r2_uploaded: bool = False


class StatusResponse(BaseModel):
    state: str
    # What the last prepare readied the rig for: "object" (/scanner/prepare), "face" (/scanner/face/prepare), or null
    mode: str | None = None
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
    face_result: FaceCaptureResult | None = None   # set when a face scan completed (POST /scanner/face/capture)
    # Upload progress (E2): seconds since the last successful upload (or since the drain started), null when no upload
    # is running; the drain gives up after upload_drain_timeout_s without one
    seconds_since_last_upload: float | None = None
    upload_drain_timeout_s: float | None = None

class CancelResponse(BaseModel):
    status: str = "success"
    state: str = "cancelled"

class StartResponse(BaseModel):
    status: str = "accepted"
    message: str = "Scan started"


class StreamActiveRequest(BaseModel):
    """JSON body of `POST /scanner/stream/active` (and `/test/stream/active`); the query parameter works as well."""
    camera_id: Annotated[int, Field(
        ge=1,
        le=2,
        description="Camera ID to display in preview (1 for cam1, 2 for cam2)."
    )]


class StreamQualityRequest(BaseModel):
    """JSON body of `POST /scanner/stream/quality` (and `/test/stream/quality`); the query parameter works as well."""
    quality: Annotated[int, Field(
        ge=1,
        le=100,
        description="JPEG compression quality (1 to 100)."
    )]


def _whole_z_steps_check(value: float | None, field_name: str) -> float | None:
    # Every height and distance of the face scan becomes a Z move: a value that is not a whole number of motor steps
    # would otherwise fail only after the carriage moved (FACE-9)
    if value is None:
        return value
    try:
        distance_to_steps(value, step_type=Z_STEP_TYPE)
    except MotorError as e:
        raise ValueError(f"{field_name} is not reachable with Z motor steps: {e}")
    return value


class FacePrepareRequest(BaseModel):
    """
    Body of `POST /scanner/face/prepare`: homing, climb to the start height, lights, cameras streaming. The rig then
    waits (state `prepared`, mode `face`) for `POST /scanner/face/capture`.
    """
    model_config = ConfigDict(extra="forbid", json_schema_extra=_defaults_as_example)

    enable_stream: Annotated[bool, Field(
        description="Whether to start the MJPEG live preview and return its URLs."
    )] = True
    illumination_brightness: Annotated[float, Field(
        gt=0.0,
        le=1.0,
        description="Chamber LED brightness (PWM), above 0 up to 1 (default 0.5: not to blind the subject)."
    )] = 0.5
    start_z_mm: Annotated[float, Field(
        ge=100.0,
        le=350.0,
        description="Height in mm above the bottom endstop where the carriage waits and the face search starts."
    )] = 180.0
    upload_r2: Annotated[bool, Field(
        description="Upload the photo pair to Cloudflare R2 and verify it (the bucket is cleaned by this prepare)."
    )] = True
    camera_config: Annotated[DualCameraConfig, Field(
        description=(
            "Camera settings. focus \"auto\" (default): the lenses stay on the saved position during the search and "
            "an autofocus sweep runs on the face at the height found; an integer or a pair: that position, no "
            "autofocus. exposure_time / analogue_gain / colour_gains: used as given, otherwise AE/AWB settle on the "
            "face and are locked before the photo."
        )
    )] = Field(default_factory=DualCameraConfig)

    @field_validator("start_z_mm")
    @classmethod
    def _whole_z_steps(cls, value: float, info) -> float:
        return _whole_z_steps_check(value, info.field_name)


class FaceCaptureRequest(BaseModel):
    """
    Body of `POST /scanner/face/capture` (after `POST /scanner/face/prepare`): how to find the height, then the photo.
    """
    model_config = ConfigDict(extra="forbid", json_schema_extra=_defaults_as_example)

    strategy: Annotated[Literal["sweep_best_score", "direct_stop", "none"], Field(
        description=(
            "'sweep_best_score' (search the range, go back to the best height), 'direct_stop' (stop as soon as the "
            "face is vertically centered) or 'none' (photo at the prepared height, no search)."
        )
    )] = "sweep_best_score"

    search_range_mm: Annotated[float, Field(
        ge=10.0,
        le=250.0,
        description="How far above the prepared start height the search goes (the end must stay in the Z travel)."
    )] = 150.0

    step_mm: Annotated[float, Field(
        ge=1.0,
        le=25.0,
        description="Climb step in mm between MediaPipe framing evaluations (must be whole Z motor steps)."
    )] = 8.0

    manual_z: Annotated[float | None, Field(
        ge=200.0,
        le=Z_DISTANCE_MAX_MM,
        description="Optional explicit height in mm (>= 200 mm). Skips the search when set."
    )] = None

    settle_s: Annotated[float, Field(
        ge=0.05,
        le=2.0,
        description="Mechanical settling delay in seconds before taking the photo."
    )] = 0.150

    @field_validator("step_mm", "search_range_mm", "manual_z")
    @classmethod
    def _whole_z_steps(cls, value: float | None, info) -> float | None:
        return _whole_z_steps_check(value, info.field_name)


class FaceCaptureResponse(BaseModel):
    """`POST /scanner/face/capture` (202): the face scan was accepted and runs in the background; poll /scanner/status."""
    status: str = "accepted"
    message: str = "Face scan started. Poll GET /scanner/status until the state is completed."
    state: str = "running"

