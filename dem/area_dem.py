#!/usr/bin/env python3
"""Manage per-location DEM areas under dem/areas/<name>/.

An area groups every flight filmed at one location — the MP4s in any
mp4/**/<name>/ folder — and holds the DEM data the viewer uses for them:

    dem/areas/index.json          area names + flight-dem bounds (read by the viewer)
    dem/areas/<name>/
      extent.geojson              polygon to order DEM tiles for (EPSG:4326)
      elvis-tiles.json            ELVIS tile listing for the extent (from `tiles`)
      elvis/                      put downloaded ELVIS zips / GeoTIFF tiles here
      area-dem.tif                mosaicked source DEM, EPSG:4326 (from `ingest`)
      flight-dem.{bin,json}       browser DEM clipped from area-dem.tif
      telemetry/                  <basename>-telemetry.json   (process_flights.py)
      visibility/                 <basename>.{bin,json}       (process_flights.py)

Workflow for a new location:

    python3 dem/area_dem.py extent marathon --buffer 1000 [--boundary site.kml]
    python3 dem/area_dem.py tiles marathon
    #   order the tiles at https://elevation.fsdf.org.au/ by uploading
    #   dem/areas/marathon/extent.geojson, then put the emailed zip(s) in
    #   dem/areas/marathon/elvis/
    python3 dem/area_dem.py ingest marathon
    python3 dem/process_flights.py --area marathon

Other commands:

    python3 dem/area_dem.py clip marathon     # re-clip flight-dem from area-dem.tif
    python3 dem/area_dem.py index             # rebuild dem/areas/index.json
"""

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "dem"
AREAS_DIR = DEM_DIR / "areas"
MP4_DIR = ROOT / "mp4"
INDEX_PATH = AREAS_DIR / "index.json"
PREPROCESS_DEM = DEM_DIR / "preprocess_dem.py"

ELVIS_PORTAL = "https://elevation.fsdf.org.au/"
# Undocumented API used by the ELVIS portal itself. It is only used to list
# tiles for information; downloads go through the portal (emailed link).
ELVIS_DOWNLOADABLES = "https://api.elevation.fsdf.org.au/elevation/downloadables"

# Public 1° Copernicus GLO-30 surface-model tiles, used under the LiDAR to fill
# parts of the extent bounds that the ordered tiles don't cover.
COPERNICUS_URL = ("/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/"
                  "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
                  "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")

NODATA = -3.4028234663852886e38   # float32 lowest; preprocess_dem.py treats < -1e30 as NoData
TRACK_SAMPLE_STEP = 10            # use every Nth telemetry frame for extent/coverage checks
M_PER_DEG_LAT = 111320.0


# ── Area / flight lookup (shared with process_flights.py) ──

