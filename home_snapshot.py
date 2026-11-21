"""Builds forecasts.json (every indexable city) and home-weather.json (the home map) for TiempoEn.

AEMET rejects bursts, so the site cannot ask for dozens of cities while visitors or crawlers wait.
This step asks for one city at a time, slowly, and publishes the result; the site reads it before it
goes to AEMET. A city that fails keeps its previous forecast, and the site judges each city's age.

Robustness rules (the workflow step is killed after 9 minutes):
- every request has a timeout and no request starts after DEADLINE;
- a 429 is retried at most twice per city, waiting 35 s then 70 s, or longer if AEMET asks for it
  with Retry-After (never more than MAX_WAIT); after BUSY_STREAK consecutive cities answered 429,
  the run stops asking;
- each city obtained is saved at once in a checkpoint that is never published; the next run reuses it;
- forecasts.json is only replaced, atomically, by a validated complete snapshot; a run that is
  interrupted, or that cannot build a valid snapshot, leaves the last valid one untouched.
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aemet

SITE = os.environ.get('HOME_SITE_URL', 'https://tiempoen.es')
MIN_AGE = timedelta(minutes=25)
PAUSE = 5
# After a 429 the site stops calling AEMET for about 30 seconds: wait that long before asking again.
COOLDOWN = 35
ATTEMPTS = 3
MAX_WAIT = 120
BUSY_STREAK = 3
# No request starts after this many seconds: the step is killed at 9 minutes and needs time to publish.
DEADLINE = 7 * 60
REQUEST_TIMEOUT = aemet.REQUEST_TIMEOUT
DAY_KEYS = ('date', 'max', 'min', 'sky', 'skyCode', 'rainProbability')
CHECKPOINT = 'forecasts.checkpoint.json'


def log(message):
    print(message, flush=True)


def get_json(url, timeout=REQUEST_TIMEOUT):
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'TiempoEn-forecast-snapshot'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise aemet.Busy(aemet.retry_after_seconds(error.headers)) from None
        raise


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def is_timeout(error):
    return isinstance(error, (TimeoutError, socket.timeout)) or (
        isinstance(error, urllib.error.URLError) and isinstance(error.reason, (TimeoutError, socket.timeout)))


def fetch_city(slug, get=get_json, direct=None, timeout=REQUEST_TIMEOUT):
    """Returns (entry or None, reason, retry_after). direct asks AEMET itself; on any failure but a 429
    the site is asked instead. A 429 is reported as 'busy' and never followed by another request here."""
    data = None
    if direct:
        try:
            data = direct(slug, timeout)
        except aemet.Busy as busy:
            return None, 'busy', busy.retry_after
        except Exception as error:
            log(f'  {slug}: direct AEMET failed ({"timeout" if is_timeout(error) else type(error).__name__}); asking the site')
    if data is None:
        try:
            data = get(f'{SITE}/api/prevision?ciudad={slug}', timeout=timeout)
        except aemet.Busy as busy:
            return None, 'busy', busy.retry_after
        except Exception as error:
            return None, 'timeout' if is_timeout(error) else f'error ({type(error).__name__})', None
    days = (data.get('daily') or {}).get('days')
    if not data.get('ok') or not days or data.get('stale'):
        return None, data.get('reason', 'stale' if data.get('stale') else 'no data'), None
    return {'daily': data['daily'], 'hourly': data.get('hourly'), 'fetchedAt': now_iso()}, 'ok', None


def is_recent(previous, now):
    try:
        generated = datetime.strptime(previous['generatedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - generated < MIN_AGE


def retry_wait(attempt, retry_after):
    """Seconds to wait before attempt+1 (attempt counts from 1), or None to give the city up."""
    wait = COOLDOWN * 2 ** (attempt - 1)
    if retry_after is not None:
        if retry_after > MAX_WAIT:
            return None
        wait = max(wait, retry_after)
    return min(wait, MAX_WAIT)


def build(slugs, previous, get=get_json, sleep=time.sleep, clock=time.monotonic, direct=None,
          deadline=DEADLINE, stop=None, on_entry=None, report=None):
    """Asks for every city within the deadline. report (a dict) receives refreshed, kept and stopped."""
    started = clock()
    remaining = lambda: deadline - (clock() - started)
    stopped = lambda: stop is not None and stop.is_set()
    cities = {slug: entry for slug, entry in (previous.get('cities') or {}).items() if slug in slugs}
    refreshed, kept, why_stop, busy_streak = [], [], None, 0
    for index, slug in enumerate(slugs):
        if stopped():
            why_stop = 'interrupted'
            break
        if remaining() <= REQUEST_TIMEOUT:
            why_stop = 'deadline'
            break
        if busy_streak >= BUSY_STREAK:
            why_stop = 'aemet-saturated'
            break
        if index:
            sleep(PAUSE)
        outcome = None
        for attempt in range(1, ATTEMPTS + 1):
            entry, reason, retry_after = fetch_city(slug, get, direct, timeout=max(1, min(REQUEST_TIMEOUT, remaining())))
            outcome = reason
            if entry:
                cities[slug] = entry
                refreshed.append(slug)
                if on_entry:
                    on_entry(slug, entry)
                break
            wait = retry_wait(attempt, retry_after) if attempt < ATTEMPTS else None
            if wait is None or stopped() or remaining() - wait <= REQUEST_TIMEOUT:
                break
            log(f'  {slug}: {reason}; retrying in {wait} s')
            sleep(wait)
        busy_streak = busy_streak + 1 if outcome == 'busy' else 0
        if outcome != 'ok':
            kept.append(slug)
        log(f'[{index + 1}/{len(slugs)}] {slug}: {"ok" if outcome == "ok" else "kept previous (" + str(outcome) + ")"} - {int(clock() - started)} s')
    if report is not None:
        report.update(refreshed=refreshed, kept=kept, stopped=why_stop,
                      not_asked=[s for s in slugs if s not in refreshed and s not in kept])
    return {'version': 1, 'generatedAt': now_iso(), 'source': 'AEMET OpenData', 'cities': cities}


def home_view(forecasts, home_slugs):
    """The home map only needs the daily summary of its own cities; no extra AEMET requests."""
    cities = {}
    for slug in home_slugs:
        entry = (forecasts.get('cities') or {}).get(slug)
        days = ((entry or {}).get('daily') or {}).get('days')
        if not days:
            continue
        cities[slug] = {
            'days': [{key: day.get(key) for key in DAY_KEYS} for day in days],
            'issued': entry['daily'].get('issued'),
            'fetchedAt': entry['fetchedAt'],
        }
    return {'version': 1, 'generatedAt': forecasts['generatedAt'], 'source': 'AEMET OpenData', 'cities': cities}


def problems(snapshot, slugs, previous):
    """Reasons why a snapshot must not replace the published one (empty list: valid)."""
    issues = []
    if snapshot.get('version') != 1 or not isinstance(snapshot.get('cities'), dict) or not snapshot.get('generatedAt'):
        return ['malformed snapshot']
    cities = snapshot['cities']
    before = previous.get('cities') or {}
    for slug, entry in cities.items():
        days = ((entry or {}).get('daily') or {}).get('days')
        if not isinstance(days, list) or not days or not all(isinstance(d, dict) and d.get('date') for d in days):
            issues.append(f'{slug}: no daily forecast')
        if not (entry or {}).get('fetchedAt'):
            issues.append(f'{slug}: no fetchedAt')
        elif slug in before and before[slug].get('fetchedAt') and entry['fetchedAt'] < before[slug]['fetchedAt']:
            issues.append(f'{slug}: older than the published forecast')
    if before:
        missing = [slug for slug in slugs if slug not in cities]
        if missing:
            issues.append(f'incomplete: {len(missing)} cities missing ({", ".join(missing[:5])})')
    elif not cities:
        issues.append('no city available')
    return issues


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
    os.replace(temporary, path)


def publish(folder, forecasts, home, slugs, previous):
    """Temporary files, validation of what was written, then atomic replacement. True when published."""
    issues = problems(forecasts, slugs, previous)
    if issues:
        log('Not published, the last valid snapshot stays: ' + '; '.join(issues[:5]))
        return False
    staged = []
    try:
        for name, data in (('forecasts.json', forecasts), ('home-weather.json', home)):
            temporary = folder / (name + '.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
            json.loads(temporary.read_text(encoding='utf8'))
            staged.append((temporary, folder / name))
        if problems(json.loads(staged[0][0].read_text(encoding='utf8')), slugs, previous):
            raise ValueError('written snapshot does not validate')
        for temporary, final in staged:
            os.replace(temporary, final)
        return True
    except (OSError, ValueError) as error:
        log(f'Not published ({error}); the last valid snapshot stays')
        return False
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()


def load_json(path):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        return {}


def with_checkpoint(previous, checkpoint):
    """Cities saved by an unfinished run are reused when newer than the published ones."""
    cities = dict(previous.get('cities') or {})
    for slug, entry in (checkpoint.get('cities') or {}).items():
        if (entry or {}).get('fetchedAt') and ((entry.get('daily') or {}).get('days')) and \
                entry['fetchedAt'] > (cities.get(slug) or {}).get('fetchedAt', ''):
            cities[slug] = entry
    return {**previous, 'cities': cities}


def fetched_time(entry):
    """When the forecast of a city was obtained; None when missing or not a valid timestamp."""
    try:
        return datetime.strptime((entry or {}).get('fetchedAt') or '', '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def priority_order(slugs, base):
    """Oldest data first, so a run cut by the deadline never leaves the same cities behind twice.
    A city without a valid date goes first; ties keep the order of the site's listing (then the slug)."""
    cities = base.get('cities') or {}
    position = {slug: index for index, slug in enumerate(slugs)}

    def key(slug):
        when = fetched_time(cities.get(slug))
        return (when is not None, when or datetime.min.replace(tzinfo=timezone.utc), position[slug], slug)

    return sorted(dict.fromkeys(slugs), key=key)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=os.environ.get('RADAR_DATA_DIR', 'data'))
    args = parser.parse_args()
    folder = Path(args.output)
    previous = load_json(folder / 'forecasts.json')
    if is_recent(previous, datetime.now(timezone.utc)):
        log('Snapshot is recent; nothing to do')
        return 0
    stop = threading.Event()

    def interrupted(signum, _frame):
        log(f'Signal {signum}: stopping; the last valid snapshot stays')
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    checkpoint_path = folder / CHECKPOINT
    base = with_checkpoint(previous, load_json(checkpoint_path))
    listing = get_json(f'{SITE}/api/mapa-ciudades')
    slugs = listing.get('all') or listing['cities']
    direct = None
    key = os.environ.get('AEMET_API_KEY', '').strip()
    ids = listing.get('ids') or {}
    if key and ids:
        log('Asking AEMET directly (key from the repository secret)')
        direct = lambda slug, timeout: aemet.forecast(ids[slug], key, timeout=timeout) if slug in ids else None
    progress = {'version': 1, 'cities': dict(load_json(checkpoint_path).get('cities') or {})}

    def save_checkpoint(slug, entry):
        progress['cities'][slug] = entry
        progress['updatedAt'] = now_iso()
        write_json(checkpoint_path, progress)

    order = priority_order(slugs, base)
    log(f'Order, oldest data first: {", ".join(order[:5])}{" ..." if len(order) > 5 else ""}')
    report = {}
    forecasts = build(order, base, direct=direct, sleep=stop.wait, stop=stop, on_entry=save_checkpoint, report=report)
    log(f"Run: {len(report['refreshed'])} refreshed, {len(report['kept'])} kept, {len(report['not_asked'])} not asked"
        f"{', stopped: ' + report['stopped'] if report['stopped'] else ''}")
    if report['stopped'] == 'interrupted':
        return 1
    if not report['refreshed']:
        log('No city refreshed; the last valid snapshot stays')
        return 1
    if not publish(folder, forecasts, home_view(forecasts, listing['cities']), slugs, previous):
        return 1
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    log(f"Snapshot ready: {len(forecasts['cities'])}/{len(slugs)} cities")
    return 0


if __name__ == '__main__':
    sys.exit(main())
