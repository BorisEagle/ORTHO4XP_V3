"""Offline regression tests using real, georeferenced Copernicus-like TIFFs.

Run: python -m unittest discover -s tests -v
"""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import O4_DEM_Utils as DEM

SOURCE = "Copernicus GLO-30 (from AWS Open Data) - worldwide"


class CopernicusOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.land = np.zeros((180, 360), dtype=np.uint8)
        self.requests = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(DEM.FNAMES, "Utils_dir", str(self.root)))
        self.stack.enter_context(patch.object(DEM, "cop30_file_name", self.path))
        self.stack.enter_context(patch.object(DEM, "ensure_elevation", self.ensure))
        # Small grids keep the offline tests quick. Production uses 3600/36.
        self.stack.enter_context(patch.object(DEM, "_COP30_SAMPLES", 24, create=True))
        self.stack.enter_context(patch.object(DEM, "_COP30_MARGIN", 1, create=True))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def path(self, lat, lon):
        return str(self.root / f"{lat}_{lon}.tif")

    def ensure(self, source, lat, lon, verbose=True):
        self.requests.append((lat, lon))
        return int(Path(self.path(lat, lon)).is_file())

    def write(self, lat, lon, width=24, height=24):
        self.land[89 - lat, 180 + lon] = 1
        # Copernicus Point samples include west/north, excluding east/south.
        x = lon + np.arange(width) / width
        y = lat + 1 - np.arange(height) / height
        unwrapped_x = np.where(x < -179, x + 360, x)
        z = (800 + 100 * np.sin(unwrapped_x[None, :] * 3)
             + 20 * y[:, None]).astype("float32")
        with rasterio.open(self.path(lat, lon), "w", driver="GTiff", width=width,
                           height=height, count=1, dtype="float32", crs="EPSG:4326",
                           transform=from_origin(lon - .5 / width, lat + 1 + .5 / height,
                                                 1 / width, 1 / height)) as f:
            f.write(z, 1)
            f.update_tags(AREA_OR_POINT="Point")

    def region(self, lat, lon, width=None, height=24):
        for y in range(lat - 1, lat + 2):
            for x in range(lon - 1, lon + 3):
                self.write(y, (x + 180) % 360 - 180, (width or {}).get(y, 24), height)

    def dem(self, lat, lon, info_only=False):
        Image.fromarray(self.land).save(self.root / "world_tiles.png")
        return DEM.DEM(lat, lon, SOURCE, fill_nodata=False, info_only=info_only)

    def test_padded_extent_and_grid(self):
        self.region(43, 6)
        dem = self.dem(43, 6)
        self.assertLess(dem.x0, 0)
        self.assertGreater(dem.x1, 1)
        self.assertLess(dem.y0, 0)
        self.assertGreater(dem.y1, 1)
        self.assertEqual(dem.alt_dem.shape, (27, 27))

    def test_east_west_tiles_agree_at_shared_coordinates(self):
        self.region(43, 6)
        a, b = self.dem(43, 6), self.dem(43, 7)
        y = np.linspace(.01, .99, 101)
        np.testing.assert_allclose(a.alt_vec(np.column_stack((np.ones_like(y), y))),
                                   b.alt_vec(np.column_stack((np.zeros_like(y), y))), atol=.001)

    def test_north_south_tiles_agree_across_column_width_change(self):
        self.region(49, 6, {48: 24, 49: 24, 50: 12})
        self.region(50, 6, {49: 24, 50: 12, 51: 12})
        a, b = self.dem(49, 6), self.dem(50, 6)
        x = np.linspace(.01, .99, 101)
        np.testing.assert_allclose(a.alt_vec(np.column_stack((x, np.ones_like(x)))),
                                   b.alt_vec(np.column_stack((x, np.zeros_like(x)))), atol=.001)
        self.assertEqual(a.alt_dem.shape, b.alt_dem.shape)

    def test_northern_coarse_pixels_leave_no_rounding_gaps(self):
        # Half a coarse pixel can coincide exactly with an output node. With
        # negative longitudes, float error used to exclude it from BOTH TIFFs.
        with patch.object(DEM, "_COP30_SAMPLES", 3600), patch.object(DEM, "_COP30_MARGIN", 36):
            for lat, width in ((65, 1800), (85, 360)):
                with self.subTest(lat=lat, width=width):
                    self.region(lat, -10, {lat - 1: width, lat: width, lat + 1: width}, height=3600)
                    dem = self.dem(lat, -10)
                    self.assertTrue(np.isfinite(dem.alt_dem).all())
                    self.assertTrue((dem.alt_dem > 100).all())

    def test_info_only_matches_grid_without_network(self):
        self.region(43, 6)
        info = self.dem(43, 6, info_only=True)
        self.assertFalse(self.requests)
        full = self.dem(43, 6)
        for key in ("x0", "x1", "y0", "y1", "nxdem", "nydem"):
            self.assertEqual(getattr(info, key), getattr(full, key))

    def test_missing_land_neighbor_aborts(self):
        self.region(43, 6)
        Path(self.path(43, 7)).unlink()
        with self.assertRaisesRegex(RuntimeError, "Copernicus.*43.*7"):
            self.dem(43, 6)

    def test_corrupt_land_raster_aborts(self):
        self.region(43, 6)
        Path(self.path(43, 6)).write_bytes(b"broken TIFF")
        with self.assertRaisesRegex(RuntimeError, "Copernicus"):
            self.dem(43, 6)

    def test_land_raster_with_wrong_geographic_extent_aborts(self):
        self.region(43, 6)
        with rasterio.open(self.path(43, 6), "r+") as src:
            src.transform = from_origin(20, 30, 1 / 24, 1 / 24)
        with self.assertRaisesRegex(RuntimeError, "Copernicus"):
            self.dem(43, 6)

    def test_all_nodata_land_cannot_be_filled_with_ocean_zero(self):
        self.write(43, 6)
        with rasterio.open(self.path(43, 6), "r+") as src:
            src.nodata = -32768
            src.write(np.full((24, 24), -32768, dtype="float32"), 1)
        with self.assertRaisesRegex(RuntimeError, "Copernicus"):
            self.dem(43, 6)

    def test_nonfinite_land_aborts(self):
        self.write(43, 6)
        with rasterio.open(self.path(43, 6), "r+") as src:
            src.write(np.full((24, 24), np.nan, dtype="float32"), 1)
        with self.assertRaisesRegex(RuntimeError, "Copernicus"):
            self.dem(43, 6)

    def test_custom_raster_retains_its_native_grid(self):
        self.write(43, 6)
        dem = DEM.DEM(43, 6, self.path(43, 6), fill_nodata=False)
        self.assertEqual(dem.alt_dem.shape, (24, 24))
        self.assertEqual(dem.x0, 0)
        self.assertFalse(self.requests)

    def test_existing_view_metadata_is_unchanged(self):
        Image.fromarray(self.land).save(self.root / "world_tiles.png")
        dem = DEM.DEM(43, 6, DEM.available_sources[1], info_only=True)
        self.assertEqual((dem.x0, dem.y0, dem.x1, dem.y1), (-.01, -.01, 1.01, 1.01))
        self.assertEqual((dem.nxdem, dem.nydem), (3673, 3673))

    def test_full_ocean_does_not_download_or_fail(self):
        dem = self.dem(0, 0)
        self.assertFalse(self.requests)
        np.testing.assert_array_equal(dem.alt_dem, 0)

    def test_dateline_neighbors_are_shifted_to_common_longitudes(self):
        self.region(10, 179)
        a, b = self.dem(10, 179), self.dem(10, -180)
        y = np.linspace(.01, .99, 101)
        np.testing.assert_allclose(a.alt_vec(np.column_stack((np.ones_like(y), y))),
                                   b.alt_vec(np.column_stack((np.zeros_like(y), y))), atol=.001)


if __name__ == "__main__":
    unittest.main()
