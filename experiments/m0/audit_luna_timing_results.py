"""Read-only timing-result and all-attempt ledger aggregation.

Requires a completed umbrella suite. Reads both sibling stage suites and the
immutable early prefix references; historical continuation scores are never
loaded. No model, Docker, scorer, or production supervisor is imported. An
optional output must be a new derived file, never an existing artifact.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SUITE = ROOT / "artifacts/handoff_expansion_runs/luna_timing_v1"
TASKS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
TIMINGS = ("early", "late")
CONDITIONS = ("strongshare", "consolidate")
IDENTITY = ("task", "timing", "repeat", "condition")


def read(path):
    if path.is_symlink() or not path.is_file(): raise ValueError("Expected ordinary saved file: " + str(path))
    return json.loads(path.read_text())


def sha(path):
    if path.is_symlink() or not path.is_file(): raise ValueError("Expected ordinary saved file: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected finite nonnegative recorded number")
    return Decimal(str(value))


def same(actual, expected, name):
    """Compare independently derived numeric objects without float sum noise."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict): raise ValueError("Wrong saved object: " + name)
        for key, value in expected.items(): same(actual[key], value, name + "." + key)
    elif isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        if isinstance(actual, bool) or not isinstance(actual, (int, float, Decimal)) or not math.isfinite(actual) or abs(Decimal(str(actual))-Decimal(str(expected))) > Decimal("0.000000001"):
            raise ValueError("Recorded aggregate differs: " + name)
    elif actual != expected:
        raise ValueError("Recorded aggregate differs: " + name)


def usage(rows):
    return {"requests":len(rows),
        "known_cost_usd":float(sum((number(r["actual_cost_usd"]) for r in rows if r.get("actual_cost_usd") is not None),Decimal(0))),
        "unknown_usage_requests":sum(r.get("actual_cost_usd") is None for r in rows),
        "prompt_tokens":sum(r.get("prompt_tokens",0) for r in rows),
        "completion_tokens":sum(r.get("completion_tokens",0) for r in rows)}


def sum_usage(records):
    records=list(records)
    return {key:(float(sum((Decimal(str(r[key])) for r in records),Decimal(0))) if key=="known_cost_usd"
                 else sum(r[key] for r in records))
            for key in ("requests","known_cost_usd","unknown_usage_requests","prompt_tokens","completion_tokens")}


def ledger_audit(run, phase, *, extension=False):
    path=run/"usage_ledger.json";rows=read(path)["requests"] if path.exists() else []
    if any(r.get("model")!="gpt-6-luna" or not r.get("phase","").startswith(phase+":") for r in rows):
        raise ValueError("Unexpected model/phase in " + str(path))
    numbers=[r["request_number"] for r in rows]
    if len(numbers)!=len(set(numbers)): raise ValueError("Duplicate request numbers")
    accepted=[r for r in rows if not r.get("excluded_from_baseline")]
    excluded=[r for r in rows if r.get("excluded_from_baseline")]
    if any(r.get("status")=="in_flight" for r in accepted): raise ValueError("Completed suite still has unsettled active requests")
    grouped=defaultdict(list)
    for r in excluded:
        if not r.get("archived_attempt"): raise ValueError("Excluded request lacks its attempt")
        grouped[r["archived_attempt"]].append(r)
    dirs={p.name.removeprefix(phase+"_"):p for p in (run/"failed_attempts").glob(phase+"_*") if p.is_dir()}
    if not set(grouped).issubset(dirs): raise ValueError("Excluded requests lost archived evidence")
    attempts=[]
    for stamp,directory in sorted(dirs.items()):
        status=read(directory/"status.json") if (directory/"status.json").exists() else {}
        elapsed=status.get("extension_elapsed_seconds" if extension else "elapsed_seconds")
        valid=isinstance(elapsed,(int,float)) and not isinstance(elapsed,bool) and math.isfinite(elapsed) and elapsed>=0
        attempts.append({"directory":str(directory),"status":status.get("status"),"error_type":status.get("error_type"),
            "usage":usage(grouped[stamp]),"known_seconds":elapsed if valid else None,"unknown_time":not valid})
    return {"directory":str(run),"phase":phase,"ledger_sha256":sha(path) if path.exists() else None,
        "all_attempt_usage":usage(rows),"accepted_usage":usage(accepted),"failed_attempt_usage":usage(excluded),
        "failed_attempts":attempts,"failed_known_seconds":sum(a["known_seconds"] or 0 for a in attempts),
        "failed_unknown_times":sum(a["unknown_time"] for a in attempts)}


