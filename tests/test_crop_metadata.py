import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "crop_agent"))
from crop_agent import add_source_metadata


class CropMetadataTests(unittest.TestCase):
    def test_adds_farm_and_forecast_identity_without_changing_agent_tasks(self):
        payload = {"task4": [{"field_id": "F1", "decision": "HARVEST NOW"}], "task1": []}
        original = {key: value.copy() if isinstance(value, list) else value for key, value in payload.items()}
        result = add_source_metadata(
            payload,
            {"farm_id": "farm-a"},
            {"run_id": "run-7"},
            generated_at="2026-10-22T12:00:00Z",
        )
        self.assertEqual(result["_meta"], {
            "farm_id": "farm-a", "run_id": "run-7", "generated_at": "2026-10-22T12:00:00Z",
        })
        self.assertEqual({key: result[key] for key in original}, original)
        self.assertNotIn("_meta", payload)


if __name__ == "__main__":
    unittest.main()
