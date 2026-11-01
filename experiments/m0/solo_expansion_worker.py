"""Fresh, single-agent controls using the native mini-swe-agent solo prompt.

The supervisor owns scheduling, the process watchdog and official evaluation.
This worker never restores a checkpoint or reads another condition's artifacts.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import subprocess
import time
from pathlib import Path

from budget_guard import BudgetGuard, write_json
from run_baselines import close_interrupted_tool_calls, digest, safe_error, usage
from run_solo_smoke import MAX_OUTPUT, MODEL, configure
from task_specs import COMMIT, REPO, TaskSpec

PHASE = "solo"
AGENT = "solo"
OFFLINE_FILE = "fresh_solo_offline_probe.txt"
ISOLATION_MARKER = "fresh_solo_isolation_ok"


def combined_task_text(task: TaskSpec) -> str:
    """Use the exact feature ordering and separators in native _spawn_solo_agent."""
    return "\n\n---\n\n".join(
        f"## Feature {fid}\n\n{(task.task_dir / f'feature{fid}' / 'feature.md').read_text()}"
        for fid in task.features
    )


def initialize_fresh_agent(agent, combined_task: str, execution_seconds=1200):
    """Initialize exactly one system and one user message, without prior state."""
    if (agent.comm is not None or agent.messages or agent.n_calls != 0 or agent.cost != 0
            or agent.sent_messages or agent._compaction_count or agent._segments
            or agent._current_segment_messages):
        raise RuntimeError("Fresh Solo received nonempty agent state")
    agent.extra_template_vars = {
        "task": combined_task, "agent_id": None, "agents": [],
        "git_enabled": False, "messaging_enabled": False,
    }
    guidance = (
        "Execution guidance: use small targeted edits and do not start background processes. "
        "There is no monetary or model-turn limit. "
        f"The total execution allowance is {execution_seconds:g} seconds, including initialization. "
        "Leave time to commit, push, and publish your combined implementation before the timeout, "
        "even if incomplete or some tests fail. Do not claim a failing test passed. "
        "Then issue the exact completion command."
    )
    agent.add_messages(
        agent.model.format_message(role="system", content=agent._render_template(agent.config.system_template)),
        agent.model.format_message(role="user", content=(
            agent._render_template(agent.config.instance_template) + "\n\n" + guidance)),
    )
    if ([message.get("role") for message in agent.messages] != ["system", "user"]
            or combined_task not in agent.messages[1].get("content", "")):
        raise RuntimeError("Fresh Solo did not receive both requirements in its initial prompt")


def _workspace_probe(env, task):
    # Inspect names and digests, never credential values. The original image is
    # pinned separately; there are no host mounts or copied evaluation inputs.
    script = """
import json, os, re, subprocess
from pathlib import Path
def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()
credential_names = sorted(name for name in os.environ
    if re.search(r'(API_KEY|ACCESS_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)', name, re.I))
forbidden = ['/patches', '/shared-evidence', '/workspace/patches',
    '/workspace/repo/checkpoint.json', '/workspace/repo/shared_git.tar',
    '/root/.aws/credentials', '/root/.netrc', '/root/.config/openai']
print(json.dumps({'head': git('rev-parse', 'HEAD'),
    'status_porcelain': git('status', '--porcelain=v1', '--untracked-files=all'),
    'remote': git('remote', 'get-url', 'origin'),
    'refs': git('for-each-ref', '--format=%(refname) %(objectname)'),
    'private_remote_refs': git('ls-remote', 'origin'),
    'credential_environment_names': credential_names,
    'forbidden_paths_present': [p for p in forbidden if Path(p).exists()],
    'source_roots_exist': all((Path('/workspace/repo') / p).is_dir() for p in SOURCE_ROOTS)}))
