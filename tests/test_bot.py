"""Issue bot checks use saved Prism outputs and mocked GitHub responses only."""

import json
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from prism import bot, cli, github_context


FIXTURES = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)


def event(action="opened", association="OWNER", comment=None, repo="owner/repo", number=12):
    issue = {"number": number, "author_association": association,
             "html_url": f"https://github.com/{repo}/issues/{number}",
             "title": "A title", "body": "$(touch /tmp/never) `echo unsafe`; $HOME"}
    if comment is not None:
        issue["author_association"] = "OWNER"
        comment = {"body": comment, "author_association": association}
    return {"action": "created" if comment is not None else action, "issue": issue,
            "comment": comment, "repository": {"full_name": repo}}


class BotTests(unittest.TestCase):
    def test_only_insider_opened_issues_and_exact_comment_command(self):
        for association in bot.INSIDERS:
            self.assertIsNone(bot.refusal(event(association=association)))
            self.assertIsNone(bot.refusal(event(association=association, comment="/prism estimate")))
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            self.assertIsNotNone(bot.refusal(event(association=association)))
            self.assertIsNotNone(bot.refusal(event(association=association, comment="/prism estimate")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism estimate-now")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism estimate again")))
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
        self.assertNotIn("Confidence", rendered)
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
        self.assertIn("spent before stopping", bot.render(None, None, "predict failed").lower())
        self.assertIn("call in flight may still be billed", bot.render(None, None, "predict failed"))

    def test_partial_failure_labels_known_subtotal_and_unknown_additional_spend(self):
        prediction = json.loads((FIXTURES / "predict-httpx-2788.json").read_text())
        prediction["openai"] = {"status": "failed", "error": {"message": "request timed out"}}
        failed = bot.render(prediction, None)
        self.assertIn("known subtotal $0.0181", failed)
        self.assertIn("additional spend unknown for openai", failed)
        estimate = json.loads((FIXTURES / "estimate-sympy-20590.json").read_text())["estimate"]
        estimate["discovery_actual_cost"] = bot.solve.discovery_cost(prediction)
        completed = bot.render(prediction, estimate)
        self.assertIn("known subtotal $0.0181", completed)
        self.assertIn("additional spend unknown for openai", completed)
        self.assertIn("excludes unknown discovery spend", completed)

    def test_timeout_after_recorded_usage_still_has_unknown_additional_spend(self):
        prediction = json.loads((FIXTURES / "predict-httpx-2788.json").read_text())
        prediction["openai"] = {"status": "failed", "estimated_cost_usd": 0.003,
                                "model_calls": 1, "budget": {"stopped_by": "timeout_seconds"}}
        estimate = json.loads((FIXTURES / "estimate-sympy-20590.json").read_text())["estimate"]
        estimate["discovery_actual_cost"] = bot.solve.discovery_cost(prediction)
        for rendered in (bot.render(prediction, None), bot.render(prediction, estimate)):
            self.assertIn("known subtotal $0.0211", rendered)
            self.assertIn("additional spend unknown for openai", rendered)
        self.assertIn("excludes unknown discovery spend", bot.render(prediction, estimate))

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

    def test_event_pairs_and_untrusted_text_stay_data(self):
        for repo, number, comment in (("one/repo", 12, None), ("two/project", 93, "/prism estimate")):
            item = event(repo=repo, number=number, comment=comment)
            self.assertIsNone(bot.refusal(item))
            self.assertIn("$(touch", item["issue"]["body"])
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "event.json"
                path.write_text(json.dumps(item))
                with mock.patch.object(github_context, "api_v2", return_value=[]), redirect_stdout(io.StringIO()):
                    cli.main(["issue-gate", "--event", str(path)])
        self.assertIsNotNone(bot.refusal(event(comment="hello")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism estimate", association="NONE")))
        self.assertIsNotNone(bot.refusal(event(comment="/prism estimate") | {"issue": {"pull_request": {}}}))
        bot_event = event(comment="/prism estimate")
        bot_event["comment"]["user"] = {"type": "Bot", "login": "other[bot]"}
        self.assertIsNotNone(bot.refusal(bot_event))

    def test_predict_accepts_issue_commit_without_benchmark_pr(self):
        with mock.patch("prism.github_context.build_context", side_effect=ValueError("context reached")) as build, \
             self.assertRaisesRegex(ValueError, "context reached"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r",
                      "--commit", "a" * 40, "--checkout", "/tmp/source"])
        self.assertEqual(build.call_args.kwargs["commit"], "a" * 40)
        self.assertIsNone(build.call_args.kwargs["pr_url"])

    def test_predict_event_supplies_the_triggering_issue_text(self):
        item = event(repo="o/r", number=1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "event.json"
            path.write_text(json.dumps(item))
            with mock.patch("prism.github_context.build_context", side_effect=ValueError("context reached")) as build, \
                 self.assertRaisesRegex(ValueError, "context reached"):
                cli.main(["predict", item["issue"]["html_url"], "--repo", "gh/o/r", "--event", str(path),
                          "--commit", "a" * 40, "--checkout", "/tmp/source"])
            self.assertEqual(build.call_args.kwargs["issue_override"]["body"], item["issue"]["body"])
            with mock.patch("prism.github_context.build_context") as build, \
                 self.assertRaisesRegex(SystemExit, "does not match"):
                cli.main(["predict", item["issue"]["html_url"], "--repo", "gh/other/repo", "--event", str(path),
                          "--commit", "a" * 40, "--checkout", "/tmp/source"])
            build.assert_not_called()

    def test_workflow_resolves_default_and_historical_refs_once(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/prism-estimate.yml").read_text()
        section = workflow.split("      - name: Resolve one immutable source commit\n", 1)[1]
        block = section.split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        script = "\n".join(line.removeprefix("          ") for line in block.splitlines())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gh = root / "gh"
            gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$2\" >> \"$CALLS\"\n"
                          "case \"$2\" in\n  repos/o/r) echo main;;\n"
                          "  repos/o/r/commits/main) printf '%040d\\n' 1;;\n"
                          "  repos/o/r/commits/old%2Fbranch) printf '%040d\\n' 2;;\n"
                          "  *) exit 1;;\nesac\n")
            gh.chmod(0o755)
            for ref, expected in (("", "0" * 39 + "1"), ("old/branch", "0" * 39 + "2")):
                output, calls = root / "output", root / "calls"
                output.write_text("")
                calls.write_text("")
                env = os.environ | {"PATH": f"{root}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r",
                                    "REPLAY_REF": ref, "GITHUB_OUTPUT": str(output), "CALLS": str(calls)}
                result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(), f"commit={expected}\n")
                self.assertEqual(calls.read_text().splitlines(),
                                 (["repos/o/r"] if not ref else []) +
                                 ["repos/o/r/commits/main" if not ref else "repos/o/r/commits/old%2Fbranch"])

    def test_predict_refuses_an_unpaired_pre_merge_pin_before_network(self):
        with mock.patch("prism.entire.ensure_ready") as ready, \
             self.assertRaisesRegex(SystemExit, "--pre-merge-sha requires --pr-url"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r",
                      "--pre-merge-sha", "a" * 40])
        ready.assert_not_called()

if __name__ == "__main__":
    unittest.main()
