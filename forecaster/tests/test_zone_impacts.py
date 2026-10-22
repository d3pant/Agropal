import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from forecaster import compute_zone_impacts


class ZoneImpactTests(unittest.TestCase):
    def setUp(self):
        self.zones = [{"zone_id": "south", "name": "Downwind", "polygon": {"type": "Polygon", "coordinates": [[[-117.01, 32.89], [-116.99, 32.89], [-116.99, 32.91], [-117.01, 32.91], [-117.01, 32.89]]]}}]
        self.fire = {"location": {"lat": 33, "lon": -117}, "frp_mw": 1}

    def test_missing_weather_does_not_invent_impact_eta(self):
        result = compute_zone_impacts(self.zones, self.fire, None)
        self.assertIsNone(result[0]["time_to_impact_hours"])
        self.assertEqual(result[0]["threat_level"], "UNKNOWN")

    def test_uses_downwind_direction_and_provided_weather(self):
        result = compute_zone_impacts(self.zones, self.fire, None,
                                      {"wind_direction_degrees": 0, "wind_speed_kmh": 20, "soil_moisture": 0.1})
        self.assertTrue(result[0]["threatened"])
        self.assertIsNotNone(result[0]["time_to_impact_hours"])


if __name__ == "__main__":
    unittest.main()
