"""Integrator supervisor policy/accounting tests; no API or Docker."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import run_luna_integrator as runner


class IntegratorTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.runs=self.root/"artifacts/handoff_expansion_runs"
        self.origin=self.runs/"luna_timing_v1";self.suite=self.runs/"luna_integrator_unit"
        (self.root/"artifacts").mkdir();(self.root/"frozen.py").write_text("# Frozen fixture\n")
        self.manifest={"frozen.py":runner.file_hash(self.root/"frozen.py")}
        self.early_records={};self.late_records={};self.extensions=[]
        self.old_workers={c:mock.Mock(return_value=0) for c in runner.CONDITIONS}
        self.modules={"luna_collaboration_worker":SimpleNamespace(phase_worker=self.old_workers["strongshare"]),
            "luna_integrator_worker":SimpleNamespace(phase_worker=self.old_workers["integrator"])}
        patches=[mock.patch.object(runner,"ROOT",self.root),mock.patch.object(runner,"RUNS",self.runs),
            mock.patch.object(runner,"PROOF",self.root/"artifacts/proof.json"),
            mock.patch.object(runner,"source_manifest",side_effect=lambda:{"frozen.py":runner.file_hash(self.root/"frozen.py")}),
            mock.patch.object(runner.timing,"verify_sources"),
            mock.patch.object(runner.timing,"extension_record",side_effect=self.extension_record),
            mock.patch.object(runner,"cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment",return_value={}),
            mock.patch.dict("sys.modules",self.modules),
            mock.patch.dict(runner.os.environ,{"OPENAI_API_KEY":"unit-placeholder-not-a-real-key"})]
        for patch in patches:patch.start();self.addCleanup(patch.stop)
        early_refs={}
        for key,early_time,late_time in zip(runner.TASK_KEYS,(96.202,89.784),(116.537,108.302)):
            directory=self.runs/"luna_collaboration_v1"/key/"prefix"
            early={"task":runner.get_task(key).as_dict(),"status":"completed","checkpoint_sha256":("a" if key==runner.TASK_KEYS[0] else "b")*64,
                "elapsed_seconds":early_time,"accepted_usage":self.usage(.01),"all_attempt_usage":self.usage(.01),"files_sha256":{}}
            late={**copy.deepcopy(early),"checkpoint_sha256":("c" if key==runner.TASK_KEYS[0] else "d")*64,
                "elapsed_seconds":late_time,"accepted_usage":self.usage(.02),"all_attempt_usage":self.usage(.02)}
            self.early_records[key]=early;self.late_records[key]=late
            early_refs[key]={"directory":str(directory),"record":early,"protocol_sha256":"e"*64}
            self.add_usage(directory,"prefix",.01)
            late_dir=runner.timing.stage_suite(self.origin,"late")/key/"prefix"
            self.add_usage(late_dir,"prefix",.02)
        protocol={"offline_selftest":False,"model":runner.MODEL,"source_manifest":self.manifest,"early_references":early_refs,
            "provider":"OpenAI","endpoint":"https://api.openai.com/v1/chat/completions","reasoning_effort":"none", "temperature":.7,
            "top_p":.8,"max_completion_tokens":16384,"service_tier":"default","prompt_cache_options":{"mode":"implicit","ttl":"30m"},
            "pricing_usd_per_million":{"ordinary_input":.1,"cached_input":.01,"cache_write_30m":.125,"output":.5}}
        self.write(self.origin/"protocol.json",protocol);self.write(self.origin/"status.json",{"status":"completed"})
        self.write(self.origin/"summary.json",{"rows":[{"official_success":True}]*16})
        (self.origin/"source_snapshot").mkdir();(self.origin/"source_snapshot/frozen.py").write_bytes((self.root/"frozen.py").read_bytes())
        self.protocol=runner.protocol_for(False)
        self.write(self.suite/"protocol.json",self.protocol)

    @staticmethod
    def usage(cost,requests=1,unknown=0):
        return {"requests":requests,"known_cost_usd":cost,"unknown_usage_requests":unknown,"prompt_tokens":100,"completion_tokens":10}

    @staticmethod
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))

    def extension_record(self,source,task,protocol):
        self.extensions.append(source)
        self.assertEqual(source,runner.timing.stage_suite(self.origin,"late")/task.key/"prefix")
        return copy.deepcopy(self.late_records[task.key])

    def add_usage(self,run,condition,cost,agent="agent1",status="completed",excluded=False):
        path=run/"usage_ledger.json";data=runner.read_json(path,{"requests":[]})
        data["requests"].append({"model":runner.MODEL,"run_id":run.name,"phase":condition+":"+agent,
            "request_number":len(data["requests"])+1,"status":status,"actual_cost_usd":cost,"reserved_usd":.2,
            "excluded_from_baseline":excluded,"prompt_tokens":100 if cost is not None else 0,"completion_tokens":10 if cost is not None else 0})
        self.write(path,data)

    def job(self,timing="early",condition="integrator",repeat=1,key=None):
        spec={"task":key or runner.TASK_KEYS[0],"timing":timing,"condition":condition,"repeat":repeat}
        path=runner.job_path(self.suite,spec);config=runner.configure_run(path,spec,self.protocol,self.suite)
        return path,spec,config

    def test_reference_late_adds_extension_cost_but_not_early_elapsed_twice(self):
        refs=runner.checkpoint_references()
        for key,early_seconds,late_seconds in zip(runner.TASK_KEYS,(96.202,89.784),(116.537,108.302)):
            early,late=refs[key]["early"],refs[key]["late"]
            self.assertEqual(early["historical_usage"]["known_cost_usd"],.01)
            self.assertAlmostEqual(late["historical_usage"]["known_cost_usd"],.03)
            self.assertEqual(late["record"]["all_attempt_usage"]["known_cost_usd"],.02)
            self.assertEqual(early["historical_execution_seconds"],early_seconds)
            self.assertEqual(late["historical_execution_seconds"],late_seconds)
            self.assertNotEqual(early["record"]["checkpoint_sha256"],late["record"]["checkpoint_sha256"])
        self.assertEqual([runner.remaining_seconds(k,refs[k][t]["record"]) for k in runner.TASK_KEYS for t in runner.TIMINGS],
            [1370.059,1349.724,1180.115,1161.597])

    def test_historical_failure_time_uses_extension_only_and_preserves_unknown(self):
        key=runner.TASK_KEYS[0];original=runner.read_json(self.origin/"protocol.json")
        early=Path(original["early_references"][key]["directory"])
        late=runner.timing.stage_suite(self.origin,"late")/key/"prefix"
        self.write(early/"failed_attempts/prefix_old/status.json",{"elapsed_seconds":5})
        self.write(late/"failed_attempts/prefix_old/status.json",{"elapsed_seconds":103.202,"extension_elapsed_seconds":7})
        (late/"failed_attempts/prefix_unknown").mkdir()
        original["early_references"][key]["record"]["all_attempt_usage"]=self.usage(.03,requests=3,unknown=1)
        self.write(self.origin/"protocol.json",original)
        self.late_records[key]["all_attempt_usage"]=self.usage(.04,requests=2,unknown=0)
        refs=runner.checkpoint_references()[key]
        self.assertEqual(refs["early"]["historical_execution_seconds"],101.202)
        self.assertEqual(refs["late"]["historical_execution_seconds"],128.537)
        self.assertEqual(refs["late"]["historical_time_unknown_attempts"],1)
        self.assertEqual(refs["late"]["historical_usage"]["unknown_usage_requests"],1)
        self.assertAlmostEqual(refs["late"]["historical_usage"]["known_cost_usd"],.07)

    def test_sixteen_interleaved_branches_separate_stage_paths_and_joint_policy(self):
        jobs=runner.schedule();self.assertEqual(len(jobs),16);self.assertEqual(len(runner.schedule(True)),8)
        self.assertEqual(len({str(runner.job_path(self.suite,j)) for j in jobs}),16)
        for key in runner.TASK_KEYS:
            first=[(j["timing"],j["condition"]) for j in jobs if j["task"]==key and j["repeat"]==1]
            second=[(j["timing"],j["condition"]) for j in jobs if j["task"]==key and j["repeat"]==2]
            self.assertEqual(second,list(reversed(first)))
        self.assertIn("joint organization-and-delivery",self.protocol["primary_scoring"])
        self.assertIn("sidecar",self.protocol["diagnostic"])
        self.assertIn("Never replace",self.protocol["diagnostic"])
        self.assertIn("background only",self.protocol["interpretation"])

    def test_immutable_config_locks_checkpoint_budget_and_scoring_policy(self):
        for timing in runner.TIMINGS:
            for condition in runner.CONDITIONS:
                run,job,config=self.job(timing,condition)
                self.assertEqual(config["submission_policy"],"authoritative_agent1_only" if condition=="integrator" else "native_cooperative_merge")
                self.assertEqual(config["condition_version"],runner.INTEGRATOR_VARIANT if condition=="integrator" else runner.VARIANT)
                self.assertTrue(config["initialization_included_in_budget"])
                with mock.patch.object(runner,"copy_checkpoint") as copy_cp:
                    self.assertEqual(runner.worker(run,self.suite),0)
                    ref=self.protocol["checkpoint_references"][job["task"]][timing]
                    copy_cp.assert_called_once_with(Path(ref["directory"]),run,runner.get_task(job["task"]),ref["record"])
                self.assertEqual(self.old_workers[condition].call_args.kwargs["execution_seconds"],config["execution_seconds"])
                self.old_workers[condition].reset_mock()
                for field,value in {"submission_policy":"best_of","condition_version":"other","model":"other",
                    "timing":"late" if timing=="early" else "early","checkpoint_sha256":"0"*64,
                    "execution_seconds":config["execution_seconds"]+1,"initialization_included_in_budget":False}.items():
                    self.write(run/"task_config.json",{**config,field:value})
                    with self.subTest(field=field),mock.patch.object(runner,"copy_checkpoint") as copier:
                        with self.assertRaises((RuntimeError,ValueError)):runner.worker(run,self.suite)
                        copier.assert_not_called();self.old_workers[condition].assert_not_called()
                self.write(run/"task_config.json",config)

    def test_only_new_branch_ledgers_count_in_bill_both_agents_and_failed_attempts(self):
        rows=[]
        for job in runner.schedule():
            run=runner.job_path(self.suite,job);runner.configure_run(run,job,self.protocol,self.suite)
            self.add_usage(run,job["condition"],.1,agent="agent1")
            self.add_usage(run,job["condition"],.2,agent="agent2")
            ref=self.protocol["checkpoint_references"][job["task"]][job["timing"]]
            rows.append(runner.build_row(run,job,ref,{"elapsed_seconds":30},{"both_passed":False}))
        report=runner.summary_for(self.suite,self.protocol,rows,[])
        self.assertTrue(report["paid_execution_completed"])
        self.assertFalse(report["pipeline_completed"])
        diagnostics=[{"primary_outcome":False,"evaluation":{"both_passed":True}} for r in rows if r["condition"]=="strongshare"]
        report=runner.summary_for(self.suite,self.protocol,rows,[],diagnostics)
        self.assertTrue(report["pipeline_completed"]);self.assertEqual(report["actual_new_usage"]["requests"],32)
        self.assertAlmostEqual(report["actual_new_usage"]["known_cost_usd"],4.8)
        self.assertFalse(report["historical_checkpoint_cost_recharged"])
        self.assertEqual(len(report["groups"]),8)
        self.assertEqual(sum(g["official_passes"] for g in report["groups"]),0)
        run=Path(rows[0]["run_directory"]);self.add_usage(run,"strongshare",.04,excluded=True)
        self.add_usage(run,"strongshare",None,status="error",excluded=True)
        self.write(run/"failed_attempts/strongshare_old/status.json",{"elapsed_seconds":12})
        (run/"failed_attempts/strongshare_unknown").mkdir()
        ref=self.protocol["checkpoint_references"][rows[0]["task"]]["early"]
        row=runner.build_row(run,runner.schedule()[0],ref,{"elapsed_seconds":30},{"both_passed":False})
        self.assertAlmostEqual(row["full_task_known_cost_usd"],.35)
        self.assertEqual(row["full_task_unknown_usage_requests"],1)
        self.assertEqual(row["full_task_execution_seconds"],138.202)
        self.assertEqual(row["full_task_time_unknown_attempts"],1)
        report=runner.summary_for(self.suite,self.protocol,[row],[])
        self.assertAlmostEqual(report["actual_new_usage"]["known_cost_usd"],4.84)
        self.assertEqual(report["actual_new_usage"]["unknown_usage_requests"],1)
        self.assertEqual(row["accepted_usage"]["requests"],2)

    def test_lead_diagnostic_keeps_primary_failure_and_execution_immutable(self):
        run,job,_=self.job(condition="strongshare");task=runner.get_task(job["task"]);phase=run/"strongshare"
        primary={"evaluation_completed":True,"both_passed":False}
        sidecar={"evaluation_completed":True,"both_passed":True,"primary_outcome":False}
        self.write(phase/"evaluation_summary.json",primary)
        self.write(phase/"lead_only_diagnostic/evaluation_summary.json",sidecar)
        with mock.patch.object(runner,"verify_execution",return_value={"fixed":"receipt"}),mock.patch.object(runner,"run_command",return_value=0) as command:
            self.assertEqual(runner.evaluate_diagnostic(run,task,-1),sidecar)
            self.assertEqual(runner.read_json(phase/"evaluation_summary.json"),primary)
            self.assertIn("--diagnose-strongshare-lead",command.call_args.args[0])
            def corrupt_primary(*args,**kwargs):
                self.write(phase/"evaluation_summary.json",sidecar)
                return 0
            command.side_effect=corrupt_primary
            with self.assertRaisesRegex(RuntimeError,"changed original execution or primary score"):
                runner.evaluate_diagnostic(run,task,-1)

    def test_completed_failure_reused_and_only_scoring_retried(self):
        run,job,_=self.job();task=runner.get_task(job["task"]);phase=run/"integrator"
        status={"status":"completed","task":task.as_dict(),"elapsed_seconds":30}
        self.write(phase/"status.json",status)
        # A native failed result is terminal; retry only an interrupted evaluator.
        with mock.patch.object(runner,"verify_execution",return_value={"receipt":"fixed"}) as check, mock.patch.object(runner,"run_command") as command:
            self.assertEqual(runner.model_phase(run,task,"integrator",self.protocol,self.suite,-1),status)
            command.assert_not_called();check.assert_called_once()
            command.return_value=2
            with self.assertRaisesRegex(RuntimeError,"saved model execution"):runner.evaluate(run,task,"integrator",-1)
            self.write(phase/"evaluation_summary.json",{"evaluation_completed":True,"both_passed":False})
            command.return_value=0
            self.assertFalse(runner.evaluate(run,task,"integrator",-1)["both_passed"])
            self.assertEqual(command.call_count,2)
            self.assertTrue(all("luna_integrator_evaluator.py" in call.args[0][2] for call in command.call_args_list))

    def test_incomplete_resume_archives_unknown_before_new_two_agent_usage(self):
        run,job,_=self.job();task=runner.get_task(job["task"]);phase=run/"integrator"
        self.write(phase/"status.json",{"status":"error","elapsed_seconds":9})
        self.add_usage(run,"integrator",.03);self.add_usage(run,"integrator",None,status="in_flight",agent="agent2")
        def finish(*args,**kwargs):
            self.add_usage(run,"integrator",.1);self.add_usage(run,"integrator",.2,agent="agent2")
            self.write(phase/"status.json",{"status":"completed"});return 0
        with mock.patch.object(runner,"run_command",side_effect=finish),mock.patch.object(runner,"verify_execution",return_value={}) as verify:
            self.assertEqual(runner.model_phase(run,task,"integrator",self.protocol,self.suite,-1)["status"],"completed")
            self.assertTrue(verify.call_args.kwargs["freeze"])
        rows=runner.read_json(run/"usage_ledger.json")["requests"]
        self.assertTrue(all(r["excluded_from_baseline"] for r in rows[:2]))
        self.assertIsNone(rows[1]["actual_cost_usd"]);self.assertEqual(rows[1]["error_type"],"InterruptedAttemptUsageUnknown")
        self.assertAlmostEqual(runner.usage(run)["known_cost_usd"],.33)
        self.assertEqual(runner.usage(run)["unknown_usage_requests"],1)
        self.assertEqual(runner.usage(run,"integrator")["requests"],2)
        self.assertEqual(len(list((run/"failed_attempts").iterdir())),1)

    def test_integrator_deadline_is_terminal_skip_not_retry_with_two_active_contract(self):
        run,job,config=self.job();task=runner.get_task(job["task"]);phase=run/"integrator"
        for name in ("checkpoint.json","checkpoint_provenance.json"):self.write(run/name,{"fixture":True})
        (run/"shared_git.tar").write_bytes(b"fixture")
        self.write(phase/"source_manifest.json",self.manifest)
        saved={"status":"time_budget_exhausted_before_first_query","task":task.as_dict(),"phase":"integrator","condition":"integrator",
            "offline_selftest":False,"handoff_variant":runner.VARIANT,"continuation_budget_seconds":config["execution_seconds"],
            "initialization_included_in_budget":True,"elapsed_seconds":config["execution_seconds"]+.01,"continuation_model_requests":0}
        self.write(phase/"status.json",saved)
        with mock.patch.object(runner,"run_command") as command:
            self.assertEqual(runner.model_phase(run,task,"integrator",self.protocol,self.suite,-1),saved)
            self.assertEqual(runner.model_phase(run,task,"integrator",self.protocol,self.suite,-1),saved)
            command.assert_not_called()
        self.assertTrue((phase/"budget_terminal_receipt.json").is_file())
        self.write(phase/"first_request_agent2.json",{"unexpected":True})
        with self.assertRaisesRegex(ValueError,"model query"):
            runner.model_phase(run,task,"integrator",self.protocol,self.suite,-1)

    def test_source_snapshot_or_incomplete_reference_prevents_new_work(self):
        run,_,_=self.job()
        self.write(self.origin/"status.json",{"status":"paused"})
        with self.assertRaisesRegex(ValueError,"completed matching"):runner.worker(run,self.suite)
        self.write(self.origin/"status.json",{"status":"completed"})
        (self.origin/"source_snapshot/frozen.py").write_text("modified")
        with self.assertRaisesRegex(ValueError,"snapshot changed"):runner.worker(run,self.suite)
        for worker in self.old_workers.values():worker.assert_not_called()


if __name__=="__main__":unittest.main()