def rel(path):
    """Path relative to the project root when possible, for tidy log output."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def area_dir(name):
    return AREAS_DIR / name


def find_mp4s(mp4_dir=MP4_DIR):
    """Every MP4 under mp4_dir, skipping macOS AppleDouble '._' files."""
    return sorted(p for p in Path(mp4_dir).rglob("*")
                  if p.suffix.lower() == ".mp4" and p.is_file() and not p.name.startswith("._"))


def area_for_mp4(mp4_path):
    """An MP4 belongs to an area when its parent folder matches a dem/areas/<name>/ folder."""
    name = mp4_path.parent.name
    return name if area_dir(name).is_dir() else None


def area_mp4s(name, mp4_dir=MP4_DIR):
    return [p for p in find_mp4s(mp4_dir) if p.parent.name == name]


def telemetry_path(area, basename):
    return area_dir(area) / "telemetry" / f"{basename}-telemetry.json"


def load_track_points(path, step=TRACK_SAMPLE_STEP):
    """(lon, lat) pairs from a telemetry JSON, skipping frames without a GPS fix."""
    with open(path) as f:
        data = json.load(f)
    return [(e["lon"], e["lat"]) for e in data[::step]
            if abs(e.get("lat", 0)) > 1e-6 and abs(e.get("lon", 0)) > 1e-6]


def ensure_telemetry(area, mp4s):
    from process_flights import extract_telemetry  # local import: process_flights imports this module
    for mp4 in mp4s:
        path = telemetry_path(area, mp4.stem)
        if not path.exists():
            extract_telemetry(mp4, path)


# ── Geometry helpers ──

def metric_transformers(lon):
    """(to_metres, to_lonlat) transform functions for the GDA2020 MGA zone containing lon."""
    from pyproj import Transformer
    zone = int((lon + 180) // 6) + 1
    epsg = f"EPSG:{7800 + zone}"  # GDA2020 / MGA zone N
    to_m = Transformer.from_crs("EPSG:4326", epsg, always_xy=True).transform
    to_ll = Transformer.from_crs(epsg, "EPSG:4326", always_xy=True).transform
    return to_m, to_ll


def read_boundary(path):
    """All polygon geometries in a vector file (KML, GeoJSON, GPKG, SHP...), as shapely in EPSG:4326."""
    from osgeo import ogr, osr
    from shapely.geometry import shape
    ogr.UseExceptions()
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds = ogr.Open(str(path))
    if ds is None:
        sys.exit(f"Could not open boundary file {path}")
    geoms = []
    for layer in ds:
        srs = layer.GetSpatialRef()
        xform = None
        if srs is not None:
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not srs.IsSame(wgs84):
                xform = osr.CoordinateTransformation(srs, wgs84)
        for feature in layer:
            geom = feature.GetGeometryRef()
            if geom is None:
                continue
            geom = geom.Clone()
            if xform:
                geom.Transform(xform)
            geoms.append(shape(json.loads(geom.ExportToJson())))
    return geoms


def round_coords(obj, ndigits=7):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, ndigits) for v in obj]
        return [round_coords(v, ndigits) for v in obj]
    return obj


def extent_path(area):
    return area_dir(area) / "extent.geojson"


def load_extent(area):
    from shapely.geometry import shape
    from shapely.ops import unary_union
    path = extent_path(area)
    if not path.exists():
        sys.exit(f"{rel(path)} not found — run: python3 dem/area_dem.py extent {area}")
    with open(path) as f:
        gj = json.load(f)
    features = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    return unary_union([shape(f["geometry"]) for f in features])


def report_flight_coverage(area, polygon):
    """Print how much of each of the area's flights lies inside polygon."""
    from shapely.geometry import Point
    from shapely.prepared import prep
    inside_poly = prep(polygon)
    paths = sorted((area_dir(area) / "telemetry").glob("*-telemetry.json"))
    if not paths:
        print("  (no telemetry for this area yet)")
    for path in paths:
        pts = load_track_points(path)
        inside = sum(inside_poly.contains(Point(p)) for p in pts)
        flag = "" if inside == len(pts) else "   <-- partly outside"
        print(f"  {path.name[:-len('-telemetry.json')]}: {inside}/{len(pts)} sampled positions inside{flag}")


# ── extent ──

def cmd_extent(args):
    from shapely.geometry import MultiPoint, mapping
    from shapely.ops import transform, unary_union

    out_path = extent_path(args.area)
    if out_path.exists() and not args.force:
        sys.exit(f"{rel(out_path)} already exists; pass --force to replace it")

    mp4s = area_mp4s(args.area)
    print(f"Area '{args.area}': {len(mp4s)} MP4(s) in mp4/**/{args.area}/")
    ensure_telemetry(args.area, mp4s)

    tracks = {}
    for mp4 in mp4s:
        pts = load_track_points(telemetry_path(args.area, mp4.stem))
        if pts:
            tracks[mp4.stem] = pts
    boundary = read_boundary(args.boundary) if args.boundary else []
    if not tracks and not boundary:
        sys.exit("Nothing to build an extent from: no flight telemetry and no --boundary")

    ref_lon = (boundary[0].centroid.x if boundary else next(iter(tracks.values()))[0][0])
    to_m, to_ll = metric_transformers(ref_lon)
    parts = [transform(to_m, g) for g in boundary]
    parts += [MultiPoint([to_m(*p) for p in pts]).convex_hull for pts in tracks.values()]
    extent_m = unary_union(parts).buffer(args.buffer, quad_segs=8).simplify(args.simplify)
    extent_ll = transform(to_ll, extent_m)

    feature = {
        "type": "Feature",
        "properties": {
            "name": args.area,
            "buffer_m": args.buffer,
            "boundary": Path(args.boundary).name if args.boundary else None,
            "flights": sorted(tracks),
        },
        "geometry": round_coords(mapping(extent_ll)),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [feature]}, f)

    w, s, e, n = extent_ll.bounds
    print(f"Written: {rel(out_path)}")
    print(f"  {extent_ll.geom_type}, {extent_m.area / 1e6:.1f} km², bounds W={w:.5f} S={s:.5f} E={e:.5f} N={n:.5f}")
    if extent_ll.geom_type != "Polygon":
        print("  Note: the flights are far apart, so the extent is several polygons.")
    report_flight_coverage(args.area, extent_ll)
    print(f"\nNext: python3 dem/area_dem.py tiles {args.area}")


