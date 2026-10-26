import datetime as dt
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

spec = importlib.util.spec_from_file_location('model', Path(__file__).resolve().parents[1] / 'model.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
RUN = dt.datetime(2026, 10, 26, 12, tzinfo=dt.timezone.utc)


class ModelTests(unittest.TestCase):
    def test_steps_cover_five_days(self):
        self.assertEqual(m.STEPS[:3], [0, 1, 2]); self.assertEqual(m.STEPS[-1], 120)
        self.assertIn(78, m.STEPS); self.assertNotIn(79, m.STEPS); self.assertIn(81, m.STEPS)

    def test_rates_from_accumulations_hourly_and_three_hourly(self):
        a = lambda v: np.full((2, 2), v, dtype=np.float32)
        out = m.rates({0: a(0), 1: a(1.5), 2: a(1.5), 78: a(10), 81: a(16), 84: a(15)})
        self.assertEqual(float(out[1][0, 0]), 1.5)
        self.assertEqual(float(out[2][0, 0]), 0.0)
        self.assertEqual(float(out[81][0, 0]), 2.0, '6 mm in 3 h -> 2 mm/h')
        self.assertEqual(float(out[84][0, 0]), 0.0, 'negative accumulation noise is clipped')
        self.assertNotIn(0, out)

    def test_render_clouds_under_rain_and_counts_wet_pixels(self):
        rate = np.zeros((4, 4), dtype=np.float32); rate[0, 0] = 0.05; rate[1, 1] = 3; rate[2, 2] = 50
        clouds = np.zeros((4, 4), dtype=np.float32); clouds[3, 3] = 100; clouds[3, 2] = 20
        rgba, wet = m.render(rate, clouds)
        self.assertEqual(wet, 2, 'below 0.1 mm/h is not rain')
        self.assertEqual(rgba[0, 0, 3], 0)
        self.assertEqual(rgba[1, 1, 3], 245); self.assertEqual(rgba[2, 2, :3].tolist(), m.RATE_COLORS[-1].astype(int).tolist())
        self.assertEqual(rgba[3, 3, 3], 170, 'overcast sky is a translucent slate grey'); self.assertEqual(rgba[3, 2, 3], 0)

    def test_crop_keeps_the_radar_extent_with_cell_edges(self):
        # Synthetic ICON-EU-like grid: 0.0625 deg, west -23.5, north 70.5
        vals = np.arange(657 * 1377, dtype=np.float32).reshape(657, 1377)
        cut, tr = m.crop(vals, -23.5, 70.5, 0.0625, 0.0625)
        self.assertLessEqual(tr.c, m.BOUNDS[0] - 0.9); self.assertGreaterEqual(tr.f, m.BOUNDS[3] + 0.9)
        self.assertGreaterEqual(tr.c + cut.shape[1] * 0.0625, m.BOUNDS[2] + 0.9)
        self.assertLessEqual(tr.f - cut.shape[0] * 0.0625, m.BOUNDS[1] - 0.9)
        with self.assertRaises(ValueError):
            m.crop(vals[:5, :5], -23.5, 70.5, 0.0625, 0.0625)

    def test_mercator_output_matches_radar_frame_shape(self):
        vals = np.ones((300, 400), dtype=np.float32)
        out = m.to_mercator(vals, m.from_origin(-13, 47, 0.0625, 0.0625), width=256)
        self.assertEqual(out.shape[1], 256); self.assertTrue(np.nanmin(out) == 1)

    def test_only_complete_runs_are_used(self):
        def listing(*_a, **_k):
            files = [f'icon-eu_europe_regular-lat-lon_single-level_2026102612_{s:03d}_TOT_PREC.grib2.bz2' for s in m.STEPS]
            files += [f'icon-eu_europe_regular-lat-lon_single-level_2026102618_{s:03d}_TOT_PREC.grib2.bz2' for s in m.STEPS[:40]]
            return ''.join(f'<a href="{f}">' for f in files).encode()
        with patch.object(m, 'fetch', side_effect=listing):
            self.assertEqual(m.complete_runs(None), [RUN])

    def test_source_cannot_be_an_arbitrary_proxy(self):
        for url in ['https://evil.example/x', 'http://opendata.dwd.de/weather/nwp/icon-eu/grib/', 'https://opendata.dwd.de/other/']:
            with self.assertRaises(ValueError):
                m.fetch(None, url)

    def test_manifest_contract(self):
        man = m.manifest(RUN, [{'id': '20261026T130000Z'}], RUN)
        self.assertEqual(man['bounds'], list(m.BOUNDS)); self.assertEqual(man['run'], '2026-10-26T12:00:00Z')
        self.assertEqual(man['unit'], 'mm/h'); self.assertIn('DWD', man['source'])


if __name__ == '__main__':
    unittest.main()
