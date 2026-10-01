import hashlib
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'Utils'/'copernicus-classifier'))
from classifier import (classify, read_dsf, sample_grid, fingerprint, make_key, load_rasterio,
                        sample_tiff, eligible_points, discover_tiles, main, _sample_cache, runtime_defaults, audit_one)


def atom(kind, data):
    return kind + struct.pack('<I', len(data) + 8) + data


def dsf_fixture(path, compressed=False):
    path.parent.mkdir(parents=True)
    material = path.parents[2]/'terrain'/'land.ter'
    material.parent.mkdir()
    material.write_text('A\n800\nTERRAIN\nBASE_TEX example.dds\n')
    # Three land vertices, three water vertices and one object rotation.
    count = 7
    values = ([0, 32768, 65535, 0, 32768, 65535, 100],
              [0, 32768, 65535, 0, 32768, 65535, 100],
              [300, 200, 100, 0, 0, 0, 45000], [0]*count, [0]*count)
    pool = struct.pack('<IB', count, 5)
    for v in values:
        if compressed:
            delta = [v[0]] + [(v[i]-v[i-1]) % 65536 for i in range(1, count)]
            if all(value == 0 for value in delta):
                pool += bytes([3, 128+count]) + struct.pack('<H', 0)
            else:
                pool += bytes([3, count]) + struct.pack('<7H', *delta)
        else:
            pool += bytes([0]) + struct.pack('<7H', *v)
    scales = struct.pack('<10f', 1, 10, 1, 40, 65535, 0, 1, 0, 1, 0)
    commands = (b'\x01\x00\x00\x03\x00\x12\x01' + struct.pack('<ff', 0, -1)
                + b'\x17\x03' + struct.pack('<3H', 0, 1, 2)
                + b'\x03\x01\x12\x01' + struct.pack('<ff', 0, -1)
                + b'\x18\x03' + struct.pack('<6H', 0, 3, 0, 4, 0, 5)
                + b'\x07\x06\x00')
    data = (b'XPLNEDSF' + struct.pack('<I', 1)
            + atom(b'NFED', atom(b'TRET', b'terrain/land.ter\0terrain_Water\0'))
            + atom(b'DOEG', atom(b'LOOP', pool) + atom(b'LACS', scales))
            + atom(b'SDMC', commands))
    path.write_bytes(data + hashlib.md5(data).digest())


