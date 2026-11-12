"""Direct AEMET OpenData access for the forecast snapshot job.

AEMET answers 429 to the shared egress addresses of the site's host, so the job can ask AEMET itself
(from GitHub's runners, with the key stored as a repository secret). parse_daily and parse_hourly are a
line-by-line port of parseDaily/parseHourly in the site's src/weather.mjs; tests/fixtures/aemet_parity.json
holds the site parser's own output for a rich sample and tests/test_aemet.py requires the same result.
"""
import json
import math
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = 'https://opendata.aemet.es/opendata/api'


class Busy(Exception):
    """AEMET answered 429: wait before asking again."""


def number(value):
    if value is None or value in ('', '-', 'Ip') or isinstance(value, (dict, list, bool)):
        return None
    try:
        n = float(str(value).replace(',', '.'))
    except ValueError:
        return None
    if not math.isfinite(n) or '_' in str(value):
        return None
    return int(n) if n == int(n) else n


def first_number(value):
    if isinstance(value, list):
        return first_number(value[0] if value else None)
    return number(value)


def has_value(x):
    return bool(x) and bool(x.get('descripcion') or ('value' in x and x['value'] != '') or 'velocidad' in x)


def best_period(items):
    if not isinstance(items, list):
        return None
    for wanted in ('00-24', '12-24'):
        for x in items:
            if x.get('periodo') == wanted and has_value(x):
                return x
    for x in items:
        if has_value(x):
            return x
    return None


def max_probability(items):
    if not isinstance(items, list):
        return None
    for x in items:
        if x.get('periodo') == '00-24' and number(x.get('value')) is not None:
            return number(x['value'])
    values = [v for v in (number(x.get('value')) for x in items) if v is not None]
    return max(values) if values else None


def max_value(items):
    if not isinstance(items, list):
        return None
    values = [v for v in (number(x.get('value')) for x in items) if v is not None]
    return max(values) if values else None


def wind_value(x):
    if not x:
        return {'speed': None, 'direction': None}
    direction = x.get('direccion')
    if isinstance(direction, list):
        direction = direction[0] if direction else None
    return {'speed': first_number(x.get('velocidad')), 'direction': direction if isinstance(direction, str) and direction else None}


def id_matches(doc, requested):
    ident = doc.get('id')
    ident = '' if ident is None else str(ident)
    return not requested or not re.fullmatch(r'\d{1,5}', ident) or ident.zfill(5) == requested


def find_period(items, ident):
    if not isinstance(items, list):
        return None
    return next((x for x in items if x.get('periodo') == ident), None)


def day_periods(day):
    sky_items = day.get('estadoCielo') or []

    def has_all(ids):
        return all(any(x.get('periodo') == i for x in sky_items) for i in ids)

    ids = ['00-06', '06-12', '12-18', '18-24'] if has_all(['00-06', '06-12', '12-18', '18-24']) \
        else ['00-12', '12-24'] if has_all(['00-12', '12-24']) else []
    result = []
    for ident in ids:
        sky = find_period(day.get('estadoCielo'), ident)
        wind = wind_value(find_period(day.get('viento'), ident))
        item = {
            'period': ident,
            'sky': (sky or {}).get('descripcion') or '',
            'skyCode': (sky or {}).get('value') or '',
            'rainProbability': number((find_period(day.get('probPrecipitacion'), ident) or {}).get('value')),
            'windSpeed': wind['speed'],
            'windDirection': wind['direction'],
            'gust': number((find_period(day.get('rachaMax'), ident) or {}).get('value')),
        }
        if item['sky'] or item['rainProbability'] is not None or item['windSpeed'] is not None:
            result.append(item)
    return result


def parse_daily(source, requested_id):
    doc = source[0] if isinstance(source, list) and source else source
    if not isinstance(doc, dict) or not isinstance(doc.get('prediccion'), dict) or not isinstance(doc['prediccion'].get('dia'), list):
        return None
    if not id_matches(doc, requested_id):
        return None
    days = []
    for day in doc['prediccion']['dia']:
        date = str(day.get('fecha') or '')[:10]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
            continue
        sky = best_period(day.get('estadoCielo'))
        wind = wind_value(best_period(day.get('viento')))
        temp, feels, humidity = (day.get(k) or {} for k in ('temperatura', 'sensTermica', 'humedadRelativa'))
        days.append({
            'date': date,
            'max': number(temp.get('maxima')),
            'min': number(temp.get('minima')),
            'sky': (sky or {}).get('descripcion') or '',
            'skyCode': (sky or {}).get('value') or '',
            'rainProbability': max_probability(day.get('probPrecipitacion')),
            'windSpeed': wind['speed'],
            'windDirection': wind['direction'],
            'humidityMin': number(humidity.get('minima')),
            'humidityMax': number(humidity.get('maxima')),
            'uv': number(day.get('uvMax')),
            'snow': (best_period(day.get('cotaNieveProv')) or {}).get('value') or None,
            'feelsMax': number(feels.get('maxima')),
            'feelsMin': number(feels.get('minima')),
            'gust': max_value(day.get('rachaMax')),
            'periods': day_periods(day),
        })
    origin = doc.get('origen') or {}
    return {'days': days, 'issued': doc.get('elaborado') or None, 'source': origin.get('productor') or 'AEMET'}


