import io
import unittest
from types import SimpleNamespace
from unittest import mock

from prism import cli, entire


class EntireTests(unittest.TestCase):
    def test_ready_repository_passes(self):
        result = SimpleNamespace(returncode=0, stdout='{"status":"ready"}', stderr="")
        with mock.patch.object(entire.subprocess, "run", return_value=result) as run:
            entire.ensure_ready("gh/o/r")
        self.assertEqual(run.call_args.args[0][:4], ["entire", "repo", "view", "/gh/o/r"])

    def test_mixed_repository_fails_even_with_a_ready_placement(self):
        result = SimpleNamespace(
            returncode=0,
            stdout='{"status":"mixed","placements":[{"status":"ready"},{"status":"failed"}]}',
            stderr="",
        )
        with mock.patch.object(entire.subprocess, "run", return_value=result), \
             self.assertRaisesRegex(RuntimeError, "not ready.*mixed"):
            entire.ensure_ready("gh/o/r")

    def test_repository_check_timeout_is_clean(self):
        error = entire.subprocess.TimeoutExpired(["entire", "repo", "view"], 45)
        with mock.patch.object(entire.subprocess, "run", side_effect=error), \
             self.assertRaisesRegex(RuntimeError, "check timed out for gh/o/r") as raised:
            entire.ensure_ready("gh/o/r")
        self.assertIsNone(raised.exception.__cause__)

    def test_http_transport_uses_repository_data_without_cli(self):
        entire._repo_details.cache_clear()
        self.addCleanup(entire._repo_details.cache_clear)
        repos = {"repos": [{"name": "o/r", "id": "id", "cell": "cell", "status": "ready"}]}
        search = {"results": [{"path": "a.py", "line": 1, "context_line": "match"}]}
        with mock.patch.dict(entire.os.environ, {"PRISM_ENTIRE_TRANSPORT": "http"}), \
             mock.patch.object(entire, "_request", side_effect=[repos, search]), \
             mock.patch.object(entire.subprocess, "run") as run:
            entire.ensure_ready("gh/o/r")
            self.assertEqual(entire.search("match", "gh/o/r")[0].path, "a.py")
        run.assert_not_called()

    def test_http_auth_failure_is_clean(self):
        error = entire.urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO())
        self.addCleanup(error.close)
        with mock.patch.dict(entire.os.environ, {"ENTIRE_TOKEN": "token"}), \
             mock.patch.object(entire.urllib.request, "urlopen", side_effect=error), \
             self.assertRaisesRegex(RuntimeError, "authentication failed.*ENTIRE_TOKEN") as raised:
            entire._request("https://example.test")
        self.assertIsNone(raised.exception.__cause__)

    def test_malformed_http_json_is_clean(self):
        with mock.patch.dict(entire.os.environ, {"ENTIRE_TOKEN": "token"}), \
             mock.patch.object(entire.urllib.request, "urlopen", return_value=io.StringIO("not json")), \
             self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            entire._request("https://example.test")

    def test_cli_stops_before_collecting_context_when_entire_is_unavailable(self):
        with mock.patch.object(entire, "ensure_ready", side_effect=RuntimeError("Entire login required")), \
             mock.patch("prism.github_context.build_context") as build_context, \
             self.assertRaisesRegex(SystemExit, "Entire login required"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r"])
        build_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
