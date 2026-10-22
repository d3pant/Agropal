import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from forecaster.data_sources.smoke import normalize_smoke_grid, build_nomads_request, fetch_smoke_forecast


class SmokeProviderTests(unittest.TestCase):
    def setUp(self):
        self.cache_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache_temp.cleanup)
        self.cache_dir = Path(self.cache_temp.name)

    def test_converts_noaa_kg_per_m3_to_micrograms_per_m3(self):
        result = normalize_smoke_grid(
            [[1e-9, 2e-9], [None, -1]], [[33.0, 33.0], [33.1, 33.1]],
            [[-117.0, -116.9], [-117.0, -116.9]], "kg m**-3",
        )
        self.assertEqual(result["concentration_ug_m3"], [[1.0, 2.0], [None, None]])
        self.assertEqual(result["units"], "ug/m3")

    def test_rejects_unrecognized_units(self):
        with self.assertRaises(ValueError):
            normalize_smoke_grid([[1]], [[33]], [[-117]], "unknown")

    def test_request_selects_only_smoke_and_spatial_subset(self):
        params = build_nomads_request(datetime(2026, 10, 22, 6, tzinfo=timezone.utc), 4,
                                      {"min_lat": 32, "max_lat": 34, "min_lon": -118, "max_lon": -116})
        self.assertEqual(params["var_MASSDEN"], "on")
        self.assertEqual(params["subregion"], "on")
        self.assertEqual(params["file"], "hrrr.t06z.wrfsfcf04.grib2")
        self.assertEqual(params["bottomlat"], 32)
        self.assertEqual(params["rightlon"], -116)


    def test_fetch_parses_only_valid_grib_and_records_valid_times(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def raise_for_status(self): pass
            def iter_bytes(self, chunk_size): yield b"GRIBfixture"
        class Client:
            def stream(self, *args, **kwargs): return Response()
        now = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
        result = fetch_smoke_forecast({"min_lat": 32, "max_lat": 34, "min_lon": -118, "max_lon": -116},
                                      now, now, http_client=Client(), grib_reader=lambda path: {
                                          "latitudes": [[33]], "longitudes": [[-117]],
                                          "concentration_ug_m3": [[1]], "units": "ug/m3"},
                                      cache_dir=self.cache_dir)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["layers"][0]["valid_at"], "2026-10-22T06:00:00Z")
        self.assertEqual(result["height_agl_m"], 8)

    def test_fetch_rejects_non_grib_responses(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def raise_for_status(self): pass
            def iter_bytes(self, chunk_size): yield b"not a grib file"
        class Client:
            def stream(self, *args, **kwargs): return Response()
        now = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
        result = fetch_smoke_forecast({"min_lat": 32, "max_lat": 34, "min_lon": -118, "max_lon": -116},
                                      now, now, http_client=Client(), cache_dir=self.cache_dir)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["layers"], [])

    def test_cache_reuses_same_cycle_region_and_separates_new_cycles(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def raise_for_status(self): pass
            def iter_bytes(self, chunk_size): yield b"GRIBfixture"
        class Client:
            def __init__(self): self.calls = 0
            def stream(self, *args, **kwargs):
                self.calls += 1
                return Response()
        bounds = {"min_lat": 32, "max_lat": 34, "min_lon": -118, "max_lon": -116}
        reader = lambda path: {"latitudes": [[33]], "longitudes": [[-117]],
                               "concentration_ug_m3": [[1]], "units": "ug/m3"}
        with tempfile.TemporaryDirectory() as temp_dir:
            first_client = Client()
            first = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
            fetch_smoke_forecast(bounds, first, first, http_client=first_client,
                                 grib_reader=reader, cache_dir=Path(temp_dir))
            second_client = Client()
            cached = fetch_smoke_forecast(bounds, first, first, http_client=second_client,
                                          grib_reader=reader, cache_dir=Path(temp_dir))
            self.assertEqual(first_client.calls, 1)
            self.assertEqual(second_client.calls, 0)
            self.assertEqual(cached["download_bytes"], 0)
            next_cycle_client = Client()
            next_cycle = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
            fetch_smoke_forecast(bounds, next_cycle, next_cycle, http_client=next_cycle_client,
                                 grib_reader=reader, cache_dir=Path(temp_dir))
            self.assertEqual(next_cycle_client.calls, 1)


if __name__ == "__main__":
    unittest.main()
