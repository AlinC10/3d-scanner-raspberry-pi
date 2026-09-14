import math

def calculate_required_ram(
        num_images: int,
        resolution_mp: float = 12.0,
        depthmap_downscale: int = 2,
        max_input_points: int = 10000000,
        two_sides_mode: bool = False
) -> int:
    """
    Empirical equation for calculating Meshroom RAM requirements.
    RAM_GB = O + (N * R * K) + (P / 1.5e6)

    :param num_images: Number of input photos
    :type num_images: int
    :param resolution_mp: Resolution of the photos in megapixels
    :type resolution_mp: float
    :param depthmap_downscale: Downscale factor for the DepthMap node (typically 1 or 2)
    :type depthmap_downscale: int
    :param max_input_points: The maxInputPoints parameter for the Meshing node
    :type max_input_points: int
    :param two_sides_mode: Whether the pipeline is running in two-sides mode (doubles image load)
    :type two_sides_mode: bool
    :return: The calculated safe minimum RAM requirement in GB (including a 10% safety buffer)
    :rtype: int
    """
    overhead = 4.0

    if two_sides_mode:
        # Two sides mode effectively doubles the images processed before merging
        num_images *= 2

    k_coef = 0.06 if depthmap_downscale == 1 else 0.02

    # Calculate components
    base_ram = overhead
    feature_ram = num_images * resolution_mp * k_coef
    meshing_ram = max_input_points / 1_500_000.0

    total_ram_gb = base_ram + feature_ram + meshing_ram

    # Add a small 10% safety buffer and round up to the nearest integer
    safe_ram_gb = math.ceil(total_ram_gb * 1.1)

    return safe_ram_gb


if __name__ == "__main__":
    # Quick test
    test_n = 100
    print(f"RAM for {test_n} images, downscale 2, 10M points: {calculate_required_ram(test_n)} GB")
    print(
        f"RAM for {test_n} images, downscale 1, 10M points: {calculate_required_ram(test_n, depthmap_downscale=1)} GB")
