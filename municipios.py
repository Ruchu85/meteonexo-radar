"""Forecast of every Spanish municipality from AEMET's two "todos" bundles, one file per municipality.

AEMET publishes the daily (7 days) and hourly (about 48 h) forecast of all ~8,100 municipalities as
two compressed tar files: two API requests instead of two per municipality. The site reads
municipios/<INE code>.json instead of calling AEMET when somebody (or a robot) opens a municipality,
so visits no longer use the shared AEMET quota.

- Runs at most once per MIN_AGE; otherwise does nothing.
- The bundles use AEMET's XML layout turned into JSON (root wrapper, snake_case names, "valor",
  single elements not wrapped in lists). from_bundle() turns a document into the layout of the
  per-municipality endpoint, so the same parser (aemet.parse_daily / parse_hourly) is used.
- Files are written to a temporary folder, validated and swapped in; on any failure (429, timeout,
  too few municipalities) the previous folder stays as it is.
"""
import argparse
import gzip
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import aemet

MIN_AGE = timedelta(minutes=55)
DOWNLOAD_TIMEOUT = 60
# AEMET's catalogue has about 8,120 municipalities; far fewer means a broken bundle.
MIN_MUNICIPALITIES = 8000
FOLDER = 'municipios'
RENAMED = {
    'prob_precipitacion': 'probPrecipitacion', 'estado_cielo': 'estadoCielo', 'cota_nieve_prov': 'cotaNieveProv',
    'racha_max': 'rachaMax', 'sens_termica': 'sensTermica', 'humedad_relativa': 'humedadRelativa', 'uv_max': 'uvMax',
    'prob_tormenta': 'probTormenta', 'prob_nieve': 'probNieve', 'valor': 'value',
}
LISTS = {'dia', 'dato', 'probPrecipitacion', 'estadoCielo', 'cotaNieveProv', 'rachaMax', 'viento', 'precipitacion',
         'nieve', 'probTormenta', 'probNieve'}