# ── tiles ──

MAX_QUERY_URL = 3500  # ELVIS answers a GET; a long outline overflows the URI (HTTP 414)


def query_elvis(polygon):
    """Raw `available_data` list from the ELVIS downloadables API for a polygon.

    The outline is simplified as much as the URL length needs — the listing is
    informational, and the portal gets the full polygon when you order.
    """
    if polygon.geom_type != "Polygon":
        polygon = polygon.convex_hull
    simplified = polygon
    for tolerance in (0, 0.0002, 0.0005, 0.001, 0.002, 0.005):  # degrees: ~0 to ~500 m
        simplified = polygon.simplify(tolerance) if tolerance else polygon
        wkt = "POLYGON((" + ",".join(f"{x:.6f} {y:.6f}" for x, y in simplified.exterior.coords) + "))"
        url = ELVIS_DOWNLOADABLES + "?" + urllib.parse.urlencode({"polygon": wkt}, quote_via=urllib.parse.quote)
        if len(url) <= MAX_QUERY_URL:
            break
    else:
        simplified = polygon.envelope  # last resort: the bounding box
        wkt = "POLYGON((" + ",".join(f"{x:.6f} {y:.6f}" for x, y in simplified.exterior.coords) + "))"
        url = ELVIS_DOWNLOADABLES + "?" + urllib.parse.urlencode({"polygon": wkt}, quote_via=urllib.parse.quote)
    if len(simplified.exterior.coords) < len(polygon.exterior.coords):
        print(f"  (querying with a simplified outline: {len(simplified.exterior.coords)} points "
              f"instead of {len(polygon.exterior.coords)}, to fit the request URL)")
    # The API sits behind CloudFront rules that reject requests which don't
    # look like they come from the portal.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "Origin": ELVIS_PORTAL.rstrip("/"),
        "Referer": ELVIS_PORTAL,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp).get("available_data", [])


def survey_name(file_name):
    return re.split(r"[-_]", file_name, maxsplit=1)[0]


def cmd_tiles(args):
    polygon = load_extent(args.area)
    print(f"Querying ELVIS for {rel(extent_path(args.area))} ...")
    try:
        available = query_elvis(polygon)
    except Exception as exc:  # network / API change — the portal still works manually
        sys.exit(f"ELVIS query failed ({exc}). Upload the extent at {ELVIS_PORTAL} to see what's available.")

    out_path = area_dir(args.area) / "elvis-tiles.json"
    with open(out_path, "w") as f:
        json.dump(available, f, indent=1)

    for source in available:
        for data_type, by_res in (source.get("downloadables") or {}).items():
            for res, files in (by_res or {}).items():
                if not files:
                    continue
                surveys = defaultdict(lambda: [0, 0.0])
                for item in files:
                    agg = surveys[survey_name(item["file_name"]) if item.get("file_size") else item["file_name"]]
                    agg[0] += 1
                    agg[1] += float(item.get("file_size") or 0) / 1e6
                print(f"\n{source['source']} — {data_type} — {res}")
                for name, (count, mb) in sorted(surveys.items()):
                    size = f"{count} tiles, {mb:,.0f} MB" if mb else "whole-dataset product"
                    print(f"    {name}: {size}")

    print(f"\nFull listing written to {rel(out_path)}")
    print(f"\nTo order: open {ELVIS_PORTAL}, upload {rel(extent_path(args.area))} as the area,")
    print("tick the DEM surveys you want (GeoTIFF output; any CRS), and submit.")
    print(f"Put the emailed zip(s) in {rel(area_dir(args.area) / 'elvis')}/ then run:")
    print(f"    python3 dem/area_dem.py ingest {args.area}")


