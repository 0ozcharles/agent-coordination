"""Fixed integrator versus fresh StrongShare on four immutable Luna states.

Both agents work in either condition. The treatment changes organization and
authoritative delivery together; it is not a pure prompt-role causal estimate.
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
    incomplete_seconds, verify_execution_receipt)
from run_repeats import read_json, run_command
from run_solo_smoke import configure
from run_task_expansion import add_usage, file_hash, write_fixed
from run_luna_collaboration import validate_budget_terminal, EXHAUSTED_STATES, VARIANT
import run_luna_timing as timing

RUNS = ROOT / "artifacts/handoff_expansion_runs"
PROOF = ROOT / "artifacts/luna_integrator_offline_validation.json"
VERSION = "luna_integrator_v1"
INTEGRATOR_VARIANT = "designated_integrator_v1"
MODEL = "gpt-6-luna"
TASK_KEYS = timing.TASK_KEYS
TIMINGS = ("early", "late")
CONDITIONS = ("strongshare", "integrator")
TOTAL_SECONDS = timing.TOTAL_SECONDS
NEW_SOURCES = ("run_luna_integrator.py", "luna_integrator_worker.py", "luna_integrator_evaluator.py")


def source_manifest():
    return {**timing.source_manifest(), **{name:file_hash(ROOT/name) for name in NEW_SOURCES}}


def checkpoint_references(offline=False):
    origin = (Path(read_json(timing.PROOF)["suite_directory"]) if offline else RUNS/timing.VERSION)
    protocol = read_json(origin/"protocol.json")
    if (protocol["offline_selftest"] != offline or read_json(origin/"status.json")["status"] != "completed"
            or (not offline and protocol["model"] != MODEL)):
        raise ValueError("Timing reference is not the completed matching model/mode")
    timing.verify_sources(protocol)
    for name,expected in protocol["source_manifest"].items():
        if file_hash(origin/"source_snapshot"/name)!=expected: raise ValueError("Historical source snapshot changed")
    references={}
    for key in TASK_KEYS:
        early=protocol["early_references"][key]
        early_failed=incomplete_seconds(Path(early["directory"]),"prefix")
        references[key]={}
        for when in TIMINGS:
            source=Path(early["directory"]) if when=="early" else timing.stage_suite(origin,"late")/key/"prefix"
            record=early["record"] if when=="early" else timing.extension_record(source,get_task(key),protocol)
            if record["status"]!="completed": raise ValueError("Required immutable checkpoint is unavailable")
            late_failed=timing.failed_extension_seconds(source) if when=="late" else {"known_seconds":0,"unknown_attempts":0}
            historical=[early["record"]["all_attempt_usage"]]
            if when=="late": historical.append(record["all_attempt_usage"])
            references[key][when]={"directory":str(source),"record":record,
                "historical_usage":add_usage(historical),
                "historical_execution_seconds":record["elapsed_seconds"]+early_failed["known_seconds"]+late_failed["known_seconds"],
                "historical_time_unknown_attempts":early_failed["unknown_attempts"]+late_failed["unknown_attempts"],
                "origin_protocol_sha256":file_hash(origin/"protocol.json"),
                "origin_summary_sha256":file_hash(origin/"summary.json"),
                "origin_status_sha256":file_hash(origin/"status.json")}
    return references


def schedule(offline=False):
    first=(("early","strongshare"),("early","integrator"),("late","integrator"),("late","strongshare"))
    return [{"task":key,"timing":when,"repeat":repeat,"condition":condition}
            for key in TASK_KEYS for repeat,order in enumerate((first,) if offline else (first,tuple(reversed(first))),1)
            for when,condition in order]


def stage_suite(suite,when):
    if when not in TIMINGS: raise ValueError("Unknown timing")
    return suite.with_name(suite.name+"_"+when)


def job_path(suite,job):
    if job["task"] not in TASK_KEYS or job["condition"] not in CONDITIONS or job["repeat"] not in (1,2):
        raise ValueError("Invalid integrator comparison job")
    return stage_suite(suite,job["timing"])/job["task"]/"jobs"/f"{job['task']}_{job['timing']}_r{job['repeat']:02d}_{job['condition']}"


def remaining_seconds(task,record):
    return timing.remaining_seconds(task,record)


def protocol_for(offline=False):
    original=read_json(RUNS/timing.VERSION/"protocol.json")
    fields=("provider","endpoint","reasoning_effort","temperature","top_p","max_completion_tokens",
            "service_tier","prompt_cache_options","pricing_usd_per_million")
    return {"version":VERSION,"offline_selftest":offline,"model":"offline-scripted-model" if offline else MODEL,
        **{k:original[k] for k in fields},"tasks":{k:get_task(k).as_dict() for k in TASK_KEYS},
        "jobs":schedule(offline),"checkpoint_references":checkpoint_references(offline),"total_seconds":TOTAL_SECONDS,
        "condition_versions":{"strongshare":VARIANT,"integrator":INTEGRATOR_VARIANT},
        "intervention":"Both native agents continue from original histories and receive the same complete StrongShare packet/live evidence. Integrator adds a fixed organization instruction: agent1 implements its feature and integrates both requirements into the sole authoritative final patch; agent2 continues its feature and publishes/communicates reusable work. No retirement, automatic merge, repair, or post-score choice.",
        "primary_scoring":"Fresh StrongShare retains unchanged native merge/lead-fallback scoring. Integrator always tests the predesignated agent1 publication using the same pinned feature tests and native single-patch filtering; no agent2 substitute. The comparison estimates this joint organization-and-delivery policy, not a pure role-prompt effect.",
        "diagnostic":"After every paid execution ends, independently score each fresh StrongShare agent1 publication with the same feature tests, saved only as a lead-only sidecar. Never replace the native StrongShare score or select the better score.",
        "time_rule":"Same total allowance as the previous timing study. Subtract the immutable accepted checkpoint elapsed time, including late extension, from each continuation. Restore and handoff consume the remainder. In-flight calls may finish after deadline. Failed infrastructure attempts retain all cost and time; no resampling completed task failures.",
        "cost_rule":"Actual new spend includes only all sixteen branch ledgers, including interrupted attempts. No new prefix or extension. Full-task comparison adds immutable historical early work and late extension when applicable; never sum comparison values as a bill. Unknown usage/time remains unknown.",
        "order_rule":"Within each task, first repeat early SS/I then late I/SS; second repeat reverses this order. Actual implicit cache is shared, not a cold-cache experiment.",
        "interpretation":"Exploratory policy comparison on two task pairs, two nested states each, two repetitions per cell. Historical Consolidate and old StrongShare scores are background only; primary comparison uses the sixteen new branches.",
        "money_limit":None,"request_limit":None,"source_manifest":source_manifest()}


def verify_sources(protocol):
    if protocol["source_manifest"]!=source_manifest() or protocol["checkpoint_references"]!=checkpoint_references(protocol["offline_selftest"]):
        raise RuntimeError("Frozen integrator source or historical checkpoint changed")


def verify_preflight():
    proof=read_json(PROOF,{})
    if (not proof.get("passed") or proof.get("model_api_calls")!=0 or proof.get("completed_jobs")!=8 or proof.get("completed_diagnostics")!=4
            or proof.get("source_manifest")!=source_manifest()
            or proof.get("real_checkpoint_references")!=checkpoint_references(False)):
        raise RuntimeError("Exact-source offline proof of eight scored branches required")


def configure_run(run,job,protocol,suite):
    ref=protocol["checkpoint_references"][job["task"]][job["timing"]]
    config={"task_key":job["task"],"task":get_task(job["task"]).as_dict(),**{k:job[k] for k in ("condition","timing","repeat")},
        "condition_version":protocol["condition_versions"][job["condition"]],"experiment_version":VERSION,
        "offline_selftest":protocol["offline_selftest"],"model":protocol["model"],"protocol_sha256":digest(protocol),
        "integrator_suite":str(suite),"execution_seconds":remaining_seconds(job["task"],ref["record"]),
        "initialization_included_in_budget":True,"checkpoint_sha256":ref["record"]["checkpoint_sha256"],
        "submission_policy":"authoritative_agent1_only" if job["condition"]=="integrator" else "native_cooperative_merge"}
    run.mkdir(parents=True,exist_ok=True);write_fixed(run/"task_config.json",config)
    return config


def verify_execution(phase,task,condition,freeze=False):
    if condition=="strongshare": return verify_execution_receipt(phase,task,condition)
    if condition!="integrator": raise ValueError("Unknown execution condition")
    from luna_integrator_worker import freeze_execution_receipt, verify_worker_artifacts
    receipt=(freeze_execution_receipt if freeze else verify_worker_artifacts)(phase,task)
    write_fixed(phase.parent.parent.parent/"packet_record.json",{
        "checkpoint_sha256":digest(read_json(phase.parent/"checkpoint.json")),
        "handoff_packet_sha256":read_json(phase/"status.json")["handoff_packet_sha256"]})
    return receipt


def model_phase(run,task,condition,protocol,suite,lock_fd):
    phase=run/condition;saved=read_json(phase/"status.json",{})
    if saved.get("status")=="completed":
        verify_execution(phase,task,condition);cleanup_phase(run,condition);return saved
    if saved.get("status") in EXHAUSTED_STATES:
        validate_budget_terminal(run,task,condition,protocol);cleanup_phase(run,condition);return saved
    if not protocol["offline_selftest"] and not os.environ.get("OPENAI_API_KEY","").strip():
        raise RuntimeError("OPENAI_API_KEY absent; no API call made")
    archive_incomplete(run,condition);phase.mkdir();write_json(phase/"source_manifest.json",protocol["source_manifest"])
    config=read_json(run/"task_config.json")
    print(f"{task.key}: {config['timing']} {run.name}/{condition}",flush=True)
    try:
        code=run_command([sys.executable,"-B","-u",str(Path(__file__).resolve()),"--worker","--run-dir",str(run),"--suite-dir",str(suite)],
                         phase/"console.log",config["execution_seconds"]+300,lock_fd)
    finally: cleanup_phase(run,condition)
    saved=read_json(phase/"status.json",{})
    if code or saved.get("status") not in ("completed",*EXHAUSTED_STATES):
        raise RuntimeError("Integrator comparison execution paused: "+str(phase))
    if saved["status"]=="completed": verify_execution(phase,task,condition,freeze=True)
    else: validate_budget_terminal(run,task,condition,protocol)
    return saved


def evaluate(run,task,condition,lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase=run/condition;receipt=verify_execution(phase,task,condition)
    script="luna_collaboration_evaluator.py" if condition=="strongshare" else "luna_integrator_evaluator.py"
    code=run_command([sys.executable,"-B",str(ROOT/script),"--run-dir",str(phase)],phase/"evaluation_console.log",720,lock_fd,env=evaluator_environment())
    result=read_json(phase/"evaluation_summary.json",{})
    if code or not result.get("evaluation_completed"): raise RuntimeError("Evaluation paused; saved model execution will be reused")
    if receipt!=verify_execution(phase,task,condition): raise RuntimeError("Execution changed during scoring")
    return result


def evaluate_diagnostic(run,task,lock_fd):
    from evaluate_solo_smoke import evaluator_environment
    phase=run/"strongshare";before=verify_execution(phase,task,"strongshare")
    primary_hash=file_hash(phase/"evaluation_summary.json")
    code=run_command([sys.executable,"-B",str(ROOT/"luna_integrator_evaluator.py"),"--run-dir",str(phase),"--diagnose-strongshare-lead"],
                     phase/"lead_diagnostic_console.log",720,lock_fd,env=evaluator_environment())
    score=read_json(phase/"lead_only_diagnostic/evaluation_summary.json",{})
    if code or not score.get("evaluation_completed"): raise RuntimeError("Lead-only diagnostic paused; primary execution and score are retained")
    if before!=verify_execution(phase,task,"strongshare") or primary_hash!=file_hash(phase/"evaluation_summary.json"):
        raise RuntimeError("Diagnostic changed original execution or primary score")
    return score


def build_row(run,job,ref,execution,score):
    current=usage(run);historical=ref["historical_usage"];failed=incomplete_seconds(run,job["condition"])
    return {**job,"run_directory":str(run),"model":MODEL,"checkpoint_sha256":ref["record"]["checkpoint_sha256"],
        "official_success":bool(score["both_passed"]),"execution":execution,"evaluation":score,
        "accepted_usage":usage(run,job["condition"]),"all_attempt_usage":current,"historical_checkpoint_usage":historical,
        "full_task_known_cost_usd":historical["known_cost_usd"]+current["known_cost_usd"],
        "full_task_unknown_usage_requests":historical["unknown_usage_requests"]+current["unknown_usage_requests"],
        "full_task_execution_seconds":round(ref["historical_execution_seconds"]+execution["elapsed_seconds"]+failed["known_seconds"],3),
        "full_task_time_unknown_attempts":ref["historical_time_unknown_attempts"]+failed["unknown_attempts"],
        "remaining_execution_allowance_seconds":remaining_seconds(job["task"],ref["record"]),"total_execution_allowance_seconds":TOTAL_SECONDS[job["task"]]}


def comparison_groups(rows):
    groups=[]
    for task in TASK_KEYS:
        for when in TIMINGS:
            for condition in CONDITIONS:
                selected=[r for r in rows if (r["task"],r["timing"],r["condition"])==(task,when,condition)]
                if not selected: continue
                groups.append({"task":task,"timing":when,"condition":condition,"n":len(selected),
                    "official_passes":sum(r["official_success"] for r in selected),
                    "known_cost_mean":sum(r["full_task_known_cost_usd"] for r in selected)/len(selected),
                    "unknown_usage_requests":sum(r["full_task_unknown_usage_requests"] for r in selected),
                    "execution_seconds_mean":sum(r["full_task_execution_seconds"] for r in selected)/len(selected),
                    "unknown_time_attempts":sum(r["full_task_time_unknown_attempts"] for r in selected)})
    return groups


def summary_for(suite,protocol,rows,skipped,diagnostics=None):
    diagnostics=diagnostics or []
    ledgers=[p for when in TIMINGS for p in stage_suite(suite,when).glob("*/jobs/*/usage_ledger.json")]
    primary_complete=len(rows)+len(skipped)==len(protocol["jobs"])
    expected=sum(r["condition"]=="strongshare" for r in rows)
    return {"pipeline_completed":primary_complete and len(diagnostics)==expected,"paid_execution_completed":primary_complete,"model":protocol["model"],
        "protocol_sha256":digest(protocol),"planned_jobs":len(protocol["jobs"]),"rows":rows,"skipped_jobs":skipped,
        "actual_new_usage":add_usage([usage(p.parent) for p in ledgers]),"groups":comparison_groups(rows),
        "strongshare_lead_only_diagnostics":diagnostics,
        "historical_checkpoint_cost_recharged":False,"interpretation":protocol["interpretation"]}


def supervise(suite,offline=False):
    configure();suite.mkdir(parents=True,exist_ok=True)
    with (ROOT/"artifacts/model_run.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not offline: verify_preflight()
        protocol=protocol_for(offline);write_fixed(suite/"protocol.json",protocol)
        for name,expected in protocol["source_manifest"].items():
            destination=suite/"source_snapshot"/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists() and file_hash(destination)!=expected: raise RuntimeError("Frozen source snapshot changed")
            if not destination.exists(): destination.write_bytes((ROOT/name).read_bytes())
        rows,skipped,diagnostics=[],[],[]
        try:
            for job in protocol["jobs"]:
                verify_sources(protocol);task=get_task(job["task"]);ref=protocol["checkpoint_references"][job["task"]][job["timing"]]
                run=job_path(suite,job);config=configure_run(run,job,protocol,suite)
                if config["execution_seconds"]<=0:
                    skipped.append({**job,"reason":"total_budget_exhausted"});continue
                copy_checkpoint(Path(ref["directory"]),run,task,ref["record"])
                write_json(suite/"status.json",{"status":"running",**job,"completed_jobs":len(rows),"skipped_jobs":len(skipped),"planned_jobs":len(protocol["jobs"])})
                execution=model_phase(run,task,job["condition"],protocol,suite,lock.fileno())
                if execution["status"] in EXHAUSTED_STATES:
                    skipped.append({**job,"reason":execution["status"],"run_directory":str(run)})
                else:
                    score=evaluate(run,task,job["condition"],lock.fileno());row=build_row(run,job,ref,execution,score)
                    rows.append(row);write_json(run/"result.json",row)
                    print(f"{job['task']} {job['timing']} {job['condition']} r{job['repeat']}: official={row['official_success']}",flush=True)
                write_json(suite/"summary.json",summary_for(suite,protocol,rows,skipped,diagnostics))
            for row in rows:
                if row["condition"]!="strongshare": continue
                verify_sources(protocol)
                write_json(suite/"status.json",{"status":"running","stage":"lead_only_diagnostic","task":row["task"],"timing":row["timing"],"repeat":row["repeat"],"completed_jobs":len(rows),"completed_diagnostics":len(diagnostics)})
                score=evaluate_diagnostic(Path(row["run_directory"]),get_task(row["task"]),lock.fileno())
                diagnostics.append({**{k:row[k] for k in ("task","timing","repeat")},"run_directory":row["run_directory"],"primary_outcome":False,"evaluation":score})
                print(f"Lead-only diagnostic {row['task']} {row['timing']} r{row['repeat']}: {score['both_passed']}",flush=True)
                write_json(suite/"summary.json",summary_for(suite,protocol,rows,skipped,diagnostics))
            verify_sources(protocol);summary=summary_for(suite,protocol,rows,skipped,diagnostics);write_json(suite/"summary.json",summary)
            if offline:
                if len(rows)!=8 or len(diagnostics)!=4 or skipped or summary["actual_new_usage"]["requests"]: raise RuntimeError("Offline proof requires eight branches, four fixed-lead diagnostics and zero API calls")
                for row in rows:
                    outcomes=row["execution"].get("outcomes",{})
                    if set(outcomes)!={"agent1","agent2"} or any(o.get("status")!="Submitted" for o in outcomes.values()):
                        raise RuntimeError("Both offline agents must submit in every condition")
                write_json(PROOF,{"passed":True,"model_api_calls":0,"completed_jobs":8,"completed_diagnostics":4,"source_manifest":protocol["source_manifest"],
                    "real_checkpoint_references":checkpoint_references(False),"suite_directory":str(suite)})
            write_json(suite/"status.json",{"status":"completed","completed_jobs":len(rows),"skipped_jobs":len(skipped),"planned_jobs":len(protocol["jobs"]),"completed_diagnostics":len(diagnostics)})
            return 0
        except (Exception,KeyboardInterrupt) as exc:
            write_json(suite/"summary.json",summary_for(suite,protocol,rows,skipped,diagnostics))
            write_json(suite/"status.json",{"status":"paused","completed_jobs":len(rows),"skipped_jobs":len(skipped),"error":safe_error(exc)})
            print("Integrator comparison paused: "+safe_error(exc),flush=True);return 2


def worker(run,suite):
    protocol=read_json(suite/"protocol.json");verify_sources(protocol)
    jobs=[j for j in protocol["jobs"] if job_path(suite,j)==run]
    if len(jobs)!=1: raise ValueError("Unplanned integrator comparison branch")
    job=jobs[0];task=get_task(job["task"]);ref=protocol["checkpoint_references"][job["task"]][job["timing"]]
    config=read_json(run/"task_config.json")
    # Reconstruct the entire expected immutable config instead of trusting path labels.
    if configure_run(run,job,protocol,suite)!=config: raise ValueError("Worker configuration changed")
    if config["execution_seconds"]<=0: raise ValueError("No continuation allowance")
    copy_checkpoint(Path(ref["directory"]),run,task,ref["record"])
    if job["condition"]=="strongshare":
        from luna_collaboration_worker import phase_worker
    else:
        from luna_integrator_worker import phase_worker
    return phase_worker(run,job["condition"],task,offline=protocol["offline_selftest"],execution_seconds=config["execution_seconds"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-selftest",action="store_true");parser.add_argument("--verify-preflight",action="store_true")
    parser.add_argument("--suite-dir",type=Path);parser.add_argument("--worker",action="store_true");parser.add_argument("--run-dir",type=Path)
    args=parser.parse_args()
    if args.verify_preflight: verify_preflight();return 0
    name="luna_integrator_offline_"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") if args.offline_selftest else VERSION
    suite=(args.suite_dir or RUNS/name).resolve()
    if suite.parent!=RUNS.resolve() or not re.fullmatch(r"luna_integrator_[A-Za-z0-9_]+",suite.name): raise ValueError("Invalid integrator suite path")
    if args.worker: return worker(args.run_dir.resolve(strict=True),suite)
    return supervise(suite,args.offline_selftest)


if __name__=="__main__": raise SystemExit(main())
