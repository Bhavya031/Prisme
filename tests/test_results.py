import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prism import benchmark, cli, github_context, pipeline, results
from prism.types import Prediction, Usage

SECRET = "sk-ant-test-0123456789abcdefSECRET"
# Row fields of the showcase runs.json for the pipeline stages (s2, s3), in order.
ROW_FIELDS = [
    "stage", "case_id", "provider", "model", "predicted", "true_files", "matched", "missed", "extra",
    "predicted_count", "true_count", "precision", "recall", "exact_match", "model_calls", "wall_seconds",
    "neatlogs_trace_id", "cost_usd", "cost_type", "tokens_input", "tokens_cached_input", "tokens_cache_write",
    "tokens_output", "tokens_total", "need_more_used", "condensed", "by_category",
]
SUMMARY_FIELDS = [
    "stage", "provider", "cases", "mean_precision", "mean_recall", "exact_matches", "total_tokens",
    "total_cost_usd", "cost_type", "mean_wall_seconds",
]


def manifest_cases(count):
    return [
        {"issue_url": f"https://github.com/o/repo{n}/issues/{n}", "pr_url": f"https://github.com/o/repo{n}/pull/9{n}",
         "fork": f"me/repo{n}", "pre_merge_sha": "a" * 40, "modified_existing_files": ["src/a.py"]}
        for n in range(1, count + 1)
    ]


def prediction(provider):
    usage = Usage(input=100, output=20, cached_input=10, cache_write=5, reasoning=7 if provider == "openai" else None)
    return Prediction(
        provider=provider, by_category={"fix": ["src/a.py"], "tests": ["tests/x.py"], "changelog": [], "docs": [], "config": []},
        predicted=["src/a.py", "tests/x.py"], dropped_nonexistent=[], model_calls=2, need_more_used=False,
        condensed=False, usage=usage, estimated_cost_usd=0.0123,
    )


class ResultSavingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.output = self.folder / "out"
        self.events = []
        self.fail = {}  # case number -> exception raised by a provider

        async def predict(ctx, provider):
            if provider == "openai" and ctx.number in self.fail:
                raise self.fail[ctx.number]
            self.events.append(("predict", ctx.number, provider))
            return prediction(provider)

        def build_context(issue_url, **_):
            return SimpleNamespace(number=int(issue_url.rsplit("/", 1)[-1]), known_paths={"src/a.py", "src/b.py", "tests/x.py"})

        def true_files(pr_url, known_paths):
            self.events.append(("truth", pr_url))
            return {"src/a.py", "src/b.py"}

        # Nothing from the real environment or a local .env may reach tracing or model selection.
        dropped = ("NEATLOGS_API_KEY", "PRISM_CLAUDE_MODEL", "PRISM_OPENAI_MODEL", "PRISM_ENTIRE_TRANSPORT")
        environment = {key: value for key, value in os.environ.items() if key not in dropped}
        environment.update({"ANTHROPIC_API_KEY": SECRET, "GITHUB_TOKEN": "ghp_" + "Z" * 36})
        for patch in (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(cli, "_load_env"),
            mock.patch.object(github_context, "build_context", side_effect=build_context),
            mock.patch.object(github_context, "true_files", side_effect=true_files),
            mock.patch.object(pipeline, "predict", side_effect=predict),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def manifest(self, count):
        path = self.folder / "cases.json"
        path.write_text(json.dumps(manifest_cases(count)))
        return path

    def run_benchmark(self, count=3, **options):
        with contextlib.redirect_stderr(io.StringIO()):
            return benchmark.run(self.manifest(count), one=False, dry_run=False, output_dir=self.output, **options)

    def saved_text(self):
        return "\n".join(path.read_text() for path in self.output.rglob("*") if path.is_file())

    def test_successful_run_saves_every_artifact(self):
        result = self.run_benchmark()
        run_dir = Path(result["saved"]["run_dir"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sorted(path.name for path in run_dir.iterdir()),
                         ["cases", "manifest.json", "result.json", "run.json", "runs.json", "stage.json"])
        self.assertEqual(len(list((run_dir / "cases").iterdir())), 3)
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text()), manifest_cases(3))
        saved = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(saved, json.loads(json.dumps(result)))
        self.assertEqual(saved["settings"]["models"], {"claude": "claude-sonnet-5-5", "openai": "gpt-6-sol"})
        self.assertIn("commit", saved["source"])
        self.assertIn("dirty", saved["source"])
        self.assertTrue(saved["started_at"].endswith("Z"))
        self.assertEqual(saved["cost"]["type"], "estimated_list_price")
        self.assertIsNone(saved["cost"]["recorded_usd"])
        self.assertAlmostEqual(saved["cost"]["estimated_usd"], 0.0123 * 6)
        case = saved["cases"][0]
        self.assertEqual(case["true_paths"], ["src/a.py", "src/b.py"])
        self.assertEqual(case["claude"]["matched"], ["src/a.py"])
        self.assertEqual(case["claude"]["missed"], ["src/b.py"])
        self.assertEqual(case["claude"]["extra"], ["tests/x.py"])
        self.assertEqual((case["claude"]["precision"], case["claude"]["recall"]), (0.5, 0.5))
        self.assertEqual(case["claude"]["tokens"]["input"], 100)
        self.assertIn("claude_seconds", case["durations"])
        self.assertEqual(saved["summary"]["openai"]["model_calls"], 6)

    def test_truth_is_fetched_only_after_both_predictions(self):
        self.run_benchmark(count=2)
        for number in (1, 2):
            own = [event for event in self.events if number in event or event[-1].endswith(f"/pull/9{number}")]
            self.assertEqual([event[0] for event in own], ["predict", "predict", "truth"])

    def test_runs_never_share_a_directory(self):
        frozen = mock.Mock(wraps=results.datetime)
        frozen.now.return_value = results.datetime(2026, 10, 10, tzinfo=results.timezone.utc)
        with mock.patch.object(results, "datetime", frozen), \
             mock.patch.object(results.secrets, "token_hex", side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"]):
            first = self.run_benchmark(count=1)
            second = self.run_benchmark(count=1)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["saved"]["run_dir"], second["saved"]["run_dir"])
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.assertEqual(json.loads(Path(first["saved"]["result"]).read_text())["run_id"], first["run_id"])

    def test_failed_case_keeps_the_others(self):
        self.fail[2] = RuntimeError(f"401 from provider, Authorization: Bearer {SECRET} x-api-key={SECRET}")
        result = self.run_benchmark()
        self.assertEqual(result["status"], "partial")
        self.assertEqual([case["status"] for case in result["cases"]], ["ok", "failed", "ok"])
        failed = result["cases"][1]
        self.assertEqual(failed["error"]["type"], "RuntimeError")
        self.assertIn("401 from provider", failed["error"]["message"])
        self.assertEqual(failed["claude"]["graded"], False)
        self.assertNotIn("true_paths", failed)
        self.assertNotIn(("truth", "https://github.com/o/repo2/pull/92"), self.events)
        rows = json.loads(Path(result["saved"]["runs_export"]).read_text())
        self.assertEqual(sorted({row["case_id"] for row in rows}), ["repo1#1", "repo3#3"])
        self.assertNotIn(SECRET, self.saved_text())

    def test_all_cases_failing_is_a_failed_run(self):
        self.fail.update({1: RuntimeError("boom"), 2: RuntimeError("boom")})
        result = self.run_benchmark(count=2)
        self.assertEqual((result["status"], result["summary"], result["cases_failed"]), ("failed", {}, 2))
        self.assertEqual(json.loads(Path(result["saved"]["runs_export"]).read_text()), [])

    def test_interruption_leaves_finished_cases_readable(self):
        self.fail[3] = KeyboardInterrupt()
        seen = []
        original = results.RunStore.save_case

        def save_case(store, case):
            path = original(store, case)
            # Each case and the run record are complete on disk as soon as the case ends.
            seen.append((json.loads(path.read_text())["status"], json.loads((store.path / "run.json").read_text())["status"]))
            return path

        with mock.patch.object(results.RunStore, "save_case", save_case):
            result = self.run_benchmark(count=4)
        self.assertEqual(seen, [("ok", "running"), ("ok", "running")])
        run_dir = Path(result["saved"]["run_dir"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error"]["type"], "KeyboardInterrupt")
        self.assertEqual(len(list((run_dir / "cases").iterdir())), 2)
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["cases_completed"], 2)
        self.assertEqual(list(run_dir.rglob("*.tmp")), [])

    def test_failed_write_does_not_replace_the_previous_file(self):
        path = self.folder / "file.json"
        results.write_json(path, {"ok": 1})
        with self.assertRaises(TypeError):
            results.write_json(path, {"bad": object()})
        self.assertEqual(json.loads(path.read_text()), {"ok": 1})
        self.assertEqual([item.name for item in self.folder.iterdir()], ["file.json"])

    def test_export_matches_the_showcase_schema(self):
        result = self.run_benchmark(count=2, stage="s4-new-repos")
        rows = json.loads(Path(result["saved"]["runs_export"]).read_text())
        self.assertEqual(len(rows), 4)
        claude, openai = rows[0], rows[1]
        self.assertEqual(list(claude), ROW_FIELDS)
        self.assertEqual(list(openai), ROW_FIELDS[:24] + ["tokens_reasoning"] + ROW_FIELDS[24:])
        self.assertEqual((claude["stage"], claude["case_id"], claude["provider"]), ("s4-new-repos", "repo1#1", "claude"))
        self.assertEqual(claude["model"], "claude-sonnet-5-5, effort medium (Anthropic Messages API)")
        self.assertEqual(openai["model"], "gpt-6-sol, effort medium (OpenAI Responses API)")
        self.assertEqual((claude["cost_usd"], claude["cost_type"]), (0.0123, "estimated_list_price"))
        self.assertEqual((claude["predicted_count"], claude["true_count"]), (2, 2))
        self.assertEqual((claude["tokens_total"], openai["tokens_total"]), (135, 120))
        stage = json.loads(Path(result["saved"]["stage_export"]).read_text())
        self.assertEqual([list(row) for row in stage["summary"]], [SUMMARY_FIELDS, SUMMARY_FIELDS])
        self.assertEqual(stage["summary"][0]["cases"], 2)

    def test_default_stage_is_unique_and_existing_stages_are_refused(self):
        result = self.run_benchmark(count=1)
        self.assertEqual(result["stage"], f"run-{result['run_id']}")
        with self.assertRaisesRegex(ValueError, "already has results"):
            self.run_benchmark(count=1, stage="s3-pipeline-clean")
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_no_secret_reaches_any_saved_file(self):
        self.fail[1] = RuntimeError(f"bad key {SECRET}; token ghp_{'Z' * 36}; https://user:hunter2pass@host/x")
        result = self.run_benchmark(count=2)
        text = self.saved_text() + json.dumps(result)
        for secret in (SECRET, "ghp_" + "Z" * 36, "hunter2pass"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_cli_prints_json_with_saved_paths_and_progress_on_stderr(self):
        self.fail[2] = RuntimeError("boom")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            cli.main(["benchmark", "--manifest", str(self.manifest(2)), "--output-dir", str(self.output)])
        self.assertEqual(stopped.exception.code, 1)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["status"], "partial")
        self.assertTrue(Path(printed["saved"]["result"]).is_file())
        self.assertIn("FAILED RuntimeError: boom", stderr.getvalue())

    def test_dry_run_writes_no_run_record(self):
        ctx = SimpleNamespace(target_pr=None, bundle=Usage(1, 1, 0, 0, None))
        with mock.patch.object(github_context, "build_context", return_value=ctx), \
             mock.patch.object(results, "RunStore") as store, mock.patch.object(results, "write_json") as write:
            benchmark.run(self.manifest(2), one=False, dry_run=True, output_dir=self.output)
        store.assert_not_called()
        write.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
