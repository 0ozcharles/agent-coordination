import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import census


def run(condition, success, passed, failed, merge="not_applicable", **evaluation):
    features = {**{f: {"passed": True} for f in passed}, **{f: {"passed": False} for f in failed}}
    return {"task": "demo_f1_f2", "condition": condition, "official_success": success,
            "accepted_usage": {"known_cost_usd": 0.01},
            "evaluation": {"feature_results": features, "merge_status": merge, **evaluation}}


class CensusTests(unittest.TestCase):
    def write_batch(self, root, rows, name="demo_v1"):
        batch = root / census.ARTIFACTS / name
        batch.mkdir(parents=True)
        (batch / "summary.json").write_text(json.dumps({"rows": rows}))
        (batch / "protocol.json").write_text(json.dumps({"model": "m"}))

    def test_labels_follow_the_documented_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_batch(root, [
                run("solo", True, ["1", "2"], []),
                run("strongshare", False, [], ["1", "2"], "conflicts", merge_strategy="naive"),
                run("strongshare", False, [], ["1", "2"], "conflicts", lead_patch_fallback_used=True),
                run("strongshare", False, [], ["1", "2"], "identical", merge_strategy="skip-merge-identical"),
                run("consolidate", False, ["1"], ["2"]),
                run("keep", False, [], [], evaluation_completed=False),
            ])
            self.write_batch(root, [run("solo", False, ["1"], ["3"])], name="other_v1")
            self.write_batch(root, [run("solo", True, [], [])], name="demo_offline_1")
            rows, source = census.load(root)
            labels = [r["label"] for r in rows]
            self.assertEqual(source, "local experiment records")
            self.assertEqual(labels, ["passed", "merge_conflict", "recoverable", "duplicate_patch",
                                      "recoverable", "infra_error", "capability_gap"])
            self.assertEqual(rows[0]["model"], "m")

    def test_snapshot_round_trip_serves_checkouts_without_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_batch(root, [run("solo", True, ["1", "2"], [])])
            rows, _ = census.load(root)
            (root / "results").mkdir()
            census.write_snapshot(rows, root / census.SNAPSHOT)
            for path in sorted((root / census.ARTIFACTS).rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            (root / census.ARTIFACTS).rmdir()
            restored, source = census.load(root)
            self.assertEqual(source, "committed census snapshot")
            self.assertIs(restored[0]["success"], True)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                census.report(restored, source)
            self.assertIn("passed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
