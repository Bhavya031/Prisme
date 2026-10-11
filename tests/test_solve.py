"""Solve-cost estimation. Nothing here needs Glance, the network or a model.

The counting and pricing tests check arithmetic only. They say nothing about whether an estimate
matches what a coding worker really spends; only a recorded worker run can show that.
"""

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prism import cli, glance, models, solve, swebench, validation
from prism.types import Usage

CONTRACT = {"repository_commit", "worker_model", "measured_context_tokens", "discovery_actual_cost", "estimator_actual_cost",
            "solve_token_estimate", "solve_cost_estimate", "end_to_end_cost_estimate", "assumptions", "confidence",
            "estimation_method"}
COMMIT = "a" * 40
# One dollar per million input tokens and ten per million output: every expected figure can be checked by hand.
PRICING = {"model": "worker-x", "uncached_input_per_million": 1.0, "cached_input_per_million": 0.1, "output_per_million": 10.0}
FLAT = {"worker_turns": 2, "retry_fraction": 0.0, "worker_overhead_tokens": 100, "context_resend_fraction": 1.0,
        "tool_response_tokens_per_turn": 50, "output_tokens_per_turn": 10, "edit_output_tokens": 0}
ASSESSMENT = {"likely_changes": [{"path": "src/a.py", "change": "guard the empty case"}], "required_tests": ["tests/test_a.py"],
              "expected_changed_lines": 10, "investigation_effort": "high", "retry_risk": "low", "uncertainty": "high",
              "rationale": "small guarded change"}
PREDICTION = {
    "claude": {"provider": "claude", "model": "claude-sonnet-5-5", "predicted": ["src/a.py", "tests/test_a.py"], "model_calls": 2,
               "usage": {"input": 1000, "output": 100, "cached_input": 0, "cache_write": 0, "reasoning": None},
               "estimated_cost_usd": 0.003},
    "openai": {"provider": "openai", "status": "failed", "model_calls": 1, "estimated_cost_usd": 0.001,
               "usage": {"input": 500, "output": 0, "cached_input": 0, "cache_write": 0, "reasoning": 0}},
}


