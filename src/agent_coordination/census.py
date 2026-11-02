"""Flatten every scored historical run into one table and label why it failed.

Labels only use what an evaluation record can show; trace-level causes
(context boundaries, missed messages) need a later trace pass.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

ARTIFACTS = "experiments/m0/artifacts"
SNAPSHOT = "results/m0_runs.csv"

COLUMNS = ["batch", "model", "task", "condition", "timing", "repeat", "success",
           "features_passed", "features_total", "passed_features", "failed_features", "merge_status",
           "scoring", "cost_usd", "label"]

# Checked in this order; the first that applies becomes the run's label.
LABELS = {
    "passed": "两项 feature 都通过",
    "infra_error": "评分未完成，不能算作 agent 失败",
    "merge_conflict": "两份 patch 无法自动合并：集成失败",
    "duplicate_patch": "两名 agent 提交了相同 patch 且未通过：重复劳动",
    "capability_gap": "失败的 feature 在同模型、同任务的任何条件下都没通过过：组织调整大概率无效",
    "recoverable": "失败的 feature 在同模型、同任务的其他运行中通过过：换一种组织方式可能救回",
}


def scoring_rule(evaluation):
    if evaluation.get("single_patch_scoring_adapter"):
        return "single_artifact"
    if evaluation.get("lead_patch_fallback_used"):
        return "lead_patch_fallback"
    strategy = evaluation.get("merge_strategy")
    if strategy == "skip-merge-identical":
        return "identical_skip"
    if strategy in (None, "not_applicable"):
        return "single_artifact"
    return f"merged_{strategy}"


def row_cost(row):
    for key in ("accepted_usage", "continuation_usage"):
        usage = row.get(key)
        if isinstance(usage, dict) and usage.get("known_cost_usd") is not None:
            return round(usage["known_cost_usd"], 6)
    return None


def batch_model(summary_path):
    protocol = summary_path.parent / "protocol.json"
    if protocol.is_file():
        return json.loads(protocol.read_text()).get("model")
    return None


def collect(artifacts):
    rows = []
    for path in sorted(Path(artifacts).glob("**/summary.json")):
        if "offline" in path.parent.name:
            continue
        data = json.loads(path.read_text())
        if not isinstance(data.get("rows"), list):
            continue
        default_model = data.get("model") or batch_model(path)
        for raw in data["rows"]:
            if not isinstance(raw, dict) or "official_success" not in raw:
                continue
            evaluation = raw.get("evaluation") or {}
            features = evaluation.get("feature_results") or raw.get("feature_results") or {}
            failed = sorted(k for k, v in features.items() if not v.get("passed"))
            passed = sorted(k for k, v in features.items() if v.get("passed"))
            rows.append({
                "batch": path.parent.name,
                "model": raw.get("model") or default_model or "unknown",
                "task": raw["task"],
                "condition": raw.get("condition") or raw.get("policy"),
                "timing": raw.get("timing") or "",
                "repeat": raw.get("repeat", ""),
                "success": bool(raw["official_success"]),
                "features_passed": len(passed),
                "features_total": len(features),
                "passed_features": " ".join(passed),
                "failed_features": " ".join(failed),
                "merge_status": evaluation.get("merge_status") or "",
                "scoring": scoring_rule(evaluation),
                "cost_usd": row_cost(raw),
                "evaluation_completed": evaluation.get("evaluation_completed", True),
            })
    return rows


def label_runs(rows):
    solved = defaultdict(set)
    for row in rows:
        solved[row["model"], row["task"]].update(row["passed_features"].split())
    for row in rows:
        row["label"] = label(row, solved[row["model"], row["task"]])
    return rows


def label(row, solved_features):
    if row["success"]:
        return "passed"
    if not row.get("evaluation_completed", True):
        return "infra_error"
    if row["merge_status"] == "conflicts" and row["scoring"].startswith("merged_"):
        return "merge_conflict"
    if row["merge_status"] == "identical":
        return "duplicate_patch"
    failed = set(row["failed_features"].split())
    if failed and not failed & solved_features:
        return "capability_gap"
    return "recoverable"


def write_snapshot(rows, path):
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_snapshot(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["success"] = row["success"] == "True"
    return rows


def load(root):
    artifacts = root / ARTIFACTS
    if artifacts.is_dir():
        return label_runs(collect(artifacts)), "local experiment records"
    snapshot = root / SNAPSHOT
    if snapshot.is_file():
        return read_snapshot(snapshot), "committed census snapshot"
    raise ValueError("No local artifacts and no census snapshot; request the artifact bundle.")


def report(rows, source):
    print(f"Source: {source}; {len(rows)} scored runs, "
          f"{len({(r['model'], r['task']) for r in rows})} model×task cells.\n")
    print("Failure labels by condition")
    conditions = sorted({r["condition"] for r in rows})
    counts = Counter((r["condition"], r["label"]) for r in rows)
    print("label".ljust(16) + "".join(c[:11].rjust(12) for c in conditions))
    for name in LABELS:
        print(name.ljust(16) + "".join(str(counts[c, name]).rjust(12) for c in conditions))
    print()
    for name, meaning in LABELS.items():
        print(f"  {name}: {meaning}")

    print("\nPasses / runs by task and condition (all batches pooled; scoring rules differ)")
    cells = defaultdict(lambda: [0, 0])
    for r in rows:
        cell = cells[r["model"], r["task"], r["condition"]]
        cell[0] += r["success"]
        cell[1] += 1
    tasks = sorted({(r["model"], r["task"]) for r in rows})
    print("model / task".ljust(42) + "".join(c[:11].rjust(12) for c in conditions) + "  informative")
    for model, task in tasks:
        values = [cells.get((model, task, c)) for c in conditions]
        rates = {v[0] / v[1] for v in values if v}
        informative = "yes" if len(rates) > 1 or rates - {0.0, 1.0} else "no"
        name = f"{model.split('/')[-1]} / {task}"[:41]
        print(name.ljust(42) + "".join(
            (f"{v[0]}/{v[1]}" if v else "-").rjust(12) for v in values) + f"  {informative}")
    print("\n'informative' = outcomes differ across or within conditions; "
          "always-pass or always-fail cells cannot tell organizations apart.")
