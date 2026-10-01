"""Server selection and custom configuration tests without network access."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import O4_OSM_Utils as OSM

XML = b'<?xml version="1.0"?><osm version="0.6"></osm>'


def response(status=200):
    result = OSM.requests.Response()
    result.status_code = status
    result._content = XML
    return result


class OverpassServerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.logs = self.stack.enter_context(patch.object(OSM.UI, 'vprint'))
        self.stack.enter_context(patch.object(OSM.UI, 'red_flag', False))
        self.stack.enter_context(patch.dict(OSM.overpass_servers, {
            'DE': 'https://de.example.invalid/api/interpreter',
            'KU': 'https://ku.example.invalid/api/interpreter',
        }, clear=True))
        self.stack.enter_context(patch.dict(OSM._server_cooldown, {}, clear=True))
        self.stack.enter_context(patch.object(OSM, 'overpass_server_choice', 'KU'))
        self.stack.enter_context(patch.object(OSM, 'max_osm_tentatives', 2))
        self.stack.enter_context(patch.object(OSM.time, 'sleep'))
        self.stack.enter_context(patch.object(OSM.random, 'choice', side_effect=lambda pool: pool[0]))
        self.session = MagicMock()
        self.session.headers = {}
        self.session.get.return_value = response()
        self.factory = self.stack.enter_context(patch.object(OSM.requests, 'Session', return_value=self.session))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(OSM.FNAMES, 'Ortho4XP_dir', self.temp))
        self.file = Path(self.temp) / 'overpass_servers.json'

    def fetch(self, code=None):
        return OSM.get_overpass_data(('node["aeroway"]', 'way["aeroway"]'),
                                     (40, -7, 41, -6), server_code=code)

    def test_unknown_saved_choice_falls_back_without_crashing(self):
        OSM.overpass_server_choice = 'NG'
        self.assertEqual(self.fetch(), XML)
        self.assertTrue(self.session.get.call_args.args[0].startswith(OSM.overpass_servers['DE']))
        self.assertIn('NG', str(self.logs.call_args_list))

    def test_unknown_explicit_code_falls_back(self):
        self.assertEqual(self.fetch('MISSING'), XML)

    def test_explicit_random_selects_a_known_server(self):
        self.assertEqual(self.fetch('random'), XML)

    def test_empty_server_table_returns_failure_without_network(self):
        OSM.overpass_servers.clear()
        self.assertEqual(self.fetch(), 0)
        self.factory.assert_not_called()

    def test_valid_preference_is_honored(self):
        self.assertEqual(self.fetch(), XML)
        self.assertTrue(self.session.get.call_args.args[0].startswith(OSM.overpass_servers['KU']))

    def test_failed_preference_rotates_to_other_server(self):
        self.session.get.side_effect = [response(503), response()]
        self.assertEqual(self.fetch(), XML)
        urls = [call.args[0] for call in self.session.get.call_args_list]
        self.assertTrue(urls[0].startswith(OSM.overpass_servers['KU']))
        self.assertTrue(urls[1].startswith(OSM.overpass_servers['DE']))

    def test_custom_nextgis_survives_loading_from_local_file(self):
        endpoint = 'https://overpass.nextgis.com/test-secret/api/interpreter'
        self.file.write_text(json.dumps({'NG': endpoint}), encoding='utf-8')
        OSM.load_custom_overpass_servers()
        OSM.overpass_server_choice = 'NG'
        self.assertEqual(self.fetch(), XML)
        self.assertTrue(self.session.get.call_args.args[0].startswith(endpoint))
        self.assertNotIn('test-secret', str(self.logs.call_args_list))

    def test_missing_custom_file_preserves_builtin_servers(self):
        before = dict(OSM.overpass_servers)
        OSM.load_custom_overpass_servers()
        self.assertEqual(OSM.overpass_servers, before)

    def test_nextgis_key_file_registers_ng_without_exposing_the_key(self):
        (Path(self.temp) / 'overpass_server_api_key.txt').write_text('test-secret\n', encoding='utf-8')
        OSM.load_custom_overpass_servers()
        OSM.overpass_server_choice = 'NG'
        self.assertEqual(self.fetch(), XML)
        self.assertTrue(self.session.get.call_args.args[0].startswith(
            'https://overpass.nextgis.com/test-secret/api/interpreter'))
        self.assertNotIn('test-secret', str(self.logs.call_args_list))

    def test_custom_url_overrides_nextgis_key_file(self):
        (Path(self.temp) / 'overpass_server_api_key.txt').write_text('test-secret', encoding='utf-8')
        endpoint = 'https://custom.example.invalid/api/interpreter'
        self.file.write_text(json.dumps({'NG': endpoint}), encoding='utf-8')
        OSM.load_custom_overpass_servers()
        self.assertEqual(OSM.overpass_servers['NG'], endpoint)

    def test_custom_endpoint_query_parameters_are_preserved(self):
        endpoint = 'https://custom.example.invalid/api/interpreter?token=test-secret'
        self.file.write_text(json.dumps({'NG': endpoint}), encoding='utf-8')
        OSM.load_custom_overpass_servers()
        self.assertEqual(self.fetch('NG'), XML)
        self.assertTrue(self.session.get.call_args.args[0].startswith(endpoint + '&data='))
        self.assertNotIn('test-secret', str(self.logs.call_args_list))

    def test_invalid_custom_config_is_atomic_and_does_not_log_secrets(self):
        for value in ('{"NG":"test-secret"', {'NG': 'test-secret'},
                      {'NG': 'https://valid.example/api', 'BAD': 'test-secret'},
                      {'NG': 'https://valid.example/api#'},
                      {'NG': 'https://valid.example/api#fragment'},
                      {'random': 'https://valid.example/api'}, ['test-secret']):
            with self.subTest(value=value):
                before = dict(OSM.overpass_servers)
                self.file.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
                OSM.load_custom_overpass_servers()
                self.assertEqual(OSM.overpass_servers, before)
                self.assertNotIn('test-secret', str(self.logs.call_args_list))


if __name__ == '__main__':
    unittest.main()
