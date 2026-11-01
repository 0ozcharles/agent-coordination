"""One CooperBench solo run with cost accounting and automatic local evaluation."""
import argparse
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from budget_guard import BudgetGuard, MODEL_PROFILES, write_json

MODEL = "Qwen/Qwen3.5-9B"
MAX_OUTPUT = 16384

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "CooperBench"
IMAGE = "akhatua/cooperbench-pallets-jinja:task1621"
BASE = "a2920752fd111c2d52c88eb57487667b3cf0ea7b"
DEADLINE_SECONDS = 1200


def configure():
    os.environ["PATH"] = "/Applications/Docker.app/Contents/Resources/bin:" + os.environ["PATH"]
    os.environ["DOCKER_HOST"] = "unix://" + str(Path.home() / ".docker/run/docker.sock")
    os.environ.pop("DOCKER_CONTEXT", None)
    os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(ROOT / "config/mini-swe-agent")
    # Prices are explicitly pinned to the verified Together page and recorded in the ledger.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
    os.environ["MSWEA_GLOBAL_CALL_LIMIT"] = "0"
    os.environ["MSWEA_GLOBAL_COST_LIMIT"] = "0"
    for name in list(os.environ):
        if name.startswith("AZURE_OPENAI_"):
            os.environ.pop(name)
    for name in ("httpx", "httpcore", "openai", "LiteLLM"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def credential(image_path):
    key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if key:
        return key
    if not image_path:
        raise RuntimeError("TOGETHER_API_KEY is absent; configure it locally")
    result = subprocess.run([str(ROOT / "credential_ocr"), image_path],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Local credential OCR failed; no API request made")
    lines = json.loads(result.stdout)
    start = next((i for i, line in enumerate(lines) if "together.ai" in line.lower()), None)
    if start is None:
        raise RuntimeError("Together credential not found in supplied image")
    segments = [lines[start].split(":", 1)[-1]]
    for line in lines[start + 1:]:
        if "openai" in line.lower() or ":" in line:
            break
        if re.fullmatch(r"[A-Za-z0-9_\- ]+", line.strip()):
            segments.append(line)
        else:
            break
    key = "".join(segments).replace(" ", "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{30,160}", key):
        raise RuntimeError("Credential OCR was ambiguous; no API request made")
    return key


def child(run_dir, credential_image):
    configure()
    import docker
    import yaml
    from openai import OpenAI
    import cooperbench.agents.mini_swe_agent_v2.adapter as adapter
    import cooperbench.agents.mini_swe_agent_v2.environments.docker as docker_env
    from cooperbench.agents.mini_swe_agent_v2.models.litellm_model import LitellmModel
    from cooperbench.agents.mini_swe_agent_v2.models.utils.actions_toolcall import BASH_TOOL
    from cooperbench.runner.solo import execute_solo

    expected = json.loads((ROOT / "artifacts/task_image.json").read_text())
    dc = docker.from_env(timeout=15)
    image = dc.images.get(IMAGE)
    if image.id != expected["Id"] or image.attrs["Architecture"] != "arm64":
        raise RuntimeError("Task image changed; stopped before API use")
    if subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip() != \
            "63b9d44d9f39a02fccf5bf0052db48a917a011fd":
        raise RuntimeError("CooperBench checkout changed")
    key = credential(credential_image)
    guard = BudgetGuard(ROOT / "artifacts/model_budget_ledger.json", run_dir.name,
                        model=MODEL, enforce_limits=False, max_output_tokens=MAX_OUTPUT)
    client = OpenAI(api_key=key, base_url="https://api.together.ai/v1",
                    max_retries=0, timeout=90.0)
    raw_dir = run_dir / "responses"
    raw_dir.mkdir()

    def request(messages, tools, phase):
        for attempt in range(2):
            row = guard.reserve(phase)
            payload = dict(model=MODEL, messages=messages, tools=tools,
                           temperature=0.7, top_p=0.8, max_tokens=MAX_OUTPUT,
                           extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            write_json(raw_dir / f"request_{row['request_number']:02d}_input.json", payload)
            try:
                response = client.chat.completions.create(**payload)
                break
            except Exception as exc:
                code = getattr(exc, "status_code", None)
                guard.fail(row, type(exc).__name__, code)
                # Preserve diagnostic bodies, never headers; remove the exact key and token patterns.
                body = json.dumps(getattr(exc, "body", None), ensure_ascii=False, default=str)
                body = body.replace(key, "[REDACTED]")
                body = re.sub(r"(?:tgp_|sk-)[A-Za-z0-9_\-]+", "[REDACTED]", body)
                write_json(raw_dir / f"request_{row['request_number']:02d}_error.json", {
                    "http_status": code, "error_type": type(exc).__name__,
                    "body": body, "request_id": getattr(exc, "request_id", None),
                })
                if attempt == 0 and code in (500, 502, 503, 504):
                    print(f"HTTP {code}; retrying once (recorded in usage ledger)", flush=True)
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Together request failed: {type(exc).__name__}, HTTP {code}") from None
        cost = guard.record(row, response.usage.model_dump() if response.usage else {},
                            response.choices[0].finish_reason)
        raw = response.model_dump()
        serialized = json.dumps(raw)
        if key in serialized:
            raise RuntimeError("Credential unexpectedly present in response; stopped")
        write_json(raw_dir / f"request_{row['request_number']:02d}.json", raw)
        print(f"request {row['request_number']}: {phase}, finish={response.choices[0].finish_reason}, cost=${cost:.6f}", flush=True)
        return response

    class BoundedModel(LitellmModel):
        def __init__(self, **kwargs):
            if kwargs.get("model_name") != MODEL or set(kwargs.get("model_kwargs") or {}) - {"drop_params"}:
                raise RuntimeError("Unexpected provider configuration; no additional request made")
            super().__init__(**kwargs)

        def _query(self, messages, **kwargs):
            allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
            clean = [{k:v for k,v in message.items() if k in allowed} for message in messages]
            return request(clean, self._tools, "solo")

        def _calculate_cost(self, response):
            usage = response.usage
            rates = MODEL_PROFILES[MODEL]
            return {"cost": usage.prompt_tokens * rates["input_rate"] + usage.completion_tokens * rates["output_rate"]}

        def summarize_context(self, *args, **kwargs):
            raise RuntimeError("Compaction is disabled in the bounded smoke run")

    OriginalEnvironment = docker_env.DockerEnvironment
    OriginalAgent = adapter.DefaultAgent

    class DeadlineAwareAgent(OriginalAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started_at = time.monotonic()
            self.deadline_reminder_sent = False

        def step(self):
            remaining_seconds = max(0, int(DEADLINE_SECONDS - 60 - (time.monotonic() - self.started_at)))
            if self.n_calls == 0 or (remaining_seconds <= 120 and not self.deadline_reminder_sent):
                if remaining_seconds <= 120:
                    self.deadline_reminder_sent = True
                self.add_messages(self.model.format_message(role="user", content=(
                    f"Execution time remaining: about {remaining_seconds} seconds. "
                    "There is no monetary or model-turn budget. Leave enough time to commit, push, "
                    "open your local PR, and then issue "
                    "the exact completion command in a separate bash call. "
                    "Submit your current implementation before the timeout even if some tests still fail; "
                    "do not claim failing tests passed. Use small targeted edits; avoid printing or "
                    "replacing entire large files in a single tool call, since oversized outputs can be truncated."
                )))
            return super().step()

    class BoundedEnvironment(OriginalEnvironment):
        def __init__(self, **kwargs):
            kwargs.update(image=expected["Id"], timeout=120, network="none", container_timeout="20m", forward_env=[],
                          run_args=["--rm", "--label", f"agent-rsi-run={run_dir.name}",
                                    "--cpus=2", "--memory=2g", "--pids-limit=256"])
            super().__init__(**kwargs)
            (run_dir / "container_id.txt").write_text(self.container_id + "\n")
            inspection = dc.containers.get(self.container_id).attrs
            assert not inspection["Mounts"]
            assert inspection["HostConfig"]["NetworkMode"] == "none"
            check = self.execute({"command": "git rev-parse HEAD; test ! -e /patches && echo no_reference_patches"})
            if BASE not in check["output"] or "no_reference_patches" not in check["output"]:
                raise RuntimeError("Task container isolation check failed")
            write_json(run_dir / "isolation.json", {
                "network": "none", "host_mounts": [], "forwarded_credentials": False,
                "base_commit": BASE, "reference_patches_present": False,
            })

        def get_template_vars(self, **kwargs):
            variables = super().get_template_vars(**kwargs)
            # The upstream implementation reports the macOS host in a Linux container prompt.
            variables.update(system="Linux", machine="aarch64", release="container", version="container")
            return variables

        def cleanup(self):
            cid = getattr(self, "container_id", None)
            if cid:
                # Preserve an unsubmitted draft for diagnosis; the evaluator still uses solo.patch only.
                try:
                    draft = subprocess.run(["docker", "exec", "-w", "/workspace/repo", cid,
                                            "git", "diff", "--binary", BASE],
                                           capture_output=True, text=True, timeout=15)
                    if draft.returncode == 0:
                        (run_dir / "working_tree_diagnostic.patch").write_text(draft.stdout)
                except Exception:
                    pass
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=20)
                self.container_id = None

    adapter.LitellmModel = BoundedModel
    adapter.DefaultAgent = DeadlineAwareAgent
    docker_env.DockerEnvironment = BoundedEnvironment
    config = {"agent": {"step_limit": 0, "cost_limit": 0.0, "compaction_enabled": False,
                          "output_path": str(run_dir / "trajectory_live.json")},
              "model": {"cost_tracking": "default", "model_kwargs": {}},
              "git_network": "none"}
    config_path = run_dir / "agent_config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    write_json(run_dir / "run_config.json", {
        "model": MODEL, "provider": "Together", "thinking_enabled": False,
        "temperature": 0.7, "top_p": 0.8,
        "price_profile": MODEL_PROFILES[MODEL],
        "max_output_tokens": MAX_OUTPUT, "max_requests_including_preflight": None,
        "deadline_seconds": DEADLINE_SECONDS, "request_timeout_seconds": 90,
        "sdk_automatic_retries": 0, "bounded_5xx_retries": 1, "compaction": False,
        "cost_accounting": "record_only", "deadline_reminder_seconds": 120,
        "edit_guidance": "Use small targeted edits to avoid truncated tool calls",
        "per_run_budget_usd": None, "round_budget_usd": None,
        "repo": "pallets_jinja_task", "task_id": 1621, "features": [1, 6],
        "framework": "mini_swe_agent_v2", "upstream_source_modified": False,
        "model_transport": "OpenAI SDK pointed at Together; local bounded subclass",
    })
    try:
        # Validate tool-call serialization without executing provider-generated code.
        probe = request([{"role": "user", "content": "Call bash with the command printf ready."}], [BASH_TOOL], "preflight")
        calls = probe.choices[0].message.tool_calls or []
        passed = any(c.function.name == "bash" and isinstance(json.loads(c.function.arguments).get("command"), str) for c in calls)
        write_json(run_dir / "preflight.json", {"passed": passed, "tool_call_count": len(calls)})
        if not passed:
            raise RuntimeError("Tool calling preflight did not pass; task not started")
        result = execute_solo(
            repo_name="pallets_jinja_task", task_id=1621, features=[1,6],
            run_name=run_dir.name, agent_name="mini_swe_agent_v2",
            model_name=MODEL, backend="docker", quiet=True, agent_config=str(config_path),
            dataset_dir=REPO / "dataset", logs_dir=ROOT / "artifacts/solo_runs",
        )
        write_json(run_dir / "agent_outcome.json", {k:v for k,v in result.items() if k != "result"})
        outcome = result.get("result", {})
        write_json(run_dir / "completion_status.json", {
            "agent_status": outcome.get("status"), "patch_nonempty": bool(outcome.get("patch", "").strip()),
        })
        print("agent completed; status:", outcome.get("status"), flush=True)
        if not outcome.get("patch", "").strip():
            raise RuntimeError("Agent did not submit a nonempty patch; inspect saved result")
    finally:
        write_json(run_dir / "usage_summary.json", guard.summary())
        client.close()
        dc.close()


def cleanup(run_dir):
    configure()
    # Label scope works even if the child was killed before writing its container ID.
    result = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=agent-rsi-run={run_dir.name}"],
                            capture_output=True, text=True, timeout=20, check=True)
    ids = [cid for cid in result.stdout.splitlines() if re.fullmatch(r"[a-f0-9]{12,64}", cid)]
    for cid in ids:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=20, check=True)
    write_json(run_dir / "cleanup.json", {"removed_remaining_containers": len(ids)})


