# MapGenie Map Extractor
This python project works to extract high-quality maps available at MapGenie.

Downloads MapGenie-backed raster tiles at each layer's **highest actually downloadable zoom**, caches them, and optionally stitches them into a lossless PNG. It supports canonical `mapgenie.io` pages and branded frontends such as `rdr2map.com` whose tiles still come from `tiles.mapgenie.io`.

## Use it first: GUI and CLI

### Windows setup

1. Install Python 3.11+ (Python 3.14 is supported) and ensure `python` works in Command Prompt/PowerShell.
2. Run `setup_windows.bat` once to install/update dependencies.
3. For the GUI, run `run_gui.bat`.

### GUI workflow

1. Paste a map page URL, for example:
    - GTA III: `https://mapgenie.io/grand-theft-auto-3/maps/liberty-city`
    - RDR2: `https://rdr2map.com/`
2. Choose an output folder.
3. Leave **Max zoom = auto** for highest available detail, or enter a cap such as `6`.
4. Leave **Transport = auto** unless diagnosing a problem.
5. Click **Analyze** to inspect layers, bounds, advertised zoom, and highest downloadable zoom.
6. Click **Download + Stitche** to cache tiles and build the PNGs.

Useful GUI controls:

- **Also discover sibling map pages**: canonical `mapgenie.io` URLs only. All tile sets on the current page are always processed.
- **Stitch...**: disable to download/cache tiles without building PNGs.
- **Workers / Delay / Retries**: tune download concurrency and pacing.
- **Max zoom**: `auto` uses the highest downloadable level; a number caps preflight/download at that zoom and still falls back lower if needed.
- **Transport**: `auto`, `chrome`, or `requests`. `auto` uses Chrome impersonation for branded sites and Requests for canonical MapGenie pages.
- **Custom CA bundle**: use an IT-provided PEM certificate bundle if corporate HTTPS inspection is not trusted automatically.
- **Disable HTTPS verification**: diagnostic last resort only.

### CLI syntax

```bash
python map_extractor.py "MAP_PAGE_URL" [options]
```

Common commands:

```bash
# Analyze configuration + live highest-downloadable zoom
python map_extractor.py "MAP_PAGE_URL" --inspect

# Download all tile sets and stitch PNGs
python map_extractor.py "MAP_PAGE_URL" --output ./output

# Cap download at z6 (falls back lower only if z6 is unavailable)
python map_extractor.py "MAP_PAGE_URL" --zoom 6 --output ./output

# Download/cache only; do not stitch
python map_extractor.py "MAP_PAGE_URL" --no-stitch --output ./output

# Process one tile-set index only
python map_extractor.py "MAP_PAGE_URL" --tileset 1 --output ./output

# Canonical mapgenie.io only: discover sibling map pages too
python map_extractor.py "MAP_PAGE_URL" --all-maps --output ./output

# Tune parallelism/rate/retries
python map_extractor.py "MAP_PAGE_URL" --concurrency 2 --request-delay 0.25 --retries 6 --output ./output

# Force a transport
python map_extractor.py "MAP_PAGE_URL" --transport chrome --output ./output
python map_extractor.py "MAP_PAGE_URL" --transport requests --output ./output

# Probe one exact tile: TILESET,Z,X,Y
python map_extractor.py "MAP_PAGE_URL" --probe-tile 0,7,95,56 --transport chrome

# Show TLS trust configuration
python map_extractor.py "MAP_PAGE_URL" --inspect --tls-status

# Use a custom CA bundle
python map_extractor.py "MAP_PAGE_URL" --inspect --ca-bundle "C:\path\to\corporate-root.pem"

# Disable certificate verification (unsafe; diagnostic only)
python map_extractor.py "MAP_PAGE_URL" --inspect --insecure

# Version / built-in option reference
python map_extractor.py --version
python map_extractor.py --help
```

### GTA III examples

```bash
python map_extractor.py "https://mapgenie.io/grand-theft-auto-3/maps/liberty-city" --inspect
python map_extractor.py "https://mapgenie.io/grand-theft-auto-3/maps/liberty-city" --output ./output
python map_extractor.py "https://mapgenie.io/grand-theft-auto-3/maps/liberty-city" --all-maps --output ./output
```

### RDR2 examples

