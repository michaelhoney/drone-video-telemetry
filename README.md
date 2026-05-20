# DJI Telemetry Map Viewer v0.5

Current version: `v0.5`

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

The terrain-aware footprint uses a shared area-wide DEM (`dem/flight-dem.{bin,json}`). The viewer now checks the flight telemetry against the DEM bounds and only uses terrain-aware ray-casting when the current flight position is inside DEM coverage. The heatmap is per-flight and keyed by MP4 basename (`dem/visibility/<basename>.{bin,json}`). See [DEM preprocessing](#dem-preprocessing) below for generating these.

## Roadmap
- v0.6:
  - side-by-side view: when window is wider than it is tall, put video + HUD on left, map on right. Vertical split is initially 50/50 but can be dragged left and right. Video is cropped at sides by default (fill height of space) but there is a toggle top-left to toggle between fill-height and fill-width 


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
- **Terrain-aware footprint (optional)** — when `dem/flight-dem.{bin,json}` loads successfully and the current flight overlaps that DEM, the same overlay switches to DEM ray-casting against real terrain. If the movie is outside the DEM extent, or leaves it mid-flight, the viewer stays on or falls back to the flat-ground estimate and explains why in the UI.
- **Visibility heatmap** — per-flight precomputed overlay showing how well each ground cell was observed across the whole flight, weighted by inverse-squared slant distance. Toggleable, with an opacity slider, and available only when the per-flight DEM-derived visibility files can be fetched.
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
2. **Generate edge rays** — sample 24 points around the image-plane rectangle (6 per edge + corners), each mapped to a ray direction using the horizontal/vertical FOV. FOV itself is derived from the 35mm-equivalent `focal_len` reported in telemetry plus the M4E's 1/1.3" sensor geometry (9.6 × 7.2 mm).
3. **Zero-config fallback** — if no DEM is loaded, intersect each downward-pointing ray with a flat ground plane at `z = 0` in takeoff-relative coordinates, using `rel_alt` as the camera height above that plane. Rays near or above the horizon are projected out to 50 km so the polygon still reaches "effectively infinite" range.
4. **DEM upgrade** — if a DEM is loaded, switch the same ray set to a coarse-to-fine terrain march: step along each ray at 20 m intervals up to 20 km, compare the ray altitude to the bilinearly interpolated DEM elevation, then refine the first hit with 2 m steps. Out-of-DEM lookups return `NaN` and terminate the march for that ray.
5. **Handle unbounded rays** — any ray that never hits terrain (looking above horizon, never descending enough, or exiting DEM coverage) is projected 50 km along its horizontal bearing. This gives the polygon an "infinite top" when the camera is near-horizontal, so the drawn polygon reaches effectively to the horizon instead of truncating.

This runs sub-millisecond per frame. The DEM itself is loaded once at startup as a `Float32Array` with bilinear-interpolated lookups.

### Visibility heatmap

Precomputed offline by `dem/compute_visibility.py` (see below), then rendered in the browser as a colour-ramped canvas inside a Leaflet `L.ImageOverlay`. The ramp is log-scaled to compress the huge dynamic range of 1/d² scores: transparent for zero, purple for barely-seen ground, through orange to bright yellow/white for well-observed ground.

## Architecture

The viewer is a single HTML file (~1100 lines) with no build step. It loads three CDN dependencies:

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

### Design decisions

- **Core viewer runs from `file://`** — MP4 loading, telemetry parsing, HUD, map, and the flat-ground estimated footprint all work with no server, because the MP4 comes in via `<input type=file>` (blob URL, not `fetch`) and map tiles come from HTTPS. Only the DEM-enhanced footprint and heatmap need `fetch()` for local files and therefore a local HTTP server.
- **Shared DEM, per-flight visibility** — one `flight-dem.{bin,json}` covers every flight in the area (loaded once); per-MP4 `visibility/<basename>.{bin,json}` is fetched fresh each time a new MP4 is loaded. DEM resolution is fully data-driven: the JSON metadata carries `pixel_size_lon`/`pixel_size_lat` and the viewer adapts.
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

The two Python scripts in `dem/` generate the data files the viewer fetches at runtime. Requires `rasterio` and `numpy`. The source DEM (`dem/area-dem.tif`, not tracked here — a ~292 MB LiDAR-derived surface model at 2 m resolution, EPSG:4326) is the input to both.

See [`dem/PLAN.md`](dem/PLAN.md) for the full design and [`dem/preprocess_dem.py`](dem/preprocess_dem.py) / [`dem/compute_visibility.py`](dem/compute_visibility.py) for the implementations.

### 1. Clip the shared DEM — once per project area

`preprocess_dem.py` clips the large source DEM to a browser-loadable `flight-dem.bin` (raw row-major `Float32Array`) plus a `flight-dem.json` metadata sidecar. Clip once to cover **every** flight in the area you care about — the viewer reuses this file for all MP4s.

```bash
# Explicit bounds (south,west,north,east) — note the '=' to keep argparse
# from interpreting the leading '-' as another flag.
python3 dem/preprocess_dem.py \
  --bounds="-42.15242,147.62674,-42.12897,147.64287" \
  --buffer 2000 \
  --resolution 5
```

Key options:

| Flag | Purpose |
|---|---|
| `--bounds` | `south,west,north,east` in decimal degrees |
| `--telemetry` | Alternative to `--bounds`: use an exported telemetry JSON's extent |
| `--buffer` | Metres to pad in every direction (default 1000) — needed because oblique cameras see well past the flight path |
| `--resolution` | Resample to this cell size in metres. Omit to keep source DEM resolution. Dropping from 2 m to 5 m shrinks the output ~6× with imperceptible quality loss for footprint polygons |

**Sizing:** at the native 2 m resolution, a 12 × 11 km clip is ~100 MB. At 5 m resolution the same clip is ~6 MB. The viewer loads this file once per session — pick the largest practical area and a resolution that keeps the download reasonable.

### 2. Compute per-flight visibility heatmap — once per MP4

`compute_visibility.py` ray-casts from every sampled telemetry frame (default 1 Hz) through a 50 × 40 grid of rays across the camera FOV, accumulating `1/d²` scores wherever rays intersect the DEM. Output goes to `dem/visibility/<basename>.{bin,json}`.

**Batch mode (recommended) —** `process_flights.py` handles the whole pipeline for every MP4 in `mp4/`: extracts telemetry directly from each MP4 via ffmpeg, writes it to `dem/telemetry/<basename>-telemetry.json`, then invokes `compute_visibility.py` to produce the visibility files. Idempotent — skips any file whose outputs already exist.

```bash
python3 dem/process_flights.py            # process everything that's missing
python3 dem/process_flights.py --force    # reprocess everything
```

Requires `ffmpeg` on PATH (`brew install ffmpeg` on macOS). No browser round-trip needed.

**Manual single-MP4 —** if you've already got a telemetry JSON (e.g. from the viewer's Export button), you can run `compute_visibility.py` directly:

```bash
python3 dem/compute_visibility.py --telemetry dem/telemetry/2026-03-30-foo-telemetry.json
```

The output basename is auto-derived by stripping `-telemetry` from the input filename; override with `--name` if needed.

Processing is numpy-vectorised with a coarse-to-fine ray march; a typical 7-minute flight takes 3-5 seconds on a laptop. Every parameter (sample rate, ray grid density, step sizes, score exponent, output cell size, sensor geometry) is a CLI flag — see `--help`.

### File layout after preprocessing

```
drone_video_telemetry/
  index.html         # The viewer
  mp4/                              # Source MP4s (and companion .SRT files)
  dem/
    area-dem.tif                    # Source DEM (large, not loaded by browser)
    preprocess_dem.py
    compute_visibility.py
    process_flights.py              # Batch orchestrator (ffmpeg + compute_visibility)
    PLAN.md                         # Full design notes
    flight-dem.bin / .json          # Shared clipped DEM for the viewer
    telemetry/
      <basename>-telemetry.json     # Extracted from MP4, or Exported from viewer
    visibility/
      <basename>.bin / .json        # Per-flight visibility heatmap
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
| `dem/area-dem.tif` | Source LiDAR DEM (not tracked, ~292 MB) |
| `dem/preprocess_dem.py` | Clip + optionally resample the DEM for browser use |
| `dem/compute_visibility.py` | Ray-cast visibility heatmap from telemetry + DEM |
| `dem/process_flights.py` | Batch-process every MP4 in `mp4/` through both steps (needs ffmpeg) |
| `dem/PLAN.md` | Full design notes for the DEM features |
| `dem/flight-dem.{bin,json}` | Shared clipped DEM (generated) |
| `dem/telemetry/<basename>-telemetry.json` | Exported per-flight telemetry (from viewer) |
| `dem/visibility/<basename>.{bin,json}` | Per-flight visibility heatmap (generated) |