def fake_measure(text):
    """Stands in for Glance: one token per four characters."""
    return {"context_tokens": max(1, len(text) // 4), "encoding": "fake", "glance_version": "fake-1",
            "input_sha256": hashlib.sha256(text.encode()).hexdigest()}


def build(mode="heuristic", **options):
    per_token, pricing = solve.worker_prices("worker-x", PRICING)
    return solve.estimate(issue_text="Crash on empty input", files={"src/a.py": "def a():\n    return 1\n" * 20},
                          commit=COMMIT, worker_model="worker-x", per_token=per_token, pricing=pricing,
                          measure=fake_measure, mode=mode, **options)


class ArithmeticOnly(unittest.TestCase):
    """Arithmetic checks of counting and pricing. Not evidence of estimator accuracy."""

    def test_token_formula_for_one_level(self):
        # 2 turns: input = 100*2 overhead + 1000 context + 1000 resent once + (50+10)*2*1/2 history = 2260.
        self.assertEqual(solve.level_tokens(1000, FLAT),
                         {"input": 2260, "uncached_input": 2260, "cached_input": 0, "output": 20, "total": 2280,
                          "worker_turns_including_retries": 2})
        # Retries add turns: ceil(2 * 1.5) = 3 -> 300 + 1000 + 2000 + 60*3 = 3480; edit tokens add to output only.
        retried = solve.level_tokens(1000, FLAT | {"retry_fraction": 0.5, "edit_output_tokens": 7})
        self.assertEqual((retried["input"], retried["output"], retried["worker_turns_including_retries"]), (3480, 37, 3))
        # A resend fraction of 0 sends the context once.
        self.assertEqual(solve.level_tokens(1000, FLAT | {"context_resend_fraction": 0})["input"], 1260)

    def test_no_cache_hit_rate_is_assumed_and_a_supplied_one_is_split_out(self):
        self.assertEqual(solve.level_tokens(1000, FLAT)["cached_input"], 0)
        split = solve.level_tokens(1000, FLAT, 0.5)
        self.assertEqual((split["uncached_input"], split["cached_input"], split["input"]), (1130, 1130, 2260))
        estimate = build()
        self.assertIsNone(estimate["assumptions"]["cache"]["cache_read_fraction"])
        self.assertTrue(all(estimate["solve_token_estimate"][level]["cached_input"] == 0 for level in solve.LEVELS))

    def test_uncached_cached_and_output_are_priced_apart(self):
        per_token, pricing = solve.worker_prices("worker-x", PRICING)
        level = {"uncached_input": 1_000_000, "cached_input": 2_000_000, "output": 100_000}
        priced = solve.cost_estimate({name: level for name in solve.LEVELS}, per_token)["central"]
        self.assertEqual(priced, {"usd": 2.2, "uncached_input_usd": 1.0, "cached_input_usd": 0.2, "output_usd": 1.0})
        self.assertEqual(pricing["usd_per_million_tokens"], {"uncached_input": 1.0, "cached_input": 0.1, "output": 10.0})
        self.assertIn("subscription", pricing["billing"].lower())
        without_cache_price, _ = solve.worker_prices("worker-x", {key: value for key, value in PRICING.items()
                                                                  if key != "cached_input_per_million"})
        with self.assertRaisesRegex(ValueError, "no cached-input price"):
            solve.cost_estimate({name: level for name in solve.LEVELS}, without_cache_price)

    def test_price_sources_need_an_explicit_model(self):
        self.assertEqual(solve.worker_prices("claude-sonnet-5-5")[1]["usd_per_million_tokens"],
                         {"uncached_input": 2.0, "cached_input": 0.1, "output": 10.0})
        catalog = {"m": {"input_cost_per_token": 3e-06, "output_cost_per_token": 6e-06}}
        self.assertEqual(solve.worker_prices("m", catalog=catalog)[1]["usd_per_million_tokens"],
                         {"uncached_input": 3.0, "cached_input": None, "output": 6.0})
        for arguments, message in ((("unknown-model",), "No price for worker model"), (("",), "worker model is required"),
                                   (("other", PRICING), "must name the model it prices"),
                                   (("worker-x", {"model": "worker-x", "output_per_million": 1}), "uncached_input_per_million")):
            with self.assertRaisesRegex(ValueError, message):
                solve.worker_prices(*arguments)

    def test_end_to_end_is_discovery_plus_estimator_plus_solve(self):
        estimate = build(discovery=solve.discovery_cost(PREDICTION))
        # Discovery counts every provider that spent tokens, the failed one included.
        self.assertEqual(estimate["discovery_actual_cost"]["usd"], 0.004)
        self.assertEqual(estimate["estimator_actual_cost"]["usd"], 0.0)
        for level in solve.LEVELS:
            total = estimate["end_to_end_cost_estimate"][level]
            self.assertAlmostEqual(total["usd"], 0.004 + 0.0 + estimate["solve_cost_estimate"][level]["usd"], places=6)
            self.assertEqual(total["solve_estimate_usd"], estimate["solve_cost_estimate"][level]["usd"])
        self.assertIsNone(build()["discovery_actual_cost"]["usd"])
        self.assertIn("not included", build()["assumptions"]["end_to_end"])

    def test_reasoning_is_not_added_to_the_planning_call_cost(self):
        usage = Usage(input=1000, output=200, cached_input=0, cache_write=0, reasoning=150)
        _, actual, _ = solve.plan("issue", {"a.py": "x"}, fake_measure, provider="openai", call=lambda *args: (ASSESSMENT, usage))
        self.assertAlmostEqual(actual["usd"], (1000 * 2 + 200 * 10) / 1_000_000)


class ModeTests(unittest.TestCase):
    def test_heuristic_mode_makes_no_model_call_and_claims_nothing(self):
        with mock.patch.object(models, "call", side_effect=AssertionError("no model call in heuristic mode")):
            estimate = build()
        self.assertEqual((estimate["estimation_method"], estimate["confidence"]), ("heuristic", "low"))
        self.assertEqual(estimate["estimator_actual_cost"]["model_calls"], 0)
        self.assertIn("Unvalidated", estimate["assumptions"]["status"])
        tokens = estimate["solve_token_estimate"]
        for field in ("input", "output"):
            self.assertLess(tokens["low"][field], tokens["central"][field])
            self.assertLess(tokens["central"][field], tokens["high"][field])
        self.assertNotIn("model_assessment", estimate["assumptions"])
        text = json.dumps(estimate).lower()
        self.assertFalse(any(word in text for word in ("validated against", "empirically", "calibrated multiplier")))

    def test_assumptions_are_configurable_and_checked(self):
        assumed = solve.assumptions({"central": {"worker_turns": 20}, "cache_read_fraction": 0.25})
        self.assertEqual((assumed["levels"]["central"]["worker_turns"], assumed["levels"]["low"]["worker_turns"]), (20, 5))
        self.assertEqual(build(assumed=assumed)["assumptions"]["levels"]["central"]["worker_turns"], 20)
        self.assertIn("Caller-supplied", build(assumed=assumed)["assumptions"]["cache"]["note"])
        for overrides, message in (({"central": {"turns": 3}}, "Unknown central assumption"), ({"median": {}}, "Unknown assumption group"),
                                   ({"low": {"worker_turns": 0}}, "at least 1"), ({"cache_read_fraction": 2}, "between 0 and 1"),
                                   ({"high": {"retry_fraction": -1}}, "nonnegative"),
                                   ({"model_adjustments": {"output_tokens_per_changed_line": "many"}}, "same shape as its default"),
                                   ({"model_adjustments": {"retry_risk_retry_multiplier": {"low": 1}}}, "same shape as its default"),
                                   ({"model_adjustments": {"max_changed_lines": {"low": 1}}}, "same shape as its default")):
            with self.assertRaisesRegex(ValueError, message):
                solve.assumptions(overrides)
        with self.assertRaisesRegex(ValueError, "low <= central <= high"):
            build(assumed=solve.assumptions({"low": {"worker_turns": 500}}))

    def test_model_assisted_mode_makes_one_bounded_call_and_records_it_apart(self):
        calls = []

        def call(provider, prompt, max_output_tokens):
            calls.append((provider, prompt, max_output_tokens))
            return ASSESSMENT, Usage(input=3000, output=400, cached_input=0, cache_write=0, reasoning=None)

        plain = build()
        estimate = build("model_assisted", planner={"provider": "claude", "max_output_tokens": 700, "call": call})
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0][0], calls[0][2]), ("claude", 700))
        self.assertIn("Crash on empty input", calls[0][1])
        self.assertIn("File: src/a.py", calls[0][1])
        actual = estimate["estimator_actual_cost"]
        self.assertEqual((actual["model_calls"], actual["usage"]["input"], actual["usage"]["output"]), (1, 3000, 400))
        self.assertAlmostEqual(actual["usd"], (3000 * 2 + 400 * 10) / 1_000_000)
        # The planning cost is in the end-to-end figure and nowhere in the solve estimate.
        central = estimate["end_to_end_cost_estimate"]["central"]
        self.assertAlmostEqual(central["usd"], actual["usd"] + estimate["solve_cost_estimate"]["central"]["usd"], places=6)
        self.assertEqual((estimate["estimation_method"], estimate["confidence"]), ("model_assisted", "low"))
        self.assertIn("prediction, not ground truth", estimate["assumptions"]["model_assessment"]["status"])
        # Same transparent model, moved by the assessment: high effort x1.6 turns, one test, low retry risk halves retries.
        levels = estimate["assumptions"]["levels"]
        self.assertEqual((levels["central"]["worker_turns"], levels["central"]["retry_fraction"]), (15 * 1.6 + 1, 0.125))
        self.assertEqual(levels["high"]["worker_turns"], 40 * 1.6 * 1.5 + 2)
        self.assertEqual(levels["central"]["edit_output_tokens"], 135)
        self.assertGreater(estimate["solve_token_estimate"]["high"]["input"], plain["solve_token_estimate"]["high"]["input"])
        self.assertEqual(estimate["measured_context_tokens"], plain["measured_context_tokens"])

    def test_planning_call_is_bounded_before_it_is_made(self):
        call = mock.Mock(side_effect=AssertionError("must not be called"))
        files = {"big.py": "x = 1\n" * 40_000}
        with self.assertRaisesRegex(ValueError, "above --planner-max-cost-usd.*No call was made"):
            solve.plan("issue", files, fake_measure, max_cost_usd=0.0001, call=call)
        with self.assertRaisesRegex(ValueError, "Planner limits must be positive"):
            solve.plan("issue", files, fake_measure, max_input_tokens=0, call=call)
        prompts = []

        def record(provider, prompt, max_output_tokens):
            prompts.append(prompt)
            return ASSESSMENT, Usage(input=1, output=1, cached_input=0, cache_write=0, reasoning=None)

        _, _, bounds = solve.plan("issue", files, fake_measure, max_input_tokens=2000, call=record)
        self.assertTrue(bounds["context_truncated"])
        self.assertLessEqual(bounds["context_tokens_sent"], 2000)
        self.assertLessEqual(fake_measure(prompts[0])["context_tokens"], 2100)
        self.assertIn(solve.TRUNCATION_MARK, prompts[0])

    def test_cost_check_counts_the_whole_request_with_a_tokenizer_margin(self):
        # The live call that prompted this: Glance counted 7868 tokens of files, the provider billed 13555 input.
        counted = lambda text: {"context_tokens": 7868 if text.startswith("Issue:") else 8300}
        with self.assertRaisesRegex(ValueError, r"about \$0\.0482 \(request counted at 8300 tokens, x2 .*No call was made"):
            solve.plan("issue", {"a.py": "x"}, counted, max_cost_usd=0.04, call=mock.Mock(side_effect=AssertionError))
        usage = Usage(input=13555, output=1500, cached_input=0, cache_write=0, reasoning=None)
        _, actual, bounds = solve.plan("issue", {"a.py": "x"}, counted, call=lambda *args: (ASSESSMENT, usage))
        self.assertEqual((bounds["request_tokens_counted"], bounds["tokenizer_safety_factor"]), (8300, 2.0))
        # Even with every output token used, that call stays under the ceiling the check computed.
        self.assertLess(actual["usd"], bounds["pre_call_cost_ceiling_usd"])
        self.assertIn("Only the output cap is enforced by the provider", bounds["note"])
        # The counted request holds the system prompt and schema as well as the prompt.
        seen = []
        solve.plan("issue", {"a.py": "x"}, lambda text: seen.append(text) or fake_measure(text),
                   call=lambda *args: (ASSESSMENT, usage))
        self.assertTrue(seen[-1].startswith(solve.PLAN_SYSTEM))
        self.assertIn('"investigation_effort"', seen[-1])
        self.assertIn(solve.PLAN_INSTRUCTION, seen[-1])

    def test_failed_planning_call_reports_its_spend_and_bad_assessments_are_refused(self):
        def billed(*args):
            raise models.Truncated("cut off", Usage(input=1000, output=100, cached_input=0, cache_write=0, reasoning=None))

        with self.assertRaisesRegex(RuntimeError, r"failed after spending an estimated \$0\.003000"):
            solve.plan("issue", {"a.py": "x"}, fake_measure, call=billed)
        with self.assertRaisesRegex(RuntimeError, "Planning call failed: ConnectionError: rate limited"):
            solve.plan("issue", {"a.py": "x"}, fake_measure, call=mock.Mock(side_effect=ConnectionError("rate limited")))
        with self.assertRaisesRegex(ValueError, "invalid retry_risk"):
            solve.apply_assessment(solve.DEFAULT_LEVELS, ASSESSMENT | {"retry_risk": "certain"}, solve.DEFAULT_ADJUSTMENTS)
        # A wild line count from the model is capped rather than trusted.
        levels, _ = solve.apply_assessment(solve.DEFAULT_LEVELS, ASSESSMENT | {"expected_changed_lines": 10**9},
                                           solve.DEFAULT_ADJUSTMENTS)
        self.assertEqual(levels["low"]["edit_output_tokens"], 5000 * 12)

    def test_planning_call_goes_through_models_call_with_its_own_system_prompt(self):
        seen = {}

        async def call(provider, stage, prompt, schema, max_output_tokens, system=None):
            seen.update(provider=provider, stage=stage, schema=schema, system=system, cap=max_output_tokens)
            return ASSESSMENT, Usage(input=10, output=5, cached_input=0, cache_write=0, reasoning=None)

        with mock.patch.object(models, "call", call), mock.patch.object(models, "close", mock.AsyncMock()):
            assessment, _, _ = solve.plan("issue", {"a.py": "x"}, fake_measure, max_output_tokens=321)
        self.assertEqual((seen["provider"], seen["stage"], seen["cap"], seen["system"]),
                         ("claude", "solve-plan", 321, solve.PLAN_SYSTEM))
        self.assertEqual(seen["schema"], solve.ASSESSMENT_SCHEMA)
        self.assertEqual(assessment, ASSESSMENT)


