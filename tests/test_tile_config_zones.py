"""Exercise the config-window save and real tile reload without a GUI."""
import contextlib
import copy
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import O4_Config_Utils as CFG


class Variable:
    def __init__(self, value):
        self.value = str(value)

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)


class TileConfigZoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'Ortho4XP_+47+010.cfg'
        self.zones = [
            [[47.95, 10.9, 48.02, 10.9, 48.02, 11.02, 47.95, 10.9], 18, 'BI'],
            [[47.8, 10.7, 48.1, 10.7, 48.1, 11.2, 47.8, 10.7], 16, 'BI'],
        ]
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(CFG.FNAMES, 'build_dir', return_value=str(self.folder)))
        current = {v: copy.deepcopy(CFG.cfg_get(v)) for v in CFG.list_tile_vars + CFG.list_app_vars}
        self.addCleanup(lambda: [CFG.cfg_set(v, value) for v, value in current.items()])
        self.popups = []
        self.window = SimpleNamespace(
            parent=SimpleNamespace(get_lat_lon=lambda: (47, 10),
                                   custom_build_dir_entry=Variable(str(self.folder))),
            v_={v: Variable(CFG.cfg_get(v)) for v in CFG.cfg_vars},
            popup=lambda *args: self.popups.append(args),
        )
        self.window.apply_changes = lambda: CFG.Ortho4XP_Config.apply_changes(self.window)
        self.window._tile_cfg_info = lambda: CFG.Ortho4XP_Config._tile_cfg_info(self.window)
        self.window.v_['default_website'].set('BI')
        self.window.v_['default_zl'].set('14')

    def save(self):
        self.assertEqual(CFG.Ortho4XP_Config.write_tile_cfg(self.window), 1)
        self.assertEqual(self.popups, [])

    def read_tile(self, inherited_zones=None):
        CFG.cfg_set('zone_list', copy.deepcopy(inherited_zones or []))
        tile = CFG.Tile(47, 10, str(self.folder))
        self.assertEqual(tile.read_from_config(), 1)
        return tile

    def test_settings_save_preserves_loaded_airport_zones(self):
        original = ('default_website=BI\ndefault_zl=14\n'
                    'custom_dem=Copernicus GLO-30 (from AWS Open Data) - worldwide\n'
                    'zone_list=' + repr(self.zones) + '\n')
        self.path.write_text(original, encoding='utf-8')
        loaded, lat, lon = CFG.Ortho4XP_Config._silent_load_tile(self.window)
        self.assertEqual((loaded, lat, lon), (True, 47, 10))
        self.window.v_['curvature_tol'].set('1.25')
        self.save()
        tile = self.read_tile()
        self.assertEqual(tile.zone_list, self.zones)
        self.assertEqual((tile.default_website, tile.default_zl), ('BI', 14))
        self.assertEqual(tile.curvature_tol, 1.25)
        self.assertIn('Copernicus', tile.custom_dem)
        self.assertEqual(Path(str(self.path) + '.bak').read_text(encoding='utf-8'), original)

    def test_empty_zones_override_nonempty_global_defaults(self):
        self.window.v_['zone_list'].set('[]')
        self.save()
        # A fresh tile must not inherit unrelated zones from global state.
        self.assertEqual(self.read_tile(inherited_zones=self.zones).zone_list, [])

    def test_repeated_save_keeps_zone_order_coordinates_and_provider(self):
        self.window.v_['zone_list'].set(repr(self.zones))
        self.save()
        first = self.path.read_bytes()
        changed = copy.deepcopy(self.zones)
        changed[0][2] = 'GO2'
        changed.append([list(self.zones[1][0]), 17, 'BI'])
        self.window.v_['zone_list'].set(repr(changed))
        self.save()
        self.assertEqual(self.read_tile().zone_list, changed)
        self.assertEqual(Path(str(self.path) + '.bak').read_bytes(), first)


if __name__ == '__main__':
    unittest.main()
