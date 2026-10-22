import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

from backend import main


class PipelineOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_crop_agent_finishes_before_livestock_reads_its_run_matched_output(self):
        crop_done = threading.Event()
        observed = []

        async def forecaster(*args):
            observed.append("forecaster")
            return {"run_id": "run-1"}

        def crop():
            observed.append("crop_started")
            time.sleep(0.05)
            crop_done.set()
            observed.append("crop_finished")
            return {"run_id": "run-1"}

        def livestock():
            observed.append("livestock_after_crop" if crop_done.is_set() else "livestock_before_crop")
            return {"run_id": "run-1"}

        with patch.object(main, "_run_forecaster_cycle", side_effect=forecaster), \
             patch.object(main, "_run_crop_subprocess", side_effect=crop), \
             patch.object(main, "_run_livestock_subprocess", side_effect=livestock), \
             patch.object(main, "_run_econ_subprocess", return_value={}), \
             patch.object(main, "_run_policy_subprocess", return_value={}), \
             patch.object(main, "_run_insurance_subprocess", return_value={}), \
             patch.object(main, "_run_report_subprocess", return_value={}):
            await main._run_pipeline_cycle(None, None)

        self.assertEqual(observed, ["forecaster", "crop_started", "crop_finished", "livestock_after_crop"])


if __name__ == "__main__":
    unittest.main()
