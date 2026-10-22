import unittest
from datetime import datetime, timezone
from Livestock.livestock_agent import (
    apply_transport_plan, parse_osrm_routes, destination_is_confirmed, osrm_route_url,
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


if __name__ == "__main__":
    unittest.main()
