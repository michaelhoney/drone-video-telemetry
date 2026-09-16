#!/usr/bin/env python3
"""Export per-flight camera coverage as a GIS layer for QGIS.

Turns the per-flight visibility heatmaps (dem/areas/<area>/visibility/<basename>.bin,
written by process_flights.py) into a vector layer: each video becomes a nested stack of
polygons, one per viewing-distance level, carrying the flight's date, time and stats as
attributes. Colleagues can then see where we have drone vision, and from when.

Each level is the ground a flight saw from that distance or closer. Heatmap scores
accumulate 1/d² per sampled second, so a cell seen for S seconds from d metres scores
S/d² — which makes a level readable as "seen for at least --min-seconds from within
<level> metres". Shorter distances mean better ground detail, so the level polygons
nest: the closest is smallest and sits inside all the others. Draw them as one
semi-transparent fill and the stack shades itself, darkest over the ground the flight
saw best.

Usage:
    python3 dem/export_coverage.py --area marathon
    python3 dem/export_coverage.py                              # every area with heatmaps
    python3 dem/export_coverage.py --area marathon --levels 300 # one feature per video
    python3 dem/export_coverage.py --area marathon --output ~/Desktop/marathon.gpkg

Output (EPSG:4326) defaults to dem/exports/<area>-coverage.geojson. Any extension
OGR knows works — .gpkg is the friendlier QGIS format; .geojson is written directly
with tidied coordinate precision.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from area_dem import AREAS_DIR, DEM_DIR, area_dir, area_mp4s, rel, round_coords, telemetry_path

EXPORTS_DIR = DEM_DIR / "exports"

# Viewing-distance levels in metres, closest first — halving each step, so each level
# is a 4x jump in the score threshold and the bands stay visually even. 25 m is as close
# as the camera gets at typical flight height; 400 m is distant, oblique context. Nested.
LEVELS = [25.0, 50.0, 100.0, 200.0, 400.0]
MIN_SECONDS = 1.0
MIN_PATCH_M2 = 2000.0   # drop coverage specks and pinholes smaller than this
SIMPLIFY_M = 5.0        # polygon simplification tolerance

TS_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def parse_levels(value):
    """'100,200,300,500' -> [100.0, 200.0, 300.0, 500.0], deduplicated, closest first."""
    try:
        levels = sorted({float(v) for v in value.split(",") if v.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a comma-separated list of distances: {value!r}")
    if not levels:
        raise argparse.ArgumentTypeError("give at least one distance")
    if levels[0] <= 0:
        raise argparse.ArgumentTypeError("distances must be positive")
    return levels


def parse_timestamp(value):
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def flight_times(telemetry):
    """(start, end) datetimes from the first and last frame carrying a timestamp."""
    start = next((parse_timestamp(e.get("timestamp")) for e in telemetry
                  if parse_timestamp(e.get("timestamp"))), None)
    end = next((parse_timestamp(e.get("timestamp")) for e in reversed(telemetry)
                if parse_timestamp(e.get("timestamp"))), None)
    return start, end


def coverage_geometry(scores, affine, threshold, min_patch_cells, simplify_m, to_m, to_ll):
    """Polygonise cells at or above threshold. Returns (geometry in EPSG:4326, area in ha)."""
    from rasterio import features
    from shapely.geometry import MultiPolygon, shape
    from shapely.ops import transform as shapely_transform, unary_union

    mask = (scores >= threshold).astype(np.uint8)
    if not mask.any():
        return None, 0.0
    if min_patch_cells > 1:
        # Sieving a 0/1 raster clears both isolated specks and pinholes in solid cover.
        # It preserves the nesting between levels: a patch or hole too small to survive
        # at one level is no larger at any closer one.
        mask = features.sieve(mask, min_patch_cells)
        if not mask.any():
            return None, 0.0

    polys = [shape(geom) for geom, value
             in features.shapes(mask, mask=mask.astype(bool), transform=affine)
             if value == 1]
    if not polys:
        return None, 0.0

    # Simplify and measure in an MGA zone, where tolerances and areas are both in metres.
    metric = unary_union([shapely_transform(to_m, p) for p in polys])
    if simplify_m > 0:
        metric = metric.simplify(simplify_m).buffer(0)
    if metric.is_empty:
        return None, 0.0
    geom = shapely_transform(to_ll, metric)
    # Always MultiPolygon: a layer of mixed geometry types upsets GPKG and shapefile writers.
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    return geom, metric.area / 10_000


def flight_features(area, name, vis_json, telemetry_json, mp4_path, args):
    """One feature per level for a flight, outermost level first. Empty if nothing qualifies."""
    from rasterio.transform import Affine
    from shapely.geometry import mapping

    from area_dem import metric_transformers

    with open(vis_json) as f:
        meta = json.load(f)
    scores = np.fromfile(str(vis_json.with_suffix(".bin")), dtype=np.float32)
    if scores.size != meta["width"] * meta["height"]:
        print(f"  {name}: heatmap .bin is {scores.size} cells, metadata says "
              f"{meta['width'] * meta['height']} — skipping", file=sys.stderr)
        return []
    scores = scores.reshape(meta["height"], meta["width"])

    max_range = meta.get("config", {}).get("max_range_m")
    if max_range and args.levels[-1] > max_range:
        print(f"  {name}: heatmap only cast rays out to {max_range:g} m, so levels beyond "
              f"that add nothing", file=sys.stderr)

    with open(telemetry_json) as f:
        telemetry = json.load(f)
    start, end = flight_times(telemetry)
    rel_alts = [e["relAlt"] for e in telemetry if isinstance(e.get("relAlt"), (int, float))]
    duration_s = telemetry[-1]["t"] - telemetry[0]["t"] if telemetry else 0.0
    if start and not name.startswith(start.strftime("%Y-%m-%d")):
        print(f"  {name}: filename date differs from the flight's recorded date "
              f"({start.date()}) — using the recorded one")

    flight_props = {
        "area": area,
        "video": name,
        "date": start.strftime("%Y-%m-%d") if start else None,
        "year": start.year if start else None,
        "flight_start": start.isoformat(timespec="seconds") if start else None,
        "flight_end": end.isoformat(timespec="seconds") if end else None,
        "duration_min": round(duration_s / 60, 1),
        "alt_agl_max_m": round(max(rel_alts), 1) if rel_alts else None,
        "alt_agl_mean_m": round(sum(rel_alts) / len(rel_alts), 1) if rel_alts else None,
        "seconds_sampled": meta.get("total_frames_processed"),
        "min_seconds": args.min_seconds,
        "cell_size_m": meta["resolution_m"],
        "mp4": str(rel(mp4_path)) if mp4_path else None,
    }

    b = meta["bounds"]
    affine = Affine(meta["pixel_size_lon"], 0.0, b["west"],
                    0.0, -meta["pixel_size_lat"], b["north"])
    to_m, to_ll = metric_transformers((b["west"] + b["east"]) / 2)
    min_patch_cells = max(1, round(args.min_patch_m2 / meta["resolution_m"] ** 2))

    out = []
    areas_ha = []
    # Widest level first, so the smaller, closer-range levels draw on top of it.
    for rank, within_m in reversed(list(enumerate(args.levels, start=1))):
        threshold = args.min_seconds / within_m**2
        geom, hectares = coverage_geometry(scores, affine, threshold, min_patch_cells,
                                           args.simplify_m, to_m, to_ll)
        if geom is None or geom.is_empty:
            continue
        props = dict(flight_props)
        props.update({
            "level": rank,
            "levels": len(args.levels),
            "within_m": within_m,
            "score_threshold": float(f"{threshold:.3g}"),
            "coverage_ha": round(hectares, 1),
        })
        # Keep the geometry last and the identifying fields first in the attribute table.
        out.append({"type": "Feature", "properties": props, "geometry": mapping(geom)})
        areas_ha.append(f"{within_m:g}m {hectares:,.0f}ha")

    if not out:
        print(f"  {name}: nothing above the closest level's threshold — skipping")
        return []
    print(f"  {name}: " + ", ".join(reversed(areas_ha))
          + (f"  ({props['date']})" if start else "  (no timestamps in telemetry)"))
    return out


def areas_with_heatmaps():
    return sorted(d.name for d in AREAS_DIR.iterdir()
                  if d.is_dir() and any((d / "visibility").glob("*.json")))


def collect_features(areas, args):
    features_out = []
    for area in areas:
        vis_jsons = sorted((area_dir(area) / "visibility").glob("*.json"))
        print(f"[{area}] {len(vis_jsons)} flight heatmap(s)")
        mp4s = {p.stem: p for p in area_mp4s(area)}
        for vis_json in vis_jsons:
            name = vis_json.stem
            telemetry_json = telemetry_path(area, name)
            if not telemetry_json.exists():
                print(f"  {name}: no {rel(telemetry_json)} — skipping", file=sys.stderr)
                continue
            features_out += flight_features(area, name, vis_json, telemetry_json,
                                            mp4s.get(name), args)
    # Newest flight first so the QGIS attribute table opens on the most recent work,
    # and within a flight widest level first so the closer ones draw over it.
    features_out.sort(key=lambda f: (f["properties"]["flight_start"] or "",
                                     f["properties"]["video"],
                                     f["properties"]["within_m"]),
                      reverse=True)
    return features_out


def write_layer(features_out, out_path):
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features_out,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".geojson", ".json"):
        for feature in collection["features"]:
            feature["geometry"]["coordinates"] = round_coords(feature["geometry"]["coordinates"])
        with open(out_path, "w") as f:
            json.dump(collection, f, indent=1)
    else:
        import geopandas as gpd
        gdf = gpd.GeoDataFrame.from_features(collection["features"], crs="EPSG:4326")
        gdf.to_file(out_path, layer=out_path.stem)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--area", help="Only export this area (default: every area with heatmaps)")
    parser.add_argument("--output", type=Path,
                        help=f"Output path; extension picks the format "
                             f"(default: {rel(EXPORTS_DIR)}/<area>-coverage.geojson)")
    parser.add_argument("--levels", type=parse_levels, default=LEVELS,
                        help="Comma-separated viewing distances in metres; each video gets one "
                             "nested polygon per level (default "
                             f"{','.join(f'{v:g}' for v in LEVELS)}). A single value gives one "
                             "feature per video")
    parser.add_argument("--min-seconds", type=float, default=MIN_SECONDS,
                        help=f"Seconds of video a cell needs at a level's range to count "
                             f"(default {MIN_SECONDS:g})")
    parser.add_argument("--min-patch-m2", type=float, default=MIN_PATCH_M2,
                        help=f"Drop coverage specks and pinholes below this size (default {MIN_PATCH_M2:g} m²)")
    parser.add_argument("--simplify-m", type=float, default=SIMPLIFY_M,
                        help=f"Polygon simplification tolerance, 0 to keep cell edges (default {SIMPLIFY_M:g} m)")
    args = parser.parse_args()

    if args.area and not area_dir(args.area).is_dir():
        sys.exit(f"No area folder {rel(area_dir(args.area))}/")
    areas = [args.area] if args.area else areas_with_heatmaps()
    if not areas:
        sys.exit("No areas have visibility heatmaps yet — run: python3 dem/process_flights.py")

    out_path = args.output or EXPORTS_DIR / f"{args.area or 'all-areas'}-coverage.geojson"
    print(f"{len(args.levels)} level(s): ground seen for {args.min_seconds:g}s or more from "
          f"within " + ", ".join(f"{v:g} m" for v in args.levels))

    features_out = collect_features(areas, args)
    if not features_out:
        sys.exit("No flights produced coverage — try a larger final --levels distance")

    write_layer(features_out, out_path)
    flights = len({f["properties"]["video"] for f in features_out})
    widest = max(args.levels)
    total_ha = sum(f["properties"]["coverage_ha"] for f in features_out
                   if f["properties"]["within_m"] == widest)
    print(f"\nWritten: {rel(out_path)} — {flights} flight(s), {len(features_out)} feature(s), "
          f"{total_ha:,.1f} ha at the {widest:g} m level (overlaps counted once per flight)")


if __name__ == "__main__":
    main()
