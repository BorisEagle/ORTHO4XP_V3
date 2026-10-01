"""DEM selection through real tile configuration and offline raster metadata."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import O4_Config_Utils as CFG
import O4_DEM_Utils as DEM


class DemDefaultTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        Image.fromarray(np.zeros((180, 360), dtype=np.uint8)).save(self.root / 'world_tiles.png')
        self.stack.enter_context(patch.object(DEM.FNAMES, 'Utils_dir', str(self.root)))
        self.stack.enter_context(patch.object(CFG.FNAMES, 'build_dir', return_value=str(self.root)))
        self.stack.enter_context(patch.object(DEM.FNAMES, 'generic_tif', return_value=str(self.root / 'automatic.tif')))
        self.stack.enter_context(patch.object(DEM, '_COP30_SAMPLES', 24))
        self.stack.enter_context(patch.object(DEM, '_COP30_MARGIN', 1))
        self.stack.enter_context(patch.object(CFG, 'custom_dem', CFG.cfg_vars['custom_dem']['default']))

    def tile(self, text):
        cfg = self.root / 'tile.cfg'
        cfg.write_text(text, encoding='utf-8')
        tile = CFG.Tile(47, 10, str(self.root))
        self.assertEqual(tile.read_from_config(str(cfg)), 1)
        return tile

    def raster(self, name):
        path = self.root / name
        with rasterio.open(path, 'w', driver='GTiff', width=3, height=3,
                           count=1, dtype='float32', crs='EPSG:4326',
                           transform=from_origin(10, 48, 1/3, 1/3)) as dataset:
            dataset.write(np.full((3, 3), 500, dtype='float32'), 1)
        return path

    def test_missing_tile_setting_inherits_copernicus_default(self):
        tile = self.tile('default_zl=14\n')
        self.assertEqual(tile.custom_dem, 'Copernicus GLO-30 (from AWS Open Data) - worldwide')
        dem = DEM.DEM(tile.lat, tile.lon, tile.custom_dem, info_only=True)
        self.assertEqual((dem.nxdem, dem.nydem), (27, 27))

    def test_blank_tile_setting_uses_copernicus_even_with_another_global_source(self):
        CFG.custom_dem = 'Viewfinderpanoramas (J. de Ferranti) - mostly worldwide'
        tile = self.tile('custom_dem=\n')
        dem = DEM.DEM(tile.lat, tile.lon, tile.custom_dem, info_only=True)
        self.assertEqual((dem.nxdem, dem.nydem), (27, 27))

    def test_explicit_viewfinder_selection_is_preserved(self):
        tile = self.tile('custom_dem=Viewfinderpanoramas (J. de Ferranti) - mostly worldwide\n')
        dem = DEM.DEM(tile.lat, tile.lon, tile.custom_dem, info_only=True)
        self.assertEqual((dem.nxdem, dem.nydem), (3673, 3673))

    def test_explicit_custom_raster_is_preserved(self):
        tile = self.tile('custom_dem=' + str(self.raster('survey.tif')) + '\n')
        dem = DEM.DEM(tile.lat, tile.lon, tile.custom_dem, info_only=True)
        self.assertEqual((dem.nxdem, dem.nydem), (3, 3))

    def test_automatic_local_raster_override_is_preserved_for_blank_source(self):
        self.raster('automatic.tif')
        tile = self.tile('custom_dem=\n')
        dem = DEM.DEM(tile.lat, tile.lon, tile.custom_dem, info_only=True)
        self.assertEqual((dem.nxdem, dem.nydem), (3, 3))


if __name__ == '__main__':
    unittest.main()
