"""S1 failure census: fresh coop/solo runs, per-agent scoring, cause labels.

Pure functions (plan, collect, diagnose, report) use only the standard library.
`execute` imports CooperBench and must run under its Python 3.12 venv; the CLI
re-launches this module there. See docs/RESEARCH_PLAN.md for the label rules.
"""
from collections import Counter, defaultdict
import csv
import json
import os
from pathlib import Path
import sys

SETTINGS = ("coop", "solo")
# gpt-6-luna list prices per token, verified 2026-10-31 (experiments/m0/luna_accounting.py).
PRICES = {"gpt-6-luna": {"input_cost_per_token": 0.10e-6, "output_cost_per_token": 0.50e-6,
                         "cache_read_input_token_cost": 0.01e-6}}
ROW_COLUMNS = ["setting", "repeat", "repo", "task_id", "features", "success", "merge_status",
               "merged_passed", "own_passed", "cross_passed", "local_passed", "patches_identical",
               "shared_files", "messages_sent", "cost_usd", "feature_labels", "label"]
CAUSES = {
    "integration": "each agent's own patch passes its feature, but the merged result fails",
    "submission": "the agent wrote passing code locally but never published it (PR / push), so the official score misses it",
    "context": "the agent failed its feature, but solo on the same task can do it",
    "capability": "the agent failed its feature, and solo cannot do it either",
}


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ("study", "question", "model", "tasks", "repeats", "output"):
        if key not in config:
            raise ValueError(f"Config is missing '{key}'.")
    if set(config.get("settings", SETTINGS)) - set(SETTINGS):
        raise ValueError("S1 settings are limited to coop and solo.")
    for task in config["tasks"]:
        if len(task.get("features", [])) != 2:
            raise ValueError("Each task needs exactly two features.")
    return config


def plan(config):
    return [{"repeat": r, "setting": s, "repo": t["repo"], "task_id": t["task_id"],
             "features": sorted(t["features"])}
            for r in range(1, config["repeats"] + 1)
            for s in config.get("settings", SETTINGS)
            for t in config["tasks"]]


def agent_errors(job_dir):
    """Agents that crashed (e.g. API errors); CooperBench still writes a normal-looking eval."""
    result = read_json(job_dir / "result.json")
    agents = result.get("agents") or ({"solo": result["agent"]} if "agent" in result else {})
    return sorted(name for name, info in agents.items() if info.get("status") == "Error")


def log_dir(output, job):
    feature_str = "_".join(f"f{f}" for f in job["features"])
    return (Path(output) / "logs" / f"r{job['repeat']}" / job["setting"] / job["repo"]
            / str(job["task_id"]) / feature_str)


# --- execution (CooperBench venv only) -------------------------------------------------

def load_env_file(root):
    """Read OPENAI_API_KEY from a gitignored .env without echoing it."""
    path = root / ".env"
    if os.environ.get("OPENAI_API_KEY") or not path.is_file():
        return
    for line in path.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "OPENAI_API_KEY" and value.strip():
            os.environ["OPENAI_API_KEY"] = value.strip().strip("'\"")


def prepare_environment(root):
    load_env_file(root)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env or export it. No request was made.")
    socket = Path.home() / ".docker/run/docker.sock"
    if "DOCKER_HOST" not in os.environ and socket.exists():
        os.environ["DOCKER_HOST"] = f"unix://{socket}"
    # The agent retries every non-auth error 10 times by default, including permanent 400s.
    os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "4")
    docker_bin = "/Applications/Docker.app/Contents/Resources/bin"
    if Path(docker_bin).is_dir():
        os.environ["PATH"] = docker_bin + os.pathsep + os.environ["PATH"]
    import litellm
    for name, price in PRICES.items():
        litellm.register_model({f"openai/{name}": {**price, "litellm_provider": "openai", "mode": "chat"},
                                name: {**price, "litellm_provider": "openai", "mode": "chat"}})


CURRENT_JOB_DIR = None  # set per job; the capture hook writes local work there


