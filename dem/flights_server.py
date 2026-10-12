#!/usr/bin/env python3
"""Serve the project root, plus a small API behind flights.html.

flights.html lists every MP4 under mp4/ with how far it has got through the
pipeline (telemetry, heatmap) and lets you queue process_flights.py runs for
chosen videos. Runs one job at a time; the log streams back to the page.

Replaces `python3 -m http.server` — index.html and places.html are served as
before, so one server does everything. It also lets places.html save
dem/areas/<area>/places.geojson in any browser (PUT /api/places/<area>).

Usage:
    python3 dem/flights_server.py            # http://localhost:5002/flights.html
    python3 dem/flights_server.py --port 8000
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from area_dem import AREAS_DIR, DEM_DIR, INDEX_PATH, MP4_DIR, ROOT, area_dir, area_for_mp4, load_track_points, rel, telemetry_path
from process_flights import load_calibration, visibility_is_current

PROCESS_FLIGHTS = DEM_DIR / "process_flights.py"
LOG_LINES = 400

# Fraction of each flight's track inside its area's DEM bounds, keyed by
# telemetry path and cached against the telemetry file's mtime.
_coverage_cache = {}


def area_bounds():
    if not INDEX_PATH.exists():
        return {}
    return {a["name"]: a["bounds"] for a in json.loads(INDEX_PATH.read_text()).get("areas", [])}


def dem_coverage(telemetry, bounds):
    """Share of the flight's track (0-1) inside the DEM bounds, or None if unknown."""
    if not bounds or not telemetry.exists():
        return None
    mtime = telemetry.stat().st_mtime
    cached = _coverage_cache.get(telemetry)
    if cached and cached[0] == mtime:
        return cached[1]
    points = load_track_points(telemetry)
    inside = sum(1 for lon, lat in points
                 if bounds["west"] <= lon <= bounds["east"] and bounds["south"] <= lat <= bounds["north"])
    fraction = inside / len(points) if points else None
    _coverage_cache[telemetry] = (mtime, fraction)
    return fraction


def list_mp4s():
    """Every MP4 under mp4/, including archive links whose drive isn't mounted."""
    return sorted(p for p in MP4_DIR.rglob("*")
                  if p.suffix.lower() == ".mp4" and not p.name.startswith("._")
                  and (p.is_file() or p.is_symlink()))


def flight_status(mp4, calibration, bounds_by_area):
    area = area_for_mp4(mp4)
    online = mp4.exists()
    info = {
        "path": str(rel(mp4)),
        "name": mp4.name,
        "folder": str(rel(mp4.parent)),
        "area": area,
        "online": online,
        "link": mp4.is_symlink(),
        "size": mp4.stat().st_size if online else None,
        "modified": mp4.stat().st_mtime if online else None,
        "telemetry": None,
        "heatmap": "no-area",
        "heatmap_made": None,
        "dem_coverage": None,
    }
    if not area:
        return info
    telemetry = telemetry_path(area, mp4.stem)
    info["telemetry"] = str(rel(telemetry)) if telemetry.exists() else None
    info["dem_coverage"] = dem_coverage(telemetry, bounds_by_area.get(area))
    vis = area_dir(area) / "visibility" / f"{mp4.stem}.json"
    if not (area_dir(area) / "area-dem.tif").exists():
        info["heatmap"] = "no-dem"
    elif vis.exists() and vis.with_suffix(".bin").exists():
        info["heatmap"] = "current" if visibility_is_current(vis, calibration) else "stale"
        info["heatmap_made"] = vis.stat().st_mtime
    else:
        info["heatmap"] = "missing"
    return info


