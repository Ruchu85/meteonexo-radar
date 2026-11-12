import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aemet

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'aemet_parity.json').read_text(encoding='utf8'))


class ParityWithTheSiteParser(unittest.TestCase):
    """The expected values are what the site's own parseDaily/parseHourly return for the same raw documents."""

    def test_daily(self):
        self.assertEqual(aemet.parse_daily(FIXTURE['rawDaily'], FIXTURE['id']), FIXTURE['daily'])

    def test_hourly(self):
        self.assertEqual(aemet.parse_hourly(FIXTURE['rawHourly'], FIXTURE['id']), FIXTURE['hourly'])

    def test_foreign_id_is_rejected(self):
        self.assertIsNone(aemet.parse_daily(FIXTURE['mismatch']['rawDaily'], FIXTURE['id']))
        self.assertIsNone(aemet.parse_daily({'foo': 2}, None))


class Requests(unittest.TestCase):
    def reader(self, calls, answers):
        def read(url, key):
            calls.append((url, key))
            return answers[len(calls) - 1]
        return read

    def test_two_step_request_for_daily_and_hourly(self):
        calls = []
        read = self.reader(calls, [
            {'estado': 200, 'datos': 'https://opendata.aemet.es/opendata/sh/d'}, FIXTURE['rawDaily'],
            {'estado': 200, 'datos': 'https://opendata.aemet.es/opendata/sh/h'}, FIXTURE['rawHourly']])
        result = aemet.forecast('28079', 'KEY', read)
        self.assertTrue(result['ok'])
        self.assertEqual(result['daily'], FIXTURE['daily'])
        self.assertEqual(result['hourly'], FIXTURE['hourly'])
        self.assertTrue(calls[0][0].endswith('/diaria/28079'))
        self.assertTrue(calls[2][0].endswith('/horaria/28079'))
        self.assertTrue(all(key == 'KEY' for _, key in calls))

    def test_429_in_body_or_status_means_wait(self):
        with self.assertRaises(aemet.Busy):
            aemet.request('/x', 'K', lambda url, key: {'estado': 429})

    def test_data_url_must_stay_on_aemet(self):
        with self.assertRaises(RuntimeError):
            aemet.request('/x', 'K', lambda url, key: {'estado': 200, 'datos': 'https://evil.example/opendata/sh/x'})

    def test_numbers_like_json_not_python(self):
        self.assertEqual([aemet.number(v) for v in (21, '9,5', 'Ip', '-', '', None, '1_0', 'x', 3.0)], [21, 9.5, None, None, None, None, None, None, 3])
        self.assertIsInstance(aemet.number(3.0), int)


if __name__ == '__main__':
    unittest.main()
