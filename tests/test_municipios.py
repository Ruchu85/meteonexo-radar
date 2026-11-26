import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aemet
import municipios as mu

FIXTURES = Path(__file__).parent / 'fixtures'
# Real AEMET bundles of 26/11/2026 reduced to three municipalities: Madrid, León and 01001 (Alegría-Dulantzi),
# whose daily file has days with a single sky element (not wrapped in a list by the XML conversion).
DAILY = (FIXTURES / 'bundle_diaria.tar.gz').read_bytes()
HOURLY = (FIXTURES / 'bundle_horaria.tar.gz').read_bytes()


class BundleTest(unittest.TestCase):
    def test_reads_every_municipality_and_converts_the_layout(self):
        docs = mu.documents(DAILY)
        self.assertEqual(sorted(docs), ['01001', '24089', '28079'])
        day = docs['28079']['prediccion']['dia'][0]
        self.assertIn('estadoCielo', day)
        self.assertIn('probPrecipitacion', day)
        self.assertNotIn('estado_cielo', day)
        self.assertTrue(all('valor' not in entry for entry in day['probPrecipitacion']))

    def test_single_elements_become_lists(self):
        days = mu.documents(DAILY)['01001']['prediccion']['dia']
        self.assertTrue(all(isinstance(day['estadoCielo'], list) for day in days))

    def test_the_site_parser_reads_every_municipality(self):
        result, skipped = mu.entries(mu.documents(DAILY), mu.documents(HOURLY), '2026-11-26T09:10:00Z')
        self.assertEqual(skipped, [])
        self.assertEqual(sorted(result), ['01001', '24089', '28079'])
        madrid = result['28079']
        self.assertEqual(len(madrid['daily']['days']), 7)
        self.assertEqual(madrid['daily']['issued'], '2026-11-26T09:03:08')
        self.assertGreaterEqual(len(madrid['hourly']['hours']), 40)
        self.assertEqual(madrid['fetchedAt'], '2026-11-26T09:10:00Z')

    def test_expired_periods_come_without_value_and_are_not_a_0(self):
        result, _ = mu.entries(mu.documents(DAILY), mu.documents(HOURLY), 'x')
        periods = {p['period']: p for p in result['28079']['daily']['days'][0]['periods']}
        self.assertIsNone(periods['00-06']['rainProbability'])

    def test_a_municipality_without_days_is_left_out(self):
        docs = mu.documents(DAILY)
        docs['24089']['prediccion']['dia'] = []
        result, skipped = mu.entries(docs, mu.documents(HOURLY), 'x')
        self.assertNotIn('24089', result)
        self.assertEqual(skipped, ['24089 (no days)'])


class DownloadTest(unittest.TestCase):
    def test_429_in_the_answer_raises_busy(self):
        with self.assertRaises(aemet.Busy):
            mu.download('diaria', 'K', read_json=lambda url, key, timeout=None: {'estado': 429})

    def test_the_data_url_must_be_aemet(self):
        with self.assertRaises(RuntimeError):
            mu.download('diaria', 'K', read_json=lambda url, key, timeout=None: {'estado': 200, 'datos': 'https://evil.example/opendata/x'})

    def test_two_requests_per_bundle_and_the_bytes_are_returned(self):
        calls = []
        data = mu.download('horaria', 'K', read_json=lambda url, key, timeout=None: calls.append(url) or {'estado': 200, 'datos': 'https://opendata.aemet.es/opendata/sh/abc'},
                           read=lambda url, key: calls.append(url) or b'bundle')
        self.assertEqual(data, b'bundle')
        self.assertEqual(calls, [f'{aemet.ROOT}/prediccion/especifica/municipio/horaria/todos', 'https://opendata.aemet.es/opendata/sh/abc'])


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.result, _ = mu.entries(mu.documents(DAILY), mu.documents(HOURLY), '2026-11-26T09:10:00Z')

    def test_too_few_municipalities_are_never_published(self):
        self.assertTrue(mu.problems(self.result, None))
        with mock.patch.object(mu, 'MIN_MUNICIPALITIES', 3):
            self.assertEqual(mu.problems(self.result, None), [])
            self.assertTrue(mu.problems(self.result, 100), 'far fewer than the previous publication')

    def test_publication_writes_one_file_per_municipality_and_an_index(self):
        self.assertTrue(mu.publish(self.folder, self.result, {'version': 1, 'generatedAt': 'g1', 'count': 3}))
        files = sorted(p.name for p in (self.folder / mu.FOLDER).iterdir())
        self.assertEqual(files, ['01001.json', '24089.json', '28079.json', 'index.json'])
        self.assertEqual(json.loads((self.folder / mu.FOLDER / '28079.json').read_text(encoding='utf8'))['daily']['issued'], '2026-11-26T09:03:08')
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [mu.FOLDER], 'no temporary folder left')

    def test_a_failed_publication_keeps_the_previous_files(self):
        mu.publish(self.folder, self.result, {'version': 1, 'generatedAt': 'g1', 'count': 3})
        broken = {'28079': {'daily': {'days': []}, 'hourly': None, 'fetchedAt': 'x'}}
        self.assertFalse(mu.publish(self.folder, broken, {'version': 1, 'generatedAt': 'g2', 'count': 1}))
        self.assertEqual(mu.load_index(self.folder)['generatedAt'], 'g1')
        self.assertTrue((self.folder / mu.FOLDER / '24089.json').exists())
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [mu.FOLDER])

    def test_recent_files_are_not_downloaded_again(self):
        now = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
        stamp = lambda d: (now - d).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertTrue(mu.is_recent({'generatedAt': stamp(timedelta(minutes=30))}, now))
        self.assertFalse(mu.is_recent({'generatedAt': stamp(timedelta(minutes=70))}, now))
        self.assertFalse(mu.is_recent({}, now))


class MainTest(unittest.TestCase):
    def run_main(self, download, previous=None):
        folder = Path(tempfile.mkdtemp())
        if previous:
            (folder / mu.FOLDER).mkdir()
            (folder / mu.FOLDER / 'index.json').write_text(json.dumps(previous), encoding='utf8')
        with mock.patch.object(mu, 'download', side_effect=download), mock.patch.object(mu, 'MIN_MUNICIPALITIES', 3), \
                mock.patch.dict('os.environ', {'AEMET_API_KEY': 'K'}), mock.patch.object(sys, 'argv', ['municipios.py', '--output', str(folder)]):
            return mu.main(), folder

    def test_a_429_keeps_the_previous_files(self):
        def busy(kind, key):
            raise aemet.Busy(30)

        code, folder = self.run_main(busy, previous={'generatedAt': '2000-01-01T00:00:00Z', 'count': 3})
        self.assertEqual(code, 1)
        self.assertEqual(mu.load_index(folder)['generatedAt'], '2000-01-01T00:00:00Z')

    def test_a_full_run_publishes(self):
        code, folder = self.run_main(lambda kind, key: DAILY if kind == 'diaria' else HOURLY)
        self.assertEqual(code, 0)
        index = mu.load_index(folder)
        self.assertEqual(index['count'], 3)
        self.assertEqual(index['issuedTo'], '2026-11-26T09:03:08')
        self.assertTrue((folder / mu.FOLDER / '01001.json').exists())

    def test_recent_files_mean_no_request_at_all(self):
        recent = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        calls = []
        code, _ = self.run_main(lambda kind, key: calls.append(kind), previous={'generatedAt': recent, 'count': 3})
        self.assertEqual((code, calls), (0, []))


if __name__ == '__main__':
    unittest.main()
