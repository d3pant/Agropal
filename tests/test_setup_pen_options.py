import unittest

from backend.main import PenInput


class PenSetupOptionsTests(unittest.TestCase):
    def test_splittable_defaults_off_and_can_be_enabled(self):
        base = {"species": "cattle", "count": 80, "age": "adult", "health": "healthy",
                "lat": 32.7, "lon": -117.1}
        self.assertFalse(PenInput(**base).splittable)
        self.assertTrue(PenInput(**base, splittable=True).splittable)


if __name__ == "__main__":
    unittest.main()
