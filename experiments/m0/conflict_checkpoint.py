"""Current Git-index conflict checkpoints, independent of the old AST gate.

The collector calls these helpers only at completed paired-tool boundaries.
Logs and conflict-marker text never establish eligibility: at least one index
must currently contain Git stage 1/2/3 entries. Inspection does not run tests,
refresh the index, resolve a conflict, or publish work.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex

CHECKPOINT_SCHEMA = "current_unmerged_checkpoint_v1"
OBSERVATION_SCHEMA = "git_current_unmerged_v1"
CHECKPOINT_RULE = "first_current_unmerged_paired_boundary_v1"
AGENTS = ("agent1", "agent2")
TERMINAL_POLICIES = ("both_active",)
METADATA_NAMES = ("index", "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "CHERRY_PICK_HEAD",
                  "REBASE_HEAD", "REVERT_HEAD", "AUTO_MERGE", "ORIG_HEAD",
                  "rebase-merge", "rebase-apply", "sequencer")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_OID = re.compile(r"[a-f0-9]{40}\Z")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode()).hexdigest()


_INSPECTION = r'''
import hashlib, json, os, pathlib, stat, subprocess
root = pathlib.Path.cwd().resolve()
env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
def git(*args):
    result = subprocess.run(['git', '--no-optional-locks', *args], env=env,
        capture_output=True, timeout=30)
    if result.returncode: raise RuntimeError('Git inspection failed: ' + args[0])
    return result.stdout
def text(data): return data.decode('utf-8', errors='surrogateescape')
def item(path):
    if not path.exists() and not path.is_symlink(): return {'type':'missing'}
    st = path.lstat()
    value = {'mode':stat.S_IMODE(st.st_mode)}
    if stat.S_ISLNK(st.st_mode): value.update(type='symlink', target=os.readlink(path))
    elif stat.S_ISREG(st.st_mode):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
        value.update(type='file', bytes=st.st_size, sha256=digest.hexdigest())
    elif stat.S_ISDIR(st.st_mode): value.update(type='directory')
    else: raise RuntimeError('Unsupported special file in conflict evidence')
    return value
raw = git('ls-files', '--unmerged', '-z')
entries = []
for record in raw.split(b'\0'):
    if not record: continue
    metadata, name = record.split(b'\t', 1)
    mode, oid, stage = metadata.decode('ascii').split()
    entries.append({'mode':mode, 'oid':oid, 'stage':int(stage), 'path':text(name)})
entries.sort(key=lambda row:(row['path'],row['stage']))
status = text(git('status', '--porcelain=v1', '-z', '--untracked-files=all'))
head = git('rev-parse','HEAD').decode('ascii').strip()
git_dir = pathlib.Path(text(git('rev-parse','--absolute-git-dir')).strip()).resolve()
# Native task repositories have their complete .git directory inside the
# snapshotted worktree. Refuse a linked checkout whose metadata lives elsewhere.
git_relative = git_dir.relative_to(root).as_posix()
metadata = {}
for name in METADATA:
    path = git_dir / name
    metadata[name] = item(path)
    if metadata[name]['type'] == 'directory':
        for parent, directories, files in os.walk(path, followlinks=False):
            for child in sorted(directories+files):
                candidate = pathlib.Path(parent)/child
                metadata[candidate.relative_to(git_dir).as_posix()] = item(candidate)
worktree = {name:item(root/name) for name in sorted({r['path'] for r in entries})}
value = {'schema':SCHEMA, 'head':head, 'git_dir':git_relative,
    'has_unmerged':bool(entries), 'unmerged_entries':entries,
    'status_porcelain_z':status, 'operation_metadata':metadata, 'conflict_worktree':worktree}
body = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
value['fingerprint_sha256'] = hashlib.sha256(body.encode()).hexdigest()
print(json.dumps(value, sort_keys=True, ensure_ascii=True))
'''.replace("METADATA", repr(METADATA_NAMES)).replace("SCHEMA", repr(OBSERVATION_SCHEMA))


def _safe_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _validate_item(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid conflict filesystem observation")
    kind = value.get("type")
    keys = {"missing": {"type"}, "directory": {"type", "mode"},
        "symlink": {"type", "mode", "target"}, "file": {"type", "mode", "bytes", "sha256"}}
    if kind not in keys or set(value) != keys[kind]:
        raise ValueError("Incomplete conflict filesystem observation")
    if kind != "missing" and (type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o7777):
        raise ValueError("Invalid conflict file mode")
    if kind == "file" and (type(value["bytes"]) is not int or value["bytes"] < 0
            or not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"])):
        raise ValueError("Invalid conflict file checksum")
    if kind == "symlink" and not isinstance(value["target"], str):
        raise ValueError("Invalid conflict symlink")


def validate_observation(value):
    keys = {"schema", "head", "git_dir", "has_unmerged", "unmerged_entries", "status_porcelain_z",
            "operation_metadata", "conflict_worktree", "fingerprint_sha256"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != OBSERVATION_SCHEMA:
        raise ValueError("Not a current Git conflict observation")
    if (not isinstance(value["head"], str) or not _OID.fullmatch(value["head"])
            or not _safe_path(value["git_dir"]) or type(value["has_unmerged"]) is not bool
            or not isinstance(value["status_porcelain_z"], str)):
        raise ValueError("Invalid Git conflict identity")
    entries = value["unmerged_entries"]
    if not isinstance(entries, list):
        raise ValueError("Invalid unmerged index entries")
    for row in entries:
        if (not isinstance(row, dict) or set(row) != {"mode", "oid", "stage", "path"}
                or row["mode"] not in ("100644", "100755", "120000", "160000")
                or not isinstance(row["oid"], str) or not _OID.fullmatch(row["oid"])
                or type(row["stage"]) is not int or row["stage"] not in (1, 2, 3)
                or not _safe_path(row["path"])):
            raise ValueError("Invalid current unmerged index entry")
    pairs = [(r["path"], r["stage"]) for r in entries]
    if pairs != sorted(set(pairs)) or bool(entries) != value["has_unmerged"]:
        raise ValueError("Unmerged index is inconsistent or duplicated")
    files, metadata = value["conflict_worktree"], value["operation_metadata"]
    if not isinstance(files, dict) or set(files) != {r["path"] for r in entries}:
        raise ValueError("Conflict worktree files do not match the current index")
    if not isinstance(metadata, dict) or not set(METADATA_NAMES).issubset(metadata):
        raise ValueError("Git operation metadata is incomplete")
    for name, item in metadata.items():
        if (not _safe_path(name) or name.split("/", 1)[0] not in METADATA_NAMES
                or "/" in name and name.split("/", 1)[0] not in ("rebase-merge", "rebase-apply", "sequencer")):
            raise ValueError("Unexpected Git operation metadata path")
        _validate_item(item)
    for item in files.values():
        _validate_item(item)
    if metadata["index"].get("type") != "file":
        raise ValueError("Missing Git index")
    body = {k: v for k, v in value.items() if k != "fingerprint_sha256"}
    if value["fingerprint_sha256"] != fingerprint(body):
        raise ValueError("Conflict observation checksum differs")
    return value


def inspect_conflict(env):
    result = env.execute({"command": "python -c " + shlex.quote(_INSPECTION)})
    if result.get("returncode") != 0:
        raise RuntimeError("Cannot inspect current unmerged Git index")
    return validate_observation(json.loads(result["output"]))


def _terminal_outcome(outcome):
    if (not isinstance(outcome, dict) or outcome.get("infrastructure_error") is not False
            or type(outcome.get("exited")) is not bool or type(outcome.get("submitted")) is not bool):
        raise ValueError("Missing explicit agent terminal state")
    if outcome["submitted"] != (outcome["exited"] and outcome.get("exit_status") == "Submitted"):
        raise ValueError("Submitted must mean a normal terminal completion")
    if not outcome["exited"] and outcome.get("exit_status") is not None:
        raise ValueError("Active agent cannot have an exit status")
    return outcome["exited"]


def conflict_eligible(observations, outcomes, terminal_policy="both_active"):
    """Pure boundary predicate. A noneligible boundary is never a new sample."""
    if terminal_policy not in TERMINAL_POLICIES or set(observations) != set(AGENTS) or set(outcomes) != set(AGENTS):
        return False
    try:
        for a in AGENTS:
            validate_observation(observations[a])
        terminal = {a: _terminal_outcome(outcomes[a]) for a in AGENTS}
    except (ValueError, TypeError):
        return False
    if any(terminal.values()):
        return False
    return any(o["has_unmerged"] for o in observations.values())


def _verify_manifest(manifest, observation):
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or set(manifest.get("roots", {})) != {"/workspace/repo", "/tmp"}
            or manifest.get("sha256") != fingerprint(manifest["roots"])):
        raise ValueError("Workspace and temporary-state manifest is incomplete or changed")
    rows = manifest["roots"]["/workspace/repo"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not _safe_path(row.get("path")) for row in rows):
        raise ValueError("Invalid workspace manifest paths")
    index = {row["path"]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError("Duplicate workspace manifest entries")
    expected = {**observation["conflict_worktree"], **{
        observation["git_dir"] + "/" + path: info for path, info in observation["operation_metadata"].items()}}
    for path, item in expected.items():
        actual = index.get(path)
        if item["type"] == "missing":
            if actual is not None:
                raise ValueError("Absent conflict file appears in snapshot manifest")
        elif (not actual or actual.get("type") != item["type"] or actual.get("mode") != item["mode"]
                or item["type"] == "file" and (actual.get("sha256") != item["sha256"] or actual.get("size") != item["bytes"])
                or item["type"] == "symlink" and actual.get("target") != item["target"]):
            raise ValueError("Conflict state differs from the saved filesystem manifest: " + path)


def _verify_history(state, outcome):
    required = {"messages", "n_calls", "cost", "sent_messages", "extra_template_vars",
        "_last_prompt_tokens", "_segments", "_current_segment_messages", "_compaction_count"}
    if not isinstance(state, dict) or not required.issubset(state) or not isinstance(state["messages"], list):
        raise ValueError("Checkpoint does not preserve complete native agent state")
    pending, exited = set(), []
    for message in state["messages"]:
        if not isinstance(message, dict):
            raise ValueError("Invalid saved history message")
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or call["id"] in pending:
                    raise ValueError("Invalid pending tool call in saved history")
                pending.add(call["id"])
        elif message.get("role") == "tool":
            if message.get("tool_call_id") not in pending:
                raise ValueError("Unpaired tool response in saved history")
            pending.remove(message["tool_call_id"])
        elif message.get("role") == "exit":
            exited.append(message)
    if pending:
        raise ValueError("Checkpoint is inside an unfinished tool call")
    if bool(exited) != outcome["exited"] or len(exited) > 1:
        raise ValueError("Terminal state differs from saved history")
    if exited and (state["messages"][-1] is not exited[0]
            or exited[0].get("extra", {}).get("exit_status") != outcome["exit_status"]):
        raise ValueError("Saved terminal history cannot be resumed as active")


def validate_conflict_checkpoint(checkpoint, task):
    """Validate data only; no Docker, model, evaluation, or old WIP validator."""
    if (checkpoint.get("schema") != CHECKPOINT_SCHEMA or "wip_checkpoint" in checkpoint
            or checkpoint.get("task") != task.as_dict() or set(checkpoint.get("agents", {})) != set(AGENTS)):
        raise ValueError("Not this task's current-unmerged checkpoint schema")
    gate = checkpoint.get("conflict_checkpoint", {})
    if (gate.get("rule") != CHECKPOINT_RULE or gate.get("eligible") is not True
            or gate.get("selection_uses_test_results") is not False
            or type(gate.get("paired_round")) is not int or gate["paired_round"] <= 0
            or checkpoint.get("prefix_steps_per_agent") != gate["paired_round"]
            or not conflict_eligible(gate.get("observations", {}), gate.get("outcomes", {}), gate.get("terminal_policy"))):
        raise ValueError("Checkpoint is not an eligible current-unmerged paired boundary")
    if not isinstance(checkpoint.get("git_archive_sha256"), str) or not _SHA.fullmatch(checkpoint["git_archive_sha256"]):
        raise ValueError("Shared Git archive checksum is missing")
    broker = checkpoint.get("broker", {})
    if (broker.get("schema_version") != 1 or broker.get("agents") != list(AGENTS)
            or set(broker.get("inboxes", {})) != set(AGENTS) or broker.get("blocked")
            or not isinstance(broker.get("events"), list) or type(broker.get("sequence")) is not int):
        raise ValueError("Mailbox is not a complete quiescent paired-agent snapshot")
    terminal = {a for a in AGENTS if gate["outcomes"][a]["exited"]}
    if set(broker.get("exited", {})) != terminal or set(broker.get("alive", [])) != set(AGENTS) - terminal:
        raise ValueError("Mailbox terminal/alive identities differ from the saved agent states")
    for a, feature in task.agents.items():
        saved, observation, outcome = checkpoint["agents"][a], gate["observations"][a], gate["outcomes"][a]
        if (saved.get("git_base") != task.base or not isinstance(saved.get("image"), str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", saved["image"])):
            raise ValueError("Agent snapshot or original Git base differs")
        state = saved.get("state", {})
        _verify_history(state, outcome)
        original = (task.task_dir / f"feature{feature}" / "feature.md").read_text()
        if state["extra_template_vars"].get("task") != original:
            raise ValueError("Original per-agent feature requirement differs")
        _verify_manifest(saved.get("manifest"), observation)
    return None


def verify_checkpoint_files(run_dir, task):
    """Bind schema validation to the actual shared-Git archive before restore."""
    run_dir = Path(run_dir)
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
    validate_conflict_checkpoint(checkpoint, task)
    if hashlib.sha256((run_dir / "shared_git.tar").read_bytes()).hexdigest() != checkpoint["git_archive_sha256"]:
        raise ValueError("Shared Git archive differs from conflict checkpoint")
    return checkpoint


def verify_restored_conflict(env, expected):
    validate_observation(expected)
    actual = inspect_conflict(env)
    if actual != expected:
        raise ValueError("Restored unmerged index, operation metadata, or worktree differs")
    return {"verified": True, "fingerprint_sha256": actual["fingerprint_sha256"],
            "has_unmerged": actual["has_unmerged"], "unmerged_entry_count": len(actual["unmerged_entries"])}