_SKIP = object()


def parse_hourly(source, requested_id):
    doc = source[0] if isinstance(source, list) and source else source
    if not isinstance(doc, dict) or not isinstance(doc.get('prediccion'), dict) or not isinstance(doc['prediccion'].get('dia'), list):
        return None
    if not id_matches(doc, requested_id):
        return None
    hours, sun = [], {}
    carry = {}
    for day in doc['prediccion']['dia']:
        if not re.match(r'\d{4}-\d{2}-\d{2}', day.get('fecha') or ''):
            continue
        date = day['fecha'][:10]
        slots, next_carry = {}, {}
        if re.fullmatch(r'\d{2}:\d{2}', day.get('orto') or '') and re.fullmatch(r'\d{2}:\d{2}', day.get('ocaso') or ''):
            sun[date] = {'rise': day['orto'], 'set': day['ocaso']}

        def ingest(entries, key, mapper=lambda entry: number(entry.get('value'))):
            if not isinstance(entries, list):
                return
            for entry in entries:
                hour = str(entry.get('periodo') or '').rjust(2, '0')
                if not re.fullmatch(r'\d{2}', hour) or int(hour) > 23:
                    continue
                slots.setdefault(hour, {})
                value = mapper(entry)
                if value is not _SKIP:
                    slots[hour][key] = value

        def ingest_spans(entries, key):
            if not isinstance(entries, list):
                return
            for entry in entries:
                m = re.fullmatch(r'(\d{2})(\d{2})', str(entry.get('periodo') or ''))
                value = number(entry.get('value'))
                if not m or value is None:
                    continue
                start, end = int(m[1]), int(m[2])
                if start > 23 or end > 24:
                    continue
                stop = end if end > start else 24
                for h in range(start, stop):
                    slots.setdefault(str(h).rjust(2, '0'), {})[key] = value
                if end <= start:
                    for h in range(end):
                        next_carry[str(h).rjust(2, '0')] = value

        wind = lambda entry: _SKIP if 'velocidad' not in entry else first_number(entry['velocidad'])
        ingest(day.get('temperatura'), 'temp')
        ingest(day.get('sensTermica'), 'feelsLike')
        ingest(day.get('probPrecipitacion'), 'rainProbability')
        ingest_spans(day.get('probPrecipitacion'), 'rainProbability')
        for hour, value in carry.items():
            if hour in slots and 'rainProbability' not in slots[hour]:
                slots[hour]['rainProbability'] = value
        ingest(day.get('precipitacion'), 'rainMm')
        ingest(day.get('humedadRelativa'), 'humidity')
        ingest(day.get('estadoCielo'), 'sky', lambda x: x.get('descripcion') or '')
        ingest(day.get('estadoCielo'), 'skyCode', lambda x: x.get('value') or '')
        ingest(day.get('vientoAndRachaMax'), 'windSpeed', wind)
        ingest(day.get('viento'), 'windSpeed', wind)
        for hour, values in slots.items():
            hours.append({'date': date, 'time': hour + ':00', **values})
        carry = next_carry
    hours.sort(key=lambda h: h['date'] + h['time'])
    return {'hours': hours, 'sun': sun, 'issued': doc.get('elaborado') or None}


def read_json(url, key, timeout=40):
    request = urllib.request.Request(url, headers={'api_key': key, 'Accept': 'application/json', 'User-Agent': 'TiempoEn-forecast-snapshot'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            # AEMET data files declare ISO-8859-15, not UTF-8.
            match = re.search(r'charset=([\w-]+)', response.headers.get('Content-Type') or '', re.I)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise Busy() from None
        raise
    try:
        return json.loads(body.decode(match.group(1) if match else 'utf-8'))
    except LookupError:
        return json.loads(body.decode('utf-8'))


def request(endpoint, key, read=read_json):
    first = read(ROOT + endpoint, key)
    if first.get('estado') == 429:
        raise Busy()
    if not first.get('datos') or (first.get('estado') or 0) >= 400:
        raise RuntimeError('AEMET service error')
    url = urlparse(first['datos'])
    if url.scheme != 'https' or url.hostname != 'opendata.aemet.es' or not url.path.startswith('/opendata/'):
        raise RuntimeError('Unexpected AEMET data URL')
    return read(first['datos'], key)


def forecast(ident, key, read=read_json):
    """Daily and hourly forecast of an INE municipality, in the format of the site's /api/prevision."""
    daily = parse_daily(request(f'/prediccion/especifica/municipio/diaria/{ident}', key, read), ident)
    hourly = parse_hourly(request(f'/prediccion/especifica/municipio/horaria/{ident}', key, read), ident)
    return {'ok': bool(daily and daily['days']), 'daily': daily, 'hourly': hourly}
