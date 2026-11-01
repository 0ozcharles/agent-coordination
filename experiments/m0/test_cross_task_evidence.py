"""Source-scoped archive tests with temporary files; no Docker or API."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import baseline_evidence
import cross_task_evidence as evidence
import cross_task_workers as workers


class LocalSource:
    def __init__(self, temporary):
        self.temporary = temporary
        self.archived = []

    def exec_run(self, command, **kwargs):
        result = subprocess.run([sys.executable, *command[1:]], capture_output=True)
        return SimpleNamespace(exit_code=result.returncode, output=result.stdout + result.stderr)

    def get_archive(self, directory):
        self.archived.append(directory)
        path = self.temporary if directory == "/tmp" else Path(directory)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(path, arcname=Path(directory).name)
        return iter([stream.getvalue()]), {}


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo, self.scratch = self.root / "repo", self.root / "scratch"
        self.repo.mkdir()
        self.scratch.mkdir()
        self.source = LocalSource(self.scratch)
        self.task = SimpleNamespace(code_roots=("src/pkg", "plugins"))
        for name, text in {"src/pkg/code.py": "VALUE = 1\n", "plugins/helper.py": "VALUE = 2\n",
                           "docs/static.js": 'api_key = "' + "S" * 30 + '"\n'}.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.scratch / "draft.py").write_text("WORK = True\n")
        (self.scratch / "pip-cache-test").mkdir()
        (self.scratch / "pip-cache-test" / "ignored.py").write_text("CACHE = True\n")

    def publisher(self, task=None):
        publisher = object.__new__(evidence.SourceScopedPublisher)
        publisher.source_roots = evidence.source_roots(task or self.task)
        return publisher

    def test_archive_preserves_relative_roots_history_task_and_tmp(self):
        metadata = {"agent_id": "agent1"}
        messages = [{"role": "user", "content": "original complete history"}]
        data = self.publisher()._pack(self.source, str(self.repo), messages, "original task", metadata)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            names = archive.getnames()
            self.assertIn("worktree/src/pkg/code.py", names)
            self.assertIn("worktree/plugins/helper.py", names)
            self.assertIn("temporary/draft.py", names)
            self.assertFalse(any("docs/" in name or "pip-cache-test" in name for name in names))
            self.assertEqual(json.load(archive.extractfile("history.json")), messages)
            self.assertEqual(archive.extractfile("task.md").read(), b"original task")
            self.assertIn(b"same source scope", archive.extractfile("scope.txt").read())
            saved = json.load(archive.extractfile("metadata.json"))
        self.assertEqual(saved["source_roots"], ["src/pkg", "plugins"])
        self.assertTrue(saved["omitted_worktree_outside_source_roots"])
        self.assertEqual({r["path"] for r in saved["files"]}, {"src/pkg/code.py", "plugins/helper.py"})
        self.assertEqual(self.source.archived, [str(self.repo / "src/pkg"), str(self.repo / "plugins"), "/tmp"])

    def test_source_secret_detection_remains_enabled(self):
        (self.repo / "src/pkg/code.py").write_text('api_key = "' + "T" * 30 + '"\n')
        with self.assertRaisesRegex(ValueError, "Possible credential detected"):
            self.publisher()._pack(self.source, str(self.repo), [], "task", {})

    def test_missing_or_symlinked_source_fails_before_archive(self):
        for roots in (("missing",), ("link/pkg",)):
            if roots[0].startswith("link"):
                (self.repo / "link").symlink_to(self.repo / "src", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "missing, non-directory, or symlinked"):
                self.publisher(SimpleNamespace(code_roots=roots))._pack(self.source, str(self.repo), [], "task", {})
            self.assertEqual(self.source.archived, [])

    def test_root_overlap_and_unsafe_paths_rejected(self):
        for roots in ((), ("src/pkg", "src/pkg"), ("src", "src/pkg"), ("../src",), ("/src",), (".git",)):
            with self.assertRaises(ValueError):
                evidence.source_roots(SimpleNamespace(code_roots=roots))

    def test_publication_and_cleanup_keep_original_atomic_implementation(self):
        self.assertIs(evidence.SourceScopedPublisher.publish, baseline_evidence.StrongSharePublisher.publish)
        self.assertIs(evidence.SourceScopedPublisher.cleanup, baseline_evidence.StrongSharePublisher.cleanup)

    def test_context_binds_task_and_restores_original_evidence_module(self):
        original = sys.modules["baseline_evidence"]
        with mock.patch.object(evidence.SourceScopedPublisher, "__init__", return_value=None) as constructor:
            with workers.packet_module_context(self.task):
                from baseline_evidence import StrongSharePublisher
                self.assertIsNot(sys.modules["baseline_evidence"], original)
                StrongSharePublisher("client", "image", "volume", "label")
            self.assertIs(sys.modules["baseline_evidence"], original)
            constructor.assert_called_once_with("client", "image", "volume", "label", task=self.task)


if __name__ == "__main__":
    unittest.main()
