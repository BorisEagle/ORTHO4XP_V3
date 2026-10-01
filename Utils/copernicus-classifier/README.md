# Check which installed tiles use Copernicus heights

## Start the audit

Open `Utils/copernicus-classifier` inside the Ortho4XP installation and double-click **Check-Copernicus.cmd**. It scans your `zOrtho4XP_*` tile folders.

Reports are written outside the application, in **`%USERPROFILE%\Documents\Ortho4XP-DEM-Audit`**. The `tests` folder is for automated regression checks; the runnable utility lives under `Utils`.

The supplied defaults are:

- Ortho4XP: detected from the app containing `Utils/copernicus-classifier`.
- Scenery: `E:\SteamLibrary\steamapps\common\X-Plane 12\Custom Scenery`

It **does not rebuild tiles, change configurations, delete imagery, or write into those directories**. A full-library audit has not been run during development. Only Tenerife `+28-017` and Rome `+41+012` were checked.

Local Copernicus TIFFs and Viewfinder HGTs are used first. When a Copernicus TIFF is missing, the normal launch checks the public Copernicus COG and reads small raster windows over the network. It does not save a complete replacement DEM into Ortho4XP. Missing Viewfinder HGTs are not downloaded; these tiles require review because there is no historical reference to compare against.

For a completely local audit, launch from PowerShell:

```powershell
& '.\Check-Copernicus.cmd' --offline
```

## What to open afterwards

| File | Meaning |
|---|---|
| `convert-to-copernicus.txt` | Active tiles confidently matching cached Viewfinder heights, with a clear difference from Copernicus. These are the conversion candidates. |
| `review-tiles.txt` | Missing references, ambiguous sources, inconsistent regions, unsupported files or unverified scenery priority. |
| `results.csv` | Open in Excel to filter every tile by status, action and fit error. |
| `summary.md` | Counts and a readable summary. |
| `results.json` | Per-tile source fingerprints, sample heights and detailed evidence. |

Lists are **partial until `Run complete: True` appears in the summary**. Reports update every 25 tiles or 30 seconds, plus at completion or a normal Ctrl+C stop. Per-tile cache files are saved immediately. A forced terminal close may leave the combined report behind the saved cache; relaunch to reconstruct it.

## Statuses

| Status | Action |
|---|---|
| `COPERNICUS` | Keep: sampled installed heights fit Copernicus and differ from Viewfinder. |
| `VIEWFINDER` | Convert, if active scenery priority is verified. |
| `UNKNOWN` | Review: the script cannot establish the source reliably. |
| `MIXED` | Review: different large regions fit different sources. |
| `NO_LAND` | Keep: decoded physical terrain contains only water. |
| `SHADOWED` | Skip: a higher priority base mesh supplies this tile in X-Plane. |
| `DISABLED` / `UNLISTED` | Skip: this pack is not active in `scenery_packs.ini`. |
| `NOT_BUILT` | Skip: the installed DSF does not exist. |

`COPERNICUS` describes source consistency in the sampled terrain; it does not certify every vertex or guarantee that airport smoothing and tile seams are correct. `UNKNOWN` does **not** mean a rebuild is necessary. Other custom DEMs and damaged flat terrain can both produce this result.

## How it decides

1. Resolve X-Plane scenery priority and skip overlays when identifying the active base mesh. Inventory each pack once on a full run.
2. Verify the installed DSF checksum and decode actual physical **land** patch vertices. Water and object rotations are excluded.
3. Retain a deterministic, bounded sample spread over an 8 × 8 geographic grid. Exclude the outer 0.002 degrees and known airport centres: 3 km for airports and 700 m for heliports, using the supplied OurAirports-derived CSV. This is an approximate modification exclusion, not an exact airport patch outline.
4. Begin with up to four points per occupied cell. Expand uncertain results to up to 32 per cell, at most 2,048 points. Sampling fewer points on an island is normal.
5. Bilinearly sample reference heights at those coordinates, respecting GeoTIFF sample centres and rectangular grids. Test the current loader's 1 arcsecond Copernicus grid as an additional variant when attribution is uncertain or Viewfinder is indicated.
6. Confirm only with both a good absolute fit and a clear advantage where references differ. A Viewfinder decision must survive both Copernicus loading variants.

