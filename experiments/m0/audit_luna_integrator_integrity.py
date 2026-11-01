"""Independent saved-artifact audit of Integrator versus fresh StrongShare.

Pure readers only: no production runner/verifier imports, subprocess, network,
Docker, or scoring. --partial tolerates unstarted/live stages and never treats
an in-flight request as free. Optional output is exclusively a NEW audit_*.json
inside the umbrella suite. Existing artifacts are never overwritten.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

# These are arithmetic/JSON helpers from the previous independent audit, not
# production code. They import only the standard library and have no side effects.
from audit_luna_timing_results import digest, number, same, usage as exact_usage, sum_usage

ROOT = Path(__file__).resolve().parent
DEFAULT_SUITE = ROOT / "artifacts/handoff_expansion_runs/luna_integrator_v1"
TASKS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
TIMINGS = ("early", "late")
CONDITIONS = ("strongshare", "integrator")
FIELDS = ("task", "timing", "repeat", "condition")
MODEL = "gpt-6-luna"


def usage(rows):
    # Saved per-run aggregates use nine decimal places; raw per-request charges
    # are checked independently before this reporting precision is applied.
    result = exact_usage(rows)
    result["known_cost_usd"] = round(result["known_cost_usd"], 9)
    return result


class Reader:
    """Memoized streaming hashes; detect mutation during this audit, not by polling."""
    def __init__(self): self.files = {}; self.hashes = {}

    def state(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file(): raise ValueError("Expected ordinary file: " + str(path))
        st = path.stat()
        return st.st_ino, st.st_size, st.st_mtime_ns

    def sha(self, path):
        path = Path(path); state = self.state(path)
        if path in self.files and self.files[path] != state:
            raise ValueError("Artifact changed during audit; rerun later: " + str(path))
        self.files[path] = state
        if path not in self.hashes:
            h = hashlib.sha256()
            with path.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
            self.hashes[path] = h.hexdigest()
        return self.hashes[path]

    def read(self, path):
        self.sha(path)
        return json.loads(Path(path).read_text())

    def info(self, record):
        path = Path(record["path"])
        same(record["sha256"], self.sha(path), "file digest")
        if "bytes" in record: same(record["bytes"], path.stat().st_size, "file bytes")

    def stable(self):
        return [str(p) for p, state in self.files.items() if self.state(p) != state]


def stage(suite, when): return suite.with_name(suite.name + "_" + when)
def identity(row): return tuple(row[k] for k in FIELDS)
def run_path(suite, row):
    return stage(suite, row["timing"]) / row["task"] / "jobs" / f"{row['task']}_{row['timing']}_r{row['repeat']:02d}_{row['condition']}"


def raw_usage(reader, run, rows, *, partial=False):
    """Reprice raw provider token buckets independently of LunaAccounting."""
    counts = Counter(); unknown = []; tokens = Counter(); error_types = Counter()
    for row in rows:
        phase, agent = row["phase"].split(":")
        owner = run / "failed_attempts" / (phase + "_" + row["archived_attempt"]) if row.get("excluded_from_baseline") else run / phase
        stem = owner / "responses" / agent / f"request_{row['request_number']:05d}"
        input_path, response_path = Path(str(stem) + "_input.json"), Path(str(stem) + ".json")
        live = partial and row["status"] == "in_flight"
        if not input_path.exists():
            if live: counts["live_inputs_not_yet_written"] += 1; continue
            raise ValueError("Ledger request has no saved transport input: " + str(stem))
        entry = reader.read(input_path); request = entry["request"]
        same({k: request[k] for k in ("model", "reasoning_effort", "temperature", "top_p", "max_completion_tokens", "service_tier")},
             {"model": MODEL, "reasoning_effort": "none", "temperature": .7, "top_p": .8,
              "max_completion_tokens": 16384, "service_tier": "default"}, "transport settings")
        same(request["extra_body"], {"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}}, "cache settings")
        same(entry["phase"], phase, "transport phase"); same(entry["agent_id"], agent, "transport owner")
        counts["inputs"] += 1; counts["transport_retries"] += entry["transport_attempt"] > 1
        for suffix in ("_error", "_query_error"):
            path = Path(str(stem) + suffix + ".json")
            if path.exists(): error_types[reader.read(path).get("error_type", "unknown")] += 1
        if row.get("actual_cost_usd") is None:
            unknown.append({"request_number": row["request_number"], "phase": row["phase"], "status": row["status"],
                "error_type": row.get("error_type"), "archived_attempt": row.get("archived_attempt"),
                "response_saved": response_path.exists()})
            if row["status"] != "in_flight" and row.get("error_type") != "InterruptedAttemptUsageUnknown" and not Path(str(stem) + "_error.json").exists():
                raise ValueError("Unknown request lost its error evidence")
            continue
        response = reader.read(response_path); u = response["usage"]; details = u.get("prompt_tokens_details") or {}
        same(u, row["usage"], "raw usage"); same(response["model"], row["response_model"], "response model")
        allowed = {"cached_tokens", "cache_write_tokens", "audio_tokens", "image_tokens", "text_tokens"}
        if any(k not in allowed and v is not None for k, v in details.items()): raise ValueError("Unrecognized potentially priced input bucket")
        if any(details.get(k) not in (None, 0) for k in ("audio_tokens", "image_tokens")):
            raise ValueError("Unexpected non-text usage")
        if not (response["model"] == MODEL or response["model"].startswith(MODEL + "-")):
            raise ValueError("Unexpected response model")
        if response.get("service_tier") not in (None, "default"): raise ValueError("Unexpected service tier")
        prompt, output = u["prompt_tokens"], u["completion_tokens"]
        cached, written = details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0
        ordinary = prompt - cached - written
        for value in (prompt, output, cached, written, ordinary):
            if type(value) is not int or value < 0: raise ValueError("Invalid token bucket")
        same(u["total_tokens"], prompt + output, "total tokens")
        long = prompt > 272000
        rates = {"ordinary_input": Decimal(".1"), "cached_input": Decimal(".01"),
                 "cache_write": Decimal(".125"), "output": Decimal(".5")}
        amounts = {"ordinary_input": ordinary, "cached_input": cached, "cache_write": written, "output": output}
        for key in rates: rates[key] *= Decimal("1.5") if long and key == "output" else (2 if long else 1)
        components = {k: float(Decimal(amounts[k]) * rates[k] / 1_000_000) for k in rates}
        same(row["actual_cost_usd"], sum(Decimal(str(v)) for v in components.values()), "recomputed charge")
        same(row["cost_components_usd"], components, "price components")
        same(row["rates_usd_per_million"], {k: float(v) for k, v in rates.items()}, "price rates")
        same(row["prompt_tokens"], prompt, "prompt tokens"); same(row["completion_tokens"], output, "output tokens")
        same(row["cached_input_tokens"], cached, "cache reads"); same(row["cache_write_tokens"], written, "cache writes")
        same(row["ordinary_input_tokens"], ordinary, "ordinary input")
        counts["responses"] += 1; tokens.update(amounts)
    return {"counts": dict(counts), "tokens": dict(tokens), "unknown_requests": unknown, "error_artifact_types": dict(error_types)}


def ledger(reader, run, phase, *, partial=False, extension=False, raw=False):
    path = run / "usage_ledger.json"
    data = reader.read(path) if path.exists() else {"requests": []}; rows = data["requests"]
    if rows and (data.get("model") != MODEL or data.get("accounting_schema") != "luna_standard_v1"):
        raise ValueError("Unexpected ledger model/schema")
    if any(r.get("model") != MODEL or r.get("phase") not in (phase + ":agent1", phase + ":agent2") for r in rows):
        raise ValueError("Unexpected model or agent in ledger")
    if len({r["request_number"] for r in rows}) != len(rows): raise ValueError("Duplicate transport request numbers")
    accepted = [r for r in rows if not r.get("excluded_from_baseline")]
    excluded = [r for r in rows if r.get("excluded_from_baseline")]
    if not partial and any(r["status"] == "in_flight" for r in rows): raise ValueError("Final audit found in-flight usage")
    grouped = defaultdict(list)
    for r in excluded:
        if not r.get("archived_attempt"): raise ValueError("Excluded request lacks archive identity")
        grouped[r["archived_attempt"]].append(r)
    attempts = []
    dirs = {p.name.removeprefix(phase + "_"): p for p in (run / "failed_attempts").glob(phase + "_*") if p.is_dir()}
    if not set(grouped).issubset(dirs): raise ValueError("Excluded requests lost their failed attempt")
    for key, directory in sorted(dirs.items()):
        state = reader.read(directory / "status.json") if (directory / "status.json").exists() else {}
        if state.get("status") == "completed" and not state.get("infrastructure_error"):
            raise ValueError("A completed model execution was archived and rerun")
        elapsed = state.get("extension_elapsed_seconds" if extension else "elapsed_seconds")
        try: elapsed = float(number(elapsed))
        except ValueError: elapsed = None
        attempts.append({"directory": str(directory), "status": state.get("status"), "usage": usage(grouped[key]),
                         "known_seconds": elapsed, "unknown_time": elapsed is None})
    return {"directory": str(run), "phase": phase, "ledger_sha256": reader.sha(path) if path.exists() else None,
        "all_attempt_usage": usage(rows), "accepted_usage": usage(accepted), "failed_attempt_usage": usage(excluded),
        "failed_attempts": attempts, "failed_known_seconds": sum(a["known_seconds"] or 0 for a in attempts),
        "failed_unknown_times": sum(a["unknown_time"] for a in attempts),
        "raw_usage": raw_usage(reader, run, rows, partial=partial) if raw else None}


def references(reader, protocol):
    checked = {}; historical = []
    for task in TASKS:
        pair = protocol["checkpoint_references"][task]; audits = {}
        late_dir = Path(pair["late"]["directory"])
        origin = late_dir.parents[1].with_name(late_dir.parents[1].name.removesuffix("_late"))
        for name in ("protocol", "summary", "status"):
            same(reader.sha(origin / (name + ".json")), pair["early"]["origin_" + name + "_sha256"], "historical origin")
            same(pair["late"]["origin_" + name + "_sha256"], pair["early"]["origin_" + name + "_sha256"], "common origin")
        for when in TIMINGS:
            ref = pair[when]; directory = Path(ref["directory"]); record = ref["record"]
            for name, expected in record["files_sha256"].items(): same(reader.sha(directory / name), expected, "historical checkpoint artifact")
            cp = reader.read(directory / "checkpoint.json")
            same(digest(cp), record["checkpoint_sha256"], "historical checkpoint semantic digest")
            audits[when] = ledger(reader, directory, "prefix", extension=when == "late")
            same(record["all_attempt_usage"], audits[when]["all_attempt_usage"], "historical all-attempt usage")
            same(record["accepted_usage"], audits[when]["accepted_usage"], "historical accepted usage")
            components = [audits["early"]["all_attempt_usage"]] + ([audits["late"]["all_attempt_usage"]] if when == "late" else [])
            same(ref["historical_usage"], sum_usage(components), "historical cost components")
            elapsed = number(record["elapsed_seconds"]) + number(audits["early"]["failed_known_seconds"])
            unknown = audits["early"]["failed_unknown_times"]
            if when == "late":
                elapsed += number(audits["late"]["failed_known_seconds"]); unknown += audits["late"]["failed_unknown_times"]
                saved = reader.read(directory / "prefix/status.json")
                same(round(number(pair["early"]["record"]["elapsed_seconds"]) + number(saved["extension_elapsed_seconds"]), 3), record["elapsed_seconds"], "cumulative late elapsed")
            same(ref["historical_execution_seconds"], elapsed, "historical time without duplicate early")
            same(ref["historical_time_unknown_attempts"], unknown, "historical unknown duration")
            checked[(task, when)] = {"reference": ref, "ledger": audits[when], "checkpoint": cp}
        historical += [audits[t]["all_attempt_usage"] for t in TIMINGS]
    return checked, sum_usage(historical)


def execution(reader, run, job, protocol, reference):
    phase = run / job["condition"]; config = reader.read(run / "task_config.json")
    saved = reader.read(phase / "status.json"); cp = reader.read(run / "checkpoint.json"); cp_hash = digest(cp)
    ref = reference["reference"]; task = protocol["tasks"][job["task"]]
    same(cp, reference["checkpoint"], "copied full checkpoint")
    same(config["checkpoint_sha256"], cp_hash, "config checkpoint")
    same(config["protocol_sha256"], digest(protocol), "config protocol")
    same(config["task"], task, "config task")
    same({k: config[k] for k in ("condition", "timing", "repeat")}, {k: job[k] for k in ("condition", "timing", "repeat")}, "config branch")
    same(config["submission_policy"], "authoritative_agent1_only" if job["condition"] == "integrator" else "native_cooperative_merge", "primary scoring policy")
    remaining = round(number(protocol["total_seconds"][job["task"]]) - number(ref["record"]["elapsed_seconds"]), 3)
    same(config["execution_seconds"], remaining, "branch allowance")
    same(saved["continuation_budget_seconds"], remaining, "worker allowance")
    same(saved["initialization_included_in_budget"], True, "startup consumes allowance")
    same(saved["status"], "completed", "accepted worker completion")
    same(saved["task"], task, "worker task")
    same(set(saved["outcomes"]), {"agent1", "agent2"}, "both agents have outcomes")
    if protocol["offline_selftest"] and any(o.get("status") != "Submitted" for o in saved["outcomes"].values()):
        raise ValueError("Offline worker did not submit both agents")
    same(reader.sha(run / "shared_git.tar"), reader.sha(Path(ref["directory"]) / "shared_git.tar"), "shared Git restore")
    provenance = reader.read(run / "checkpoint_provenance.json")
    same(provenance["source_directory"], ref["directory"], "checkpoint provenance")
    same(provenance["checkpoint_sha256"], cp_hash, "checkpoint provenance digest")
    receipt = reader.read(phase / "execution_receipt.json")
    for relative, expected in receipt["protected_files_sha256"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts: raise ValueError("Unsafe receipt path")
        same(reader.sha(run / relative), expected, "execution receipt")
    for name, expected in receipt["patch_sha256"].items(): same(reader.sha(phase / name), expected, "published patch")
    same(reader.read(phase / "source_manifest.json"), protocol["source_manifest"], "worker frozen sources")
    packet = reader.read(phase / "handoff_packet.json"); packet_hash = digest(packet)
    delivery = reader.read(phase / "handoff_delivery.json"); proof = reader.read(phase / "restore_proof.json")
    text = (phase / "handoff_packet.txt").read_text(); text_hash = hashlib.sha256(text.encode()).hexdigest()
    same(packet["checkpoint_sha256"], cp_hash, "packet checkpoint")
    same(delivery["packet_sha256"], packet_hash, "packet delivery digest")
    same(delivery["code_automatically_transferred"], False, "no code transfer")
    same(delivery["first_request_verified"], True, "both initial requests verified")
    same(proof["checkpoint_hash"], cp_hash, "restored checkpoint")
    assignment = reader.read(phase / "integrator_assignment.json") if job["condition"] == "integrator" else None
    for agent in ("agent1", "agent2"):
        inherited = cp["agents"][agent]["state"]["messages"]
        trajectory = reader.read(phase / (agent + ".traj.json"))["messages"]
        same(trajectory[:len(inherited)], inherited, "full inherited message history")
        same(proof["agents"][agent]["state_hash"], digest(cp["agents"][agent]["state"]), "restored model state")
        same(proof["agents"][agent]["filesystem_manifest_hash"], digest(cp["agents"][agent]["manifest"]), "restored worktree")
        recipient = delivery["recipients"][agent]; index = recipient["message_index"]
        same(trajectory[index]["content"], text, "complete packet in trajectory")
        same(recipient["message_sha256"], text_hash, "delivered complete packet digest")
        first = reader.read(phase / f"first_request_{agent}.json")
        same(first["verified"], True, "first request verified")
        if assignment:
            role = assignment["recipients"][agent]
            same(trajectory[role["message_index"]]["content"], role["content"], "role assignment in history")
            same(role["before_state_sha256"], digest(cp["agents"][agent]["state"]), "role original state")
        if not protocol["offline_selftest"]:
            first_input = sorted((phase / "responses" / agent).glob("request_*_input.json"))[0]
            sent = reader.read(first_input)["request"]["messages"]
            clean = lambda messages: [{k: v for k, v in m.items() if k in {"role", "content", "tool_calls", "tool_call_id", "name"}} for m in messages]
            same(sent[:len(inherited)], clean(inherited), "original history in actual first transport")
            same(sent[index]["content"], text, "complete packet in actual transport")
            if assignment: same(sent[role["message_index"]]["content"], role["content"], "role in actual transport")
    if assignment:
        final = reader.read(phase / "final_delivery.json")
        same(final["authoritative_agent"], "agent1", "fixed integrator")
        same(final["patch_sha256"], receipt["patch_sha256"]["agent1.patch"], "fixed authoritative patch")
        same(final["active_agents"], ["agent1", "agent2"], "two active agents")
        same(final["posthoc_selection"], False, "no post-score choice")
    return {"receipt_sha256": reader.sha(phase / "execution_receipt.json"), "checkpoint_sha256": cp_hash,
        "packet_sha256": packet_hash, "protected_files": len(receipt["protected_files_sha256"]),
        "full_histories_checked": 2, "first_actual_inputs_checked": 0 if protocol["offline_selftest"] else 2}


def score_audit(reader, phase, *, diagnostic=False):
    output = phase / "lead_only_diagnostic" if diagnostic else phase
    score = reader.read(output / "evaluation_summary.json"); attempt = Path(score["attempt_directory"])
    if attempt.parent != output / "evaluation_attempts": raise ValueError("Scoring attempt outside its policy")
    same(score["evaluation_completed"], True, "completed official score")
    same(reader.read(attempt / "evaluation_summary.json"), score, "accepted summary copy")
    native = reader.read(attempt / "official_evaluation.json")
    same(reader.read(attempt / "evaluation.json"), native, "native result copy")
    same(reader.read(output / "evaluation.json"), native, "accepted native result")
    same(score["both_passed"], bool(native.get("both_passed")), "official pass outcome")
    if native.get("error") or native.get("cleanup_error"): raise ValueError("Infrastructure error accepted as a task result")
    entry = reader.read(attempt / "evaluation_input.json"); manifest = entry["input_manifest"]
    same(reader.read(attempt / "integrity.json"), {"unchanged": True, "before": manifest, "after": manifest}, "scoring integrity")
    for info in manifest["protected_files"].values(): reader.info(info)
    for info in manifest["patches"].values(): reader.info(info)
    for name, checksum in manifest["evaluator_sources"].items(): same(reader.sha(ROOT / name), checksum, "scoring source")
    for i, feature in enumerate(score["features"], 1):
        same(score[f"feature_{feature}_passed"], bool((native.get(f"feature{i}") or {}).get("passed")), "feature pass")
    capture = reader.read(attempt / "evaluated_delivery.json")
    same(capture["official_result_sha256"], digest(native), "captured native result")
    reader.info(score["evaluated_delivery"]["manifest"])
    for call in capture["calls"]: reader.info(call["patch"])
    selected = capture.get("selected_call_indices", [c["index"] for c in capture["calls"]])
    if capture["delivered_patch"]:
        reader.info(capture["delivered_patch"])
        for i in selected: same(capture["calls"][i - 1]["patch"]["sha256"], capture["delivered_patch"]["sha256"], "actually tested deliverable")
        for i in selected:
            call = capture["calls"][i - 1]; feature_index = score["features"].index(call["feature"]) + 1
            value = native.get(f"feature{feature_index}") or {}
            same(call["passed"], value.get("passed"), "captured feature result")
            same(call["test_output_sha256"], hashlib.sha256(value.get("test_output", value.get("output", "")).encode()).hexdigest(), "captured test output")
    if diagnostic or phase.name == "integrator":
        same(score["primary_outcome"], not diagnostic, "primary versus sidecar")
        same(score["submission_policy"], "diagnostic_agent1_only" if diagnostic else "authoritative_agent1_only", "scoring policy")
        same(capture["selection"], "predesignated_agent1", "fixed delivery selection")
        same(capture["peer_fallback"], False, "no peer fallback")
        same(len(capture["calls"]), 2, "both feature tests")
    else:
        merge = native.get("merge") or {}
        fallback = merge.get("strategy") == "solo-agent1" and native.get("both_passed") is True
        same(score["lead_patch_fallback_used"], fallback, "native accepted fallback")
    return {"summary_sha256": reader.sha(output / "evaluation_summary.json"), "official_success": score["both_passed"],
        "selection": capture["selection"], "native_calls": len(capture["calls"]),
        "fallback_attempted": any(c["native_patch_name"] == "patch1.patch" for c in capture["calls"]),
        "fallback_accepted": capture["selection"] == "native_lead_fallback", "primary_outcome": not diagnostic}


def collect(suite, partial=False):
    suite = suite.resolve(strict=True); reader = Reader()
    protocol = reader.read(suite / "protocol.json")
    state = reader.read(suite / "status.json")
    summary = reader.read(suite / "summary.json") if (suite / "summary.json").exists() else {"rows": [], "skipped_jobs": [], "strongshare_lead_only_diagnostics": []}
    if protocol["version"] != "luna_integrator_v1": raise ValueError("Wrong suite")
    same(protocol["model"], "offline-scripted-model" if protocol["offline_selftest"] else MODEL, "protocol model")
    if not partial and (state["status"] != "completed" or not summary.get("pipeline_completed")):
        raise ValueError("Final audit requires completed primary jobs and diagnostics; use --partial while running")
    expected = {(t, w, r, c) for t in TASKS for w in TIMINGS for r in ((1,) if protocol["offline_selftest"] else (1, 2)) for c in CONDITIONS}
    jobs = protocol["jobs"]; rows = summary["rows"]; skipped = summary["skipped_jobs"]
    if len(jobs) != len(expected) or set(map(identity, jobs)) != expected: raise ValueError("Wrong planned denominator")
    first = (("early", "strongshare"), ("early", "integrator"), ("late", "integrator"), ("late", "strongshare"))
    order = [(t, w, r, c) for t in TASKS
             for r, pairs in enumerate((first,) if protocol["offline_selftest"] else (first, tuple(reversed(first))), 1)
             for w, c in pairs]
    same(list(map(identity, jobs)), order, "preregistered interleaved order")
    completed, terminal = list(map(identity, rows)), list(map(identity, skipped))
    if len(completed) != len(set(completed)) or len(terminal) != len(set(terminal)) or set(completed) & set(terminal) or not (set(completed) | set(terminal)).issubset(expected):
        raise ValueError("Duplicated or unplanned results")
    if not partial and set(completed) | set(terminal) != expected: raise ValueError("Missing primary denominator")
    if "protocol_sha256" in summary: same(summary["protocol_sha256"], digest(protocol), "summary protocol")
    if not partial:
        same(state["completed_jobs"], len(rows), "final status denominator")
        same(state["skipped_jobs"], len(skipped), "final skip denominator")
    for name, checksum in protocol["source_manifest"].items():
        same(reader.sha(ROOT / name), checksum, "current frozen source")
        same(reader.sha(suite / "source_snapshot" / name), checksum, "frozen source snapshot")
    refs, historical = references(reader, protocol); audits = {}; packets = defaultdict(set); checks = []
    for job in jobs:
        run = run_path(suite, job)
        if not (run / "task_config.json").exists(): continue
        audits[identity(job)] = ledger(reader, run, job["condition"], partial=partial and identity(job) not in completed, raw=True)
    known_ledgers = {Path(a["directory"]) / "usage_ledger.json" for a in audits.values() if a["ledger_sha256"]}
    observed = {p for t in TIMINGS for p in stage(suite, t).glob("*/jobs/*/usage_ledger.json")}
    if observed != known_ledgers: raise ValueError("Unexpected ledger outside declared jobs")
    if any(p for t in TIMINGS for p in stage(suite, t).glob("*/prefix/usage_ledger.json")):
        raise ValueError("This experiment must not generate or charge a new prefix")
    for skip in skipped:
        run = run_path(suite, skip); ref = refs[(skip["task"], skip["timing"])]["reference"]
        remaining = number(protocol["total_seconds"][skip["task"]]) - number(ref["record"]["elapsed_seconds"])
        if skip["reason"] == "total_budget_exhausted":
            if remaining > 0: raise ValueError("Positive budget incorrectly skipped")
            continue
        if skip["reason"] not in ("time_budget_exhausted_before_first_query", "budget_exhausted"):
            raise ValueError("Unknown terminal skip")
        receipt = reader.read(run / skip["condition"] / "budget_terminal_receipt.json")
        same(receipt["status"], skip["reason"], "terminal skip receipt")
        same(receipt["protocol_sha256"], digest(protocol), "terminal protocol")
        for relative, checksum in receipt["protected_files_sha256"].items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts: raise ValueError("Unsafe terminal receipt path")
            same(reader.sha(run / relative), checksum, "terminal skip evidence")
        if (run / skip["condition"] / "execution_receipt.json").exists():
            raise ValueError("Completed execution was downgraded to a skip")
    for row in rows:
        job_id = identity(row); run = run_path(suite, row); audit = audits[job_id]; ref = refs[(row["task"], row["timing"])]["reference"]
        same(reader.read(run / "result.json"), row, "result versus umbrella")
        same(row["all_attempt_usage"], audit["all_attempt_usage"], "all branch costs")
        same(row["accepted_usage"], audit["accepted_usage"], "accepted branch costs")
        full = sum_usage([ref["historical_usage"], audit["all_attempt_usage"]])
        same(row["full_task_known_cost_usd"], full["known_cost_usd"], "full-task cost")
        same(row["full_task_unknown_usage_requests"], full["unknown_usage_requests"], "full-task unknown cost")
        elapsed = number(ref["historical_execution_seconds"]) + number(row["execution"]["elapsed_seconds"]) + number(audit["failed_known_seconds"])
        same(row["full_task_execution_seconds"], round(elapsed, 3), "full-task all-attempt time")
        same(row["full_task_time_unknown_attempts"], ref["historical_time_unknown_attempts"] + audit["failed_unknown_times"], "unknown time")
        branch = execution(reader, run, row, protocol, refs[(row["task"], row["timing"])])
        score = score_audit(reader, run / row["condition"])
        same(score["official_success"], row["official_success"], "primary result")
        same(reader.read(run / row["condition"] / "status.json"), row["execution"], "execution summary")
        same(reader.read(run / row["condition"] / "evaluation_summary.json"), row["evaluation"], "evaluation summary")
        packets[(row["task"], row["timing"])].add(branch["packet_sha256"])
        checks.append({**{k: row[k] for k in FIELDS}, "execution": branch, "score": score})
    if any(len(values) != 1 for values in packets.values()): raise ValueError("Different complete packets across same-checkpoint branches")
    diagnostics = []; diag_ids = set()
    for row in summary["strongshare_lead_only_diagnostics"]:
        job = {**row, "condition": "strongshare"}; ident = identity(job)
        if ident not in completed or ident in diag_ids or row["primary_outcome"] is not False: raise ValueError("Invalid diagnostic denominator")
        run = run_path(suite, job)
        same(reader.read(run / "strongshare/lead_only_diagnostic/evaluation_summary.json"), row["evaluation"], "diagnostic summary")
        diagnostics.append({**{k: job[k] for k in FIELDS}, **score_audit(reader, run / "strongshare", diagnostic=True)})
        diag_ids.add(ident)
    if not partial and diag_ids != {x for x in completed if x[-1] == "strongshare"}: raise ValueError("Missing SS sidecars")
    if not partial: same(state["completed_diagnostics"], len(diagnostics), "final sidecar denominator")
    new = sum_usage(a["all_attempt_usage"] for a in audits.values())
    if not partial: same(summary["actual_new_usage"], new, "actual new bill")
    if protocol["offline_selftest"] and new["requests"]: raise ValueError("Offline audit found API requests")
    groups = []
    for task in TASKS:
        for when in TIMINGS:
            for condition in CONDITIONS:
                matches = lambda row: (row["task"], row["timing"], row["condition"]) == (task, when, condition)
                cell = [r for r in rows if matches(r)]
                mean = lambda key: float(sum(number(r[key]) for r in cell) / len(cell)) if cell else None
                group = {"task": task, "timing": when, "condition": condition, "planned": sum(matches(j) for j in jobs),
                    "completed": len(cell), "skipped": sum(matches(j) for j in skipped), "official_passes": sum(r["official_success"] for r in cell),
                    "known_cost_mean": mean("full_task_known_cost_usd"), "execution_seconds_mean": mean("full_task_execution_seconds"),
                    "unknown_usage_requests": sum(r["full_task_unknown_usage_requests"] for r in cell),
                    "unknown_time_attempts": sum(r["full_task_time_unknown_attempts"] for r in cell)}
                groups.append(group)
                saved = next((g for g in summary.get("groups", []) if matches(g)), None)
                if cell:
                    if saved is None: raise ValueError("Missing saved group")
                    same(saved, {"n": len(cell), **{k: group[k] for k in ("official_passes", "known_cost_mean", "execution_seconds_mean", "unknown_usage_requests", "unknown_time_attempts")}}, "group aggregate")
    changed = reader.stable()
    if changed: raise ValueError("Artifacts changed during audit; rerun later: " + ", ".join(changed[:3]))
    return {"schema": "independent_luna_integrator_integrity_v1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "partial_verified" if partial else "completed_verified", "suite_directory": str(suite), "pipeline_state": state["status"],
        "audit_api_calls": 0, "audit_docker_or_scoring_calls": 0, "planned_primary": len(jobs), "completed_primary": len(rows),
        "skipped_primary": len(skipped), "completed_sidecars": len(diagnostics), "source_and_snapshot_hashes_verified": len(protocol["source_manifest"]),
        "groups": groups, "primary_integrity": checks, "diagnostic_sidecars_only": diagnostics,
        "actual_new_usage": new, "saved_summary_usage_at_last_checkpoint": summary.get("actual_new_usage"),
        "failed_attempt_usage": sum_usage(a["failed_attempt_usage"] for a in audits.values()),
        "historical_early_and_extension_usage_not_recharged": historical,
        "ledger_partitions": list(audits.values()), "protocol_sha256": reader.sha(suite / "protocol.json"),
        "verified_file_sha256": {str(p): value for p, value in reader.hashes.items()},
        "interpretation": ["Primary comparison estimates the combined organization and authoritative-delivery policy, not a pure role-prompt effect.",
            "Two agents remain active in both arms; peer computation and every interrupted attempt are included.",
            "All new spending is branch-only. Historical early and late extension costs are included per comparison row but never recharged to this bill.",
            "Late elapsed already contains early elapsed. Only failed late-extension time is additionally included.",
            "Lead-only SS diagnostics are prespecified sidecars and never replace primary scores or enter their denominator.",
            "Unknown requests remain unknown; known costs are lower bounds where unknown usage exists.",
            "Artifact history can detect archived completed executions; it cannot prove the absence of deliberately deleted unsaved runs.",
            "Partial audits are point-in-time reads. In-flight requests may be unpriced and saved summary totals can lag current ledgers."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(); report = collect(args.suite_dir, args.partial)
    if args.output:
        target = args.output.absolute()
        if target.parent != args.suite_dir.resolve() or not re.fullmatch(r"audit_[A-Za-z0-9_]+\.json", target.name):
            raise ValueError("Output must be a NEW audit_*.json in this umbrella suite")
        with target.open("x") as f: json.dump(report, f, ensure_ascii=False, indent=2); f.write("\n")
    compact = {k: report[k] for k in ("status", "planned_primary", "completed_primary", "completed_sidecars", "source_and_snapshot_hashes_verified", "actual_new_usage", "failed_attempt_usage", "groups")}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