def stage(suite,timing):return suite.with_name(suite.name+"_"+timing)
def identity(job):return tuple(job[k] for k in IDENTITY)
def run_path(suite,job):
    return stage(suite,job["timing"])/job["task"]/"jobs"/f"{job['task']}_{job['timing']}_r{job['repeat']:02d}_{job['condition']}"


def group_results(rows,planned,skipped):
    groups=[]
    for task in TASKS:
        for timing in TIMINGS:
            for condition in CONDITIONS:
                matches=lambda r:(r["task"],r["timing"],r["condition"])==(task,timing,condition)
                selected=[r for r in rows if matches(r)];skips=[r for r in skipped if matches(r)]
                costs=[Decimal(str(r["full_task_known_cost_usd"])) for r in selected]
                times=[r["full_task_execution_seconds"] for r in selected]
                mean=lambda values:float(sum(values)/len(values)) if values else None
                groups.append({"task":task,"timing":timing,"condition":condition,
                    "planned":sum(matches(r) for r in planned),"completed":len(selected),"skipped":len(skips),
                    "official_passes":sum(r["official_success"] for r in selected),
                    "official_failures":sum(not r["official_success"] for r in selected),
                    "success_rate":mean([int(r["official_success"]) for r in selected]),
                    "known_cost_mean":mean(costs),"known_cost_range":[float(min(costs)),float(max(costs))] if costs else None,
                    "unknown_usage_requests":sum(r["full_task_unknown_usage_requests"] for r in selected),
                    "execution_seconds_mean":mean(times),"execution_seconds_range":[min(times),max(times)] if times else None,
                    "unknown_time_attempts":sum(r["full_task_time_unknown_attempts"] for r in selected),
                    "official_evaluation_seconds_mean":mean([r["evaluation"]["elapsed_seconds"] for r in selected]),
                    "skip_reasons":dict(Counter(r["reason"] for r in skips))})
    return groups


def effects(groups):
    """Task-level two-by-two contrasts, unresolved if a required cost/time is unknown."""
    results=[]
    for task in TASKS:
        cells={(g["timing"],g["condition"]):g for g in groups if g["task"]==task}
        if any(not g["completed"] for g in cells.values()):
            results.append({"task":task,"estimable":False});continue
        complete={"success_rate":True,"known_cost_mean":all(not g["unknown_usage_requests"] for g in cells.values()),
            "execution_seconds_mean":all(not g["unknown_time_attempts"] for g in cells.values())}
        subtract=lambda a,b:{m:a[m]-b[m] if valid else None for m,valid in complete.items()}
        within={t:subtract(cells[(t,"consolidate")],cells[(t,"strongshare")]) for t in TIMINGS}
        results.append({"task":task,"estimable":True,"consolidate_minus_strongshare":within,
            "late_minus_early_effect":subtract(within["late"],within["early"]),
            "late_minus_early_by_condition":{c:subtract(cells[("late",c)],cells[("early",c)]) for c in CONDITIONS},
            "cost_effect_complete":complete["known_cost_mean"],"time_effect_complete":complete["execution_seconds_mean"]})
    return results


