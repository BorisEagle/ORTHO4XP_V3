# Copernicus GLO-30 tile borders

The Copernicus loader assembles the surrounding 3 x 3 source tiles onto a
shared geographic grid. Each Ortho4XP tile includes a 0.01-degree margin
on every side, with 3,673 x 3,673 elevation nodes at one arc-second spacing.

Copernicus source rasters include the north and west boundary samples but
exclude the south and east boundary samples. Previously, reading one raster
alone clamped those missing edges to its last pixel. Neighboring tiles could
therefore use different elevations for the same border coordinate.

The loader now uses geographic transforms and bilinear resampling, including
for northern source rasters with fewer longitude columns. The common output
grid does not add detail beyond the source resolution. Source coordinates are
rounded to one millionth of a pixel to avoid floating-point gaps where coarse
northern rasters meet. Longitude wrapping handles the international dateline.

## Downloads and failures

- Keep selecting `Copernicus GLO-30 (from AWS Open Data) - worldwide` in
  `custom_dem`; no new configuration setting is required.
- Existing Copernicus TIFFs are reused. A first build may download up to eight
  additional neighboring rasters into the normal elevation cache. Subsequent
  adjacent builds reuse them.
- Ocean tiles identified by the existing `Utils/world_tiles.png` map use zero
  elevation when no local TIFF exists and need no download. These virtual
  ocean rasters match the half-pixel footprint of the preceding real TIFF,
  preventing uncovered columns where coarse northern rasters meet ocean.
- Missing, unreadable, misplaced, or invalid elevation data over mapped land
  stops the build with an error. It is not silently replaced with flat land.
- The metadata-only path returns the same grid information without downloads.
- Other elevation sources and custom TIFF loading retain their existing paths.

The land/ocean decision still depends on the repository's existing world map.
The loader requires rasterio, as provided by the application's environment.

## Rebuilding existing scenery

For affected tiles and their neighboring tiles, use the updated loader and
rebuild Step 1 (vector data), Step 2 (mesh), Step 2.5 (masks), and Step 3
(DSF/imagery). Rebuilding textures alone does not update terrain elevations.
Existing imagery can be reused, and airport zoom zones remain in the tile cfg.
Overlay extraction is independent of this DEM change.

Use consistent elevation settings when rebuilding neighboring tiles. This
change aligns their input DEMs; it does not guarantee identical finished
meshes under different airport smoothing, patches, or mesh settings.

## Verification

Run the offline regression suite from the repository root with the application's
Python environment:

```text
python -m unittest discover -s tests -v
```

The 16 tests cover shared borders, changing northern raster widths (including
native-height 3,600-row rasters), the dateline, known ocean, missing or invalid
land data, coarse coastal footprints, metadata-only loading, and existing
custom/View source behavior.

An isolated rebuild of `+43+006` and `+43+007` produced these results:

| Shared-border measurement | Previous loader | Loader with overlap |
| --- | ---: | ---: |
| Maximum difference in encoded DSFs | 36.37 m | 2.00 m |
| 95th percentile difference over land | 15.47 m | 0.0353 m |
| Median difference over land | 3.07 m | 0.00 m |

All 3,601 shared border samples in the raw DEMs matched exactly. Finished DSF
measurements interpolate decoded boundary vertices at their combined latitude
positions plus regular samples; meshes may use different vertices and encoded
precision. Both DSF checksums, referenced terrain/texture files, and all 50 DDS
headers and mip-chain sizes passed validation. Tile cfg bytes were preserved.

These are file-level checks. A visual test flight in X-Plane is still required
before considering the change visually validated.

The coastal correction was additionally verified with the actual cached
Shetland rasters: Step 1 for `+60-001` completed, and all 3,601 border samples
matched the adjacent ocean tile. The raw DEMs for the previously tested
southern pair remained byte-for-byte identical.
