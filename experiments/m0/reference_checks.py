# Run with the pinned CooperBench project's Python; no model calls are made.
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Keep framework config local and skip the model-price fetch during evaluator-only checks.
m0_root = Path(__file__).resolve().parent
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(m0_root / "config" / "mini-swe-agent")
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from cooperbench.eval.sandbox import run_patch_test, test_solo

dataset = m0_root / "CooperBench" / "dataset"
task = dataset / "pallets_jinja_task" / "task1621"
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = m0_root / "artifacts" / f"reference_checks_{stamp}"
out.mkdir(parents=True, exist_ok=False)

def save(name, result):
    (out / f"{name}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def checked_call(name, function, **kwargs):
    try:
        result = function(**kwargs)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "stage": name}
        save(name, result)
        raise SystemExit(f"检查无法执行，错误已保存到 {out}") from exc
    save(name, result)
    return result

for feature in (1, 6):
    result = checked_call(
        f"feature{feature}_reference", run_patch_test,
        repo_name="pallets_jinja_task", task_id=1621, feature_id=feature,
        agent_patch=task / f"feature{feature}" / "feature.patch",
        backend="docker", timeout=600, dataset_dir=dataset,
    )
    print(f"feature{feature}:", result.get("passed"), result.get("error"))
    if result.get("error") or result.get("passed") is not True:
        raise SystemExit(f"参考实现检查未通过，先查看 {out}")

result = checked_call(
    "combined_reference", test_solo,
    repo_name="pallets_jinja_task", task_id=1621,
    feature1_id=1, feature2_id=6, patch=task / "combined.patch",
    backend="docker", timeout=600, dataset_dir=dataset,
)
print("combined both_passed:", result.get("both_passed"), result.get("error"))
if result.get("error") or result.get("both_passed") is not True:
    raise SystemExit(f"综合参考实现检查未通过，先查看 {out}")
print("环境与参考评分检查通过，结果保存在", out)
