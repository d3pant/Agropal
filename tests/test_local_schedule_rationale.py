import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from Livestock.livestock_agent import explain_multitrip_value_locally


class LocalScheduleRationaleTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_disabled_without_local_model_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(await explain_multitrip_value_locally({"trips": []}, []))

    async def test_returns_only_bounded_display_text_from_local_model(self):
        response = Mock()
        response.json.return_value = {"response": "A second trip fits; crop duration is unknown."}
        client = AsyncMock()
        client.post.return_value = response
        schedule = {"summary": {"animals_scheduled": 40}, "trips": [{"pen_id": "p1"}]}
        crops = [{"field_id": "F1", "duration_hours": None}]
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434/", "OLLAMA_MODEL": "local-model"}):
            text = await explain_multitrip_value_locally(schedule, crops, client=client)
        self.assertEqual(text, "A second trip fits; crop duration is unknown.")
        self.assertEqual(client.post.call_args.args[0], "http://localhost:11434/api/generate")
        self.assertFalse(client.post.call_args.kwargs["json"]["stream"])

    async def test_local_model_failure_falls_back_without_changing_schedule(self):
        client = AsyncMock()
        client.post.side_effect = TimeoutError("local model timed out")
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434", "OLLAMA_MODEL": "local-model"}):
            self.assertIsNone(await explain_multitrip_value_locally({"trips": []}, [], client=client))

    async def test_malformed_local_model_payload_is_ignored(self):
        response = Mock()
        response.json.return_value = {"message": "unexpected response shape"}
        client = AsyncMock()
        client.post.return_value = response
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434", "OLLAMA_MODEL": "local-model"}):
            self.assertIsNone(await explain_multitrip_value_locally({"trips": []}, [], client=client))


if __name__ == "__main__":
    unittest.main()