class ContractTests(unittest.TestCase):
    def test_json_has_exactly_the_separated_keys(self):
        for estimate in (build(), build("model_assisted", planner={"call": lambda *args: (
                ASSESSMENT, Usage(input=1, output=1, cached_input=0, cache_write=0, reasoning=None))})):
            estimate = json.loads(json.dumps(estimate))
            self.assertEqual(set(estimate), CONTRACT)
            self.assertEqual(estimate["repository_commit"], COMMIT)
            self.assertEqual(estimate["worker_model"], "worker-x")
            self.assertIsInstance(estimate["measured_context_tokens"], int)
            for key in ("solve_token_estimate", "solve_cost_estimate", "end_to_end_cost_estimate"):
                self.assertEqual(set(estimate[key]), {"low", "central", "high"})
            self.assertEqual(set(estimate["solve_token_estimate"]["low"]),
                             {"input", "uncached_input", "cached_input", "output", "total", "worker_turns_including_retries"})
            self.assertEqual(set(estimate["solve_cost_estimate"]["low"]),
                             {"usd", "uncached_input_usd", "cached_input_usd", "output_usd"})
            self.assertEqual(estimate["discovery_actual_cost"]["scope"], "file_discovery")
            self.assertEqual(estimate["confidence"], "low")
            self.assertIn(estimate["estimation_method"], solve.METHODS)

    def test_existing_discovery_outputs_say_they_are_not_solve_cost(self):
        from prism import cost, policy, results

        self.assertEqual(results.COST_SCOPE, "file_discovery")
        prediction = {"claude": {"usage": PREDICTION["claude"]["usage"], "model": "claude-sonnet-5-5"}}
        catalog = {"claude-sonnet-5-5": {"litellm_provider": "anthropic", "mode": "chat", "input_cost_per_token": 2e-06,
                                         "output_cost_per_token": 1e-05}}
        report = cost.build_report(prediction, catalog, None, 1)
        self.assertEqual(report["cost_scope"], "file_discovery")
        self.assertIn("not solving the issue", cost.markdown_table(report))
        coding = policy.recommend({}, "coding", "simple", "low", 1, 60)
        self.assertIn("solve-estimate", coding["solve_cost"])


