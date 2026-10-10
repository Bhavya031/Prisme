import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prism import github_context, pipeline
from prism.types import Bundle, Context


class ContextTests(unittest.TestCase):
    def test_target_mentions_are_removed_without_matching_longer_numbers(self):
        target = 6589
        for value in ("#6589", "PR #6589", "pull request 6589", "/pull/6589"):
            with self.subTest(value=value):
                self.assertTrue(github_context._target_mention(target).search(value))
                self.assertEqual(github_context.clean_v2(f"keep\n{value}\n#65890", target), "keep\n#65890")
                with self.assertRaises(ValueError):
                    pipeline._safe_prompt(value, target)
        self.assertFalse(github_context._target_mention(target).search("#65890"))

    def test_comments_apply_the_cutoff(self):
        comments = [
            {"created_at": "2020-01-01T00:00:00Z", "body": "before"},
            {"created_at": "2020-01-02T00:00:00Z", "body": "at cutoff"},
        ]
        with mock.patch.object(github_context, "api_v2", return_value=comments):
            result = github_context.comments_v2("repos/o/r", 1, "2020-01-02T00:00:00Z")
        self.assertEqual([comment["body"] for comment in result], ["before"])

    def test_build_context_excludes_the_target_reference(self):
        before = "a" * 40

        def api(path):
            values = {
                "repos/o/r/issues/1": {"state": "closed", "title": "See #42\nSee #7\nSee #8", "body": ""},
                "repos/o/r/pulls/42": {"merged_at": "x", "created_at": "2020-01-02T00:00:00Z", "merge_commit_sha": "merge"},
                "repos/o/r/commits/merge": {"parents": [{"sha": before}]},
                "repos/fork/repo/commits/main": {"sha": before},
                f"repos/o/r/git/trees/{before}?recursive=1": {"tree": [{"path": "src/a.py", "type": "blob"}]},
                "repos/o/r/issues/1/comments?per_page=100&page=1": [],
                "repos/o/r/issues/7": {"created_at": "2020-01-01T00:00:00Z", "title": "old", "body": ""},
                "repos/o/r/issues/7/comments?per_page=100&page=1": [],
                "repos/o/r/issues/8": {"created_at": "2020-01-02T00:00:00Z", "title": "future", "body": ""},
            }
            return values[path]

        with mock.patch.object(github_context, "api_v2", side_effect=api):
            ctx = github_context.build_context(
                "https://github.com/o/r/issues/1", entire_repo="gh/fork/repo",
                pr_url="https://github.com/o/r/pull/42", pre_merge_sha=before,
            )
        self.assertEqual(ctx.safe_refs, {("o", "r", 7)})
        self.assertEqual([reference["reference"] for reference in ctx.bundle.references], ["o/r#7"])

    def test_fetch_reference_rejects_unsafe_refs_and_honors_cutoff(self):
        ctx = Context(Bundle({}, [], [], {}), set(), {("o", "r", 7)}, "2020-01-02T00:00:00Z", 42, "gh/o/r")

        def api(path):
            values = {
                "repos/o/r/issues/7": {"created_at": "2020-01-01T00:00:00Z", "pull_request": {}},
                "repos/o/r/pulls/7": {"created_at": "2020-01-01T00:00:00Z"},
                "repos/o/r/pulls/7/files?per_page=100&page=1": [{"filename": "src/a.py"}],
            }
            return values[path]

        with mock.patch.object(github_context, "api_v2", side_effect=api):
            self.assertIsNone(github_context.fetch_reference("#42 files", ctx))
            self.assertIsNone(github_context.fetch_reference("#99 files", ctx))
            self.assertEqual(github_context.fetch_reference("#7 files", ctx), {"reference": "o/r#7", "files": ["src/a.py"]})

    def test_only_sha_addressed_responses_are_cached(self):
        calls = []

        def urlopen(request, timeout):
            calls.append(request.full_url)
            return io.StringIO(json.dumps({"ok": True}))

        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(github_context, "CACHE", Path(cache)), \
             mock.patch.object(github_context, "_gh_token_checked", True), \
             mock.patch.object(github_context.urllib.request, "urlopen", side_effect=urlopen):
            github_context.api_v2("repos/o/r/issues/1")
            github_context.api_v2("repos/o/r/issues/1")
            tree = "a" * 40
            github_context.api_v2(f"repos/o/r/git/trees/{tree}?recursive=1")
            github_context.api_v2(f"repos/o/r/git/trees/{tree}?recursive=1")
        self.assertEqual(len(calls), 3)

    def test_github_cli_token_is_reused(self):
        with mock.patch.dict(github_context.os.environ, {}, clear=True), \
             mock.patch.object(github_context, "_gh_token_checked", False), \
             mock.patch.object(github_context, "_gh_token", None), \
             mock.patch.object(github_context.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="token\n")) as run:
            self.assertEqual(github_context._github_token(), "token")
            self.assertEqual(github_context._github_token(), "token")
        run.assert_called_once()
