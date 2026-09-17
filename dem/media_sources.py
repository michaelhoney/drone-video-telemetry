#!/usr/bin/env python3
"""Source videos kept in a media archive outside the repo, and what we derive from them.

A media folder is a drone archive laid out the way DJI writes it, one dated folder per
field day (e.g. the Dropbox drone folder):

    <media>/2025-10-27-Quoin/DJI_202510271228_006_Quoin_cliffs/DJI_20251027122931_0001_V.MP4

Any text after the DJI_<YYYYMMDDHHMM>_<NNN>_ prefix of a flight folder is the name the
pilot gave the mission. Used by seed_places.py and link_videos.py.

Telemetry and heatmaps for each source video are cached under dem/areas/<area>/sources/,
keyed by the video's own filename, so places can be seeded and videos named before a
video has a name in the repo. link_videos.py copies them to the named telemetry/ and
visibility/ files that process_flights.py and the viewer use.
"""

import json
import re
import shutil
from pathlib import Path

import numpy as np

from area_dem import area_dir, rel, round_coords

DJI_FOLDER_RE = re.compile(r"^DJI_\d{12}_\d{3}(?:_(.*))?$")
DJI_VIDEO_RE = re.compile(r"^DJI_\d{14}_\d{4}_[A-Z]$")


def folder_description(folder_name):
    """'DJI_202510271228_006_Quoin_cliffs' -> 'Quoin_cliffs'; '' when the mission wasn't named."""
    match = DJI_FOLDER_RE.match(folder_name)
    return (match.group(1) or "") if match else ""


def find_source_videos(media_dir):
    """Every MP4 under media_dir, skipping macOS AppleDouble '._' files."""
    return sorted(p for p in Path(media_dir).rglob("*")
                  if p.suffix.lower() == ".mp4" and p.is_file() and not p.is_symlink()
                  and not p.name.startswith("._"))


def sources_dir(area):
    return area_dir(area) / "sources"


def source_telemetry_path(area, mp4):
    return sources_dir(area) / "telemetry" / f"{mp4.stem}-telemetry.json"


def source_visibility_path(area, mp4):
    return sources_dir(area) / "visibility" / f"{mp4.stem}.json"


def ensure_source(area, mp4, calibration):
    """Cached (telemetry, heatmap json) paths for a source video, extracting what's missing.

    Returns (telemetry, None) when the area has no DEM yet, or (None, None) when the video
    has no telemetry track.
    """
    from process_flights import extract_telemetry, run_compute_visibility, visibility_is_current

    telemetry = source_telemetry_path(area, mp4)
    if not telemetry.exists() and not extract_telemetry(mp4, telemetry):
        return None, None
    if not (area_dir(area) / "area-dem.tif").exists():
        return telemetry, None
    vis = source_visibility_path(area, mp4)
    if not (vis.exists() and vis.with_suffix(".bin").exists() and visibility_is_current(vis, calibration)):
        if not run_compute_visibility(telemetry, area, mp4, calibration,
                                      output_dir=sources_dir(area), quiet=True):
            return telemetry, None
    return telemetry, vis


def read_heatmap(vis_json):
    """(scores 2D array, metadata, affine transform) for a heatmap .json/.bin pair."""
    from rasterio.transform import Affine
    meta = json.loads(Path(vis_json).read_text())
    scores = np.fromfile(str(Path(vis_json).with_suffix(".bin")), dtype=np.float32)
    scores = scores.reshape(meta["height"], meta["width"])
    b = meta["bounds"]
    affine = Affine(meta["pixel_size_lon"], 0.0, b["west"], 0.0, -meta["pixel_size_lat"], b["north"])
    return scores, meta, affine


def heatmap_cells(vis_json, min_score=0.0):
    """(lons, lats, scores) of the heatmap cell centres scoring above min_score."""
    scores, meta, _ = read_heatmap(vis_json)
    rows, cols = np.nonzero(scores > min_score)
    b = meta["bounds"]
    lons = b["west"] + (cols + 0.5) * meta["pixel_size_lon"]
    lats = b["north"] - (rows + 0.5) * meta["pixel_size_lat"]
    return lons, lats, scores[rows, cols].astype(np.float64)


def load_telemetry(path):
    """Telemetry frames with a GPS fix."""
    return [e for e in json.loads(Path(path).read_text())
            if abs(e.get("lat", 0)) > 1e-6 and abs(e.get("lon", 0)) > 1e-6]


def write_geojson(path, features, ndigits=7):
    for feature in features:
        feature["geometry"]["coordinates"] = round_coords(feature["geometry"]["coordinates"], ndigits)
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=1))
    print(f"Written: {rel(path)} ({len(features)} features)")


def load_polygons(path, name_fields=("name",)):
    """[(name, shapely geometry in EPSG:4326, properties)] from a vector file, skipping unnamed features."""
    import geopandas as gpd
    gdf = gpd.read_file(path)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    out = []
    for _, row in gdf.iterrows():
        name = next((row[f] for f in name_fields if f in row and isinstance(row[f], str) and row[f].strip()), None)
        if name and row.geometry is not None and not row.geometry.is_empty:
            props = {k: v for k, v in row.items() if k != "geometry"}
            out.append((name.strip(), row.geometry, props))
    return out


def write_tracks(area, flights):
    """dem/areas/<area>/tracks.geojson for the places editor: one simplified line per flight.

    flights: [(telemetry path, {"video": ..., "path": ...})]
    """
    from shapely.geometry import LineString, mapping
    from shapely.ops import transform

    from area_dem import metric_transformers
    from export_coverage import flight_times

    features = []
    to_m = to_ll = None
    for telemetry, props in flights:
        frames = load_telemetry(telemetry)
        if len(frames) < 2:
            continue
        if to_m is None:
            to_m, to_ll = metric_transformers(frames[0]["lon"])
        start, _ = flight_times(frames)
        line = LineString([to_m(e["lon"], e["lat"]) for e in frames[::10]]).simplify(3)
        features.append({"type": "Feature", "properties": {
            **props,
            "start": start.isoformat(timespec="minutes") if start else None,
            "duration_min": round((frames[-1]["t"] - frames[0]["t"]) / 60, 1),
        }, "geometry": mapping(transform(to_ll, line))})
    features.sort(key=lambda f: f["properties"]["start"] or "")
    write_geojson(area_dir(area) / "tracks.geojson", features, ndigits=6)


def copy_if_missing(src, dst):
    """Copy src to dst unless dst exists. Returns True if copied."""
    if Path(dst).exists():
        return False
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True