def supervise(credential_image):
    configure()
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with (ROOT / "artifacts/model_run.lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = ROOT / "artifacts/solo_runs" / f"solo_smoke_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=False)
        print(f"Run: {run_dir}\nStarting preflight and solo task (cost accounting only)...", flush=True)
        args = [sys.executable, "-u", str(Path(__file__).resolve()), "--child", str(run_dir)]
        if credential_image:
            args += ["--credential-image", credential_image]
        started = time.monotonic()
        with (run_dir / "console.log").open("w") as log:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    pass_fds=(lock.fileno(),))
            timed_out = False
            try:
                code = proc.wait(timeout=DEADLINE_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    code = proc.wait()
            except BaseException:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise
            finally:
                cleanup(run_dir)
        write_json(run_dir / "process_status.json", {
            "exit_code": code, "timed_out": timed_out, "elapsed_seconds": time.monotonic()-started,
        })
        guard = BudgetGuard(ROOT / "artifacts/model_budget_ledger.json", run_dir.name,
                            model=MODEL, enforce_limits=False, max_output_tokens=MAX_OUTPUT)
        write_json(run_dir / "usage_summary.json", guard.summary())
        print(str(run_dir), flush=True)
        if code != 0:
            completion = run_dir / "completion_status.json"
            result = run_dir / "solo/pallets_jinja_task/1621/f1_f6/result.json"
            if result.exists():
                data = json.loads(result.read_text()).get("agent", {})
                print("Stopped:", data.get("error") or data.get("status"), flush=True)
            else:
                errors = sorted((run_dir / "responses").glob("*_error.json"))
                if errors:
                    error = json.loads(errors[-1].read_text())
                    print(f"Stopped: HTTP {error['http_status']} ({error['error_type']})", flush=True)
            raise SystemExit("Run stopped; details are saved in console.log and responses/*_error.json")
        print("Submission saved; running the official evaluator (no model calls).", flush=True)
        from evaluate_solo_smoke import evaluator_environment
        evaluation = subprocess.run([
            sys.executable, "-u", str(ROOT / "evaluate_solo_smoke.py"), "--run-dir", str(run_dir),
        ], env=evaluator_environment())
        summary_path = run_dir / "evaluation_summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {"status": "error"}
        write_json(run_dir / "pipeline_summary.json", {
            "pipeline_completed": summary.get("status") in ("passed", "failed"),
            "task_solved": summary.get("both_passed", False),
            "agent_status": json.loads((run_dir / "completion_status.json").read_text()).get("agent_status"),
            "evaluation": summary, "usage": guard.summary(),
        })
        if evaluation.returncode:
            raise SystemExit("Agent submitted a patch, but evaluation needs inspection; see evaluation_summary.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--credential-image")
    args = parser.parse_args()
    if args.child:
        child(Path(args.child), args.credential_image)
    else:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        supervise(args.credential_image)