class DSFTests(unittest.TestCase):
    def test_water_and_object_rotation_are_not_land_heights(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'Earth nav data'/'+40+010'/'a.dsf'
            dsf_fixture(p)
            info, xyz = read_dsf(p, 40, 10, max_per_cell=32)
            self.assertEqual(info['land_vertices'], 3)
            self.assertEqual(sorted(xyz[:, 2].tolist()), [100, 200, 300])

    def test_differenced_compression_wraps_negative_deltas(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'Earth nav data'/'+40+010'/'a.dsf'
            dsf_fixture(p, compressed=True)
            _, xyz = read_dsf(p, 40, 10, max_per_cell=32)
            self.assertEqual(sorted(xyz[:, 2].tolist()), [100, 200, 300])

    def test_invalid_checksum_cannot_be_classified(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'Earth nav data'/'+40+010'/'a.dsf'
            dsf_fixture(p)
            p.write_bytes(p.read_bytes()[:-16] + b'x'*16)
            with self.assertRaises(ValueError):
                read_dsf(p, 40, 10)

    def test_unknown_command_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'Earth nav data'/'+40+010'/'a.dsf'
            dsf_fixture(p)
            data = p.read_bytes()[:-16]
            pos = data.index(b'SDMC')+8
            data = data[:pos] + b'\xff' + data[pos+1:]
            p.write_bytes(data+hashlib.md5(data).digest())
            with self.assertRaisesRegex(ValueError, 'Unsupported DSF command'):
                read_dsf(p, 40, 10)

    def test_changed_terrain_material_invalidates_cached_land_samples(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'pack'/'Earth nav data'/'+40+010'/'+40+010.dsf'
            dsf_fixture(p)
            output = Path(t)/'reports'
            (output/'cache').mkdir(parents=True)
            args = SimpleNamespace(output=output, refresh=False, per_cell=32)
            row = {'tile': '+40+010', 'dsf': str(p)}
            _, before, sig_before = _sample_cache(row, args, np.empty((0, 3)), {})
            material = p.parents[2]/'terrain'/'land.ter'
            material.write_text('A\n800\nTERRAIN\nWATER_COLOR_MASK\n')
            _, after, sig_after = _sample_cache(row, args, np.empty((0, 3)), {})
            self.assertEqual(before['land_vertices'], 3)
            self.assertEqual(after['land_vertices'], 0)
            self.assertNotEqual(make_key(sig_before), make_key(sig_after))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.c = np.arange(128, dtype=float) + 100
        self.h = self.c + 20
        self.cells = np.repeat([0, 7, 56, 63], 32)

    def decision(self, z, c=None, h=None, cells=None):
        return classify(z, self.c if c is None else c, self.h if h is None else h,
                        self.cells if cells is None else cells)

    def test_installed_heights_identify_copernicus(self):
        self.assertEqual(self.decision(self.c+.05)['status'], 'COPERNICUS')

    def test_viewfinder_is_the_conversion_candidate(self):
        self.assertEqual(self.decision(self.h+.1)['status'], 'VIEWFINDER')

    def test_two_bad_fits_do_not_create_a_relative_winner(self):
        self.assertEqual(self.decision(self.c+100)['status'], 'UNKNOWN')

    def test_similar_references_cannot_establish_provenance(self):
        self.assertEqual(self.decision(self.c, h=self.c+.2)['status'], 'UNKNOWN')

    def test_missing_copernicus_does_not_automatically_mean_old(self):
        self.assertEqual(self.decision(self.h, c=np.full(128, np.nan))['status'], 'UNKNOWN')

    def test_single_region_is_insufficient_for_confirmation(self):
        self.assertEqual(self.decision(self.c, cells=np.zeros(128, int))['status'], 'UNKNOWN')

    def test_distinct_regions_matching_distinct_sources_require_review(self):
        z = self.c.copy()
        z[64:] = self.h[64:]
        self.assertEqual(self.decision(z)['status'], 'MIXED')

    def test_alternate_only_viewfinder_match_cannot_trigger_conversion(self):
        points = np.array([[10+(i%8+.5)/8, 40+(i//8+.5)/8, 100] for i in range(64)])
        info = {'land_patches': 1, 'water_patches': 0, 'land_vertices': 64,
                'sampled_vertices': 64, 'eligible_vertices': 64, 'checksum': 'fixture',
                'properties': {}, 'terrain_fingerprints': {}, 'border_excluded': 0, 'airport_excluded': 0}
        with tempfile.TemporaryDirectory() as t:
            output = Path(t)
            (output/'cache').mkdir()
            args = SimpleNamespace(output=output, refresh=False, offline=True)
            row = {'tile': '+40+010', 'dsf': 'fixture.dsf', 'priority_verified': True}
            with patch('classifier._sample_cache', return_value=(points, info, {})), \
                 patch('classifier._reference_info', return_value=({'copernicus':'cop.tif', 'viewfinder':'view.hgt'}, {}, [])), \
                 patch('classifier.sample_tiff', side_effect=lambda s,x,y,r: np.full(len(x),101.0)), \
                 patch('classifier.regridded_cop', side_effect=lambda s,x,y,lat,lon,r: np.full(len(x),120.0)), \
                 patch('classifier.sample_hgt', side_effect=lambda s,lat,lon,x,y: np.full(len(x),100.0)):
                result = audit_one(row, args, np.empty((0,3)), {}, None)
            self.assertEqual(result['status'], 'UNKNOWN')


class RasterTests(unittest.TestCase):
    def test_pixel_centres_and_rectangular_raster_are_respected(self):
        a = np.array([[10, 20, 30], [50, 60, 70]], float)
        v = sample_grid(a, np.array([0, 1.5]), np.array([0, .5]))
        np.testing.assert_allclose(v, [10, 45])

    def test_outside_and_nodata_samples_are_not_clamped_into_land(self):
        a = np.array([[10, -32768], [50, 60]], float)
        v = sample_grid(a, np.array([-1, .5]), np.array([0, .5]), nodata=-32768)
        self.assertTrue(np.isnan(v).all())

    def test_replaced_reference_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'a'
            p.write_bytes(b'a')
            before = fingerprint(p)
            p.write_bytes(b'different')
            self.assertNotEqual(before, fingerprint(p))

    def test_changed_reference_invalidates_a_cached_decision(self):
        key = make_key({'dsf': 'abc', 'cop': 'old', 'settings': [32]})
        self.assertNotEqual(key, make_key({'dsf': 'abc', 'cop': 'new', 'settings': [32]}))

    def test_geotiff_point_coordinates_are_not_shifted_half_a_pixel(self):
        rio = load_rasterio(r'E:\GAMES\ORTHO4XP_V3-3.6')
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'point.tif'
            with rio.open(p, 'w', driver='GTiff', width=3, height=2, count=1, dtype='float32',
                          crs='EPSG:4326', transform=rio.Affine(1, 0, 9.5, 0, -1, 41.5)) as ds:
                ds.write(np.array([[10, 20, 30], [50, 60, 70]], np.float32), 1)
                ds.update_tags(AREA_OR_POINT='Point')
            v = sample_tiff(p, np.array([10, 11.5]), np.array([41, 40.5]), rio)
            np.testing.assert_allclose(v, [10, 45])

    def test_airport_modification_zone_does_not_enter_reference_comparison(self):
        xyz = np.array([[10.5, 40.5, 999], [10.8, 40.8, 123], [10, 40.7, 222]])
        v, excluded = eligible_points(xyz, np.array([[10.5, 40.5, 3.0]]), 40, 10)
        np.testing.assert_allclose(v, [[10.8, 40.8, 123]])
        self.assertEqual(excluded['airport_excluded'], 1)


class InventoryTests(unittest.TestCase):
    def test_installed_utility_uses_app_root_and_its_height_reports_folder(self):
        with tempfile.TemporaryDirectory() as t:
            app = Path(t)/'Ortho4XP'
            utility = app/'Utils'/'copernicus-classifier'
            utility.mkdir(parents=True)
            (app/'Ortho4XP.py').write_text('# app marker')
            home = Path(t)/'User'
            ortho, scenery, output = runtime_defaults(utility, home)
            self.assertEqual(ortho, app)
            self.assertEqual(output, app/'Height-Reports')

    def test_higher_priority_mesh_prevents_conversion_of_shadowed_ortho(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)/'Custom Scenery'
            root.mkdir()
            for pack in ('zOrtho4XP_+40+010', 'Airport Mesh'):
                dsf_fixture(root/pack/'Earth nav data'/'+40+010'/'+40+010.dsf')
            (root/'scenery_packs.ini').write_text('SCENERY_PACK Custom Scenery/Airport Mesh/\n'
                                                 'SCENERY_PACK Custom Scenery/zOrtho4XP_+40+010/\n')
            rows, _ = discover_tiles(root, ['+40+010'])
            self.assertEqual(rows[0]['status'], 'SHADOWED')

    def test_disabled_tile_is_not_a_conversion_candidate(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)/'Custom Scenery'
            root.mkdir()
            dsf_fixture(root/'zOrtho4XP_+40+010'/'Earth nav data'/'+40+010'/'+40+010.dsf')
            (root/'scenery_packs.ini').write_text('SCENERY_PACK_DISABLED Custom Scenery/zOrtho4XP_+40+010/\n')
            rows, _ = discover_tiles(root, ['+40+010'])
            self.assertEqual(rows[0]['status'], 'DISABLED')

    def test_cli_cannot_write_reports_inside_scenery(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)/'Custom Scenery'
            output = root/'audit'
            self.assertEqual(main(['--scenery', str(root), '--output', str(output), '--offline']), 1)
            self.assertFalse(output.exists())

    def test_cli_can_write_to_dedicated_height_reports_inside_app(self):
        with tempfile.TemporaryDirectory() as t:
            app = Path(t)/'Ortho4XP'
            scenery = Path(t)/'Custom Scenery'
            app.mkdir()
            scenery.mkdir()
            output = app/'Height-Reports'
            self.assertEqual(main(['--ortho', str(app), '--scenery', str(scenery),
                                   '--output', str(output), '--tile=+40+010', '--offline']), 0)
            self.assertTrue((output/'summary.md').is_file())
            self.assertEqual(list(scenery.iterdir()), [])

    def test_cli_cannot_write_reports_into_elevation_cache(self):
        with tempfile.TemporaryDirectory() as t:
            app = Path(t)/'Ortho4XP'
            output = app/'Elevation_data'/'audit'
            self.assertEqual(main(['--ortho', str(app), '--output', str(output), '--offline']), 1)
            self.assertFalse(output.exists())

    def test_cli_reports_missing_tile_without_modifying_scenery(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)/'Custom Scenery'
            root.mkdir()
            output = Path(t)/'reports'
            self.assertEqual(main(['--scenery', str(root), '--output', str(output), '--tile=+40+010', '--offline']), 0)
            self.assertEqual(list(root.iterdir()), [])
            import json
            result = json.loads((output/'results.json').read_text())
            self.assertEqual(result['tiles'][0]['status'], 'NOT_BUILT')
            self.assertEqual((output/'convert-to-copernicus.txt').read_text(), '')


if __name__ == '__main__':
    unittest.main()
