# MeteoNexo radar

Procesador del radar de [MeteoNexo](https://meteonexo.vercel.app/radar). Cada 10 minutos GitHub Actions descarga los compuestos de reflectividad (DBZH) de EUMETNET OPERA, los reproyecta a Web Mercator, los recorta a Península y Baleares y publica PNG transparentes más un `manifest.json` en GitHub Pages:

- Manifiesto: https://ruchu85.github.io/meteonexo-radar/manifest.json
- Estado del último ciclo: https://ruchu85.github.io/meteonexo-radar/health.json

Un ciclo fallido no publica nada: Pages sigue sirviendo la última secuencia válida. No se generan datos sintéticos ni extrapolaciones.

## Fuente y licencia de los datos

Datos de radar: [EUMETNET OPERA](https://eumetnet.github.io/openradardata-documentation/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Procesado (reproyección por vecino más próximo, recorte y escala de color) por MeteoNexo.

## Local

```bash
pip install -r requirements.txt
python worker.py --probe      # lista observaciones recientes sin descargar
python worker.py              # un ciclo en ./data (requiere Linux/macOS/WSL)
python -m unittest discover -s tests -v
```

El código fuente de este procesador se mantiene sincronizado con `radar/` del repositorio de la web.
