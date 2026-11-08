import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home_snapshot


def answer(date='old', **extra):
    data = {'ok': True, 'daily': {'days': [{'date': date, 'max': 20, 'min': 9, 'sky': 'Despejado', 'skyCode': '11', 'rainProbability': 0, 'wind': 1}], 'issued': 'x'}, 'hourly': {'hours': []}}
    data.update(extra)
    return data


class ForecastSnapshotTest(unittest.TestCase):
    def test_keeps_previous_forecast_when_a_city_fails(self):
        previous = {'cities': {'madrid': {'daily': {'days': [{'date': 'old'}]}, 'fetchedAt': 'then'}, 'gone': {'daily': {'days': []}}}}

        def get(url):
            if 'madrid' in url:
                raise OSError('HTTP 500')
            return answer('new')

        snapshot = home_snapshot.build(['madrid', 'bilbao'], previous, get=get, sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'], [{'date': 'old'}])
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')
        self.assertEqual(snapshot['cities']['bilbao']['daily']['days'][0]['date'], 'new')
        self.assertNotIn('gone', snapshot['cities'])

    def test_stores_daily_and_hourly_forecast(self):
        snapshot = home_snapshot.build(['madrid'], {}, get=lambda url: answer(), sleep=lambda _: None)
        entry = snapshot['cities']['madrid']
        self.assertEqual(entry['hourly'], {'hours': []})
        self.assertTrue(entry['fetchedAt'].endswith('Z'))

    def test_waits_out_the_site_cooldown_and_asks_again_for_the_same_city(self):
        calls, pauses = [], []

        def get(url):
            calls.append(url)
            return answer() if len(calls) > 1 else {'ok': False, 'reason': 'unavailable'}

        snapshot = home_snapshot.build(['madrid', 'bilbao'], {}, get=get, sleep=pauses.append)
        self.assertEqual([c.split('=')[1] for c in calls], ['madrid', 'madrid', 'bilbao'])
        self.assertEqual(pauses, [home_snapshot.COOLDOWN, home_snapshot.PAUSE])
        self.assertEqual(set(snapshot['cities']), {'madrid', 'bilbao'})

    def test_gives_up_on_a_city_after_three_attempts(self):
        calls = []
        snapshot = home_snapshot.build(['madrid'], {}, get=lambda url: calls.append(url) or {'ok': False}, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)
        self.assertEqual(snapshot['cities'], {})

    def test_no_more_retries_once_the_time_budget_is_spent(self):
        calls, ticks = [], iter([0, 10_000, 10_000, 10_000])
        home_snapshot.build(['madrid'], {}, get=lambda url: calls.append(url) or {'ok': False}, sleep=lambda _: None, clock=lambda: next(ticks))
        self.assertEqual(len(calls), 1)

    def test_asks_for_cities_one_at_a_time_with_pauses(self):
        pauses = []
        home_snapshot.build(['a', 'b', 'c'], {}, get=lambda url: answer(), sleep=pauses.append)
        self.assertEqual(pauses, [home_snapshot.PAUSE, home_snapshot.PAUSE])

    def test_stale_answers_do_not_count_as_new(self):
        previous = {'cities': {'madrid': {'daily': {'days': [{'date': 'old'}]}, 'fetchedAt': 'then'}}}
        snapshot = home_snapshot.build(['madrid'], previous, get=lambda url: answer('x', stale=True), sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')

    def test_home_map_view_comes_from_the_same_data(self):
        forecasts = {'generatedAt': '2026-11-08T10:00:00Z', 'cities': {
            'madrid': {'daily': answer()['daily'], 'hourly': None, 'fetchedAt': '2026-11-08T09:59:00Z'},
            'getafe': {'daily': answer()['daily'], 'hourly': None, 'fetchedAt': '2026-11-08T09:59:00Z'}}}
        view = home_snapshot.home_view(forecasts, ['madrid', 'bilbao'])
        self.assertEqual(list(view['cities']), ['madrid'])
        day = view['cities']['madrid']['days'][0]
        self.assertEqual(set(day), set(home_snapshot.DAY_KEYS))
        self.assertEqual(view['cities']['madrid']['fetchedAt'], '2026-11-08T09:59:00Z')

    def test_recent_snapshot_is_not_rebuilt(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        stamp = lambda d: (now - d).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertTrue(home_snapshot.is_recent({'generatedAt': stamp(timedelta(minutes=10))}, now))
        self.assertFalse(home_snapshot.is_recent({'generatedAt': stamp(timedelta(minutes=40))}, now))
        self.assertFalse(home_snapshot.is_recent({}, now))


if __name__ == '__main__':
    unittest.main()