def collect(suite):
    suite=suite.resolve(strict=True)
    protocol,state,summary=[read(suite/name) for name in ("protocol.json","status.json","summary.json")]
    if (protocol.get("version")!="luna_timing_v1" or protocol.get("model") not in ("gpt-6-luna","offline-scripted-model")
            or state.get("status")!="completed" or summary.get("pipeline_completed") is not True):
        raise ValueError("Final audit requires the completed timing suite")
    same(summary["protocol_sha256"],digest(protocol),"protocol digest")
    planned,rows,skipped=protocol["jobs"],summary["rows"],summary["skipped_jobs"]
    repeats=(1,) if protocol["offline_selftest"] else (1,2)
    expected={(t,s,r,c) for t in TASKS for s in TIMINGS for r in repeats for c in CONDITIONS}
    ids=[identity(r) for r in planned];completed=[identity(r) for r in rows];terminal=[identity(r) for r in skipped]
    if (len(ids)!=len(expected) or set(ids)!=expected or len(completed)!=len(set(completed))
            or len(terminal)!=len(set(terminal)) or set(completed)&set(terminal) or set(completed)|set(terminal)!=expected):
        raise ValueError("Duplicated, unplanned, missing, or overlapping timing outcomes")
    same(state["completed_jobs"],len(rows),"completed jobs");same(state["skipped_jobs"],len(skipped),"skipped jobs")
    for name,checksum in protocol["source_manifest"].items():
        if sha(ROOT/name)!=checksum or sha(suite/"source_snapshot"/name)!=checksum:raise ValueError("Source changed: "+name)
    early_audits,extension_audits,job_audits={}, {}, {}
    historical_hashes=0
    for task in TASKS:
        reference=protocol["early_references"][task];origin=Path(reference["directory"]);record=reference["record"]
        if sha(origin.parent.parent/"protocol.json")!=reference["protocol_sha256"]:raise ValueError("Early protocol changed")
        for name,checksum in record["files_sha256"].items():
            if sha(origin/name)!=checksum:raise ValueError("Historical early artifact changed")
            historical_hashes+=1
        early_audits[task]=ledger_audit(origin,"prefix")
        same(record["all_attempt_usage"],early_audits[task]["all_attempt_usage"],"historical early usage")
        late=stage(suite,"late")/task/"prefix";audit=ledger_audit(late,"prefix",extension=True)
        extension_audits[task]=audit;late_record=summary["late_records"][task]
        same(late_record["all_attempt_usage"],audit["all_attempt_usage"],"extension usage")
        same(late_record["accepted_usage"],audit["accepted_usage"],"accepted extension usage")
        execution=read(late/"prefix/status.json")
        if abs(number(late_record["elapsed_seconds"])-number(record["elapsed_seconds"])-number(execution["extension_elapsed_seconds"]))>Decimal(".003"):
            raise ValueError("Late cumulative time does not equal early plus extension")
    for job in planned:
        run=run_path(suite,job)
        if (run/"task_config.json").exists():job_audits[identity(job)]=ledger_audit(run,job["condition"])
    expected_ledgers={Path(a["directory"])/"usage_ledger.json" for a in (*extension_audits.values(),*job_audits.values()) if a["ledger_sha256"]}
    actual_ledgers=set()
    for timing in TIMINGS:
        actual_ledgers.update(stage(suite,timing).glob("*/jobs/*/usage_ledger.json"))
        actual_ledgers.update(stage(suite,timing).glob("*/prefix/usage_ledger.json"))
    if actual_ledgers!=expected_ledgers:raise ValueError("Unexpected or missing ledger outside declared new runs")
    for row in rows:
        task,timing=row["task"],row["timing"];run=run_path(suite,row);branch=job_audits[identity(row)]
        if Path(row["run_directory"])!=run or read(run/"result.json")!=row:raise ValueError("Result and umbrella summary differ")
        early=protocol["early_references"][task]["record"];ext=extension_audits[task] if timing=="late" else None
        record=summary["late_records"][task] if ext else early
        same(row["all_attempt_usage"],branch["all_attempt_usage"],"branch all-attempt usage")
        same(row["accepted_usage"],branch["accepted_usage"],"branch accepted usage")
        charges=[early_audits[task]["all_attempt_usage"],branch["all_attempt_usage"]]+([ext["all_attempt_usage"]] if ext else [])
        full=sum_usage(charges)
        same(row["full_task_known_cost_usd"],full["known_cost_usd"],"full-task cost")
        same(row["full_task_unknown_usage_requests"],full["unknown_usage_requests"],"full-task unknowns")
        time=number(record["elapsed_seconds"])+number(early_audits[task]["failed_known_seconds"])+number(row["execution"]["elapsed_seconds"])+number(branch["failed_known_seconds"])
        if ext:time+=number(ext["failed_known_seconds"])
        same(row["full_task_execution_seconds"],round(time,3),"full-task execution seconds")
        same(row["full_task_time_unknown_attempts"],early_audits[task]["failed_unknown_times"]+branch["failed_unknown_times"]+(ext["failed_unknown_times"] if ext else 0),"unknown times")
        same(row["remaining_execution_allowance_seconds"],round(number(protocol["total_seconds"][task])-number(record["elapsed_seconds"]),3),"remaining allowance")
        same(row["checkpoint_sha256"],record["checkpoint_sha256"],"timing checkpoint")
        execution,score=[read(run/row["condition"]/name) for name in ("status.json","evaluation_summary.json")]
        if execution!=row["execution"] or score!=row["evaluation"] or not score["evaluation_completed"] or bool(score["both_passed"])!=row["official_success"]:
            raise ValueError("Result no longer matches saved execution/score")
    audits=[*extension_audits.values(),*job_audits.values()]
    new=sum_usage(a["all_attempt_usage"] for a in audits)
    same(summary["actual_new_usage"],new,"actual new spend")
    historical=sum_usage(a["all_attempt_usage"] for a in early_audits.values())
    same(summary["historical_early_usage_not_recharged"],historical,"historical spend")
    if protocol["offline_selftest"] and new["requests"]:raise ValueError("Offline suite contains paid requests")
    groups=group_results(rows,planned,skipped);contrasts=effects(groups)
    for actual,expected in zip(summary["timing_effects"],contrasts):same(actual,expected,"timing effects")
    return {"schema":"independent_luna_timing_aggregation_v1","generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "status":"completed_verified","offline_selftest":protocol["offline_selftest"],"suite_directory":str(suite),
        "audit_api_calls":0,"audit_docker_or_evaluator_calls":0,"protocol_sha256":digest(protocol),"summary_sha256":sha(suite/"summary.json"),
        "source_and_snapshot_hashes_verified":len(protocol["source_manifest"]),"historical_early_artifact_hashes_verified":historical_hashes,
        "planned_continuations":len(planned),"completed_continuations":len(rows),"terminal_skips":len(skipped),
        "group_results":groups,"timing_effects":contrasts,"actual_new_usage":new,
        "new_extension_usage_counted_once":sum_usage(a["all_attempt_usage"] for a in extension_audits.values()),
        "new_continuation_usage":sum_usage(a["all_attempt_usage"] for a in job_audits.values()),
        "new_failed_attempt_usage":sum_usage(a["failed_attempt_usage"] for a in audits),
        "historical_early_usage_not_recharged":historical,
        "failed_attempts":[{"run":a["directory"],**attempt} for a in audits for attempt in a["failed_attempts"]],
        "ledger_partitions":audits,"skipped_jobs":skipped,
        "interpretation":["Only newly collected early/late branches enter this comparison; old eight continuation scores were never loaded.",
            "Actual spending counts each late extension once plus every new branch, including failed attempts. Historical early fees are separate.",
            "Per-branch full-task costs include historical early work, late extension only for late, and all branch attempts. These are not billing totals.",
            "Execution time excludes official scoring. Failed extension contributes extension_elapsed_seconds only, never historical early elapsed again.",
            "Cost/time effects remain null if required usage/duration is unknown. Known means are lower bounds in that case, not complete costs.",
            "Unavailable late checkpoints remain terminal skips; no substitute checkpoint or fabricated zero-success sample is introduced.",
            "Two task pairs with one nested trajectory each; repeats are not independent tasks. Effects are descriptive, not significance claims.",
            "This is aggregate/ledger/hash validation. Raw provider usage, complete restored history, and native evaluated-delivery receipts need the separate final integrity audit."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-dir",type=Path,default=DEFAULT_SUITE)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args();report=collect(args.suite_dir)
    data=json.dumps(report,ensure_ascii=False,indent=2)+"\n"
    if args.output:
        if args.output.exists() or args.output.is_symlink():raise ValueError("Refusing to overwrite existing artifact")
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open("x") as f:f.write(data)
    print(data,end="")


if __name__=="__main__":main()