class GlanceTests(unittest.TestCase):
    def test_location_order_and_clear_error_when_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder, "glance")
            binary.write_text("#!/bin/sh\n")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            other = Path(folder, "other")
            other.write_text("#!/bin/sh\n")
            other.chmod(other.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {glance.ENV: str(other), "PATH": folder}):
                self.assertEqual(glance.locate(str(binary)), str(binary))
                self.assertEqual(glance.locate(), str(other))
                with self.assertRaisesRegex(glance.GlanceError, "--glance is not an executable file"):
                    glance.locate(str(Path(folder, "absent")))
            with mock.patch.dict(os.environ, {"PATH": folder}):
                os.environ.pop(glance.ENV, None)
                self.assertEqual(glance.locate(), str(binary))
            with mock.patch.dict(os.environ, {"PATH": str(Path(folder, "empty"))}):
                os.environ.pop(glance.ENV, None)
                with self.assertRaisesRegex(glance.GlanceError, "Pass --glance.*PRISM_GLANCE.*PATH.*does not fall back"):
                    glance.locate()

    def test_assembled_context_holds_issue_paths_separators_and_contents(self):
        text = glance.assemble("  The issue  ", {"a.py": "A = 1\n", "b/c.py": "C = 2\n"})
        self.assertEqual(text, "Issue:\nThe issue\n\nFile: a.py\n---\nA = 1\n\n---\n\nFile: b/c.py\n---\nC = 2\n\n---\n\n")

    def counted(self, text="context", **changes):
        data = text.encode()
        return {"schema_version": "1", "glance_version": "v9.9.9", "encoding": "o200k_base", "context_tokens": 4321,
                "input_bytes": len(data), "input_sha256": hashlib.sha256(data).hexdigest()} | changes

    def run_measure(self, payload, returncode=0, text="context", stderr=b"boom"):
        seen = {}

        def run(command, **options):
            seen.update(command=command, **options)
            stdout = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return SimpleNamespace(returncode=returncode, stdout=b"" if returncode else stdout, stderr=stderr)

        return glance.measure(text, glance="/bin/glance", run=run), seen

    def test_measure_pipes_exactly_the_bundle_to_glance_count_and_records_its_provenance(self):
        text = "Issue:\nna\u00efve \U0001f44d\r\n\nFile: a.py\n---\nA = 1\n\n---\n\n"
        measured, seen = self.run_measure(self.counted(text), text=text)
        self.assertEqual(seen["command"], ["/bin/glance", "count", "--json", "--encoding", "o200k_base", "-"])
        # The exact UTF-8 bytes go to stdin: no file, no directory, no config, nothing for Glance to select from.
        self.assertEqual(seen["input"], text.encode())
        self.assertEqual(measured, {"context_tokens": 4321, "encoding": "o200k_base", "glance_schema_version": "1",
                                    "glance_version": "v9.9.9", "input_bytes": len(text.encode()),
                                    "input_sha256": hashlib.sha256(text.encode()).hexdigest()})
        self.assertNotIn("env", seen)

    def test_measure_rejects_a_count_of_anything_but_the_bytes_it_sent(self):
        for changes, message in (({"schema_version": "2"}, "schema '2' is not the supported schema '1'"),
                                 ({"encoding": "cl100k_base"}, "used encoding 'cl100k_base'"),
                                 ({"input_bytes": 6}, "measured different input than Prism sent: 6 bytes"),
                                 ({"input_sha256": "0" * 64}, "measured different input than Prism sent"),
                                 ({"context_tokens": True}, "invalid token count"), ({"context_tokens": -1}, "invalid token count"),
                                 ({"context_tokens": "12"}, "invalid token count")):
            with self.assertRaisesRegex(glance.GlanceError, message):
                self.run_measure(self.counted(**changes))
        incomplete = self.counted()
        del incomplete["input_sha256"]
        # `glance estimate` output, a missing field, and non-JSON are all refused rather than read as a count.
        for payload in (incomplete, {"schema_version": "2", "context_tokens": 5, "selected_files": []}, b"recorded", b"[1]"):
            with self.assertRaisesRegex(glance.GlanceError, "output Prism cannot read"):
                self.run_measure(payload)

    def test_measure_has_no_fallback_when_count_is_missing_or_fails(self):
        calls = []

        def old_glance(command, **_):
            calls.append(command[1])
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b'glance: unknown command "count"\n')

        with self.assertRaisesRegex(glance.GlanceError, "unknown command.*predates the `count` command, upgrade.*does not fall back"):
            glance.measure("context", glance="/bin/glance", run=old_glance)
        self.assertEqual(calls, ["count"])
        # The upgrade hint does not depend on how Glance words the failure.
        with self.assertRaisesRegex(glance.GlanceError, "glance count failed: glance: no such verb.*upgrade"):
            self.run_measure(self.counted(), returncode=1, stderr=b"glance: no such verb")
        never = mock.Mock(side_effect=AssertionError("glance must not be called"))
        with self.assertRaisesRegex(glance.GlanceError, "over the 8388608-byte limit.*does not truncate"):
            glance.measure("x" * ((8 << 20) + 1), glance="/bin/glance", run=never)
        with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
            glance.measure("lone surrogate \ud83d", glance="/bin/glance", run=never)
        with self.assertRaisesRegex(ValueError, "only these encodings"):
            glance.measure("x", glance="/bin/glance", encoding="claude", run=never)

    def test_files_are_read_at_the_recorded_commit_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                                       *args], check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            Path(folder, "a.py").write_text("old = 1\n")
            git("add", "a.py")
            git("commit", "-q", "-m", "one")
            first = git("rev-parse", "HEAD")
            Path(folder, "a.py").write_text("new = 2\n")
            git("commit", "-q", "-am", "two")
            Path(folder, "a.py").write_text("uncommitted = 3\n")
            self.assertEqual(glance.resolve_commit(first[:10], folder), first)
            Path(folder, "blob.bin").write_bytes(b"\xff\xfe\x00")
            git("add", "blob.bin")
            git("commit", "-q", "-m", "three")
            third = git("rev-parse", "HEAD")
            self.assertEqual(glance.read_files(["a.py"], first, checkout=folder), ({"a.py": "old = 1\n"}, []))
            # Missing and non-text paths are reported, each with its reason, and every file comes from the one commit.
            self.assertEqual(glance.read_files(["a.py", "missing.py", "blob.bin", "a.py"], third, checkout=folder),
                             ({"a.py": "new = 2\n"}, [{"path": "missing.py", "reason": "not a file at the recorded commit"},
                                                      {"path": "blob.bin", "reason": "not UTF-8 text"}]))
            # A directory is not content: `git show` would have printed its listing.
            Path(folder, "pkg").mkdir()
            Path(folder, "pkg", "m.py").write_text("m = 1\n")
            git("add", "pkg")
            git("commit", "-q", "-m", "four")
            self.assertEqual(glance.read_files(["pkg", "pkg/m.py"], git("rev-parse", "HEAD"), checkout=folder),
                             ({"pkg/m.py": "m = 1\n"}, [{"path": "pkg", "reason": "not a file at the recorded commit"}]))
            with self.assertRaisesRegex(ValueError, "No selected file could be read.*missing.py: not a file at the recorded commit"):
                glance.read_files(["missing.py"], first, checkout=folder)
            with self.assertRaisesRegex(ValueError, "not found in checkout"):
                glance.resolve_commit("f" * 40, folder)
        with self.assertRaisesRegex(ValueError, "full 40-character SHA"):
            glance.resolve_commit("main")
        with self.assertRaisesRegex(ValueError, "file names alone"):
            glance.read_files([], COMMIT, github_repo="o/r", fetch=None)
        with self.assertRaisesRegex(ValueError, "exactly one source"):
            glance.read_files(["a.py"], COMMIT)

    def test_github_contents_are_requested_at_the_commit(self):
        import base64

        asked = []

        def fetch(path):
            asked.append(path)
            if "/contents/" in path:
                return {"type": "file", "size": 9, "sha": "blob1", "content": ""}
            return {"content": base64.b64encode(b"big = 1\n").decode()}

        self.assertEqual(glance.read_files(["src/a b.py"], COMMIT, github_repo="o/r", fetch=fetch), ({"src/a b.py": "big = 1\n"}, []))
        self.assertEqual(asked, [f"repos/o/r/contents/src/a%20b.py?ref={COMMIT}", "repos/o/r/git/blobs/blob1"])


