"""A small, dependency-free entrypoint; imports no model or Docker clients."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import census, merge, s1, s2


def find_root():
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for path in candidates:
        if (path / "configs/project.json").is_file() and (path / "experiments/m0").is_dir():
            return path
    raise ValueError("Run this command inside an agent-coordination checkout, or install it editable.")


def project_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Registry paths must stay inside the project.")
    return path


def registry(root):
    return json.loads((root / "configs/project.json").read_text())


def read_results(root, study):
    live = project_path(root, study["suite"]) / "summary.json"
    if live.is_file():
        return json.loads(live.read_text()), "local experiment records"
    snapshot = study.get("snapshot")
    if snapshot and project_path(root, snapshot).is_file():
        return json.loads(project_path(root, snapshot).read_text()), "committed result snapshot"
    raise ValueError("This study's results are local-only. Request its artifact bundle from a teammate.")


def show_results(data, source):
    print(f"Source: {source}")
    groups = data.get("comparison_groups", [])
    if not groups:
        print("This historical study uses a different result schema; open its summary.json.")
        return
    print("Task | Condition | Passed | Mean continuation cost (includes retries)")
    for row in groups:
        cost = row.get("remaining_known_cost_mean")
        amount = "not recorded" if cost is None else f"${cost:.5f}"
        unknown = row.get("unknown_usage_requests", 0)
        suffix = f" + {unknown} requests with unknown usage" if unknown else ""
        print(f"{row['task']} | {row['condition']} | {row['successes']}/{row['n']} | {amount}{suffix}")
    usage = data.get("actual_new_usage")
    if usage:
        print(f"Actual new spend: ${usage['known_cost_usd']:.5f} known; "
              f"{usage['unknown_usage_requests']} unknown-usage requests.")
    print("Historical shared prefixes are excluded from actual new spend.")


def execution_command(root, study, mode, python=None):
    # A venv interpreter normally symlinks outside the checkout.
    interpreter = Path(python).expanduser().absolute() if python else (
        root / "experiments/m0/CooperBench/.venv/bin/python")
    command = [str(interpreter), "-B", "-u", str(project_path(root, study["entrypoint"]))]
    if mode == "offline":
        command.append("--offline-selftest")
    else:
        command += ["--suite-dir", str(project_path(root, study["suite"]))]
    return command


def s1_command(root, args):
    config_path = args.config.resolve()
    config = s1.load_config(config_path)
    output = project_path(root, config["output"])
    if args.freeze:
        rows = s1.diagnose(s1.collect(config, output))
        kept, dropped = s1.screen(config, rows)
        for task in dropped:
            print(f"drop {task['repo']}/{task['task_id']} {task['features']}: {task['reason']}")
        target = project_path(root, str(args.freeze))
        target.write_text(json.dumps(s1.main_config(config, kept), ensure_ascii=False, indent=2) + "\n")
        print(f"Kept {len(kept)} of {len(config['tasks'])} pairs; wrote {args.freeze}")
        return 0
    if args.report:
        if not s1.collect(config, output):
            raise ValueError(f"No evaluated runs under {config['output']}.")
        s1.analyze(config, output)
        return 0
    jobs = s1.plan(config)
    done = sum((s1.log_dir(output, job) / "eval.json").is_file() for job in jobs)
    print(f"{config['study']}: {config['question']}")
    print(f"Model {config['model']}; {len(config['tasks'])} tasks x {config['repeats']} repeats "
          f"x {len(config.get('settings', s1.SETTINGS))} settings = {len(jobs)} jobs, {done} already done.")
    print("For scale: M0 gpt-6-luna runs cost about $0.004-0.03 each.")
    code = launch(root, args, "agent_coordination.s1", [str(config_path)], output,
                  args.workers or config.get("workers", 1))
    if code is not None and s1.collect(config, output):
        s1.analyze(config, output)
    return code or 0


def launch(root, args, module, module_args, output, workers):
    """Run a module under the CooperBench venv, split over worker processes; None if preview only."""
    interpreter = Path(args.python).expanduser().absolute() if args.python else (
        root / "experiments/m0/CooperBench/.venv/bin/python")
    command = [str(interpreter), "-B", "-u", "-m", module, *module_args]
    print(shlex.join(command))
    if not args.execute:
        print("Preview only. Add --execute to run the missing jobs.")
        return None
    if not interpreter.is_file():
        raise ValueError("CooperBench Python is missing; see docs/DEVELOPMENT.md.")
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    if workers <= 1:
        return subprocess.call(command, cwd=root, env=env)
    output.mkdir(parents=True, exist_ok=True)
    processes = []
    for index in range(workers):
        log = open(output / f"worker_{index}.log", "a")
        processes.append((subprocess.Popen(command + ["--shard", f"{index}/{workers}"], cwd=root, env=env,
                                           stdout=log, stderr=subprocess.STDOUT), log))
    print(f"Started {workers} workers; logs in {output.relative_to(root)}/worker_*.log", flush=True)
    codes = [process.wait() for process, _ in processes]
    for _, log in processes:
        log.close()
    return max(codes)


def merge_command(root, args):
    config_path = args.config.resolve()
    config = merge.load_config(config_path)
    if args.set not in config["sets"]:
        raise ValueError(f"Unknown set; the config defines {', '.join(config['sets'])}.")
    output = project_path(root, config["output"]) / args.set
    if args.report:
        merge.report(config, root, args.set)
        return 0
    jobs = merge.select(config, root, args.set)
    done = sum((merge.target_dir(output, job) / "merge.json").is_file() for job in jobs)
    print(f"{config['study']} [{args.set}]: {config['question']}")
    print(f"{len(jobs)} conflicted coop runs, {done} already merged; methods: {', '.join(config['methods'])}.")
    code = launch(root, args, "agent_coordination.merge", [str(config_path), args.set], output, args.workers or 1)
    if code is not None:
        merge.report(config, root, args.set)
    return code or 0


def s2_command(root, args):
    config_path = args.config.resolve()
    config = s2.load_config(config_path)
    output = project_path(root, config["output"])
    if args.report:
        s2.report(config, root)
        return 0
    jobs = s2.plan(config, root)
    done = sum((s1.log_dir(root / s2.arm_config(config, root, name)["output"], job) / "eval.json").is_file()
               for name, job in jobs)
    print(f"{config['study']}: {config['question']}")
    print(f"{len(config['arms'])} arms ({', '.join(config['arms'])}) = {len(jobs)} jobs, {done} already done.")
    if "draft" in config.get("status", ""):
        print(f"Config status: {config['status']}")
    code = launch(root, args, "agent_coordination.s2", [str(config_path)], output,
                  args.workers or config.get("workers", 1))
    if code is not None:
        s2.report(config, root)
    return code or 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent Coordination: browse studies, label historical failures, resume existing experiments.")
    parser.add_argument("--root", type=Path, help="Explicit checkout path")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("list", help="List historical study entrypoints")
    table = subs.add_parser("census", help="All scored historical runs with a failure label")
    table.add_argument("--export", action="store_true", help=f"Rewrite {census.SNAPSHOT} from local artifacts")
    for name in ("show", "results"):
        sub = subs.add_parser(name)
        sub.add_argument("study")
    fresh = subs.add_parser("s1", help="S1 failure census from a config: preview, run, or report")
    fresh.add_argument("config", type=Path)
    fresh.add_argument("--execute", action="store_true", help="Run missing jobs (paid model calls)")
    fresh.add_argument("--report", action="store_true", help="Re-label existing logs; no Docker or API")
    fresh.add_argument("--python", help="CooperBench Python interpreter")
    fresh.add_argument("--freeze", type=Path, help="Apply the screening rule and write the S1 main config here")
    fresh.add_argument("--workers", type=int, help="Parallel worker processes (default: config 'workers' or 1)")
    remerge = subs.add_parser("merge", help="Offline merge test: re-merge conflicted S1 coop runs")
    remerge.add_argument("config", type=Path)
    remerge.add_argument("--set", default="dev", help="Which run set in the config (default: dev)")
    remerge.add_argument("--execute", action="store_true", help="Merge and test (Docker; llm makes paid calls)")
    remerge.add_argument("--report", action="store_true", help="Summarize existing merge records")
    remerge.add_argument("--python", help="CooperBench Python interpreter")
    remerge.add_argument("--workers", type=int, help="Parallel worker processes (default 1)")
    stage2 = subs.add_parser("s2", help="S2 targeted interventions from a config: preview, run, or report")
    stage2.add_argument("config", type=Path)
    stage2.add_argument("--execute", action="store_true", help="Run missing jobs (paid model calls)")
    stage2.add_argument("--report", action="store_true", help="Re-label existing logs; no Docker or API")
    stage2.add_argument("--python", help="CooperBench Python interpreter")
    stage2.add_argument("--workers", type=int, help="Parallel worker processes (default: config 'workers' or 1)")
    run = subs.add_parser("run", help="Print the command; --execute actually launches it")
    run.add_argument("study")
    run.add_argument("--mode", choices=("resume", "offline"), default="resume")
    run.add_argument("--python", help="Experiment Python interpreter")
    run.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve() if args.root else find_root()
        settings = registry(root)
        if args.command == "list":
            for key, study in settings["studies"].items():
                print(f"{key:14} {study['title']}")
            return 0
        if args.command == "census":
            rows, source = census.load(root)
            if args.export:
                if source != "local experiment records":
                    raise ValueError("Exporting needs local artifacts.")
                census.write_snapshot(rows, root / census.SNAPSHOT)
                print(f"Wrote {census.SNAPSHOT}\n")
            census.report(rows, source)
            return 0
        if args.command == "s1":
            return s1_command(root, args)
        if args.command == "merge":
            return merge_command(root, args)
        if args.command == "s2":
            return s2_command(root, args)
        if args.study not in settings["studies"]:
            raise ValueError("Unknown study. Run 'list' to see available names.")
        study = settings["studies"][args.study]
        if args.command == "show":
            print(json.dumps(study, ensure_ascii=False, indent=2))
            print("Historical checkpoint and image assets are not included in Git.")
            return 0
        if args.command == "results":
            show_results(*read_results(root, study))
            return 0
        command = execution_command(root, study, args.mode, args.python)
        print(shlex.join(command), flush=True)
        if not args.execute:
            print("Preview only. Nothing was launched. Add --execute when ready.")
            return 0
        if not Path(command[0]).is_file():
            raise ValueError("Experiment Python is missing; see docs/DEVELOPMENT.md.")
        if not project_path(root, study["suite"]).is_dir():
            raise ValueError("Historical suite is absent. Restore its artifacts before resuming or testing it.")
        return subprocess.call(command, cwd=root / "experiments/m0")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

