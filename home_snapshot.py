"""Builds forecasts.json (every indexable city) and home-weather.json (the home map) for TiempoEn.

AEMET rejects bursts, so the site cannot ask for dozens of cities while visitors or crawlers wait.
This step asks the site for one city at a time, slowly, and publishes the result; the site reads it
before it goes to AEMET. The AEMET key stays on the site. A city that fails keeps its previous
forecast, and the site judges each city's age on its own.
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aemet

SITE = os.environ.get('HOME_SITE_URL', 'https://tiempoen.es')
MIN_AGE = timedelta(minutes=25)
PAUSE = 5
# After a 429 the site stops calling AEMET for about 30 seconds: wait that long before asking again.
COOLDOWN = 35
# The workflow step stops at 9 minutes; no new retries after this many seconds.
BUDGET = 8 * 60
DAY_KEYS = ('date', 'max', 'min', 'sky', 'skyCode', 'rainProbability')


def get_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'TiempoEn-forecast-snapshot'})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fetch_city(slug, get=get_json, direct=None):
    """direct (optional) asks AEMET itself; when it fails for any reason other than 429, the site is asked."""
    data = None
    if direct:
        try:
            data = direct(slug)
        except aemet.Busy:
            print(f'{slug}: AEMET busy (429)')
            return None
        except Exception as error:
            print(f'{slug}: direct AEMET failed ({type(error).__name__}); asking the site')
    if data is None:
        try:
            data = get(f'{SITE}/api/prevision?ciudad={slug}')
        except Exception as error:
            print(f'{slug}: {error}')
            return None
    days = (data.get('daily') or {}).get('days')
    if not data.get('ok') or not days or data.get('stale'):
        print(f"{slug}: {data.get('reason', 'stale' if data.get('stale') else 'no data')}")
        return None
    return {'daily': data['daily'], 'hourly': data.get('hourly'), 'fetchedAt': now_iso()}


def is_recent(previous, now):
    try:
        generated = datetime.strptime(previous['generatedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - generated < MIN_AGE


def build(slugs, previous, get=get_json, sleep=time.sleep, clock=time.monotonic, direct=None):
    started = clock()
    cities = {slug: entry for slug, entry in (previous.get('cities') or {}).items() if slug in slugs}
    for index, slug in enumerate(slugs):
        if index:
            sleep(PAUSE)
        for attempt in range(3):
            entry = fetch_city(slug, get, direct)
            if entry:
                cities[slug] = entry
                break
            if attempt < 2 and clock() - started < BUDGET:
                sleep(COOLDOWN)
            else:
                break
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


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=os.environ.get('RADAR_DATA_DIR', 'data'))
    args = parser.parse_args()
    folder = Path(args.output)
    try:
        previous = json.loads((folder / 'forecasts.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        previous = {}
    if is_recent(previous, datetime.now(timezone.utc)):
        print('Snapshot is recent; nothing to do')
        return
    listing = get_json(f'{SITE}/api/mapa-ciudades')
    slugs = listing.get('all') or listing['cities']
    direct = None
    key = os.environ.get('AEMET_API_KEY', '').strip()
    ids = listing.get('ids') or {}
    if key and ids:
        print('Asking AEMET directly (key from the repository secret)')
        direct = lambda slug: aemet.forecast(ids[slug], key) if slug in ids else None
    forecasts = build(slugs, previous, direct=direct)
    if not forecasts['cities']:
        raise SystemExit('No city available')
    write(folder / 'forecasts.json', forecasts)
    write(folder / 'home-weather.json', home_view(forecasts, listing['cities']))
    print(f"Snapshot ready: {len(forecasts['cities'])}/{len(slugs)} cities")


if __name__ == '__main__':
    main()
