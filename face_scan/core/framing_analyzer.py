"""
Stereo Framing Analyzer: Dual-Camera Landmark Tracking & Framing Evaluation
============================================================================
Evaluates face framing across both cameras simultaneously at 27° convergence.
"""

import sys
from pathlib import Path
from typing import Dict, Any, Optional
import numpy as np

# Add project root and face_scan directory to sys.path so modules resolve in any environment
_FACE_SCAN_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _FACE_SCAN_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_FACE_SCAN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from face_scan.face_scanner import FaceScanner
except (ImportError, ValueError):
    from face_scanner import FaceScanner


# Weight of the share of face landmarks inside the frame in the score of one camera (0..1 -> 0..VISIBILITY_WEIGHT)
VISIBILITY_WEIGHT = 0.25


class StereoFramingAnalyzer:
    """
    Evaluates face framing across both cameras simultaneously.

    Convergence & Geometry (27° convergence + 15° pitch down):
      - Camera 0 (Left): Looks at subject's right cheek, right ear (lm 234/127), nose, and right eye.
      - Camera 1 (Right): Looks at subject's left cheek, left ear (lm 454/356), nose, and left eye.
      - Shared Central Features: Nose tip (lm 1), mouth, inner eye corners.
    """
    def __init__(self, face_scanner: Optional[FaceScanner] = None):
        self.scanner = face_scanner

    def evaluate_frame(self, frame_bgr: np.ndarray, cam_index: int) -> Dict[str, Any]:
        """Runs MediaPipe on a single camera frame and computes camera-specific framing metrics."""
        cam_label = "Left (Cam 0)" if cam_index == 0 else "Right (Cam 1)"
        if self.scanner is None or frame_bgr is None:
            return {
                "detected": False,
                "score": 0.0,
                "nose_y": None,
                "nose_x": None,
                "lateral_visible": False,
                "visible_ratio": 0.0,
                "cam_label": cam_label
            }

        scan_res = self.scanner.scan_image(frame_bgr)
        faces = scan_res.get("faces", [])

        if not faces:
            return {
                "detected": False,
                "score": 0.0,
                "nose_y": None,
                "nose_x": None,
                "lateral_visible": False,
                "visible_ratio": 0.0,
                "cam_label": cam_label
            }

        face = faces[0]
        key_pts = face.get("key_points", {})
        nose = key_pts.get("nose_tip")

        if not nose:
            return {
                "detected": False,
                "score": 0.0,
                "nose_y": None,
                "nose_x": None,
                "lateral_visible": False,
                "visible_ratio": 0.0,
                "cam_label": cam_label
            }

        norm_y = nose["normalized"][1]
        norm_x = nose["normalized"][0]

        # 1. Centering score: maximum (1.0) when nose is at Y = 0.50 (vertical optical center)
        y_dist = abs(norm_y - 0.50)
        centering_score = max(0.0, 1.0 - (2.0 * y_dist))

        # 2. Horizontal centering penalty (if subject is too far off-center)
        x_dist = abs(norm_x - 0.50)
        h_penalty = max(0.0, (x_dist - 0.20) * 1.5) if x_dist > 0.20 else 0.0

        # 3. Face size score: optimal when face occupies >= 40% frame height
        bbox = face["bounding_box"]
        h_frame = frame_bgr.shape[0]
        face_height_ratio = (bbox["ymax"] - bbox["ymin"]) / float(h_frame)
        size_score = min(1.0, face_height_ratio / 0.40)

        # 4. Check dedicated lateral profile landmark for this camera
        # Cam 0 (Left camera) looks at subject's right cheek (Landmark 234)
        # Cam 1 (Right camera) looks at subject's left cheek (Landmark 454)
        lateral_idx = 234 if cam_index == 0 else 454
        norm_landmarks = face.get("normalized_landmarks", [])
        lateral_visible = False
        if len(norm_landmarks) > lateral_idx:
            lm_lat = norm_landmarks[lateral_idx]
            # Ensure landmark is within frame bounds [0.02, 0.98]
            if 0.02 <= lm_lat["x"] <= 0.98 and 0.02 <= lm_lat["y"] <= 0.98:
                lateral_visible = True

        lateral_bonus = 0.15 if lateral_visible else 0.0

        # 5. Share of the face mesh inside the frame. MediaPipe returns all 478 landmarks whenever it finds a face (the
        # ones outside the picture are extrapolated), so this is what "how many facial points were found" means here:
        # it drops when the forehead or the chin is cut by the edge of the frame.
        visible = sum(1 for lm in norm_landmarks if 0.0 <= lm["x"] <= 1.0 and 0.0 <= lm["y"] <= 1.0)
        visible_ratio = visible / float(len(norm_landmarks)) if norm_landmarks else 0.0
        visibility_bonus = VISIBILITY_WEIGHT * visible_ratio

        composite_cam_score = max(
            0.0, (centering_score * 0.65 + size_score * 0.20 + lateral_bonus + visibility_bonus) - h_penalty
        )

        # Face box as frame fractions (x, y, w, h): the autofocus scores the sharpness there
        w_frame = frame_bgr.shape[1]
        bbox_norm = (
            bbox["xmin"] / float(w_frame), bbox["ymin"] / float(h_frame),
            (bbox["xmax"] - bbox["xmin"]) / float(w_frame), face_height_ratio,
        )

        return {
            "detected": True,
            "score": float(composite_cam_score),
            "nose_y": float(norm_y),
            "nose_x": float(norm_x),
            "lateral_visible": lateral_visible,
            "visible_ratio": float(visible_ratio),
            "bbox": bbox,
            "bbox_norm": bbox_norm,
            "cam_label": cam_label
        }

    def evaluate_stereo_pair(
        self,
        frame_left: np.ndarray,
        frame_right: np.ndarray
    ) -> Dict[str, Any]:
        """Combines detections from Left (Cam 0) and Right (Cam 1) cameras."""
        left_eval = self.evaluate_frame(frame_left, 0)
        right_eval = self.evaluate_frame(frame_right, 1)

        both_detected = left_eval["detected"] and right_eval["detected"]
        any_detected = left_eval["detected"] or right_eval["detected"]

        if not both_detected:
            score = 0.5 * (left_eval["score"] + right_eval["score"])
            avg_nose_y = left_eval["nose_y"] if left_eval["detected"] else right_eval["nose_y"]
        else:
            # Stereo lock: both cameras see their respective half of the face
            score = left_eval["score"] + right_eval["score"]
            avg_nose_y = 0.5 * (left_eval["nose_y"] + right_eval["nose_y"])
            # Bonus when both lateral ears/cheeks are clearly captured
            if left_eval.get("lateral_visible") and right_eval.get("lateral_visible"):
                score += 0.20

        return {
            "both_detected": both_detected,
            "any_detected": any_detected,
            "total_score": float(score),
            "avg_nose_y": avg_nose_y,
            "left": left_eval,
            "right": right_eval
        }
