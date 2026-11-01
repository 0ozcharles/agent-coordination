"""Luna collaboration policy/integrity tests; local fixtures only, no API/Docker."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import run_luna_collaboration as runner
import run_handoff_expansion as shared
import task_specs


class LunaCollaborationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "artifacts").mkdir()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.suite = self.runs / "luna_collaboration_unit"
        self.solo = self.runs / "luna_solo_v1"
        shutil.copyfile(runner.ROOT / "run_handoff_pilot.py", self.root / "run_handoff_pilot.py")
        (self.root / "worker.py").write_text("# Frozen synthetic runtime\n")
        self.manifest = {"worker.py": runner.file_hash(self.root / "worker.py")}
        self.events, self.score_count = [], 0
        self.prefix_times = dict(zip(runner.TASK_KEYS, (80.25, 30.5)))
        self.not_reached = set()
        self.packet_message = lambda packet: json.dumps(packet, sort_keys=True)
        patches = [mock.patch.object(runner, "ROOT", self.root),
            mock.patch.object(runner, "RUNS", self.runs),
            mock.patch.object(runner, "SOLO", self.solo),
            mock.patch.object(runner, "PROOF", self.root / "artifacts/offline.json"),
            mock.patch.object(shared, "ROOT", self.root),
            mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.object(runner, "source_manifest", return_value=self.manifest),
            mock.patch.object(runner, "verify_references"),
            mock.patch.object(runner, "verify_preflight"),
            mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict("sys.modules", {"cross_task_packet": SimpleNamespace(packet_message=self.packet_message)}),
            mock.patch.dict(runner.os.environ, {"OPENAI_API_KEY": "unit-placeholder-not-a-real-key"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        settings = {"model":runner.MODEL,"reasoning_effort":"none","offline_selftest":False,
            "provider":"OpenAI","endpoint":"https://api.openai.com/v1/chat/completions",
            "temperature":.7,"top_p":.8,"max_completion_tokens":16384,"service_tier":"default",
            "prompt_cache_options":{"mode":"implicit","ttl":"30m"},"context_window_tokens":1050000,
            "pricing_usd_per_million":{"input":.1,"output":.5}}
        self.write(self.solo / "protocol.json", settings)
        self.write(self.solo / "summary.json", {"pipeline_completed":True,"rows":[]})
        for key, seconds in zip(runner.TASK_KEYS, (1466.261, 1269.899)):
            task = runner.get_task(key)
            for feature in task.features:
                path = task.task_dir / f"feature{feature}/feature.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"Original requirement for {key} feature {feature}")
            for repeat in (1, 2):
                run = self.solo / key / "solo_jobs" / f"{key}_r{repeat:02d}_solo"
                self.write(run / "task_config.json", {"task":task.as_dict(),"fresh_solo":True,
                    "execution_seconds":seconds})
                self.write(run / "result.json", {"model":runner.MODEL,"condition":"solo", "task":key,
                    "repeat":repeat,"evaluation":{"evaluation_completed":True}})
                self.write(run / "solo/execution_receipt.json", {"immutable":"historical fixture"})
                self.add_usage(run, "solo", 100)
        self.solo_before = {str(p.relative_to(self.solo)):p.read_bytes()
                            for p in self.solo.rglob("*") if p.is_file()}
        self.protocol = runner.protocol_for(False)
        patch = mock.patch.object(runner, "run_command", side_effect=self.command)
        self.command_mock = patch.start()
        self.addCleanup(patch.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def add_usage(self, run, condition, cost, status="completed", agent="agent1"):
        path = run / "usage_ledger.json"
        ledger = runner.read_json(path, {"requests":[]})
        ledger["requests"].append({"model":runner.MODEL,"phase":condition+":"+agent,
            "run_id":run.name,"request_number":len(ledger["requests"])+1,"status":status,
            "actual_cost_usd":cost,"reserved_usd":.1,
            "prompt_tokens":100 if cost is not None else 0,"completion_tokens":10 if cost is not None else 0})
        self.write(path, ledger)

    def command(self, command, *args, **kwargs):
        path = Path(command[command.index("--run-dir")+1])
        worker = "--worker" in command
        run = path if worker else path.parent
        condition = command[command.index("--condition")+1] if worker else path.name
        self.events.append(("model" if worker else "evaluation",str(run.parent),run.name,condition))
        if worker:
            self.complete_model(run, condition)
        else:
            phase = run/condition
            if not runner.read_json(phase/"evaluation_summary.json",{}).get("evaluation_completed"):
                self.score_count += 1
                self.write(phase/"evaluation_summary.json",{"evaluation_completed":True,
                    "both_passed":False,"elapsed_seconds":2.5})
        return 0

    def complete_model(self, run, condition):
        config = runner.read_json(run/"task_config.json")
        task = runner.get_task(config["task_key"])
        phase = run/condition
        offline = config["offline_selftest"]
        status = {"phase":condition,"condition":condition,"status":"completed","task":task.as_dict(),
            "offline_selftest":offline,"elapsed_seconds":self.prefix_times[task.key] if condition=="prefix" else 100,
            "infrastructure_error":False,"initialization_included_in_budget":True,
            "continuation_budget_seconds":config["execution_seconds"]}
        if condition == "prefix":
            if not offline:
                self.add_usage(run,condition,.005)
                self.add_usage(run,condition,.005,agent="agent2")
            if task.key in self.not_reached:
                status["status"] = "checkpoint_not_reached"
            else:
                (run/"shared_git.tar").write_bytes(task.key.encode())
                checkpoint = {"task":task.as_dict(),"broker":{"blocked":{},"exited":[]},
                    "git_archive_sha256":runner.file_hash(run/"shared_git.tar"),
                    "wip_checkpoint":{"progress":{},"outcomes":{}},"agents":{}}
                for agent, feature in task.agents.items():
                    checkpoint["agents"][agent] = {"git_base":task.base,"image":task.image,
                        "manifest":{"source.py":task.key},"state":{"messages":[{"role":"system","content":"Original system"}],
                            "extra_template_vars":{"task":(task.task_dir/f"feature{feature}/feature.md").read_text()}}}
                    checkpoint["wip_checkpoint"]["progress"][agent] = {"has_code_change":True,
                        "changed_files":[{"path":task.code_roots[0]+"/source.py"}]}
                    checkpoint["wip_checkpoint"]["outcomes"][agent] = {"submitted":False,"exited":False,"infrastructure_error":False}
                self.write(run/"checkpoint.json",checkpoint)
        else:
            checkpoint = runner.read_json(run/"checkpoint.json")
            checkpoint_hash = runner.digest(checkpoint)
            packet = {"schema_version":2,"checkpoint_sha256":checkpoint_hash,"task_key":task.key,
                "base_commit":task.base,"checkpoint_only":True,"agents":{a:{} for a in task.agents}}
            text = self.packet_message(packet)
            packet_hash,text_hash = runner.digest(packet),hashlib.sha256(text.encode()).hexdigest()
            self.write(phase/"handoff_packet.json",packet)
            (phase/"handoff_packet.txt").write_text(text)
            active = ("agent1",) if condition=="consolidate" else tuple(task.agents)
            delivery = {"condition":condition,"checkpoint_sha256":checkpoint_hash,"packet_sha256":packet_hash,
                "source_checkpoint_verified":True,"code_automatically_transferred":False,
                "first_request_verified":True,"recipients":{}}
            for agent in active:
                delivery["recipients"][agent] = {"packet_sha256":packet_hash,"message_sha256":text_hash,
                    "message_index":2,"history_preserved":True,"all_packet_content_delivered":True,"role":"user"}
                self.write(phase/f"first_request_{agent}.json",{"verified":True,"recipient":agent,
                    "full_packet_present":True,"message_sha256":text_hash,"actual_transport_input_verified":not offline})
                self.write(phase/f"{agent}.traj.json",{"messages":[{"role":"user","content":text}]})
                if not offline:
                    self.add_usage(run,condition,.1,agent=agent)
                    self.write(phase/"responses"/agent/"request_00001_input.json",
                        {"request":{"model":runner.MODEL,"messages":[{"role":"user","content":text}]}})
            self.write(phase/"handoff_delivery.json",delivery)
            self.write(phase/"restore_proof.json",{"checkpoint_hash":checkpoint_hash,
                "broker_hash":runner.digest(checkpoint["broker"]),"agents":{a:{
                    "state_hash":runner.digest(saved["state"]),"filesystem_manifest_hash":runner.digest(saved["manifest"]),
                    "source_image":saved["image"],"filesystem_verified":True} for a,saved in checkpoint["agents"].items()}})
            status.update(handoff_variant=runner.VARIANT,handoff_packet_sha256=packet_hash,handoff_seconds=4,
                outcomes={a:{"status":"Submitted","phase_turns":4,"infrastructure_error":False} for a in active})
            if condition == "consolidate":
                (phase/"final.patch").write_text("Synthetic combined publication")
                status.update(outcome="Submitted",retired_agent_model_calls=0,
                    final_patch_sha256=runner.file_hash(phase/"final.patch"))
                self.write(phase/"intervention.json",{"retired":"agent2"})
            else:
                for feature in task.features:
                    (phase/f"agent{feature}.patch").write_text(f"Synthetic feature {feature} publication")
        self.write(phase/"status.json",status)

    def run_suite(self, offline=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.supervise(self.suite,offline)

    def summary(self):
        return runner.read_json(self.suite/"summary.json")

    def prepare_job(self, condition="strongshare", key=None):
        task = runner.get_task(key or runner.TASK_KEYS[0])
        self.write(self.suite/"protocol.json",self.protocol)
        prefix = self.suite/task.key/"prefix"
        runner.configure_run(prefix,task,"prefix",self.protocol,1200)
        runner.run_model(prefix,task,"prefix",self.protocol,-1)
        record = runner.verify_luna_prefix(prefix,task,self.protocol)
        seconds = runner.remaining_seconds(self.protocol,task.key,record)
        run = self.suite/task.key/"jobs"/f"{task.key}_r01_{condition}"
        runner.configure_run(run,task,condition,self.protocol,seconds,record["checkpoint_sha256"])
        runner.copy_checkpoint(prefix,run,task,record)
        return run,task,record

    def test_eight_jobs_same_checkpoint_same_remaining_budget_and_historical_solo_unchanged(self):
        self.assertEqual(self.run_suite(),0)
        summary = self.summary()
        self.assertEqual(len(summary["rows"]),8)
        self.assertTrue(summary["pipeline_completed"])
        self.assertFalse(any(row["official_success"] for row in summary["rows"]))
        self.assertIn("historical",summary["solo_reference"]["interpretation"])
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"],1.22)
        self.assertEqual(summary["actual_new_usage"]["requests"],16)
        for key,total in zip(runner.TASK_KEYS,(1466.261,1269.899)):
            rows = [row for row in summary["rows"] if row["task"]==key]
            self.assertEqual([(r["repeat"],r["condition"]) for r in rows],
                             [(1,"strongshare"),(1,"consolidate"),(2,"consolidate"),(2,"strongshare")])
            self.assertEqual({r["remaining_execution_allowance_seconds"] for r in rows},{round(total-self.prefix_times[key],3)})
            hashes = {runner.file_hash(Path(r["run_directory"])/"checkpoint.json") for r in rows}
            self.assertEqual(len(hashes),1)
            for row in rows:
                self.assertEqual(row["total_execution_allowance_seconds"],total)
                self.assertAlmostEqual(row["full_task_all_attempt_known_cost_usd"],.01+row["all_attempt_usage"]["known_cost_usd"])
                provenance = runner.read_json(Path(row["run_directory"])/"checkpoint_provenance.json")
                self.assertEqual(provenance["source_directory"],str(self.suite/key/"prefix"))
        self.assertEqual(self.solo_before,{str(p.relative_to(self.solo)):p.read_bytes()
                         for p in self.solo.rglob("*") if p.is_file()})

    def test_completed_failed_models_and_scores_are_not_sampled_again_on_resume(self):
        self.assertEqual(self.run_suite(),0)
        expected = self.summary()
        self.events.clear()
        with mock.patch.dict(runner.os.environ,{"OPENAI_API_KEY":""}):
            self.assertEqual(self.run_suite(),0)
        self.assertTrue(all(event[0]=="evaluation" for event in self.events))
        self.assertEqual(self.score_count,8)
        self.assertEqual(self.summary(),expected)

    def test_no_checkpoint_skips_all_four_dependents_without_resampling(self):
        key = runner.TASK_KEYS[0]
        self.not_reached.add(key)
        self.assertEqual(self.run_suite(),0)
        summary = self.summary()
        self.assertEqual(len(summary["skipped_jobs"]),4)
        self.assertEqual({row["reason"] for row in summary["skipped_jobs"]},{"checkpoint_not_reached"})
        self.assertEqual(len(summary["rows"]),4)
        self.events.clear()
        self.assertEqual(self.run_suite(),0)
        self.assertFalse(any(event[0]=="model" for event in self.events))

    def test_prefix_exhausting_total_budget_skips_dependents_and_keeps_prefix_cost(self):
        key = runner.TASK_KEYS[1]
        self.prefix_times[key] = 1269.899
        self.assertEqual(self.run_suite(),0)
        summary = self.summary()
        self.assertEqual(len(summary["rows"]),4)
        self.assertEqual({j["reason"] for j in summary["skipped_jobs"]},{"prefix_exhausted_total_budget"})
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"],.62)
        self.assertFalse(list((self.suite/key/"jobs").glob("*")))

    def test_invalid_prefix_elapsed_never_yields_a_continuation_allowance(self):
        for value in (True,False,None,0,-1,float("nan"),float("inf"),"3"):
            with self.subTest(elapsed=value),self.assertRaises(ValueError):
                runner.remaining_seconds(self.protocol,runner.TASK_KEYS[0],{"elapsed_seconds":value})

    def test_foreign_model_or_protocol_prefix_rejected_before_continuation(self):
        run,task,_ = self.prepare_job()
        prefix = self.suite/task.key/"prefix"
        for path,change in ((prefix/"usage_ledger.json",lambda x:x["requests"][0].update(model="Qwen/Qwen3.5-9B")),
                            (prefix/"task_config.json",lambda x:x.update(protocol_sha256="0"*64))):
            with self.subTest(file=path.name):
                old = path.read_bytes();value=json.loads(old);change(value);self.write(path,value)
                try:
                    with self.assertRaises(ValueError):
                        runner.verify_luna_prefix(prefix,task,self.protocol)
                finally:
                    path.write_bytes(old)
        self.assertFalse((run/"strongshare").exists())

    def test_incomplete_continuation_preserves_cost_unknown_time_and_original_checkpoint(self):
        original = self.command
        failed = False
        def fail_once(command,*args,**kwargs):
            nonlocal failed
            if "--worker" in command and command[command.index("--condition")+1]=="strongshare" and not failed:
                failed=True;run=Path(command[command.index("--run-dir")+1])
                self.add_usage(run,"strongshare",.3)
                self.add_usage(run,"strongshare",None,"in_flight")
                self.write(run/"strongshare/status.json",{"status":"error","elapsed_seconds":7})
                return 2
            return original(command,*args,**kwargs)
        self.command_mock.side_effect=fail_once
        self.assertEqual(self.run_suite(),2)
        checkpoint = (self.suite/runner.TASK_KEYS[0]/"prefix/checkpoint.json").read_bytes()
        self.assertEqual(self.run_suite(),0)
        summary=self.summary();first=summary["rows"][0]
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"],1.52)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"],1)
        self.assertEqual(first["accepted_usage"]["known_cost_usd"],.2)
        self.assertEqual(first["all_attempt_usage"]["known_cost_usd"],.5)
        self.assertEqual(first["remaining_execution_known_seconds"],107)
        self.assertEqual(first["full_task_all_attempt_unknown_usage_requests"],1)
        self.assertEqual(checkpoint,(Path(first["run_directory"])/"checkpoint.json").read_bytes())
        archived=runner.read_json(Path(first["run_directory"])/"usage_ledger.json")["requests"]
        self.assertEqual(sum(bool(row.get("excluded_from_baseline")) for row in archived),2)
        self.assertEqual(next(row for row in archived if row.get("actual_cost_usd") is None)["error_type"],"InterruptedAttemptUsageUnknown")

    def test_interrupted_prefix_is_charged_once_in_actual_total_and_retained_in_full_task_all_attempt(self):
        original=self.command;failed=False
        def fail_once(command,*args,**kwargs):
            nonlocal failed
            if "--worker" in command and command[command.index("--condition")+1]=="prefix" and not failed:
                failed=True;run=Path(command[command.index("--run-dir")+1])
                self.add_usage(run,"prefix",.3)
                self.add_usage(run,"prefix",None,"in_flight")
                self.write(run/"prefix/status.json",{"status":"error","elapsed_seconds":9})
                return 2
            return original(command,*args,**kwargs)
        self.command_mock.side_effect=fail_once
        self.assertEqual(self.run_suite(),2)
        self.assertEqual(self.run_suite(),0)
        summary=self.summary()
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"],1.52)
        self.assertEqual(summary["actual_new_usage"]["unknown_usage_requests"],1)
        first=summary["rows"][0]
        self.assertAlmostEqual(first["full_task_all_attempt_known_cost_usd"],.51)
        self.assertEqual(first["full_task_all_attempt_unknown_usage_requests"],1)
        self.assertAlmostEqual(first["full_task_execution_seconds"],self.prefix_times[first["task"]]+9+100)
        self.assertEqual(first["remaining_execution_allowance_seconds"],1386.011)

    def test_evaluator_failure_retries_only_scoring(self):
        original=self.command;failed=False
        def fail_once(command,*args,**kwargs):
            nonlocal failed
            if "--worker" not in command and not failed:
                failed=True;return 2
            return original(command,*args,**kwargs)
        self.command_mock.side_effect=fail_once
        self.assertEqual(self.run_suite(),2)
        initial_model_events=[event for event in self.events if event[0]=="model"]
        self.assertEqual(self.run_suite(),0)
        for event in initial_model_events:self.assertEqual(self.events.count(event),1)
        self.assertEqual(self.score_count,8)

    def test_worker_rejects_wrong_time_task_or_unplanned_job_without_dispatch(self):
        run,task,_=self.prepare_job()
        original=runner.read_json(run/"task_config.json")
        with mock.patch("luna_collaboration_worker.phase_worker") as call:
            for change in ({"execution_seconds":1200},{"task_key":runner.TASK_KEYS[1]},
                           {"model":"Qwen/Qwen3.5-9B"},{"protocol_sha256":"0"*64}):
                with self.subTest(change=change):
                    self.write(run/"task_config.json",{**original,**change})
                    with self.assertRaises(ValueError):runner.worker(run,task,"strongshare",False)
                    call.assert_not_called()
            self.write(run/"task_config.json",original)
            unplanned=run.with_name(task.key+"_r99_strongshare");run.rename(unplanned)
            with self.assertRaises(ValueError):runner.worker(unplanned,task,"strongshare",False)
            call.assert_not_called()

    def test_changed_historical_solo_and_final_source_prevent_resume_or_offline_proof(self):
        receipt=next(self.solo.glob("*/solo_jobs/*/solo/execution_receipt.json"))
        receipt.write_text('{"modified":true}')
        with self.assertRaisesRegex(RuntimeError,"historical Solo"):
            runner.verify_sources(self.protocol)
        receipt.write_bytes(self.solo_before[str(receipt.relative_to(self.solo))])
        original=self.command
        def change_final(command,*args,**kwargs):
            code=original(command,*args,**kwargs)
            if self.score_count==4:runner.source_manifest.return_value={"worker.py":"mutated"}
            return code
        self.command_mock.side_effect=change_final
        self.assertEqual(self.run_suite(offline=True),2)
        self.assertFalse(runner.PROOF.exists())

    def test_exact_offline_proof_has_two_prefixes_four_submissions_and_zero_requests(self):
        self.assertEqual(self.run_suite(offline=True),0)
        proof=runner.read_json(runner.PROOF)
        self.assertEqual((proof["prefixes_completed"],proof["jobs_completed"],proof["model_api_calls"]),(2,4,0))
        self.assertEqual(proof["source_manifest"],self.manifest)
        self.assertEqual(proof["solo_reference"],self.protocol["solo_reference"])
        self.assertEqual(self.summary()["actual_new_usage"]["known_cost_usd"],0)

    def make_prequery_terminal(self, run, task, condition="strongshare"):
        phase=run/condition
        phase.mkdir(exist_ok=True)
        config=runner.read_json(run/"task_config.json")
        saved={"status":runner.EXHAUSTED,"phase":condition,"condition":condition,
            "task":task.as_dict(),"offline_selftest":config["offline_selftest"],
            "handoff_variant":runner.VARIANT,"continuation_budget_seconds":config["execution_seconds"],
            "initialization_included_in_budget":True,"infrastructure_error":False,
            "continuation_model_requests":0,"elapsed_seconds":config["execution_seconds"]+.1,
            "continuation_seconds":0.0}
        self.write(phase/"source_manifest.json",self.protocol["source_manifest"])
        self.write(phase/"status.json",saved)
        return saved

    def test_prequery_budget_terminal_is_frozen_and_not_reexecuted(self):
        run,task,_=self.prepare_job()
        saved=self.make_prequery_terminal(run,task)
        runner.validate_budget_terminal(run,task,"strongshare",self.protocol)
        self.command_mock.reset_mock()
        with mock.patch.dict(runner.os.environ,{"OPENAI_API_KEY":""}):
            self.assertEqual(runner.run_model(run,task,"strongshare",self.protocol,-1),saved)
        self.command_mock.assert_not_called()
        self.assertTrue((run/"strongshare/budget_terminal_receipt.json").is_file())
        self.write(run/"strongshare/status.json",{**saved,"elapsed_seconds":saved["elapsed_seconds"]+1})
        with self.assertRaisesRegex(RuntimeError,"Frozen configuration changed"):
            runner.run_model(run,task,"strongshare",self.protocol,-1)
        self.command_mock.assert_not_called()

    def test_budget_terminal_rejects_foreign_task_early_expiry_and_hidden_model_usage(self):
        run,task,_=self.prepare_job()
        saved=self.make_prequery_terminal(run,task)
        for change in ({"task":runner.get_task(runner.TASK_KEYS[1]).as_dict()},
                       {"elapsed_seconds":2},{"continuation_model_requests":1}):
            with self.subTest(change=change):
                self.write(run/"strongshare/status.json",{**saved,**change})
                with self.assertRaises(ValueError):
                    runner.validate_budget_terminal(run,task,"strongshare",self.protocol)
        self.write(run/"strongshare/status.json",saved)
        self.add_usage(run,"strongshare",None,"in_flight")
        with self.assertRaisesRegex(ValueError,"contains a model query"):
            runner.validate_budget_terminal(run,task,"strongshare",self.protocol)
        self.assertFalse((run/"strongshare/budget_terminal_receipt.json").exists())

    def test_completed_execution_receipt_cannot_be_downgraded_to_budget_skip(self):
        run,task,_=self.prepare_job()
        runner.run_model(run,task,"strongshare",self.protocol,-1)
        self.make_prequery_terminal(run,task)
        self.command_mock.reset_mock()
        with self.assertRaisesRegex(ValueError,"completed execution cannot"):
            runner.run_model(run,task,"strongshare",self.protocol,-1)
        self.command_mock.assert_not_called()

    def test_terminal_skip_remains_in_planned_count_and_resume_does_not_retry_it(self):
        original=self.command;skipped_run=None
        def skip_once(command,*args,**kwargs):
            nonlocal skipped_run
            if "--worker" in command and command[command.index("--condition")+1]=="strongshare" and skipped_run is None:
                run=Path(command[command.index("--run-dir")+1])
                task=runner.get_task(runner.read_json(run/"task_config.json")["task_key"])
                self.make_prequery_terminal(run,task)
                skipped_run=run
                return 0
            return original(command,*args,**kwargs)
        self.command_mock.side_effect=skip_once
        self.assertEqual(self.run_suite(),0)
        summary=self.summary()
        self.assertTrue(summary["pipeline_completed"])
        self.assertEqual((len(summary["rows"]),len(summary["skipped_jobs"]),summary["planned_jobs"]),(7,1,8))
        self.assertEqual(summary["actual_new_usage"]["known_cost_usd"],1.02)
        self.assertEqual(summary["skipped_jobs"][0]["run_directory"],str(skipped_run))
        self.events.clear()
        self.assertEqual(self.run_suite(),0)
        self.assertTrue(all(event[0]=="evaluation" for event in self.events))
        self.assertEqual(self.score_count,7)

    def test_partial_deadline_keeps_peer_cost_and_rejects_missing_agent_query_proof(self):
        run,task,_=self.prepare_job()
        phase=run/"strongshare";phase.mkdir()
        self.write(phase/"source_manifest.json",self.protocol["source_manifest"])
        self.complete_model(run,"strongshare")
        (phase/"first_request_agent2.json").unlink()
        shutil.rmtree(phase/"responses/agent2")
        ledger=runner.read_json(run/"usage_ledger.json")
        ledger["requests"]=[row for row in ledger["requests"] if row["phase"]!="strongshare:agent2"]
        self.write(run/"usage_ledger.json",ledger)
        saved=runner.read_json(phase/"status.json")
        saved.update(status="budget_exhausted",unqueried_agents=["agent2"],
                     elapsed_seconds=saved["continuation_budget_seconds"]+.1)
        saved["outcomes"]["agent2"].update(status="deadline")
        self.write(phase/"status.json",saved)
        self.assertEqual(runner.validate_budget_terminal(run,task,"strongshare",self.protocol),saved)
        self.assertEqual(runner.usage(run)["known_cost_usd"],.1)
        self.write(phase/"first_request_agent2.json",{"verified":True})
        with self.assertRaisesRegex(ValueError,"Partial-query deadline"):
            runner.validate_budget_terminal(run,task,"strongshare",self.protocol)


if __name__ == "__main__":
    unittest.main()
