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
# Observed frames reference {id}-r/-c.png; forecast frames {base}-f{lead}.png and the base coverage.
for name in {Path(frame[key]).name for frame in manifest['frames'] for key in ('image', 'coverage')}:
    shutil.copy2(data / 'frames' / name, site / 'frames' / name)
for name in ('manifest.json', 'health.json', 'home-weather.json'):
    if (data / name).exists():
        shutil.copy2(data / name, site / name)
# ICON-EU forecast: manifest plus only the images it references.
model_manifest = data / 'model' / 'manifest.json'
if model_manifest.exists():
    model = json.loads(model_manifest.read_text())
    (site / 'model').mkdir(parents=True, exist_ok=True)
    for frame in model['frames']:
        name = Path(frame['image']).name
        shutil.copy2(data / 'model' / name, site / 'model' / name)
    shutil.copy2(model_manifest, site / 'model' / 'manifest.json')
    print(f"Model ready: run {model['run']}, {len(model['frames'])} frames")
(site / '.nojekyll').touch()
(site / 'index.html').write_text(
    '<!doctype html><meta charset="utf-8"><title>TiempoEn radar data</title>'
    '<p>Datos de radar procesados para <a href="https://tiempoen.es/radar">TiempoEn</a>. '
    'Fuente: <a href="https://eumetnet.github.io/openradardata-documentation/">EUMETNET OPERA</a>, '
    '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>.</p>'
)
forecast = sum(f.get('kind') == 'forecast' for f in manifest['frames'])
print(f"Site ready: {len(manifest['frames']) - forecast} observed + {forecast} forecast frames, last {manifest['frames'][-1]['time']}")
