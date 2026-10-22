import unittest
from datetime import datetime, timezone
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from Livestock.livestock_agent import (
    apply_transport_plan, compute_routes_for_pen, load_latest_crop_output, parse_osrm_routes, destination_is_confirmed, osrm_route_url,
    reserve_site_capacity, smoke_forecast_window_end,
)


class OsrmRouteParsingTests(unittest.TestCase):
    def test_remote_osrm_url_uses_https_and_local_service_is_configurable(self):
        self.assertTrue(osrm_route_url(33, -117, 34, -118).startswith("https://"))
        self.assertEqual(
            osrm_route_url(33, -117, 34, -118, "http://localhost:5000/"),
            "http://localhost:5000/route/v1/driving/-117,33;-118,34",
        )

    def test_preserves_route_geometry_and_segment_timing_for_each_alternative(self):
        data = {"code": "Ok", "routes": [
            {"duration": 120, "distance": 1000,
             "geometry": {"type": "LineString", "coordinates": [[-117, 33], [-116.9, 33.1]]},
             "legs": [{"annotation": {"duration": [50, 70], "distance": [400, 600]}}]},
            {"duration": 180, "distance": 1500,
             "geometry": {"type": "LineString", "coordinates": [[-117, 33], [-117.1, 33.1]]},
             "legs": [{"annotation": {"duration": [100, 80], "distance": [800, 700]}}]},
        ]}
        routes = parse_osrm_routes(data)
        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[0]["geometry"]["coordinates"][1], [-116.9, 33.1])
        self.assertEqual(routes[1]["segment_durations_seconds"], [100, 80])
        self.assertEqual(routes[0]["distance_km"], 1)

    def test_missing_annotations_are_unknown_not_fabricated(self):
        routes = parse_osrm_routes({"code": "Ok", "routes": [{
            "duration": 80, "distance": 800,
            "geometry": {"type": "LineString", "coordinates": [[-117, 33], [-116.9, 33.1]]},
            "legs": [{}],
        }]})
        self.assertIsNone(routes[0]["segment_durations_seconds"])
        self.assertEqual(routes[0]["quality"], "partial")

    def test_invalid_or_empty_route_returns_no_candidates(self):
        self.assertEqual(parse_osrm_routes({"code": "NoRoute", "routes": []}), [])
        self.assertEqual(parse_osrm_routes({"code": "Ok", "routes": [{"geometry": {"type": "Point"}}]}), [])

    def test_destination_requires_recent_acceptance_and_capacity(self):
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        site = {"availability_confirmed": True, "accepted_head": 80, "verified_at": "2026-10-22T10:00:00Z"}
        self.assertTrue(destination_is_confirmed(site, 50, now))
        self.assertFalse(destination_is_confirmed(site, 90, now))
        site["verified_at"] = "2026-10-21T10:00:00Z"
        self.assertFalse(destination_is_confirmed(site, 50, now))

    def test_transport_plan_caps_planned_animals_to_available_capacity(self):
        pens = [
            {"pen_id": "p1", "species": "cattle", "count": 100,
             "avg_market_value_usd": 100, "decision": "evacuate", "route_duration_hours": 1},
            {"pen_id": "p2", "species": "sheep", "count": 100,
             "avg_market_value_usd": 50, "decision": "evacuate", "route_duration_hours": 1},
        ]
        farm = {"pens": pens, "infrastructure": {"vehicle_capacity_head": 50, "available_trailers": 1}}
        result = apply_transport_plan(pens, farm, 2)
        self.assertEqual(result["summary"]["animals_can_evacuate"], 50)
        self.assertEqual(sum(p.get("animals_planned_to_move", 0) for p in pens), 50)
        self.assertEqual(sum(p["decision"] == "evacuate" for p in pens), 0)
        self.assertIn("partial_plan", {p["decision"] for p in pens})

    def test_receiving_capacity_is_reserved_across_pens(self):
        site = {"lat": 33.0, "lon": -117.0}
        remaining = {(33.0, -117.0): 120}
        self.assertTrue(reserve_site_capacity(remaining, site, 80))
        self.assertFalse(reserve_site_capacity(remaining, site, 50))
        self.assertEqual(remaining[(33.0, -117.0)], 40)

    def test_forecast_window_includes_loading_delay_after_longest_route(self):
        departure = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
        end = smoke_forecast_window_end(departure, 1.0, 60)
        self.assertEqual(end, datetime(2026, 10, 22, 8, tzinfo=timezone.utc))


class RouteCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_candidates_include_vehicle_positioning_and_return_legs(self):
        line = {"type": "LineString", "coordinates": [[-117.0, 33.0], [-117.0, 34.0]]}
        def result(duration, route_id):
            return {"duration_hours": duration, "distance_km": duration * 40,
                    "geometry": line, "route_id": route_id,
                    "segment_durations_seconds": [duration * 3600],
                    "segment_distances_meters": [duration * 40000],
                    "quality": "assessed", "source": "osrm", "alternatives": []}

        with patch("Livestock.livestock_agent.query_osrm", new_callable=AsyncMock) as query:
            query.side_effect = [result(0.1, "approach"), result(1, "out"), result(1.2, "return")]
            routes = {}
            await compute_routes_for_pen(
                AsyncMock(),
                {"pen_id": "p1", "centroid": {"lat": 33.1, "lon": -117.0}},
                [{"name": "shelter", "lat": 34.0, "lon": -117.0}], routes,
                {"lat": 33.0, "lon": -117.0},
            )

        candidate = routes["p1"][0]
        self.assertEqual(candidate["approach_duration_hours"], 0.1)
        self.assertEqual(candidate["duration_hours"], 1)
        self.assertEqual(candidate["return_duration_hours"], 1.2)
        self.assertIsNotNone(candidate["approach_geometry"])
        self.assertIsNotNone(candidate["return_geometry"])


class CropOutputSelectionTests(unittest.TestCase):
    def test_ignores_erpc_and_raw_files_when_selecting_full_crop_output(self):
        with tempfile.TemporaryDirectory() as directory:
            crop_dir = Path(directory)
            full = crop_dir / "output_20261022_120000.json"
            erpc = crop_dir / "output_to_erpc.json"
            raw = crop_dir / "output_20261022_120001_raw.json"
            full.write_text(json.dumps({"_meta": {"run_id": "run-current"}, "task4": []}))
            erpc.write_text(json.dumps({"economic_report": {}}))
            raw.write_text(json.dumps({"raw_llm_response": "bad"}))
            os.utime(full, (100, 100))
            os.utime(erpc, (200, 200))
            os.utime(raw, (300, 300))
            self.assertEqual(load_latest_crop_output(crop_dir)["_meta"]["run_id"], "run-current")


if __name__ == "__main__":
    unittest.main()
