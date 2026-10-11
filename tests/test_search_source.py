import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prism import cli, entire, models, pipeline
from prism.types import Bundle, Context, Hit, Usage

SECRET = "entire-token-value-123456"
PLAN = {"expected_file_kinds": []}


class SearchSourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        git = ["git", "-C", folder.name, "-c", "user.name=Test", "-c", "user.email=test@example.test"]
        subprocess.run([*git, "init", "-q"], check=True)
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_text("shared pinned text\n")
        (self.root / "src/quiet.py").write_text("nothing to match\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "pinned"], check=True)
        self.pinned = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        # What Entire's newer index holds: changed text in a.py and a file the pin does not have.
        (self.root / "src/a.py").write_text("shared indexed later\n")
        (self.root / "src/new.py").write_text("shared new file\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "later"], check=True)
        self.indexed = {"shared": [Hit("src/a.py", 1, "shared indexed later"), Hit("src/new.py", 1, "shared new file"),
                                   Hit("src/quiet.py", 1, "shared only in the index")]}
        environment = mock.patch.dict(entire.os.environ, {"ENTIRE_TOKEN": SECRET, "PRISM_ENTIRE_TRANSPORT": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def ctx(self, source):
        return Context(Bundle({}, [], [], {}), {"src/a.py", "src/quiet.py"}, set(), None, None, "gh/o/r",
                       search_commit=self.pinned, search_checkout=self.root, search_source=source)

    def test_entire_names_the_files_and_the_pinned_commit_supplies_the_text(self):
        with mock.patch.object(entire, "search_many", return_value=self.indexed) as search:
            grouped, discovery = pipeline._grouped(["shared"], self.ctx("entire"), PLAN)
        search.assert_called_once_with(["shared"], "gh/o/r")
        shown = json.dumps(grouped)
        self.assertEqual(grouped["files"][0], {"path": "src/a.py", "hit_count": 1, "terms": ["shared"],
                                               "lines": ["1: shared pinned text"]})
        # A candidate whose match exists only in the newer index stays a path with no text.
        self.assertEqual(grouped["files"][1], {"path": "src/quiet.py", "hit_count": 0, "terms": [], "lines": []})
        for newer in ("indexed", "src/new.py", "new file"):
            self.assertNotIn(newer, shown)
        self.assertEqual(discovery, {
            "source": "entire", "command": "entire search --code", "pinned_commit": self.pinned,
            "line_text_from": "pinned commit", "terms": ["shared"], "hits_per_term": {"shared": 3},
            "candidates_kept": ["src/a.py", "src/quiet.py"], "candidates_dropped": ["src/new.py"],
            "entire_index_freshness": "unavailable",
        })
        self.assertNotIn(SECRET, json.dumps(discovery))

    def test_git_source_never_calls_entire(self):
        with mock.patch.object(entire, "search_many", side_effect=AssertionError("Entire was searched")):
            grouped, discovery = pipeline._grouped(["shared"], self.ctx("git"), PLAN)
        self.assertEqual(grouped["files"][0]["lines"], ["1: shared pinned text"])
        self.assertEqual((discovery["source"], discovery["command"], discovery["candidates_kept"]),
                         ("git", "git grep at the pinned commit", ["src/a.py"]))
        self.assertNotIn("entire_index_freshness", discovery)

    def test_entire_failure_is_actionable_and_does_not_fall_back_to_git(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr=f"401 unauthorized for bearer {SECRET}")
        for run, reason in ((mock.Mock(return_value=failed), "401 unauthorized"),
                            (mock.Mock(side_effect=FileNotFoundError), "not installed"),
                            (mock.Mock(side_effect=subprocess.TimeoutExpired("entire", 45)), "timed out"),
                            (mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="not json", stderr="")),
                             "unexpected response")):
            with mock.patch.object(entire.subprocess, "run", run), \
                 mock.patch.object(entire, "search_many_at_commit") as git_search, \
                 self.assertRaisesRegex(RuntimeError, f"Entire search failed for gh/o/r: .*{reason}.*ENTIRE_TOKEN.*"
                                                      "entire repo view /gh/o/r.*not used as a fallback") as raised:
                pipeline._grouped(["shared"], self.ctx("entire"), PLAN)
            git_search.assert_not_called()
            self.assertNotIn(SECRET, str(raised.exception))

    def test_prediction_output_carries_the_discovery_record(self):
        usage = Usage(input=1, output=1, cached_input=0, cache_write=0, reasoning=None)
        answers = iter([{"problem": "", "suspected_cause": "", "search_terms": ["shared"], "expected_file_kinds": [],
                         "brief": ""},
                        {"fix": ["src/a.py"], "tests": [], "changelog": [], "docs": [], "config": [], "need_more": None}])
        prompts = []

        async def call(provider, stage, prompt, schema, cap):
            prompts.append(prompt)
            return next(answers), usage

        with mock.patch.object(models, "call", side_effect=call), \
             mock.patch.object(entire, "search_many", return_value=self.indexed):
            prediction = asyncio.run(pipeline.predict(self.ctx("entire"), "claude"))
        self.assertEqual((prediction.discovery["source"], prediction.discovery["pinned_commit"]), ("entire", self.pinned))
        self.assertIn("shared pinned text", prompts[1])
        self.assertNotIn("indexed", prompts[1])

    def test_cli_checks_entire_only_when_it_is_the_search_source(self):
        pin = ["--commit", "a" * 40, "--checkout", "/tmp/source"]
        for extra, checked in ((["--search", "entire", *pin], True), (["--search", "git", *pin], False), (pin, False)):
            with mock.patch.object(cli, "_load_env"), mock.patch.object(entire, "ensure_ready") as ready, \
                 mock.patch("prism.github_context.build_context", side_effect=ValueError("context reached")), \
                 self.assertRaisesRegex(ValueError, "context reached"):
                cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r", *extra])
            self.assertEqual(ready.called, checked)
        with mock.patch.object(cli, "_load_env"), self.assertRaisesRegex(SystemExit, "--search git requires --checkout"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r", "--search", "git"])


if __name__ == "__main__":
    unittest.main()
