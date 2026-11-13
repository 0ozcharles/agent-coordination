"""ParallACT tasks in the CooperBench coop runner.

ParallACT (Mediratta et al. 2026, arXiv 2611.00314) gives two coding agents separate requests on one
repository. In its contradictory instances the two requests change the same code in incompatible ways,
and a reconciled implementation that satisfies both is known to exist. Each instance here becomes a
two-feature CooperBench task: feature 1 is request A, feature 2 is request B. The agents are CooperBench's
coop agents (mini_swe_agent_v2 with Redis messaging), or in the solo setting one agent given both requests;
they run in the instance's SWE-Bench Pro image, where the repository at /app is reset to the base commit and
linked to /workspace/repo.

Scoring follows ParallACT's grader, loaded from the pinned checkout (its eval/runner.py): each agent's
patch against its own request's tests, and the merged patch against the combined tests, which hold both
requests' tests. The two patches are merged with git in the instance image; conflicted files are resolved
with the merge test's llm method (merge.resolve). Agent-written test files are dropped before scoring, as
CooperBench does.

A config may define arms. Each arm runs every sampled instance; its instruction, if it has one, is given
to both agents as a user message before their first model call, so it applies from the start of the run.

`execute` and `gold` import CooperBench and run under its venv with Docker; `report` uses only the
standard library.
"""
from collections import Counter
import csv
import importlib.util
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import merge, s1

FEATURES = (1, 2)  # feature 1 = request A, feature 2 = request B
SIDES = {1: "A", 2: "B"}

# Run once in each agent container, before CooperBench's git setup.
SETUP = """set -e
cd /app
git reset --hard -q {base}
git clean -fdq
cd /
rmdir /workspace/repo 2>/dev/null || true
mkdir -p /workspace
[ -e /workspace/repo ] || ln -s /app /workspace/repo
test "$(cd /workspace/repo && git rev-parse HEAD)" = "{base}"
"""

# Merge the two agents' patches on the base commit in the instance image. With /m/resolved present,
# its files replace the conflicted files and the merge is committed.
MERGE = r"""set -u
cd /app
BASE="$1"
git reset --hard -q "$BASE" && git clean -fdq
git config user.email merge@local; git config user.name merge; git config merge.conflictstyle diff3
git checkout -q -B side_a "$BASE"
[ ! -s /m/a.patch ] || git apply --index /m/a.patch 2>>/m/apply.err || echo A >> /m/apply_failed
git commit -qm a --allow-empty --no-verify
git checkout -q -B side_b "$BASE"
[ ! -s /m/b.patch ] || git apply --index /m/b.patch 2>>/m/apply.err || echo B >> /m/apply_failed
git commit -qm b --allow-empty --no-verify
git checkout -q side_a
if git merge -q --no-edit side_b > /m/merge.log 2>&1; then
  echo clean > /m/status
else
  echo conflict > /m/status
  git diff --name-only --diff-filter=U > /m/conflicts.txt
  while read -r f; do
    [ -f "$f" ] || continue
    mkdir -p "/m/conflicted/$(dirname "$f")" && cp "$f" "/m/conflicted/$f"
  done < /m/conflicts.txt
  if [ -d /m/resolved ]; then
    MISSING=0
    while read -r f; do [ -f "/m/resolved/$f" ] || MISSING=1; done < /m/conflicts.txt
    if [ "$MISSING" = 0 ]; then
      while read -r f; do cp "/m/resolved/$f" "$f"; done < /m/conflicts.txt
      git add -A && git commit -qm merged --no-verify && echo resolved > /m/status
    fi
  fi
fi
git diff "$BASE" HEAD > /m/merged.patch
"""


def load_config(path):
    """A config may extend another file in the same folder; its own keys replace the base's."""
    config = json.loads(Path(path).read_text())
    if "extends" in config:
        config = {**load_config(Path(path).parent / config["extends"]), **config}
    return config


def source_dir(config, root):
    return root / config["source"]["path"]


def all_instances(config, root):
    """Every instance in the checkout, keyed by instance id, with its corpus folder."""
    found = {}
    for path in sorted((source_dir(config, root) / "dataset").glob("**/instance_*.json")):
        data = json.loads(path.read_text())
        data["_corpus"] = str(path.parent.parent.relative_to(source_dir(config, root) / "dataset"))
        found[data["instance_id"]] = data
    if not found:
        raise FileNotFoundError(f"No ParallACT instances under {source_dir(config, root)}; see docs/DEVELOPMENT.md.")
    return found


