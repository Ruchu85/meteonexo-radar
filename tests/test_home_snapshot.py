import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home_snapshot


class HomeSnapshotTest(unittest.TestCase):
    def test_keeps_previous_forecast_when_a_city_fails(self):
        previous = {'cities': {'madrid': {'days': [{'date': 'old'}]}, 'gone': {'days': []}}}

        def get(url):
            if 'madrid' in url:
                raise OSError('HTTP 500')
            return {'ok': True, 'days': [{'date': 'new'}], 'issued': 'x'}

        snapshot = home_snapshot.build(['madrid', 'bilbao'], previous, get=get, sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['days'], [{'date': 'old'}])
        self.assertEqual(snapshot['cities']['bilbao']['days'], [{'date': 'new'}])
        self.assertNotIn('gone', snapshot['cities'])

    def test_retries_a_failed_city_once_after_a_pause(self):
        calls, pauses = [], []

        def get(url):
            calls.append(url)
            return {'ok': len(calls) > 1, 'days': [{'date': 'd'}], 'reason': 'unavailable'}

        snapshot = home_snapshot.build(['madrid'], {}, get=get, sleep=pauses.append)
        self.assertEqual(len(calls), 2)
        self.assertIn(home_snapshot.RETRY_PAUSE, pauses)
        self.assertIn('madrid', snapshot['cities'])

    def test_asks_for_cities_one_at_a_time_with_pauses(self):
        pauses = []
        home_snapshot.build(['a', 'b', 'c'], {}, get=lambda url: {'ok': True, 'days': [{}]}, sleep=pauses.append)
        self.assertEqual(pauses, [home_snapshot.PAUSE, home_snapshot.PAUSE])

    def test_stale_answers_do_not_count_as_new(self):
        previous = {'cities': {'madrid': {'days': [{'date': 'old'}], 'fetchedAt': 'then'}}}
        snapshot = home_snapshot.build(['madrid'], previous, get=lambda url: {'ok': True, 'days': [{'date': 'x'}], 'stale': True}, sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')

    def test_recent_snapshot_is_not_rebuilt(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        stamp = lambda d: (now - d).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertTrue(home_snapshot.is_recent({'generatedAt': stamp(timedelta(minutes=10))}, now))
        self.assertFalse(home_snapshot.is_recent({'generatedAt': stamp(timedelta(minutes=40))}, now))
        self.assertFalse(home_snapshot.is_recent({}, now))


if __name__ == '__main__':
    unittest.main()