def log(message):
    print(message, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def from_bundle(node):
    """AEMET's bundle layout (XML turned into JSON) to the layout of the per-municipality endpoint."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            name = RENAMED.get(key, key)
            value = from_bundle(value)
            if name in LISTS and isinstance(value, dict):
                value = [value]
            out[name] = value
        return out
    if isinstance(node, list):
        return [from_bundle(item) for item in node]
    return node


def read_bytes(url, key, timeout=DOWNLOAD_TIMEOUT):
    request = urllib.request.Request(url, headers={'api_key': key, 'User-Agent': 'TiempoEn-municipios'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise aemet.Busy(aemet.retry_after_seconds(error.headers)) from None
        raise


def download(kind, key, read_json=aemet.read_json, read=read_bytes):
    """kind is 'diaria' or 'horaria'. Returns the bundle bytes (a gzip-compressed tar)."""
    first = read_json(f'{aemet.ROOT}/prediccion/especifica/municipio/{kind}/todos', key, timeout=aemet.REQUEST_TIMEOUT)
    if first.get('estado') == 429:
        raise aemet.Busy()
    if not first.get('datos') or (first.get('estado') or 0) >= 400:
        raise RuntimeError(f'AEMET {kind}/todos: {first.get("descripcion", "no data")}')
    url = urlparse(first['datos'])
    if url.scheme != 'https' or url.hostname != 'opendata.aemet.es' or not url.path.startswith('/opendata/'):
        raise RuntimeError('Unexpected AEMET data URL')
    return read(first['datos'], key)


def documents(bundle):
    """{INE code: municipality document} from a gzip-compressed tar of localidad_<code>.json files."""
    archive = tarfile.open(fileobj=io.BytesIO(gzip.decompress(bundle)))
    found = {}
    for member in archive.getmembers():
        match = re.search(r'(\d{5})\.json$', member.name)
        if not match or not member.isfile():
            continue
        doc = json.loads(archive.extractfile(member).read().decode('utf-8'))
        found[match.group(1)] = from_bundle(doc.get('root', doc))
    return found


def entries(daily_docs, hourly_docs, fetched_at):
    """Parsed forecast per municipality; a municipality without a valid daily forecast is left out."""
    result, skipped = {}, []
    for code, doc in daily_docs.items():
        try:
            daily = aemet.parse_daily([doc], code)
            hourly = aemet.parse_hourly([hourly_docs[code]], code) if code in hourly_docs else None
        except Exception as error:
            skipped.append(f'{code} ({type(error).__name__})')
            continue
        if not daily or not daily.get('days'):
            skipped.append(f'{code} (no days)')
            continue
        result[code] = {'daily': daily, 'hourly': hourly, 'fetchedAt': fetched_at}
    return result, skipped


def problems(result, previous_count):
    issues = []
    if len(result) < MIN_MUNICIPALITIES:
        issues.append(f'only {len(result)} municipalities')
    if previous_count and len(result) < 0.95 * previous_count:
        issues.append(f'{len(result)} municipalities, previous publication had {previous_count}')
    return issues


def load_index(folder):
    try:
        return json.loads((folder / FOLDER / 'index.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        return {}


def is_recent(index, now):
    try:
        generated = datetime.strptime(index['generatedAt'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return now - generated < MIN_AGE


def publish(folder, result, index):
    """Write every file to a temporary folder, check it, then swap it in. True when published."""
    target, staging, old = folder / FOLDER, folder / (FOLDER + '.new'), folder / (FOLDER + '.old')
    for leftover in (staging, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    staging.mkdir(parents=True)
    try:
        for code, entry in result.items():
            (staging / f'{code}.json').write_text(json.dumps(entry, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
        (staging / 'index.json').write_text(json.dumps(index, ensure_ascii=False, separators=(',', ':')), encoding='utf8')
        written = sum(1 for path in staging.glob('*.json') if path.name != 'index.json')
        if written != len(result):
            raise ValueError(f'{written} files written for {len(result)} municipalities')
        sample = json.loads((staging / f'{next(iter(result))}.json').read_text(encoding='utf8'))
        if not sample.get('daily', {}).get('days'):
            raise ValueError('written file without forecast days')
        if target.exists():
            target.rename(old)
        staging.rename(target)
        if old.exists():
            shutil.rmtree(old)
        return True
    except (OSError, ValueError) as error:
        log(f'Not published ({error}); the previous municipality files stay')
        if not target.exists() and old.exists():
            old.rename(target)
        return False
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=os.environ.get('RADAR_DATA_DIR', 'data'))
    args = parser.parse_args()
    folder = Path(args.output)
    previous = load_index(folder)
    if is_recent(previous, datetime.now(timezone.utc)):
        log(f"Municipality files are recent ({previous['generatedAt']}); nothing to do")
        return 0
    key = os.environ.get('AEMET_API_KEY', '').strip()
    if not key:
        log('No AEMET_API_KEY; nothing to do')
        return 1
    try:
        bundles = {kind: download(kind, key) for kind in ('diaria', 'horaria')}
    except aemet.Busy as busy:
        log(f'AEMET busy (429){f", retry after {busy.retry_after} s" if busy.retry_after else ""}; the previous files stay')
        return 1
    except Exception as error:
        log(f'Download failed ({type(error).__name__}: {error}); the previous files stay')
        return 1
    fetched_at = now_iso()
    daily_docs, hourly_docs = documents(bundles['diaria']), documents(bundles['horaria'])
    result, skipped = entries(daily_docs, hourly_docs, fetched_at)
    issued = sorted({entry['daily'].get('issued') for entry in result.values() if entry['daily'].get('issued')})
    log(f'Bundles: {len(daily_docs)} daily, {len(hourly_docs)} hourly; {len(result)} usable, {len(skipped)} skipped'
        f'{" (" + ", ".join(skipped[:5]) + ")" if skipped else ""}; issued {issued[0] if issued else "?"} to {issued[-1] if issued else "?"}')
    issues = problems(result, previous.get('count'))
    if issues:
        log('Not published, the previous files stay: ' + '; '.join(issues))
        return 1
    index = {'version': 1, 'generatedAt': fetched_at, 'source': 'AEMET OpenData', 'count': len(result),
             'issuedFrom': issued[0] if issued else None, 'issuedTo': issued[-1] if issued else None}
    if not publish(folder, result, index):
        return 1
    log(f'Municipality files ready: {len(result)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