The current conservative thresholds require:

- At least 24 points where the two DEMs differ by at least 3 m.
- Support in at least three small geographic cells and two large tile quadrants.
- Winning median error ≤ 2 m, 90th percentile error ≤ 6 m at discriminating points.
- Median advantage ≥ 2 m, and at least 80% of discriminating points favouring the winner by over 1 m.
- Median error ≤ 2 m and 90th percentile ≤ 8 m over all paired samples.
- No sufficiently supported quadrant matching the opposing source. Two quadrants supporting each source produce `MIXED`.

Configs, build dates, `.alt` dimensions and the mere presence of a DEM file **never establish provenance**. Timestamps help invalidate a cache; they do not determine classification.

## Resume and refresh

Run the same command again to resume. Decoded sample caches are keyed by the DSF fingerprint, airport exclusions, sample settings and algorithm version; referenced terrain materials are checked for changes too. Decisions also depend on the reference fingerprints. Local fingerprints include file size, modification time and hashes of the file's ends; the DSF's checksum footer is included. Remote references use the HTTP ETag. Changed inputs cause rechecking. Unknown results are retried, so fixing missing references can resolve them on the next run.

To force fresh evidence:

```powershell
& '.\Check-Copernicus.cmd' --refresh
```

Keep airport exclusion data, scenery and reference files unchanged while an audit is running. Use `--refresh` if files were modified while their timestamps and end blocks were deliberately preserved.

## Check selected tiles only

```powershell
& '.\Check-Copernicus.cmd' --tile=+28-017 --tile=+41+012 --offline --output "$env:USERPROFILE\Documents\Ortho4XP-DEM-Pilot"
```

Always use `--tile=...`, including for southern hemisphere tiles whose names start with `-`. A selected-tile run checks scenery priority for those names; it does not decode unrelated tiles.

Change paths if needed:

```powershell
& '.\Check-Copernicus.cmd' --ortho 'E:\GAMES\ORTHO4XP_V3-3.6' --scenery 'E:\SteamLibrary\steamapps\common\X-Plane 12\Custom Scenery'
```

Reports must be outside the Ortho4XP and Custom Scenery directories. Keep this folder's script, launcher and `airports.csv` together. The launcher tries the application's venv first, then the working bundled Python on this computer; the script reuses Rasterio from the existing Ortho4XP venv if needed. Nothing is installed. On another computer, set `ORTHO_AUDIT_PYTHON` to a working Python executable with NumPy and Rasterio available.

## Validation and limits

The numeric decoder comparison is retained in [pilot-validation.json](validation/pilot-validation.json).

Development validation checked **two real tiles only**:

- Tenerife `+28-017`: `COPERNICUS`, independently agreeing with the previous DSFTool-based inspection.
- Older Rome `+41+012`: `VIEWFINDER`, matching cached HGT heights and differing from Copernicus under both tested loading variants.

The local pilot and synthetic tests cover source separation, abstention, mixed regions, object/water exclusion, differenced pool decoding, checksum failures, Point raster coordinates, missing tiles and scenery priority. A remote smoke test reached the public COG metadata, but this sandbox's Windows GDAL TLS credentials prevented reading its pixels. Live pixel access is therefore not validated here. Network failures remain `UNKNOWN`, never an automatic conversion.

The binary reader supports standard 16-bit DSF pool encodings and documented terrain commands. Compressed whole-file 7z DSFs, indirect raster heights and unknown command extensions require review. Historical Copernicus loader bugs beyond the native and current grid variants are not reconstructed. Files named `_COP30.tif` and `.hgt` are assumed to contain their expected reference products; the audit proves consistency with their values, not their download history.

Developer checks from the repository root, using its Python environment:

```powershell
python -m unittest discover -s tests -v
```

DSF decoding follows the [X-Plane DSF file format specification](https://developer.x-plane.com/article/dsf-file-format-specification/). Airport exclusion coordinates are derived from the existing [OurAirports data](https://ourairports.com/data/). Remote Copernicus naming follows the installed Ortho4XP DEM loader and the public Copernicus DEM 30 m COG dataset.