def capture_local_work():
    """Save each agent's local diff against the base commit before CooperBench reads its PR.

    CooperBench grades only published work (PR or pushed branch). Keeping the local diff
    lets the analysis tell "wrote working code but never published" from "could not write it".
    """
    from cooperbench.agents._coop.runtime import normalize_patch
    from cooperbench.agents.mini_swe_agent_v2.connectors.git import GitConnector
    if getattr(GitConnector, "_s1_capture", False):
        return
    original = GitConnector.submitted_patch

    def submitted_patch(self, env):
        target = CURRENT_JOB_DIR
        if target is not None and self._base_sha:
            try:
                diff = self._exec(env, "git add -A >/dev/null 2>&1; git --no-pager diff --cached --no-color "
                                       f"{self._base_sha} -- . ':(exclude)*.pyc' ':(exclude)**/__pycache__/**'")
                target.mkdir(parents=True, exist_ok=True)
                (target / f"local_{self.agent_id}.patch").write_text(normalize_patch(diff.get("output", "") or ""))
            except Exception as exc:
                print(f"local work capture failed for {self.agent_id}: {exc}", flush=True)
        return original(self, env)

    GitConnector.submitted_patch = submitted_patch
    GitConnector._s1_capture = True


def api_messages(messages):
    """Replace internal `exit` messages with something the OpenAI API accepts.

    When a coop agent ends without a PR, CooperBench nudges it to keep going but leaves its
    `exit` message in the history. OpenAI rejects that role, so the next call failed and the
    run was lost as an agent error. The exit answers the assistant's last tool calls, so it
    becomes their tool results; an exit that follows anything else is dropped.
    """
    prepared = []
    for message in messages:
        if message.get("role") != "exit":
            prepared.append(message)
            continue
        previous = prepared[-1] if prepared else {}
        calls = (previous.get("tool_calls") or []) if previous.get("role") == "assistant" else []
        answered = {m.get("tool_call_id") for m in prepared if m.get("role") == "tool"}
        for call in calls:
            if call.get("id") not in answered:
                prepared.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": message.get("content") or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})
    return prepared


def accept_nudged_agents():
    """Apply `api_messages` to every request mini_swe_agent_v2 sends."""
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    if getattr(LitellmModel, "_s1_exit_fix", False):
        return
    original = LitellmModel._prepare_messages_for_api

    def prepare(self, messages):
        return original(self, api_messages(messages))

    LitellmModel._prepare_messages_for_api = prepare
    LitellmModel._s1_exit_fix = True


def keep_image_path():
    """Run agent commands in a non-login shell so the image's PATH survives.

    CooperBench runs every command with `bash -lc`. The login shell reloads /etc/profile,
    which resets PATH and drops toolchains installed via Docker ENV: Go in the go_chi
    images and cargo in the typst image. Agents there could not build or test at all.
    For the other images the two shells give the same PATH and variables.
    """
    from cooperbench.agents.mini_swe_agent_v2.environments.docker import DockerEnvironment
    if getattr(DockerEnvironment, "_s1_path_fix", False):
        return
    original = DockerEnvironment.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if list(self.config.interpreter) == ["bash", "-lc"]:
            self.config.interpreter = ["bash", "-c"]

    DockerEnvironment.__init__ = init
    DockerEnvironment._s1_path_fix = True


def agent_features(job_dir):
    """Map CooperBench agent ids (agent1, agent2, solo) to their feature ids."""
    result = read_json(job_dir / "result.json")
    if "agents" in result:
        return {name: str(info.get("feature_id")) for name, info in result["agents"].items()}
    return {"solo": "solo"}