# ── ingest / clip ──

def extract_zips(root):
    """Unzip every zip under root next to itself, repeating for zips inside zips."""
    seen = set()
    while True:
        pending = [z for z in root.rglob("*.zip") if not z.name.startswith("._") and z not in seen]
        if not pending:
            return
        for zpath in pending:
            seen.add(zpath)
            dest = zpath.with_suffix("")
            marker = dest / ".extracted"
            if marker.exists():
                continue
            print(f"  unzipping {rel(zpath)}")
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(dest)
            marker.touch()


def survey_sort_key(path):
    """Oldest survey first, so gdalwarp paints newer surveys over older ones.

    Survey tiles start with their name and year (Nile2017-DEM-1m_...); names
    without a leading year (e.g. statewide mosaics) sort first as a base layer.
    """
    match = re.match(r"[A-Za-z]+(\d{4})", path.name)
    return (int(match.group(1)) if match else 0, path.name)


def copernicus_tiles(bounds):
    """/vsicurl paths of the existing Copernicus GLO-30 tiles covering bounds (W, S, E, N)."""
    from osgeo import gdal
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    w, s, e, n = bounds
    paths = []
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            path = COPERNICUS_URL.format(ns="S" if lat < 0 else "N", lat=abs(lat),
                                         ew="W" if lon < 0 else "E", lon=abs(lon))
            if gdal.VSIStatL(path) is not None:  # no tiles over open ocean
                paths.append(path)
    return paths


def make_flight_dem(area, bounds, resolution):
    """Clip area-dem.tif to bounds (W, S, E, N) as flight-dem.{bin,json} via preprocess_dem.py."""
    d = area_dir(area)
    w, s, e, n = bounds
    cmd = [sys.executable, str(PREPROCESS_DEM),
           "--dem", str(d / "area-dem.tif"),
           "--output-dir", str(d),
           f"--bounds={s},{w},{n},{e}",
           "--buffer", "0",
           "--resolution", str(resolution)]
    print(f"\nClipping flight DEM at {resolution} m ...")
    if subprocess.run(cmd).returncode != 0:
        sys.exit("preprocess_dem.py failed")


