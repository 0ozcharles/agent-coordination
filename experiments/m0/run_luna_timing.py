"""Paired early/late handoff pilot. Historical results never enter new scores.

One fixed two-round ordinary-cooperation extension per task derives late from
the immutable early Luna checkpoint. Two separate flat stage suites preserve
the original per-task checkpoint/packet invariant of the native evaluator.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import math
import os
from pathlib import Path
import re
import sys

from budget_guard import write_json
from cross_task_specs import ROOT, get_task
from run_baselines import cleanup_phase, digest, safe_error, usage
from run_handoff_expansion import (archive_incomplete, copy_checkpoint,
    incomplete_seconds, prefix_record, verify_execution_receipt)
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed
from run_luna_collaboration import (source_manifest as parent_sources,
    verify_luna_prefix, validate_budget_terminal, EXHAUSTED_STATES, VARIANT)

RUNS = ROOT / "artifacts/handoff_expansion_runs"
REFERENCE = RUNS / "luna_collaboration_v1"
PROOF = ROOT / "artifacts/luna_timing_offline_validation.json"
VERSION = "luna_timing_v1"
MODEL = "gpt-6-luna"
TASK_KEYS = ("llama_index17070_f1_f3", "pillow68_f1_f2")
TOTAL_SECONDS = dict(zip(TASK_KEYS, (1466.261, 1269.899)))
TIMINGS = ("early", "late")
CONDITIONS = ("strongshare", "consolidate")
ADDITIONAL_ROUNDS = 2
EXTENSION_HORIZON = 300.0
NEW_SOURCES = ("run_luna_timing.py", "luna_timing_prefix.py")


def source_manifest():
    return {**parent_sources(), **{name: file_hash(ROOT/name) for name in NEW_SOURCES}}


def early_references(offline=False):
    directory = (Path(read_json(ROOT/"artifacts/luna_collaboration_offline_validation.json")["suite_directory"])
                 if offline else REFERENCE)
    protocol = read_json(directory/"protocol.json")
    if protocol["offline_selftest"] != offline or (not offline and protocol["model"] != MODEL):
        raise ValueError("Early reference model or mode changed")
    for relative, expected in protocol["source_manifest"].items():
        if file_hash(ROOT/relative) != expected or file_hash(directory/"source_snapshot"/relative) != expected:
            raise RuntimeError("Frozen early checkpoint sources changed: " + relative)
    result = {}
    for key in TASK_KEYS:
        prefix = directory/key/"prefix"
        record = verify_luna_prefix(prefix, get_task(key), protocol)
        if record["status"] != "completed":
            raise ValueError("Early reference checkpoint is not complete")
        result[key] = {"directory": str(prefix), "record": record,
                       "protocol_sha256": file_hash(directory/"protocol.json")}
    return result


def schedule(offline=False):
    first = (("early", "strongshare"), ("late", "consolidate"),
             ("late", "strongshare"), ("early", "consolidate"))
    return [{"task": key, "timing": timing, "repeat": repeat, "condition": condition}
            for key in TASK_KEYS
            for repeat, order in enumerate((first,) if offline else (first, tuple(reversed(first))), 1)
            for timing, condition in order]


def stage_suite(suite, timing):
    if timing not in TIMINGS:
        raise ValueError("Unknown handoff timing")
    return suite.with_name(suite.name + "_" + timing)


def job_path(suite, job):
    if job["task"] not in TASK_KEYS or job["condition"] not in CONDITIONS or job["repeat"] not in (1,2):
        raise ValueError("Invalid job")
    return stage_suite(suite, job["timing"])/job["task"]/"jobs"/f"{job['task']}_{job['timing']}_r{job['repeat']:02d}_{job['condition']}"


def remaining_seconds(task, record):
    elapsed = record.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (float,int)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("Invalid prefix elapsed time")
    return round(TOTAL_SECONDS[task] - elapsed, 3)


def protocol_for(offline=False):
    settings = read_json(REFERENCE/"protocol.json")
    fields = ("provider", "endpoint", "reasoning_effort", "temperature", "top_p", "max_completion_tokens",
              "service_tier", "prompt_cache_options", "pricing_usd_per_million")
    return {"version": VERSION, "model": "offline-scripted-model" if offline else MODEL,
        "offline_selftest": offline, **{k: settings[k] for k in fields},
        "tasks": {k: get_task(k).as_dict() for k in TASK_KEYS}, "jobs": schedule(offline),
        "early_references": early_references(offline), "total_seconds": TOTAL_SECONDS,
        "additional_paired_rounds": ADDITIONAL_ROUNDS, "extension_horizon_seconds": EXTENSION_HORIZON,
        "timing_rule": "Early is the frozen first paired boundary with source changes by both unfinished agents. Late is exactly two further ordinary-cooperation paired steps from that same state, without new evidence or role messages. No test-based choice or replacement checkpoint.",
        "unreachable_rule": "Any submission/exit before or at late, loss of eligibility, or extension deadline produces a terminal unavailable late timing. Skip its four branches; early branches continue. Never resample a completed unavailable trajectory.",
        "time_rule": "The same total task allowance applies to both timings. Late extension includes restore and snapshot. Continuation remainder subtracts accepted early and extension elapsed, including restore/handoff. In-flight calls may finish after deadline. Infrastructure retries use the same allowance, with all failed time/cost reported separately and in operational totals.",
        "comparison": "Sixteen fresh continuations only: within each task compare late versus early for SS and C, plus the change in C-minus-SS between timings. Two task pairs and one ordinary-cooperation trajectory per task; repeats are not independent tasks.",
        "cost_rule": "Actual new spend counts each late extension ledger once plus every new continuation ledger including interrupted attempts. Historical early work is separately reported, never recharged to new spend. Per-task-equivalent cost/time adds historical early work, late extension only for late, and the continuation. No summing per-task-equivalent values as billing total.",
        "cache_rule": "Fixed reversed order across repeats; actual shared implicit-cache costs, not isolated cold starts. Historical eight continuation scores and four Solo scores are background only.",
        "money_limit": None, "request_limit": None, "source_manifest": source_manifest()}


def verify_sources(protocol):
    if protocol["source_manifest"] != source_manifest() or protocol["early_references"] != early_references(protocol["offline_selftest"]):
        raise RuntimeError("Frozen timing sources or early reference changed")


def verify_preflight():
    proof = read_json(PROOF, {})
    if (not proof.get("passed") or proof.get("model_api_calls") != 0 or proof.get("completed_jobs") != 8
            or proof.get("completed_extensions") != 2 or proof.get("source_manifest") != source_manifest()
            or proof.get("real_early_references") != early_references(False)):
        raise RuntimeError("Exact-source offline two-extension/eight-continuation proof required")


def configure_run(run, task, condition, timing, protocol, seconds, suite, record=None):
    config = {"task_key":task.key, "task":task.as_dict(), "condition":condition, "timing":timing,
        "condition_version":VARIANT, "experiment_version":VERSION,
        "offline_selftest":protocol["offline_selftest"], "model":protocol["model"],
        "protocol_sha256":digest(protocol), "timing_suite":str(suite),
        "execution_seconds":seconds, "initialization_included_in_budget":True,
        "early_checkpoint_sha256":protocol["early_references"][task.key]["record"]["checkpoint_sha256"]}
    if record:
        config["checkpoint_sha256"] = record["checkpoint_sha256"]
    run.mkdir(parents=True,exist_ok=True)
    write_fixed(run/"task_config.json",config)


def extension_record(run, task, protocol):
    from luna_timing_prefix import verify_receipt
    verify_receipt(run, task, early_prefix_dir=Path(protocol["early_references"][task.key]["directory"]))
    record = prefix_record(run,task)
    status = read_json(run/"prefix/status.json")
    early = protocol["early_references"][task.key]["record"]
    if (abs(record["elapsed_seconds"]-early["elapsed_seconds"]-status["extension_elapsed_seconds"]) > .003
            or read_json(run/"prefix/source_manifest.json") != protocol["source_manifest"]):
        raise ValueError("Late prefix timing/source mismatch")
    return record


def model_phase(run, task, condition, protocol, suite, lock_fd):
    phase = run/condition
    saved = read_json(phase/"status.json",{})
    terminal = ("completed","checkpoint_not_reached") if condition=="prefix" else ("completed",*EXHAUSTED_STATES)
    def verify():
        if condition=="prefix": extension_record(run,task,protocol)
        elif saved["status"]=="completed": verify_execution_receipt(phase,task,condition)
        else: validate_budget_terminal(run,task,condition,protocol)
    if saved.get("status") in terminal:
        verify();cleanup_phase(run,condition);return saved
    if not protocol["offline_selftest"] and not os.environ.get("OPENAI_API_KEY","").strip():
        raise RuntimeError("OPENAI_API_KEY absent; no API call made")
    archive_incomplete(run,condition)
    phase.mkdir()
    write_json(phase/"source_manifest.json",protocol["source_manifest"])
    config = read_json(run/"task_config.json")
    print(f"{task.key}: {config['timing']} {run.name}/{condition}",flush=True)
    command=[sys.executable,"-B","-u",str(Path(__file__).resolve()),"--worker","--run-dir",str(run),"--suite-dir",str(suite)]
    try:
        code=run_command(command,phase/"console.log",config["execution_seconds"]+300,lock_fd)
    finally:
        cleanup_phase(run,condition)
    saved=read_json(phase/"status.json",{})
    if code or saved.get("status") not in terminal:
        raise RuntimeError("Timing execution paused: "+str(phase))
    verify();return saved


def evaluate(run,task,condition,lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase=run/condition
    receipt=verify_execution_receipt(phase,task,condition)
    code=run_command([sys.executable,"-B",str(ROOT/"luna_collaboration_evaluator.py"),"--run-dir",str(phase)],
                     phase/"evaluation_console.log",720,lock_fd,env=evaluator_environment())
    result=read_json(phase/"evaluation_summary.json",{})
    if code or not result.get("evaluation_completed"):
        raise RuntimeError("Evaluation paused; completed execution will be reused")
    if receipt != verify_execution_receipt(phase,task,condition):
        raise RuntimeError("Execution changed during evaluation")
    return result


def failed_extension_seconds(run):
    total, unknown=0.,0
    for attempt in (run/"failed_attempts").glob("prefix_*"):
        if not attempt.is_dir(): continue
        value=read_json(attempt/"status.json",{}).get("extension_elapsed_seconds")
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0:
            total+=value
        else: unknown+=1
    return {"known_seconds":total,"unknown_attempts":unknown}


def build_row(run,job,early,record,extension,execution,score):
    branch=usage(run); late=job["timing"]=="late"
    extension_usage=usage(extension) if late else {"known_cost_usd":0,"unknown_usage_requests":0}
    early_failed=incomplete_seconds(Path(early["directory"]),"prefix")
    extension_failed=failed_extension_seconds(extension) if late else {"known_seconds":0,"unknown_attempts":0}
    branch_failed=incomplete_seconds(run,job["condition"])
    historical=early["record"]["all_attempt_usage"]
    full_time=record["elapsed_seconds"]+early_failed["known_seconds"]+extension_failed["known_seconds"]+execution["elapsed_seconds"]+branch_failed["known_seconds"]
    return {**job,"run_directory":str(run),"model":MODEL,"checkpoint_sha256":record["checkpoint_sha256"],
        "official_success":bool(score["both_passed"]),"execution":execution,"evaluation":score,
        "accepted_usage":usage(run,job["condition"]),"all_attempt_usage":branch,
        "historical_early_usage":historical,"late_extension_usage":extension_usage,
        "full_task_known_cost_usd":historical["known_cost_usd"]+extension_usage["known_cost_usd"]+branch["known_cost_usd"],
        "full_task_unknown_usage_requests":historical["unknown_usage_requests"]+extension_usage["unknown_usage_requests"]+branch["unknown_usage_requests"],
        "full_task_execution_seconds":round(full_time,3),
        "full_task_time_unknown_attempts":early_failed["unknown_attempts"]+extension_failed["unknown_attempts"]+branch_failed["unknown_attempts"],
        "total_execution_allowance_seconds":TOTAL_SECONDS[job["task"]],
        "remaining_execution_allowance_seconds":remaining_seconds(job["task"],record)}


def timing_effects(rows):
    result=[]
    for task in TASK_KEYS:
        groups={}
        for timing in TIMINGS:
            for condition in CONDITIONS:
                selected=[r for r in rows if (r["task"],r["timing"],r["condition"])==(task,timing,condition)]
                if not selected: continue
                groups[(timing,condition)]={"n":len(selected),"success_rate":sum(r["official_success"] for r in selected)/len(selected),
                    "known_cost_mean":sum(r["full_task_known_cost_usd"] for r in selected)/len(selected),
                    "unknown_usage_requests":sum(r["full_task_unknown_usage_requests"] for r in selected),
                    "execution_seconds_mean":sum(r["full_task_execution_seconds"] for r in selected)/len(selected),
                    "unknown_time_attempts":sum(r["full_task_time_unknown_attempts"] for r in selected)}
        if len(groups)!=4:
            result.append({"task":task,"estimable":False});continue
        complete={"success_rate":True,
            "known_cost_mean":all(g["unknown_usage_requests"]==0 for g in groups.values()),
            "execution_seconds_mean":all(g["unknown_time_attempts"]==0 for g in groups.values())}
        difference=lambda a,b:{metric:a[metric]-b[metric] if complete[metric] else None for metric in complete}
        delta={timing:difference(groups[(timing,"consolidate")],groups[(timing,"strongshare")]) for timing in TIMINGS}
        result.append({"task":task,"estimable":True,"groups":[{"timing":t,"condition":c,**v} for (t,c),v in groups.items()],
            "consolidate_minus_strongshare":delta,
            "late_minus_early_effect":difference(delta["late"],delta["early"]),
            "late_minus_early_by_condition":{condition:difference(groups[("late",condition)],groups[("early",condition)]) for condition in CONDITIONS},
            "cost_effect_complete":complete["known_cost_mean"],"time_effect_complete":complete["execution_seconds_mean"],
            "interpretation":"Descriptive effect on available completed branches of one nested trajectory; inspect each group n and skipped jobs. Unknown cost/time makes that effect unresolved, not zero. No significance claim."})
    return result


def summary_for(suite,protocol,rows,skipped,late_records):
    ledgers=[]
    for timing in TIMINGS:
        stage=stage_suite(suite,timing)
        ledgers+=list(stage.glob("*/jobs/*/usage_ledger.json"))
        if timing=="late": ledgers+=list(stage.glob("*/prefix/usage_ledger.json"))
    return {"pipeline_completed":len(rows)+len(skipped)==len(protocol["jobs"]),"model":protocol["model"],
        "protocol_sha256":digest(protocol),"planned_jobs":len(protocol["jobs"]),"rows":rows,"skipped_jobs":skipped,
        "late_records":late_records,"actual_new_usage":add_usage([usage(p.parent) for p in ledgers]),
        "historical_early_usage_not_recharged":add_usage([v["record"]["all_attempt_usage"] for v in protocol["early_references"].values()]),
        "timing_effects":timing_effects(rows)}


def supervise(suite,offline=False):
    configure();suite.mkdir(parents=True,exist_ok=True)
    with (ROOT/"artifacts/model_run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not offline: verify_preflight()
        protocol=protocol_for(offline);write_fixed(suite/"protocol.json",protocol)
        for name,expected in protocol["source_manifest"].items():
            destination=suite/"source_snapshot"/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists() and file_hash(destination)!=expected: raise RuntimeError("Frozen snapshot changed")
            if not destination.exists(): destination.write_bytes((ROOT/name).read_bytes())
        rows,skipped,late_records=[],[],{}
        try:
            for key in TASK_KEYS:
                verify_sources(protocol);task=get_task(key);early=protocol["early_references"][key]
                late=stage_suite(suite,"late")/key/"prefix"
                horizon=min(EXTENSION_HORIZON,remaining_seconds(key,early["record"]))
                configure_run(late,task,"prefix","late",protocol,horizon,suite)
                write_json(suite/"status.json",{"status":"running","task":key,"stage":"late_extension","completed_jobs":len(rows)})
                model_phase(late,task,"prefix",protocol,suite,lock.fileno())
                late_records[key]=extension_record(late,task,protocol)
                for job in [j for j in protocol["jobs"] if j["task"]==key]:
                    verify_sources(protocol)
                    record=early["record"] if job["timing"]=="early" else late_records[key]
                    seconds=remaining_seconds(key,record)
                    if record["status"]!="completed" or seconds<=0:
                        skipped.append({**job,"reason":"late_checkpoint_unavailable" if record["status"]!="completed" else "total_budget_exhausted"})
                    else:
                        run=job_path(suite,job);source=Path(early["directory"]) if job["timing"]=="early" else late
                        configure_run(run,task,job["condition"],job["timing"],protocol,seconds,suite,record)
                        copy_checkpoint(source,run,task,record)
                        write_json(suite/"status.json",{"status":"running",**job,"completed_jobs":len(rows),"skipped_jobs":len(skipped),"planned_jobs":len(protocol["jobs"])})
                        execution=model_phase(run,task,job["condition"],protocol,suite,lock.fileno())
                        if execution["status"] in EXHAUSTED_STATES:
                            skipped.append({**job,"reason":execution["status"],"run_directory":str(run)})
                        else:
                            score=evaluate(run,task,job["condition"],lock.fileno())
                            row=build_row(run,job,early,record,late,execution,score)
                            rows.append(row);write_json(run/"result.json",row)
                            print(f"{key} {job['timing']} {job['condition']} r{job['repeat']}: official={row['official_success']}",flush=True)
                    write_json(suite/"summary.json",summary_for(suite,protocol,rows,skipped,late_records))
            verify_sources(protocol)
            summary=summary_for(suite,protocol,rows,skipped,late_records);write_json(suite/"summary.json",summary)
            if offline:
                if len(rows)!=8 or skipped or summary["actual_new_usage"]["requests"] or any(r["status"]!="completed" for r in late_records.values()):
                    raise RuntimeError("Offline proof requires two late checkpoints, eight scored branches and zero API calls")
                for row in rows:
                    active=("agent1",) if row["condition"]=="consolidate" else ("agent1","agent2")
                    outcomes=row["execution"].get("outcomes",{})
                    if set(outcomes)!=set(active) or any(outcomes[a].get("status")!="Submitted" for a in active):
                        raise RuntimeError("Offline proof requires every active agent to submit")
                write_json(PROOF,{"passed":True,"model_api_calls":0,"completed_jobs":8,"completed_extensions":2,
                    "source_manifest":protocol["source_manifest"],"real_early_references":early_references(False),"suite_directory":str(suite)})
            write_json(suite/"status.json",{"status":"completed","completed_jobs":len(rows),"skipped_jobs":len(skipped),"planned_jobs":len(protocol["jobs"])})
            return 0
        except (Exception,KeyboardInterrupt) as exc:
            write_json(suite/"summary.json",summary_for(suite,protocol,rows,skipped,late_records))
            write_json(suite/"status.json",{"status":"paused","completed_jobs":len(rows),"skipped_jobs":len(skipped),"error":safe_error(exc)})
            print("Timing experiment paused: "+safe_error(exc),flush=True);return 2


def worker(run,suite):
    protocol=read_json(suite/"protocol.json");verify_sources(protocol)
    config=read_json(run/"task_config.json");key=config["task_key"];task=get_task(key)
    if key not in TASK_KEYS or config["timing_suite"]!=str(suite) or config["protocol_sha256"]!=digest(protocol):
        raise ValueError("Worker protocol identity differs")
    condition,timing=config["condition"],config["timing"]
    early=protocol["early_references"][key]
    if (config["task"]!=task.as_dict() or config["model"]!=protocol["model"]
            or config["offline_selftest"]!=protocol["offline_selftest"] or config["experiment_version"]!=VERSION
            or config.get("condition_version")!=VARIANT or config.get("initialization_included_in_budget") is not True
            or config["early_checkpoint_sha256"]!=early["record"]["checkpoint_sha256"]):
        raise ValueError("Worker task/model/reference differs")
    if condition=="prefix":
        if run!=stage_suite(suite,"late")/key/"prefix" or timing!="late": raise ValueError("Invalid extension path")
        seconds=min(EXTENSION_HORIZON,remaining_seconds(key,early["record"]))
        if config["execution_seconds"]!=seconds: raise ValueError("Extension horizon differs")
        from luna_timing_prefix import phase_worker
        return phase_worker(run,task,early_prefix_dir=Path(early["directory"]),execution_seconds=seconds,
                            offline=protocol["offline_selftest"],additional_rounds=ADDITIONAL_ROUNDS)
    jobs=[j for j in protocol["jobs"] if job_path(suite,j)==run]
    if len(jobs)!=1 or (jobs[0]["task"],jobs[0]["condition"],jobs[0]["timing"])!=(key,condition,timing):
        raise ValueError("Unplanned timing branch")
    source=Path(early["directory"]) if timing=="early" else stage_suite(suite,"late")/key/"prefix"
    record=early["record"] if timing=="early" else extension_record(source,task,protocol)
    seconds=remaining_seconds(key,record)
    if record["status"]!="completed" or seconds<=0 or config["execution_seconds"]!=seconds or config["checkpoint_sha256"]!=record["checkpoint_sha256"]:
        raise ValueError("Wrong checkpoint or remaining allowance")
    copy_checkpoint(source,run,task,record)
    from luna_collaboration_worker import phase_worker
    return phase_worker(run,condition,task,offline=protocol["offline_selftest"],execution_seconds=seconds)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-selftest",action="store_true")
    parser.add_argument("--verify-preflight",action="store_true")
    parser.add_argument("--suite-dir",type=Path)
    parser.add_argument("--worker",action="store_true")
    parser.add_argument("--run-dir",type=Path)
    args=parser.parse_args()
    if args.verify_preflight: verify_preflight();return 0
    name="luna_timing_offline_"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else VERSION
    suite=(args.suite_dir or RUNS/name).resolve()
    if suite.parent!=RUNS.resolve() or not re.fullmatch(r"luna_timing_[A-Za-z0-9_]+",suite.name): raise ValueError("Invalid timing suite path")
    if args.worker: return worker(args.run_dir.resolve(strict=True),suite)
    return supervise(suite,args.offline_selftest)


if __name__=="__main__":
    raise SystemExit(main())