def score_local(job_dir, job, dataset_dir):
    """Score local work wherever it differs from what was published."""
    target = job_dir / "local_eval.json"
    if target.is_file():
        return
    from cooperbench.eval.sandbox import test_solo
    f1, f2 = job["features"]
    scores = {}
    for agent_id, feature in agent_features(job_dir).items():
        local = job_dir / f"local_{agent_id}.patch"
        published = job_dir / ("solo.patch" if agent_id == "solo" else f"agent{feature}.patch")
        if not local.is_file():
            continue
        text = local.read_text()
        if published.is_file() and published.read_text().strip() == text.strip():
            scores[feature] = {"same_as_published": True}
            continue
        result = test_solo(repo_name=job["repo"], task_id=job["task_id"], feature1_id=f1, feature2_id=f2,
                           patch=text, backend="docker", dataset_dir=dataset_dir)
        scores[feature] = {str(f1): bool(result.get("feature1", {}).get("passed")),
                           str(f2): bool(result.get("feature2", {}).get("passed")),
                           "error": result.get("error")}
    target.write_text(json.dumps(scores, indent=2))


def score_agents(job_dir, job, dataset_dir):
    """Score each coop agent's own patch alone against both features."""
    target = job_dir / "agent_eval.json"
    if target.is_file():
        return
    from cooperbench.eval.sandbox import test_solo
    f1, f2 = job["features"]
    scores = {}
    for fid in (f1, f2):
        patch = job_dir / f"agent{fid}.patch"
        result = test_solo(repo_name=job["repo"], task_id=job["task_id"], feature1_id=f1,
                           feature2_id=f2, patch=patch.read_text() if patch.is_file() else "",
                           backend="docker", dataset_dir=dataset_dir)
        scores[str(fid)] = {str(f1): bool(result.get("feature1", {}).get("passed")),
                            str(f2): bool(result.get("feature2", {}).get("passed")),
                            "error": result.get("error")}
    target.write_text(json.dumps(scores, indent=2))


def require_redis(url):
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(url)
    try:
        socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), timeout=2).close()
    except OSError:
        raise RuntimeError(f"Redis is not reachable at {url}; coop messaging needs it. "
                           "Start one with: docker run -d --name cooperbench-redis -p 6379:6379 redis:7")


