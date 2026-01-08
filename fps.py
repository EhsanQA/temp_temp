#!/usr/bin/env python3

def minutes_until_full_from_bitrate(storage_gb: float, bitrate_mbps: float, use_gib: bool = True) -> float:
    """
    storage_gb: 80 means 80 GB (decimal) or 80 GiB (binary) depending on use_gib
    bitrate_mbps: megabits per second (e.g., 10 means 10 Mbps)
    use_gib=True => 1 GiB = 1024^3 bytes, else 1 GB = 10^9 bytes
    """
    bytes_total = storage_gb * (1024**3 if use_gib else 10**9)
    bits_per_sec = bitrate_mbps * 1_000_000
    seconds = (bytes_total * 8) / bits_per_sec
    return seconds / 60


def minutes_until_full_uncompressed(storage_gb: float, width: int, height: int, fps: float, bits_per_pixel: float, use_gib: bool = True) -> float:
    """
    Uncompressed estimate:
    bytes/sec = width * height * bits_per_pixel/8 * fps
    Examples:
      RGB24 => bits_per_pixel=24
      YUV420 => bits_per_pixel=12 (approx)
    """
    bytes_total = storage_gb * (1024**3 if use_gib else 10**9)
    bytes_per_frame = width * height * (bits_per_pixel / 8.0)
    bytes_per_sec = bytes_per_frame * fps
    seconds = bytes_total / bytes_per_sec
    return seconds / 60


if __name__ == "__main__":
    STORAGE_GB = 80
    WIDTH, HEIGHT = 1920, 1080
    FPS = 10

    # --- Compressed (YOU choose bitrate) ---
    for bitrate in [5, 10, 20]:  # Mbps examples
        mins = minutes_until_full_from_bitrate(STORAGE_GB, bitrate_mbps=bitrate, use_gib=True)
        print(f"Compressed @ {bitrate:>2} Mbps -> {mins:.1f} minutes (assuming 80 GiB)")

    # --- Uncompressed examples ---
    mins_rgb24 = minutes_until_full_uncompressed(STORAGE_GB, WIDTH, HEIGHT, FPS, bits_per_pixel=24, use_gib=True)
    mins_yuv420 = minutes_until_full_uncompressed(STORAGE_GB, WIDTH, HEIGHT, FPS, bits_per_pixel=12, use_gib=True)
    print(f"Uncompressed RGB24 -> {mins_rgb24:.2f} minutes (80 GiB)")
    print(f"Uncompressed YUV420 -> {mins_yuv420:.2f} minutes (80 GiB)")
