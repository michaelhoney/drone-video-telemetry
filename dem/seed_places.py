#!/usr/bin/env python3
"""Seed an area's places layer from the missions pilots named in the drone archive.

A place is a named area of ground — a dam, a cliff line, a monitoring site — that
link_videos.py uses to give videos descriptive names. Pilots already name missions on
the controller (DJI_202510271228_006_Quoin_cliffs), so every named flight folder in the
media archive becomes a seed polygon for its place:

  - photo missions: the hull of where the photos were aimed, from the laser rangefinder
    target each DJI photo records (LRFTargetLat/Lon), so these are usually close already;
  - video-only missions: the ground the camera saw from within 50 m, from the flight's
    visibility heatmap — a rough area around the subject rather than its outline.

Seeds are marked status "seed" and are meant to be checked and adjusted in places.html
(served from the project root), which saves back to dem/areas/<area>/places.geojson.

Re-running never overwrites that file's places: it adds only seeds whose name and source
folders aren't already in it, so places you renamed or redrew stay as they are.
--replace starts again from seeds.

Mission names map to place names through dem/areas/<area>/place-names.json (optional):

    {
      "places": [["honeysuckle-A-nadir|Honeysuckle-Afocus", "Honeysuckle A"],
                 ["nw-dam-oblique", "Stockers high dam"]],
      "ignore": ["^H2$", "demo"]
    }

Each "places" entry is a regular expression matched against the folder's mission name;
unmatched mission names become places of their own. Generic route names DJI Pilot
generates (Create-Area-Route1, NewSmart3DCaptureRoute1) and test flights are always
ignored.

Usage:
    python3 dem/seed_places.py quoin --media "/path/to/Dropbox/.../drone"
    python3 dem/seed_places.py quoin --media DIR --paddocks DIR/quoin_paddocks.gpkg

Also writes, for places.html:
    dem/areas/<area>/tracks.geojson     flight tracks of the archive's videos
    dem/areas/<area>/paddocks.geojson   from --paddocks (any vector format), if given
"""

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

from area_dem import area_dir, metric_transformers, rel
from export_coverage import coverage_geometry
from media_sources import (ensure_source, find_source_videos, folder_description, load_polygons,
                           read_heatmap, write_geojson, write_tracks)
from process_flights import extract_telemetry, load_calibration

ALWAYS_IGNORE = [r"Create-Area-Route", r"NewSmart3DCaptureRoute", r"test", r"^demo"]
PADDOCK_NAME_FIELDS = ("name", "Name", "NAME", "Pdk_namesh", "PDK_NAME")

MIN_PHOTOS = 5            # fewer rangefinder targets than this can't outline a place
PHOTO_BUFFER_M = 15       # around the hull of rangefinder targets
VIDEO_WITHIN_M = 50       # video seeds: ground seen from this close
SIMPLIFY_M = 12           # few enough vertices to drag by hand

XMP_RE = re.compile(rb'drone-dji:(LRFTargetLat|LRFTargetLon|LRFStatus)="([^"]*)"')


def load_names(path):
    config = json.loads(Path(path).read_text()) if path and Path(path).exists() else {}
    places = [(re.compile(pattern, re.I), name) for pattern, name in config.get("places", [])]
    ignore = [re.compile(p, re.I) for p in ALWAYS_IGNORE + config.get("ignore", [])]
    return places, ignore


def place_name(description, places, ignore):
    if not description or any(p.search(description) for p in ignore):
        return None
    mapped = next((name for pattern, name in places if pattern.search(description)), None)
    if mapped:
        return mapped
    words = re.sub(r"[_\-]+", " ", description).strip()
    return words[:1].upper() + words[1:]


def rangefinder_targets(folder):
    """(lon, lat) the camera was aimed at for each photo with a valid laser rangefinder reading."""
    targets = []
    for photo in folder.iterdir():
        if photo.suffix.upper() != ".JPG" or photo.name.startswith("._"):
            continue
        with open(photo, "rb") as f:
            tags = {k.decode(): v.decode() for k, v in XMP_RE.findall(f.read(120_000))}
        if tags.get("LRFStatus") == "Normal" and "LRFTargetLat" in tags:
            targets.append((float(tags["LRFTargetLon"]), float(tags["LRFTargetLat"])))
    return targets


def collect_missions(media, places, ignore):
    """{place name: {"folders": [...], "targets": [(lon, lat)], "videos": [Path]}}"""
    groups = {}
    for folder in sorted(p for p in media.glob("*/DJI_*") if p.is_dir()):
        name = place_name(folder_description(folder.name), places, ignore)
        if not name:
            continue
        group = groups.setdefault(name, {"folders": [], "targets": [], "videos": []})
        group["folders"].append(f"{folder.parent.name}/{folder.name}")
        group["targets"] += rangefinder_targets(folder)
        group["videos"] += [p for p in folder.iterdir()
                            if p.suffix.lower() == ".mp4" and not p.name.startswith("._")]
    return groups


def photo_seed(targets, to_m):
    from shapely.geometry import MultiPoint
    pts = np.array([to_m(lon, lat) for lon, lat in targets])
    centre = np.median(pts, axis=0)
    dist = np.hypot(*(pts - centre).T)
    keep = pts[dist <= max(3 * np.median(dist), 30)]   # drop stray rangefinder hits
    return MultiPoint([tuple(p) for p in keep]).convex_hull.buffer(PHOTO_BUFFER_M)


