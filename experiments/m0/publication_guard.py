"""Equal, bounded publication reminders for native Solo and cooperative agents.

Only a voluntary native Submitted exit can be deferred. The guard inspects Git
without changing files, refs, commits, or publications. The model must perform
any commit/push/PR itself, within the existing execution allowance.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shlex
import threading
import time

GUARD_VERSION = "publication_parity_v1"
SCHEMA = "publication_guard_v1"
MAX_NUDGES = 2
_CONTEXT_LOCK = threading.RLock()
_ACTIVE = False

# Runs inside the already isolated agent environment. GIT_OPTIONAL_LOCKS=0
# prevents even a status query from refreshing the index on disk. No fetch,
# reset, add, commit, push, tag, or PR command is executed by this script.
_INSPECT = r'''
import json, os, pathlib, re, subprocess, sys
agent, base = sys.argv[1:]
env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
def git(*args, okay=(0,)):
    result = subprocess.run(['git', '--no-optional-locks', *args], capture_output=True,
                            text=True, timeout=10, env=env)
    if result.returncode not in okay:
        raise RuntimeError('read-only Git inspection failed: ' + args[0])
    return result
remote = git('remote', 'get-url', 'origin').stdout.strip()
if not pathlib.Path(remote).is_absolute() or not pathlib.Path(remote).is_dir():
    raise RuntimeError('publication inspection requires the native local Git remote')
output = git('ls-remote', '--refs', 'origin', 'refs/tags/pr/' + agent,
             'refs/heads/' + agent).stdout
refs = {}
for line in output.splitlines():
    sha, name = line.split()
    if not re.fullmatch('[a-f0-9]{40}', sha): raise RuntimeError('invalid remote object')
    refs[name] = sha
pr = 'refs/tags/pr/' + agent in refs
head = refs.get('refs/heads/' + agent)
record = dict(pr_open=pr, remote_branch=head, remote_nonempty=False, dirty=False, local_ahead=0)
if not pr and head:
    commit = git('--git-dir=' + remote, 'cat-file', '-t', head).stdout.strip() == 'commit'
    ancestor = git('--git-dir=' + remote, 'merge-base', '--is-ancestor', base, head, okay=(0,1)).returncode == 0
    if commit and ancestor:
        record['remote_nonempty'] = git('--git-dir=' + remote, 'diff', '--quiet', base, head,
                                       '--', okay=(0,1)).returncode == 1
if not pr and not record['remote_nonempty']:
    record['dirty'] = bool(git('status', '--porcelain').stdout.strip())
    record['local_ahead'] = int(git('rev-list', '--count', (head or base) + '..HEAD').stdout.strip())
print(json.dumps(record, sort_keys=True))
'''


def _voluntary_submitted(agent):
    if not agent.messages or not isinstance(agent.messages[-1], dict):
        return False
    last = agent.messages[-1]
    extra = last.get("extra")
    return (last.get("role") == "exit" and isinstance(extra, dict)
            and extra.get("exit_status") == "Submitted"
            and isinstance(extra.get("submission"), str)
            and last.get("content") == extra["submission"]
            and not any(k in extra for k in ("exception_str", "traceback")))


def inspect_publication(agent, base):
    agent_id = agent.agent_id
    if agent_id not in ("solo", "agent1", "agent2") or not re.fullmatch(r"[a-f0-9]{40}", base):
        raise ValueError("Publication guard requires a native agent and pinned base")
    command = "python -c " + shlex.quote(_INSPECT) + " " + shlex.quote(agent_id) + " " + shlex.quote(base)
    result = agent.env.execute({"command": command})
    if result.get("returncode") != 0:
        raise RuntimeError("Read-only publication inspection failed")
    record = json.loads(result.get("output", ""))
    if (set(record) != {"pr_open", "remote_branch", "remote_nonempty", "dirty", "local_ahead"}
            or any(type(record[k]) is not bool for k in ("pr_open", "remote_nonempty", "dirty"))
            or type(record["local_ahead"]) is not int or record["local_ahead"] < 0
            or record["remote_branch"] is not None and not re.fullmatch(r"[a-f0-9]{40}", record["remote_branch"])):
        raise RuntimeError("Invalid read-only publication evidence")
    return record


def reminder(agent_id, dirty):
    held = "uncommitted changes" if dirty else "commits that are not pushed"
    return (
        f"You have not submitted anything. Your work is still local ({held}) and "
        "no pull request or nonempty published branch exists, so it would be graded as an empty submission.\n\n"
        "    git add <the files you want to submit>\n"
        '    git commit -m "..."\n'
        f"    git push origin HEAD:{agent_id}\n"
        '    gh pr create --title "..." --body "..."\n\n'
        "Then echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT again. "
        "If you intentionally withdraw this contribution, you may leave an empty submission; "
        "do not invent changes merely to publish. This reminder does not extend the execution deadline."
    )


@contextmanager
def publication_guard(run_dir, *, deadline=None):
    """Temporarily replace only the native reminder method; always restore it.

    run_dir is the job directory. The receipt is inside its condition directory
    so the unchanged infrastructure-retry archiver preserves each attempt.
    The default deadline starts at context entry, conservatively before worker
    initialization; the original worker also keeps its own deadline.
    """
    global _ACTIVE
    os.environ.setdefault("MSWEA_GLOBAL_CONFIG_DIR", str(Path(__file__).resolve().parent / "config/mini-swe-agent"))
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent

    started = time.monotonic()
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "task_config.json").read_text())
    condition = config.get("condition")
    seconds, base = config.get("execution_seconds"), config.get("task", {}).get("base")
    if (condition not in ("solo", "strongshare", "integrator")
            or not isinstance(base, str) or not re.fullmatch(r"[a-f0-9]{40}", base)
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        raise ValueError("Publication guard requires a valid job identity and time allowance")
    if deadline is None:
        deadline = started + seconds
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline > started + seconds:
        raise ValueError("Guard deadline must be finite and cannot extend the existing allowance")
    path = run_dir / condition / "publication_guard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("Publication guard evidence already exists; archive an incomplete phase before retrying")
    record = {"schema": SCHEMA, "guard_version": GUARD_VERSION, "run_id": run_dir.name,
        "task_key": config.get("task_key"), "condition": condition,
        "max_nudges_per_agent": MAX_NUDGES, "deadline_monotonic": deadline,
        "started_monotonic": started, "execution_seconds": seconds,
        "deadline_extended": False, "automatic_publication": False,
        "counts": {}, "events": [], "context_closed": False}
    event_lock = threading.RLock()

    def persist():
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)

    def event(agent, reason, *, observation=None, nudged=False, error_type=None):
        item = {"index": len(record["events"]) + 1, "agent_id": agent.agent_id,
                "elapsed_seconds": round(time.monotonic() - started, 6), "reason": reason,
                "nudged": nudged, "nudge_count": record["counts"].get(agent.agent_id, 0)}
        if observation is not None: item["publication_state"] = observation
        if error_type is not None: item["error_type"] = error_type
        record["events"].append(item)
        persist()

    def nudge(agent):
        # Agent loops are concurrent; serialize the short local inspection and
        # evidence write, not any model call. No provider is called here.
        with event_lock:
            if not _voluntary_submitted(agent):
                event(agent, "not_voluntary_submitted")
                return False
            if time.monotonic() >= deadline:
                event(agent, "deadline_reached")
                return False
            if record["counts"].get(agent.agent_id, 0) >= MAX_NUDGES:
                event(agent, "reminder_limit_reached")
                return False
            try:
                observation = inspect_publication(agent, base)
            except Exception as exc:
                event(agent, "inspection_error", error_type=type(exc).__name__)
                raise
            if observation["pr_open"]:
                event(agent, "already_has_pr", observation=observation)
                return False
            if observation["remote_nonempty"]:
                event(agent, "already_published_branch", observation=observation)
                return False
            if not observation["dirty"] and not observation["local_ahead"]:
                event(agent, "no_unpublished_work", observation=observation)
                return False
            if time.monotonic() >= deadline:
                event(agent, "deadline_reached_after_inspection", observation=observation)
                return False
            count = record["counts"].get(agent.agent_id, 0) + 1
            record["counts"][agent.agent_id] = count
            agent._submit_nudges = count
            agent.add_messages(agent.model.format_message(role="user", content=reminder(agent.agent_id, observation["dirty"])))
            event(agent, "unpublished_local_work", observation=observation, nudged=True)
            return True

    with _CONTEXT_LOCK:
        if _ACTIVE:
            raise RuntimeError("Publication guard contexts cannot be nested")
        previous = DefaultAgent._nudge_unsubmitted
        _ACTIVE = True
        try:
            persist()
            DefaultAgent._nudge_unsubmitted = nudge
            yield record
        finally:
            DefaultAgent._nudge_unsubmitted = previous
            _ACTIVE = False
            with event_lock:
                record["context_closed"] = True
                record["elapsed_seconds"] = round(time.monotonic() - started, 6)
                persist()


publication_guard_context = publication_guard
