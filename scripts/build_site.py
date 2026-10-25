"""Copies the published manifest and only the frames it references into the Pages folder.

The worker keeps 24 h of frames locally (restored between runs through the Actions cache);
publishing just the referenced ones keeps every Pages deployment small.
"""
import json
import shutil
import sys
from pathlib import Path

data, site = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads((data / 'manifest.json').read_text())
(site / 'frames').mkdir(parents=True, exist_ok=True)
for frame in manifest['frames']:
    for suffix in ('r', 'c'):
        name = f"{frame['id']}-{suffix}.png"
        shutil.copy2(data / 'frames' / name, site / 'frames' / name)
for name in ('manifest.json', 'health.json'):
    if (data / name).exists():
        shutil.copy2(data / name, site / name)
(site / '.nojekyll').touch()
(site / 'index.html').write_text(
    '<!doctype html><meta charset="utf-8"><title>MeteoNexo radar data</title>'
    '<p>Datos de radar procesados para <a href="https://meteonexo.vercel.app/radar">MeteoNexo</a>. '
    'Fuente: <a href="https://eumetnet.github.io/openradardata-documentation/">EUMETNET OPERA</a>, '
    '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.</p>'
)
print(f"Site ready: {len(manifest['frames'])} frames, latest {manifest['frames'][-1]['time']}")
