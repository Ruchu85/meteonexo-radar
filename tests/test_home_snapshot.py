import json
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aemet
import home_snapshot as hs


def answer(date='old', **extra):
    data = {'ok': True, 'daily': {'days': [{'date': date, 'max': 20, 'min': 9, 'sky': 'Despejado', 'skyCode': '11', 'rainProbability': 0, 'wind': 1}], 'issued': 'x'}, 'hourly': {'hours': []}}
    data.update(extra)
    return data


class Clock:
    """A fake monotonic clock that advances only when the code sleeps (or when told to)."""

    def __init__(self):
        self.now = 0.0
        self.pauses = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


def entry(fetched, date='d'):
    return {'daily': {'days': [{'date': date}]}, 'hourly': None, 'fetchedAt': fetched}


class ForecastSnapshotTest(unittest.TestCase):
    def test_keeps_previous_forecast_when_a_city_fails(self):
        previous = {'cities': {'madrid': {'daily': {'days': [{'date': 'old'}]}, 'fetchedAt': 'then'}, 'gone': {'daily': {'days': []}}}}

        def get(url, timeout=None):
            if 'madrid' in url:
                raise OSError('HTTP 500')
            return answer('new')

        snapshot = hs.build(['madrid', 'bilbao'], previous, get=get, sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'], [{'date': 'old'}])
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')
        self.assertEqual(snapshot['cities']['bilbao']['daily']['days'][0]['date'], 'new')
        self.assertNotIn('gone', snapshot['cities'])

    def test_stores_daily_and_hourly_forecast(self):
        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: answer(), sleep=lambda _: None)
        entry_ = snapshot['cities']['madrid']
        self.assertEqual(entry_['hourly'], {'hours': []})
        self.assertTrue(entry_['fetchedAt'].endswith('Z'))

    def test_waits_out_the_site_cooldown_and_asks_again_for_the_same_city(self):
        calls, clock = [], Clock()

        def get(url, timeout=None):
            calls.append(url)
            return answer() if len(calls) > 1 else {'ok': False, 'reason': 'unavailable'}

        snapshot = hs.build(['madrid', 'bilbao'], {}, get=get, sleep=clock.sleep, clock=clock)
        self.assertEqual([c.split('=')[1] for c in calls], ['madrid', 'madrid', 'bilbao'])
        self.assertEqual(clock.pauses, [hs.COOLDOWN, hs.PAUSE])
        self.assertEqual(set(snapshot['cities']), {'madrid', 'bilbao'})

    def test_gives_up_on_a_city_after_three_attempts_with_growing_waits(self):
        calls, clock = [], Clock()
        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: calls.append(url) or {'ok': False}, sleep=clock.sleep, clock=clock)
        self.assertEqual(len(calls), hs.ATTEMPTS)
        self.assertEqual(clock.pauses, [hs.COOLDOWN, 2 * hs.COOLDOWN], 'backoff: 35 s, then 70 s')
        self.assertEqual(snapshot['cities'], {})

    def test_asks_for_cities_one_at_a_time_with_pauses(self):
        clock = Clock()
        hs.build(['a', 'b', 'c'], {}, get=lambda url, timeout=None: answer(), sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.pauses, [hs.PAUSE, hs.PAUSE])

    def test_stale_answers_do_not_count_as_new(self):
        previous = {'cities': {'madrid': {'daily': {'days': [{'date': 'old'}]}, 'fetchedAt': 'then'}}}
        snapshot = hs.build(['madrid'], previous, get=lambda url, timeout=None: answer('x', stale=True), sleep=lambda _: None)
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')

    def test_home_map_view_comes_from_the_same_data(self):
        forecasts = {'generatedAt': '2026-11-08T10:00:00Z', 'cities': {
            'madrid': {'daily': answer()['daily'], 'hourly': None, 'fetchedAt': '2026-11-08T09:59:00Z'},
            'getafe': {'daily': answer()['daily'], 'hourly': None, 'fetchedAt': '2026-11-08T09:59:00Z'}}}
        view = hs.home_view(forecasts, ['madrid', 'bilbao'])
        self.assertEqual(list(view['cities']), ['madrid'])
        day = view['cities']['madrid']['days'][0]
        self.assertEqual(set(day), set(hs.DAY_KEYS))
        self.assertEqual(view['cities']['madrid']['fetchedAt'], '2026-11-08T09:59:00Z')

    def test_recent_snapshot_is_not_rebuilt(self):
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        stamp = lambda d: (now - d).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertTrue(hs.is_recent({'generatedAt': stamp(timedelta(minutes=10))}, now))
        self.assertFalse(hs.is_recent({'generatedAt': stamp(timedelta(minutes=40))}, now))
        self.assertFalse(hs.is_recent({}, now))


