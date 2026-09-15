#!/usr/bin/env python3
"""Batch-process area MP4s into per-area telemetry + visibility data.

An MP4 belongs to an area when its parent folder has the same name as a
dem/areas/<name>/ folder, e.g. mp4/matrice-4E-mp4/marathon/*.MP4 belongs to
dem/areas/marathon/ (set areas up with dem/area_dem.py). For each such MP4:
  1. Extract embedded DJI subtitle track to an SRT stream via ffmpeg,
     parse it, and write dem/areas/<area>/telemetry/<basename>-telemetry.json.
  2. Invoke compute_visibility.py against dem/areas/<area>/area-dem.tif to
     produce dem/areas/<area>/visibility/<basename>.{bin,json}.

Both steps are skipped if their outputs already exist (use --force to
reprocess). Step 2 waits until the area has an area-dem.tif (see
`area_dem.py ingest`). MP4s outside an area folder are skipped. ffmpeg must
be on PATH.

Usage:
    python3 dem/process_flights.py
    python3 dem/process_flights.py --area marathon
    python3 dem/process_flights.py --force
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from area_dem import DEM_DIR, MP4_DIR, area_dir, area_for_mp4, find_mp4s, rel, telemetry_path

COMPUTE_VIS = DEM_DIR / "compute_visibility.py"

# Camera calibration saved from the viewer's calibration panel.
CALIBRATION_FILE = DEM_DIR / "camera-calibration.json"
DEFAULT_CALIBRATION = {"pitch_offset_deg": 0.0, "yaw_offset_deg": 0.0, "fov_scale": 1.0}
CALIBRATION_FLAGS = {"pitch_offset_deg": "--pitch-offset", "yaw_offset_deg": "--yaw-offset", "fov_scale": "--fov-scale"}


def read_calibration(data):
    """Calibration values from a dict, falling back to defaults for anything missing."""
    cal = dict(DEFAULT_CALIBRATION)
    for key in cal:
        if isinstance((data or {}).get(key), (int, float)):
            cal[key] = float(data[key])
    return cal


def load_calibration():
    if not CALIBRATION_FILE.exists():
        return dict(DEFAULT_CALIBRATION)
    with open(CALIBRATION_FILE) as f:
        return read_calibration(json.load(f))


def visibility_calibration(vis_json):
    """Calibration an existing visibility heatmap was computed with."""
    with open(vis_json) as f:
        return read_calibration(json.load(f).get("config", {}).get("calibration"))


# Same regex shape the JS parser uses (see index.html parseTelemetryLine /
# parseSRT). Keep this in sync if the field list there ever grows.
FIELD_RE = re.compile(r"(\w+):\s*([-\d.\/]+)")
TS_RE = re.compile(r"FrameCnt:\s*\d+\s+(.+?)(?:\s*\[|$)")
TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")

# Map DJI SRT field name -> output JSON key. Fields not listed are dropped.
FIELD_MAP = {
    "latitude": ("lat", float),
    "longitude": ("lon", float),
    "rel_alt": ("relAlt", float),
    "abs_alt": ("absAlt", float),
    "gb_yaw": ("yaw", float),
    "gb_pitch": ("pitch", float),
    "iso": ("iso", str),
    "shutter": ("shutter", str),
    "fnum": ("fnum", str),
    "focal_len": ("focalLen", str),
}


def parse_telemetry_line(text, t):
    entry = {"t": t}
    for m in FIELD_RE.finditer(text):
        key, val = m.group(1), m.group(2)
        if key in FIELD_MAP:
            out_key, caster = FIELD_MAP[key]
            entry[out_key] = caster(val) if caster is float else val.strip()
    tsm = TS_RE.search(text)
    if tsm:
        entry["timestamp"] = tsm.group(1).strip()
    if "lat" in entry and "lon" in entry:
        return entry
    return None


def parse_srt(text):
    entries = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        tm = TIME_RE.search(lines[1])
        if not tm:
            continue
        t = (int(tm.group(1)) * 3600 + int(tm.group(2)) * 60
             + int(tm.group(3)) + int(tm.group(4)) / 1000)
        data = " ".join(lines[2:])
        e = parse_telemetry_line(data, t)
        if e:
            entries.append(e)
    entries.sort(key=lambda x: x["t"])
    return entries


def extract_telemetry(mp4_path, out_json):
    """Use ffmpeg to pipe the first subtitle stream as SRT, parse, write JSON."""
    print(f"  extracting telemetry from {mp4_path.name} ...")
    t0 = time.time()
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(mp4_path),
         "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"    ffmpeg failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    entries = parse_srt(result.stdout)
    if not entries:
        print("    no telemetry entries parsed (empty subtitle track?)", file=sys.stderr)
        return False
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(entries, f)
    print(f"    {len(entries)} entries -> {rel(out_json)}  ({time.time()-t0:.1f}s)")
    return True


def video_frame_size(mp4_path):
    """'WIDTHxHEIGHT' of the first video stream via ffprobe, or None."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(mp4_path)],
        capture_output=True, text=True, check=False,
    )
    size = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return size if re.fullmatch(r"\d+x\d+", size) else None


