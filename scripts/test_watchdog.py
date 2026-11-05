"""Enforce CooperBench's 600 s test timeout while a runner is working.

CooperBench's Docker backend calls exec_run inside `with ThreadPoolExecutor(...)`. On timeout the
`with` block still waits for the command, so a test that never ends hangs the worker. This script
kills any test (runner.sh) that has run for more than 600 s; the feature then fails, as it would
under the intended timeout. Each kill is appended to the log. Start it after the workers; it exits
when no process matches the pattern.

    python3 scripts/test_watchdog.py runs/upstream/census/timeouts.log [pattern]
"""
import subprocess
import sys
import time

LIMIT = 600
SKIP = {"cooperbench-git", "cooperbench-redis"}
FIND = ("for p in /proc/[0-9]*; do c=$(tr '\\0' ' ' < $p/cmdline 2>/dev/null); "
        "case \"$c\" in *runner.sh*) echo \"${p#/proc/} $c\";; esac; done")


def run(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout


def main(log, pattern="agent_coordination"):
    seen = {}
    while run("pgrep", "-f", pattern).strip():
        now = time.time()
        for line in run("docker", "ps", "--format", "{{.Names}} {{.Image}}").splitlines():
            name, image = line.split(" ", 1)
            if name in SKIP:
                continue
            for proc in run("docker", "exec", name, "sh", "-c", FIND).splitlines():
                pid, _, command = proc.partition(" ")
                if not pid.isdigit():  # a command line with an embedded newline spills onto extra lines
                    continue
                start = seen.setdefault((name, pid), now)
                if now - start > LIMIT:
                    run("docker", "exec", name, "sh", "-c", "kill -9 -1")
                    with open(log, "a") as out:
                        out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} killed after {int(now - start)}s: "
                                  f"{image} {command.strip()}\n")
                    seen.pop((name, pid))
        time.sleep(30)


if __name__ == "__main__":
    main(*sys.argv[1:])
