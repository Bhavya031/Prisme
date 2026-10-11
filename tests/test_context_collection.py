import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from prism import github_context
from prism.types import Bundle, Context


BEFORE = "a" * 40
CUTOFF = "2020-01-02T00:00:00Z"
OLD = "2020-01-01T00:00:00Z"
MARKER = "\n[Reference truncated; original is longer]"


def comments_path(number, page=1, repo="o/r"):
    return f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}"


def benchmark_values(issue, comments=()):
    return {
        "repos/o/r/issues/1": {"state": "closed", "created_at": OLD, **issue},
        "repos/o/r/pulls/42": {"merged_at": "x", "created_at": CUTOFF, "merge_commit_sha": "merge"},
        "repos/o/r/commits/merge": {"parents": [{"sha": BEFORE}]},
        "repos/fork/repo": {"default_branch": "main"},
        "repos/fork/repo/commits/main": {"sha": BEFORE},
        f"repos/o/r/git/trees/{BEFORE}?recursive=1": {"tree": [{"path": "src/a.py", "type": "blob"}]},
        comments_path(1): [{"created_at": OLD, "body": body} for body in comments],
    }


class FakeGitHub:
    """Stands in for api_v2: records every request and how many ran at once."""

    def __init__(self, values, delays=None):
        self.values = values
        self.delays = delays or {}
        self.calls = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, path):
        with self._lock:
            self.calls.append(path)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delays.get(path, 0.01))
            value = self.values[path]
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            with self._lock:
                self.active -= 1


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "failed", {}, io.BytesIO())


def build(fake, **overrides):
    arguments = {"entire_repo": "gh/fork/repo", "pr_url": "https://github.com/o/r/pull/42", "pre_merge_sha": BEFORE}
    arguments.update(overrides)
    with mock.patch.object(github_context, "api_v2", side_effect=fake):
        return github_context.build_context("https://github.com/o/r/issues/1", **arguments)