class DirectAemetTest(unittest.TestCase):
    def test_uses_aemet_directly_and_never_asks_the_site(self):
        site = []
        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: site.append(url) or answer(), sleep=lambda _: None, direct=lambda slug, timeout: answer('direct'))
        self.assertEqual(site, [])
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'][0]['date'], 'direct')

    def test_429_from_aemet_waits_and_does_not_fall_back_to_the_site(self):
        site, calls, clock = [], [], Clock()

        def direct(slug, timeout):
            calls.append(slug)
            if len(calls) == 1:
                raise aemet.Busy()
            return answer('direct')

        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: site.append(url) or answer(), sleep=clock.sleep, clock=clock, direct=direct)
        self.assertEqual(site, [])
        self.assertEqual(clock.pauses, [hs.COOLDOWN])
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'][0]['date'], 'direct')

    def test_other_aemet_errors_fall_back_to_the_site(self):
        def direct(slug, timeout):
            raise RuntimeError('AEMET service error')

        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: answer('site'), sleep=lambda _: None, direct=direct)
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'][0]['date'], 'site')

    def test_a_city_without_a_known_code_goes_through_the_site(self):
        snapshot = hs.build(['madrid'], {}, get=lambda url, timeout=None: answer('site'), sleep=lambda _: None, direct=lambda slug, timeout: None)
        self.assertEqual(snapshot['cities']['madrid']['daily']['days'][0]['date'], 'site')


