# MeteoNexo radar

Procesador del radar de [MeteoNexo](https://meteonexo.vercel.app/radar). Cada 10 minutos GitHub Actions descarga los compuestos de reflectividad (DBZH) de EUMETNET OPERA, los reproyecta a Web Mercator, los recorta a Península y Baleares y publica en GitHub Pages la última hora observada más una previsión inmediata (nowcast) de la próxima hora, como PNG transparentes y un `manifest.json`:

- Manifiesto: https://ruchu85.github.io/meteonexo-radar/manifest.json
- Estado del último ciclo: https://ruchu85.github.io/meteonexo-radar/health.json

Un ciclo fallido no publica nada: Pages sigue sirviendo la última secuencia válida.

## Previsión inmediata

Los fotogramas con `kind: "forecast"` (+10 a +60 min) se calculan con [pysteps](https://pysteps.github.io/): movimiento por Lucas-Kanade a partir de las observaciones de hace 20, 10 y 0 minutos, y advección semi-lagrangiana del último campo. Solo desplaza los ecos observados: no crea ni intensifica lluvia, así que no anticipa tormentas que aún no existen. Si falta alguna entrada o el cálculo falla, se publican solo las observaciones.

## Pronóstico por horas (ICON-EU)

`model.py` publica en `model/` la precipitación prevista (mm/h) y la nubosidad del modelo ICON-EU del Deutscher Wetterdienst, horaria hasta +78 h y cada 3 h hasta +120 h, cuando DWD termina una pasada (00/06/12/18 UTC). Si falla, el radar se publica igualmente y se conserva el pronóstico anterior.

## Fuente y licencia de los datos

Datos de radar: [EUMETNET OPERA](https://eumetnet.github.io/openradardata-documentation/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Procesado (reproyección por vecino más próximo, recorte, escala de color y extrapolación) por MeteoNexo.

Pronóstico: Quelle: [Deutscher Wetterdienst](https://opendata.dwd.de/weather/nwp/icon-eu/) (DL-DE-BY-2.0 / GeoNutzV), modelo ICON-EU; recortado, reproyectado y coloreado por MeteoNexo.

## Local

```bash
pip install -r requirements.txt
python worker.py --probe      # lista observaciones recientes sin descargar
python worker.py              # un ciclo en ./data (requiere Linux/macOS/WSL)
python -m unittest discover -s tests -v
```

El código fuente de este procesador se mantiene sincronizado con `radar/` del repositorio de la web.
