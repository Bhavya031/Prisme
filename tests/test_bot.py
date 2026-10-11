"""Issue bot checks use saved Prism outputs and mocked GitHub responses only."""

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from prism import bot, cli, github_context


FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)


def event(action="opened", association="OWNER", comment=None):
    issue = {"number": 12, "author_association": association}
    if comment is not None:
        issue["author_association"] = "OWNER"
        comment = {"body": comment, "author_association": association}
    return {"action": "created" if comment is not None else action, "issue": issue,
            "comment": comment, "repository": {"full_name": "owner/repo"}}


class BotTests(unittest.TestCase):
    def test_only_insider_opened_issues_and_exact_comment_command(self):
        for association in bot.INSIDERS:
            self.assertIsNone(bot.refusal(event(association=association)))
            self.assertIsNone(bot.refusal(event(association=association, comment="/prism estimate again")))
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            self.assertIsNotNone(bot.refusal(event(association=association)))
            self.assertIsNotNone(bot.refusal(event(association=association, comment="/prism estimate")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism estimate-now")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism\nestimate")))
        self.assertIsNotNone(bot.refusal(event(action="edited")))
        pull = event()
        pull["issue"]["pull_request"] = {}
        self.assertIsNotNone(bot.refusal(pull))

    def test_repository_limit_uses_only_bot_reservations_and_results(self):
        previous = {"user": {"login": "github-actions[bot]"}, "body": bot.MARKER + "\nEstimating",
                    "updated_at": "2026-10-11T03:55:00Z"}
        stranger = {"user": {"login": "other"}, "body": bot.MARKER,
                    "updated_at": "2026-10-11T03:59:00Z"}
        seen = []

        def fetch(path):
            seen.append(path)
            return [stranger, previous]

        note = bot.gate(event(), fetch, NOW)
        self.assertIn("04:05 UTC", note)
        self.assertIn("made no model call", note)
        self.assertIn("issues/comments", seen[0])
        self.assertIsNone(bot.gate(event(), lambda _: [stranger, previous],
                                   datetime(2026, 10, 11, 4, 5, tzinfo=timezone.utc)))
        never = mock.Mock(side_effect=AssertionError("outsider reached API"))
        with self.assertRaises(PermissionError):
            bot.gate(event(association="NONE"), never, NOW)
        never.assert_not_called()

    def test_rate_limit_paginates_busy_repositories(self):
        unrelated = {"user": {"login": "someone"}, "body": "hello", "updated_at": "2026-10-11T03:59:00Z"}
        ours = {"user": {"login": "github-actions[bot]"}, "body": bot.MARKER,
                "updated_at": "2026-10-11T03:55:00Z"}
        fetch = mock.Mock(side_effect=[[unrelated] * 100, [ours]])
        self.assertIn("04:05 UTC", bot.gate(event(), fetch, NOW))
        self.assertIn("page=2", fetch.call_args.args[0])

    def test_saved_estimate_table_and_saved_prediction_spend(self):
        saved = json.loads((FIXTURES / "estimate-sympy-20590.json").read_text())["estimate"]
        prediction = json.loads((FIXTURES / "predict-httpx-2788.json").read_text())
        rendered = bot.render(None, saved)
        for expected in ("66,041", "349,705 / 1,739,279 / 12,188,460", "$0.7114 / $3.5698 / $24.9529",
                         "not counted", "claude-sonnet-5-5", "cffd4e0f", "unvalidated guesses"):
            self.assertIn(expected, rendered)
        failure = bot.render(prediction, None, "Glance count failed")
        self.assertIn("$0.0181", failure)
        self.assertIn("Glance count failed", failure)
        self.assertIn("httpx/_auth.py", failure)

    def test_failure_redacts_secrets_and_reports_cap_spend(self):
        prediction = json.loads((FIXTURES / "predict-httpx-2788.json").read_text())
        prediction["claude"]["budget"]["stopped_by"] = "max_cost_usd"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-value-12345"}):
            rendered = bot.render(prediction, None, "failed secret-value-12345")
        self.assertIn("max_cost_usd", rendered)
        self.assertIn("$0.0181", rendered)
        self.assertNotIn("secret-value-12345", rendered)

    def test_cli_renders_saved_json_and_gates_without_network(self):
        output = io.StringIO()
        with redirect_stdout(output):
            cli.main(["issue-comment", "--estimate", str(FIXTURES / "estimate-sympy-20590.json")])
        self.assertIn("66,041", output.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "event.json"
            path.write_text(json.dumps(event()))
            output = io.StringIO()
            with mock.patch.object(github_context, "api_v2", return_value=[]), redirect_stdout(output):
                cli.main(["issue-gate", "--event", str(path)])
            self.assertEqual(output.getvalue(), "note=\n")

    def test_predict_refuses_an_unpaired_pre_merge_pin_before_network(self):
        with mock.patch("prism.entire.ensure_ready") as ready, \
             self.assertRaisesRegex(SystemExit, "--pre-merge-sha requires --pr-url"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r",
                      "--pre-merge-sha", "a" * 40])
        ready.assert_not_called()

if __name__ == "__main__":
    unittest.main()
