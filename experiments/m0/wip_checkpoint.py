"""Observe source progress at completed tool boundaries, without running tests.

The first paired round with semantic Python changes by both unfinished agents
is eligible. Comments, formatting, test files, and output logs cannot trigger it.
Syntax-invalid source is still work in progress; its validity is not a gate.
"""
import json
import shlex
import time
from concurrent.futures import ThreadPoolExecutor


def code_progress(env, task):
    # Runs inside the agent's existing sandbox. Git reads committed, staged,
    # unstaged and untracked source against the original benchmark base.
    script = r'''
import ast, hashlib, json, subprocess
from pathlib import Path
config = json.loads(CONFIG)
def git(*args):
    return subprocess.check_output(['git', *args])
names = set(git('diff', '--name-only', '-z', config['base'], '--', *config['code_roots']).decode().split('\0'))
untracked = set(git('ls-files', '--others', '--exclude-standard', '-z', '--', *config['code_roots']).decode().split('\0'))
names.update(untracked)
def signature(data):
    try:
        return ast.dump(ast.parse(data), include_attributes=False)
    except (SyntaxError, UnicodeError, ValueError):
        return 'invalid-python:' + hashlib.sha256(data).hexdigest()
changed = []
for name in sorted(names):
    if not name.endswith('.py') or any(p in ('tests', '__pycache__') for p in Path(name).parts):
        continue
    path = Path(name)
    if path.is_symlink():
        continue
    before = subprocess.run(['git','show',config['base']+':'+name],capture_output=True)
    old = before.stdout if before.returncode == 0 else b''
    new = path.read_bytes() if path.is_file() else b''
    if old == new or signature(old) == signature(new):
        continue
    changed.append({'path':name,'before_sha256':hashlib.sha256(old).hexdigest(),
                    'after_sha256':hashlib.sha256(new).hexdigest(),
                    'untracked':name in untracked,'deleted':not path.exists()})
print(json.dumps({'changed_files':changed,'has_code_change':bool(changed),
                  'head':git('rev-parse','HEAD').decode().strip(),
                  'git_status':git('status','--porcelain=v1').decode()}))
'''.replace("CONFIG", repr(json.dumps(task.as_dict())))
    result = env.execute({"command": "python -c " + shlex.quote(script)})
    if result.get("returncode") != 0:
        raise RuntimeError("Cannot inspect source progress: " + str(result.get("output", ""))[-500:])
    value = json.loads(result["output"].strip())
    if set(value) != {"changed_files", "has_code_change", "head", "git_status"}:
        raise ValueError("Invalid source progress observation")
    return value


def checkpoint_eligible(progress, outcomes):
    agents = {"agent1", "agent2"}
    if set(progress) != agents or set(outcomes) != agents:
        return False
    return all(progress[a].get("has_code_change") and progress[a].get("changed_files")
               and not outcomes[a].get("submitted") and not outcomes[a].get("exited")
               and not outcomes[a].get("infrastructure_error") for a in agents)


def validate_checkpoint(checkpoint, task):
    if checkpoint.get("task") != task.as_dict():
        raise ValueError("Checkpoint belongs to a different task")
    evidence = checkpoint.get("wip_checkpoint", {})
    if not checkpoint_eligible(evidence.get("progress", {}), evidence.get("outcomes", {})):
        raise ValueError("Checkpoint requires source changes by both unfinished agents")
    if set(checkpoint.get("agents", {})) != set(task.agents):
        raise ValueError("Checkpoint agent identities differ")
    for agent_id, feature in task.agents.items():
        saved = checkpoint["agents"][agent_id]
        expected = (task.task_dir / f"feature{feature}/feature.md").read_text()
        if saved.get("git_base") != task.base or saved.get("state", {}).get("extra_template_vars", {}).get("task") != expected:
            raise ValueError("Checkpoint base or feature instruction differs")
    if checkpoint.get("broker", {}).get("blocked") or checkpoint.get("broker", {}).get("exited"):
        raise ValueError("Checkpoint has active waits or departed agents")


def drive_prefix(agents, envs, task, phase_dir, seconds=1200):
    """Run paired steps and stop at the first eligible completed round.

    A completed submission or the fixed time horizon ends prefix collection.
    No test result is consulted, and an ineligible prefix is not resampled.
    """
    from budget_guard import write_json
    from run_baselines import close_interrupted_tool_calls, safe_error
    from cooperbench.agents.mini_swe_agent_v2.exceptions import InterruptAgentFlow

    def one_step(agent_id):
        agent = agents[agent_id]
        try:
            try:
                agent.step()
            except InterruptAgentFlow as exc:
                if any(m.get("role") == "exit" for m in exc.messages):
                    close_interrupted_tool_calls(agent)
                agent.add_messages(*exc.messages)
            return {"exited": any(m.get("role") == "exit" for m in agent.messages),
                    "submitted": bool(agent._opened_pr()), "infrastructure_error": False}
        except Exception as exc:
            return {"infrastructure_error": True, "error": safe_error(exc)}
        finally:
            agent.save(agent.config.output_path)

    deadline, rounds, evidence = time.monotonic() + seconds, 0, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        while time.monotonic() < deadline:
            futures = {a: pool.submit(one_step, a) for a in agents}
            outcomes = {a: f.result() for a, f in futures.items()}
            rounds += 1
            if any(o.get("infrastructure_error") for o in outcomes.values()):
                raise RuntimeError("Prefix infrastructure failure: " + json.dumps(outcomes))
            progress = {a: code_progress(envs[a], task) for a in agents}
            eligible = checkpoint_eligible(progress, outcomes)
            evidence = {"rule": "first_paired_round_both_source_changes_and_unfinished_v1",
                        "paired_round": rounds, "progress": progress, "outcomes": outcomes,
                        "eligible": eligible, "selection_uses_test_results": False,
                        "time_horizon_seconds": seconds}
            write_json(phase_dir / f"progress_round_{rounds:03d}.json", evidence)
            write_json(phase_dir / "progress.json", evidence)
            if eligible or any(o["exited"] or o["submitted"] for o in outcomes.values()):
                break
    return {a: {"status": "checkpoint" if evidence.get("eligible") else "checkpoint_not_reached",
                "phase_turns": rounds, "model_turns": agents[a].n_calls,
                "infrastructure_error": False} for a in agents}, evidence
