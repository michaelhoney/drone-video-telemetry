# DJI Telemetry Map Viewer v0.6

Current version: `v0.6`

An HTML application that plays DJI drone video alongside a live satellite map, synchronised frame-by-frame using telemetry extracted from the MP4 file or a companion `.SRT` sidecar. It shows a zero-config estimated camera ground footprint by default using `rel_alt` and a flat-ground assumption, and can optionally upgrade that footprint plus a visibility heatmap using a Digital Elevation Model when the loaded flight actually overlaps the DEM coverage.

Built for DJI drones including the **Matrice 4E**, **Matrice 300 + Zenmuse**, and **Mavic 3 Thermal (M3T)**. Supports three telemetry pathways: embedded `mov_text` subtitle tracks, binary `djmd` protobuf tracks, and companion `.SRT` sidecar files.

## Quick start — viewer only

1. Open `index.html` in a modern browser (Chrome, Firefox, Safari, Edge) on any OS.
2. Drop files onto the drop zone overlaid on the video area (or click to browse):
   - **MP4 only** — telemetry is extracted from the embedded subtitle or djmd track
   - **MP4 + SRT together** — video plays, telemetry is parsed from the `.SRT` sidecar
   - **SRT only** — telemetry and map without video (useful for drones like the M3T whose MP4s have no embedded telemetry track)
3. The app parses telemetry, renders the flight path on the map, and begins playback automatically.

That's it for the core viewer — drop files in, everything else happens in the browser, no server required. The map will show an estimated camera footprint immediately, clearly labelled as a flat-ground estimate derived from relative altitude.

> **Tip:** The telemetry HUD and map are below the video. If they're not visible, a "Map & telemetry readout are below" indicator appears in the video area — scroll down to see them.

## Quick start — with DEM features (terrain-aware footprint + heatmap)

The terrain-aware footprint polygon and visibility heatmap layers need to `fetch()` local DEM data files, which the browser won't allow from a `file://` page. Run a local HTTP server from the project root:

```bash
cd drone_video_telemetry
python3 -m http.server 8000
# then open http://localhost:8000
```

DEM data is organised by area — one location with any number of flights, each in `dem/areas/<area>/`. When a flight loads, the viewer reads `dem/areas/index.json`, picks the area whose DEM covers the most of the flight, and loads that area's shared DEM (`dem/areas/<area>/flight-dem.{bin,json}`). It only uses terrain-aware ray-casting while the current flight position is inside DEM coverage. The heatmap is per-flight and keyed by MP4 basename (`dem/areas/<area>/visibility/<basename>.{bin,json}`). See [DEM preprocessing](#dem-preprocessing) below for generating these.

## Responsive layout

The viewer adapts its layout based on window aspect ratio:

- **Stacked mode** (window taller than 1.2× its width) — video on top, HUD strip in the middle, map below. A vertical drag handle between the HUD and map lets you resize the split.
- **Side-by-side mode** (window wider than 1.2× its height) — video + controls + HUD on the left, map on the right, with a horizontal drag handle to resize the split (default 50/50). The compass tape is hidden to keep the HUD compact.

In side-by-side mode, a **video fit toggle** appears top-left on the video with two states:
- **Fill height** (default) — video fills the vertical space, cropping the sides
- **Fill width** — video fits within the panel width, with letterboxing above/below

## Keyboard controls

| Key | Action |
|-----|--------|
| Space | Play / pause toggle |
| Left arrow | Seek back 10 seconds |
| Right arrow | Seek forward 10 seconds |

## What it shows

### HUD strip (between video and map)

Laid out left-to-right in a 3-column grid (2:1:2 proportions):

| Left group | Centre | Right group |
|---|---|---|
| Wall-clock time (date + HH:MM:SS) | Sliding compass tape (N, NE, E...) | Horizontal speed (m/s) |
| Gimbal pitch (animated V-shape in circle) | | Vertical speed (m/s) |
| Aircraft heading (animated arrow in compass) | | Relative altitude (m above takeoff) |
| | | Absolute altitude (m AMSL) |

The heading and pitch instruments have animated canvas graphics with wireframe indicators that rotate in real time.

### Map

