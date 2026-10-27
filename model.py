#!/usr/bin/env python3
"""ICON-EU (DWD open data) precipitation + cloud forecast -> Web Mercator PNG sequence.

Source: Deutscher Wetterdienst open data, ICON-EU regular lat-lon grid (0.0625 deg, ~6.5 km),
hourly to +78 h and 3-hourly to +120 h for the 00/06/12/18 UTC runs. Licence: free use including
commercial, attribution "Quelle: Deutscher Wetterdienst" (GeoNutzV / DL-DE-BY-2.0).

Output (in <output>/model): one PNG per valid time with clouds (grey) under precipitation rate
(mm/h colour scale), plus manifest.json. A run is processed once; later cycles reuse the files.
A failed or incomplete run never replaces the previous valid manifest.
"""
from __future__ import annotations
import argparse
import bz2
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlparse

import numpy as np
from PIL import Image
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import reproject, transform, Resampling

UTC = dt.timezone.utc
SOURCE = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib/'
RUNS = ('00', '06', '12', '18')               # the runs that reach +120 h
STEPS = list(range(0, 79)) + list(range(81, 121, 3))
BOUNDS = (-11.5, 34.5, 5.5, 45.6)             # same extent as the radar, so the viewer reuses it
WIDTH = 1024
# Precipitation rate (mm/h) colour scale: drizzle -> heavy showers -> extreme.
RATE_STOPS = np.array([0.1, 0.5, 1, 2, 4, 8, 15, 30], dtype=np.float32)
RATE_COLORS = np.array([[168, 214, 240], [110, 180, 232], [55, 135, 215], [30, 90, 190],
                        [250, 210, 70], [245, 140, 50], [220, 50, 70], [160, 60, 170]], dtype=np.float32)
FILE_RE = re.compile(r'icon-eu_europe_regular-lat-lon_single-level_(\d{10})_(\d{3})_TOT_PREC\.grib2\.bz2')
LOG = logging.getLogger('meteonexo.model')
# Bump when the rendering changes: an already processed run is rendered again with the new look.
RENDER_VERSION = 2


def iso(value): return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def session():
    s = requests.Session()
    s.headers.update({'User-Agent': 'MeteoNexo-Model/1.0 (+DWD-opendata-consumer)'})
    s.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])))
    return s


def fetch(s, url, limit=16 * 1024 * 1024):
    # Only DWD's open-data tree can be requested; every URL is built here.
    u = urlparse(url)
    if u.scheme != 'https' or u.netloc != 'opendata.dwd.de' or not u.path.startswith('/weather/nwp/icon-eu/grib/'):
        raise ValueError('Unexpected source URL')
    r = s.get(url, timeout=(15, 120), allow_redirects=False)
    r.raise_for_status()
    if len(r.content) > limit:
        raise ValueError('Source file exceeds limit')
    return r.content


def complete_runs(s):
    """Runs (datetime) whose TOT_PREC directory already lists every step up to +120 h."""
    found = []
    for hh in RUNS:
        listing = fetch(s, f'{SOURCE}{hh}/tot_prec/', limit=4 * 1024 * 1024).decode('utf8', 'replace')
        by_run = {}
        for run, step in FILE_RE.findall(listing):
            by_run.setdefault(run, set()).add(int(step))
        for run, steps in by_run.items():
            if set(STEPS) <= steps:
                found.append(dt.datetime.strptime(run, '%Y%m%d%H').replace(tzinfo=UTC))
    return sorted(set(found))


