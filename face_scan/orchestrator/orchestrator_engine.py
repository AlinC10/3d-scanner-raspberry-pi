"""
Orchestrator Engine: Autonomous Mechatronic 3D Scanning Cycle
============================================================
Coordinates Z-axis carriage motion, dual-camera lores auto-framing,
vibration settling, 12.3MP synchronized photo capture, and 3D reconstruction.
"""

import sys
import time
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# Ensure project root and face_scan directory are in sys.path
_FACE_SCAN_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _FACE_SCAN_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_FACE_SCAN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from face_scan.core.framing_analyzer import StereoFramingAnalyzer
from face_scan.face_scanner import FaceScanner
from face_scan.hardware_bridge import (
    HARDWARE_AVAILABLE,
    MockMotor,
    MockCameras,
    MockEndstop,
    Motor,
    Endstop,
    TopEndstopTriggered,
    DualCameraRPISync,
    Z_STEP_TYPE,
    Z_TRAVEL_MAX_MM,
)

log = logging.getLogger("3DScanner.Orchestrator")


class FaceScannerOrchestrator:
    """
    Orchestrates the entire scanning cycle:
      - Motor movement & endstop surveillance
      - Dual camera lores streaming and auto-framing
      - Apex detection & optimal height return with backlash elimination
      - Synchronized stereo 12.3MP photo capture
      - Fused dual-camera 3D facial reconstruction
    """
    def __init__(
        self,
        use_mock: bool = False,
        test_image_path: Optional[str] = None,
        strategy: str = "sweep_best_score",
        step_distance_mm: float = 8.0,
        max_travel_mm: float = 150.0,
        settle_delay_s: float = 0.150,
        output_dir: str = "./output_scan"
    ):
        self.use_mock = use_mock or not HARDWARE_AVAILABLE
        self.strategy = strategy
        self.step_distance_mm = step_distance_mm
        self.max_travel_mm = max_travel_mm
        self.settle_delay_s = settle_delay_s
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.current_z_mm = 0.0

        # 1. Initialize MediaPipe Face Scanner & Stereo Analyzer
        log.info("Initializing MediaPipe Face Landmarker...")
        self.scanner = FaceScanner(num_faces=1)
        self.framing_analyzer = StereoFramingAnalyzer(self.scanner)

        # 2. Initialize Hardware / Mocks
        if self.use_mock:
            log.info("Initializing in MOCK HARDWARE mode.")
            self.motor = MockMotor(motor_id=1)
            self.cameras = MockCameras(test_image_path=test_image_path)
            self.up_endstop = MockEndstop(pin=22, label="TOP")
            self.down_endstop = MockEndstop(pin=4, label="BOTTOM")
        else:
            log.info("Connecting to REAL HARDWARE...")
            self.motor = Motor(motor_id=1, dir_pin=4, step_pin=5, en_pin=3, step_type=Z_STEP_TYPE)
            self.up_endstop = Endstop(pin=22, pull_up=True, bounce_time=0.02)
            self.down_endstop = Endstop(pin=4, pull_up=True, bounce_time=0.02)
            self.cameras = DualCameraRPISync(
                resolution=(4056, 3040),
                preview_resolution=(960, 720),
                framerate=10
            )
            self.cameras.prepare_scan()

    def check_top_endstop(self) -> bool:
        """Returns True if the top physical limit switch is pressed."""
        if hasattr(self.up_endstop, "is_active"):
            return bool(self.up_endstop.is_active)
        return False

    def check_bottom_endstop(self) -> bool:
        """Returns True if the bottom physical limit switch is pressed."""
        if hasattr(self.down_endstop, "is_active"):
            return bool(self.down_endstop.is_active)
        return False

    def home_to_bottom(self):
        """Homes the carriage by descending until the bottom limit switch is hit."""
        log.info("Homing Z-axis to bottom endstop (GPIO 4)...")
        if self.use_mock:
            self.current_z_mm = 0.0
            log.info("[MOCK] Homing complete. Z = 0.0 mm")
            return

        # Real hardware homing
        try:
            self.motor.enable()
            if self.check_bottom_endstop():
                log.info("Already at bottom limit switch.")
                self.current_z_mm = 0.0
                return

            steps_budget = self.motor.distance_to_step_conversion(Z_TRAVEL_MAX_MM + 20)
            self.motor.rotate(clockwise=False, steps=steps_budget, delay=0.0010)
        except Exception as e:
            log.warning("Homing notice: %s", e)
        finally:
            self.current_z_mm = 0.0

    def move_z_up(self, distance_mm: float) -> bool:
        """Commands the Z axis motor to climb upward by distance_mm."""
        if self.check_top_endstop():
            log.warning("Top endstop is ALREADY triggered! Cannot climb further.")
            return False

        if self.use_mock:
            steps = self.motor.distance_to_step_conversion(distance_mm)
            self.motor.rotate(clockwise=True, steps=steps)
            self.current_z_mm += distance_mm
            if self.current_z_mm >= self.max_travel_mm:
                self.up_endstop.simulate_trigger(True)
            return True
        else:
            try:
                self.motor.enable()
                steps = self.motor.distance_to_step_conversion(distance_mm)
                success = self.motor.rotate(clockwise=True, steps=steps, delay=0.0010)
                self.current_z_mm += distance_mm
                return success
            except TopEndstopTriggered:
                log.warning("TopEndstopTriggered during climb!")
                return False
            except Exception as e:
                log.error("Motor climb failed: %s", e)
                return False

    def move_z_down(self, distance_mm: float) -> bool:
        """Commands the Z axis motor to descend downward by distance_mm."""
        if self.check_bottom_endstop():
            log.warning("Bottom endstop is ALREADY reached! Cannot descend further.")
            return False

        if self.use_mock:
            steps = self.motor.distance_to_step_conversion(distance_mm)
            self.motor.rotate(clockwise=False, steps=steps)
            self.current_z_mm = max(0.0, self.current_z_mm - distance_mm)
            return True
        else:
            try:
                self.motor.enable()
                steps = self.motor.distance_to_step_conversion(distance_mm)
                success = self.motor.rotate(clockwise=False, steps=steps, delay=0.0010)
                self.current_z_mm = max(0.0, self.current_z_mm - distance_mm)
                return success
            except Exception as e:
                log.error("Motor descent failed: %s", e)
                return False

    def move_to_height(self, target_z_mm: float, compensate_backlash: bool = True) -> bool:
        """
        Moves the carriage precisely to target_z_mm.
        Uses anti-backlash compensation if moving downward.
        """
        delta = target_z_mm - self.current_z_mm
        if abs(delta) < 0.2:
            return True

        if delta > 0:
            return self.move_z_up(delta)
        else:
            descent_distance = abs(delta)
            if compensate_backlash:
                overshoot_mm = 3.0
                total_descent = descent_distance + overshoot_mm
                log.info("Backlash Compensation: Descending %.2f mm then climbing +%.1f mm...", total_descent, overshoot_mm)
                self.move_z_down(total_descent)
                time.sleep(0.05)
                return self.move_z_up(overshoot_mm)
            else:
                return self.move_z_down(descent_distance)

    def run_auto_framing(self) -> float:
        """
        Executes autonomous vertical sweep scanning:
          - Evaluates dual-camera lores streams at each step
          - Supports both 'sweep_best_score' and 'direct_stop'
        """
        log.info("=" * 60)
        log.info("STARTING AUTO-FRAMING SCAN (Strategy: %s)", self.strategy)
        log.info("Step distance: %.1f mm | Max Travel: %.1f mm", self.step_distance_mm, self.max_travel_mm)
        log.info("=" * 60)

        best_score = -1.0
        best_z = self.current_z_mm
        passed_apex_count = 0
        step_idx = 0

        while self.current_z_mm < self.max_travel_mm:
            if self.check_top_endstop():
                log.warning("Top endstop switch hit at Z = %.1f mm. Stopping climb.", self.current_z_mm)
                break

            # 1. Grab synchronized lores frames from Left (0) and Right (1)
            frame_left = self.cameras.get_preview_frame(0, sim_vertical_shift=self.current_z_mm)
            frame_right = self.cameras.get_preview_frame(1, sim_vertical_shift=self.current_z_mm)

            # 2. Evaluate dual-camera framing score
            eval_res = self.framing_analyzer.evaluate_stereo_pair(frame_left, frame_right)
            score = eval_res["total_score"]
            avg_nose_y = eval_res["avg_nose_y"]
            log.info(
                "[Step %02d | Z=%5.1f mm] Score: %4.2f (L: %4.2f, R: %4.2f) | Nose Y: %s",
                step_idx,
                self.current_z_mm,
                score,
                eval_res["left"]["score"],
                eval_res["right"]["score"],
                f"{avg_nose_y:.3f}" if avg_nose_y is not None else "N/A"
            )

            # Strategy A: Direct stop
            if self.strategy == "direct_stop" and eval_res["both_detected"] and avg_nose_y is not None:
                if 0.45 <= avg_nose_y <= 0.55:
                    log.info("DIRECT STOP TRIGGERED! Face centered at Z=%.1f mm (Nose Y=%.3f)", self.current_z_mm, avg_nose_y)
                    best_z = self.current_z_mm
                    break

            # Strategy B: Sweep best score
            if score > best_score:
                best_score = score
                best_z = self.current_z_mm
                passed_apex_count = 0
            else:
                if best_score > 1.2:
                    passed_apex_count += 1
                    if passed_apex_count >= 7:
                        log.info("EARLY EXIT: Scored passed apex (Best: %.2f at %.1f mm). Climbed past forehead.", best_score, best_z)
                        break

            # 3. Climb next step
            success = self.move_z_up(self.step_distance_mm)
            if not success:
                break

            step_idx += 1
            time.sleep(0.05)

        log.info("-" * 60)
        log.info("SWEEP COMPLETE. Best score: %.2f found at Z = %.1f mm.", best_score, best_z)
        log.info("-" * 60)

        # Reposition carriage directly to the optimal height with backlash compensation
        if abs(self.current_z_mm - best_z) > 0.5:
            log.info("Returning carriage to optimal capture height: %.1f mm...", best_z)
            self.move_to_height(best_z, compensate_backlash=True)

        return best_z

    def capture_stereo_photo(
        self,
        name_prefix: str = "scan",
        save_to_input_images_rig: bool = True
    ) -> Tuple[Path, Path]:
        """
        Pauses for mechanical stabilization then triggers synchronized stereo capture.
        Saves 12.3MP full resolution images directly into the target directories.
        """
        log.info("Settling carriage for %.0f ms to damp vibration...", self.settle_delay_s * 1000)
        time.sleep(self.settle_delay_s)

        # Check if output_dir is already the rig directory (ends with 'rig')
        if self.output_dir.name == "rig" or (self.output_dir / "0").is_dir():
            dir_0 = self.output_dir / "0"
            dir_1 = self.output_dir / "1"
            dir_0.mkdir(parents=True, exist_ok=True)
            dir_1.mkdir(parents=True, exist_ok=True)
            left_path = dir_0 / f"{name_prefix}_left_12mp.jpg"
            right_path = dir_1 / f"{name_prefix}_right_12mp.jpg"
        else:
            left_path = self.output_dir / f"{name_prefix}_left_12mp.jpg"
            right_path = self.output_dir / f"{name_prefix}_right_12mp.jpg"

        log.info("TRIGGERING 12.3MP STEREO CAPTURE...")
        self.cameras.capture_photo(str(left_path), str(right_path))
        log.info("Saved Camera 0 (Left):  %s", left_path)
        log.info("Saved Camera 1 (Right): %s", right_path)

        # Ensure standard rig/0 and rig/1 directories are populated
        if save_to_input_images_rig:
            import shutil
            rig_dir = Path("input_images/rig").resolve()
            rig_0 = rig_dir / "0"
            rig_1 = rig_dir / "1"
            rig_0.mkdir(parents=True, exist_ok=True)
            rig_1.mkdir(parents=True, exist_ok=True)

            target_0 = rig_0 / left_path.name
            target_1 = rig_1 / right_path.name
            if target_0.resolve() != left_path.resolve():
                shutil.copy2(str(left_path), str(target_0))
                shutil.copy2(str(right_path), str(target_1))
                log.info("Synchronized copies into rig folders: %s & %s", target_0, target_1)

        # Also keep a local mirror inside output_dir/rig if output_dir is not input_images/rig
        if self.output_dir.name != "rig" and not (self.output_dir / "0").is_dir():
            local_rig_0 = self.output_dir / "rig" / "0"
            local_rig_1 = self.output_dir / "rig" / "1"
            local_rig_0.mkdir(parents=True, exist_ok=True)
            local_rig_1.mkdir(parents=True, exist_ok=True)
            local_t0 = local_rig_0 / left_path.name
            local_t1 = local_rig_1 / right_path.name
            if local_t0.resolve() != left_path.resolve():
                import shutil
                shutil.copy2(str(left_path), str(local_t0))
                shutil.copy2(str(right_path), str(local_t1))

        return left_path, right_path

    def upload_to_cloudflare_r2(self, left_photo_path: Path, right_photo_path: Path) -> Dict[str, Any]:
        """
        Dedicated hook for uploading stereo photos to Cloudflare R2.
        """
        log.info("=" * 60)
        log.info("CLOUDFLARE R2 UPLOAD HOOK")
        log.info("=" * 60)

        log.info("[R2 HOOK] Camera 0 (Left) photo: %s", left_photo_path)
        log.info("[R2 HOOK] Camera 1 (Right) photo: %s", right_photo_path)

        # Upload metadata payload
        r2_result = {
            "status": "ready_for_upload",
            "left_photo": str(left_photo_path),
            "right_photo": str(right_photo_path),
            "left_r2_url": f"https://r2.your-domain.com/scans/{left_photo_path.name}",
            "right_r2_url": f"https://r2.your-domain.com/scans/{right_photo_path.name}",
        }
        log.info("[R2 HOOK] Photos prepared for R2 synchronization: %s", r2_result)
        return r2_result

    def reconstruct_3d_face(self, left_photo_path: Path, right_photo_path: Path) -> Optional[Dict[str, Any]]:
        """
        Runs the canonical 3D Face Reconstruction fusing BOTH stereo views.
        Produces OBJ, MTL, Fused Stereo UV 2K/4K PNG, GLB, STL, and 3MF.
        """
        log.info("=" * 60)
        log.info("RUNNING STEREO 3D RECONSTRUCTION PIPELINE (Step 1 Canonical Mesh)...")
        log.info("Fusing Left Camera (Right Cheek) + Right Camera (Left Cheek)...")
        log.info("=" * 60)

        try:
            from reconstruct_3d import Face3DReconstructor  # type: ignore
        except ImportError:
            log.warning(
                "Local 3D reconstruction module 'reconstruct_3d' is not installed. "
                "In this scanner rig architecture, 3D reconstruction is run in the cloud via RunPod/Meshroom."
            )
            return None

        with Face3DReconstructor() as reconstructor:
            res = reconstructor.reconstruct_from_stereo_pair(
                left_image_input=str(left_photo_path),
                right_image_input=str(right_photo_path),
                output_dir=str(self.output_dir),
                name_prefix="face_mesh",
                tex_size=2048
            )
            log.info("Reconstruction finished successfully!")
            log.info("3D Deliverables generated in: %s", self.output_dir.resolve())
            log.info("  -> OBJ: %s | %s", res["obj"]["high"].relative_to(self.output_dir), res["obj"]["low"].relative_to(self.output_dir))
            log.info("  -> GLB: %s | %s", res["glb"]["high"].relative_to(self.output_dir), res["glb"]["low"].relative_to(self.output_dir))
            log.info("  -> STL: %s | %s", res["stl"]["high"].relative_to(self.output_dir), res["stl"]["low"].relative_to(self.output_dir))
            log.info("  -> 3MF: %s | %s", res["3mf"]["high"].relative_to(self.output_dir), res["3mf"]["low"].relative_to(self.output_dir))
            return res

    def close(self):
        """Cleanly powers down motor holding torque and releases camera streams."""
        log.info("Shutting down scanner orchestrator and releasing hardware...")
        try:
            self.motor.stop()
            self.motor.disable(settle_delay=0.2)
        except Exception as e:
            log.warning("Motor release error: %s", e)
        if hasattr(self, "cameras") and hasattr(self.cameras, "close"):
            try:
                self.cameras.close()
            except Exception as e:
                log.warning("Camera release error: %s", e)
