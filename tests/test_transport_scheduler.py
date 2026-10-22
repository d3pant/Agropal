import unittest
from datetime import datetime, timedelta, timezone

from Livestock.transport_scheduler import normalize_crop_tasks, schedule_evacuation_trips


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def route(site="North Shelter", duration=1.0, return_duration=1.0,
          accepted=500, return_safe="clear", smoke="assessed"):
    return {
        "site": {"name": site, "lat": 34.0, "lon": -117.0,
                 "availability_confirmed": True, "accepted_head": accepted,
                 "verified_at": NOW.isoformat()},
        "duration_hours": duration,
        "approach_duration_hours": 0.0,
        "approach_source": "osrm",
        "return_duration_hours": return_duration,
        "fire_hazard": {"status": "clear", "intersects": False},
        "return_fire_hazard": {"status": return_safe, "intersects": return_safe == "intersects"},
        "smoke_assessment": {"status": smoke, "integrated_exposure_ug_h_m3": 10.0},
        "return_smoke_assessment": {"status": smoke, "integrated_exposure_ug_h_m3": 10.0},
        "source": "osrm",
        "geometry": {"type": "LineString", "coordinates": [[-117.0, 33.0], [-117.0, 34.0]]},
        "return_geometry": {"type": "LineString", "coordinates": [[-117.0, 34.0], [-117.0, 33.0]]},
        "return_source": "osrm",
    }


def pen(pen_id, count=40, eta=12, duration=1.0, return_duration=1.0,
        **route_args):
    return {
        "pen_id": pen_id, "count": count, "decision": "evacuate",
        "priority_score": 50, "time_to_impact_hours": eta,
        "route_options": [route(duration=duration, return_duration=return_duration, **route_args)],
    }


def farm(capacity=50, vehicles=2, accepted=500):
    return {
        "pens": [],
        "infrastructure": {"vehicle_capacity_head": capacity, "available_trailers": vehicles},
    }


CONFIG = {"loading_minutes": 30, "unloading_minutes": 30, "fire_eta_margin": 0.8}


