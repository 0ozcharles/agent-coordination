"""Local baseline runtime: isolated Docker filesystems and resumable mailboxes.

Importing this module does not call Docker or a model. Snapshot callers must
pause both agent loops at complete tool boundaries before collecting state.
Docker commits preserve files, not running processes or mounted volumes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import tarfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
RUN_LABEL = "agent-rsi-run"
GIT_REMOTE_PATH = "/shared-git/team.git"
os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(ROOT / "config/mini-swe-agent"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

from cooperbench.agents.mini_swe_agent_v2.environments.docker import DockerEnvironment


class InfrastructureError(RuntimeError):
    """The sandbox cannot execute reliably; the coordinator must stop model use."""


class MemoryBroker:
    """A branch-local, thread-safe mailbox store with explicit lifecycle state."""

    def __init__(self, agents):
        self.agents = list(agents)
        if not self.agents or len(set(self.agents)) != len(self.agents):
            raise ValueError("Agent names must be nonempty and unique")
        self.condition = threading.Condition(threading.RLock())
        self.inboxes = {agent: [] for agent in self.agents}
        self.exited = {}
        self.alive = set()
        self.blocked = {}
        self.events = []
        self.sequence = 0

    def connector(self, agent_id):
        return MemoryConnector(agent_id, self.agents, self)

    def snapshot(self, *, require_quiescent=True):
        with self.condition:
            if require_quiescent and self.blocked:
                raise RuntimeError("Cannot checkpoint while send_and_wait is active")
            return copy.deepcopy({
                "schema_version": 1, "agents": self.agents, "inboxes": self.inboxes,
                "exited": self.exited, "alive": sorted(self.alive),
                "blocked": self.blocked, "events": self.events, "sequence": self.sequence,
            })

    def restore(self, data):
        state = copy.deepcopy(data)
        if state.get("schema_version") != 1 or state.get("agents") != self.agents:
            raise ValueError("Mailbox snapshot schema or agent identities do not match")
        if state.get("blocked"):
            raise ValueError("An active blocking wait cannot be resumed from a mailbox snapshot")
        if set(state["inboxes"]) != set(self.agents):
            raise ValueError("Mailbox snapshot is incomplete")
        with self.condition:
            self.inboxes = state["inboxes"]
            self.exited = state["exited"]
            self.alive = set(state["alive"])
            self.blocked = {}
            self.events = state["events"]
            self.sequence = state["sequence"]
            self.condition.notify_all()
        return self

    @classmethod
    def from_snapshot(cls, data):
        return cls(data["agents"]).restore(data)


class MemoryConnector:
    """Duck-types the native MessagingConnector; initialization never clears mail."""

    def __init__(self, agent_id, agents, broker):
        if agent_id not in broker.agents or list(agents) != broker.agents:
            raise ValueError("Connector identities must match its branch's broker")
        self.agent_id, self.agents, self.broker = agent_id, list(agents), broker
        self.heartbeat()

    def setup(self, env):
        pass

    def heartbeat(self):
        with self.broker.condition:
            if self.agent_id not in self.broker.exited:
                self.broker.alive.add(self.agent_id)
            self.broker.condition.notify_all()

    def mark_exited(self, published=False):
        with self.broker.condition:
            self.broker.exited[self.agent_id] = bool(published)
            self.broker.alive.discard(self.agent_id)
            self.broker.condition.notify_all()

    def has_exited(self, agent_id):
        with self.broker.condition:
            return agent_id in self.broker.exited

    def is_unreachable(self, agent_id):
        # The coordinator owns these threads and must mark crashed agents exited.
        # Wall-clock heartbeat expiration would incorrectly kill paused checkpoints.
        return False

    def has_published(self, agent_id):
        with self.broker.condition:
            return self.broker.exited.get(agent_id) is True

    def send(self, recipient, content):
        with self.broker.condition:
            if recipient not in self.broker.inboxes or recipient in self.broker.exited:
                return False
            self.broker.sequence += 1
            message = {"from": self.agent_id, "to": recipient, "content": str(content),
                       "timestamp": datetime.now(timezone.utc).isoformat(),
                       "sequence": self.broker.sequence}
            self.broker.inboxes[recipient].append(message)
            self.broker.events.append(copy.deepcopy(message))
            self.broker.condition.notify_all()
            return True

    def receive(self):
        with self.broker.condition:
            messages = self.broker.inboxes[self.agent_id]
            self.broker.inboxes[self.agent_id] = []
            return copy.deepcopy(messages)

    def send_and_wait(self, recipient, content, timeout=60):
        with self.broker.condition:
            if not self.send(recipient, content):
                return False, []
            self.broker.blocked[recipient] = self.agent_id
            deadline = time.monotonic() + max(0, timeout)
            try:
                while True:
                    messages = self.receive()
                    if messages:
                        return True, messages
                    if self.has_exited(recipient):
                        return True, []
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return True, []
                    self.broker.condition.wait(remaining)
            finally:
                if self.broker.blocked.get(recipient) == self.agent_id:
                    del self.broker.blocked[recipient]

    def is_awaited_by(self, sender):
        with self.broker.condition:
            return self.broker.blocked.get(self.agent_id) == sender

    def broadcast(self, content):
        for agent in self.agents:
            if agent != self.agent_id:
                self.send(agent, content)

    def peek(self):
        with self.broker.condition:
            return len(self.broker.inboxes[self.agent_id])


def _docker_environment():
    clean = {name: os.environ[name] for name in ("HOME", "USER", "TMPDIR", "LANG") if name in os.environ}
    clean["DOCKER_HOST"] = os.environ.get("DOCKER_HOST", "unix://" + str(Path.home() / ".docker/run/docker.sock"))
    clean["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return clean


def _docker(args, *, timeout=120, stdin=None, stdout=None, check=True):
    return subprocess.run(
        [DOCKER, *map(str, args)], env=_docker_environment(), stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=check,
    )


def _volume_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid Docker volume name")
    return value


def _labels(run_label, resource):
    return ["--label", f"{RUN_LABEL}={run_label}", "--label", f"agent-rsi-resource={resource}"]


def _new_volume(name, run_label):
    _volume_name(name)
    existing = _docker(["volume", "inspect", name], check=False)
    if existing.returncode == 0:
        raise ValueError("Refusing to reuse an existing volume: " + name)
    _docker(["volume", "create", *_labels(run_label, "git-volume"), name])
    return name


def _volume_helper(volume, image, run_label, command, *, readonly=False, stdin=None, stdout=None):
    name = "agent-rsi-volume-" + uuid.uuid4().hex[:12]
    mount = f"type=volume,source={_volume_name(volume)},target=/volume" + (",readonly" if readonly else "")
    args = ["run", "--rm", "--name", name, "--pull=never", "--network", "none",
            "--cpus=2", "--memory=2g", "--pids-limit=128", *_labels(run_label, "helper"),
            "--mount", mount, "--entrypoint", command[0]]
    if stdin is not None:
        args.append("-i")
    try:
        return _docker([*args, image, *command[1:]], timeout=180, stdin=stdin, stdout=stdout)
    finally:
        _docker(["rm", "-f", name], timeout=20, check=False)


def create_shared_git_volume(name, image, run_label):
    """Create a fresh named volume containing /team.git; return its name."""
    _new_volume(name, run_label)
    try:
        _volume_helper(name, image, run_label, [
            "/bin/sh", "-c", "git init --bare /volume/team.git && "
            "git -C /volume/team.git config receive.denyCurrentBranch ignore",
        ])
    except BaseException:
        _docker(["volume", "rm", name], check=False)
        raise
    return name


def backup_volume(volume, archive, image, run_label):
    """Export a quiescent Git volume without exposing a host directory to Docker."""
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            _volume_helper(volume, image, run_label, ["tar", "-C", "/volume", "-cpf", "-", "."],
                           readonly=True, stdout=stream)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return {"archive": str(archive), "sha256": digest, "bytes": archive.stat().st_size, "volume": volume}


def restore_volume(archive, new_volume, image, run_label, *, expected_sha256=None):
    """Restore into a newly created volume, never into the source volume."""
    archive = Path(archive)
    if expected_sha256 and hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Shared Git archive checksum does not match")
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.isdev() or member.isfifo():
                raise ValueError("Unsafe member in shared Git archive")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError("Unsafe link in shared Git archive")
    _new_volume(new_volume, run_label)
    try:
        with archive.open("rb") as stream:
            _volume_helper(new_volume, image, run_label, ["tar", "-C", "/volume", "-xpf", "-"], stdin=stream)
    except BaseException:
        _docker(["volume", "rm", new_volume], check=False)
        raise
    return new_volume


_PROCESS_SCRIPT = r'''
import json, os
rows = []
for name in os.listdir('/proc'):
    if not name.isdigit() or int(name) == os.getpid(): continue
    try:
        raw = open('/proc/' + name + '/stat').read()
        fields = raw[raw.rfind(')') + 2:].split()
        raw_cmd = open('/proc/' + name + '/cmdline', 'rb').read()
        argv = [item.decode(errors='replace') for item in raw_cmd.rstrip(b'\0').split(b'\0')] if raw_cmd else []
        rows.append({'pid': int(name), 'ppid': int(fields[1]), 'state': fields[0], 'starttime': fields[19], 'command': ' '.join(argv), 'argv': argv})
    except FileNotFoundError:
        pass
print(json.dumps(sorted(rows, key=lambda row: row['pid'])))
'''

_MANIFEST_SCRIPT = r'''
import hashlib, json, os, stat
roots = ['/workspace/repo', '/tmp']
mounts = [line.split()[4].replace('\\040', ' ') for line in open('/proc/self/mountinfo')]
for root in roots:
    if any(mount == root or mount.startswith(root + '/') for mount in mounts):
        raise RuntimeError('Unexpected mount under checkpoint root: ' + root)
result = {}
for root in roots:
    rows = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories + files):
            path = os.path.join(parent, name)
            s = os.lstat(path)
            row = {'path': os.path.relpath(path, root), 'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'gid': s.st_gid}
            if stat.S_ISLNK(s.st_mode): row.update(type='symlink', target=os.readlink(path))
            elif stat.S_ISDIR(s.st_mode): row.update(type='directory')
            elif stat.S_ISREG(s.st_mode):
                digest = hashlib.sha256()
                with open(path, 'rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
                row.update(type='file', size=s.st_size, sha256=digest.hexdigest())
            else: row.update(type='special', device=s.st_rdev)
            rows.append(row)
    result[root] = sorted(rows, key=lambda row: row['path'])
body = json.dumps(result, sort_keys=True, separators=(',', ':'))
print(json.dumps({'schema_version': 1, 'roots': result, 'sha256': hashlib.sha256(body.encode()).hexdigest()}))
'''


class SnapshotDockerEnvironment(DockerEnvironment):
    """Native bash execution with scoped volumes and filesystem-only snapshots."""

    def __init__(self, *, image, run_label, agent_id, shared_git_volume,
                 evidence_volume=None, timeout=120, container_timeout="40m", **kwargs):
        self.run_label, self.agent_id = run_label, agent_id
        self.shared_git_volume, self.evidence_volume = shared_git_volume, evidence_volume
        self._managed_processes = {}
        _volume_name(shared_git_volume)
        run_args = ["--rm", "--init", "--pull=never", *_labels(run_label, "agent"),
                    "--label", f"agent-rsi-agent={agent_id}", "--cpus=2", "--memory=2g", "--pids-limit=256",
                    "--mount", f"type=volume,source={shared_git_volume},target=/shared-git"]
        if evidence_volume:
            run_args += ["--mount", f"type=volume,source={_volume_name(evidence_volume)},target=/shared-evidence,readonly"]
        allowed_env = {"PAGER", "MANPAGER", "LESS", "PIP_PROGRESS_BAR", "TQDM_DISABLE", "LANG", "LC_ALL"}
        if set(kwargs.get("env", {})) - allowed_env:
            raise ValueError("Unexpected container environment variables; credentials are never forwarded")
        kwargs.update(image=image, cwd="/workspace/repo", executable=DOCKER,
                      timeout=timeout, network="none", container_timeout=container_timeout,
                      forward_env=[], run_args=run_args)
        try:
            super().__init__(**kwargs)
            rows = self._process_table()
            for row in self._idle_process_tree(rows):
                self._managed_processes[row["pid"]] = row["starttime"]
        except BaseException:
            self.cleanup()
            raise

    @property
    def git_remote(self):
        return GIT_REMOTE_PATH

    def get_template_vars(self, **kwargs):
        values = super().get_template_vars(**kwargs)
        values.update(system="Linux", machine="aarch64", release="container", version="container")
        return values

    def _process_table(self):
        result = _docker(["exec", self.container_id, "python", "-c", _PROCESS_SCRIPT], timeout=20)
        return json.loads(result.stdout)

    def _idle_process_tree(self, rows):
        """Accept only Docker's init and the exact shell/sleep keeping it alive."""
        live = [row for row in rows if row.get("state") not in {"Z", "X"}]
        by_pid = {row["pid"]: row for row in live}
        sleep_command = f"sleep {self.config.container_timeout}"
        for row in live:
            argv = row.get("argv") or shlex.split(row.get("command", ""))
            if row["pid"] == 1:
                valid = (bool(argv) and Path(argv[0]).name in {"docker-init", "tini"}
                         and argv[1:] == ["--", "/bin/bash", "-c", sleep_command])
            else:
                parent = by_pid.get(row["ppid"])
                parent_argv = [] if parent is None else (parent.get("argv") or shlex.split(parent.get("command", "")))
                expected_shell = ["/bin/bash", "-c", sleep_command]
                expected_sleep = ["sleep", str(self.config.container_timeout)]
                valid_parent = parent is not None and (parent["pid"] == 1 or parent_argv == expected_shell)
                valid = valid_parent and argv in (expected_shell, expected_sleep, ["/bin/sleep", str(self.config.container_timeout)])
            if not valid:
                raise InfrastructureError("Checkpoint has an unexpected active process: "
                                          f"pid={row['pid']} state={row.get('state', '?')} "
                                          f"command={row.get('command', '')[:160]!r}")
        if 1 not in by_pid or len(live) < 2:
            raise InfrastructureError("Docker init/sleep process tree is incomplete")
        return live

    def execute(self, action, cwd="", *, timeout=None):
        if not getattr(self, "container_id", None):
            raise InfrastructureError("Agent container is unavailable")
        result = super().execute(action, cwd, timeout=timeout)
        if result.get("returncode", 0) == 0:
            return result
        diagnostic = str(result.get("output", "")) + "\n" + str(result.get("exception_info", ""))
        failure = re.search(
            r"OCI runtime (?:exec |create )?failed|procReady not received|"
            r"unable to start container process|No such container|"
            r"container\s+\S+\s+is not running|container\s+\S+\s+is paused|"
            r"Cannot connect to (?:the )?Docker daemon|error during connect|"
            r"(?:out of memory|cannot allocate memory|resource temporarily unavailable)|"
            r"cgroup[^\n]*(?:pids|memory)[^\n]*(?:limit|failed)",
            diagnostic, flags=re.IGNORECASE,
        )
        if failure:
            raise InfrastructureError("Docker execution failed: " + diagnostic[:2000])
        exception_type = result.get("extra", {}).get("exception_type")
        if exception_type in {"FileNotFoundError", "PermissionError", "OSError", "ConnectionError"}:
            raise InfrastructureError("Docker client failed: " + diagnostic[:2000])
        # A nonzero exit can be an ordinary failed test. Check the container state
        # before deciding that its output is safe to feed back to the model.
        try:
            state_result = _docker(["inspect", "--format", "{{json .State}}", self.container_id], timeout=20, check=False)
            if state_result.returncode:
                raise InfrastructureError("Cannot inspect failed agent container: " + state_result.stderr.decode(errors="replace")[:1000])
            state = json.loads(state_result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise InfrastructureError("Cannot verify agent container after command failure") from exc
        if state.get("OOMKilled") or not state.get("Running") or state.get("Paused") or state.get("Dead"):
            raise InfrastructureError("Agent container is unhealthy: " + json.dumps(state, sort_keys=True)[:2000])
        return result

    def checkpoint_quiescence(self):
        rows = self._process_table()
        live = self._idle_process_tree(rows)
        unexpected = [row for row in live
                      if self._managed_processes.get(row["pid"]) != row["starttime"]]
        if unexpected:
            raise RuntimeError("Cannot snapshot live agent/background processes: " +
                               ", ".join(f"pid={row['pid']} state={row.get('state', '?')} "
                                         f"command={row.get('command', '')[:160]!r}" for row in unexpected))
        return {"quiescent": True, "processes": rows, "restores_processes": False}

    def workspace_manifest(self):
        self.checkpoint_quiescence()
        result = _docker(["exec", self.container_id, "python", "-c", _MANIFEST_SCRIPT], timeout=120)
        return json.loads(result.stdout)

    def snapshot_image(self, tag=None):
        self.checkpoint_quiescence()
        # Docker pauses by default; the deprecated --pause flag prints a warning
        # to stdout on newer clients, corrupting the image-ID-only response.
        args = ["commit", "--change", f"LABEL {RUN_LABEL}={self.run_label}",
                "--change", "LABEL agent-rsi-resource=snapshot", self.container_id]
        if tag:
            args.append(tag)
        result = _docker(args, timeout=180)
        image_id = result.stdout.decode().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError("Docker commit did not return an immutable image ID: "
                               f"stdout={result.stdout[:2000]!r}; stderr={result.stderr[:2000]!r}")
        return image_id

    def cleanup(self):
        container_id = getattr(self, "container_id", None)
        if container_id:
            result = _docker(["rm", "-f", container_id], timeout=30, check=False)
            if result.returncode and b"No such container" not in result.stderr:
                raise RuntimeError("Unable to remove owned agent container")
            self.container_id = None


def checkpoint_quiescence(env):
    return env.checkpoint_quiescence()


def verify_workspace_manifest(env, expected):
    actual = env.workspace_manifest()
    if actual["sha256"] != expected["sha256"] or actual["roots"] != expected["roots"]:
        raise RuntimeError("Restored workspace and /tmp differ from their checkpoint manifest")
    return actual
