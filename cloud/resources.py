"""
resources.py — Peak RAM estimate of the Turntable Studio pipeline, used to size the RunPod pod.

Copied from Turntable Studio's pipeline/resources.py (the Pi does not have the pipeline package). Keep the
constants in sync with that file when its RAM model is refit (tools/fit_ram_model.py there).
"""

from __future__ import annotations

# DensifyPointCloud dominates: A + B * N * P (N photos, P working pixels)
RAM_MODEL_A_GB = 6.8
RAM_MODEL_B_BYTES_PER_PX = 12.0
# Other steps never exceed this on the measured sets (ReconstructMesh 4.7 GB, texrecon 4.6 GB)
RAM_FLOOR_GB = 5.0


def working_megapixels(width: int, height: int, resolution_level: int) -> float:
    """Megapixels DensifyPointCloud works on: every resolution level halves both sides."""
    return (float(width) * float(height)) / (4 ** max(0, int(resolution_level))) / 1e6


def estimate_ram_gb(
    n_images: int,
    width: int = 4056,
    height: int = 3040,
    resolution_level: int = 1,
    a_gb: float = RAM_MODEL_A_GB,
    b_bytes_per_px: float = RAM_MODEL_B_BYTES_PER_PX,
) -> float:
    """
    Peak RAM (GB) of the pipeline, dominated by DensifyPointCloud:
    A + B * N * P, at least RAM_FLOOR_GB. Without margin: callers add their own.
    """
    p = working_megapixels(width, height, resolution_level)
    densify = a_gb + b_bytes_per_px * n_images * p * 1e6 / 1e9
    return max(RAM_FLOOR_GB, densify)
