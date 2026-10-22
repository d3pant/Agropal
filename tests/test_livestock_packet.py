import unittest
from datetime import datetime, timezone, timedelta

from forecaster.livestock_packet import build_livestock_packet, validate_livestock_packet


class LivestockPacketTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"farm_id": "farm-a", "centroid": {"lat": 33.0, "lon": -117.0}, "pens": []}
        self.config = {"farm_id": "farm-a", "location": {"lat": 33.0, "lon": -117.0}}
        self.status = {
            "timestamp": "2026-10-22T10:00:00Z", "farm_lat": 33.0, "farm_lon": -117.0,
            "farm_id": "farm-a",
            "threat_level": "WARNING", "next_update_minutes": 30,
            "wind_direction_degrees": 0, "nearest_fire": {"location": {"lat": 32.0, "lon": -117.0}},
            "spread_prediction": {"wind_direction_deg": 272, "wind_speed_kmh": 12,
                                  "soil_moisture": 0.1, "time_to_farm": {"hours": None}},
        }

    def test_packet_preserves_zero_wind_and_computes_transport_direction(self):
        packet = build_livestock_packet(self.status, self.profile, self.config, "run-1")
        self.assertEqual(packet["wind_at_farm"]["direction_from_degrees"], 0)
        self.assertEqual(packet["wind_at_farm"]["direction_to_degrees"], 180)
        self.assertEqual(packet["wind_at_fire"]["direction_from_degrees"], 272)
        self.assertEqual(packet["wind_at_fire"]["direction_to_degrees"], 92)

    def test_packet_marks_missing_impact_as_unknown_and_carries_identity(self):
        packet = build_livestock_packet(self.status, self.profile, self.config, "run-1")
        self.assertEqual(packet["run_id"], "run-1")
        self.assertEqual(packet["farm_id"], "farm-a")
        self.assertIsNone(packet["spread_prediction"]["time_to_farm_hours"])
        self.assertEqual(packet["spread_prediction"]["impact_status"], "unknown")

    def test_rejects_farm_location_mismatch(self):
        profile = {**self.profile, "centroid": {"lat": 34.0, "lon": -118.0}}
        packet = build_livestock_packet(self.status, profile, self.config, "run-1")
        errors = validate_livestock_packet(packet, profile, datetime(2026, 10, 22, 10, 1, tzinfo=timezone.utc))
        self.assertTrue(any("location" in error["field"] for error in errors))

    def test_rejects_forecast_computed_for_other_coordinates(self):
        self.status["farm_lat"] = 34.0
        packet = build_livestock_packet(self.status, self.profile, self.config, "run-1")
        errors = validate_livestock_packet(packet, self.profile, datetime(2026, 10, 22, 10, 1, tzinfo=timezone.utc))
        self.assertTrue(any(error["field"] == "status_location" for error in errors))

    def test_rejects_expired_and_wrong_farm_packets(self):
        packet = build_livestock_packet(self.status, self.profile, self.config, "run-1")
        packet["farm_id"] = "old-farm"
        errors = validate_livestock_packet(packet, self.profile, datetime(2026, 10, 22, 10, 31, tzinfo=timezone.utc))
        self.assertTrue(any(error["field"] == "farm_id" for error in errors))
        self.assertTrue(any(error["field"] == "valid_until" for error in errors))

    def test_null_wind_stays_unknown(self):
        self.status["wind_direction_degrees"] = None
        packet = build_livestock_packet(self.status, self.profile, self.config, "run-1")
        self.assertIsNone(packet["wind_at_farm"]["direction_to_degrees"])


if __name__ == "__main__":
    unittest.main()
