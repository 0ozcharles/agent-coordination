import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_rsi.cli import execution_command, main, project_path, read_results


class ProjectCliTests(unittest.TestCase):
    def test_result_snapshot_supports_a_checkout_without_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"actual_new_usage": {"unknown_usage_requests": 2}}))
            data, source = read_results(root, {"suite": "missing", "snapshot": "result.json"})
            self.assertEqual(data["actual_new_usage"]["unknown_usage_requests"], 2)
            self.assertEqual(source, "committed result snapshot")
            (root / "missing").mkdir()
            (root / "missing/summary.json").write_text('{"newer": true}')
            self.assertEqual(read_results(root, {"suite": "missing", "snapshot": "result.json"})[0], {"newer": True})

    def test_run_preview_never_launches_a_process(self):
        with patch("agent_rsi.cli.subprocess.call") as launch, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["run", "early-roles"]), 0)
            launch.assert_not_called()
            self.assertIn("Preview only", output.getvalue())

    def test_commands_use_argument_lists_and_reject_external_paths(self):
        root = Path("/tmp/project with spaces")
        study = {"entrypoint": "runner.py", "suite": "saved suite"}
        command = execution_command(root, study, "resume", "/tmp/python with spaces")
        self.assertEqual(command[-1], str((root / "saved suite").resolve()))
        self.assertEqual(command[0], "/tmp/python with spaces")
        self.assertIn("--offline-selftest", execution_command(root, study, "offline"))
        with self.assertRaises(ValueError):
            project_path(root, "../outside")


if __name__ == "__main__":
    unittest.main()