def repo_name(instance):
    """CooperBench repo name for an instance, e.g. parallact_nodebb."""
    return "parallact_" + instance["repo"].split("/")[-1].lower().replace("-", "_").replace(".", "_")


def task_ids(instances):
    """A stable integer per instance: its position among all instance ids at the pinned commit."""
    return {iid: n for n, iid in enumerate(sorted(instances))}


def select(config, root):
    """The sampled instances, in a fixed order: an explicit list, or a draw from each corpus that can
    leave out the instances another config samples."""
    instances = all_instances(config, root)
    family = [i for i in instances.values() if i["family"] == config["family"]]
    sample = config["sample"]
    if "instances" in sample:
        return [instances[i] for i in sample["instances"]]
    excluded = ({i["instance_id"] for i in select(load_config(root / sample["exclude"]), root)}
                if sample.get("exclude") else set())
    chosen = []
    for corpus, count in sample["corpora"].items():
        pool = sorted((i for i in family if i["_corpus"] == corpus and i["instance_id"] not in excluded),
                      key=lambda i: i["instance_id"])
        if count != "all":
            pool = random.Random(sample["seed"]).sample(pool, count)
        chosen.extend(pool)
    return chosen


def plan(config, root):
    """One job per repeat, instance and arm; the arms of an instance are adjacent, so a partial run stays balanced."""
    ids = task_ids(all_instances(config, root))
    arms = [{"arm": a} for a in config["arms"]] if config.get("arms") else [{}]
    return [{"instance": i["instance_id"], "repo": repo_name(i), "task_id": ids[i["instance_id"]],
             "repeat": r, "features": list(FEATURES), "setting": config.get("setting", "coop"), **arm}
            for r in range(1, config.get("repeats", 1) + 1) for i in select(config, root) for arm in arms]


def logs_dir(output, job):
    return Path(output) / job["arm"] / "logs" if job.get("arm") else Path(output) / "logs"


def job_dir(output, job):
    return (logs_dir(output, job) / f"r{job['repeat']}" / job.get("setting", "coop") / job["repo"]
            / str(job["task_id"]) / "f1_f2")


def instruction(config, arm, agent):
    """The arm's instruction for one agent (instruction_agent1/2 override the shared one), or None."""
    spec = config["arms"][arm] if arm else {}
    text = spec.get(f"instruction_{agent}", spec.get("instruction"))
    return config.get("instruction_prefix", "") + text if text else None


def write_dataset(output, instance, job):
    """CooperBench reads each agent's task from dataset/<repo>/task<id>/feature<n>/feature.md. The reference
    patch goes next to it, as in CooperBench's dataset; it stays on the host, where agents cannot read it."""
    task = Path(output) / "dataset" / job["repo"] / f"task{job['task_id']}"
    for feature in FEATURES:
        target = task / f"feature{feature}" / "feature.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(instance[f"agent{SIDES[feature]}_problem_statement"].strip() + "\n")
        target.with_name("feature.patch").write_text(instance.get(f"agent{SIDES[feature]}_solution") or "")
    return Path(output) / "dataset"


