#!/usr/bin/env python3
"""Precompute visibility heatmap by casting camera rays against a DEM.

Usually run for you by process_flights.py, which fills in the area paths.

Usage:
    # Output filenames are derived from the telemetry filename by default:
    # <basename>-telemetry.json -> <output-dir>/visibility/<basename>.{bin,json}
    python3 dem/compute_visibility.py \
        --telemetry dem/areas/quoin/telemetry/2026-03-30-foo-telemetry.json \
        --dem dem/areas/quoin/area-dem.tif --output-dir dem/areas/quoin

    # Or pass --name explicitly (useful when the telemetry path doesn't follow the convention):
    python3 dem/compute_visibility.py --telemetry path/to/telem.json --name 2026-03-30-foo \
        --dem dem/areas/quoin/area-dem.tif --output-dir dem/areas/quoin

Output:
    <output-dir>/visibility/<name>.bin   — Float32Array, row-major
    <output-dir>/visibility/<name>.json  — metadata sidecar

The viewer (index.html) fetches dem/areas/<area>/visibility/<basename>.* by
basename derived from the loaded MP4 filename.
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import rasterio

# ── Configurable parameters ──
TEMPORAL_SAMPLE_RATE = 1.0    # Hz — sample telemetry every N seconds
RAY_GRID_H = 50               # rays across horizontal FOV
RAY_GRID_V = 40               # rays across vertical FOV
RAY_STEP_M = 2.0              # step size in metres
MAX_RANGE_M = 1000.0           # max ray march distance
SCORE_EXPONENT = 2.0           # score = 1/d^n
OUTPUT_RESOLUTION_M = 10.0     # output grid cell size
SENSOR_WIDTH_MM = 9.6          # M4E sensor width  } full 4:3 sensor that the 35 mm-equivalent focal
SENSOR_HEIGHT_MM = 7.2         # M4E sensor height } length refers to; only the aspect affects FOV
FRAME_ASPECT = 16 / 9          # video frame: full sensor width, top/bottom cropped (3840x2160 from 5280x3956)

# Bumped when the camera/terrain model changes enough that existing heatmaps are stale
# (process_flights.py recomputes anything written by an older version).
MODEL_VERSION = 2


def compute_fov(sensor_w_mm, sensor_h_mm, equiv_focal_mm, frame_aspect=FRAME_ASPECT):
    """Compute H/V FOV of a video frame from sensor dimensions and 35mm-equivalent focal length.

    The equivalent focal length is quoted for the full sensor (84° diagonal at
    24 mm for the M4E). Video keeps the full sensor width and crops the height
    to the frame aspect ratio.
    """
    diag_mm = math.sqrt(sensor_w_mm**2 + sensor_h_mm**2)
    # actual_fl = equiv_focal / crop_factor, crop_factor = 43.27 / diag
    crop_factor = 43.27 / diag_mm  # 43.27mm is 35mm format diagonal
    actual_fl = equiv_focal_mm / crop_factor
    frame_h_mm = min(sensor_h_mm, sensor_w_mm / frame_aspect)
    h_fov = 2 * math.atan(sensor_w_mm / (2 * actual_fl))
    v_fov = 2 * math.atan(frame_h_mm / (2 * actual_fl))
    return math.degrees(h_fov), math.degrees(v_fov)


def parse_aspect(value):
    """'16:9', '3840x2160' or '1.7778' -> width / height."""
    for sep in (":", "x"):
        if sep in value:
            w, h = value.split(sep)
            return float(w) / float(h)
    return float(value)


def load_dem_rasterio(dem_path):
    """Load DEM using rasterio. Returns (data, transform, bounds)."""
    with rasterio.open(dem_path) as src:
        data = src.read(1)
        transform = src.transform
        bounds = src.bounds
    # Replace NoData
    nodata_mask = data < -1e30
    if np.any(nodata_mask):
        valid = data[~nodata_mask]
        data[nodata_mask] = float(np.min(valid)) if valid.size > 0 else 0.0
    return data, transform, bounds


class DEMLookup:
    """Fast DEM elevation lookup with bilinear interpolation."""

    def __init__(self, data, transform, bounds):
        self.data = data.astype(np.float32)
        self.height, self.width = data.shape
        self.west = transform.c
        self.north = transform.f
        self.pixel_size_lon = transform.a
        self.pixel_size_lat = -transform.e
        self.bounds = bounds

    def get_elevation_batch(self, lats, lons):
        """Bilinear interpolation for arrays of lat/lon. Returns elevations."""
        cols = (lons - self.west) / self.pixel_size_lon
        rows = (self.north - lats) / self.pixel_size_lat

        c0 = np.floor(cols).astype(np.int32)
        r0 = np.floor(rows).astype(np.int32)
        c1 = c0 + 1
        r1 = r0 + 1

        # Clamp
        c0 = np.clip(c0, 0, self.width - 1)
        c1 = np.clip(c1, 0, self.width - 1)
        r0 = np.clip(r0, 0, self.height - 1)
        r1 = np.clip(r1, 0, self.height - 1)

        fc = cols - np.floor(cols)
        fr = rows - np.floor(rows)

        v00 = self.data[r0, c0]
        v01 = self.data[r0, c1]
        v10 = self.data[r1, c0]
        v11 = self.data[r1, c1]

        top = v00 + (v01 - v00) * fc
        bot = v10 + (v11 - v10) * fc
        return top + (bot - top) * fr


def build_ray_directions(yaw_deg, pitch_deg, h_fov_deg, v_fov_deg, grid_h, grid_v):
    """Generate ray direction vectors in local ENU (East, North, Up) coords.

    yaw: compass heading (0=N, 90=E)
    pitch: gimbal pitch (0=horizontal, -90=nadir)
    """
    yaw_rad = math.radians(yaw_deg)
    pitch_rad = math.radians(pitch_deg)

    # Camera look direction in ENU
    look = np.array([
        math.sin(yaw_rad) * math.cos(pitch_rad),   # east
        math.cos(yaw_rad) * math.cos(pitch_rad),   # north
        math.sin(pitch_rad)                          # up
    ])

    # Camera right vector (perpendicular to look, in horizontal plane)
    right = np.array([math.cos(yaw_rad), -math.sin(yaw_rad), 0.0])

    # Camera up vector (perpendicular to look and right)
    up = np.cross(right, look)
    up_norm = np.linalg.norm(up)
    if up_norm > 1e-10:
        up = up / up_norm

    # Generate ray offsets on image plane
    h_half = math.tan(math.radians(h_fov_deg / 2))
    v_half = math.tan(math.radians(v_fov_deg / 2))

    # Grid of normalised image coords [-1, 1]
    us = np.linspace(-1, 1, grid_h)
    vs = np.linspace(-1, 1, grid_v)
    uu, vv = np.meshgrid(us, vs)
    uu = uu.ravel()
    vv = vv.ravel()

    # Ray directions: look + u*h_half*right + v*v_half*up
    dirs = (look[np.newaxis, :] +
            uu[:, np.newaxis] * h_half * right[np.newaxis, :] +
            vv[:, np.newaxis] * v_half * up[np.newaxis, :])

    # Normalise
    norms = np.linalg.norm(dirs, axis=1, keepdims=True)
    dirs = dirs / norms

    # Filter out rays not pointing meaningfully downward.
    # 0.01 (0.57°) was too loose — near-horizontal rays graze distant terrain.
    # -0.05 (~3°) ensures only rays with a real downward component survive.
    mask = dirs[:, 2] < -0.05
    return dirs[mask]


def march_rays_vectorised(cam_pos_enu, ray_dirs, dem_lookup, origin_lat, origin_lon,
                          step_m, max_range_m, score_exp):
    """March all rays simultaneously and return (hit_lats, hit_lons, scores).

    cam_pos_enu: [east, north, up] in metres from origin
    ray_dirs: (N, 3) normalised direction vectors in ENU
    """
    n_rays = ray_dirs.shape[0]
    if n_rays == 0:
        return np.array([]), np.array([]), np.array([])

    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(origin_lat))

    n_steps = int(max_range_m / step_m)

    # Use coarse-to-fine: first step at 10m, then refine
    coarse_step = max(step_m * 5, 10.0)
    n_coarse = int(max_range_m / coarse_step)

    # Coarse pass
    steps = np.arange(1, n_coarse + 1) * coarse_step  # (n_coarse,)
    # positions: (n_coarse, n_rays, 3)
    positions = cam_pos_enu[np.newaxis, np.newaxis, :] + steps[:, np.newaxis, np.newaxis] * ray_dirs[np.newaxis, :, :]

    # Convert to lat/lon
    lons = origin_lon + positions[:, :, 0] / m_per_deg_lon
    lats = origin_lat + positions[:, :, 1] / m_per_deg_lat
    alts = positions[:, :, 2]

    # Flatten for batch lookup
    flat_lats = lats.ravel()
    flat_lons = lons.ravel()
    dem_elevs = dem_lookup.get_elevation_batch(flat_lats, flat_lons).reshape(lats.shape)

    # Find first intersection: ray alt <= dem elevation
    below = alts <= dem_elevs  # (n_coarse, n_rays)

    # For each ray, find the first step where it goes below
    # Use argmax on the boolean — gives first True index (0 if never True)
    first_below = np.argmax(below, axis=0)  # (n_rays,)
    ever_below = np.any(below, axis=0)       # (n_rays,)

    # Refine hits with fine steps
    hit_lats = []
    hit_lons = []
    hit_dists = []

    # Process rays that had coarse hits
    hit_ray_indices = np.where(ever_below)[0]
    if len(hit_ray_indices) == 0:
        return np.array([]), np.array([]), np.array([])

    # For each hitting ray, refine between (coarse_step_before, coarse_step_at)
    coarse_indices = first_below[hit_ray_indices]
    start_dists = np.where(coarse_indices > 0, steps[coarse_indices - 1], 0.0)
    end_dists = steps[coarse_indices]

    # Fine march for all hitting rays simultaneously
    n_fine = int(coarse_step / step_m) + 2
    fine_fracs = np.linspace(0, 1, n_fine)

    # (n_fine, n_hit_rays)
    fine_dists = start_dists[np.newaxis, :] + fine_fracs[:, np.newaxis] * (end_dists - start_dists)[np.newaxis, :]

    hit_dirs = ray_dirs[hit_ray_indices]  # (n_hit, 3)

    fine_pos = cam_pos_enu[np.newaxis, np.newaxis, :] + fine_dists[:, :, np.newaxis] * hit_dirs[np.newaxis, :, :]

    fine_lons = origin_lon + fine_pos[:, :, 0] / m_per_deg_lon
    fine_lats = origin_lat + fine_pos[:, :, 1] / m_per_deg_lat
    fine_alts = fine_pos[:, :, 2]

    fine_dem = dem_lookup.get_elevation_batch(fine_lats.ravel(), fine_lons.ravel()).reshape(fine_lats.shape)

    fine_below = fine_alts <= fine_dem
    fine_first = np.argmax(fine_below, axis=0)
    fine_ever = np.any(fine_below, axis=0)

    # Gather results
    valid = fine_ever
    valid_indices = np.where(valid)[0]

    if len(valid_indices) == 0:
        return np.array([]), np.array([]), np.array([])

    step_idx = fine_first[valid_indices]
    result_lats = fine_lats[step_idx, valid_indices]
    result_lons = fine_lons[step_idx, valid_indices]
    result_dists = fine_dists[step_idx, valid_indices]

    # Score = 1 / d^exp
    result_dists = np.maximum(result_dists, 1.0)  # avoid division by zero
    scores = 1.0 / np.power(result_dists, score_exp)

    return result_lats, result_lons, scores


def main():
    parser = argparse.ArgumentParser(description="Compute visibility heatmap from flight telemetry + DEM")
    parser.add_argument("--telemetry", required=True, help="Path to flight-telemetry.json")
    parser.add_argument("--dem", required=True, help="Path to DEM GeoTIFF, e.g. dem/areas/<area>/area-dem.tif")
    parser.add_argument("--output-dir", required=True,
                        help="Output directory, e.g. dem/areas/<area> (files go in its visibility/ subfolder)")
    parser.add_argument("--sample-rate", type=float, default=TEMPORAL_SAMPLE_RATE)
    parser.add_argument("--ray-grid-h", type=int, default=RAY_GRID_H)
    parser.add_argument("--ray-grid-v", type=int, default=RAY_GRID_V)
    parser.add_argument("--ray-step", type=float, default=RAY_STEP_M)
    parser.add_argument("--max-range", type=float, default=MAX_RANGE_M)
    parser.add_argument("--score-exponent", type=float, default=SCORE_EXPONENT)
    parser.add_argument("--output-resolution", type=float, default=OUTPUT_RESOLUTION_M)
    parser.add_argument("--sensor-width", type=float, default=SENSOR_WIDTH_MM)
    parser.add_argument("--sensor-height", type=float, default=SENSOR_HEIGHT_MM)
    parser.add_argument("--frame-aspect", type=parse_aspect, default=FRAME_ASPECT,
                        help="Video frame aspect ratio, e.g. 16:9 or 3840x2160 (default 16:9)")
    parser.add_argument("--pitch-offset", type=float, default=0.0,
                        help="Degrees added to gimbal pitch; negative tilts down (viewer camera calibration)")
    parser.add_argument("--yaw-offset", type=float, default=0.0,
                        help="Degrees added to heading (viewer camera calibration)")
    parser.add_argument("--alt-datum", choices=["takeoff", "absolute"], default="takeoff",
                        help="'takeoff' (default) shifts abs_alt so the takeoff point sits on the DEM, "
                             "correcting DJI's barometric datum; 'absolute' uses abs_alt as logged")
    parser.add_argument("--fov-scale", type=float, default=1.0,
                        help="Scale on the image-plane half-width/height tangents; < 1 narrows the view "
                             "(viewer camera calibration)")
    parser.add_argument(
        "--name",
        help="Output basename. Defaults to the telemetry filename minus a trailing '-telemetry' suffix. "
             "Output files go to <output-dir>/visibility/<name>.{bin,json}.",
    )
    args = parser.parse_args()

    # Derive output basename
    if args.name:
        out_name = args.name
    else:
        stem = Path(args.telemetry).stem  # e.g. "2026-03-30-foo-telemetry"
        out_name = stem[: -len("-telemetry")] if stem.endswith("-telemetry") else stem
    print(f"Output basename: {out_name}")

    # Load telemetry
    print(f"Loading telemetry from {args.telemetry}...")
    if args.telemetry.lower().endswith(".mp4"):
        print(
            "Error: --telemetry expects a JSON file, not an MP4. "
            "Use the viewer's 'Export Telemetry' button to produce "
            "<basename>-telemetry.json and pass that here.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(args.telemetry) as f:
        telem_all = json.load(f)
    print(f"  {len(telem_all)} total frames")

    # Sample at configured rate
    sample_interval = 1.0 / args.sample_rate
    sampled = []
    last_t = -999
    for e in telem_all:
        if e["t"] - last_t >= sample_interval:
            sampled.append(e)
            last_t = e["t"]
    print(f"  {len(sampled)} frames after {args.sample_rate} Hz sampling")

    # Compute FOV from first frame's focal length
    first_fl = float(sampled[0].get("focalLen", "24.00"))
    h_fov, v_fov = compute_fov(args.sensor_width, args.sensor_height, first_fl, args.frame_aspect)
    h_fov = math.degrees(2 * math.atan(math.tan(math.radians(h_fov / 2)) * args.fov_scale))
    v_fov = math.degrees(2 * math.atan(math.tan(math.radians(v_fov / 2)) * args.fov_scale))
    print(f"  FOV: {h_fov:.1f}° x {v_fov:.1f}° (from {first_fl}mm equiv, frame aspect {args.frame_aspect:.3f})")
    print(f"  Calibration: pitch {args.pitch_offset:+.1f}°, heading {args.yaw_offset:+.1f}°, FOV scale {args.fov_scale:.3f}")

    # Load DEM
    print(f"Loading DEM from {args.dem}...")
    dem_data, dem_transform, dem_bounds = load_dem_rasterio(args.dem)
    dem = DEMLookup(dem_data, dem_transform, dem_bounds)
    print(f"  Grid: {dem.width} x {dem.height}")

    # At takeoff the aircraft is on the ground (rel_alt 0), so its abs_alt should equal the
    # DEM there. DJI's barometric datum is often several metres out, which scales every ray
    # by the same fraction, so shift abs_alt to put takeoff on the terrain.
    alt_offset = 0.0
    if args.alt_datum == "takeoff":
        on_ground = next((e for e in telem_all
                          if abs(e.get("relAlt", 9e9)) < 0.5 and abs(e.get("lat", 0)) > 1e-6), None)
        if on_ground is None:
            print("  Altitude datum: no on-ground frame found, using abs_alt as logged")
        else:
            dem_z = float(dem.get_elevation_batch(np.array([on_ground["lat"]]),
                                                  np.array([on_ground["lon"]]))[0])
            offset = dem_z - (on_ground["absAlt"] - on_ground["relAlt"])
            if abs(offset) > 50:
                print(f"  Altitude datum: implausible offset {offset:+.1f} m, using abs_alt as logged")
            else:
                alt_offset = offset
                print(f"  Altitude datum: shifting abs_alt by {alt_offset:+.1f} m to put takeoff on the DEM")

    # Compute flight bounding box for output grid
    lats = [e["lat"] for e in telem_all]
    lons = [e["lon"] for e in telem_all]
    centre_lat = (min(lats) + max(lats)) / 2
    centre_lon = (min(lons) + max(lons)) / 2

    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(centre_lat))

    # Output grid bounds: flight bbox + max_range buffer
    buffer_deg_lat = args.max_range / m_per_deg_lat
    buffer_deg_lon = args.max_range / m_per_deg_lon
    out_south = min(lats) - buffer_deg_lat
    out_north = max(lats) + buffer_deg_lat
    out_west = min(lons) - buffer_deg_lon
    out_east = max(lons) + buffer_deg_lon

    # Clamp to DEM bounds
    out_south = max(out_south, dem_bounds.bottom)
    out_north = min(out_north, dem_bounds.top)
    out_west = max(out_west, dem_bounds.left)
    out_east = min(out_east, dem_bounds.right)

    # Output grid dimensions
    out_pixel_lat = args.output_resolution / m_per_deg_lat
    out_pixel_lon = args.output_resolution / m_per_deg_lon
    out_height = int(math.ceil((out_north - out_south) / out_pixel_lat))
    out_width = int(math.ceil((out_east - out_west) / out_pixel_lon))
    print(f"Output grid: {out_width} x {out_height} ({out_width * out_height:,} cells) at {args.output_resolution}m")

    # Accumulator grid
    accum = np.zeros((out_height, out_width), dtype=np.float64)

    # Process each sampled frame
    t_start = time.time()
    for i, frame in enumerate(sampled):
        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(sampled) - i - 1) / rate if rate > 0 else 0
            print(f"  Frame {i+1}/{len(sampled)} ({rate:.1f} frames/s, ETA {eta:.0f}s)")

        cam_lat = frame["lat"]
        cam_lon = frame["lon"]
        cam_alt = frame["absAlt"] + alt_offset
        yaw = frame["yaw"] + args.yaw_offset
        pitch = frame["pitch"] + args.pitch_offset

        # Camera position in local ENU (metres from centre)
        cam_e = (cam_lon - centre_lon) * m_per_deg_lon
        cam_n = (cam_lat - centre_lat) * m_per_deg_lat
        cam_u = cam_alt  # on the DEM's vertical datum (see alt_offset above)
        cam_pos = np.array([cam_e, cam_n, cam_u])

        # Build rays
        ray_dirs = build_ray_directions(yaw, pitch, h_fov, v_fov, args.ray_grid_h, args.ray_grid_v)

        # March rays
        hit_lats, hit_lons, scores = march_rays_vectorised(
            cam_pos, ray_dirs, dem, centre_lat, centre_lon,
            args.ray_step, args.max_range, args.score_exponent
        )

        if len(hit_lats) == 0:
            continue

        # Map hits to output grid
        hit_rows = ((out_north - hit_lats) / out_pixel_lat).astype(np.int32)
        hit_cols = ((hit_lons - out_west) / out_pixel_lon).astype(np.int32)

        # Filter in-bounds
        valid = (hit_rows >= 0) & (hit_rows < out_height) & (hit_cols >= 0) & (hit_cols < out_width)
        hit_rows = hit_rows[valid]
        hit_cols = hit_cols[valid]
        scores = scores[valid]

        # Accumulate (use np.add.at for unbuffered accumulation)
        np.add.at(accum, (hit_rows, hit_cols), scores)

    elapsed = time.time() - t_start
    print(f"Processed {len(sampled)} frames in {elapsed:.1f}s")

    max_score = float(np.max(accum))
    nonzero_cells = int(np.count_nonzero(accum))
    print(f"Max score: {max_score:.6f}, non-zero cells: {nonzero_cells:,}")

    # Write output
    out_dir = Path(args.output_dir) / "visibility"
    out_dir.mkdir(parents=True, exist_ok=True)

    bin_path = out_dir / f"{out_name}.bin"
    accum_f32 = accum.astype(np.float32)
    accum_f32.tofile(str(bin_path))
    print(f"Written: {bin_path} ({bin_path.stat().st_size / 1024 / 1024:.1f} MB)")

    meta = {
        "bounds": {
            "north": round(out_north, 8),
            "south": round(out_south, 8),
            "east": round(out_east, 8),
            "west": round(out_west, 8),
        },
        "width": out_width,
        "height": out_height,
        "resolution_m": args.output_resolution,
        "pixel_size_lon": out_pixel_lon,
        "pixel_size_lat": out_pixel_lat,
        "max_score": max_score,
        "total_frames_processed": len(sampled),
        "config": {
            "temporal_sample_rate": args.sample_rate,
            "ray_grid": [args.ray_grid_h, args.ray_grid_v],
            "ray_step_m": args.ray_step,
            "max_range_m": args.max_range,
            "score_exponent": args.score_exponent,
            "fov_deg": [round(h_fov, 2), round(v_fov, 2)],
            "frame_aspect": round(args.frame_aspect, 4),
            "calibration": {
                "pitch_offset_deg": args.pitch_offset,
                "yaw_offset_deg": args.yaw_offset,
                "fov_scale": args.fov_scale,
            },
            "alt_datum": args.alt_datum,
            "alt_datum_offset_m": round(alt_offset, 2),
            "model_version": MODEL_VERSION,
        },
    }
    json_path = out_dir / f"{out_name}.json"
    with open(json_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Written: {json_path}")


if __name__ == "__main__":
    main()