```bash
# Analyze. RDR2 currently advertises z8 but the public tile set resolves to z7.
python map_extractor.py "https://rdr2map.com/" --inspect --tls-status

# Known-good z7 diagnostic probe
python map_extractor.py "https://rdr2map.com/" --probe-tile 0,7,95,56 --transport chrome

# Example z8 probe; currently returns storage-level AccessDenied
python map_extractor.py "https://rdr2map.com/" --probe-tile 0,8,190,112 --transport chrome

# Normal download; preflight selects z7 because z8 is unavailable
python map_extractor.py "https://rdr2map.com/" --output ./output

# Intentionally cap RDR2 at z6
python map_extractor.py "https://rdr2map.com/" --zoom 6 --output ./output
```

Do **not** use `--all-maps` for `rdr2map.com`; branded sites do not share one sibling-page URL convention. Every tile set exposed by the supplied page is already processed.

### macoS / Linux

```bash
python3 -m pip install -r requirements.txt
python3 gui.py
```

If Tk is unavailable, use the CLI.

## How it works

The extractor does not automate panning or take screenshots. It discovers the map configuration from embedded page data when possible and can fall back to MapGenie's public catalogue/full-map metadata.

For branded sites, the frontend and tile host are deliberately independent. A page such as `https://rdr2map.com/` can therefore resolve tile templates hosted under `https://tiles.mapgenie.io/games/... `.

For each tile set it:

1. Resolves the tile template, bounds, format, and advertised zoom range.
2. Probes from the advertised maximum, or the optional configured zoom cap, downward.
3. Selects the highest level at or below that target that actually returns image data.
4. Downloads tiles with caching, bounded concurrency, pacing, retries, and resume support.
5. Optionally streams the result into a PNG without allocating the complete image in RAM.

## Effective zoom and RDR2

Map metadata is not always the same as public tile availability. RDR2 currently advertises z8, while confirmed z8 objects return XML `403 AccessDenied`; z7 returns JPEG tiles normally.

The extractor distinguishes that storage-level `AccessDenied` response from a generic WAF/CDN 403. It can therefore:

- fall back from an unavailable advertised maximum to the highest proven image zoom;
- record `advertised_max_zoom`, `highest_downloadable_zoom`, `selected_zoom`, and probe diagnostics in the manifest;
- treat sparse storage-level `AccessDenied` holes inside an already proven zoom as transparent/missing tiles instead of rate limiting;
- avoid silently downgrading on ordinary 401/403/429/5xx failures.

Branded frontends default to the `chrome` transport via `curl_cffi`, including browser-like TLS/HTTP2 behavior and the required CORS origin/referer context. Canonical `mapgenie.io` pages default to Requests.

# output

A typical output tree is:

```text
output/
└── <game>/
    └── <map>/
        ├── map_info.json
        ├── tileset_0_z*_manifest.json
        ├── <map>_tileset-0__z*.png
        └── tiles/
            └── tileset_0/
                └── <zoom>/
                    └── <x>/
                        └── <y>.ext
```

Existing non-empty cached tiles are reused on later runs, so interrupted jobs can resume.

## Corporate proxy / certificate errors

If the browser works but Python reports `CERTIFICATE_VERIFY_FAILED` or a self-signed certificate in the chain, an HTTPS-inspection proxy may be presenting an organization-specific CA.

On Windows the extractor uses the native trust roots where possible. Check the active setting with `--tls-status`. If necessary, pass an
IT-provided PEM file with `--ca-bundle`. Use `-- insecure` only as a temporary diagnostic because it disables server-certificate verification.

## Large-map memory behavior

Downloads keep only a small bounded set of jobs in flight rather than creating a future for every coordinate. Stitching is also streaming: PNG scanlines are written incrementally, keeping roughly one horizontal tile row in memory instead of one giant RGBA canvas.

## v1.8.1 maintenance release

- Fixes GUI startup on Python 3.14: Tk variables are now created through one helper that always binds the Tk master explicitly.
- Adds a regression test for Boolean Tk variable initialization.
- Keeps the v1.8 bounded in-flight downloader and lean refactor unchanged.
- Moves GUI/CLI usage and command examples to the top of this README.

## Limitations

- MapGenie can change its page/API structures or tile access rules.
- `--all-maps` is intentionally limited to canonical `mapgenie.io` URL layouts.
- The extractor requires bounded tile coordinates and refuses unsafe unbounded full-grid crawling.
- It downloads artwork from the original host and does not bundle MapGenie/game assets. Use downloaded material according to the site's terms and applicable rights.