def run_compute_visibility(telemetry_json, area, mp4_path, calibration):
    """Invoke compute_visibility.py as a subprocess against the area's DEM."""
    d = area_dir(area)
    cmd = [sys.executable, str(COMPUTE_VIS),
           "--telemetry", str(telemetry_json),
           "--dem", str(d / "area-dem.tif"),
           "--output-dir", str(d)]
    for key, flag in CALIBRATION_FLAGS.items():
        cmd += [flag, str(calibration[key])]
    frame_size = video_frame_size(mp4_path)
    if frame_size:
        cmd += ["--frame-aspect", frame_size]
    print(f"  computing visibility for {telemetry_json.name} ...")
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def process_one(mp4_path, area, calibration, force=False):
    """Returns 'OK', 'TELEMETRY ONLY' (area has no DEM yet) or 'FAIL'."""
    basename = mp4_path.stem
    d = area_dir(area)
    telemetry_json = telemetry_path(area, basename)
    vis_json = d / "visibility" / f"{basename}.json"
    vis_bin = d / "visibility" / f"{basename}.bin"

    # Step 1: telemetry
    if telemetry_json.exists() and not force:
        print(f"  telemetry already present: {rel(telemetry_json)}")
    elif not extract_telemetry(mp4_path, telemetry_json):
        print(f"  SKIPPED: telemetry extraction failed")
        return "FAIL"

    # Step 2: visibility
    if not (d / "area-dem.tif").exists():
        print(f"  visibility waiting on {rel(d / 'area-dem.tif')} — run: python3 dem/area_dem.py ingest {area}")
        return "TELEMETRY ONLY"
    if vis_json.exists() and vis_bin.exists() and not force:
        if visibility_calibration(vis_json) == calibration:
            print(f"  visibility already present: {rel(vis_json)}")
            return "OK"
        print(f"  camera calibration changed since {rel(vis_json)} was made — recomputing")
    if not run_compute_visibility(telemetry_json, area, mp4_path, calibration):
        print(f"  FAILED: visibility compute")
        return "FAIL"
    return "OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mp4-dir", default=str(MP4_DIR),
                        help=f"Directory searched recursively for MP4s (default: {rel(MP4_DIR)})")
    parser.add_argument("--area", help="Only process MP4s belonging to this area")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess even if outputs already exist")
    args = parser.parse_args()

    if not COMPUTE_VIS.exists():
        print(f"Missing {COMPUTE_VIS}", file=sys.stderr)
        sys.exit(1)
    if args.area and not area_dir(args.area).is_dir():
        print(f"No area folder {rel(area_dir(args.area))}/ — set it up with: "
              f"python3 dem/area_dem.py extent {args.area}", file=sys.stderr)
        sys.exit(1)

    located = [(p, area_for_mp4(p)) for p in find_mp4s(args.mp4_dir)]
    jobs = [(p, a) for p, a in located if a and (not args.area or a == args.area)]
    unlocated = [p for p, a in located if not a]

    if unlocated and not args.area:
        print(f"Skipping {len(unlocated)} MP4(s) that aren't in an area folder:")
        for p in unlocated:
            print(f"  {rel(p)}")
        print()
    if not jobs:
        print("No area MP4s to process", file=sys.stderr)
        sys.exit(1)

    calibration = load_calibration()
    print(f"Processing {len(jobs)} MP4(s)")
    if CALIBRATION_FILE.exists():
        print(f"Camera calibration from {rel(CALIBRATION_FILE)}: pitch {calibration['pitch_offset_deg']:+.1f}°, "
              f"heading {calibration['yaw_offset_deg']:+.1f}°, FOV scale {calibration['fov_scale']:.3f}")
    if args.force:
        print("--force: will reprocess even when outputs exist")

    summary = []
    for p, area in jobs:
        print()
        print(f"=== [{area}] {p.name} ===")
        summary.append((area, p.name, process_one(p, area, calibration, force=args.force)))

    print()
    print("=== Summary ===")
    for area, name, status in summary:
        print(f"  {status:<14}  [{area}] {name}")
    if any(status == "FAIL" for _, _, status in summary):
        sys.exit(2)


if __name__ == "__main__":
    main()