def video_seed(area, videos, calibration, to_m, to_ll):
    from shapely.ops import transform, unary_union
    parts = []
    for video in videos:
        _, vis = ensure_source(area, video, calibration)
        if vis is None:
            continue
        scores, _, affine = read_heatmap(vis)
        geom, _ = coverage_geometry(scores, affine, 1 / VIDEO_WITHIN_M**2, 20, 0, to_m, to_ll)
        if geom is not None:
            parts.append(transform(to_m, max(geom.geoms, key=lambda p: p.area)))
    # Close small gaps so the seed reads as one area rather than a scatter of fragments.
    return unary_union(parts).buffer(25).buffer(-25) if parts else None


def seed_features(area, groups, calibration, to_m, to_ll):
    from shapely.geometry import mapping
    from shapely.ops import transform
    features = []
    for name, group in groups.items():
        if len(group["targets"]) >= MIN_PHOTOS:
            geom = photo_seed(group["targets"], to_m)
            source = f"rangefinder targets of {len(group['targets'])} photos"
        elif group["videos"]:
            geom = video_seed(area, group["videos"], calibration, to_m, to_ll)
            source = f"camera view within {VIDEO_WITHIN_M} m of {len(group['videos'])} video(s)"
        else:
            geom = None
        if geom is None or geom.is_empty:
            print(f"  {name}: no rangefinder photos or usable video — skipped")
            continue
        geom = geom.simplify(SIMPLIFY_M)
        features.append({"type": "Feature", "properties": {
            "name": name, "status": "seed", "source": source,
            "folders": group["folders"], "notes": ""},
            "geometry": mapping(transform(to_ll, geom))})
        print(f"  {name:24} {geom.area / 1e4:6.1f} ha  {source}")
    return features


def merge_with_existing(path, seeds):
    """Existing places plus seeds for names and folders they don't already cover."""
    existing = json.loads(path.read_text())["features"]
    names = {f["properties"].get("name", "").strip().lower() for f in existing}
    folders = {folder for f in existing for folder in f["properties"].get("folders") or []}
    added = []
    for seed in seeds:
        p = seed["properties"]
        if p["name"].lower() in names or folders.intersection(p["folders"]):
            continue
        added.append(seed)
    print(f"\n{len(existing)} existing place(s) kept; adding {len(added)} new seed(s)"
          + (": " + ", ".join(s["properties"]["name"] for s in added) if added else ""))
    return existing + added


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("area")
    parser.add_argument("--media", required=True, type=Path,
                        help="Drone archive folder holding dated flight folders (e.g. the Dropbox drone folder)")
    parser.add_argument("--names", type=Path,
                        help="Mission name -> place name mapping (default dem/areas/<area>/place-names.json)")
    parser.add_argument("--paddocks", type=Path,
                        help="Paddock polygons in any vector format, copied to paddocks.geojson for places.html")
    parser.add_argument("--replace", action="store_true",
                        help="Overwrite places.geojson with fresh seeds, discarding edits")
    args = parser.parse_args()

    d = area_dir(args.area)
    if not d.is_dir():
        sys.exit(f"No area folder {rel(d)}/")
    if not args.media.is_dir():
        sys.exit(f"Media folder not found: {args.media}")
    places, ignore = load_names(args.names or d / "place-names.json")
    calibration = load_calibration()

    videos = find_source_videos(args.media)
    print(f"{len(videos)} video(s) under {args.media}")
    if not videos:
        sys.exit("Nothing to seed from")

    # Telemetry first: tracks need it, and it fixes the metric projection for the area.
    flights = []
    for video in videos:
        telemetry = d / "sources" / "telemetry" / f"{video.stem}-telemetry.json"
        if telemetry.exists() or extract_telemetry(video, telemetry):
            flights.append((telemetry, {"video": video.stem, "path": str(video.relative_to(args.media))}))
    if not flights:
        sys.exit("No telemetry in any video")
    first = next(e for e in json.loads(flights[0][0].read_text()) if abs(e.get("lon", 0)) > 1e-6)
    to_m, to_ll = metric_transformers(first["lon"])

    print("\nSeeds from named missions:")
    seeds = seed_features(args.area, collect_missions(args.media, places, ignore), calibration, to_m, to_ll)

    out = d / "places.geojson"
    features = seeds if args.replace or not out.exists() else merge_with_existing(out, seeds)
    print()
    write_geojson(out, features)
    write_tracks(args.area, flights)

    if args.paddocks:
        from shapely import force_2d
        from shapely.geometry import mapping
        paddocks = load_polygons(args.paddocks, PADDOCK_NAME_FIELDS)
        write_geojson(d / "paddocks.geojson", [
            {"type": "Feature", "properties": {"name": name},
             "geometry": mapping(force_2d(geom).simplify(0.00002))}   # ~2 m; plenty for a context layer
            for name, geom, _ in paddocks], ndigits=6)

    print(f"\nCheck and adjust the seeds in places.html?area={args.area} "
          f"(serve the project root, e.g. python3 -m http.server 5002)")


if __name__ == "__main__":
    main()