def execute(config, root, shard=(0, 1)):
    """Run this worker's share of missing jobs; worker k of n takes every n-th job."""
    prepare_environment(root)
    if "coop" in config.get("settings", SETTINGS) and config.get("messaging", True):
        require_redis(config.get("redis", "redis://localhost:6379"))
    from cooperbench import run
    capture_local_work()
    accept_nudged_agents()
    keep_image_path()
    global CURRENT_JOB_DIR
    output = root / config["output"]
    dataset_dir = str(root / "experiments/m0/CooperBench/dataset")
    agent_config = str(root / config["agent_config"]) if config.get("agent_config") else None
    output.mkdir(parents=True, exist_ok=True)
    if shard[0] == 0:
        (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    for job in plan(config)[shard[0]::shard[1]]:
        job_dir = log_dir(output, job)
        name = f"r{job['repeat']} {job['setting']} {job['repo']}/{job['task_id']} {job['features']}"
        try:
            if (job_dir / "eval.json").is_file() and agent_errors(job_dir):
                # Keep the failed attempt for accounting, then retry once in this invocation.
                attempt = 1
                while job_dir.with_name(f"{job_dir.name}.error{attempt}").exists():
                    attempt += 1
                job_dir.rename(job_dir.with_name(f"{job_dir.name}.error{attempt}"))
                print(f"retry {name} after agent error", flush=True)
            if not (job_dir / "eval.json").is_file():
                print(f"run {name}", flush=True)
                CURRENT_JOB_DIR = job_dir
                run(run_name=f"r{job['repeat']}", repo=job["repo"], task_id=job["task_id"],
                    features=job["features"], model_name=f"openai/{config['model']}",
                    agent=config.get("agent", "mini_swe_agent_v2"), setting=job["setting"],
                    backend="docker", concurrency=1, auto_eval=True,
                    messaging_enabled=config.get("messaging", True), git_enabled=config.get("git", False),
                    redis_url=config.get("redis", "redis://localhost:6379"), agent_config=agent_config,
                    dataset_dir=dataset_dir, logs_dir=str(output / "logs"))
            CURRENT_JOB_DIR = None
            if (job_dir / "eval.json").is_file() and not agent_errors(job_dir):
                if job["setting"] == "coop":
                    score_agents(job_dir, job, dataset_dir)
                if not read_json(job_dir / "eval.json").get("both_passed"):
                    score_local(job_dir, job, dataset_dir)
        except Exception as exc:  # one broken job must not stop the rest of the shard
            print(f"job failed: {name}: {type(exc).__name__}: {exc}", flush=True)
    if shard[1] == 1:
        analyze(config, output)


def analyze(config, output):
    rows = diagnose(collect(config, output))
    write_outputs(config, output, rows)
    report(rows)
    return rows


# --- analysis (standard library only) --------------------------------------------------

def patch_files(text):
    return {line.split(" b/", 1)[1] for line in text.splitlines()
            if line.startswith("diff --git ") and " b/" in line}


def read_json(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def collect(config, output):
    rows = []
    for job in plan(config):
        job_dir = log_dir(output, job)
        evaluation = read_json(job_dir / "eval.json")
        if not evaluation:
            continue
        result = read_json(job_dir / "result.json")
        f1, f2 = (str(f) for f in job["features"])
        merged = {f1: bool(evaluation.get("feature1", {}).get("passed")),
                  f2: bool(evaluation.get("feature2", {}).get("passed"))}
        row = {**job, "features": f"{f1} {f2}", "success": bool(evaluation.get("both_passed")),
               "merge_status": (evaluation.get("merge") or {}).get("status", ""),
               "merged_passed": " ".join(f for f in (f1, f2) if merged[f]),
               "eval_error": evaluation.get("error"), "cost_usd": result.get("total_cost"),
               "messages_sent": result.get("messages_sent", ""), "own_passed": "",
               "cross_passed": "", "local_passed": "", "patches_identical": "", "shared_files": "",
               "agents_scored": False,
               "agent_errors": " ".join(agent_errors(job_dir))}
        row["local_passed"] = local_passed(job_dir, job, row)
        if job["setting"] == "coop":
            agents = read_json(job_dir / "agent_eval.json")
            if agents:
                row["agents_scored"] = True
                row["own_passed"] = " ".join(f for f in (f1, f2) if agents[f][f])
                row["cross_passed"] = " ".join(f for f, g in ((f1, f2), (f2, f1)) if agents[f][g])
            patches = [(job_dir / f"agent{f}.patch") for f in (f1, f2)]
            if all(p.is_file() for p in patches):
                texts = [p.read_text() for p in patches]
                row["patches_identical"] = bool(texts[0].strip()) and texts[0] == texts[1]
                row["shared_files"] = len(patch_files(texts[0]) & patch_files(texts[1]))
        rows.append(row)
    return rows


def local_passed(job_dir, job, row):
    """Features the agents' local work passes: own feature per coop agent, both for solo."""
    scores = read_json(job_dir / "local_eval.json")
    passed = []
    for feature, score in scores.items():
        if score.get("same_as_published"):
            continue
        if feature == "solo":
            passed += [f for f in (str(x) for x in job["features"]) if score.get(f)]
        elif score.get(feature):
            passed.append(feature)
    return " ".join(sorted(passed))


def solo_rates(rows):
    counts = defaultdict(lambda: [0, 0])
    for row in rows:
        if row["setting"] != "solo" or row.get("eval_error") or row.get("agent_errors"):
            continue
        for feature in row["features"].split():
            cell = counts[row["repo"], row["task_id"], feature]
            # Capability means the model can write the feature, published or not.
            cell[0] += feature in row["merged_passed"].split() + row.get("local_passed", "").split()
            cell[1] += 1
    return {key: passed / total for key, (passed, total) in counts.items()}


def diagnose(rows, solo_threshold=0.5):
    """Label each run; for coop, integration > submission > context > capability."""
    rates = solo_rates(rows)
    for row in rows:
        row["feature_labels"] = ""
        if row.get("eval_error") or row.get("agent_errors"):
            row["label"] = "infra_error"
        elif row["success"]:
            row["label"] = "passed"
        elif row["setting"] == "solo":
            written = set(row["merged_passed"].split() + row.get("local_passed", "").split())
            row["label"] = "submission" if written >= set(row["features"].split()) else "solo_failed"
        elif not row["agents_scored"]:
            row["label"] = "unscored"
        else:
            labels = {}
            merged = row["merged_passed"].split()
            own = row["own_passed"].split()
            local = row.get("local_passed", "").split()
            for feature in row["features"].split():
                if feature in merged:
                    continue
                if feature in own:
                    labels[feature] = "integration"
                elif feature in local:
                    labels[feature] = "submission"
                else:
                    rate = rates.get((row["repo"], row["task_id"], feature))
                    labels[feature] = ("unknown" if rate is None else
                                       "context" if rate >= solo_threshold else "capability")
            row["feature_labels"] = " ".join(f"{f}:{c}" for f, c in labels.items())
            found = set(labels.values())
            row["label"] = next((c for c in ("integration", "submission", "context", "capability", "unknown")
                                 if c in found), "unknown")
    return rows


def write_outputs(config, output, rows):
    with open(output / "rows.csv", "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    labels = Counter(r["label"] for r in rows if r["setting"] == "coop")
    summary = {"study": config["study"], "question": config["question"], "model": config["model"],
               "runs": len(rows), "coop_labels": dict(labels),
               "known_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6)}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))


def report(rows):
    for setting in SETTINGS:
        subset = [r for r in rows if r["setting"] == setting]
        if subset:
            print(f"{setting}: {sum(r['success'] for r in subset)}/{len(subset)} passed")
    solo_unpublished = sum(r["label"] == "submission" for r in rows if r["setting"] == "solo")
    if solo_unpublished:
        print(f"  ({solo_unpublished} solo runs wrote passing code but never published it)")
    labels = Counter(r["label"] for r in rows if r["setting"] == "coop")
    failures = sum(n for name, n in labels.items() if name != "passed")
    print(f"\nCoop failure causes ({failures} failed runs)")
    for name, meaning in CAUSES.items():
        print(f"  {name:12} {labels[name]:4}  {meaning}")
    for name in sorted(set(labels) - set(CAUSES) - {"passed"}):
        print(f"  {name:12} {labels[name]:4}")
    flags = [r for r in rows if r["setting"] == "coop" and (r["patches_identical"] is True or r["cross_passed"])]
    print(f"\nCoordination flags (identical patches or an agent passing its partner's feature): {len(flags)} runs")
    cost = sum(r["cost_usd"] or 0 for r in rows)
    print(f"Known model cost: ${cost:.4f}")


def screen(config, rows, min_runs=2):
    """Apply the screening rule: keep pairs whose valid runs vary in outcome."""
    by_pair = defaultdict(list)
    for row in rows:
        if row["label"] != "infra_error":
            by_pair[row["repo"], row["task_id"]].append(row)
    kept, dropped = [], []
    for task in config["tasks"]:
        runs = [r for r in by_pair[task["repo"], task["task_id"]]
                if r["features"] == " ".join(str(f) for f in sorted(task["features"]))]
        some_feature = any(r["merged_passed"] or r.get("local_passed") for r in runs)
        all_passed = bool(runs) and all(r["success"] for r in runs)
        reason = ("too few valid runs" if len(runs) < min_runs else
                  "never passed a feature" if not some_feature else
                  "always passed" if all_passed else None)
        (dropped if reason else kept).append({**task, **({"reason": reason} if reason else {})})
    return kept, dropped


def main_config(screen_config, kept, repeats=3):
    return {**{k: v for k, v in screen_config.items() if k not in ("decision_rule", "sampling")},
            "study": "s1_main",
            "question": "What share of coop failures is integration, context, or capability?",
            "screened_from": screen_config["study"],
            "repeats": repeats, "tasks": [{k: t[k] for k in ("repo", "task_id", "features")} for t in kept],
            "output": "runs/s1_main"}


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    shard = (0, 1)
    if len(argv) == 3 and argv[1] == "--shard":
        index, count = (int(v) for v in argv[2].split("/"))
        shard = (index, count)
    execute(config, root, shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
