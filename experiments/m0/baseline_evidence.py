"""Publish immutable StrongShare evidence through a Docker-only writer.

The caller owns the named volume and mounts it read-only in both agents at
``/shared-evidence``. This helper never extracts an agent archive on the host.
Only a completed snapshot becomes visible through an atomic ``latest`` link.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import posixpath
from pathlib import PurePosixPath
import re
import tarfile
from threading import RLock
import uuid


_EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                  ".ruff_cache", ".cache", ".tox", ".nox", ".venv", "venv"}
_CREDENTIAL_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials",
                     "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_TEMP_CACHE_PREFIXES = ("pip-cache-", "pip-ephem-wheel-cache-", "pip-unpack-",
                        "pip-build-env-", "pip-build-tracker-", "uv-cache-")
_SECRET_PATTERNS = [
    re.compile(rb"\bsk-(?:proj-|org-)?[A-Za-z0-9_-]{24,}"),
    re.compile(rb"\btgo_v1_[A-Za-z0-9_-]{24,}"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key)"
               rb"[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_./+=-]{24,}[\"']"),
]
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class _ArchiveReader:
    """Adapt the Docker archive iterator to tarfile's streaming reader."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = bytearray()
        self.total = 0

    def read(self, size):
        while len(self.pending) < size:
            chunk = next(self.chunks, None)
            if chunk is None:
                break
            self.total += len(chunk)
            if self.total > _MAX_ARCHIVE_BYTES:
                raise ValueError("Worktree archive exceeds the 256 MiB publishing limit")
            self.pending.extend(chunk)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


def _reject_secrets(data, location):
    if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
        # Never put the matching value into logs or exception messages.
        raise ValueError(f"Possible credential detected; evidence not published: {location}")