""".replace("SOURCE_ROOTS", repr(list(task.code_roots)))
    result = env.execute({"command": "python -c " + shlex.quote(script)})
    if result.get("returncode") != 0:
        raise RuntimeError("Initial workspace inspection failed")
    proof = json.loads(result["output"])
    refs = [line.split() for line in proof["refs"].splitlines()]
    remote_refs = [line.split() for line in proof["private_remote_refs"].splitlines()]
    if (proof["head"] != task.base or proof["status_porcelain"]
            or proof["remote"] != "/shared-git/team.git" or not proof["source_roots_exist"]
            or proof["credential_environment_names"] or proof["forbidden_paths_present"]
            or not refs or any(len(row) != 2 or row[1] != task.base for row in refs)
            or {row[0] for row in refs} != {"refs/heads/solo", "refs/remotes/origin/solo", "refs/remotes/origin/main"}
            or {tuple(row) for row in remote_refs} != {
                (task.base, "refs/heads/main"), (task.base, "refs/heads/solo")}):
        raise RuntimeError("Initial workspace is not an isolated clean Solo base: " + json.dumps(proof))
    return proof


def inspect_initial_state(env, dc, task, volume_name, image_id, agent):
    container = dc.containers.get(env.container_id)
    container.reload()
    attrs = container.attrs
    mounts = attrs.get("Mounts", [])
    config = attrs.get("HostConfig", {})
    environment_names = [value.partition("=")[0] for value in attrs.get("Config", {}).get("Env", [])]
    credentials = [name for name in environment_names
                   if re.search(r"(API_KEY|ACCESS_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", name, re.I)]
    if (attrs.get("Image") != image_id or config.get("NetworkMode") != "none"
            or config.get("Init") is not True or config.get("NanoCpus") != 2_000_000_000
            or config.get("Memory") != 2 * 1024 ** 3 or credentials
            or len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != volume_name
            or mounts[0].get("Destination") != "/shared-git" or not mounts[0].get("RW")):
        raise RuntimeError("Initial Docker isolation, image, resource or credential check failed")
    workspace = _workspace_probe(env, task)
    combined = combined_task_text(task)
    if (agent.comm is not None or [message.get("role") for message in agent.messages] != ["system", "user"]
            or agent.n_calls or agent.cost or agent.extra_template_vars.get("task") != combined
            or combined not in agent.messages[1].get("content", "")):
        raise RuntimeError("Initial Solo history/model state is not fresh")
    return {
        "verified": True, "fresh_solo": True, "task": task.as_dict(),
        "base_image_id": image_id, "base_commit": task.base,
        "workspace": workspace, "private_git_volume": volume_name,
        "mounts": [{key: mount.get(key) for key in ("Type", "Name", "Destination", "RW")} for mount in mounts],
        "network_mode": "none", "docker_init": True, "cpus": 2, "memory_bytes": config["Memory"],
        "container_environment_names": sorted(environment_names), "credentials_forwarded": False,
        "checkpoint_loaded": False, "history_restored": False, "peer_evidence_mounted": False,
        "reference_patches_mounted": False, "communication_enabled": False,
        "initial_message_count": 2, "initial_messages": agent.messages,
        "initial_messages_sha256": digest(agent.messages), "initial_model_calls": 0,
        "combined_task_sha256": hashlib.sha256(combined.encode()).hexdigest(),
        "feature_spec_sha256": {str(fid): hashlib.sha256(
            (task.task_dir / f"feature{fid}" / "feature.md").read_bytes()).hexdigest() for fid in task.features},
    }


def make_offline_model(task):
    """Exercise the real sandbox and publication path with zero provider calls."""
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from openai.types.chat import ChatCompletion
    commands = [
        "test \"$(git rev-parse HEAD)\" = " + shlex.quote(task.base)
        + " && test -z \"$(git status --porcelain)\" && test ! -e /patches"
        + " && test ! -e /shared-evidence && test ! -e checkpoint.json"
        + " && test \"$(git remote get-url origin)\" = /shared-git/team.git"
        + " && echo " + ISOLATION_MARKER,
        "printf 'synthetic fresh solo infrastructure check, not a feature solution\\n' > " + OFFLINE_FILE
        + " && git add " + OFFLINE_FILE + " && git commit -m 'Offline fresh solo publication' && git push",
        "gh pr create --title 'Offline fresh solo test' --body 'Synthetic infrastructure verification'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]

    class OfflineModel(LitellmModel):
        def __init__(self):
            super().__init__(model_name="offline-scripted-model", cost_tracking="ignore_errors")
            self.index = 0

        def _query(self, messages, **kwargs):
            if self.index >= len(commands):
                raise RuntimeError("Offline Solo requested an unexpected additional model turn")
            command = commands[self.index]
            self.index += 1
            return ChatCompletion.model_validate({
                "id": f"offline-fresh-solo-{self.index}", "object": "chat.completion", "created": 0,
                "model": "offline-scripted-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "Offline infrastructure check.", "tool_calls": [{
                    "id": f"call_fresh_solo_{self.index}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": command})}}]}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def _calculate_cost(self, response):
            return {"cost": 0.0}

    return OfflineModel()


def phase_worker(run_dir: Path, task: TaskSpec, offline=False, execution_seconds=1200):
    started = time.monotonic()
    if not math.isfinite(execution_seconds) or execution_seconds <= 0:
        raise ValueError("execution_seconds must be finite and positive")
    deadline = started + execution_seconds
    run_dir = Path(run_dir)
    phase_dir = run_dir / PHASE
    phase_dir.mkdir(parents=True, exist_ok=True)
    result = {"phase": PHASE, "status": "initializing", "offline_selftest": offline,
              "fresh_solo": True, "task": task.as_dict(), "active_agent": AGENT,
              "execution_budget_seconds": execution_seconds, "initialization_included_in_budget": True,
              "money_limit": None, "request_limit": None}
    write_json(phase_dir / "status.json", result)
    dc = env = agent = model = None
    volume_created = False
    label = run_dir.name + "-" + PHASE
    volume_name = "rsi-" + label.lower().replace("_", "-")
    exit_code = 2
    cleanup_errors = []
    try:
        configure()
        import docker
        import yaml
        from baseline_runtime import SnapshotDockerEnvironment, create_shared_git_volume
        from baseline_model import make_model
        from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
        from cooperbench.agents.mini_swe_agent_v2.config import get_config_path
        from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
        from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow

        if any((run_dir / name).exists() for name in ("checkpoint.json", "shared_git.tar")):
            raise RuntimeError("Fresh Solo run directory must not contain checkpoint inputs")
        expected = json.loads(task.image_record_path.read_text())
        dc = docker.from_env(timeout=120)
        if (dc.images.get(task.image).id != expected["Id"]
                or expected.get("expected_base") != task.base):
            raise RuntimeError("Task base/image differs from the verified image record")
        if subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
            raise RuntimeError("Pinned CooperBench commit changed")
        subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--",
                        "src/cooperbench/agents/mini_swe_agent_v2", "src/cooperbench/runner/solo.py",
                        str(task.task_dir.relative_to(REPO))], check=True, capture_output=True)
        cfg = yaml.safe_load(get_config_path("solo").read_text())
        create_shared_git_volume(volume_name, expected["Id"], label)
        volume_created = True
        env = SnapshotDockerEnvironment(image=expected["Id"], run_label=label, agent_id=AGENT,
                shared_git_volume=volume_name, evidence_volume=None, container_timeout="40m",
                **cfg.get("environment", {}))
        git = GitConnector(agent_id=AGENT, agents=[AGENT], server_url="/shared-git/team.git")
        git.setup(env)
        if git._base_sha != task.base:
            raise RuntimeError("Native Git submission base is not the original task base")
        guard = BudgetGuard(run_dir / "usage_ledger.json", run_dir.name, model=MODEL,
                            enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        model = make_offline_model(task) if offline else make_model(MODEL, guard, PHASE, AGENT, phase_dir)
        model.config = model.config.model_copy(update={
            key: cfg["model"][key] for key in ("observation_template", "format_error_template")})
        agent = DefaultAgent(model, env, comm=None, agent_id=AGENT,
                **{**cfg["agent"], "step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                   "output_path": phase_dir / "solo.traj.json"})
        initialize_fresh_agent(agent, combined_task_text(task), execution_seconds)
        initial = inspect_initial_state(env, dc, task, volume_name, expected["Id"], agent)
        initial["initialization_seconds"] = round(time.monotonic() - started, 3)
        write_json(phase_dir / "initial_state.json", initial)
        agent.save(agent.config.output_path)
        result.update(status="running", initialization_seconds=initial["initialization_seconds"])
        write_json(phase_dir / "status.json", result)
        reminded, turns, outcome, isolation_passed = False, 0, "deadline", False
        while time.monotonic() < deadline:
            if deadline - time.monotonic() <= 120 and not reminded:
                reminded = True
                agent.add_messages(model.format_message(role="user", content=(
                    "About two minutes remain. Publish your combined implementation, even if incomplete; "
                    "do not claim a failing test passed. Then issue the exact completion command.")))
            try:
                agent.step()
            except InterruptAgentFlow as exc:
                if any(message.get("role") == "exit" for message in exc.messages):
                    close_interrupted_tool_calls(agent)
                agent.add_messages(*exc.messages)
            turns += 1
            agent.save(agent.config.output_path)
            if offline and turns == 1:
                observations = [message for message in agent.messages if message.get("role") == "tool"]
                isolation_passed = bool(observations and observations[-1].get("extra", {}).get("returncode") == 0
                                        and ISOLATION_MARKER in str(observations[-1]))
                if not isolation_passed:
                    raise RuntimeError("Offline fresh Solo isolation probe failed")
            if agent.messages[-1].get("role") == "exit":
                # The native solo connector is absent, so this normally returns
                # False. Do not invent a peer merely to trigger coop PR nudging.
                if agent._nudge_unsubmitted():
                    agent.messages = [message for message in agent.messages if message.get("role") != "exit"]
                    continue
                outcome = agent.messages[-1].get("extra", {}).get("exit_status", "submitted")
                break
        patch = git.submitted_patch(env) or ""
        (phase_dir / "final.patch").write_text(patch)
        publication = env.execute({"command": "git ls-remote origin refs/tags/pr/solo refs/heads/solo"})
        remote_refs = publication.get("output", "") if publication.get("returncode") == 0 else ""
        pr_opened = "refs/tags/pr/solo" in remote_refs
        diagnostic = env.execute({"command": "git diff --binary " + shlex.quote(task.base)})
        (phase_dir / "unsubmitted_diagnostic.patch").write_text(diagnostic.get("output", ""))
        if offline and (not isolation_passed or turns != 4 or outcome != "Submitted" or not pr_opened
                        or f"diff --git a/{OFFLINE_FILE} b/{OFFLINE_FILE}" not in patch
                        or usage(run_dir, PHASE)["requests"] != 0):
            raise RuntimeError("Offline Solo did not complete four synthetic steps with zero API calls")
        result.update(status="completed", outcome=outcome, phase_turns=turns, model_turns=agent.n_calls,
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      final_patch_sha256=hashlib.sha256((phase_dir / "final.patch").read_bytes()).hexdigest(),
                      final_patch_bytes=(phase_dir / "final.patch").stat().st_size,
                      published_pr=pr_opened, published_remote_refs=remote_refs,
                      submission_strategy="pr" if pr_opened else ("native_pushed_branch_fallback" if patch.strip() else "none"),
                      offline_isolation_probe_passed=isolation_passed if offline else None,
                      infrastructure_error=False)
        exit_code = 0
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__, error=safe_error(exc),
                      elapsed_seconds=round(time.monotonic() - started, 3), infrastructure_error=True)
    finally:
        if agent is not None:
            try:
                agent.save(agent.config.output_path)
            except Exception as exc:
                cleanup_errors.append("trajectory save: " + safe_error(exc))
        if model is not None and getattr(model, "close", None):
            try:
                model.close()
            except Exception as exc:
                cleanup_errors.append("model close: " + safe_error(exc))
        if env is not None:
            try:
                env.cleanup()
            except Exception as exc:
                cleanup_errors.append("container cleanup: " + safe_error(exc))
        if dc is not None and volume_created:
            try:
                dc.volumes.get(volume_name).remove()
            except Exception as exc:
                cleanup_errors.append("private volume cleanup: " + safe_error(exc))
        if dc is not None:
            try:
                dc.close()
            except Exception as exc:
                cleanup_errors.append("Docker client close: " + safe_error(exc))
        if cleanup_errors:
            # A completed, hashed submission remains reusable after a cleanup
            # failure. Pause the supervisor without authorizing a paid rerun.
            result.update(infrastructure_error=True, cleanup_errors=cleanup_errors)
            if result.get("status") != "completed":
                result["status"] = "error"
            exit_code = 2
        write_json(phase_dir / "usage_summary.json", usage(run_dir, PHASE))
        write_json(phase_dir / "status.json", result)
    return exit_code