- **Satellite imagery** from Esri World Imagery (free, no API key)
- **Full flight path** shown as a semi-transparent polyline
- **Travelled portion** highlighted in orange, growing as the video plays
- **Drone marker** -- arrow/chevron SVG rotated to match aircraft heading
- **Auto-follow** -- map centres on the drone during playback
- **Manual pan** -- dragging the map disengages auto-follow; a "Re-lock to drone" button appears to resume

### Footprint and DEM overlays

A small control panel appears on the map after telemetry loads:

- **Estimated footprint (default)** — semi-transparent orange polygon on the map showing what the camera sees on the ground, updated every telemetry tick. When no DEM is available, it uses `rel_alt` as camera height above takeoff level and intersects the camera rays with a flat ground plane. The UI labels this explicitly as an estimate based on the flat-ground assumption, and draws it dashed to distinguish it from the terrain-aware mode.
- **Terrain-aware footprint (optional)** — when an area in `dem/areas/index.json` covers the current flight and its `flight-dem.{bin,json}` loads successfully, the same overlay switches to DEM ray-casting against real terrain. If no area covers the movie, or it leaves the DEM mid-flight, the viewer stays on or falls back to the flat-ground estimate and explains why in the UI.
- **Visibility dot heatmap** — per-flight precomputed overlay showing how well each ground area was observed across the whole flight, weighted by inverse-squared slant distance and rendered as a hexagonal dot lattice. Toggleable, with opacity and palette swatch controls, and available only when the per-flight DEM-derived visibility files can be fetched.
- **Calibrate camera…** button — pauses the video and opens a calibration panel for correcting the camera model against reality. Click up to four features in the video that you can also find on the map (a house, a fence corner, a lone tree); each gets a numbered pin on the video and a matching pin on the map where the model says that point is on the ground. Adjust **Pitch offset**, **Heading offset** and **FOV scale** until the map pins sit on their features — the footprint redraws live. Pins clear when the video plays or seeks. Flat ground with features near the top of the frame gives the most sensitive pitch estimate, and checking a steep and a shallow frame separates a pitch offset from an FOV error. Values are remembered in the browser; the panel shows them as JSON to save as `dem/camera-calibration.json`, which the viewer loads by default and `process_flights.py` applies to the heatmaps.
- **Export Telemetry .JSON** button — dumps the parsed telemetry array as `<basename>-telemetry.json` for feeding into the Python visibility pipeline.

### Loading

- Drop zone overlays the entire video area before a file is loaded; it disappears once a file is selected
- The telemetry HUD strip is visible from the start (showing `--` placeholder values) so the layout is clear before any video is loaded
- A "Map & telemetry readout are below" indicator appears in the video area whenever the map panel is scrolled out of view — before and after load — and hides automatically once the map is visible
- Orange progress bar fills the drop zone background during file ingestion
- Status messages show read progress percentage
- Video starts playing automatically once telemetry extraction completes

## How it works

### Telemetry extraction — three pathways

The viewer supports three telemetry sources, auto-detected based on what files are dropped:

**Path 1 — Embedded `mov_text` subtitle track (e.g. Matrice 4E)**

DJI M4E MP4 files contain telemetry as a `mov_text` subtitle track (typically stream index 3). Each subtitle sample covers one video frame (~33 ms at 29.97 fps) and contains a text block like:

```
FrameCnt: 0 2026-03-30 11:53:23.539
[iso: 240] [shutter: 1/2500.0] [fnum: 2.8] [ev: 0] [color_md: default]
[ae_meter_md: 1] [focal_len: 24.00] [dzoom_ratio: 1.00],
[latitude: -42.137698] [longitude: 147.638924]
[rel_alt: 0.000 abs_alt: 541.387]
[gb_yaw: -68.3 gb_pitch: 0.0 gb_roll: 0.0]
```

Note: some fields share brackets (e.g. `rel_alt` and `abs_alt` are inside one `[]` pair, as are `gb_yaw`, `gb_pitch`, and `gb_roll`). The parser handles this by matching `key: value` pairs individually rather than one-per-bracket.

