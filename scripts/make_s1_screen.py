"""Write configs/s1_screen.json: a repo-stratified sample of CooperBench lite pairs.

The sample depends only on the subset file, the cap and the seed, never on results.
"""
import json
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[1]
SUBSET = ROOT / "experiments/m0/CooperBench/dataset/subsets/lite.json"
CAP_PER_REPO = 6
SEED = 20261101


def main():
    pairs = {}
    for task in json.loads(SUBSET.read_text())["tasks"]:
        for pair in task["pairs"]:
            pairs.setdefault(task["repo"], []).append(
                {"repo": task["repo"], "task_id": task["task_id"], "features": sorted(pair)})
    rng = random.Random(SEED)
    chosen = []
    for repo in sorted(pairs):
        chosen += rng.sample(pairs[repo], min(CAP_PER_REPO, len(pairs[repo])))
    config = {
        "study": "s1_screen",
        "question": "Which lite pairs are informative (outcomes vary) for the S1 failure census?",
        "decision_rule": "Keep a pair if, over its 2 coop + 2 solo runs, some feature passed at least once "
                         "and not every run passed both features. Freeze the kept list before S1 main.",
        "sampling": {"subset": "lite", "cap_per_repo": CAP_PER_REPO, "seed": SEED},
        "model": "gpt-6-luna",
        "agent": "mini_swe_agent_v2",
        "agent_config": "configs/agents/luna_mini_swe.yaml",
        "settings": ["coop", "solo"],
        "repeats": 2,
        "messaging": True,
        "git": False,
        "tasks": chosen,
        "output": "runs/s1_screen",
    }
    path = ROOT / "configs/s1_screen.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {path.relative_to(ROOT)}: {len(chosen)} pairs, "
          f"{len({(t['repo'], t['task_id']) for t in chosen})} task images")


if __name__ == "__main__":
    main()
