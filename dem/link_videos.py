#!/usr/bin/env python3
"""Link an area's archive videos into mp4/ under descriptive names, without copying them.

Videos stay in the drone archive (e.g. Dropbox). Each gets a symlink in the area's mp4
folder, named for when it was flown and what it shows:

    mp4/matrice-4E-mp4/quoin/2025-10-27-1229-quoin-cliffs.MP4
        -> <archive>/2025-10-27-Quoin/DJI_202510271228_006_Quoin_cliffs/DJI_20251027122931_0001_V.MP4

The label after the date and time is, in order of preference:
  hand     the name a person already gave the file (a renamed archive file, or a local
           copy being replaced), minus its leading date;
  mission  the mission name the pilot gave the DJI flight folder, mapped to a place name
           through place-names.json the same way seed_places.py maps it (generic route
           names like Create-Area-Route1 don't count);
  place    the place(s) in dem/areas/<area>/places.geojson that hold at least
           --place-share of the flight's visibility score — where the camera looked
           closest and longest (see seed_places.py and places.html);
  paddock  the paddock(s) making up at least --paddock-share of the ground seen from
           within --within-m (dem/areas/<area>/paddocks.geojson).

The default run is a dry run that writes dem/areas/<area>/video-links.csv and prints the
plan. Edit the name column there to override any name, then --apply. Names in that file
are kept on later runs; --rename proposes fresh ones for every video.

--apply creates or renames the links, gives each linked video its telemetry and
heatmap under its new name (from the dem/areas/<area>/sources/ cache, or by renaming
the files a local copy already had), and moves local copies that are byte-identical to
their archive original to the Trash. Afterwards process_flights.py finds everything
current and export_coverage.py includes the new flights.

Links only resolve while the archive drive is mounted; process_flights.py skips
unresolvable links, and telemetry and heatmaps already made keep working without it.

Usage:
    python3 dem/link_videos.py quoin --media "/path/to/Dropbox/.../drone"
    python3 dem/link_videos.py quoin --media DIR --apply
"""

import argparse
import csv
import filecmp
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from area_dem import MP4_DIR, area_dir, area_mp4s, rel, telemetry_path
from export_coverage import flight_times
from media_sources import (DJI_VIDEO_RE, copy_if_missing, ensure_source, find_source_videos,
                           folder_description, heatmap_cells, load_polygons, load_telemetry,
                           write_tracks)
from process_flights import load_calibration
from seed_places import load_names, place_name

PLACE_SHARE = 0.20
PADDOCK_SHARE = 0.25
WITHIN_M = 200.0
MAX_LABEL_PARTS = 2
LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[-_ ]*")
CSV_FIELDS = ["name", "auto_name", "basis", "status", "start", "duration_min", "source"]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_link_dir(area):
    """The folder the area's MP4s already live in, or mp4/<area>."""
    parents = {p.parent for p in area_mp4s(area)}
    return parents.pop() if len(parents) == 1 else MP4_DIR / area


def shares_in(polygons, lons, lats, weights):
    """[(name, share of total weight)] for polygons holding any weight, largest first."""
    import shapely
    total = weights.sum()
    if total <= 0:
        return []
    out = []
    for name, geom in polygons:
        inside = shapely.contains_xy(geom, lons, lats)
        if inside.any():
            out.append((name, float(weights[inside].sum() / total)))
    return sorted(out, key=lambda kv: -kv[1])


def auto_label(video, vis_json, hand_name, places, paddocks, args):
    """(label, basis) for a video, following the preference order in the module docstring."""
    if hand_name:
        return LEADING_DATE_RE.sub("", hand_name), "hand"
    mission = place_name(folder_description(video.parent.name), *args.mission_names)
    if mission:
        return slug(mission), "mission"
    if vis_json:
        lons, lats, scores = heatmap_cells(vis_json)
        picked = [n for n, s in shares_in(places, lons, lats, scores) if s >= args.place_share]
        if picked:
            return "+".join(slug(n) for n in picked[:MAX_LABEL_PARTS]), "place"
        near = scores >= 1 / args.within_m**2
        paddock_shares = shares_in(paddocks, lons[near], lats[near], np.ones(int(near.sum())))
        picked = [n for n, s in paddock_shares if s >= args.paddock_share] or [n for n, _ in paddock_shares[:1]]
        if picked:
            return "+".join(slug(n) for n in picked[:MAX_LABEL_PARTS]), "paddock"
    return "flight", "none"


