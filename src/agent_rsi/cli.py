"""A small, dependency-free entrypoint; imports no model or Docker clients."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys


def find_root():
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for path in candidates:
        if (path / "configs/project.json").is_file() and (path / "experiments/m0").is_dir():
            return path
    raise ValueError("Run this command inside an Agent RSI checkout, or install it editable.")


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent RSI: browse studies, inspect results, resume existing experiments.")
    parser.add_argument("--root", type=Path, help="Explicit checkout path")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("list", help="List historical study entrypoints")
    for name in ("show", "results"):
        sub = subs.add_parser(name)
        sub.add_argument("study")
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

