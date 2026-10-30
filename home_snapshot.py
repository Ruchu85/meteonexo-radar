"""Builds home-weather.json: the forecast behind the TiempoEn home map.

AEMET rejects bursts, so the site cannot ask for every city while a visitor waits. This step asks
the site for one city at a time, slowly, and publishes the result. The AEMET key stays on the site.
A city that fails keeps its previous forecast until the next run.
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SITE = os.environ.get('HOME_SITE_URL', 'https://tiempoen.es')
MIN_AGE = timedelta(minutes=25)
PAUSE = 5
RETRY_PAUSE = 35


def get_json(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'TiempoEn-home-snapshot'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_city(slug, get=get_json):
    try:
        data = get(f'{SITE}/api/semana?ciudad={slug}&fresh=1')
    except Exception as error:
        print(f'{slug}: {error}')
        return None
    if not data.get('ok') or not data.get('days') or data.get('stale'):
        print(f"{slug}: {data.get('reason', 'stale' if data.get('stale') else 'no data')}")
        return None
    return {'days': data['days'], 'issued': data.get('issued'), 'fetchedAt': now_iso()}


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def is_recent(previous, now):
    try:
        generated = datetime.strptime(previous['generatedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - generated < MIN_AGE


def build(slugs, previous, get=get_json, sleep=time.sleep):
    cities = {slug: entry for slug, entry in (previous.get('cities') or {}).items() if slug in slugs}
    missing = []
    for index, slug in enumerate(slugs):
        if index:
            sleep(PAUSE)
        entry = fetch_city(slug, get)
        if entry:
            cities[slug] = entry
        else:
            missing.append(slug)
    if missing:
        sleep(RETRY_PAUSE)
        for slug in missing:
            entry = fetch_city(slug, get)
            if entry:
                cities[slug] = entry
            sleep(PAUSE)
    return {'version': 1, 'generatedAt': now_iso(), 'source': 'AEMET OpenData', 'cities': cities}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=os.environ.get('RADAR_DATA_DIR', 'data'))
    args = parser.parse_args()
    target = Path(args.output) / 'home-weather.json'
    try:
        previous = json.loads(target.read_text(encoding='utf8'))
    except (OSError, ValueError):
        previous = {}
    if is_recent(previous, datetime.now(timezone.utc)):
        print('Snapshot is recent; nothing to do')
        return
    slugs = get_json(f'{SITE}/api/mapa-ciudades')['cities']
    snapshot = build(slugs, previous)
    if not snapshot['cities']:
        raise SystemExit('No city available')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
    print(f"Snapshot ready: {len(snapshot['cities'])}/{len(slugs)} cities")


if __name__ == '__main__':
    main()