@unittest.skipUnless(os.environ.get("PRISM_GLANCE_LIVE") and os.environ.get(glance.ENV),
                     "set PRISM_GLANCE_LIVE=1 and PRISM_GLANCE to check the real Glance binary")
class RealGlanceBundleTests(unittest.TestCase):
    """Opt-in: does the real `glance count` count exactly the multi-file bundle, nothing selected, cut or added?"""

    def count(self, text):
        return glance.measure(text, glance=os.environ[glance.ENV])["context_tokens"]

    def test_multi_file_bundle_is_counted_whole_with_nothing_added(self):
        files = {f"pkg/module_{index}.py": "".join(
            f"def function_{index}_{line}(value):\n    return value * {line} + {index}  # row {line}\n" for line in range(400))
            for index in range(3)}
        whole = glance.assemble("Crash on empty input\r\n\nSteps: call parse('') and read the na\u00efve traceback.", files)
        measured = glance.measure(whole, glance=os.environ[glance.ENV])
        self.assertEqual((measured["input_bytes"], measured["input_sha256"], measured["glance_schema_version"]),
                         (len(whole.encode()), hashlib.sha256(whole.encode()).hexdigest(), "1"))
        self.assertTrue(measured["glance_version"])
        cut = whole.index("File: pkg/module_2.py")
        total, first, second = measured["context_tokens"], self.count(whole[:cut]), self.count(whole[cut:])
        # Parts and whole agree up to a token or two at the join, with no frame to subtract: nothing dropped or added.
        self.assertLessEqual(abs(total - (first + second)), 3)
        self.assertGreater(first, second)
        # Text at the very end is counted, and empty input counts as zero tokens.
        self.assertGreater(self.count(whole + "zebra quokka wombat axolotl\n"), total)
        self.assertEqual(self.count(""), 0)

    def test_a_bundle_of_several_megabytes_is_not_truncated(self):
        block = "".join(f"value_{line} = compute(line={line}, scale={line % 7})\n" for line in range(30_000))
        self.assertGreater(len(block.encode()), 1 << 20)
        once, twice = self.count(block), self.count(block + block)
        self.assertLessEqual(abs(twice - 2 * once), 3)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name, "estimates", "task-1.json")
        with mock.patch.object(validation.results, "now", return_value="2026-10-11T10:00:00Z"):
            self.saved = validation.save_estimate(build(), self.path, "task-1")
        self.tokens = self.saved["estimate"]["solve_token_estimate"]

    def record(self, **options):
        central = self.tokens["central"]
        values = {"started_at": "2026-10-11T10:05:00Z", "run_status": "completed", "verified_outcome": "resolved",
                  "worker_model": "worker-x", "uncached_input": central["input"], "cached_input": 0, "cache_write": 0,
                  "output": central["output"], "usage_source": "provider-usage"} | options
        return validation.record_run(json.loads(self.path.read_text()), **values)

    def record_saved(self, saved):
        return validation.record_run(saved, started_at="2026-10-11T10:05:00Z", run_status="completed", verified_outcome="resolved",
                                     worker_model="worker-x", uncached_input=1, cached_input=0, cache_write=0, output=1)

    def test_estimate_is_saved_before_the_run_and_never_replaced(self):
        on_disk = json.loads(self.path.read_text())
        self.assertEqual((on_disk["schema"], on_disk["task_id"], on_disk["saved_at"]),
                         (validation.ESTIMATE_SCHEMA, "task-1", "2026-10-11T10:00:00Z"))
        self.assertEqual(set(on_disk["estimate"]), CONTRACT)
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            validation.save_estimate(build(), self.path, "task-1")
        with self.assertRaisesRegex(ValueError, "before the estimate was saved"):
            self.record(started_at="2026-10-11T09:59:59Z")
        with self.assertRaisesRegex(ValueError, "needs a UTC offset"):
            self.record(started_at="2026-10-11T10:05:00")
        tampered = json.loads(self.path.read_text())
        tampered["estimate"]["solve_cost_estimate"]["high"]["usd"] = 999
        for field, value in (("task_id", "another-task"), ("saved_at", "2020-01-01T00:00:00Z")):
            with self.assertRaisesRegex(ValueError, "changed after it was saved"):
                validation.record_run(json.loads(self.path.read_text()) | {field: value}, started_at="2026-10-11T10:05:00Z",
                                      run_status="completed", verified_outcome="resolved", worker_model="worker-x",
                                      uncached_input=1, cached_input=0, cache_write=0, output=1)
        with self.assertRaisesRegex(ValueError, "schema 'prism.solve-estimate/1'.*save a new one"):
            self.record_saved(json.loads(self.path.read_text()) | {"schema": "prism.solve-estimate/1"})
        with self.assertRaisesRegex(ValueError, "changed after it was saved"):
            validation.record_run(tampered, started_at="2026-10-11T10:05:00Z", run_status="completed", verified_outcome="resolved",
                                  worker_model="worker-x", uncached_input=1, cached_input=0, cache_write=0, output=1)
        with self.assertRaisesRegex(ValueError, "the estimate is for 'worker-x'"):
            self.record(worker_model="other")

    def test_solved_run_is_compared_with_the_estimate(self):
        record = self.record()
        self.assertEqual((record["outcome_class"], record["solve_cost_usd"]), ("solved", record["actual_cost"]["usd"]))
        for name in ("input_tokens", "output_tokens", "cost_usd"):
            self.assertEqual((record["comparison"][name]["position"], record["comparison"][name]["actual_over_central"]),
                             ("within", 1.0))
        self.assertIn("not a billed amount", record["actual_cost"]["basis"])
        # The run record carries where the context count came from.
        measurement = self.saved["estimate"]["assumptions"]["measurement"]
        self.assertEqual({key: record["context_measurement"][key] for key in ("glance_version", "input_sha256", "encoding")},
                         {"glance_version": "fake-1", "input_sha256": measurement["input_sha256"], "encoding": "fake"})
        self.assertEqual(record["context_measurement"]["measured_context_tokens"], self.saved["estimate"]["measured_context_tokens"])
        over = self.record(uncached_input=self.tokens["high"]["input"] + 1, billed_usd=12.5)
        self.assertEqual((over["comparison"]["input_tokens"]["position"], over["comparison"]["input_tokens"]["within_range"]),
                         ("above_high", False))
        self.assertEqual((over["solve_cost_usd"], over["actual_cost"]["basis"]), (12.5, "billed amount reported by the caller"))

    def test_cached_and_cache_write_tokens_are_priced_apart_in_the_actual_cost(self):
        record = self.record(uncached_input=1_000_000, cached_input=2_000_000, cache_write=500_000, output=100_000)
        # 1.5M at $1 + 2M at $0.10 + 0.1M at $10 = 1.5 + 0.2 + 1.0
        self.assertEqual((record["actual_cost"]["list_price_usd"], record["actual_tokens"]["input"]), (2.7, 3_500_000))

    def test_capped_or_interrupted_runs_are_incomplete_and_never_a_solve_cost(self):
        for status, outcome in (("capped", "unverified"), ("interrupted", "unverified"), ("failed", "unresolved"),
                                ("capped", "resolved")):
            record = self.record(run_status=status, verified_outcome=outcome, uncached_input=self.tokens["high"]["input"] * 2)
            self.assertEqual((record["outcome_class"], record["solve_cost_usd"], record["comparison"]),
                             ("incomplete", None, None))
            self.assertGreater(record["spend_before_stop_usd"], 0)
            self.assertTrue(record["already_above_high_estimate"]["input_tokens"])
            self.assertIn("not the cost of solving", record["note"])
        unresolved = self.record(verified_outcome="unresolved")
        self.assertEqual((unresolved["outcome_class"], unresolved["solve_cost_usd"]), ("completed_unresolved", None))
        self.assertEqual(self.record(verified_outcome="unverified")["outcome_class"], "completed_unverified")

    def test_compare_counts_solved_runs_only_and_makes_no_accuracy_claim(self):
        summary = validation.compare([self.record(), self.record(run_status="capped", verified_outcome="unverified"),
                                      self.record(uncached_input=self.tokens["high"]["input"] + 1)])
        self.assertEqual((summary["runs"], summary["solved_runs"], summary["incomplete_runs_excluded"]), (3, 2, 1))
        self.assertEqual(summary["by_outcome_class"], {"incomplete": 1, "solved": 2})
        self.assertEqual(summary["within_range_solved_runs"]["input_tokens"], 1)
        self.assertIsNone(summary["accuracy_claim"])
        self.assertIn("not evidence", summary["note"])
        empty = validation.compare([self.record(run_status="interrupted", verified_outcome="unverified")])
        self.assertEqual((empty["solved_runs"], empty["within_range_solved_runs"]), (0, None))
        with self.assertRaisesRegex(ValueError, "solve-record"):
            validation.compare([{"schema": "other"}])

    def test_runs_can_be_mirrored_to_glance_records_with_incomplete_ones_censored(self):
        solved = validation.glance_record_args(self.record(cached_input=7, cache_write=3, billed_usd=1.5), "r.jsonl")
        pairs = dict(zip(solved[1::2], solved[2::2]))
        self.assertEqual(solved[0], "record")
        self.assertEqual((pairs["--censored"], pairs["--outcome"], pairs["--actual-cached"], pairs["--spend"], pairs["--id"]),
                         ("none", "resolved", "7", "1.5", "task-1"))
        self.assertEqual(int(pairs["--actual-uncached"]), self.tokens["central"]["input"] + 3)
        self.assertEqual((int(pairs["--predicted-low"]), int(pairs["--predicted-high"])),
                         (self.tokens["low"]["total"], self.tokens["high"]["total"]))
        for options, censored in (({"run_status": "capped", "verified_outcome": "unverified"}, "budget-stopped"),
                                  ({"run_status": "failed", "verified_outcome": "unverified"}, "failed"),
                                  ({"verified_outcome": "unresolved"}, "failed")):
            args = validation.glance_record_args(self.record(**options), "r.jsonl")
            self.assertEqual(args[args.index("--censored") + 1], censored)
            self.assertNotIn("--spend", args)
        ran = []
        validation.mirror_to_glance(self.record(), "/bin/glance", "r.jsonl",
                                    run=lambda command, **_: ran.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertEqual(ran[0][:2], ["/bin/glance", "record"])
        with self.assertRaisesRegex(RuntimeError, "glance record failed: nope"):
            validation.mirror_to_glance(self.record(), "/bin/glance", "r.jsonl",
                                        run=lambda *_, **__: SimpleNamespace(returncode=1, stdout="", stderr="nope"))


GOLD_LINE = "    if value is None or not value.strip():  # guard added by the gold patch"
INSTANCE = {
    "instance_id": "demo__demo-1", "repo": "demo/demo", "base_commit": COMMIT,
    "problem_statement": "Crash on empty input\n\nCalling parse('') raises IndexError.",
    "hints_text": "A maintainer comment that arrived after the fix was discussed, long enough to be distinctive text.",
    "patch": ("--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,5 @@\n def parse(value):\n"
              f"+{GOLD_LINE}\n+        return []  # nothing to parse, hand back an empty list\n"
              "+    value = value.strip()  # normalise once before splitting the fields\n     return value.split()\n"),
    "test_patch": ("--- /dev/null\n+++ b/tests/test_empty.py\n@@ -0,0 +1,3 @@\n"
                   "+def test_parse_empty_returns_an_empty_list_for_blank_input():\n+    assert parse('') == []\n"),
    "FAIL_TO_PASS": json.dumps(["tests/test_empty.py::test_parse_empty_returns_an_empty_list_for_blank_input"]),
    "PASS_TO_PASS": ["tests/test_a.py::test_parse_words"],
}
BASE_FILES = {"src/a.py": "def parse(value):\n    return value.split()\n"}


class LeakTests(unittest.TestCase):
    def check(self, files=None, commit=COMMIT, **texts):
        return swebench.leak_check(INSTANCE, commit=commit, files=BASE_FILES if files is None else files,
                                   texts={"issue_text": INSTANCE["problem_statement"]} | texts)

    def test_only_the_visible_fields_reach_the_estimator(self):
        task = swebench.visible_task(INSTANCE)
        self.assertEqual(set(task), {"instance_id", "repo", "base_commit", "problem_statement"})
        self.assertFalse(set(task) & set(swebench.HIDDEN))
        with self.assertRaisesRegex(ValueError, "lacks base_commit"):
            swebench.visible_task(INSTANCE | {"base_commit": ""})

    def test_clean_inputs_pass_and_the_report_claims_nothing_about_accuracy(self):
        report = self.check(estimate=json.dumps(build()))
        self.assertEqual((report["status"], report["instance_id"]), ("passed", "demo__demo-1"))
        self.assertGreaterEqual(report["markers"]["patch_lines"], 3)
        self.assertEqual(report["markers"]["paths_created_by_patches"], 1)
        self.assertIn("nothing about estimate accuracy", report["scope"])

    def test_gold_patch_and_evaluator_material_are_caught(self):
        patched = "def parse(value):\n" + "\n".join(line[1:] for line in INSTANCE["patch"].splitlines() if line.startswith("+ ")) + "\n"
        cases = {
            "not the task's base_commit": dict(commit="b" * 40),
            "exist only in the gold or test patch": dict(files=BASE_FILES | {"tests/test_empty.py": "x"}),
            "block added by the gold or test patch": dict(files={"src/a.py": patched}),
            "issue_text contains a gold or test patch line": dict(issue_text=INSTANCE["problem_statement"] + "\n" + GOLD_LINE),
            "estimate contains an evaluator test id": dict(estimate="see " + json.loads(INSTANCE["FAIL_TO_PASS"])[0]),
            "prompt contains the withheld hints_text": dict(prompt=INSTANCE["hints_text"]),
            "estimate carries a hidden SWE-bench field": dict(estimate='{"test_patch": "..."}'),
        }
        for message, options in cases.items():
            with self.assertRaisesRegex(swebench.LeakError, message):
                self.check(**options)

    def test_leaks_inside_a_real_estimate_dict_are_caught_despite_json_escaping(self):
        quoted = '        raise ValueError("value must not be blank or None here")'
        instance = INSTANCE | {"patch": INSTANCE["patch"] + f"@@ -9,1 +9,2 @@\n+{quoted}\n"}
        block = "\n".join(line[1:] for line in INSTANCE["patch"].splitlines() if line.startswith("+ "))

        def check(**assessment):
            estimate = build()
            estimate["assumptions"]["model_assessment"] = {"rationale": "fine", **assessment}
            return swebench.leak_check(instance, commit=COMMIT, files=BASE_FILES,
                                       texts={"issue_text": INSTANCE["problem_statement"], "estimate": estimate})

        self.assertEqual(check()["status"], "passed")
        # In serialized JSON the quote and the newlines are escaped, so only the real string values can match.
        self.assertNotIn(quoted.strip(), json.dumps({"rationale": quoted}))
        for message, assessment in (("estimate contains a gold or test patch line", {"rationale": f"add {quoted.strip()}"}),
                                    ("estimate contains a gold or test patch", {"likely_changes": [{"change": block}]}),
                                    ("estimate contains the withheld hints_text", {"rationale": INSTANCE["hints_text"]}),
                                    ("estimate carries a hidden SWE-bench field", {"test_patch": "x"})):
            with self.assertRaisesRegex(swebench.LeakError, message):
                check(**assessment)

    def test_cli_withholds_hidden_fields_and_refuses_a_leaking_selection(self):
        seen = []

        def measure(text, **_):
            seen.append(text)
            return fake_measure(text)

        with tempfile.TemporaryDirectory() as folder:
            instance = Path(folder, "instance.json")
            instance.write_text(json.dumps(INSTANCE))
            pricing = Path(folder, "pricing.json")
            pricing.write_text(json.dumps(PRICING))
            saved = Path(folder, "pre-run.json")
            arguments = ["solve-estimate", "--swebench-instance", str(instance), "--worker-model", "worker-x",
                         "--pricing", str(pricing), "--glance", "/bin/glance"]
            stdout = io.StringIO()
            with mock.patch.object(cli, "_load_env"), mock.patch.object(glance, "locate", return_value="/bin/glance"), \
                 mock.patch.object(glance, "measure", measure), contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(io.StringIO()), \
                 mock.patch.object(glance, "read_files", return_value=(dict(BASE_FILES), [])) as read:
                cli.main([*arguments, "--files", "src/a.py", "--save", str(saved)])
                with self.assertRaisesRegex(SystemExit, "LeakError.*exist only in the gold or test patch"):
                    read.return_value = (BASE_FILES | {"tests/test_empty.py": "x"}, [])
                    cli.main([*arguments, "--files", "src/a.py", "tests/test_empty.py"])
            estimate = json.loads(stdout.getvalue())
            self.assertEqual(set(estimate), CONTRACT)
            self.assertEqual(estimate["repository_commit"], COMMIT)
            self.assertEqual(read.call_args_list[0].kwargs, {"checkout": None, "github_repo": "demo/demo"})
            self.assertEqual(estimate["assumptions"]["leak_check"]["status"], "passed")
            on_disk = saved.read_text()
            self.assertEqual(json.loads(on_disk)["task_id"], "demo__demo-1")
            for hidden in (GOLD_LINE, "test_parse_empty_returns", INSTANCE["hints_text"], "test_parse_words"):
                self.assertNotIn(hidden, on_disk)
                self.assertFalse(any(hidden in text for text in seen))


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.object(cli, "_load_env"):
            try:
                cli.main([str(value) for value in argv])
            except SystemExit as error:
                code = error.code
        return stdout.getvalue(), stderr.getvalue(), code

    def test_estimate_record_and_compare_keep_stdout_json_and_diagnostics_on_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "issue.txt").write_text("Crash on empty input")
            (folder / "prediction.json").write_text(json.dumps(PREDICTION))
            (folder / "pricing.json").write_text(json.dumps(PRICING))
            base = ["solve-estimate", "--issue-file", folder / "issue.txt", "--prediction", folder / "prediction.json",
                    "--commit", COMMIT, "--github-repo", "o/r", "--worker-model", "worker-x", "--pricing", folder / "pricing.json"]
            with mock.patch.object(glance, "locate", return_value="/bin/glance"), \
                 mock.patch.object(glance, "measure", lambda text, **_: fake_measure(text)), \
                 mock.patch.object(glance, "read_files", return_value=(
                     {"src/a.py": "a = 1\n"}, [{"path": "tests/test_a.py", "reason": "missing at the recorded commit"}])) as read:
                stdout, stderr, code = self.run_cli(*base, "--save", folder / "pre.json", "--task-id", "task-9")
            self.assertIsNone(code)
            estimate = json.loads(stdout)
            self.assertEqual(set(estimate), CONTRACT)
            # The only completed prediction is chosen; the failed provider's spend still counts as discovery cost.
            self.assertEqual(read.call_args.args[0], ["src/a.py", "tests/test_a.py"])
            self.assertEqual(estimate["discovery_actual_cost"]["usd"], 0.004)
            self.assertIn("saved pre-run estimate", stderr)
            self.assertIn("measuring 1 file(s)", stderr)
            self.assertIn("excluded from the measured context: tests/test_a.py (missing at the recorded commit)", stderr)
            measurement = estimate["assumptions"]["measurement"]
            self.assertEqual((measurement["included_paths"], measurement["excluded_paths"], measurement["encoding"]),
                             (["src/a.py"], [{"path": "tests/test_a.py", "reason": "missing at the recorded commit"}], "fake"))
            self.assertIn("measured selected-context tokens", measurement["label"])
            self.assertNotIn("glance_wrapper", measurement)
            self.assertIn("glance count", measurement["tool"])

            started = "2099-01-01T00:00:00Z"
            usage = ["--worker-model", "worker-x", "--uncached-input", 5000, "--output", 300, "--started-at", started]
            stdout, _, code = self.run_cli("solve-record", "--estimate", folder / "pre.json", "--run-status", "capped",
                                           "--verified-outcome", "unverified", *usage, "--out", folder / "capped.json")
            self.assertEqual((code, json.loads(stdout)["outcome_class"], json.loads(stdout)["solve_cost_usd"]),
                             (None, "incomplete", None))
            stdout, _, code = self.run_cli("solve-record", "--estimate", folder / "pre.json", "--run-status", "completed",
                                           "--verified-outcome", "resolved", *usage, "--out", folder / "capped.json")
            self.assertEqual(stdout, "")
            self.assertIn("never overwritten", str(code))
            # The record is written, parent folder included, before the Glance append; a failed write appends nothing.
            order = []
            nested = folder / "runs" / "new" / "solved.json"
            record = ["solve-record", "--estimate", folder / "pre.json", "--run-status", "completed", "--verified-outcome",
                      "resolved", *usage, "--glance-records", folder / "glance.jsonl", "--glance", "/bin/glance"]
            with mock.patch.object(glance, "locate", return_value="/bin/glance"), \
                 mock.patch.object(validation, "mirror_to_glance", side_effect=lambda *_: order.append(nested.exists())):
                _, _, code = self.run_cli(*record, "--out", nested)
                self.assertEqual((code, order, json.loads(nested.read_text())["outcome_class"]), (None, [True], "solved"))
                with mock.patch.object(validation.results, "write_json", side_effect=OSError("disk full")):
                    stdout, _, code = self.run_cli(*record, "--out", folder / "runs" / "other.json")
                self.assertEqual((stdout, order), ("", [True]))
                self.assertIn("disk full", str(code))
            stdout, _, code = self.run_cli("solve-compare", folder / "capped.json")
            self.assertEqual((code, json.loads(stdout)["solved_runs"], json.loads(stdout)["accuracy_claim"]), (None, 0, None))

    def test_missing_glance_and_bad_inputs_are_errors_with_empty_stdout(self):
        with tempfile.TemporaryDirectory() as folder:
            issue = Path(folder, "issue.txt")
            issue.write_text("Crash")
            base = ["solve-estimate", "--issue-file", issue, "--files", "a.py", "--github-repo", "o/r"]
            with mock.patch.dict(os.environ, {"PATH": folder}):
                os.environ.pop(glance.ENV, None)
                stdout, _, code = self.run_cli(*base, "--commit", COMMIT, "--worker-model", "claude-sonnet-5-5")
            self.assertEqual(stdout, "")
            self.assertRegex(str(code), "prism solve-estimate: GlanceError: Glance not found. Pass --glance")
            with mock.patch.object(glance, "locate", return_value="/bin/glance"), \
                 mock.patch.object(glance, "read_files", side_effect=subprocess.TimeoutExpired("git", 60)):
                stdout, _, code = self.run_cli(*base, "--commit", COMMIT, "--worker-model", "claude-sonnet-5-5")
            self.assertEqual(stdout, "")
            self.assertIn("prism solve-estimate: TimeoutExpired", str(code))
            for extra, message in ((["--worker-model", "claude-sonnet-5-5"], "--commit is required"),
                                   (["--commit", COMMIT, "--worker-model", "nobody-knows"], "No price for worker model"),
                                   (["--commit", "main", "--worker-model", "claude-sonnet-5-5", "--glance", "/bin/sh"],
                                    "full 40-character SHA")):
                stdout, _, code = self.run_cli(*base, *extra)
                self.assertEqual(stdout, "")
                self.assertIn(message, str(code))


if __name__ == "__main__":
    unittest.main()
