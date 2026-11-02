import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import s1

PATCH = "diff --git a/pkg/{0}.py b/pkg/{0}.py\n+change\n"


def config(tasks, repeats=1):
    return {"study": "t", "question": "q", "model": "m", "repeats": repeats, "output": "out",
            "tasks": [{"repo": "r", "task_id": i, "features": [1, 2]} for i in tasks]}


class S1Tests(unittest.TestCase):
    def write(self, output, repeat, setting, task_id, merged, own=None, patches=None, error=False):
        job = {"repeat": repeat, "setting": setting, "repo": "r", "task_id": task_id, "features": [1, 2]}
        path = s1.log_dir(output, job)
        path.mkdir(parents=True)
        (path / "eval.json").write_text(json.dumps({
            "feature1": {"passed": "1" in merged}, "feature2": {"passed": "2" in merged},
            "both_passed": merged == "12", "merge": {"status": "clean"}}))
        status = "Error" if error else "Submitted"
        agents = ({"agent": {"status": status}} if setting == "solo"
                  else {"agents": {"agent1": {"status": status}, "agent2": {"status": "Submitted"}}})
        (path / "result.json").write_text(json.dumps({"total_cost": 0.01, **agents}))
        if own is not None:
            (path / "agent_eval.json").write_text(json.dumps(
                {f: {g: g in own[f] for g in "12"} for f in "12"}))
        for fid, text in (patches or {}).items():
            (path / f"agent{fid}.patch").write_text(text)

    def test_labels_separate_integration_context_and_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            # Task 1: own patches each pass, merge breaks feature 2 -> integration.
            self.write(output, 1, "coop", 1, "1", own={"1": "1", "2": "2"},
                       patches={"1": PATCH.format("a"), "2": PATCH.format("a")})
            # Task 2: agent 2 fails its feature; solo solves it -> context.
            self.write(output, 1, "coop", 2, "1", own={"1": "1", "2": ""})
            self.write(output, 1, "solo", 2, "12")
            # Task 3: agent 2 fails; solo also fails it -> capability.
            self.write(output, 1, "coop", 3, "1", own={"1": "12", "2": ""})
            self.write(output, 1, "solo", 3, "1")
            # Task 4: passes; task 5 never got per-agent scores.
            self.write(output, 1, "coop", 4, "12", own={"1": "1", "2": "2"},
                       patches={"1": PATCH.format("x"), "2": PATCH.format("x")})
            self.write(output, 1, "coop", 5, "")
            # Task 6: an API error must not count as a capability failure, nor feed solo rates.
            self.write(output, 1, "coop", 6, "", own={"1": "", "2": ""}, error=True)
            self.write(output, 1, "solo", 6, "", error=True)
            rows = s1.diagnose(s1.collect(config([1, 2, 3, 4, 5, 6]), output))
            labels = {(r["setting"], r["task_id"]): r["label"] for r in rows}
            self.assertEqual(labels, {
                ("coop", 1): "integration", ("coop", 2): "context", ("solo", 2): "passed",
                ("coop", 3): "capability", ("solo", 3): "solo_failed",
                ("coop", 4): "passed", ("coop", 5): "unscored",
                ("coop", 6): "infra_error", ("solo", 6): "infra_error"})
            first = next(r for r in rows if r["task_id"] == 1)
            self.assertEqual((first["shared_files"], first["patches_identical"]), (1, True))
            third = next(r for r in rows if r["task_id"] == 3 and r["setting"] == "coop")
            self.assertEqual(third["cross_passed"], "1")
            self.assertEqual(third["feature_labels"], "2:capability")

    def test_unpublished_work_is_labelled_submission_and_counts_as_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            # Coop: agent for feature 2 wrote passing code locally but published nothing.
            self.write(output, 1, "coop", 1, "1", own={"1": "1", "2": ""})
            coop = s1.log_dir(output, {"repeat": 1, "setting": "coop", "repo": "r", "task_id": 1, "features": [1, 2]})
            (coop / "local_eval.json").write_text(json.dumps({"1": {"same_as_published": True},
                                                              "2": {"1": False, "2": True}}))
            # Solo on task 2 published nothing but its local work passes both features,
            # so a coop agent failing feature 2 there is a context failure, not capability.
            self.write(output, 1, "solo", 2, "")
            solo = s1.log_dir(output, {"repeat": 1, "setting": "solo", "repo": "r", "task_id": 2, "features": [1, 2]})
            (solo / "local_eval.json").write_text(json.dumps({"solo": {"1": True, "2": True}}))
            self.write(output, 1, "coop", 2, "1", own={"1": "1", "2": ""})
            rows = s1.diagnose(s1.collect(config([1, 2]), output))
            labels = {(r["setting"], r["task_id"]): r["label"] for r in rows}
            self.assertEqual(labels, {("coop", 1): "submission", ("solo", 2): "submission",
                                      ("coop", 2): "context"})

    def test_screen_keeps_only_pairs_with_varying_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for repeat in (1, 2):
                self.write(output, repeat, "solo", 1, "12")             # always passes
                self.write(output, repeat, "solo", 2, "")               # never passes a feature
                self.write(output, repeat, "solo", 3, "12" if repeat == 1 else "1")  # varies
            self.write(output, 1, "solo", 4, "1")                       # one valid run only
            self.write(output, 2, "solo", 4, "", error=True)
            cfg = config([1, 2, 3, 4], repeats=2)
            kept, dropped = s1.screen(cfg, s1.diagnose(s1.collect(cfg, output)))
            self.assertEqual([t["task_id"] for t in kept], [3])
            self.assertEqual({t["task_id"]: t["reason"] for t in dropped},
                             {1: "always passed", 2: "never passed a feature", 4: "too few valid runs"})
            main = s1.main_config({**cfg, "decision_rule": "x"}, kept)
            self.assertEqual((main["repeats"], len(main["tasks"]), "decision_rule" in main), (3, 1, False))

    def test_exit_messages_become_tool_results(self):
        call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}, {"id": "c2"}]}
        exit_ = {"role": "exit", "content": "done"}
        nudge = {"role": "user", "content": "submit"}
        self.assertEqual(s1.api_messages([call, exit_, nudge]), [
            call, {"role": "tool", "tool_call_id": "c1", "content": "done"},
            {"role": "tool", "tool_call_id": "c2", "content": "done"}, nudge])
        self.assertEqual(s1.api_messages([nudge, exit_, nudge]), [nudge, nudge])

    def test_outputs_and_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.write(output, 1, "solo", 1, "12")
            rows = s1.diagnose(s1.collect(config([1]), output))
            s1.write_outputs(config([1]), output, rows)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual((summary["runs"], summary["known_cost_usd"]), (1, 0.01))
            self.assertTrue((output / "rows.csv").is_file())
            bad = output / "bad.json"
            bad.write_text(json.dumps({**config([1]), "settings": ["team"]}))
            with self.assertRaises(ValueError):
                s1.load_config(bad)
            self.assertEqual(len(s1.plan(config([1, 2], repeats=3))), 12)


if __name__ == "__main__":
    unittest.main()