def decode(grib_bz2):
    """GRIB2 message -> (values[nj, ni] north-up, west, north, dx, dy)."""
    import eccodes
    h = eccodes.codes_new_from_message(bz2.decompress(grib_bz2))
    try:
        g = lambda k: eccodes.codes_get(h, k)
        ni, nj = g('Ni'), g('Nj')
        if ni * nj > 4_000_000:
            raise ValueError('Unexpected grid size')
        vals = eccodes.codes_get_values(h).astype(np.float32).reshape(nj, ni)
        missing = g('missingValue')
        vals[vals == missing] = np.nan
        lat0, lat1 = g('latitudeOfFirstGridPointInDegrees'), g('latitudeOfLastGridPointInDegrees')
        lon0 = g('longitudeOfFirstGridPointInDegrees')
        if lon0 > 180:
            lon0 -= 360
        dx, dy = g('iDirectionIncrementInDegrees'), g('jDirectionIncrementInDegrees')
        if g('jScansPositively') == 1:          # south-up -> north-up
            vals = vals[::-1]
        north = max(lat0, lat1)
        return vals, lon0, north, dx, dy
    finally:
        eccodes.codes_release(h)


def crop(vals, west, north, dx, dy, margin=1.0):
    """Cut the Iberia window (+margin) out of the European grid to keep reprojection cheap."""
    c0 = max(0, int((BOUNDS[0] - margin - west) / dx)); c1 = min(vals.shape[1], int((BOUNDS[2] + margin - west) / dx) + 1)
    r0 = max(0, int((north - (BOUNDS[3] + margin)) / dy)); r1 = min(vals.shape[0], int((north - (BOUNDS[1] - margin)) / dy) + 1)
    if c1 - c0 < 10 or r1 - r0 < 10:
        raise ValueError('Model grid does not cover the radar extent')
    # Grid values are cell centres: the outer edge is half a cell further out.
    return vals[r0:r1, c0:c1], from_origin(west + c0 * dx - dx / 2, north - r0 * dy + dy / 2, dx, dy)


def to_mercator(vals, src_transform, width=WIDTH):
    xx, yy = transform('EPSG:4326', 'EPSG:3857', [BOUNDS[0], BOUNDS[2]], [BOUNDS[1], BOUNDS[3]])
    height = round(width * (yy[1] - yy[0]) / (xx[1] - xx[0]))
    out = np.full((height, width), np.nan, dtype=np.float32)
    reproject(vals, out, src_transform=src_transform, src_crs='EPSG:4326',
              dst_transform=from_bounds(xx[0], yy[0], xx[1], yy[1], width, height), dst_crs='EPSG:3857',
              src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear)
    return out


def render(rate, clouds):
    """Clouds (total cover %) as translucent grey, precipitation rate (mm/h) coloured on top."""
    h, w = rate.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    cc = np.nan_to_num(clouds, nan=0.0)
    alpha = np.clip((cc - 20) / 80, 0, 1) * 170
    # Slate grey: visible over the light IGN base map without hiding it.
    rgba[..., 0] = 112; rgba[..., 1] = 122; rgba[..., 2] = 138
    rgba[..., 3] = alpha.astype(np.uint8)
    r = np.nan_to_num(rate, nan=0.0)
    wet = r >= RATE_STOPS[0]
    lr = np.log(np.clip(r, RATE_STOPS[0], RATE_STOPS[-1]))
    for ch in range(3):
        rgba[..., ch] = np.where(wet, np.interp(lr, np.log(RATE_STOPS), RATE_COLORS[:, ch]), rgba[..., ch]).astype(np.uint8)
    rgba[..., 3] = np.where(wet, 245, rgba[..., 3])
    return rgba, int(np.count_nonzero(wet))


def atomic_png(target, array):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.png', dir=target.parent, delete=False) as fh:
        temp = Path(fh.name)
    try:
        # A 96-colour palette (alpha kept) cuts each frame ~8x with no visible change on this colour scale.
        Image.fromarray(array).quantize(colors=96, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(temp, format='PNG', optimize=True)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(target, value):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=target.parent, delete=False) as fh:
        json.dump(value, fh, ensure_ascii=False, separators=(',', ':')); temp = Path(fh.name)
    os.replace(temp, target)