class RobustnessTest(unittest.TestCase):
    def test_timeouts_are_bounded_and_a_timed_out_city_keeps_its_previous_forecast(self):
        timeouts, clock = [], Clock()
        previous = {'cities': {'madrid': entry('then')}}

        def get(url, timeout=None):
            timeouts.append(timeout)
            if 'madrid' in url:
                raise socket.timeout('timed out')
            return answer('new')

        report = {}
        snapshot = hs.build(['madrid', 'bilbao'], previous, get=get, sleep=clock.sleep, clock=clock, report=report)
        self.assertTrue(all(t is not None and t <= hs.REQUEST_TIMEOUT for t in timeouts), timeouts)
        self.assertEqual(snapshot['cities']['madrid']['fetchedAt'], 'then')
        self.assertEqual(report['refreshed'], ['bilbao'])
        self.assertEqual(report['kept'], ['madrid'])

    def test_retry_after_from_aemet_is_respected(self):
        clock, calls = Clock(), []

        def direct(slug, timeout):
            calls.append(slug)
            if len(calls) == 1:
                raise aemet.Busy(retry_after=50)
            return answer('direct')

        hs.build(['madrid'], {}, get=lambda url, timeout=None: answer(), sleep=clock.sleep, clock=clock, direct=direct)
        self.assertEqual(clock.pauses, [50])

    def test_a_retry_after_longer_than_the_limit_gives_the_city_up(self):
        clock, calls = Clock(), []

        def direct(slug, timeout):
            calls.append(slug)
            raise aemet.Busy(retry_after=hs.MAX_WAIT + 1)

        hs.build(['madrid'], {}, get=lambda url, timeout=None: answer(), sleep=clock.sleep, clock=clock, direct=direct)
        self.assertEqual(calls, ['madrid'])
        self.assertEqual(clock.pauses, [])

    def test_stops_asking_when_aemet_answers_429_to_several_cities_in_a_row(self):
        clock, calls = Clock(), []

        def direct(slug, timeout):
            calls.append(slug)
            raise aemet.Busy()

        report = {}
        hs.build(['a', 'b', 'c', 'd', 'e'], {}, get=lambda url, timeout=None: answer(), sleep=clock.sleep, clock=clock, direct=direct, report=report)
        self.assertNotIn('d', calls)
        self.assertEqual(report['stopped'], 'aemet-saturated')
        self.assertEqual(report['not_asked'], ['d', 'e'])

    def test_no_request_starts_after_the_deadline(self):
        clock, calls = Clock(), []

        def get(url, timeout=None):
            calls.append(url)
            clock.now += 200
            return answer()

        report = {}
        hs.build(['a', 'b', 'c', 'd'], {}, get=get, sleep=clock.sleep, clock=clock, deadline=420, report=report)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report['stopped'], 'deadline')
        self.assertLessEqual(clock.now, 420 + hs.REQUEST_TIMEOUT)

    def test_an_interrupted_run_stops_and_reports_it(self):
        stop = threading.Event()
        saved = []

        def get(url, timeout=None):
            if 'b' in url.split('=')[1]:
                stop.set()
            return answer()

        report = {}
        hs.build(['a', 'b', 'c'], {}, get=get, sleep=lambda _: None, stop=stop, on_entry=lambda slug, e: saved.append(slug), report=report)
        self.assertEqual(report['stopped'], 'interrupted')
        self.assertEqual(saved, ['a', 'b'])
        self.assertEqual(report['not_asked'], ['c'])


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.previous = {'version': 1, 'generatedAt': 'g0', 'cities': {'a': entry('2026-11-21T10:00:00Z'), 'b': entry('2026-11-21T10:00:00Z')}}
        (self.folder / 'forecasts.json').write_text(json.dumps(self.previous), encoding='utf8')

    def published(self):
        return json.loads((self.folder / 'forecasts.json').read_text(encoding='utf8'))

    def snapshot(self, cities):
        return {'version': 1, 'generatedAt': 'g1', 'source': 'AEMET OpenData', 'cities': cities}

    def test_a_valid_complete_snapshot_replaces_the_previous_one_atomically(self):
        new = self.snapshot({'a': entry('2026-11-21T11:00:00Z'), 'b': entry('2026-11-21T10:00:00Z')})
        self.assertTrue(hs.publish(self.folder, new, {'version': 1, 'cities': {}}, ['a', 'b'], self.previous))
        self.assertEqual(self.published()['generatedAt'], 'g1')
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['forecasts.json', 'home-weather.json'], 'no temporary file left')

    def test_an_incomplete_snapshot_never_replaces_the_last_valid_one(self):
        partial = self.snapshot({'a': entry('2026-11-21T11:00:00Z')})
        self.assertFalse(hs.publish(self.folder, partial, {'version': 1, 'cities': {}}, ['a', 'b'], self.previous))
        self.assertEqual(self.published()['generatedAt'], 'g0')
        self.assertFalse((self.folder / 'home-weather.json').exists())

    def test_a_city_older_than_the_published_one_is_rejected(self):
        older = self.snapshot({'a': entry('2026-11-21T09:00:00Z'), 'b': entry('2026-11-21T10:00:00Z')})
        self.assertFalse(hs.publish(self.folder, older, {'version': 1, 'cities': {}}, ['a', 'b'], self.previous))
        self.assertEqual(self.published()['generatedAt'], 'g0')

    def test_a_city_without_forecast_days_is_rejected(self):
        broken = self.snapshot({'a': {'daily': {'days': []}, 'fetchedAt': '2026-11-21T11:00:00Z'}, 'b': entry('2026-11-21T10:00:00Z')})
        self.assertFalse(hs.publish(self.folder, broken, {'version': 1, 'cities': {}}, ['a', 'b'], self.previous))
        self.assertEqual(self.published()['generatedAt'], 'g0')

    def test_checkpoint_of_an_unfinished_run_is_reused_when_newer(self):
        checkpoint = {'cities': {'a': entry('2026-11-21T11:00:00Z', 'new'), 'b': entry('2026-11-21T09:00:00Z', 'older')}}
        base = hs.with_checkpoint(self.previous, checkpoint)
        self.assertEqual(base['cities']['a']['fetchedAt'], '2026-11-21T11:00:00Z')
        self.assertEqual(base['cities']['b']['fetchedAt'], '2026-11-21T10:00:00Z', 'an older checkpoint entry is ignored')

    def test_retry_after_header_parsing(self):
        self.assertEqual(aemet.retry_after_seconds({'Retry-After': '42'}), 42)
        self.assertIsNone(aemet.retry_after_seconds({'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}))
        self.assertIsNone(aemet.retry_after_seconds({}))
        self.assertIsNone(aemet.retry_after_seconds(None))


class MainTest(unittest.TestCase):
    """main(): only a finished run with new data may replace forecasts.json; the checkpoint survives otherwise."""

    def run_main(self, report_update, cities):
        from unittest import mock
        folder = Path(tempfile.mkdtemp())
        previous = {'version': 1, 'generatedAt': '2000-01-01T00:00:00Z', 'cities': {'a': entry('2026-11-21T10:00:00Z')}}
        (folder / 'forecasts.json').write_text(json.dumps(previous), encoding='utf8')
        (folder / hs.CHECKPOINT).write_text(json.dumps({'version': 1, 'cities': {'a': entry('2026-11-21T10:30:00Z')}}), encoding='utf8')

        def fake_build(slugs, base, **kw):
            kw['report'].update({'refreshed': [], 'kept': [], 'not_asked': [], 'stopped': None, **report_update})
            return {'version': 1, 'generatedAt': '2026-11-21T11:00:00Z', 'source': 'AEMET OpenData', 'cities': cities}

        with mock.patch.object(hs, 'get_json', return_value={'cities': ['a'], 'all': ['a']}),                 mock.patch.object(hs, 'build', side_effect=fake_build),                 mock.patch.object(sys, 'argv', ['home_snapshot.py', '--output', str(folder)]):
            code = hs.main()
        return code, folder, json.loads((folder / 'forecasts.json').read_text(encoding='utf8'))

    def test_interrupted_run_publishes_nothing_and_keeps_the_checkpoint(self):
        code, folder, published = self.run_main({'stopped': 'interrupted', 'refreshed': ['a']}, {'a': entry('2026-11-21T11:00:00Z')})
        self.assertEqual(code, 1)
        self.assertEqual(published['generatedAt'], '2000-01-01T00:00:00Z')
        self.assertTrue((folder / hs.CHECKPOINT).exists())

    def test_run_without_new_data_publishes_nothing(self):
        code, folder, published = self.run_main({'refreshed': []}, {'a': entry('2026-11-21T10:00:00Z')})
        self.assertEqual(code, 1)
        self.assertEqual(published['generatedAt'], '2000-01-01T00:00:00Z')

    def test_finished_run_publishes_and_clears_the_checkpoint(self):
        code, folder, published = self.run_main({'refreshed': ['a']}, {'a': entry('2026-11-21T11:00:00Z')})
        self.assertEqual(code, 0)
        self.assertEqual(published['generatedAt'], '2026-11-21T11:00:00Z')
        self.assertFalse((folder / hs.CHECKPOINT).exists())
        self.assertTrue((folder / 'home-weather.json').exists())


if __name__ == '__main__':
    unittest.main()