def existing_links(link_dir, media):
    """{source path: link path} for symlinks in link_dir pointing into the archive."""
    media = Path(media).resolve()
    links = {}
    for p in link_dir.iterdir() if link_dir.is_dir() else []:
        if p.is_symlink():
            target = Path(os.path.realpath(p))
            if media in target.parents:
                links[target] = p
    return links


def local_copies(link_dir, sources):
    """{source path: local file} for real files in link_dir byte-identical to an archive video."""
    by_size = {}
    for s in sources:
        by_size.setdefault(s.stat().st_size, []).append(s)
    copies = {}
    for p in sorted(link_dir.iterdir()) if link_dir.is_dir() else []:
        if p.is_symlink() or p.suffix.lower() != ".mp4" or p.name.startswith("._") or not p.is_file():
            continue
        for s in by_size.get(p.stat().st_size, []):
            if s not in copies and filecmp.cmp(p, s, shallow=False):
                copies[s] = p
                break
    return copies


def read_plan(path):
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {row["source"]: row for row in csv.DictReader(f)}


def unique(name, taken):
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate, n = f"{name}-{n}", n + 1
    taken.add(candidate.lower())
    return candidate


def build_plan(args, sources, links, copies, places, paddocks, calibration):
    previous = {} if args.rename else read_plan(args.plan)
    rows, taken = [], set()
    for video in sources:
        telemetry, vis = ensure_source(args.area, video, calibration)
        if telemetry is None:
            print(f"  {video.name}: no telemetry — skipped", file=sys.stderr)
            continue
        frames = load_telemetry(telemetry)
        start, _ = flight_times(frames)
        if start is None:
            print(f"  {video.name}: no timestamps in telemetry — skipped", file=sys.stderr)
            continue
        copy = copies.get(video)
        hand = next((stem for stem in (video.stem, copy.stem if copy else None)
                     if stem and not DJI_VIDEO_RE.match(stem)), None)
        label, basis = auto_label(video, vis, hand, places, paddocks, args)
        auto_name = f"{start:%Y-%m-%d-%H%M}-{label}"
        kept = previous.get(str(video), {}).get("name")
        name = unique(kept or auto_name, taken)

        current = links.get(video) or copy
        if current is None:
            status = "new link"
        elif copy is not None:
            status = "replace copy" if copy.stem != name else "replace copy (same name)"
        else:
            status = "linked" if current.stem == name else f"rename from {current.stem}"
        rows.append({"name": name, "auto_name": auto_name, "basis": basis, "status": status,
                     "start": start.isoformat(timespec="minutes"),
                     "duration_min": round((frames[-1]["t"] - frames[0]["t"]) / 60, 1),
                     "source": str(video), "_current": current, "_copy": copy,
                     "_vis": vis, "_telemetry": telemetry})
    return sorted(rows, key=lambda r: r["start"])