def rates(totals):
    """Accumulated precipitation per step -> mean rate (mm/h) over the interval ending at each step."""
    out, prev_step, prev = {}, None, None
    for step in sorted(totals):
        if prev is not None:
            out[step] = np.clip(totals[step] - prev, 0, None) / (step - prev_step)
        prev_step, prev = step, totals[step]
    return out


def process(s, run, output, base):
    stamp = run.strftime('%Y%m%d%H'); hh = run.strftime('%H')
    totals, clouds = {}, {}
    for step in STEPS:
        for var, store in (('tot_prec', totals), ('clct', clouds)):
            if var == 'clct' and step == 0:
                continue
            name = f'icon-eu_europe_regular-lat-lon_single-level_{stamp}_{step:03d}_{var.upper()}.grib2.bz2'
            vals, west, north, dx, dy = decode(fetch(s, f'{SOURCE}{hh}/{var}/{name}'))
            cut, src = crop(vals, west, north, dx, dy)
            store[step] = to_mercator(cut, src)
    frames = []
    for step, rate in rates(totals).items():
        valid = run + dt.timedelta(hours=step)
        rgba, wet = render(rate, clouds[step])
        name = f'{stamp}-{step:03d}.png'
        atomic_png(output / name, rgba)
        frames.append({'id': valid.strftime('%Y%m%dT%H%M%SZ'), 'time': iso(valid), 'step': step,
                       'intervalHours': 1 if step <= 78 else 3, 'image': f'{base}/model/{name}', 'wetPixels': wet})
    return frames


def manifest(run, frames, now):
    return {'version': 1, 'renderVersion': RENDER_VERSION, 'product': 'ICON-EU', 'quantity': 'precipitation_rate', 'unit': 'mm/h', 'projection': 'EPSG:3857',
            'bounds': list(BOUNDS), 'run': iso(run), 'generatedAt': iso(now), 'frames': frames,
            'source': 'Deutscher Wetterdienst (DWD), modelo ICON-EU', 'license': 'DL-DE-BY-2.0 / GeoNutzV',
            'sourceUrl': 'https://opendata.dwd.de/weather/nwp/icon-eu/',
            'processing': 'Recorte de Peninsula y Baleares, reproyeccion bilineal a Web Mercator, tasa media por intervalo y escala de color de TiempoEn.'}


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default=os.environ.get('RADAR_DATA_DIR', str(Path(__file__).resolve().parents[1] / 'data/radar')))
    p.add_argument('--public-base', default=os.environ.get('RADAR_PUBLIC_BASE_URL', '/radar-data'))
    args = p.parse_args()
    out = Path(args.output) / 'model'; out.mkdir(parents=True, exist_ok=True)
    s = session(); now = dt.datetime.now(UTC)
    runs = complete_runs(s)
    if not runs:
        LOG.warning('No complete ICON-EU run listed; keeping the previous forecast'); return 0
    run = runs[-1]
    current = out / 'manifest.json'
    previous = json.loads(current.read_text()) if current.exists() else {}
    if previous.get('run') == iso(run) and previous.get('renderVersion') == RENDER_VERSION:
        LOG.info('ICON-EU %s already processed', iso(run)); return 0
    stage = Path(tempfile.mkdtemp(dir=out.parent, prefix='model-'))
    try:
        frames = process(s, run, stage, args.public_base.rstrip('/'))
        if len(frames) < 80:
            raise RuntimeError(f'Only {len(frames)} frames processed')
        # Swap in the new run: images first, manifest last; then drop the previous run's images.
        for f in stage.glob('*.png'):
            os.replace(f, out / f.name)
        atomic_json(current, manifest(run, frames, now))
        keep = {Path(f['image']).name for f in frames}
        for f in out.glob('*.png'):
            if f.name not in keep:
                f.unlink(missing_ok=True)
        LOG.info('ICON-EU %s published: %s frames to %s', iso(run), len(frames), frames[-1]['time'])
        return 0
    except Exception as exc:
        LOG.error('ICON-EU %s failed (%s: %s); previous forecast kept', iso(run), type(exc).__name__, exc)
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