def cmd_ingest(args):
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()

    d = area_dir(args.area)
    elvis_dir = d / "elvis"
    if not elvis_dir.is_dir():
        sys.exit(f"{rel(elvis_dir)}/ not found — put the ELVIS download(s) there first")
    polygon = load_extent(args.area)

    extract_zips(elvis_dir)
    tiles = sorted((p for p in elvis_dir.rglob("*")
                    if p.suffix.lower() in (".tif", ".tiff") and not p.name.startswith("._")),
                   key=survey_sort_key)
    other_rasters = [p for p in elvis_dir.rglob("*") if p.suffix.lower() in (".asc", ".xyz", ".grd", ".ecw")]
    if other_rasters:
        print(f"  Note: ignoring {len(other_rasters)} non-GeoTIFF raster(s) (e.g. {other_rasters[0].name}); "
              "order GeoTIFF output to use them")
    if not tiles:
        sys.exit(f"No GeoTIFF tiles found under {rel(elvis_dir)}/")

    counts = defaultdict(int)
    for t in tiles:
        counts[survey_name(t.name)] += 1
    print(f"Found {len(tiles)} GeoTIFF tile(s) — later surveys take priority where they overlap:")
    for name in sorted(counts, key=lambda n: survey_sort_key(Path(n))):
        print(f"    {name}: {counts[name]}")

    w, s, e, n = polygon.bounds
    sources = [str(t) for t in tiles]
    if args.fill == "copernicus":
        fill = copernicus_tiles((w, s, e, n))
        print(f"Filling gaps under the tiles with Copernicus GLO-30 (30 m surface model, "
              f"includes tree canopy): {len(fill)} tile(s)")
        sources = fill + sources  # first source is painted first, so tiles win where present

    x_res = args.resolution / (M_PER_DEG_LAT * math.cos(math.radians((s + n) / 2)))
    y_res = args.resolution / M_PER_DEG_LAT
    out_path = d / "area-dem.tif"
    tmp_path = d / "area-dem.tmp.tif"
    print(f"\nMosaicking to EPSG:4326 at ~{args.resolution} m over the extent bounds ...")
    gdal.Warp(
        str(tmp_path), sources,
        options=gdal.WarpOptions(
            dstSRS="EPSG:4326",
            outputBounds=(w, s, e, n),
            xRes=x_res, yRes=y_res,
            resampleAlg="bilinear",
            dstNodata=NODATA,
            outputType=gdal.GDT_Float32,
            multithread=True,
            warpOptions=["NUM_THREADS=ALL_CPUS"],
            creationOptions=["COMPRESS=LZW", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"],
            callback=gdal.TermProgress_nocb,
        ),
    )
    tmp_path.replace(out_path)

    ds = gdal.Open(str(out_path))
    data = ds.GetRasterBand(1).ReadAsArray()
    valid = data > -1e30
    size_mb = out_path.stat().st_size / 1e6
    print(f"Written: {rel(out_path)} ({ds.RasterXSize} x {ds.RasterYSize}, {size_mb:.0f} MB)")
    if valid.any():
        print(f"  elevation {data[valid].min():.1f} – {data[valid].max():.1f} m, "
              f"{valid.mean() * 100:.1f}% of the extent bounds has data")
    else:
        sys.exit("  No valid elevations in the mosaic — do the tiles overlap the extent?")
    ds = None

    make_flight_dem(args.area, (w, s, e, n), args.flight_resolution)
    write_index()
    print("\nFlights in this area:")
    report_flight_coverage(args.area, polygon)
    print(f"\nNext: python3 dem/process_flights.py --area {args.area}"
          " (add --force to recompute visibility made with an older DEM)")


def cmd_clip(args):
    if not (area_dir(args.area) / "area-dem.tif").exists():
        sys.exit(f"{rel(area_dir(args.area) / 'area-dem.tif')} not found — run ingest first")
    make_flight_dem(args.area, load_extent(args.area).bounds, args.flight_resolution)
    write_index()


# ── index ──

def write_index():
    areas = []
    for meta_path in sorted(AREAS_DIR.glob("*/flight-dem.json")):
        with open(meta_path) as f:
            meta = json.load(f)
        areas.append({
            "name": meta_path.parent.name,
            "bounds": meta["bounds"],
            "resolution_m": meta.get("resolution_m"),
        })
    AREAS_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump({"areas": areas}, f, indent=2)
    print(f"Written: {rel(INDEX_PATH)} ({', '.join(a['name'] for a in areas) or 'no areas with a flight DEM'})")


def cmd_index(args):
    write_index()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n".join(__doc__.splitlines()[1:]))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extent", help="Build extent.geojson from the area's flights (+ optional boundary file)")
    p.add_argument("area")
    p.add_argument("--buffer", type=float, default=1000.0, help="Metres to buffer the flights/boundary (default 1000)")
    p.add_argument("--boundary", help="Vector file (KML, GeoJSON, GPKG, SHP) to include, e.g. property parcels")
    p.add_argument("--simplify", type=float, default=5.0, help="Outline simplification tolerance in metres (default 5)")
    p.add_argument("--force", action="store_true", help="Replace an existing extent.geojson")
    p.set_defaults(func=cmd_extent)

    p = sub.add_parser("tiles", help="List ELVIS DEM tiles available for the area's extent")
    p.add_argument("area")
    p.set_defaults(func=cmd_tiles)

    p = sub.add_parser("ingest", help="Mosaic ELVIS tiles into area-dem.tif and clip the flight DEM")
    p.add_argument("area")
    p.add_argument("--resolution", type=float, default=2.0, help="area-dem.tif cell size in metres (default 2)")
    p.add_argument("--flight-resolution", type=float, default=5.0, help="flight-dem cell size in metres (default 5)")
    p.add_argument("--fill", choices=["copernicus", "none"], default="copernicus",
                   help="What to put where the tiles don't cover the extent bounds (default copernicus; "
                        "'none' leaves NoData, which preprocess_dem.py fills with the minimum elevation)")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("clip", help="Re-clip flight-dem.{bin,json} from an existing area-dem.tif")
    p.add_argument("area")
    p.add_argument("--flight-resolution", type=float, default=5.0, help="flight-dem cell size in metres (default 5)")
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser("index", help="Rebuild dem/areas/index.json for the viewer")
    p.set_defaults(func=cmd_index)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