The app uses [mp4box.js](https://github.com/nicomikaelson/nicomikaelson.github.io) (loaded from CDN) to demux the MP4 container in the browser. It reads the file in 64 MB chunks via `FileReader`, finds the subtitle track, and extracts all samples. Each sample's binary payload is `mov_text` format: 2-byte big-endian length prefix followed by UTF-8 text.

**Path 2 — Binary `djmd` protobuf track (e.g. Matrice 300 + Zenmuse thermal)**

When the MP4 contains a `djmd` codec track instead of a text subtitle track, the viewer decodes the binary protobuf messages to extract GPS position, altitude, gimbal angles, and velocity. In this path the lat/lon represents the camera ground-target point rather than the drone position.

**Path 3 — Companion `.SRT` sidecar file (e.g. Mavic 3 Thermal / M3T)**

Some DJI drones (notably the M3T) write telemetry to a `.SRT` file alongside the `.MP4` rather than embedding it. Each SRT entry is a standard subtitle block with DJI telemetry in `<font>` tags:

```
1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-04-29 18:07:13.766
[focal_len: 40.00] [dzoom_ratio: 1.00], [latitude: -38.302142] [longitude: 145.093433] [rel_alt: 53.527 abs_alt: 140.924] [gb_yaw: 20.4 gb_pitch: -23.6 gb_roll: 0.0] </font>
```

Drop the `.MP4` and `.SRT` together to get video + telemetry, or drop a `.SRT` alone for map-only telemetry playback. Fields are extracted by the same regex parser as Path 1.

### Playback sync

Telemetry is parsed into a flat array sorted by time (seconds from start):

```js
[{ t, lat, lon, relAlt, absAlt, yaw, pitch, iso, shutter, fnum, focalLen, timestamp }, ...]
```

On every `timeupdate` and `seeked` event from the `<video>` element, the app binary-searches for the two telemetry entries bracketing the current time, then linearly interpolates position, altitude, and angles between them. Yaw interpolation wraps correctly across the 0/360 boundary.

### Speed calculation

Vertical and horizontal speeds are derived from telemetry deltas, sampled every ~0.3 seconds to smooth out noise. Horizontal distance uses the haversine formula. Both reset to zero on seek.

### Heading note

The M4E gimbal does not independently rotate on the yaw axis during manual flight, so `gb_yaw` reliably represents aircraft heading. The app uses it directly to rotate the drone marker -- no need to derive heading from positional deltas.

### Camera footprint

The footprint polygon is computed every frame inside the browser:

1. **Build camera basis** — from the interpolated `yaw`/`pitch` telemetry, derive the look, right, and up vectors in local East-North-Up coordinates.
2. **Generate edge rays** — sample 24 points around the image-plane rectangle (6 per edge + corners), each mapped to a ray direction using the horizontal/vertical FOV. FOV itself is derived from the 35mm-equivalent `focal_len` reported in telemetry, which DJI quotes for the full 4:3 sensor (84° diagonal at 24 mm). Video keeps the full sensor width and crops the top and bottom, so the vertical FOV comes from the loaded video's own aspect ratio (3840 × 2160 → 71.5° × 44.1°), defaulting to 16:9 when only an SRT is loaded. `compute_visibility.py` does the same, with `process_flights.py` passing each MP4's frame size.
3. **Zero-config fallback** — if no DEM is loaded, intersect each downward-pointing ray with a flat ground plane at `z = 0` in takeoff-relative coordinates, using `rel_alt` as the camera height above that plane. Rays near or above the horizon are projected out to 50 km so the polygon still reaches "effectively infinite" range.
4. **DEM upgrade** — if a DEM is loaded, switch the same ray set to a coarse-to-fine terrain march: step along each ray at 20 m intervals up to 20 km, compare the ray altitude to the bilinearly interpolated DEM elevation, then refine the first hit with 2 m steps. Out-of-DEM lookups return `NaN` and terminate the march for that ray. Camera height comes from `abs_alt` shifted onto the DEM's datum: at takeoff `rel_alt` is 0, so `abs_alt` should equal the DEM at that point, and DJI's barometric datum is often several metres out (up to +6 m, or 10% of flying height, across these flights). Without the shift every ray reaches proportionally too far. `compute_visibility.py` applies the same correction (`--alt-datum absolute` to disable).
5. **Handle unbounded rays** — a ray that leaves DEM coverage, or stays above terrain for the full 20 km march, stops at the last point where the DEM could still be sampled, so the footprint is bounded by real coverage. (Without this, a single ray grazing a ridge and flying on past the DEM edge stretched the drawn polygon hundreds of kilometres.) Only rays that never reach the DEM at all — and every ray in flat-ground mode that points at or above the horizon — are projected 50 km along their horizontal bearing, giving the estimated footprint an "infinite top" when the camera is near-horizontal.

This runs sub-millisecond per frame. The DEM itself is loaded once at startup as a `Float32Array` with bilinear-interpolated lookups.

### Visibility heatmap

Precomputed offline by `dem/compute_visibility.py` (see below), then rendered in the browser as a hexagonal dot lattice inside a Leaflet `L.ImageOverlay`. Zero-score ground stays transparent with no dot. Non-zero cells are sampled into staggered hex positions, with dot area and colour log-scaled against a robust high-percentile cap to compress the huge dynamic range of 1/d² scores. The map control offers two redraw-only palette swatches: **Lava** for high-contrast satellite viewing, and **Viridis** for a familiar perceptual scientific ramp. Both use the top quarter of their colour range so low-score dots stay readable over satellite imagery.

## Architecture

The viewer is a single HTML file (~2200 lines) with no build step. It loads three CDN dependencies:

| Library | Version | Purpose |
|---|---|---|
| [Leaflet](https://leafletjs.com/) | 1.9.4 | Map rendering and interaction |
| [Esri World Imagery](https://server.arcgisonline.com/) | -- | Satellite tile layer (free, no key) |
| [mp4box.js](https://github.com/nicomikaelson/nicomikaelson.github.io) | 0.5.2 | Client-side MP4 demuxing |

### Code structure (single `<script>` block)

1. **State & map init** -- Leaflet map with Esri tiles, drone marker, follow/relock logic
2. **HUD drawing** -- Canvas-based heading compass, pitch indicator, and sliding compass tape
3. **Telemetry parsers** -- Regex-based field extraction from subtitle/SRT text (Paths 1 & 3), protobuf decoder for djmd binary (Path 2)
4. **MP4 extraction** -- mp4box.js integration with chunked reading and progress reporting
4b. **SRT loading** -- Direct text parsing of companion `.SRT` files, with optional paired MP4 for video
5. **Interpolation** -- Binary search + lerp with angle wrapping for yaw
6. **Speed calculation** -- Haversine horizontal speed + vertical speed from altitude deltas
7. **Update loop** -- Wired to video `timeupdate`/`seeked`, updates marker, path, HUD, and map position
8. **Keyboard controls** -- Space for play/pause, arrow keys for ±10s seek
9. **File loading** -- Drop zone with drag/drop and click-to-browse, supports MP4, SRT, or both together; progress bar, autoplay
10. **Footprint mode UI** -- clear map labeling for flat-ground estimated vs DEM terrain-aware footprint modes
11. **Layout management** -- aspect-ratio-driven stacked/side-by-side switching, draggable split handles, video fit toggle

### Design decisions

- **Core viewer runs from `file://`** — MP4 loading, telemetry parsing, HUD, map, and the flat-ground estimated footprint all work with no server, because the MP4 comes in via `<input type=file>` (blob URL, not `fetch`) and map tiles come from HTTPS. Only the DEM-enhanced footprint and heatmap need `fetch()` for local files and therefore a local HTTP server.
- **Per-area DEM, per-flight visibility** — one `flight-dem.{bin,json}` per area covers every flight at that location (loaded once, and only reloaded when a flight from a different area is opened); per-MP4 `visibility/<basename>.{bin,json}` is fetched fresh each time a new MP4 is loaded. The area is chosen from `dem/areas/index.json` by how many telemetry positions fall inside each area's DEM bounds. DEM resolution is fully data-driven: the JSON metadata carries `pixel_size_lon`/`pixel_size_lat` and the viewer adapts.
- **Frame-accurate map updates** — the map, HUD and footprint update from `requestVideoFrameCallback` (the exact presentation time of each displayed frame), falling back to `timeupdate` where that isn't supported. `timeupdate` alone fires only ~4 times a second, which leaves the map up to 250 ms behind the video — several degrees of heading during a pan, and enough to throw the footprint off by tens of metres at range. The telemetry itself is frame-aligned: measured against image motion during hover-and-yaw pans, it matches the picture to within 20 ms.
- **Chunked file reading** — 64 MB chunks avoid allocating multi-gigabyte ArrayBuffers for large drone videos.
- **No animation on map follow** — `map.setView()` with `animate: false` prevents tile flicker during continuous tracking.
- **Fixed-width HUD values** — `min-width` on value elements prevents layout reflow when numbers change width.
- **Orange highlight theme** — travelled path, drone marker, compass pointer, footprint polygon, and instrument graphics all use `#ffa028`.

## Extracting telemetry manually

If you need the raw SRT file for other purposes:

```bash
ffmpeg -i input.MP4 -map 0:3 -f srt telemetry.srt
```

## DEM preprocessing

DEM data is organised by **area**: one location with any number of flights. A video belongs to an area when it sits in a folder named after the area anywhere under `mp4/` — e.g. `mp4/matrice-4E-mp4/marathon/2026-02-18-elkington.MP4` belongs to `marathon` — and each area keeps its data in `dem/areas/<area>/`. Videos outside an area folder still play, with the flat-ground footprint.

The scripts in `dem/` generate the data files the viewer fetches at runtime. They need `numpy`, `rasterio`, the GDAL Python bindings (`osgeo`), `shapely` and `pyproj`, plus `ffmpeg` on PATH (`brew install ffmpeg` on macOS).

See [`dem/PLAN.md`](dem/PLAN.md) for the original design, and [`dem/area_dem.py`](dem/area_dem.py), [`dem/process_flights.py`](dem/process_flights.py), [`dem/preprocess_dem.py`](dem/preprocess_dem.py) and [`dem/compute_visibility.py`](dem/compute_visibility.py) for the implementations.

### 1. Define the area extent — once per area

```bash
python3 dem/area_dem.py extent marathon --buffer 1000 --boundary ~/Desktop/greater_marathon.kml
```

This extracts telemetry for every MP4 in the area's folders (if not already done), merges each flight's track with any boundary polygons you pass (KML, GeoJSON, GPKG or shapefile — e.g. property parcels), buffers the result by `--buffer` metres and writes `dem/areas/<area>/extent.geojson` (EPSG:4326). It also reports whether each flight lies fully inside. Pass `--force` to replace an existing extent.

Choose the buffer with oblique footage in mind: for the Elkington flight (camera mostly 20–30° below horizontal, up to 120 m up), 90% of ground hits were within 740 m, 95% within 1.2 km and 99% within 3.3 km.

### 2. Order DEM tiles from ELVIS

```bash
python3 dem/area_dem.py tiles marathon
```

Lists what [ELVIS](https://elevation.fsdf.org.au/) holds for the extent — per source, data type, resolution and survey, with tile counts and sizes — and saves the full listing to `dem/areas/<area>/elvis-tiles.json`. The tile files can't be downloaded directly, so order them through the portal:

1. Open https://elevation.fsdf.org.au/ and upload `dem/areas/<area>/extent.geojson` as the area.
2. Tick the **Digital Elevation Models → 1 Metre** surveys (the point clouds are much larger and not needed), choose GeoTIFF output (any CRS), enter your email and submit.
3. Put the emailed zip(s) in `dem/areas/<area>/elvis/`. Zipped or already-unzipped tiles both work.

### 3. Build the area DEM

```bash
python3 dem/area_dem.py ingest marathon
```

This unzips the download and mosaics every GeoTIFF tile into `dem/areas/<area>/area-dem.tif` (EPSG:4326) over the extent's bounding box, then clips the browser DEM `flight-dem.bin` (raw row-major `Float32Array`) plus its `flight-dem.json` metadata via `preprocess_dem.py`, and rebuilds `dem/areas/index.json`. Mixed GDA94 / GDA2020 tiles are reprojected, and where surveys overlap the newest one wins. ELVIS only supplies tiles that touch the extent polygon, so the rest of the bounding box is filled from the public Copernicus GLO-30 30 m model (a surface model, so it includes tree canopy).

| Flag | Purpose |
|---|---|
| `--resolution` | `area-dem.tif` cell size in metres (default 2) |
| `--flight-resolution` | `flight-dem` cell size in metres (default 5) — 2 m → 5 m shrinks the browser file ~6× with imperceptible quality loss for footprint polygons |
| `--fill` | `copernicus` (default) or `none` — with `none`, gaps become the DEM's minimum elevation |

**Sizing:** Marathon's 12 × 9.6 km extent is 76 one-metre tiles (a 236 MB zip), giving a 38 MB `area-dem.tif` and a 15 MB `flight-dem.bin`. The viewer loads `flight-dem.bin` once per area per session, so keep it to a reasonable download.

Related commands: `python3 dem/area_dem.py clip <area>` re-clips `flight-dem` from an existing `area-dem.tif` (e.g. at a different `--flight-resolution`), and `python3 dem/area_dem.py index` rebuilds `dem/areas/index.json` if you add or remove an area folder by hand.

### 4. Compute per-flight visibility heatmaps — once per MP4

`compute_visibility.py` ray-casts from every sampled telemetry frame (default 1 Hz) through a 50 × 40 grid of rays across the camera FOV, accumulating `1/d²` scores wherever rays intersect the DEM. Rays pointing above horizontal are cast too — on sloping ground they hit real terrain, and dropping them left the top of the frame unscored. Hits closer than `--min-range` (default 5 m) score as if at that distance, so ground a couple of metres below a low-flying drone doesn't swamp the grid. Output goes to `dem/areas/<area>/visibility/<basename>.{bin,json}`.

**Batch mode (recommended) —** `process_flights.py` finds every MP4 under `mp4/` that sits in an area folder, extracts its telemetry via ffmpeg to `dem/areas/<area>/telemetry/<basename>-telemetry.json`, then runs `compute_visibility.py` against the area's `area-dem.tif`. Idempotent — skips any file whose outputs already exist. Flights in an area without an `area-dem.tif` yet get telemetry only.

```bash
python3 dem/process_flights.py                   # every area, whatever's missing
python3 dem/process_flights.py --area marathon   # one area
python3 dem/process_flights.py --force           # reprocess (e.g. after a new DEM)
```

No browser round-trip needed.

**Camera calibration —** if `dem/camera-calibration.json` exists (saved from the viewer's calibration panel), `process_flights.py` passes its `pitch_offset_deg`, `yaw_offset_deg` and `fov_scale` to `compute_visibility.py` (`--pitch-offset`, `--yaw-offset`, `--fov-scale`). Each heatmap records the calibration it was made with, and `process_flights.py` recomputes any heatmap whose calibration differs from the file — no `--force` needed.

```json
{
  "pitch_offset_deg": -4.5,
  "yaw_offset_deg": 0,
  "fov_scale": 1
}
```

**Manual single-MP4 —** if you've already got a telemetry JSON (e.g. from the viewer's Export button), you can run `compute_visibility.py` directly:

```bash
python3 dem/compute_visibility.py \
  --telemetry dem/areas/quoin/telemetry/2026-03-30-foo-telemetry.json \
  --dem dem/areas/quoin/area-dem.tif \
  --output-dir dem/areas/quoin
```

The output basename is auto-derived by stripping `-telemetry` from the input filename; override with `--name` if needed.

Processing is numpy-vectorised with a coarse-to-fine ray march; a typical 7-minute flight takes 3-5 seconds on a laptop. Every parameter (sample rate, ray grid density, step sizes, score exponent, output cell size, sensor geometry) is a CLI flag — see `--help`.

### 5. Export sitewide coverage for QGIS — whenever you want a shareable layer

`export_coverage.py` turns the per-flight heatmaps into a vector layer. Each video becomes a **nested stack of polygons, one per viewing-distance level**, carrying the flight's date, time and stats as attributes. Load it in QGIS beside your other layers to show colleagues where we have drone vision, from when, and how good a look we got.

```bash
python3 dem/export_coverage.py --area marathon   # -> dem/exports/marathon_drone_video_coverage_<today>.gpkg
python3 dem/export_coverage.py                   # every area with heatmaps
python3 dem/export_coverage.py --area marathon --output ~/Desktop/marathon.geojson
```

Output is EPSG:4326 MultiPolygon, datestamped with the day it was made so a copy sent to someone says how current it is. GeoPackage is the default: it comes out about half the size of GeoJSON and keeps `date` and `flight_start` as real date types. `--output` with another extension picks another format — `.geojson` is written directly with tidied coordinate precision; anything else OGR knows (`.shp`, `.fgb`) goes through geopandas.

**Levels —** heatmap scores accumulate `1/d²` per sampled second, so a cell seen for S seconds from d metres scores `S/d²`. That makes each level readable as *"seen for at least `--min-seconds` from within `<level>` metres"*. Shorter distance means better ground detail, so the levels nest — the closest is the smallest and sits inside all the others. The default halves at each step, which puts a clean 4× jump in threshold between bands:

| Level | Roughly |
|---|---|
| 25 m | as close as the camera gets at normal flight height — near-nadir, right under the track |
| 50 m | close enough to pick out an individual plant |
| 100 m | good working detail |
| 200 m | recognisable structure — tracks, canopy gaps, erosion |
| 400 m | distant and oblique — context only |

```bash
python3 dem/export_coverage.py --area marathon --levels 100,200,300,500  # retune the ramp
python3 dem/export_coverage.py --area marathon --levels 300              # one feature per video
```

Flights sit around 50 m AGL, so the inner levels trace the flight lines and the outer bands fan out around them. They also open up the blind spot directly beneath the aircraft, where the camera looked ahead rather than straight down — the holes in the innermost polygons are real, not artefacts.

**Styling the stack in QGIS —** because the levels nest, a *single* semi-transparent fill does the work: set the layer to one fill colour at ~25 % opacity with no stroke, and the four overlapping polygons shade themselves — palest where a flight only glimpsed the ground from 500 m, darkest over what it saw from within 100 m. For crisper bands instead, categorise on `within_m` with an opaque ramp, or load the file four times filtered to one level each if you want separate layer entries with independent opacity.

Cells above a level's threshold are polygonised at the heatmap's 10 m grid, then cleaned: `--min-patch-m2` (default 2000 m²) drops coverage specks and pinholes, and `--simplify-m` (default 5 m) trims vertices — raise it for smoother outlines, set 0 to keep exact cell edges.

**Attributes —** `area`, `video`, `date`, `year`, `flight_start`, `flight_end`, `duration_min`, `alt_agl_max_m`, `alt_agl_mean_m`, `seconds_sampled`, `mp4` (the path back to the source video), plus per level: `level` (1 = closest), `levels`, `within_m`, `min_seconds`, `score_threshold`, `cell_size_m` and `coverage_ha`.

Dates come from the telemetry's own timestamps, not the filename; a mismatch between the two is reported as it exports. Features are written newest flight first, and widest level first within a flight so the closer levels draw on top. Flights overlap each other as well, so categorise on `year` or `date` to show when each patch was last flown.

### 6. Places — named areas of ground

A place is a named area of ground — a dam, a cliff line, a monitoring site — kept per area in `dem/areas/<area>/places.geojson`. Places are the vocabulary for describing where drone footage is of.

**Seed them from the drone archive.** Pilots already name missions on the controller (`DJI_202510271228_006_Quoin_cliffs`), so `seed_places.py` turns every named flight folder in the archive into a seed polygon:

```bash
python3 dem/seed_places.py quoin --media "/path/to/Dropbox/.../quoin/drones/drone" \
    --paddocks "/path/to/.../quoin_paddocks.gpkg"
```

- **Photo missions** are outlined from where the photos were aimed — each DJI photo records its laser rangefinder target (`LRFTargetLat`/`LRFTargetLon`) — so these are usually close already.
- **Video-only missions** get the ground the camera saw from within 50 m, from the flight's visibility heatmap: a rough area around the subject rather than its outline.

Mission names map to place names through an optional `dem/areas/<area>/place-names.json` (regular expressions → names, plus names to ignore); unmatched mission names become places of their own, and DJI's generic route names and test flights are skipped. Re-running only adds seeds whose name and source folders aren't in `places.geojson` already, so places you've renamed or redrawn are kept; `--replace` starts over.

Telemetry and heatmaps for archive videos are cached in `dem/areas/<area>/sources/`, keyed by the video's own filename.

**Check and edit them in `places.html`.** Serve the project root and open `http://localhost:5002/places.html?area=quoin`: satellite imagery with paddocks, every flight track (hover for the video), and the places. Select a place to drag its vertices or move it, rename it, add notes, and mark seeds *confirmed*; draw new ones with **+ Polygon** or **+ Circle**. **Save** writes `places.geojson` — in Chrome or Edge pick that file once and later saves go straight to it; other browsers download a copy. Unsaved edits are kept in the browser until you save.

### File layout after preprocessing

```
drone_video_telemetry/
  index.html                        # The viewer
  places.html                       # Places editor
  mp4/
    matrice-4E-mp4/
      marathon/*.MP4                # Folder name = area name
      quoin/*.MP4
  dem/
    area_dem.py                     # Area extent, ELVIS tiles, mosaic, clip, index
    process_flights.py              # Batch orchestrator (ffmpeg + compute_visibility)
    preprocess_dem.py               # Clip area-dem.tif to flight-dem
    compute_visibility.py
    export_coverage.py              # Per-flight coverage polygons for QGIS
    seed_places.py                  # Seed places.geojson from named missions in the drone archive
    media_sources.py                # Archive videos: telemetry/heatmap cache, tracks
    PLAN.md                         # Original design notes
    exports/
      <area>_drone_video_coverage_<date>.gpkg      # Coverage polygons for QGIS
    areas/
      index.json                    # Area names + DEM bounds (read by the viewer)
      marathon/
        extent.geojson              # Area polygon (upload to ELVIS)
        elvis-tiles.json            # ELVIS tile listing for the extent
        elvis/                      # Downloaded ELVIS zips / tiles
        area-dem.tif                # Mosaicked source DEM (not loaded by browser)
        flight-dem.bin / .json      # Shared clipped DEM for the viewer
        telemetry/
          <basename>-telemetry.json # Extracted from MP4, or Exported from viewer
        visibility/
          <basename>.bin / .json    # Per-flight visibility heatmap
        places.geojson              # Named places (edit in places.html)
        place-names.json            # Mission name -> place name mapping for seeding
        paddocks.geojson            # Context layer for places.html
        tracks.geojson              # Flight tracks for places.html
        sources/                    # Telemetry + heatmaps of archive videos, by source filename
```

## Browser compatibility

Works on macOS, Windows, and Linux. Tested in Chrome and Safari; should work in any modern browser (Chrome, Edge, Firefox, Safari). Requires:
- `FileReader` API
- `<video>` element with MP4 support — H.264 works everywhere; H.265/HEVC requires hardware decode support (available in Chrome and Edge on most modern hardware)
- ES2017+ (async/await, `**` operator)
- Canvas 2D

## Files

| File | Description |
|---|---|
| `index.html` | The viewer (single HTML file, no build step) |
| `README.md` | This file |
| `mp4/*.MP4` | DJI video files (Matrice 4E, Matrice 300, Mavic 3T, etc.) |
| `mp4/**/*.SRT` | Companion SRT telemetry sidecar files (e.g. from M3T) |
| `dem/area_dem.py` | Per-area extent, ELVIS tile listing, mosaic + clip, area index |
| `dem/preprocess_dem.py` | Clip + optionally resample an area DEM for browser use |
| `dem/compute_visibility.py` | Ray-cast visibility heatmap from telemetry + DEM |
| `dem/process_flights.py` | Batch-process every area MP4 under `mp4/` through telemetry + visibility (needs ffmpeg) |
| `dem/export_coverage.py` | Export per-flight coverage polygons as a GIS layer for QGIS |
| `dem/seed_places.py` | Seed an area's places from named missions in the drone archive |
| `dem/media_sources.py` | Shared helpers for archive videos: telemetry and heatmap cache, flight tracks |
| `places.html` | Map editor for an area's places |
| `dem/PLAN.md` | Original design notes for the DEM features |
| `dem/camera-calibration.json` | Camera pitch / heading / FOV corrections saved from the viewer's calibration panel (optional) |
| `dem/areas/index.json` | Area names and DEM bounds for the viewer (generated) |
| `dem/areas/<area>/extent.geojson` | Area polygon used to order DEM tiles (generated) |
| `dem/areas/<area>/area-dem.tif` | Mosaicked source DEM (generated from ELVIS tiles, not tracked) |
| `dem/areas/<area>/flight-dem.{bin,json}` | Shared clipped DEM for the area (generated) |
| `dem/areas/<area>/telemetry/<basename>-telemetry.json` | Per-flight telemetry (extracted, or exported from viewer) |
| `dem/areas/<area>/visibility/<basename>.{bin,json}` | Per-flight visibility heatmap (generated) |
| `dem/exports/<area>_drone_video_coverage_<date>.gpkg` | Per-flight coverage polygons for QGIS (generated) |
