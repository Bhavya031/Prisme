import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from prism import cli, cost, entire, github_context, models, pipeline, policy
from prism.types import Bundle, Context, Usage

# Prices per token, chosen so every expected figure is easy to check by hand.
CATALOG = {
    "sample_spec": {"input_cost_per_token": 1},
    "claude-sonnet-5-5": {"litellm_provider": "anthropic", "mode": "chat", "input_cost_per_token": 2e-06,
                          "cache_read_input_token_cost": 1e-07, "cache_creation_input_token_cost": 2.5e-06,
                          "output_cost_per_token": 1e-05, "max_input_tokens": 1_000_000, "max_output_tokens": 128_000},
    "gpt-6-sol": {"litellm_provider": "openai", "mode": "responses", "input_cost_per_token": 2e-06,
                  "cache_read_input_token_cost": 2e-07, "output_cost_per_token": 1e-05,
                  "max_input_tokens": 922_000, "max_output_tokens": 128_000, "supports_response_schema": True},
    "deepseek-flash": {"litellm_provider": "deepseek", "mode": "chat", "input_cost_per_token": 1e-06,
                       "output_cost_per_token": 2e-06, "max_input_tokens": 1000},
    "gpt-embed": {"litellm_provider": "openai", "mode": "embedding", "input_cost_per_token": 1e-06, "output_cost_per_token": 0},
    "other/model": {"litellm_provider": "mistral", "mode": "chat", "input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06},
}
CLAUDE_TOKENS = {"input": 1000, "output": 100, "cached_input": 400, "cache_write": 200, "reasoning": None}
OPENAI_TOKENS = {"input": 1000, "output": 100, "cached_input": 400, "cache_write": 0, "reasoning": 60}


def graded(precision=0.5, recall=1.0, true_paths=("a.py", "b.py"), cost_usd=(0.02, 0.01)):
    return {
        "status": "ok", "wall_seconds": 30.0, "true_paths": list(true_paths),
        "claude": {"tokens": CLAUDE_TOKENS, "precision": precision, "recall": recall, "estimated_cost_usd": cost_usd[0]},
        "openai": {"tokens": OPENAI_TOKENS, "precision": precision, "recall": recall, "estimated_cost_usd": cost_usd[1]},
    }


def saved_result(cases=None, claude=(0.8, 0.7, 0.04), openai=(0.9, 0.6, 0.02)):
    """A result.json as `prism benchmark` saves it; summary values are (recall, precision, total USD)."""
    cases = [graded(), graded()] if cases is None else cases
    return {
        "run_id": "run-1", "stage": "s-test", "settings": {"models": {"claude": "claude-sonnet-5-5", "openai": "gpt-6-sol"}},
        "cases": cases + [{"status": "failed", "claude": {"tokens": {"input": 10**9}}}],
        "summary": {name: {"mean_recall": recall, "mean_precision": precision, "exact_matches": 1, "estimated_cost_usd": usd}
                    for name, (recall, precision, usd) in (("claude", claude), ("openai", openai))},
    }


class NoNetwork(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(cost.urllib.request, "urlopen", side_effect=AssertionError("network used in a test"))
        self.urlopen = patch.start()
        self.addCleanup(patch.stop)


class CostTests(NoNetwork):
    def test_cached_input_is_inside_openai_input_but_apart_from_claude_input(self):
        report = cost.build_report(saved_result([graded()]), CATALOG, "sha", new_cases=10)
        self.assertEqual(report["token_profiles"]["openai"]["uncached_input"], 600)
        self.assertEqual(report["token_profiles"]["claude"]["uncached_input"], 1000)
        by_name = {row["model"]: row for row in report["models"]}
        # claude: 1000 input + 400 cache read + 200 cache write + 100 output
        self.assertAlmostEqual(by_name["claude-sonnet-5-5"]["estimated_usd_per_case"], 0.00354)
        # openai: 600 uncached + 400 cached + 100 output; the 60 reasoning tokens are part of output
        self.assertAlmostEqual(by_name["gpt-6-sol"]["estimated_usd_per_case"], 0.00228)
        self.assertAlmostEqual(by_name["gpt-6-sol"]["estimated_usd_new_cases"], 0.0228)
        # The catalog agrees with Prism's own list prices for the tested models.
        for provider, tokens in (("claude", CLAUDE_TOKENS), ("openai", OPENAI_TOKENS)):
            self.assertAlmostEqual(report["tested_models"][provider]["estimated_usd_per_case"],
                                   models.estimate_cost(provider, Usage(**tokens)), places=6)
        self.assertAlmostEqual(report["current_model_pair"]["estimated_usd_new_cases"], 0.0582)

    def test_report_marks_measured_and_extrapolated_models_and_money_as_estimated(self):
        report = cost.build_report(saved_result(), CATALOG, "sha", new_cases=4)
        self.assertEqual((report["observed_cases"], report["cost_type"]), (2, "estimated_list_price"))
        self.assertEqual([row["model"] for row in report["models"]], ["deepseek-flash", "gpt-6-sol", "claude-sonnet-5-5"])
        deepseek, openai, claude = report["models"]
        self.assertEqual((openai["basis"], openai["mean_recall"], openai["mean_precision"]), ("measured", 0.9, 0.6))
        self.assertEqual((claude["basis"], claude["token_counts_from"]), ("measured", "claude"))
        # An untested model reuses OpenAI's counts, pays full input price for cached tokens, and has no accuracy.
        self.assertEqual((deepseek["basis"], deepseek["token_counts_from"], deepseek["mean_recall"]), ("extrapolated", "openai", None))
        self.assertAlmostEqual(deepseek["estimated_usd_per_case"], 0.0012)
        self.assertEqual((openai["observed_input_fits"], deepseek["observed_input_fits"]), (True, True))
        self.assertEqual(report["tested_models"]["claude"]["largest_observed_input_tokens"], 1600)
        self.assertEqual(report["five_hour_view"]["estimated_paired_runtime_minutes"], 2.0)
        self.assertTrue(all(key.startswith("estimated_usd") for row in report["models"] for key in row if "usd" in key and key != "usd_per_million_tokens"))
        self.assertIn("| gpt-6-sol | measured |", cost.markdown_table(report))
        self.assertNotIn("GLM-5.3 Flash on one B300", cost.markdown_table(report))

    def test_single_predict_output_is_priced_per_model(self):
        prediction = {"claude": {"provider": "claude", "usage": CLAUDE_TOKENS, "estimated_cost_usd": 0.00354, "model": "claude-sonnet-5-5"}}
        report = cost.build_report(prediction, CATALOG, None, new_cases=1)
        self.assertEqual((report["source"]["kind"], report["observed_cases"]), ("prediction", 1))
        self.assertNotIn("five_hour_view", report)
        by_name = {row["model"]: row for row in report["models"]}
        self.assertEqual(by_name["claude-sonnet-5-5"]["basis"], "measured")
        self.assertEqual((by_name["gpt-6-sol"]["basis"], by_name["gpt-6-sol"]["token_counts_from"]), ("extrapolated", "claude"))
        # 1600 input tokens do not fit this catalog's 1000-token deepseek entry.
        self.assertIs(by_name["deepseek-flash"]["observed_input_fits"], False)

    def test_bad_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "completed cases"):
            cost.build_report(saved_result([]), CATALOG, None, 10)
        with self.assertRaisesRegex(ValueError, "lacks a model used by the run: gpt-6-sol"):
            cost.build_report(saved_result(), {name: row for name, row in CATALOG.items() if name != "gpt-6-sol"}, None, 10)
        broken = graded()
        broken["openai"] = {**broken["openai"], "tokens": {**OPENAI_TOKENS, "cached_input": 2000}}
        with self.assertRaisesRegex(ValueError, "cached tokens exceed"):
            cost.build_report(saved_result([broken]), CATALOG, None, 10)
        with self.assertRaisesRegex(ValueError, "Expected a benchmark result"):
            cost.build_report({"nothing": 1}, CATALOG, None, 10)

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.object(cli, "_load_env"):
            try:
                cli.main(list(argv))
            except SystemExit as error:
                code = error.code
        return stdout.getvalue(), stderr.getvalue(), code

    def test_cli_prints_json_and_reports_an_unreachable_catalog_on_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            (run_dir / "result.json").write_text(json.dumps(saved_result()))
            (run_dir / "catalog.json").write_text(json.dumps(CATALOG))
            stdout, _, code = self.run_cli("cost", "--results", str(run_dir), "--catalog", str(run_dir / "catalog.json"), "--new-cases", "3")
            self.assertIsNone(code)
            self.assertEqual(json.loads(stdout)["new_cases_forecast"], 3)
            self.urlopen.assert_not_called()

            self.urlopen.side_effect = urllib.error.URLError("no route to host")
            stdout, _, code = self.run_cli("cost", "--results", str(run_dir))
            self.assertEqual(stdout, "")
            self.assertRegex(str(code), "Price catalog unavailable \\(no route to host\\)")

            stdout, _, code = self.run_cli("policy", "--results", str(run_dir), "--task", "file-finding", "--complexity", "simple",
                                           "--risk", "low", "--max-cost-usd", "0.1", "--timeout-seconds", "60")
            self.assertEqual((json.loads(stdout)["status"], code), ("recommended", None))
            stdout, _, code = self.run_cli("policy", "--results", str(run_dir / "missing"), "--task", "file-finding", "--complexity",
                                           "simple", "--risk", "low", "--max-cost-usd", "0.1", "--timeout-seconds", "60")
            self.assertEqual(stdout, "")
            self.assertIn("FileNotFoundError", str(code))


class PolicyTests(NoNetwork):
    def recommend(self, data=None, task="file-finding", complexity="simple", risk="low", max_cost_usd=0.1, **options):
        return policy.recommend(saved_result() if data is None else data, task, complexity, risk, max_cost_usd, 120, **options)

    def test_recommends_the_best_affordable_tested_model(self):
        result = self.recommend(issue_url="https://github.com/o/r/issues/1", repo="gh/me/r")
        self.assertEqual((result["status"], result["model"], result["provider"]), ("recommended", "gpt-6-sol", "openai"))
        self.assertEqual(result["evidence"]["estimated_usd_per_case"], 0.01)
        self.assertEqual(result["evidence_source"]["run_id"], "run-1")
        self.assertEqual(result["input_output_budget"]["remaining_for_retrieval_tokens"], 1_046_000)
        self.assertEqual(result["ready_to_run_cli"],
                         "prism predict https://github.com/o/r/issues/1 --repo gh/me/r --provider openai --format json "
                         "--max-cost-usd 0.1 --timeout-seconds 120 --max-output-tokens 4000 --max-extra-rounds 6")
        cli.parser().parse_args(result["ready_to_run_cli"].split()[1:])
        self.assertNotIn("/Users/", json.dumps(result))
        self.assertNotIn("entire_compare", json.dumps(result))
        # Between the two, only Claude fits a budget below OpenAI's average.
        self.assertEqual(self.recommend(data=saved_result(openai=(0.9, 0.6, 0.5)))["model"], "claude-sonnet-5-5")
        self.assertEqual(self.recommend(risk="high")["status"], "review_required")

    def test_statuses_without_a_recommendation(self):
        over = self.recommend(max_cost_usd=0.001)
        self.assertEqual((over["status"], over["model"], len(over["candidates"])), ("over_budget", None, 2))
        coding = self.recommend(task="coding")
        self.assertEqual((coding["status"], coding["model"]), ("needs_evaluation", None))
        overflow = self.recommend(input_tokens=1_050_000)
        self.assertEqual((overflow["status"], overflow["model"]), ("context_overflow", "gpt-6-sol"))
        self.assertIs(overflow["input_output_budget"]["can_retrieve_more"], False)
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self.recommend(max_cost_usd=0)
        with self.assertRaisesRegex(ValueError, "graded cases"):
            self.recommend(data=saved_result([]))
        with self.assertRaisesRegex(ValueError, "supplied together"):
            self.recommend(issue_url="https://github.com/o/r/issues/1")

    def test_context_file_sets_the_input_estimate_and_must_not_name_the_target_pr(self):
        result = self.recommend(context={"issue": "x" * 5000})
        self.assertGreater(result["input_output_budget"]["input_tokens"], 6000)
        self.assertIn("complete_model_prompt", result)
        with self.assertRaisesRegex(ValueError, "leaked"):
            self.recommend(context={"issue": "see #77"}, pr_url="https://github.com/o/r/pull/77")

    def test_capability_and_context_budget(self):
        cases = [graded(precision=1.0, recall=1.0), graded(precision=0.5, recall=0.5)]
        small_only = policy.capability(cases, "claude")
        self.assertEqual(small_only["small_change_evidence"], {"cases": 2, "full_recall_cases": 1, "mean_precision": 0.75})
        self.assertEqual(small_only["broad_change_evidence"], {"cases": 0, "mean_recall": None, "mean_precision": None})
        broad = policy.capability([graded(recall=0.25, true_paths=("a", "b", "c", "d"))], "openai")
        self.assertEqual(broad["broad_change_evidence"], {"cases": 1, "mean_recall": 0.25, "mean_precision": 0.5})

        budget = policy.context_budget("claude-sonnet-5-5", 900_000, 4000, 6, 6)
        self.assertEqual((budget["remaining_for_retrieval_tokens"], budget["can_retrieve_more"]), (96_000, False))
        with self.assertRaisesRegex(ValueError, "exceeds the model maximum"):
            policy.context_budget("gpt-6-sol", 0, 200_000, 0, 6)
        with self.assertRaisesRegex(ValueError, "No context limits"):
            policy.context_budget("untested-model", 0, 4000, 0, 6)


class BudgetTests(unittest.TestCase):
    """The pipeline with fake model calls: 1000 input and 100 output tokens each, $0.003 for either provider."""

    def setUp(self):
        self.calls = []
        self.need_more = ["#5 comments", "#6 comments", "#7 comments", None]
        self.delay = 0

        async def call(provider, stage, prompt, schema, max_output_tokens=models.MAX_TOKENS):
            await asyncio.sleep(self.delay)
            self.calls.append((stage, prompt, max_output_tokens))
            usage = Usage(input=1000, output=100, cached_input=0, cache_write=0, reasoning=None)
            if stage == "plan":
                return {"problem": "", "suspected_cause": "", "search_terms": ["term"], "expected_file_kinds": [], "brief": ""}, usage
            decisions = sum(stage == "decide" for stage, *_ in self.calls)
            return {"fix": ["src/a.py", "gone.py"], "tests": [], "changelog": [], "docs": [], "config": [],
                    "need_more": self.need_more[decisions - 1]}, usage

        dropped = ("NEATLOGS_API_KEY", "PRISM_CLAUDE_MODEL", "PRISM_OPENAI_MODEL")
        environment = {key: value for key, value in os.environ.items() if key not in dropped}
        for patch in (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(models, "call", side_effect=call),
            mock.patch.object(entire, "search_many", return_value={"term": []}),
            mock.patch.object(github_context, "fetch_reference", side_effect=lambda request, ctx: {"reference": request}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.ctx = Context(bundle=Bundle(issue={"title": "t"}, references=[], path_hints=[], repo_map={}),
                           known_paths={"src/a.py"}, safe_refs=set(), cutoff=None, target_pr=None, entire_repo="gh/o/r")

    def predict(self, **budget):
        return asyncio.run(pipeline.predict(self.ctx, "claude", **budget))

    def test_defaults_keep_one_extra_round_and_the_default_output_cap(self):
        result = self.predict()
        self.assertEqual([(stage, cap) for stage, _, cap in self.calls], [("plan", 4000), ("decide", 4000), ("decide", 4000)])
        self.assertTrue(self.calls[2][1].endswith('\n\nAdditional referenced item:\n{"reference": "#5 comments"}'))
        self.assertEqual((result.model_calls, result.need_more_used, result.predicted, result.dropped_nonexistent),
                         (3, True, ["src/a.py"], ["gone.py"]))
        self.assertAlmostEqual(result.estimated_cost_usd, 0.009)
        self.assertEqual({key: result.budget[key] for key in ("max_cost_usd", "max_output_tokens", "max_extra_rounds",
                                                              "timeout_seconds", "extra_rounds_used", "stopped_by", "over_max_cost")},
                         {"max_cost_usd": None, "max_output_tokens": 4000, "max_extra_rounds": 1, "timeout_seconds": None,
                          "extra_rounds_used": 1, "stopped_by": None, "over_max_cost": False})

    def test_cost_limit_stops_further_calls_after_the_call_that_reached_it(self):
        # $0.006 is reached by the decision, the second call: its answer is kept and no extra round runs.
        result = self.predict(max_cost_usd=0.005, max_extra=3)
        self.assertEqual((len(self.calls), result.model_calls, result.need_more_used), (2, 2, False))
        self.assertEqual((result.budget["stopped_by"], result.budget["over_max_cost"]), ("max_cost_usd", True))
        self.assertAlmostEqual(result.estimated_cost_usd, 0.006)
        self.assertGreater(result.estimated_cost_usd, result.budget["max_cost_usd"])
        self.assertIn("One call can cross max_cost_usd", result.budget["note"])

    def test_cost_limit_reached_by_the_plan_makes_no_second_call(self):
        with self.assertRaisesRegex(pipeline.BudgetStop, r"budget reached after plan: estimated \$0.003000 of \$0.003") as raised:
            self.predict(max_cost_usd=0.003)
        self.assertEqual(len(self.calls), 1)
        spent = raised.exception.spent
        self.assertEqual((spent["model_calls"], spent["usage"].input, spent["budget"]["stopped_by"]), (1, 1000, "max_cost_usd"))
        self.assertAlmostEqual(spent["estimated_cost_usd"], 0.003)

    def test_cost_limit_stops_between_extra_rounds(self):
        result = self.predict(max_cost_usd=0.008, max_extra=3)
        self.assertEqual((len(self.calls), result.budget["extra_rounds_used"], result.budget["stopped_by"]), (3, 1, "max_cost_usd"))

    def test_extra_round_and_output_limits(self):
        result = self.predict(max_extra=6, max_output_tokens=900)
        # Three requests, then the model stops asking; each round adds to the same prompt.
        self.assertEqual((len(self.calls), result.budget["extra_rounds_used"], result.budget["stopped_by"]), (5, 3, None))
        self.assertEqual({cap for _, _, cap in self.calls}, {900})
        self.assertEqual(self.calls[-1][1].count("Additional referenced item:"), 3)
        self.calls.clear()
        result = self.predict(max_extra=0)
        self.assertEqual((len(self.calls), result.need_more_used, result.budget["stopped_by"]), (2, False, None))

    def test_timeout_and_invalid_budgets(self):
        self.delay = 0.2
        with self.assertRaisesRegex(pipeline.BudgetStop, "timed out after 0.05s with 0 model call") as raised:
            self.predict(timeout_seconds=0.05)
        self.assertEqual((raised.exception.spent["estimated_cost_usd"], raised.exception.spent["budget"]["stopped_by"]),
                         (0.0, "timeout_seconds"))
        # Calls that finished before the timeout keep their usage.
        with self.assertRaisesRegex(pipeline.BudgetStop, "with 1 model call") as raised:
            self.predict(timeout_seconds=0.3)
        self.assertAlmostEqual(raised.exception.spent["estimated_cost_usd"], 0.003)
        for budget in ({"max_cost_usd": 0}, {"timeout_seconds": 0}, {"max_output_tokens": 0}, {"max_output_tokens": 128_001}, {"max_extra": -1}):
            with self.assertRaisesRegex(ValueError, "must be positive"):
                self.predict(**budget)

    def test_oversized_prompt_is_rejected_before_any_call(self):
        self.ctx.bundle.issue["body"] = "x" * 1_000_000
        with self.assertRaisesRegex(ValueError, "exceed model context window"):
            self.predict()
        self.assertEqual(self.calls, [])

    def test_cli_passes_budgets_and_labels_the_cost(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "_load_env"), mock.patch.object(entire, "ensure_ready"), \
             mock.patch.object(github_context, "build_context", return_value=self.ctx), contextlib.redirect_stdout(stdout):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r", "--provider", "openai", "--format", "json",
                      "--max-cost-usd", "0.005", "--max-extra-rounds", "3", "--timeout-seconds", "30", "--max-output-tokens", "800"])
        prediction = json.loads(stdout.getvalue())["openai"]
        self.assertEqual((prediction["model"], prediction["cost_type"]), ("gpt-6-sol", "estimated_list_price"))
        self.assertIn("neatlogs_trace_id", prediction)
        self.assertEqual(prediction["budget"] | {"note": None},
                         {"max_cost_usd": 0.005, "max_output_tokens": 800, "max_extra_rounds": 3, "timeout_seconds": 30.0,
                          "extra_rounds_used": 0, "stopped_by": "max_cost_usd", "over_max_cost": True, "note": None})
        self.assertNotIn("status", prediction)
        # The JSON a predict run prints is a valid input for `prism cost`.
        report = cost.build_report(json.loads(stdout.getvalue()), CATALOG, None, 1)
        self.assertAlmostEqual(report["tested_models"]["openai"]["estimated_usd_per_case"], prediction["estimated_cost_usd"])

    def test_cli_reports_a_budget_stop_as_json_with_what_was_spent(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_load_env"), mock.patch.object(entire, "ensure_ready"), \
             mock.patch.object(github_context, "build_context", return_value=self.ctx), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
             self.assertRaisesRegex(SystemExit, "All requested providers failed"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r", "--provider", "claude", "--format", "json",
                      "--max-cost-usd", "0.001"])
        failed = json.loads(stdout.getvalue())["claude"]
        self.assertEqual((failed["status"], failed["error"]["type"], failed["model_calls"], failed["usage"]["input"]),
                         ("failed", "BudgetStop", 1, 1000))
        self.assertAlmostEqual(failed["estimated_cost_usd"], 0.003)
        self.assertEqual((failed["budget"]["stopped_by"], failed["budget"]["over_max_cost"], failed["model"]),
                         ("max_cost_usd", True, "claude-sonnet-5-5"))
        self.assertIn("claude failed: Model cost budget reached after plan", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