def _json_bytes(value):
    def serialize(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        raise TypeError(f"Evidence is not JSON serializable: {type(item).__name__}")
    return (json.dumps(value, ensure_ascii=False, indent=2, default=serialize) + "\n").encode("utf-8")


def _regular_file(archive, name, data, mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = mode
    archive.addfile(entry, io.BytesIO(data))


class StrongSharePublisher:
    """One publisher per volume; concurrent agent1/agent2 publication is safe.

    ``publish`` returns metadata; ``version`` returns the most recently
    completed metadata or None. Returned dictionaries are independent copies.
    ``cleanup`` removes only this helper container, retaining the evidence volume.
    Do not supply credentials in task text, messages, or repository files.
    """

    def __init__(self, docker_client, image, volume, run_id):
        self.client = docker_client
        self.run_id = str(run_id)
        self._locks = {agent: RLock() for agent in ("agent1", "agent2")}
        self._versions = {agent: None for agent in self._locks}
        self._closed = False
        self.helper = self.client.containers.run(
            image,
            entrypoint=["python", "-u", "-c"],
            command=["import time; time.sleep(2147483647)"],
            detach=True,
            user="0:0",
            network_disabled=True,
            volumes={volume: {"bind": "/evidence", "mode": "rw"}},
            labels={"agent-rsi.role": "strongshare-evidence-writer", "agent-rsi.run": self.run_id,
                    "agent-rsi-run": self.run_id},
        )
        try:
            self._exec("import os; "
                       "os.makedirs('/evidence/agent1/snapshots', exist_ok=True); "
                       "os.makedirs('/evidence/agent2/snapshots', exist_ok=True)")
        except BaseException:
            self.helper.remove(force=True)
            self._closed = True
            raise

    def _exec(self, script, *args):
        result = self.helper.exec_run(["python", "-c", script, *args])
        if result.exit_code:
            detail = result.output.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"Evidence writer failed ({result.exit_code}): {detail}")

    def _agent_lock(self, agent_id):
        if agent_id not in self._locks:
            raise ValueError("agent_id must be agent1 or agent2")
        return self._locks[agent_id]

    def _append_tree(self, target, source, directory, destination, skip_unsupported=False):
        """Copy one validated container tree into the shared in-memory archive."""
        source_root = PurePosixPath(directory).name
        if not directory.startswith("/") or not source_root or ".." in PurePosixPath(directory).parts:
            raise ValueError("Evidence source must be an absolute non-root container directory")
        chunks, _ = source.get_archive(directory)
        files = []
        excluded = set()
        seen = set()
        try:
            with tarfile.open(fileobj=_ArchiveReader(chunks), mode="r|*") as original:
                root = tarfile.TarInfo(destination)
                root.type = tarfile.DIRTYPE
                root.mode = 0o755
                target.addfile(root)
                for entry in original:
                    path = PurePosixPath(entry.name)
                    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != source_root:
                        raise ValueError("Unsafe or unexpected archive member path")
                    parts = path.parts[1:]
                    if not parts:
                        continue
                    relative = PurePosixPath(*parts).as_posix()
                    if (any(part in _EXCLUDED_DIRS for part in parts)
                            or parts[-1] in _CREDENTIAL_NAMES
                            or parts[-1].startswith(".env.")
                            or (destination == "temporary"
                                and any(part.startswith(_TEMP_CACHE_PREFIXES) for part in parts))):
                        excluded.add(relative)
                        continue
                    if relative in seen:
                        raise ValueError(f"Duplicate {destination} archive member: {relative}")
                    seen.add(relative)
                    name = destination + "/" + relative
                    clean = tarfile.TarInfo(name)
                    clean.mode = 0o755 if entry.isdir() else 0o644 | (entry.mode & 0o111)
                    if entry.isdir():
                        clean.type = tarfile.DIRTYPE
                        target.addfile(clean)
                    elif entry.isfile():
                        if entry.size > _MAX_ARCHIVE_BYTES:
                            raise ValueError("Evidence file exceeds the publishing size limit")
                        data = original.extractfile(entry).read()
                        _reject_secrets(data, name)
                        _regular_file(target, name, data, clean.mode)
                        files.append({"path": relative, "type": "file", "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest()})
                    elif entry.issym():
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), entry.linkname))
                        if (posixpath.isabs(entry.linkname) or resolved == ".." or resolved.startswith("../")
                                or any(part in _EXCLUDED_DIRS for part in PurePosixPath(resolved).parts)):
                            if skip_unsupported:
                                excluded.add(relative)
                                continue
                            raise ValueError(f"Symlink escapes published {destination}: {relative}")
                        clean.type = tarfile.SYMTYPE
                        clean.linkname = entry.linkname
                        target.addfile(clean)
                        files.append({"path": relative, "type": "symlink", "target": entry.linkname})
                    elif entry.islnk():
                        # Hard links may target excluded entries or cross archive
                        # directories; fail rather than publish incomplete evidence.
                        if skip_unsupported:
                            excluded.add(relative)
                            continue
                        raise ValueError(f"Hard-linked {destination} entry is unsupported: {relative}")
                    else:
                        if skip_unsupported:
                            excluded.add(relative)
                            continue
                        raise ValueError(f"Non-file {destination} entry is unsupported: {relative}")
        finally:
            if hasattr(chunks, "close"):
                chunks.close()
        return files, sorted(excluded)

    def _pack(self, source, workdir, messages, task, metadata):
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w") as target:
            metadata["files"], metadata["excluded_paths"] = self._append_tree(
                target, source, workdir, "worktree")
            metadata["temporary_files"], metadata["temporary_excluded_paths"] = self._append_tree(
                target, source, "/tmp", "temporary", skip_unsupported=True)
            metadata["temporary_source"] = "/tmp"
            history = _json_bytes(messages)
            task_json = _json_bytes(task)
            task_text = task.encode("utf-8") if isinstance(task, str) else task_json
            for name, data in (("messages.json", history), ("history.json", history),
                               ("task.json", task_json), ("task.md", task_text),
                               ("metadata.json", _json_bytes(metadata))):
                _reject_secrets(data, name)
                _regular_file(target, name, data)
        return packed.getvalue()

    def publish(self, agent_id, container_id, workdir, messages, task, step):
        with self._agent_lock(agent_id):
            if self._closed:
                raise RuntimeError("Evidence publisher has been cleaned up")
            step_name = str(step)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", step_name):
                raise ValueError("step must be a safe single path component")
            workdir = str(workdir).rstrip("/")
            source = self.client.containers.get(container_id)
            prefix = f"/evidence/{agent_id}"
            final_path = f"{prefix}/snapshots/{step_name}"
            token = uuid.uuid4().hex
            staging = f"{prefix}/snapshots/.staging-{token}"
            metadata = {
                "schema_version": 1, "run_id": self.run_id, "agent_id": agent_id,
                "step": step, "container_id": source.id, "workdir": workdir,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "snapshot_path": f"/shared-evidence/{agent_id}/snapshots/{step_name}",
                "latest_path": f"/shared-evidence/{agent_id}/latest",
            }
            archive = self._pack(source, workdir, messages, task, metadata)
            self._exec("import os,sys; "
                       "assert not os.path.lexists(sys.argv[2]), 'Snapshot step already exists'; "
                       "os.makedirs(sys.argv[1], exist_ok=False)", staging, final_path)
            try:
                if not self.helper.put_archive(staging, archive):
                    raise RuntimeError("Docker refused the evidence archive")
                self._exec(
                    "import os,sys; "
                    "stage,final,link,temp,target=sys.argv[1:]; "
                    "assert not os.path.lexists(final), 'Snapshot step already exists'; "
                    "os.rename(stage,final); os.symlink(target,temp); os.replace(temp,link)",
                    staging, final_path, f"{prefix}/latest", f"{prefix}/.latest-{token}",
                    f"snapshots/{step_name}",
                )
            except BaseException:
                try:
                    self._exec("import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", staging)
                except Exception:
                    pass
                raise
            self._versions[agent_id] = metadata
            return json.loads(json.dumps(metadata))

    def version(self, agent_id):
        with self._agent_lock(agent_id):
            return json.loads(json.dumps(self._versions[agent_id]))

    def cleanup(self):
        with ExitStack() as stack:
            for agent in sorted(self._locks):
                stack.enter_context(self._locks[agent])
            if not self._closed:
                self.helper.remove(force=True)
                self._closed = True