def write_plan(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def move_derived(area, old, new):
    """Rename a video's telemetry and heatmap files from basename old to new."""
    d = area_dir(area)
    pairs = [(telemetry_path(area, old), telemetry_path(area, new))]
    pairs += [(d / "visibility" / f"{old}{ext}", d / "visibility" / f"{new}{ext}") for ext in (".json", ".bin")]
    for src, dst in pairs:
        if src.exists() and not dst.exists():
            src.rename(dst)


def apply_row(args, row):
    link = args.link_dir / f"{row['name']}.MP4"
    source, current, copy = Path(row["source"]), row["_current"], row["_copy"]

    # Check everything before changing anything, so a skipped row leaves no half-renamed files.
    if (link.exists() or link.is_symlink()) and link != copy:
        if not (link.is_symlink() and Path(os.path.realpath(link)) == source):
            return f"SKIPPED: {link.name} already exists and isn't this video"
    if copy is not None and shutil.which("trash") is None:
        return f"SKIPPED: no 'trash' command to move the local copy {copy.name} aside"

    if current is not None and current.stem != row["name"]:
        move_derived(args.area, current.stem, row["name"])
    if copy is not None:
        subprocess.run(["trash", str(copy)], check=True)
    elif current is not None and current != link:
        current.unlink()                      # an old symlink under the previous name
    if not link.is_symlink():
        link.symlink_to(source)

    d = area_dir(args.area)
    copy_if_missing(row["_telemetry"], telemetry_path(args.area, row["name"]))
    if row["_vis"]:
        for ext in (".json", ".bin"):
            copy_if_missing(Path(row["_vis"]).with_suffix(ext), d / "visibility" / f"{row['name']}{ext}")
    return "done"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("area")
    parser.add_argument("--media", required=True, type=Path, help="Drone archive folder")
    parser.add_argument("--link-dir", type=Path,
                        help="Where the links go (default: the folder the area's MP4s are in, else mp4/<area>)")
    parser.add_argument("--apply", action="store_true", help="Create the links (default is a dry run)")
    parser.add_argument("--rename", action="store_true",
                        help="Propose fresh names for every video, ignoring names in video-links.csv")
    parser.add_argument("--place-share", type=float, default=PLACE_SHARE,
                        help=f"Share of a flight's visibility score a place needs to name it (default {PLACE_SHARE})")
    parser.add_argument("--paddock-share", type=float, default=PADDOCK_SHARE,
                        help=f"Share of the nearby ground a paddock needs to name it (default {PADDOCK_SHARE})")
    parser.add_argument("--within-m", type=float, default=WITHIN_M,
                        help=f"'Nearby ground' for paddocks: seen from within this range (default {WITHIN_M:g} m)")
    args = parser.parse_args()

    d = area_dir(args.area)
    if not d.is_dir():
        sys.exit(f"No area folder {rel(d)}/")
    if not args.media.is_dir():
        sys.exit(f"Archive folder not found (is the drive mounted?): {args.media}")
    args.link_dir = args.link_dir or default_link_dir(args.area)
    args.plan = d / "video-links.csv"
    args.mission_names = load_names(d / "place-names.json")

    places = [(n, g) for n, g, _ in load_polygons(d / "places.geojson")] if (d / "places.geojson").exists() else []
    paddocks = [(n, g) for n, g, _ in load_polygons(d / "paddocks.geojson")] if (d / "paddocks.geojson").exists() else []
    print(f"{len(places)} place(s), {len(paddocks)} paddock(s); links in {rel(args.link_dir)}/")

    sources = find_source_videos(args.media)
    links = existing_links(args.link_dir, args.media)
    print(f"{len(sources)} archive video(s); checking {rel(args.link_dir)}/ for local copies ...")
    copies = local_copies(args.link_dir, sources)
    rows = build_plan(args, sources, links, copies, places, paddocks, load_calibration())

    write_plan(args.plan, rows)
    width = max(len(r["name"]) for r in rows)
    print()
    for r in rows:
        override = "" if r["name"] == r["auto_name"] else f"  (auto: {r['auto_name']})"
        print(f"  {r['name']:<{width}}  {r['basis']:<7}  {r['status']}{override}")
    counts = {}
    for r in rows:
        kind = re.sub(r" (from .*|\(.*\))$", "", r["status"])
        counts[kind] = counts.get(kind, 0) + 1
    print(f"\nPlan written to {rel(args.plan)} — {', '.join(f'{v} {k}' for k, v in counts.items())}")

    if not args.apply:
        print("Dry run. Edit names in the plan if needed, then re-run with --apply.")
        return

    args.link_dir.mkdir(parents=True, exist_ok=True)
    (d / "telemetry").mkdir(exist_ok=True)
    (d / "visibility").mkdir(exist_ok=True)
    print("\nApplying:")
    problems = 0
    for r in rows:
        if r["status"] == "linked":
            continue
        result = apply_row(args, r)
        problems += result != "done"
        print(f"  {result:<6}  {r['name']}")
    write_tracks(args.area, [(telemetry_path(args.area, r["name"]), {"video": r["name"], "path": r["source"]})
                             for r in rows if telemetry_path(args.area, r["name"]).exists()])
    print(f"\nNext: python3 dem/process_flights.py --area {args.area} "
          f"&& python3 dem/export_coverage.py --area {args.area}")
    if problems:
        sys.exit(2)


if __name__ == "__main__":
    main()
