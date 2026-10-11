import asyncio
import unittest
from unittest import mock

from prism import cfo_worker, models
from prism.types import Usage


COMMIT = "79a6a4f18909e999d471036958455a9625d21035"
REQUEST = {"request_id": "SYNTHETIC/prism-ci-k2-issue40-run1", "repository": "Bhavya031/k-2",
           "issue_url": "https://github.com/Bhavya031/k-2/issues/40", "pinned_commit": COMMIT,
           "worker_model": "claude-sonnet-5-5"}
ESTIMATE = {"repository_commit": COMMIT, "worker_model": REQUEST["worker_model"],
            "end_to_end_cost_estimate": {"central": {"usd": 0.004}},
            "assumptions": {"pricing": {"usd_per_million_tokens": {
                "uncached_input": 2.0, "cached_input": 0.1, "output": 10.0}}}}
ALLOCATION = {"request_id": REQUEST["request_id"], "allocation_id": "allocation-1",
              "repository": REQUEST["repository"], "issue": 40, "issue_url": REQUEST["issue_url"],
              "pinned_commit": COMMIT, "usd_cap": 0.004, "decision": "allocated"}


class CFOBudgetedWorkerTests(unittest.TestCase):
    def worker(self, allocation=ALLOCATION, estimate=ESTIMATE):
        return cfo_worker.BudgetedWorker(estimate, allocation, **REQUEST)

    def test_refuses_missing_denied_or_mismatched_allocation_before_a_call(self):
        for allocation in ({}, ALLOCATION | {"decision": "denied"},
                           ALLOCATION | {"request_id": "other"}, ALLOCATION | {"issue": 41},
                           ALLOCATION | {"pinned_commit": "other"}, ALLOCATION | {"usd_cap": 1}):
            with self.subTest(allocation=allocation), self.assertRaises(ValueError):
                self.worker(allocation)
        with self.assertRaises(ValueError):
            self.worker(estimate=ESTIMATE | {"worker_model": "other"})
        with self.assertRaisesRegex(ValueError, "prices differ"):
            self.worker(estimate=ESTIMATE | {"assumptions": {"pricing": {"usd_per_million_tokens": {
                "uncached_input": 1.0, "cached_input": 0.1, "output": 10.0}}}})
        with mock.patch.dict("os.environ", {"PRISM_CLAUDE_MODEL": "other"}):
            with self.assertRaisesRegex(ValueError, "Configured provider model"):
                self.worker()

    def test_reserves_full_call_cost_then_records_provider_usage(self):
        worker = self.worker()
        seen = []

        async def fake(provider, stage, prompt, schema, max_output_tokens, *, system):
            seen.append((provider, max_output_tokens))
            return {"ok": True}, Usage(input=5, cached_input=0, cache_write=0, output=1, reasoning=None)

        result = asyncio.run(worker.call("code", "prompt", {}, max_input_tokens=5,
                                         max_output_tokens=1, call=fake))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, [("claude", 1)])
        self.assertEqual(worker.result()["spent_usd"], "0.00002")
        with self.assertRaises(cfo_worker.BudgetStop):
            asyncio.run(worker.call("code", "prompt", {}, max_input_tokens=5,
                                    max_output_tokens=400, call=fake))
        self.assertEqual(seen, [("claude", 1)])
        self.assertEqual(worker.result()["status"], "capped")
        self.assertEqual(worker.result()["usd_cap"], "0.004")
        self.assertEqual(worker.result()["actual_tokens"]["output"], 1)

    def test_billed_failure_keeps_reported_usage_in_final_output(self):
        worker = self.worker()

        async def fake(*args, **kwargs):
            raise models.Truncated("output limit", Usage(input=2, cached_input=0, cache_write=0,
                                                            output=1, reasoning=None))

        with self.assertRaises(models.Truncated):
            asyncio.run(worker.call("code", "prompt", {}, max_input_tokens=5,
                                    max_output_tokens=1, call=fake))
        self.assertEqual((worker.result()["status"], worker.result()["spent_usd"],
                          worker.result()["model_calls"]), ("failed", "0.000014", 1))

    def test_provider_usage_over_declared_limit_stops_run(self):
        worker = self.worker()

        async def fake(*args, **kwargs):
            return {}, Usage(input=2000, cached_input=0, cache_write=0, output=1, reasoning=None)

        with self.assertRaisesRegex(cfo_worker.BudgetStop, "declared call token limits"):
            asyncio.run(worker.call("code", "prompt", {}, max_input_tokens=5,
                                    max_output_tokens=1, call=fake))
        self.assertEqual(worker.result()["status"], "capped")

    def test_completed_output_records_spend_against_cap(self):
        worker = self.worker()
        self.assertEqual((worker.finish()["status"], worker.result()["spent_usd"],
                          worker.result()["remaining_usd"]), ("completed", "0", "0.004"))

    def test_cached_usage_is_priced_exactly(self):
        worker = self.worker()
        worker._add_usage(Usage(input=2, cached_input=3, cache_write=1, output=1, reasoning=None))
        self.assertEqual(worker.result()["spent_usd"], "0.0000168")


if __name__ == "__main__":
    unittest.main()
