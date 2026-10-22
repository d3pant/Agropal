import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from models.spread_model import time_to_impact


class SpreadDirectionTests(unittest.TestCase):
    def test_fire_moves_downwind_from_meteorological_wind_direction(self):
        downwind = time_to_impact(33, -117, 32.9, -117, 0, 20, 0.1, 1)
        upwind = time_to_impact(33, -117, 33.1, -117, 0, 20, 0.1, 1)
        self.assertTrue(downwind["threatened"])
        self.assertFalse(upwind["threatened"])
        self.assertGreater(downwind["effective_ros_kmh"], upwind["effective_ros_kmh"])


if __name__ == "__main__":
    unittest.main()