class ConcurrentCollectionTests(unittest.TestCase):
    def references(self, count=6):
        values = benchmark_values({"title": "bug", "body": " ".join(f"#{number}" for number in range(2, 2 + count))})
        for number in range(2, 2 + count):
            values[f"repos/o/r/issues/{number}"] = {"created_at": OLD, "title": f"t{number}", "body": f"b{number}"}
            values[comments_path(number)] = [{"created_at": OLD, "body": f"c{number}"}]
        return values

    def test_references_keep_their_order_when_later_ones_finish_first(self):
        values = self.references()
        fake = FakeGitHub(values, delays={"repos/o/r/issues/2": 0.2, comments_path(3): 0.15})
        ctx = build(fake)
        self.assertEqual([reference["reference"] for reference in ctx.bundle.references],
                         [f"o/r#{number}" for number in range(2, 8)])
        self.assertEqual(ctx.bundle.references[0], {
            "reference": "o/r#2", "type": "issue", "title": "t2", "body": "b2", "comments": ["c2"]})
        self.assertEqual(ctx.safe_refs, {("o", "r", number) for number in range(2, 8)})

    def test_each_url_is_fetched_once_and_only_needed_urls_are_fetched(self):
        values = self.references()
        # The issue also mentions itself, which used to fetch it and its comments a second time.
        values["repos/o/r/issues/1"]["body"] += " #1"
        fake = FakeGitHub(values)
        ctx = build(fake)
        self.assertEqual(sorted(fake.calls), sorted(values))
        self.assertIn("o/r#1", [reference["reference"] for reference in ctx.bundle.references])

    def test_requests_overlap_but_stay_within_the_worker_bound(self):
        values = self.references(count=30)
        fake = FakeGitHub(values, delays={path: 0.03 for path in values})
        build(fake)
        self.assertGreater(fake.peak, 1)
        self.assertLessEqual(fake.peak, github_context.WORKERS + 1)

    def test_collection_is_faster_than_one_request_at_a_time(self):
        values = self.references(count=12)
        fake = FakeGitHub(values, delays={path: 0.05 for path in values})
        started = time.monotonic()
        build(fake)
        self.assertLess(time.monotonic() - started, 0.05 * len(values) / 2)

    def test_a_failed_request_raises_the_same_error(self):
        values = self.references()
        failure = http_error(500)
        values[comments_path(4)] = failure
        with self.assertRaises(urllib.error.HTTPError) as raised:
            build(FakeGitHub(values))
        self.assertIs(raised.exception, failure)

    def test_the_first_failure_in_reference_order_wins(self):
        values = self.references()
        first, second = http_error(502), http_error(503)
        values["repos/o/r/issues/3"] = first
        values["repos/o/r/issues/6"] = second
        # The later reference fails long before the earlier one does.
        fake = FakeGitHub(values, delays={"repos/o/r/issues/3": 0.2})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            build(fake)
        self.assertIs(raised.exception, first)

    def test_an_earlier_step_failure_wins_over_a_later_one(self):
        values = self.references()
        values["repos/fork/repo/commits/main"] = {"sha": "b" * 40}
        values[comments_path(1)] = http_error(500)
        with self.assertRaisesRegex(ValueError, "default branch does not match"):
            build(FakeGitHub(values), pre_merge_sha=None)

    def test_missing_references_are_still_skipped(self):
        values = self.references()
        values["repos/o/r/issues/4"] = http_error(404)
        del values[comments_path(4)]
        fake = FakeGitHub(values)
        ctx = build(fake)
        self.assertEqual([reference["reference"] for reference in ctx.bundle.references],
                         ["o/r#2", "o/r#3", "o/r#5", "o/r#6", "o/r#7"])
        self.assertEqual(sorted(fake.calls), sorted(values))

    def test_cutoff_and_target_filtering_are_unchanged(self):
        values = benchmark_values({"title": "bug", "body": "#42\n#7 #8 #9"}, comments=["see #10", "fixed by #42\nkeep"])
        values[comments_path(1)].append({"created_at": CUTOFF, "body": "late #11"})
        values["repos/o/r/issues/7"] = {"created_at": OLD, "title": "old", "body": "", "pull_request": {}, "comments": 3}
        values["repos/o/r/pulls/7"] = {"title": "old PR", "body": "body\nrefs #42", "changed_files": 2}
        values["repos/o/r/issues/8"] = {"created_at": CUTOFF, "title": "future", "body": ""}
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": "b"}
        values[comments_path(9)] = [{"created_at": OLD, "body": "early"}, {"created_at": CUTOFF, "body": "late"}]
        values["repos/o/r/issues/10"] = {"created_at": OLD, "title": "ten", "body": ""}
        values[comments_path(10)] = []
        fake = FakeGitHub(values)
        ctx = build(fake)
        self.assertEqual(sorted(fake.calls), sorted(values))
        self.assertEqual(ctx.bundle.issue, {"title": "bug", "body": "#7 #8 #9", "comments": ["see #10", "keep"]})
        self.assertEqual(ctx.bundle.references, [
            {"reference": "o/r#7", "type": "PR", "title": "old PR", "body": "body",
             "pointer": "PR #7: 3 comments, 2 files changed (not included)"},
            {"reference": "o/r#9", "type": "issue", "title": "nine", "body": "b", "comments": ["early"]},
            {"reference": "o/r#10", "type": "issue", "title": "ten", "body": "", "comments": []},
        ])
        self.assertEqual(ctx.safe_refs, {("o", "r", 7), ("o", "r", 9), ("o", "r", 10)})
        self.assertEqual((ctx.cutoff, ctx.target_pr), (CUTOFF, 42))

    def test_a_leak_still_raises(self):
        values = benchmark_values({"title": "bug", "body": ""})
        with mock.patch.object(github_context, "clean_v2", side_effect=lambda value, target: value or ""):
            values["repos/o/r/issues/1"]["body"] = "fixed by #42"
            with self.assertRaisesRegex(ValueError, "leaked"):
                build(FakeGitHub(values))

    def test_a_bad_pair_fails_after_the_issue_fetch_and_fetches_nothing_else(self):
        fake = FakeGitHub(benchmark_values({"title": "bug", "body": ""}))
        with self.assertRaisesRegex(ValueError, "Bad issue/PR pair"):
            build(fake, pr_url="https://github.com/other/r/pull/42")
        self.assertEqual(fake.calls, ["repos/o/r/issues/1"])

    def test_predict_mode_uses_the_default_branch_without_a_cutoff(self):
        values = {
            "repos/o/r/issues/1": {"state": "open", "title": "bug", "body": "#2 in src/a.py"},
            "repos/o/r": {"default_branch": "main"},
            "repos/o/r/commits/main": {"sha": BEFORE},
            f"repos/o/r/git/trees/{BEFORE}?recursive=1": {"tree": [{"path": "src/a.py", "type": "blob"}]},
            comments_path(1): [{"created_at": "2030-01-01T00:00:00Z", "body": "new"}],
            "repos/o/r/issues/2": {"created_at": "2030-01-01T00:00:00Z", "title": "two", "body": "b"},
            comments_path(2): [],
        }
        fake = FakeGitHub(values)
        ctx = build(fake, entire_repo="gh/o/r", pr_url=None, pre_merge_sha=None)
        self.assertEqual(sorted(fake.calls), sorted(values))
        self.assertEqual(ctx.bundle.issue["comments"], ["new"])
        self.assertEqual([reference["reference"] for reference in ctx.bundle.references], ["o/r#2"])
        self.assertEqual(ctx.bundle.path_hints, ["src/a.py"])
        self.assertEqual((ctx.cutoff, ctx.target_pr), (None, None))

    def test_concurrent_cache_writes_leave_one_complete_file(self):
        path = f"repos/o/r/git/trees/{BEFORE}?recursive=1"
        payload = {"tree": [{"path": f"src/{number}.py", "type": "blob"} for number in range(2000)]}

        def urlopen(request, timeout):
            return io.StringIO(json.dumps(payload))

        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(github_context, "CACHE", Path(cache)), \
             mock.patch.object(github_context, "_gh_token_checked", True), \
             mock.patch.object(github_context.urllib.request, "urlopen", side_effect=urlopen):
            results = []
            threads = [threading.Thread(target=lambda: results.append(github_context.api_v2(path))) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            files = list(Path(cache).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text()), payload)
        self.assertEqual(results, [payload] * 16)