def load_runner(config, root):
    """ParallACT's Docker test runner from the pinned checkout (it imports nothing from its package)."""
    path = source_dir(config, root) / "eval" / "runner.py"
    spec = importlib.util.spec_from_file_location("parallact_eval_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_source(config, root):
    head = subprocess.run(["git", "-C", str(source_dir(config, root)), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head != config["source"]["commit"]:
        raise RuntimeError(f"ParallACT checkout is at {head or 'nothing'}, the config pins "
                           f"{config['source']['commit']}; see docs/DEVELOPMENT.md.")


# --- agents (CooperBench venv) ---------------------------------------------------------

def install_hooks(images):
    """Point CooperBench at each instance's image and set the repository up in every agent container.

    `images` maps (repo, task_id) to {"image", "base"}. Images are linux/amd64, so Docker runs them
    under emulation on Apple silicon.
    """
    import cooperbench.runner.coop as coop
    import cooperbench.runner.solo as solo
    from cooperbench.agents.mini_swe_agent_v2.environments.docker import DockerEnvironment
    os.environ["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
    bases = {v["image"]: v["base"] for v in images.values()}
    for module in (coop, solo):  # each imported get_image_name by name
        if not getattr(module, "_parallact_images", None):
            original_name = module.get_image_name

            def image_name(repo, task_id, module=module, original_name=original_name):
                entry = module._parallact_images.get((repo, int(task_id)))
                return entry["image"] if entry else original_name(repo, task_id)

            module.get_image_name = image_name
        module._parallact_images = images
    if getattr(DockerEnvironment, "_parallact_setup", False):
        DockerEnvironment._parallact_bases = bases
        return
    original_start = DockerEnvironment._start_container

    def start(self):
        original_start(self)
        base = DockerEnvironment._parallact_bases.get(self.config.image)
        if base:
            out = self.execute({"command": SETUP.format(base=shlex.quote(base))}, cwd="/", timeout=900)
            if out.get("returncode") != 0:
                raise RuntimeError(f"repository setup failed in {self.config.image}: {out.get('output', '')[-1500:]}")

    DockerEnvironment._start_container = start
    DockerEnvironment._parallact_bases = bases
    DockerEnvironment._parallact_setup = True


GUIDANCE = {}  # agent id -> instruction for the job this process is running


def install_guidance():
    """Give each agent its arm's instruction as a user message just before its first model call."""
    from cooperbench.agents.mini_swe_agent_v2.agents.default import DefaultAgent
    if getattr(DefaultAgent, "_parallact_guidance", False):
        return
    original_query = DefaultAgent.query

    def query(self):
        text = GUIDANCE.get(self.agent_id)
        if text and self.n_calls == 0 and not getattr(self, "_guided", False):
            self._guided = True
            self.add_messages(self.model.format_message(role="user", content=text))
        return original_query(self)

    DefaultAgent.query = query
    DefaultAgent._parallact_guidance = True


def guidance_given(target, texts):
    """Whether each agent's history has its instruction before its first reply (None: the arm has none)."""
    from . import conflict_states as cs
    if not any(texts.values()):
        return None
    for agent, text in texts.items():
        full, path = Path(target) / f"{agent}_full_traj.json", Path(target) / f"agent{agent[-1]}_traj.json"
        msgs = cs.history(full)[0] if full.is_file() else read_json(path).get("messages", [])
        first_reply = next((n for n, m in enumerate(msgs) if m.get("role") == "assistant"), len(msgs))
        if text and not any(m.get("role") == "user" and m.get("content") == text for m in msgs[:first_reply]):
            return False
    return True


def ensure_image(image, timeout=3600):
    """Pull the image first: CooperBench starts agent containers with a 120 s limit, too short for a pull."""
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        subprocess.run(["docker", "pull", "-q", "--platform", "linux/amd64", image],
                       capture_output=True, text=True, timeout=timeout, check=True)


def run_agents(config, root, job, output, instance):
    from cooperbench.runner.coop import execute_coop
    from cooperbench.runner.solo import execute_solo
    ensure_image(instance["runtime"]["image_uri"])
    dataset = write_dataset(output, instance, job)
    agent_config = str(root / config["agent_config"]) if config.get("agent_config") else None
    s1.CURRENT_JOB_DIR = job_dir(output, job)
    logs = str(logs_dir(output, job))
    try:
        if job.get("setting") == "solo":  # one agent; CooperBench joins the two requests as Feature 1 and 2
            execute_solo(repo_name=job["repo"], task_id=job["task_id"], features=job["features"],
                         run_name=f"r{job['repeat']}", agent_name=config.get("agent", "mini_swe_agent_v2"),
                         model_name=f"openai/{config['model']}", force=True, quiet=True, backend="docker",
                         agent_config=agent_config, dataset_dir=str(dataset), logs_dir=logs)
            return
        texts = {agent: instruction(config, job.get("arm"), agent) for agent in ("agent1", "agent2")}
        GUIDANCE.clear()
        GUIDANCE.update({a: t for a, t in texts.items() if t})
        execute_coop(repo_name=job["repo"], task_id=job["task_id"], features=job["features"],
                     run_name=f"r{job['repeat']}", agent_name=config.get("agent", "mini_swe_agent_v2"),
                     model_name=f"openai/{config['model']}", redis_url=config.get("redis", "redis://localhost:6379"),
                     force=True, quiet=True, git_enabled=False, messaging_enabled=True, backend="docker",
                     agent_config=agent_config, dataset_dir=str(dataset), logs_dir=logs)
        if job.get("arm"):
            (job_dir(output, job) / "arm.json").write_text(json.dumps({"arm": job["arm"], "instructions": texts}, indent=2))
    finally:
        GUIDANCE.clear()
        s1.CURRENT_JOB_DIR = None


def agent_errors(target):
    result = json.loads((target / "result.json").read_text()) if (target / "result.json").is_file() else {}
    agents = result.get("agents") or ({"solo": result["agent"]} if "agent" in result else {})
    return [a for a, info in agents.items() if info.get("status") == "Error"]


# --- scoring (CooperBench venv for the test-file filter and the llm merge) -------------

def clean_patch(text):
    from cooperbench.eval.sandbox import _filter_test_files
    return _filter_test_files(text or "") or ""


def docker_run(image, script, mount, args=(), timeout=1800):
    command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "bash",
               "-v", f"{mount}:/m", image, "/m/script.sh", *args]
    (Path(mount) / "script.sh").write_text(script)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def merge_patches(config, root, instance, patches, target):
    """Merge two patches on the base commit with git; resolve conflicts with the llm method.

    Returns {"status": clean|resolved|unresolved|apply_failed, "patch": merged diff or None, ...}.
    """
    record = {}
    scratch = Path(target) / "merge_work"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    (scratch / "a.patch").write_text(patches[0] if patches[0].strip() else "")  # empty: that side changes nothing
    (scratch / "b.patch").write_text(patches[1] if patches[1].strip() else "")
    image, base = instance["runtime"]["image_uri"], instance["base_commit"]
    docker_run(image, MERGE, scratch, [base])
    status = (scratch / "status").read_text().strip() if (scratch / "status").is_file() else "error"
    if (scratch / "apply_failed").is_file():
        record.update(status="apply_failed", sides=(scratch / "apply_failed").read_text().split())
        return record
    if status == "conflict":
        paths = [p for p in (scratch / "conflicts.txt").read_text().split("\n") if p]
        files = {p: (scratch / "conflicted" / p).read_text(errors="replace")
                 for p in paths if (scratch / "conflicted" / p).is_file()}
        record.update(conflicted_files=paths,
                      conflict_blocks=sum(len(merge.conflicts(merge.blocks(t))) for t in files.values()))
        merge_config = merge.load_config(root / config["merge_config"])
        method, calls, resolved = config.get("merge_method", "llm"), [], None
        try:
            if method != "none":  # "none": plain git merge, a conflict stays unresolved
                resolved = merge.resolve(merge_config, method, files, calls)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        if calls:
            (Path(target) / "merge_calls.json").write_text(json.dumps(calls, indent=2))
            record["cost_usd"] = sum(c["cost_usd"] or 0 for c in calls)
        if resolved is not None and set(resolved) == set(paths):
            for path, text in resolved.items():
                out = scratch / "resolved" / path
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text)
            docker_run(image, MERGE, scratch, [base])
            status = (scratch / "status").read_text().strip()
        status = "resolved" if status == "resolved" else "unresolved"
    record["status"] = status
    merged = scratch / "merged.patch"
    record["patch"] = merged.read_text() if status in ("clean", "resolved") and merged.is_file() else None
    return record


def test_state(runner, instance, patch, tests, scratch, timeout):
    """Run tests on base plus `patch` (None or empty: base unchanged); keep a compact result."""
    result = runner.run(instance, patch if patch and patch.strip() else None, tests["patches"],
                        tests["fail_to_pass"], tests.get("pass_to_pass"), scratch_dir=scratch, timeout=timeout)
    return {k: result[k] for k in ("applied", "fail_to_pass", "n_pass", "n_total", "p2p_pass", "p2p_total")} | {
        "stdout_tail": result["stdout_tail"][-3000:], "stderr_tail": result["stderr_tail"][-3000:]}


def test_sets(instance):
    a, b = instance["agentA_fail_to_pass"], instance["agentB_fail_to_pass"]
    return {"A": {"patches": [instance["agentA_test_patch"]], "fail_to_pass": a,
                  "pass_to_pass": instance.get("agentA_pass_to_pass") or []},
            "B": {"patches": [instance["agentB_test_patch"]], "fail_to_pass": b},
            "combined": {"patches": [instance["combined_test_patch"]], "fail_to_pass": a + b}}


def side_passed(result, ids):
    return bool(ids) and all(result["fail_to_pass"].get(t) == "PASSED" for t in ids)


def score(config, root, job, output, instance, runner):
    """Write parallact_eval.json: each agent's patch on its own tests, the merge, the merged patch on all tests."""
    target = job_dir(output, job)
    out = target / "parallact_eval.json"
    if out.is_file():
        return json.loads(out.read_text())
    timeout = config.get("test_timeout", 2400)
    scratch = Path(output) / "scratch"
    sets = test_sets(instance)
    if job.get("setting") == "solo":
        record = score_solo(instance, target, sets, runner, scratch, timeout)
        out.write_text(json.dumps(record, indent=2))
        return record
    patches = [clean_patch((target / f"agent{f}.patch").read_text() if (target / f"agent{f}.patch").is_file() else "")
               for f in FEATURES]
    record = {"instance": instance["instance_id"], "empty": [SIDES[f] for f, p in zip(FEATURES, patches) if not p.strip()],
              "own": {}}
    for feature, patch in zip(FEATURES, patches):
        side = SIDES[feature]
        record["own"][side] = test_state(runner, instance, patch, sets[side], scratch, timeout)
    merged = merge_patches(config, root, instance, patches, target)
    record["merge"] = {k: v for k, v in merged.items() if k != "patch"}
    if merged.get("patch") is not None:
        (target / "merged.patch").write_text(merged["patch"])
        result = test_state(runner, instance, merged["patch"], sets["combined"], scratch, timeout)
        record["merged"] = result | {
            "A": side_passed(result, instance["agentA_fail_to_pass"]),
            "B": side_passed(result, instance["agentB_fail_to_pass"])}
    out.write_text(json.dumps(record, indent=2))
    return record


def score_solo(instance, target, sets, runner, scratch, timeout):
    """The solo patch against the combined tests. CooperBench grades only work the agent published (a PR or
    a pushed branch); its local work at the end is scored too when it differs."""
    record = {"instance": instance["instance_id"], "setting": "solo"}
    texts = {name: clean_patch((target / f).read_text() if (target / f).is_file() else "")
             for name, f in (("solo", "solo.patch"), ("local", "local_solo.patch"))}
    for name, text in texts.items():
        if name == "local" and text.strip() == texts["solo"].strip():
            record["local"] = {"same_as_published": True}
            continue
        result = test_state(runner, instance, text, sets["combined"], scratch, timeout)
        record[name] = result | {"empty": not text.strip(),
                                 "A": side_passed(result, instance["agentA_fail_to_pass"]),
                                 "B": side_passed(result, instance["agentB_fail_to_pass"])}
    return record


def gold(config, root, instance, output, runner):
    """Check the harness on one instance with its reference patches; writes gold/<instance>.json."""
    out = Path(output) / "gold" / f"{instance['instance_id']}.json"
    if out.is_file():
        return json.loads(out.read_text())
    out.parent.mkdir(parents=True, exist_ok=True)
    timeout = config.get("test_timeout", 2400)
    scratch = Path(output) / "scratch"
    sets = test_sets(instance)
    started = time.time()
    record = {"instance": instance["instance_id"],
              "base_combined": test_state(runner, instance, None, sets["combined"], scratch, timeout),
              "A_alone": test_state(runner, instance, instance["agentA_solution"], sets["A"], scratch, timeout),
              "B_alone": test_state(runner, instance, instance["agentB_solution"], sets["B"], scratch, timeout),
              "reconciled": test_state(runner, instance, instance["reconciliation_patch"], sets["combined"],
                                       scratch, timeout)}
    work = Path(output) / "gold" / instance["instance_id"]
    naive = merge_patches({**config, "merge_method": "none"}, root, instance,
                          [instance["agentA_solution"], instance["agentB_solution"]], work)
    record["naive_merge"] = {k: v for k, v in naive.items() if k != "patch"}
    if naive.get("patch") is not None:
        record["naive_combined"] = test_state(runner, instance, naive["patch"], sets["combined"], scratch, timeout)
    shutil.rmtree(work, ignore_errors=True)
    record["checks"] = gold_checks(record, instance)
    record["seconds"] = round(time.time() - started)
    out.write_text(json.dumps(record, indent=2))
    return record


def gold_checks(record, instance):
    a, b = instance["agentA_fail_to_pass"], instance["agentB_fail_to_pass"]
    return {"base_fails_all": record["base_combined"]["n_pass"] == 0,
            "A_alone_passes": side_passed(record["A_alone"], a),
            "B_alone_passes": side_passed(record["B_alone"], b),
            "reconciled_passes": side_passed(record["reconciled"], a + b),
            "naive_fails": not (record.get("naive_combined") and side_passed(record["naive_combined"], a + b))}


def images_for(config, root, jobs):
    instances = all_instances(config, root)
    return {(j["repo"], j["task_id"]): {"image": instances[j["instance"]]["runtime"]["image_uri"],
                                        "base": instances[j["instance"]]["base_commit"]} for j in jobs}


def run_job(config, root, job, output, instance, runner):
    target = job_dir(output, job)
    name = f"{job.get('arm', '')} r{job['repeat']} {job['instance']}".strip()
    try:
        if (target / "result.json").is_file() and agent_errors(target):
            attempt = 1
            while target.with_name(f"{target.name}.error{attempt}").exists():
                attempt += 1
            target.rename(target.with_name(f"{target.name}.error{attempt}"))
            print(f"retry {name} after agent error", flush=True)
        if not (target / "result.json").is_file():
            print(f"run {name}", flush=True)
            run_agents(config, root, job, output, instance)
        if (target / "result.json").is_file() and not agent_errors(target):
            score(config, root, job, output, instance, runner)
            print(f"scored {name}", flush=True)
    except Exception as exc:  # one broken job must not stop the rest
        print(f"job failed: {name}: {type(exc).__name__}: {exc}", flush=True)


def claimed(output, key):
    try:
        os.mkdir(Path(output) / "claims" / key)
        return True
    except FileExistsError:
        return False


def execute(config, root, shard=(0, 1), limit=None):
    """Run missing jobs; workers share one queue through claims/ (the CLI clears it first)."""
    check_source(config, root)
    s1.setup_runner(root, True, config.get("redis", "redis://localhost:6379"))
    import litellm
    # litellm waits 6000 s for a response by default, so one stalled API connection held a run for
    # over an hour in the pilot. The agent retries a timed-out call.
    litellm.request_timeout = config.get("request_timeout", 300)
    output = root / config["output"]
    (output / "claims").mkdir(parents=True, exist_ok=True)
    jobs = plan(config, root)[:limit] if limit else plan(config, root)
    install_hooks(images_for(config, root, jobs))
    install_guidance()
    instances, runner = all_instances(config, root), load_runner(config, root)
    if shard[0] == 0:
        (output / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    for job in jobs:
        if claimed(output, f"{job.get('arm', '')}_r{job['repeat']}_{job['task_id']}"):
            run_job(config, root, job, output, instances[job["instance"]], runner)


def execute_gold(config, root, shard=(0, 1), limit=None):
    """Reference-patch checks for the sampled instances; Docker only, no model calls."""
    check_source(config, root)
    s1.prepare_environment(root)
    output = root / config["output"]
    (output / "claims_gold").mkdir(parents=True, exist_ok=True)
    runner = load_runner(config, root)
    chosen = select(config, root)[:limit] if limit else select(config, root)
    for instance in chosen:
        try:
            os.mkdir(output / "claims_gold" / instance["instance_id"])
        except FileExistsError:
            continue
        try:
            record = gold(config, root, instance, output, runner)
            print(f"gold {instance['instance_id']}: {record['checks']} ({record['seconds']} s)", flush=True)
        except Exception as exc:
            print(f"gold failed: {instance['instance_id']}: {type(exc).__name__}: {exc}", flush=True)


# --- analysis (standard library) -------------------------------------------------------

# repo, task_id, features, merge_status, merged_passed and own_passed follow the S1 rows, so that
# conflict_states can read these runs.
ROW_COLUMNS = ["setting", "repeat", "instance", "repo", "task_id", "features", "source_repo", "corpus", "mutex_type",
               "finished", "messages", "cost_usd", "steps", "minutes", "empty_patches", "shared_files", "own_A",
               "own_B", "merge_status", "conflict_blocks", "merged_A", "merged_B", "both", "both_clean_merge",
               "own_both", "local_A", "local_B", "merged_passed", "own_passed", "test_errors"]


def passed(a, b):
    return " ".join(str(f) for f, ok in zip(FEATURES, (a, b)) if ok)


def read_json(path):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else {}


def collect(config, root):
    output = root / config["output"]
    instances = all_instances(config, root)
    rows = []
    for job in plan(config, root):
        target, instance = job_dir(output, job), instances[job["instance"]]
        result, ev = read_json(target / "result.json"), read_json(target / "parallact_eval.json")
        write_dataset(output, instance, job)
        row = {"arm": job.get("arm", ""),
               "setting": job["setting"], "repeat": job["repeat"], "instance": job["instance"], "repo": job["repo"],
               "task_id": job["task_id"], "features": "1 2", "source_repo": instance["repo"],
               "corpus": instance["_corpus"], "mutex_type": instance.get("mutex_type", ""),
               "finished": bool(ev)}
        if result:
            row.update(messages=result.get("messages_sent", 0), cost_usd=round(result.get("total_cost", 0), 4),
                       steps=result.get("total_steps", 0), minutes=round(result.get("duration_seconds", 0) / 60, 1))
            if job["setting"] == "coop":
                texts = [(target / f"agent{f}.patch").read_text() if (target / f"agent{f}.patch").is_file() else ""
                         for f in FEATURES]
                row["shared_files"] = len(s1.patch_files(texts[0]) & s1.patch_files(texts[1]))
            if job.get("arm"):
                row["instruction_given"] = guidance_given(target, read_json(target / "arm.json").get("instructions", {}))
        if ev and job["setting"] == "solo":
            solo, local = ev["solo"], ev.get("local", {})
            local = solo if local.get("same_as_published") else local
            row.update(empty_patches=int(solo["empty"]), merge_status="solo", merged_A=solo["A"], merged_B=solo["B"],
                       merged_passed=passed(solo["A"], solo["B"]),
                       both=solo["A"] and solo["B"], local_A=local.get("A", False), local_B=local.get("B", False),
                       test_errors=sum(s.get("applied", "ok") != "ok" for s in (solo, local)))
        elif ev:
            merged = ev.get("merged", {})
            states = [ev["own"]["A"], ev["own"]["B"]] + ([merged] if merged else [])
            row.update(empty_patches=len(ev["empty"]),
                       own_A=side_passed(ev["own"]["A"], instance["agentA_fail_to_pass"]),
                       own_B=side_passed(ev["own"]["B"], instance["agentB_fail_to_pass"]),
                       merge_status=ev["merge"]["status"], conflict_blocks=ev["merge"].get("conflict_blocks", 0),
                       merged_A=merged.get("A", False), merged_B=merged.get("B", False),
                       both=bool(merged.get("A") and merged.get("B")),
                       both_clean_merge=bool(ev["merge"]["status"] == "clean" and merged.get("A") and merged.get("B")),
                       own_both=bool(side_passed(ev["own"]["A"], instance["agentA_fail_to_pass"])
                                     and side_passed(ev["own"]["B"], instance["agentB_fail_to_pass"])),
                       test_errors=sum(s["applied"] != "ok" for s in states))
            row.update(merged_passed=passed(row["merged_A"], row["merged_B"]),
                       own_passed=passed(row["own_A"], row["own_B"]))
        rows.append(row)
    return rows


def arm_summaries(config, rows):
    """Per arm: outcomes and behaviour, and paired comparisons with the control on the instances both finished.

    A comparison counts the instances where the two arms differ (arm higher / control higher) with an
    exact sign test. `concessions` are runs where one agent submitted no code of its own."""
    from .branch import exact_p
    control, out = config.get("control"), {}
    done = {(r["arm"], r["repeat"], r["instance"]): r for r in rows if r["finished"]}
    measures = {"both_pass": lambda r: int(r["both"]),
                "requests_passed": lambda r: int(r["merged_A"]) + int(r["merged_B"]),
                "messages": lambda r: r.get("messages", 0)}
    for arm in config["arms"]:
        R = [r for (a, _, _), r in done.items() if a == arm]
        entry = {"finished": len(R), "both_pass": sum(r["both"] for r in R),
                 "requests_passed": sum(int(r["merged_A"]) + int(r["merged_B"]) for r in R),
                 "own_both": sum(r["own_both"] for r in R),
                 "merge_conflicts": sum(r["merge_status"] != "clean" for r in R),
                 "concessions": sum(r["empty_patches"] == 1 for r in R),
                 "concessions_both_pass": sum(r["empty_patches"] == 1 and r["both"] for r in R),
                 "mean_messages": round(sum(r.get("messages", 0) for r in R) / len(R), 1) if R else None,
                 "mean_minutes": round(sum(r.get("minutes", 0) for r in R) / len(R), 1) if R else None,
                 "cost_usd": round(sum(r.get("cost_usd", 0) for r in R), 2),
                 "instruction_missing": sum(r.get("instruction_given") is False for r in R)}
        if control and arm != control:
            pairs = [(r, done[(control, k[1], k[2])]) for k, r in done.items()
                     if k[0] == arm and (control, k[1], k[2]) in done]
            entry["vs_control"] = {"pairs": len(pairs)}
            for name, value in measures.items():
                up = sum(value(a) > value(c) for a, c in pairs)
                down = sum(value(a) < value(c) for a, c in pairs)
                entry["vs_control"][name] = {"arm_higher": up, "control_higher": down, "p": exact_p(up, up + down)}
        out[arm] = entry
    return out


def summarize(config, rows, gold_records):
    done = [r for r in rows if r["finished"]]
    outcome = Counter(("both" if r["merged_A"] and r["merged_B"] else "A only" if r["merged_A"]
                       else "B only" if r["merged_B"] else "neither") for r in done)
    extra = ({"both_pass_local_work": sum(bool(r["local_A"] and r["local_B"]) for r in done)}
             if config.get("setting") == "solo" else
             {"both_pass_clean_merge": sum(r["both_clean_merge"] for r in done)})
    return {"study": config["study"], "setting": config.get("setting", "coop"), "jobs": len(rows), "finished": len(done),
            "both_pass": sum(r["both"] for r in done), **extra,
            "merged_outcomes": dict(outcome),
            "own_pass": None if config.get("setting") == "solo" else
            {"A": sum(bool(r.get("own_A")) for r in done), "B": sum(bool(r.get("own_B")) for r in done)},
            "merge_status": dict(Counter(r["merge_status"] for r in done)),
            "runs_with_messages": sum(1 for r in done if r.get("messages")),
            "mean_messages": round(sum(r.get("messages", 0) for r in done) / len(done), 1) if done else None,
            "runs_with_shared_files": sum(1 for r in done if r.get("shared_files")),
            "empty_patches": sum(r["empty_patches"] for r in done),
            "test_errors": sum(r["test_errors"] for r in done),
            "cost_usd": round(sum(r.get("cost_usd", 0) for r in rows), 2),
            "mean_minutes": round(sum(r.get("minutes", 0) for r in done) / len(done), 1) if done else None,
            **({"control": config.get("control"), "arms": arm_summaries(config, rows)} if config.get("arms") else {}),
            "gold": {"checked": len(gold_records),
                     "all_checks_pass": sum(all(g["checks"].values()) for g in gold_records),
                     "failed": {g["instance"]: [k for k, v in g["checks"].items() if not v]
                                for g in gold_records if not all(g["checks"].values())}}}


def report(config, root):
    output = root / config["output"]
    rows = collect(config, root)
    gold_records = [read_json(p) for p in sorted((output / "gold").glob("*.json"))]
    summary = summarize(config, rows, gold_records)
    output.mkdir(parents=True, exist_ok=True)
    for path in (output / "rows.csv", root / "results" / f"{config['study']}_rows.csv"):
        with open(path, "w", newline="") as handle:
            columns = (["arm", "instruction_given"] if config.get("arms") else []) + ROW_COLUMNS
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    for path in (output / "summary.json", root / "results" / f"{config['study']}_summary.json"):
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(argv):
    root = Path(__file__).resolve().parents[2]
    config = load_config(argv[0])
    shard, limit = (0, 1), None
    if "--shard" in argv:
        shard = tuple(int(v) for v in argv[argv.index("--shard") + 1].split("/"))
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    if "--gold" in argv:
        execute_gold(config, root, shard, limit)
    else:
        execute(config, root, shard, limit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
