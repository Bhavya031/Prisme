import asyncio
import hashlib
import json
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from prism import cfo_actions, cfo_run, glance, models
from prism.types import Usage


class CFORunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checkout = Path(self.temp.name, "repo")
        self.checkout.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.checkout), *args], text=True).strip()
        git("init", "-q")
        git("remote", "add", "origin", "https://github.com/Bhavya031/k-2.git")
        (self.checkout / "code.py").write_text("value = 1\n")
        git("add", "code.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
        self.commit = git("rev-parse", "HEAD")
        self.event = {"repository": {"full_name": "Bhavya031/k-2"},
                      "issue": {"number": 40, "html_url": "https://github.com/Bhavya031/k-2/issues/40",
                                "title": "Fix value", "body": "Set it to two."}}
        issue_text = "Fix value\n\nSet it to two."
        digest = hashlib.sha256(glance.assemble(issue_text, {"code.py": "value = 1\n"}).encode()).hexdigest()
        self.estimate = {"repository_commit": self.commit, "worker_model": "claude-sonnet-5-5",
                         "end_to_end_cost_estimate": {"central": {"usd": Decimal("0.045123456789")}},
                         "assumptions": {"pricing": {"usd_per_million_tokens": {
                             "uncached_input": Decimal("2.0"), "cached_input": Decimal("0.1"), "output": Decimal("10.0")}},
                             "measurement": {"included_paths": ["code.py"], "input_sha256": digest}}}
        self.allocation = {"request_id": "request-1", "allocation_id": "allocation-1",
                           "repository": "Bhavya031/k-2", "issue": 40,
                           "issue_url": self.event["issue"]["html_url"], "pinned_commit": self.commit,
                           "usd_cap": Decimal("0.045123456789"), "decision": "allocated"}

    def test_coding_call_edits_only_selected_file_and_reconciles_exact_usd(self):
        calls = []
        async def fake(*args, **kwargs):
            calls.append(args)
            return {"edits": [{"path": "code.py", "content": "value = 2\n"}], "done": True}, Usage(
                input=5, cached_input=0, cache_write=0, output=1, reasoning=None)
        result = asyncio.run(cfo_run.run(self.estimate, self.allocation, self.event, self.checkout, call=fake))
        self.assertEqual((len(calls), (self.checkout / "code.py").read_text()), (1, "value = 2\n"))
        self.assertEqual(result["changed_paths"], ["code.py"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reconciliation"], {"request_id": "request-1", "allocation_id": "allocation-1",
                          "usd_cap": "0.045123456789", "actual_usd": "0.00002",
                          "unused_reservation_usd": "0.045103456789"})

    def test_refuses_bad_allocation_or_checkout_before_model_call(self):
        async def never(*args, **kwargs):
            self.fail("model call before gate")
        for allocation, event in ((self.allocation | {"decision": "denied"}, self.event),
                                  (self.allocation | {"issue": 41}, self.event),
                                  (self.allocation | {"repository": "other/repo"}, self.event),
                                  (self.allocation | {"pinned_commit": "other"}, self.event),
                                  (self.allocation, self.event | {"issue": self.event["issue"] | {"body": "changed"}})):
            with self.subTest(allocation=allocation, event=event), self.assertRaises(ValueError):
                asyncio.run(cfo_run.run(self.estimate, allocation, event, self.checkout, call=never))
        (self.checkout / "untracked").write_text("x")
        with self.assertRaisesRegex(ValueError, "Checkout"):
            asyncio.run(cfo_run.run(self.estimate, self.allocation, self.event, self.checkout, call=never))

    def test_cap_stops_before_second_call_and_keeps_first_usage(self):
        calls = []
        async def fake(*args, **kwargs):
            calls.append(1)
            return {"edits": [{"path": "code.py", "content": "value = 2\n"}], "done": False}, Usage(
                input=1000, cached_input=0, cache_write=0, output=100, reasoning=None)
        result = asyncio.run(cfo_run.run(self.estimate, self.allocation, self.event, self.checkout, call=fake))
        self.assertEqual((len(calls), result["status"], result["reconciliation"]["actual_usd"]),
                         (1, "capped", "0.003"))

    def test_rejects_edits_outside_selected_paths(self):
        async def fake(*args, **kwargs):
            return {"edits": [{"path": "../escape", "content": "bad"}], "done": True}, Usage(
                input=5, cached_input=0, cache_write=0, output=1, reasoning=None)
        result = asyncio.run(cfo_run.run(self.estimate, self.allocation, self.event, self.checkout, call=fake))
        self.assertEqual((result["status"], (self.checkout / "code.py").read_text()), ("failed", "value = 1\n"))

    def test_billed_provider_failure_still_reconciles_reported_usage(self):
        async def fake(*args, **kwargs):
            raise models.Truncated("limit", Usage(input=5, cached_input=0, cache_write=0,
                                                   output=1, reasoning=None))
        result = asyncio.run(cfo_run.run(self.estimate, self.allocation, self.event, self.checkout, call=fake))
        self.assertEqual((result["status"], result["error_type"], result["reconciliation"]["actual_usd"]),
                         ("failed", "Truncated", "0.00002"))

    def test_writeback_plan_uses_matching_snapshot_and_never_calls_cfo(self):
        prefix = cfo_actions.PREFIX
        record = {"request_id": "request-1", "allocation_id": "allocation-1",
                  "usd_cap": "0.045123456789", "actual_usd": "0.00002",
                  "unused_reservation_usd": "0.045103456789"}
        budget = ({"as_of": 1}, [("budget", {prefix + "repository": "Bhavya031/k-2",
                                              prefix + "spending USD": "1.00"})])
        request = ({"as_of": 2}, [("request-1", {prefix + "allocation ID": "allocation-1",
                                                   prefix + "decision": "allocated",
                                                   prefix + "allocated USD cap": "0.045123456789",
                                                   prefix + "reservation USD": "0.045123456789",
                                                   prefix + "request repository": "Bhavya031/k-2"})])
        plan = cfo_run.writeback_plan(record, budget, request)
        self.assertTrue(plan["requires_atomic_application"])
        self.assertEqual(plan["updates"][0]["arguments"]["update"]["values"]["rows"],
                         [["budget", "1.00002"]])
        self.assertEqual(plan["updates"][1]["arguments"]["update"]["values"]["rows"],
                         [["request-1", "reconciled", "0"]])
        with self.assertRaises(ValueError):
            cfo_run.writeback_plan(record | {"allocation_id": "wrong"}, budget, request)
        with self.assertRaises(ValueError):
            cfo_run.writeback_plan(record | {"unused_reservation_usd": "0"}, budget, request)


if __name__ == "__main__":
    unittest.main()
