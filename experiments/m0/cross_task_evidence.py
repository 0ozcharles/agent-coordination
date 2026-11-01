"""Read-only peer snapshots restricted to the predeclared task source roots.

The original publisher still owns archive validation, secret detection, atomic
publication, history/task serialization and cleanup. Only the worktree scope
changes; original repository assets outside the source roots are not copied.
"""
from __future__ import annotations

import io
import json
from pathlib import PurePosixPath
import tarfile

from baseline_evidence import (
    StrongSharePublisher, _json_bytes, _regular_file, _reject_secrets,
)


def source_roots(task):
    roots = tuple(task.code_roots)
    if not roots or len(set(roots)) != len(roots):
        raise ValueError("Unique explicit source roots are required")
    for root in roots:
        if not isinstance(root, str):
            raise ValueError("Source evidence root must be text")
        path = PurePosixPath(root)
        if (not root or path.is_absolute() or path.as_posix() != root
                or any(part in (".", "..", ".git") for part in path.parts)):
            raise ValueError("Source evidence root must be a safe relative directory")
        if any(root != other and path.is_relative_to(PurePosixPath(other)) for other in roots):
            raise ValueError("Overlapping source evidence roots are unsupported")
    return roots


def validate_source_directories(source, workdir, roots):
    """Fail on absent roots or any symlinked root/ancestor, without reading data."""
    script = """
import json, stat, sys
from pathlib import Path
base = Path(sys.argv[1])
for relative in json.loads(sys.argv[2]):
    current = base / relative
    for path in (current, *current.parents):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SystemExit(3)
"""
    result = source.exec_run(["python", "-c", script, workdir, json.dumps(list(roots))],
                             stdout=True, stderr=True)
    code = result.exit_code if hasattr(result, "exit_code") else result[0]
    if code != 0:
        raise ValueError("A declared source root is missing, non-directory, or symlinked")


class SourceScopedPublisher(StrongSharePublisher):
    def __init__(self, *args, task, **kwargs):
        self.source_roots = source_roots(task)
        super().__init__(*args, **kwargs)

    def _pack(self, source, workdir, messages, task, metadata):
        validate_source_directories(source, workdir, self.source_roots)
        metadata["source_roots"] = list(self.source_roots)
        metadata["omitted_worktree_outside_source_roots"] = True
        metadata["worktree_scope"] = "declared_task_source_roots_v1"
        metadata["files"], metadata["excluded_paths"] = [], []
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w") as target:
            directories = set()

            def directory(name):
                if name not in directories:
                    entry = tarfile.TarInfo(name)
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                    target.addfile(entry)
                    directories.add(name)

            directory("worktree")
            for root in self.source_roots:
                destination = PurePosixPath("worktree") / root
                for parent in reversed(destination.parents):
                    if str(parent) != ".":
                        directory(str(parent))
                files, excluded = self._append_tree(
                    target, source, str(PurePosixPath(workdir) / root), str(destination))
                metadata["files"].extend({**row, "path": str(PurePosixPath(root) / row["path"])}
                                         for row in files)
                metadata["excluded_paths"].extend(str(PurePosixPath(root) / path) for path in excluded)
            metadata["temporary_files"], metadata["temporary_excluded_paths"] = self._append_tree(
                target, source, "/tmp", "temporary", skip_unsupported=True)
            metadata["temporary_source"] = "/tmp"
            scope = (
                "Peer worktree snapshot scope: only the predeclared task source directories below are published.\n"
                + "\n".join(self.source_roots)
                + "\nPaths remain relative to the original repository under worktree/. Other repository files "
                  "are intentionally omitted. This same source scope applies to StrongShare and Consolidate. "
                  "StrongShare snapshots update after completed steps; the retired Consolidate peer stays frozen. "
                  "Original task text and model/tool history are complete. The temporary/ directory uses "
                  "the original safe /tmp snapshot rules. Secret checks and unsafe-path rejection remain enabled.\n"
            ).encode("utf-8")
            history = _json_bytes(messages)
            task_json = _json_bytes(task)
            task_text = task.encode("utf-8") if isinstance(task, str) else task_json
            for name, data in (("messages.json", history), ("history.json", history),
                               ("task.json", task_json), ("task.md", task_text),
                               ("scope.txt", scope), ("metadata.json", _json_bytes(metadata))):
                _reject_secrets(data, name)
                _regular_file(target, name, data)
        return packed.getvalue()
