import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prism import cfo_actions


PREFIX = cfo_actions.PREFIX
REQUEST_ID = "SYNTHETIC/prism-ci-k2-issue40-run38138900610"
COMMIT = "79a6a4f18909e999d471036958455a9625d21035"
BASE_ROW = {
    PREFIX + "request repository": "Bhavya031/k-2",
    PREFIX + "issue URL": "https://github.com/Bhavya031/k-2/issues/40",
    PREFIX + "pinned commit": COMMIT,
    PREFIX + "worker model": "claude-sonnet-5-5",
    PREFIX + "estimated USD": 1.40262,
    PREFIX + "decision": "allocated",
    PREFIX + "allocation ID": "SYNTHETIC/prism-test-allocation-" + REQUEST_ID,
    PREFIX + "allocated USD cap": 1.40262,
    PREFIX + "reservation USD": 1.40262,
    PREFIX + "reason": "central end-to-end estimate is within available balance and policy cap",
}
BUDGET = {PREFIX + "repository": "Bhavya031/k-2", PREFIX + "budget USD": 25,
          PREFIX + "spending USD": 0, PREFIX + "existing reservations USD": 0,
          PREFIX + "max budget fraction": 0.2}
OLD = {PREFIX + "request repository": "Bhavya031/k-2", PREFIX + "issue URL": "old",
       PREFIX + "pinned commit": "old", PREFIX + "worker model": "old",
       PREFIX + "estimated USD": 1.40415, PREFIX + "decision": "allocated",
       PREFIX + "allocation ID": "old", PREFIX + "allocated USD cap": 1.40415,
       PREFIX + "reservation USD": 1.40415, PREFIX + "available balance USD": 23.59585,
       PREFIX + "reason": "old"}


class CFOActions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.estimate = Path(self.directory.name) / "estimate.json"
        self.estimate.write_text(json.dumps({
            "repository_commit": COMMIT,
            "worker_model": "claude-sonnet-5-5",
            "end_to_end_cost_estimate": {"central": {"usd": 1.40262}},
            "solve_token_estimate": {"central": {"input": 636519, "uncached_input": 636519,
                                                  "cached_input": 0, "output": 11400, "total": 647919}},
        }))
        self.request_data = {"as_of": 10, "columns": [{"display_name": PREFIX + name}
                                                        for name in cfo_actions.REQUEST_COLUMNS]}

    def test_additive_write_then_readback(self):
        new = BASE_ROW | {PREFIX + "available balance USD": 22.19323}
        reads = [(None, [("budget", BUDGET)]), (self.request_data, [("old", OLD)]),
                 (self.request_data, [("old", OLD)]),
                 (self.request_data, [("old", OLD), (REQUEST_ID, new)])]
        with patch.object(cfo_actions, "refresh_access_token", return_value="access"), \
             patch.object(cfo_actions, "read_table", side_effect=reads), \
             patch.object(cfo_actions, "mcp", side_effect=[{"capabilities": {}}, {}]) as call:
            result = cfo_actions.allocate(self.estimate, 38138900610)
        self.assertEqual((result["request_id"], result["usd_cap"]), (REQUEST_ID, 1.40262))
        self.assertEqual((result["request_row_count"], result["table_row_count"]), (1, 2))
        self.assertAlmostEqual(result["request_reservation_usd"], 1.40262)
        self.assertAlmostEqual(result["table_reservation_usd"], 2.80677)
        write = call.call_args_list[1].args[2]["arguments"]
        self.assertEqual(write["org_id"], "102909")
        self.assertEqual(write["update"]["target"], cfo_actions.REQUEST_TABLE)
        self.assertEqual(write["update"]["dimensions"][0]["add_items"], [REQUEST_ID])
        self.assertEqual(write["update"]["values"]["rows"][0][-3], 1.40262)

    def test_repeat_reuses_row_without_write(self):
        existing = BASE_ROW | {PREFIX + "available balance USD": 22.19323}
        reads = [(None, [("budget", BUDGET)]), (self.request_data, [("old", OLD), (REQUEST_ID, existing)]),
                 (self.request_data, [("old", OLD), (REQUEST_ID, existing)])]
        with patch.object(cfo_actions, "refresh_access_token", return_value="access"), \
             patch.object(cfo_actions, "read_table", side_effect=reads), \
             patch.object(cfo_actions, "mcp", return_value={"capabilities": {}}) as call:
            result = cfo_actions.allocate(self.estimate, 38138900610)
        self.assertEqual(result["decision"], "allocated")
        self.assertEqual((result["request_row_count"], result["table_row_count"]), (1, 2))
        self.assertAlmostEqual(result["request_reservation_usd"], 1.40262)
        self.assertAlmostEqual(result["table_reservation_usd"], 2.80677)
        self.assertEqual(call.call_count, 1)

    def test_request_id_uses_issue_and_estimate_run(self):
        request_id = "SYNTHETIC/prism-ci-k2-issue41-run38138900611"
        issue_url = "https://github.com/Bhavya031/k-2/issues/41"
        existing = BASE_ROW | {PREFIX + "issue URL": issue_url,
                               PREFIX + "available balance USD": 23.59738}
        reads = [(None, [("budget", BUDGET)]), (self.request_data, [(request_id, existing)]),
                 (self.request_data, [(request_id, existing)])]
        with patch.object(cfo_actions, "refresh_access_token", return_value="access"), \
             patch.object(cfo_actions, "read_table", side_effect=reads), \
             patch.object(cfo_actions, "mcp", return_value={"capabilities": {}}) as call:
            result = cfo_actions.allocate(self.estimate, 38138900611, 41)
        self.assertEqual((result["request_id"], result["issue_url"]), (request_id, issue_url))
        self.assertEqual(call.call_count, 1)

    def test_secret_write_failure_does_not_rotate_refresh_token(self):
        secrets = {"CFO_OAUTH_CLIENT_ID": "client", "CFO_OAUTH_REFRESH_TOKEN": "refresh",
                   "CFO_SECRET_WRITE_TOKEN": "writer"}
        with patch.dict(os.environ, secrets), \
             patch.object(cfo_actions.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, stderr="gh: Resource not accessible by personal access token (HTTP 403)")) as run, \
             patch.object(cfo_actions.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(RuntimeError, "OAuth refresh was not attempted: gh: .*HTTP 403"):
                cfo_actions.refresh_access_token()
        self.assertEqual(run.call_args.args[0][3], "CFO_SECRET_WRITE_TOKEN")
        urlopen.assert_not_called()

    def test_readback_issue_comes_from_request_url(self):
        issue_url = "https://github.com/Bhavya031/k-2/issues/41"
        row = BASE_ROW | {PREFIX + "issue URL": issue_url}
        request = {"repository": "Bhavya031/k-2", "issue_url": issue_url,
                   "pinned_commit": COMMIT, "model": "claude-sonnet-5-5", "estimated_usd": 1.40262}
        with patch.object(cfo_actions, "read_table", return_value=(None, [(REQUEST_ID, row)])):
            result = cfo_actions.allocate_existing("access", request, REQUEST_ID)
        self.assertEqual(result["issue"], 41)


if __name__ == "__main__":
    unittest.main()
