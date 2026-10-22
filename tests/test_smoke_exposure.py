import unittest
from datetime import datetime, timezone
from Livestock.smoke_exposure import assess_route, assess_fire_hazard, rank_routes


class SmokeExposureTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
        self.route = {"duration_hours": 1, "distance_km": 4,
                      "geometry": {"type": "LineString", "coordinates": [[-117.0, 33.0], [-116.98, 33.0], [-116.96, 33.0]]},
                      "segment_durations_seconds": [1800, 1800], "segment_distances_meters": [2000, 2000]}
        self.forecast = {"status": "available", "spatial_resolution_km": 3, "layers": [
            {"valid_at": "2026-10-22T06:00:00Z", "latitudes": [[33.0, 33.0], [33.02, 33.02]],
             "longitudes": [[-117.0, -116.98], [-117.0, -116.98]],
             "concentration_ug_m3": [[10, 10], [10, 10]]},
            {"valid_at": "2026-10-22T07:00:00Z", "latitudes": [[33.0, 33.0], [33.02, 33.02]],
             "longitudes": [[-117.0, -116.98], [-117.0, -116.98]],
             "concentration_ug_m3": [[10, 10], [10, 10]]},
        ]}

    def test_uniform_concentration_produces_correct_integral(self):
        result = assess_route(self.route, self.forecast, self.start)
        self.assertEqual(result["status"], "assessed")
        self.assertAlmostEqual(result["integrated_exposure_ug_h_m3"], 10, places=1)
        self.assertEqual(result["peak_concentration_ug_m3"], 10)
        self.assertEqual(result["coverage_fraction"], 1)
        self.assertEqual(len(result["route_segment_evidence"]), 2)
        self.assertEqual(result["route_segment_evidence"][0]["coverage_fraction"], 1)

    def test_loading_delay_changes_time_aligned_smoke_exposure(self):
        changing = {**self.forecast, "layers": [
            {**self.forecast["layers"][0], "concentration_ug_m3": [[0, 0], [0, 0]]},
            {**self.forecast["layers"][1], "concentration_ug_m3": [[20, 20], [20, 20]]},
            {"valid_at": "2026-10-22T08:00:00Z", "latitudes": [[33.0, 33.0], [33.02, 33.02]],
             "longitudes": [[-117.0, -116.98], [-117.0, -116.98]],
             "concentration_ug_m3": [[40, 40], [40, 40]]},
        ]}
        on_time = assess_route(self.route, changing, self.start)
        delayed = assess_route(self.route, changing, self.start, loading_minutes=60)
        self.assertGreater(delayed["integrated_exposure_ug_h_m3"], on_time["integrated_exposure_ug_h_m3"])

    def test_missing_forecast_is_unknown(self):
        result = assess_route(self.route, {"status": "unavailable", "layers": []}, self.start)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["integrated_exposure_ug_h_m3"])

    def test_missing_osrm_segment_timing_is_unknown(self):
        route = {key: value for key, value in self.route.items() if key != "segment_durations_seconds"}
        result = assess_route(route, self.forecast, self.start)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("segment timing unavailable", result["reason"])

    def test_partial_grid_coverage_does_not_count_as_clear(self):
        sparse = {**self.forecast, "layers": [{**self.forecast["layers"][0], "latitudes": [[33.0]],
                "longitudes": [[-117.0]], "concentration_ug_m3": [[0]]},
                {**self.forecast["layers"][1], "latitudes": [[33.0]],
                "longitudes": [[-117.0]], "concentration_ug_m3": [[0]]}]}
        result = assess_route(self.route, sparse, self.start)
        self.assertEqual(result["status"], "partial")
        self.assertLess(result["coverage_fraction"], 1)
        self.assertIsNone(result["integrated_exposure_ug_h_m3"])

    def test_feasibility_filters_slow_route_before_smoke_rank(self):
        ranked = rank_routes([
            {"route_id": "clean-slow", "source": "osrm", "geometry": {"type": "LineString"}, "duration_hours": 3, "smoke_assessment": {"status": "assessed", "integrated_exposure_ug_h_m3": 0}, "destination_verified": True},
            {"route_id": "smoky-fast", "source": "osrm", "geometry": {"type": "LineString"}, "duration_hours": 1, "smoke_assessment": {"status": "assessed", "integrated_exposure_ug_h_m3": 12}, "destination_verified": True},
        ], impact_eta_hours=2)
        self.assertEqual(ranked["ranked"][0]["route_id"], "smoky-fast")
        self.assertEqual(ranked["rejected"][0]["route_id"], "clean-slow")

    def test_fire_spread_polygon_crossing_is_separate_from_smoke(self):
        polygon = {"type": "Polygon", "coordinates": [[[-116.99, 32.99], [-116.97, 32.99],
                    [-116.97, 33.01], [-116.99, 33.01], [-116.99, 32.99]]]}
        route = {"duration_hours": 1, "geometry": {"type": "LineString",
                 "coordinates": [[-117.0, 33.0], [-116.96, 33.0]]},
                 "segment_durations_seconds": [3600]}
        result = assess_fire_hazard(route, {"6h": {"polygon": polygon}}, fire_present=True)
        self.assertEqual(result["status"], "intersects")
        self.assertTrue(result["intersects"])

    def test_unverified_destination_stays_in_manual_review(self):
        route = {"route_id": "candidate", "source": "osrm", "geometry": {"type": "LineString"},
                 "duration_hours": 1, "fire_hazard": {"status": "clear", "intersects": False},
                 "destination_verified": False,
                 "smoke_assessment": {"status": "assessed", "integrated_exposure_ug_h_m3": 1}}
        ranked = rank_routes([route], impact_eta_hours=2)
        self.assertEqual(ranked["ranked"], [])
        self.assertEqual(ranked["review_required"][0]["route_id"], "candidate")
        self.assertIn("destination", ranked["review_required"][0]["review_reason"])


if __name__ == "__main__":
    unittest.main()