class ReferenceBoundTests(unittest.TestCase):
    def test_reference_bodies_and_comments_are_bounded(self):
        values = benchmark_values({"title": "bug", "body": "#7 #9"})
        values["repos/o/r/issues/7"] = {"created_at": OLD, "title": "pr", "body": "", "pull_request": {}}
        values["repos/o/r/pulls/7"] = {"title": "pr", "body": "p" * 2001}
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": "i" * 5000}
        values[comments_path(9)] = [{"created_at": OLD, "body": str(number) * 501} for number in range(1, 7)]
        pr, issue = build(FakeGitHub(values)).bundle.references
        self.assertEqual(pr["body"], "p" * 2000 + MARKER)
        self.assertEqual(issue["body"], "i" * 2000 + MARKER)
        self.assertEqual(issue["comments"], [str(number) * 500 + MARKER for number in range(1, 5)])
        self.assertEqual(issue["pointer"], "issue #9: first 4 of 6 comments included")

    def test_references_within_the_bounds_are_unchanged(self):
        values = benchmark_values({"title": "bug", "body": "#9"})
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": "i" * 2000}
        values[comments_path(9)] = [{"created_at": OLD, "body": "c" * 500} for _ in range(4)]
        self.assertEqual(build(FakeGitHub(values)).bundle.references, [
            {"reference": "o/r#9", "type": "issue", "title": "nine", "body": "i" * 2000, "comments": ["c" * 500] * 4}])

    def test_the_issue_itself_is_never_truncated(self):
        values = benchmark_values({"title": "bug", "body": "b" * 9000}, comments=["c" * 3000] * 7)
        issue = build(FakeGitHub(values)).bundle.issue
        self.assertEqual(issue, {"title": "bug", "body": "b" * 9000, "comments": ["c" * 3000] * 7})

    def test_target_lines_are_removed_before_truncating(self):
        text = "a" * 1500 + "\nfixed by #42\n" + "b" * 1500 + "\nalso PR 42\n" + "c" * 10
        values = benchmark_values({"title": "bug", "body": "#9"})
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": text}
        values[comments_path(9)] = [{"created_at": OLD, "body": "x" * 495 + "\n#42\n" + "y" * 100}]
        reference = build(FakeGitHub(values)).bundle.references[0]
        self.assertEqual(reference["body"], "a" * 1500 + "\n" + "b" * 499 + MARKER)
        self.assertEqual(reference["comments"], ["x" * 495 + "\n" + "y" * 4 + MARKER])

    def test_truncating_never_shortens_a_number_into_the_target(self):
        text = "a" * 1996 + " #425 " + "b" * 100
        self.assertEqual(github_context._reference_excerpt(text, 42), "a" * 1996 + " " + MARKER)
        values = benchmark_values({"title": "bug", "body": "#9"})
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": text}
        values[comments_path(9)] = []
        values["repos/o/r/issues/425"] = http_error(404)
        self.assertEqual(build(FakeGitHub(values)).bundle.references[0]["body"], "a" * 1996 + " " + MARKER)

    def test_bounds_can_be_switched_off(self):
        values = benchmark_values({"title": "bug", "body": "#9"})
        values["repos/o/r/issues/9"] = {"created_at": OLD, "title": "nine", "body": "i" * 5000}
        values[comments_path(9)] = [{"created_at": OLD, "body": "c" * 900} for _ in range(6)]
        with mock.patch.multiple(github_context, REFERENCE_BODY_LIMIT=None, REFERENCE_COMMENT_LIMIT=None,
                                 REFERENCE_COMMENTS=None):
            reference = build(FakeGitHub(values)).bundle.references[0]
        self.assertEqual(reference, {"reference": "o/r#9", "type": "issue", "title": "nine", "body": "i" * 5000,
                                     "comments": ["c" * 900] * 6})

    def test_fetch_reference_returns_every_comment_in_full(self):
        ctx = Context(Bundle({}, [], [], {}), set(), {("o", "r", 9), ("o", "r", 10)}, CUTOFF, 42, "gh/o/r")
        values = {
            "repos/o/r/issues/9": {"created_at": OLD, "body": "i" * 5000 + "\nfixed by #42"},
            comments_path(9): [{"created_at": OLD, "body": str(number) * 900} for number in range(1, 7)]
            + [{"created_at": CUTOFF, "body": "late"}],
            "repos/o/r/issues/10": {"created_at": OLD, "body": "short"},
            comments_path(10): [],
        }
        with mock.patch.object(github_context, "api_v2", side_effect=values.__getitem__):
            self.assertEqual(github_context.fetch_reference("#9 comments", ctx), {
                "reference": "o/r#9", "comments": [str(number) * 900 for number in range(1, 7)]})
            self.assertEqual(github_context.fetch_reference("#10 comments", ctx), {"reference": "o/r#10", "comments": []})
