import unittest
from types import SimpleNamespace
from unittest import mock

from prism import cli, entire


class EntireTests(unittest.TestCase):
    def test_ready_repository_passes(self):
        result = SimpleNamespace(returncode=0, stdout='{"clusters":[{"status":"ready"}]}', stderr="")
        with mock.patch.object(entire.subprocess, "run", return_value=result) as run:
            entire.ensure_ready("gh/o/r")
        self.assertEqual(run.call_args.args[0][:4], ["entire", "repo", "view", "/gh/o/r"])

    def test_unready_repository_fails(self):
        result = SimpleNamespace(returncode=0, stdout='{"clusters":[{"status":"seeding"}]}', stderr="")
        with mock.patch.object(entire.subprocess, "run", return_value=result), \
             self.assertRaisesRegex(RuntimeError, "not ready.*seeding"):
            entire.ensure_ready("gh/o/r")

    def test_cli_stops_before_collecting_context_when_entire_is_unavailable(self):
        with mock.patch.object(entire, "ensure_ready", side_effect=RuntimeError("Entire login required")), \
             mock.patch("prism.github_context.build_context") as build_context, \
             self.assertRaisesRegex(SystemExit, "Entire login required"):
            cli.main(["predict", "https://github.com/o/r/issues/1", "--repo", "gh/o/r"])
        build_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
