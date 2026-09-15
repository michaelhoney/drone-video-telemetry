#!/usr/bin/env python3
"""Clip a large DEM to a flight bounding box and export as raw Float32 + JSON metadata.

Usually run for you by `area_dem.py ingest` / `area_dem.py clip`.

Usage:
    # Clip around explicit bounds (south,west,north,east) with 1km buffer
    python3 dem/preprocess_dem.py --bounds="-42.15,147.61,-42.09,147.66" --buffer 1000 \
        --dem dem/areas/quoin/area-dem.tif --output-dir dem/areas/quoin

    # Clip around a telemetry JSON file
    python3 dem/preprocess_dem.py --telemetry dem/areas/quoin/telemetry/foo-telemetry.json --buffer 1000 \
        --dem dem/areas/quoin/area-dem.tif --output-dir dem/areas/quoin

Output:
    <output-dir>/flight-dem.bin   — raw Float32Array, row-major (NW corner first)
    <output-dir>/flight-dem.json  — metadata sidecar
"""

import argparse
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds


def parse_bounds(bounds_str):
    """Parse 'south,west,north,east' string."""
    parts = [float(x) for x in bounds_str.split(",")]
    if len(parts) != 4:
        raise ValueError("Bounds must be 'south,west,north,east'")
    south, west, north, east = parts
    return south, west, north, east


def bounds_from_telemetry(telemetry_path):
    """Compute bounding box from telemetry JSON array."""
    with open(telemetry_path) as f:
        data = json.load(f)
    lats = [e["lat"] for e in data]
    lons = [e["lon"] for e in data]
    return min(lats), min(lons), max(lats), max(lons)


def buffer_bounds(south, west, north, east, buffer_m):
    """Expand bounds by buffer_m metres in all directions."""
    lat_centre = (south + north) / 2
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_centre))
    dlat = buffer_m / m_per_deg_lat
    dlon = buffer_m / m_per_deg_lon
    return south - dlat, west - dlon, north + dlat, east + dlon


def main():
    parser = argparse.ArgumentParser(description="Clip DEM for browser use")
    parser.add_argument("--bounds", help="south,west,north,east")
    parser.add_argument("--telemetry", help="Path to flight-telemetry.json")
    parser.add_argument("--buffer", type=float, default=1000.0, help="Buffer in metres (default: 1000)")
    parser.add_argument("--dem", required=True, help="Path to source DEM GeoTIFF, e.g. dem/areas/<area>/area-dem.tif")
    parser.add_argument("--output-dir", required=True, help="Output directory, e.g. dem/areas/<area>")
    parser.add_argument(
        "--resolution",
        type=float,
        default=None,
        help="Target resolution in metres. If given, the clip is resampled "
             "(bilinear) to this cell size — use to shrink the output file. "
             "Default: keep source DEM resolution.",
    )
    args = parser.parse_args()

    if args.bounds:
        south, west, north, east = parse_bounds(args.bounds)
    elif args.telemetry:
        south, west, north, east = bounds_from_telemetry(args.telemetry)
    else:
        print("Error: provide --bounds or --telemetry", file=sys.stderr)
        sys.exit(1)

    print(f"Flight bounds: S={south:.6f} W={west:.6f} N={north:.6f} E={east:.6f}")

    south, west, north, east = buffer_bounds(south, west, north, east, args.buffer)
    print(f"Buffered bounds ({args.buffer}m): S={south:.6f} W={west:.6f} N={north:.6f} E={east:.6f}")

    with rasterio.open(args.dem) as src:
        # Clamp to DEM extent
        dem_bounds = src.bounds
        south = max(south, dem_bounds.bottom)
        west = max(west, dem_bounds.left)
        north = min(north, dem_bounds.top)
        east = min(east, dem_bounds.right)

        window = from_bounds(west, south, east, north, src.transform)
        # Round to integer pixel indices
        window = window.round_offsets().round_lengths()

        win_transform = src.window_transform(window)

        if args.resolution is not None:
            # Resample on read. Work out the target shape for this resolution.
            centre_lat = (south + north) / 2
            src_pixel_lon = src.transform.a
            src_pixel_lat = -src.transform.e
            src_res_m = ((src_pixel_lon * 111320.0 * math.cos(math.radians(centre_lat))) +
                         (src_pixel_lat * 111320.0)) / 2
            scale = src_res_m / args.resolution
            new_w = max(1, int(round(window.width * scale)))
            new_h = max(1, int(round(window.height * scale)))
            print(f"Resampling {int(window.width)} x {int(window.height)} @ ~{src_res_m:.1f}m "
                  f"-> {new_w} x {new_h} @ ~{args.resolution:.1f}m")
            data = src.read(
                1,
                window=window,
                out_shape=(new_h, new_w),
                resampling=Resampling.bilinear,
            )
            # Adjust transform for the new shape: scale pixel size by (old / new).
            sx = window.width / new_w
            sy = window.height / new_h
            win_transform = win_transform * rasterio.Affine.scale(sx, sy)
        else:
            data = src.read(1, window=window)

        print(f"Clipped grid: {data.shape[1]} x {data.shape[0]} ({data.shape[1] * data.shape[0]:,} cells)")

    # Replace NoData with minimum valid elevation
    nodata_mask = data < -1e30
    valid_data = data[~nodata_mask]
    if valid_data.size > 0:
        fill_val = float(np.min(valid_data))
    else:
        fill_val = 0.0
    data[nodata_mask] = fill_val
    nodata_count = int(np.sum(nodata_mask))
    if nodata_count:
        print(f"Replaced {nodata_count:,} NoData cells with {fill_val:.1f}")

    # Compute actual bounds from the window transform
    height, width = data.shape
    pixel_size_lon = win_transform.a
    pixel_size_lat = -win_transform.e  # positive value
    actual_west = win_transform.c
    actual_north = win_transform.f
    actual_east = actual_west + width * pixel_size_lon
    actual_south = actual_north - height * pixel_size_lat

    # Approximate resolution in metres
    centre_lat = (actual_south + actual_north) / 2
    res_m_lon = pixel_size_lon * 111320.0 * math.cos(math.radians(centre_lat))
    res_m_lat = pixel_size_lat * 111320.0
    resolution_m = round((res_m_lon + res_m_lat) / 2, 2)

    out_dir = Path(args.output_dir)

    # Write binary
    bin_path = out_dir / "flight-dem.bin"
    data_f32 = data.astype(np.float32)
    data_f32.tofile(str(bin_path))
    print(f"Written: {bin_path} ({bin_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Write JSON metadata
    meta = {
        "bounds": {
            "north": round(actual_north, 8),
            "south": round(actual_south, 8),
            "east": round(actual_east, 8),
            "west": round(actual_west, 8),
        },
        "width": width,
        "height": height,
        "resolution_m": resolution_m,
        "pixel_size_lon": pixel_size_lon,
        "pixel_size_lat": pixel_size_lat,
        "elevation_min": round(float(np.min(data)), 1),
        "elevation_max": round(float(np.max(data)), 1),
        "nodata_replaced_with": fill_val if nodata_count > 0 else None,
    }
    json_path = out_dir / "flight-dem.json"
    with open(json_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Written: {json_path}")
    print(f"Elevation range: {meta['elevation_min']}m – {meta['elevation_max']}m")


if __name__ == "__main__":
    main()