class TransportSchedulerTests(unittest.TestCase):
    def test_schedules_independent_vehicle_cycles_and_returns(self):
        pens = [pen("p1", duration=1), pen("p2", duration=1), pen("p3", duration=1)]
        for item in pens:
            item["route_options"][0]["approach_duration_hours"] = 0.25
            item["route_options"][0]["approach_geometry"] = item["route_options"][0]["geometry"]
            item["route_options"][0]["approach_fire_hazard"] = {"status": "clear"}
            item["route_options"][0]["approach_smoke_assessment"] = {"status": "assessed"}
        result = schedule_evacuation_trips(
            pens,
            farm(vehicles=2), NOW, config=CONFIG,
        )
        trips = result["trips"]
        self.assertEqual(len(trips), 3)
        by_vehicle = {}
        for trip in trips:
            by_vehicle.setdefault(trip["vehicle_id"], []).append(trip)
        self.assertEqual(len(by_vehicle), 2)
        for vehicle_trips in by_vehicle.values():
            vehicle_trips.sort(key=lambda item: item["trip_number"])
            for earlier, later in zip(vehicle_trips, vehicle_trips[1:]):
                self.assertGreaterEqual(
                    datetime.fromisoformat(later["loading_start_at"]),
                    datetime.fromisoformat(earlier["vehicle_available_at"]) + timedelta(minutes=15),
                )
                self.assertEqual(earlier["return_at"], earlier["vehicle_available_at"])

    def test_least_deadline_slack_precedes_earliest_finish_and_ties_are_stable(self):
        result = schedule_evacuation_trips(
            [pen("far", eta=3, duration=1.5), pen("near", eta=10, duration=0.2)],
            farm(vehicles=1), NOW, config=CONFIG,
        )
        self.assertEqual(result["trips"][0]["pen_id"], "far")

        tied = schedule_evacuation_trips(
            [pen("z", eta=3), pen("a", eta=3)], farm(vehicles=1), NOW, config=CONFIG,
        )
        self.assertEqual(tied["trips"][0]["pen_id"], "a")

    def test_excludes_unknown_eta_and_smoke_but_allows_only_terminal_unsafe_return(self):
        candidates = [pen("unknown_eta", eta=None), pen("unknown_smoke", smoke="partial")]
        result = schedule_evacuation_trips(candidates, farm(), NOW, config=CONFIG)
        self.assertEqual(result["trips"], [])
        self.assertEqual(set(result["unscheduled_by_pen"]), {"unknown_eta", "unknown_smoke"})

        terminal = pen("unsafe_return", count=80, accepted=500, return_safe="intersects")
        terminal["splittable"] = True
        result = schedule_evacuation_trips([terminal], farm(capacity=50, vehicles=1), NOW, config=CONFIG)
        self.assertEqual(len(result["trips"]), 1)
        self.assertFalse(result["trips"][0]["round_trip_possible"])
        self.assertIsNone(result["trips"][0]["return_at"])

        unverified = pen("unverified")
        unverified["route_options"][0]["site"]["availability_confirmed"] = False
        unverified_result = schedule_evacuation_trips([unverified], farm(), NOW, config=CONFIG)
        self.assertEqual(unverified_result["trips"], [])

    def test_reserves_site_capacity_and_splits_only_with_explicit_permission(self):
        profile = farm(capacity=50, vehicles=2)
        one = pen("p1", count=40, accepted=60)
        two = pen("p2", count=40, accepted=60)
        result = schedule_evacuation_trips([one, two], profile, NOW, config=CONFIG)
        self.assertEqual(sum(item["animal_count"] for item in result["trips"]), 40)

        too_large = pen("large", count=80, accepted=200)
        result = schedule_evacuation_trips([too_large], farm(capacity=50, vehicles=2), NOW, config=CONFIG)
        self.assertEqual(result["trips"], [])
        too_large["splittable"] = True
        result = schedule_evacuation_trips([too_large], farm(capacity=50, vehicles=2), NOW, config=CONFIG)
        self.assertEqual(sum(item["animal_count"] for item in result["trips"]), 80)
        self.assertTrue(all(item["animal_count"] <= 50 for item in result["trips"]))

    def test_conflicting_capacity_confirmations_make_destination_unavailable(self):
        first = pen("p1", count=40, accepted=60)
        second = pen("p2", count=40, accepted=100)
        result = schedule_evacuation_trips([first, second], farm(), NOW, config=CONFIG)
        self.assertEqual(result["trips"], [])

    def test_zero_time_never_forces_a_trip_and_allocations_never_exceed_counts(self):
        result = schedule_evacuation_trips(
            [pen("p1", eta=0.1)], farm(vehicles=2), NOW, config=CONFIG,
        )
        self.assertEqual(result["trips"], [])

        result = schedule_evacuation_trips(
            [pen("p1", count=40)], farm(vehicles=2), NOW, config=CONFIG,
        )
        self.assertLessEqual(result["scheduled_by_pen"]["p1"], 40)

    def test_unverified_vehicle_positioning_blocks_trip(self):
        candidate = pen("p1")
        candidate["route_options"][0]["approach_duration_hours"] = 0.2
        candidate["route_options"][0]["approach_source"] = "fallback"
        candidate["route_options"][0]["approach_geometry"] = candidate["route_options"][0]["geometry"]
        result = schedule_evacuation_trips([candidate], farm(), NOW, config=CONFIG)
        self.assertEqual(result["trips"], [])

    def test_reassesses_future_departure_and_does_not_reuse_stale_route_safety(self):
        load = pen("p1", count=80)
        load["splittable"] = True

        def assess_at_schedule(option, positioning_departure, outbound_departure, return_departure):
            if outbound_departure - NOW > timedelta(hours=1):
                return {"outbound_smoke": {"status": "unknown"},
                        "outbound_fire": {"status": "clear"},
                        "return_smoke": {"status": "assessed"},
                        "return_fire": {"status": "clear"}}
            return {"outbound_smoke": {"status": "assessed"},
                    "outbound_fire": {"status": "clear"},
                    "return_smoke": {"status": "assessed"},
                    "return_fire": {"status": "clear"}}

        result = schedule_evacuation_trips(
            [load], farm(capacity=50, vehicles=1), NOW,
            config=CONFIG, route_assessor=assess_at_schedule,
        )
        self.assertEqual(len(result["trips"]), 1)
        self.assertEqual(result["scheduled_by_pen"]["p1"], 50)

    def test_return_route_assessment_starts_after_unloading(self):
        observed = []

        def assess_at_schedule(option, positioning_departure, outbound_departure, return_departure):
            observed.append(return_departure)
            return {"approach_smoke": {"status": "assessed"}, "approach_fire": {"status": "clear"},
                    "outbound_smoke": {"status": "assessed"}, "outbound_fire": {"status": "clear"},
                    "return_smoke": {"status": "assessed"}, "return_fire": {"status": "clear"}}

        schedule_evacuation_trips([pen("p1")], farm(vehicles=1), NOW,
                                  config=CONFIG, route_assessor=assess_at_schedule)
        # 30 min loading + 1 h outbound + 30 min unloading.
        self.assertEqual(observed[0], NOW + timedelta(hours=2))

    def test_crop_tasks_support_aliases_and_keep_missing_duration_unknown(self):
        output = {
            "_meta": {"farm_id": "farm-a", "run_id": "run-1"},
            "task4": [{"field_id": "F1", "decision": "HARVEST NOW", "fire_arrival_hours": 4}],
            "task1": [{"field_id": "F2", "action": "CLEAR", "feasible_with_farm_resources": True,
                       "uprooting_strategy": {"labor_hours_needed": 5, "required_equipment": "disc plow"}}],
            "task3": [{"field_id": "invented", "technique": "WET FIREBREAK"}],
            "task2": {"crop_destructions": [{"field_id": "F1", "estimated_loss_usd": 1200}]},
        }
        tasks = normalize_crop_tasks(output, "farm-a", "run-1", {"F1", "F2"})
        self.assertEqual({item["field_id"] for item in tasks}, {"F1", "F2"})
        self.assertIsNone(next(item for item in tasks if item["field_id"] == "F1")["duration_hours"])
        self.assertEqual(next(item for item in tasks if item["field_id"] == "F1")["estimated_crop_loss_usd"], 1200)
        field_task = next(item for item in tasks if item["field_id"] == "F2")
        self.assertEqual(field_task["labor_hours_needed"], 5)
        self.assertEqual(field_task["required_equipment"], "disc plow")
        self.assertEqual(normalize_crop_tasks(output, "other-farm", "run-1", {"F1", "F2"}), [])
        self.assertEqual(normalize_crop_tasks(output, "farm-a", "other-run", {"F1", "F2"}), [])


if __name__ == "__main__":
    unittest.main()