class Jobs:
    """One process_flights.py run at a time, fed from a queue of MP4 paths."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = deque()      # (path, force)
        self.current = None       # path being processed
        self.log = deque(maxlen=LOG_LINES)
        self.results = {}         # path -> last status line
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, paths, force):
        with self.lock:
            queued = {p for p, _ in self.queue} | {self.current}
            for p in paths:
                if p not in queued:
                    self.queue.append((p, force))
                    self.results.pop(p, None)

    def cancel(self, paths):
        with self.lock:
            self.queue = deque((p, f) for p, f in self.queue if p not in paths)

    def snapshot(self):
        with self.lock:
            return {"current": self.current, "queued": [p for p, _ in self.queue],
                    "results": dict(self.results), "log": list(self.log)}

    def _say(self, line):
        with self.lock:
            self.log.append(line)

    def _run(self):
        while True:
            with self.lock:
                job = self.queue.popleft() if self.queue else None
                self.current = job[0] if job else None
            if not job:
                time.sleep(0.5)
                continue
            path, force = job
            cmd = [sys.executable, "-u", str(PROCESS_FLIGHTS), str(ROOT / path)] + (["--force"] if force else [])
            self._say(f"$ process_flights.py {path}{' --force' if force else ''}")
            t0 = time.time()
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                self._say(line.rstrip())
            proc.wait()
            status = "OK" if proc.returncode == 0 else "FAIL"
            self._say(f"→ {status} in {time.time() - t0:.0f}s")
            with self.lock:
                self.results[path] = status


class Handler(SimpleHTTPRequestHandler):
    """Static files with byte-range support, so the viewer can stream and seek
    multi-GB MP4s, plus the flights API."""
    jobs = None
    _range_remaining = None

    def send_head(self):
        byte_range = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        path = self.translate_path(self.path)
        if not byte_range or os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        size = os.fstat(f.fileno()).st_size
        first, last = byte_range.groups()
        if first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        elif last:
            start, end = max(size - int(last), 0), size - 1
        else:
            start, end = 0, size - 1
        if start > end:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        f.seek(start)
        self._range_remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining, self._range_remaining = self._range_remaining, None
        try:
            if remaining is None:
                return super().copyfile(source, outputfile)
            while remaining > 0:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser dropped a video request after seeking

    def end_headers(self):
        # Make browsers revalidate pages (Safari otherwise keeps serving stale
        # copies of edited HTML); unchanged files still come back as 304s.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/flights":
            calibration = load_calibration()
            bounds = area_bounds()
            return self._json({"flights": [flight_status(p, calibration, bounds) for p in list_mp4s()],
                               "jobs": self.jobs.snapshot()})
        if self.path == "/api/jobs":
            return self._json(self.jobs.snapshot())
        return super().do_GET()

    def do_PUT(self):
        area = re.fullmatch(r"/api/places/([\w-]+)", self.path)
        if not area or not area_dir(area.group(1)).is_dir() or area_dir(area.group(1)).parent != AREAS_DIR:
            return self._json({"error": "unknown area"}, 404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return self._json({"error": "bad JSON"}, 400)
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
            return self._json({"error": "expected a GeoJSON FeatureCollection"}, 400)
        path = area_dir(area.group(1)) / "places.geojson"
        # Write via a temp file so a failed save can't truncate it, keeping the previous version.
        tmp = path.with_suffix(".geojson.tmp")
        tmp.write_bytes(body)
        if path.exists():
            shutil.copy2(path, path.with_suffix(".geojson.bak"))
        tmp.replace(path)
        return self._json({"saved": str(rel(path)), "features": len(data["features"])})

    def do_POST(self):
        if self.path not in ("/api/process", "/api/cancel"):
            return self._json({"error": "not found"}, 404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad JSON"}, 400)
        # Only accept MP4s we'd list ourselves, so the API can't be pointed elsewhere.
        known = {str(rel(p)) for p in list_mp4s()}
        paths = [p for p in body.get("paths", []) if p in known]
        if self.path == "/api/process":
            self.jobs.add(paths, bool(body.get("force")))
        else:
            self.jobs.cancel(paths)
        return self._json(self.jobs.snapshot())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    Handler.jobs = Jobs()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(ROOT)))
    print(f"Serving {ROOT} at http://localhost:{args.port}/  (flights: /flights.html)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
