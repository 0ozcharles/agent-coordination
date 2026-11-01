"""Timing orchestration/accounting tests. Temporary fixtures, no API or Docker."""
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

import run_luna_timing as runner
import run_handoff_expansion as shared
import task_specs


class TimingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.runs = self.root / "artifacts/handoff_expansion_runs"
        self.suite = self.runs / "luna_timing_unit"
        self.reference = self.runs / "luna_collaboration_v1"
        (self.root / "artifacts").mkdir()
        shutil.copyfile(runner.ROOT / "run_handoff_pilot.py", self.root / "run_handoff_pilot.py")
        (self.root / "runtime.py").write_text("# Frozen fixture\n")
        self.manifest = {"runtime.py": runner.file_hash(self.root / "runtime.py")}
        self.events, self.scorings = [], []
        self.unreachable, self.fail_evaluation_once = set(), False
        self.unsubmitted = False
        self.extension_elapsed = 20.0
        self.packet_message = lambda p: json.dumps(p, sort_keys=True)
        self.verify_extension = mock.Mock(side_effect=self.check_extension)
        self.extension_worker = mock.Mock(return_value=0)
        self.continuation_worker = mock.Mock(return_value=0)
        self.modules = {
            "cross_task_packet": SimpleNamespace(packet_message=self.packet_message),
            "luna_timing_prefix": SimpleNamespace(verify_receipt=self.verify_extension, phase_worker=self.extension_worker),
            "luna_collaboration_worker": SimpleNamespace(phase_worker=self.continuation_worker),
        }
        patches = [mock.patch.object(runner, "ROOT", self.root), mock.patch.object(runner, "RUNS", self.runs),
            mock.patch.object(runner, "REFERENCE", self.reference),
            mock.patch.object(runner, "PROOF", self.root / "artifacts/offline.json"),
            mock.patch.object(shared, "ROOT", self.root), mock.patch.object(task_specs, "ROOT", self.root),
            mock.patch.object(task_specs, "REPO", self.root / "CooperBench"),
            mock.patch.object(runner, "source_manifest", side_effect=lambda: {"runtime.py":runner.file_hash(self.root/"runtime.py")}),
            mock.patch.object(runner, "early_references", side_effect=lambda offline=False:copy.deepcopy(self.references)),
            mock.patch.object(runner, "verify_preflight"), mock.patch.object(runner, "configure"),
            mock.patch.object(runner, "cleanup_phase"),
            mock.patch("evaluate_solo_smoke.evaluator_environment", return_value={}),
            mock.patch.dict("sys.modules", self.modules),
            mock.patch.dict(runner.os.environ, {"OPENAI_API_KEY":"unit-placeholder-not-a-real-key"})]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        settings = {"provider":"OpenAI", "endpoint":"https://api.openai.com/v1/chat/completions",
            "reasoning_effort":"none", "temperature":.7, "top_p":.8, "max_completion_tokens":16384,
            "service_tier":"default", "prompt_cache_options":{"mode":"implicit","ttl":"30m"},
            "pricing_usd_per_million":{"input":.1,"output":.5}}
        self.write(self.reference/"protocol.json", settings)
        self.write(self.reference/"summary.json", {"rows":[{"official_success":True}]*8})
        self.references = {}
        for key,elapsed in zip(runner.TASK_KEYS,(96.202,89.784)):
            task=runner.get_task(key); prefix=self.reference/key/"prefix"
            for feature in task.features:
                path=task.task_dir/f"feature{feature}/feature.md"
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(f"Original {key} feature {feature}")
            self.write(prefix/"task_config.json", {"model":runner.MODEL,"task":task.as_dict()})
            self.write(prefix/"prefix/source_manifest.json", self.manifest)
            self.write(prefix/"prefix/status.json", {"status":"completed","elapsed_seconds":elapsed,"offline_selftest":False})
            self.add_usage(prefix,"prefix",.01)
            self.checkpoint(prefix,task,"early")
            self.references[key]={"directory":str(prefix),"record":shared.prefix_record(prefix,task),"protocol_sha256":"a"*64}
        self.old_bytes={str(p.relative_to(self.reference)):p.read_bytes() for p in self.reference.rglob("*") if p.is_file()}
        self.protocol=runner.protocol_for(False)
        patch=mock.patch.object(runner,"run_command",side_effect=self.command)
        self.commands=patch.start();self.addCleanup(patch.stop)

    @staticmethod
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))

    def add_usage(self,run,phase,cost,agent="agent1",status="completed"):
        path=run/"usage_ledger.json";ledger=runner.read_json(path,{"requests":[]})
        ledger["requests"].append({"run_id":run.name,"model":runner.MODEL,"phase":phase+":"+agent,
            "request_number":len(ledger["requests"])+1,"status":status,"actual_cost_usd":cost,
            "reserved_usd":.2,"prompt_tokens":100 if cost is not None else 0,"completion_tokens":10 if cost is not None else 0})
        self.write(path,ledger)

    def checkpoint(self,run,task,timing):
        run.mkdir(parents=True,exist_ok=True);(run/"shared_git.tar").write_bytes((task.key+timing).encode())
        cp={"task":task.as_dict(),"broker":{"blocked":{},"exited":[]},"agents":{},
            "git_archive_sha256":runner.file_hash(run/"shared_git.tar"),"wip_checkpoint":{"progress":{},"outcomes":{}}}
        for agent,feature in task.agents.items():
            cp["agents"][agent]={"git_base":task.base,"image":task.image,"manifest":{"timing":timing},
                "state":{"messages":[{"role":"system","content":"Original system"}],
                    "extra_template_vars":{"task":(task.task_dir/f"feature{feature}/feature.md").read_text()}}}
            cp["wip_checkpoint"]["progress"][agent]={"has_code_change":True,"changed_files":[{"path":task.code_roots[0]+"/changed.py"}]}
            cp["wip_checkpoint"]["outcomes"][agent]={"submitted":False,"exited":False,"infrastructure_error":False}
        self.write(run/"checkpoint.json",cp)

    def check_extension(self,run,task,*,early_prefix_dir):
        self.assertEqual(early_prefix_dir,Path(self.references[task.key]["directory"]))
        receipt=runner.read_json(run/"prefix/execution_receipt.json")
        for name,checksum in receipt["protected_files_sha256"].items():
            if runner.file_hash(run/name)!=checksum: raise ValueError("Extension receipt changed")

    def command(self,command,*args,**kwargs):
        worker="--worker" in command;path=Path(command[command.index("--run-dir")+1])
        if worker:
            self.events.append(path)
            self.complete(path)
        else:
            if self.fail_evaluation_once:
                self.fail_evaluation_once=False
                return 2
            if not runner.read_json(path/"evaluation_summary.json",{}).get("evaluation_completed"):
                self.scorings.append(path)
                self.write(path/"evaluation_summary.json",{"evaluation_completed":True,"both_passed":False,"elapsed_seconds":2.0})
        return 0

    def complete(self,run):
        config=runner.read_json(run/"task_config.json");task=runner.get_task(config["task_key"])
        condition=config["condition"];phase=run/condition;offline=config["offline_selftest"]
        status={"status":"completed","phase":condition,"condition":condition,"task":task.as_dict(),
            "offline_selftest":offline,"elapsed_seconds":30.0,"infrastructure_error":False,
            "continuation_budget_seconds":config["execution_seconds"],"initialization_included_in_budget":True}
        if condition=="prefix":
            early=self.references[task.key]["record"]
            status.update(elapsed_seconds=early["elapsed_seconds"]+self.extension_elapsed,
                extension_elapsed_seconds=self.extension_elapsed)
            if task.key in self.unreachable: status["status"]="checkpoint_not_reached"
            else: self.checkpoint(run,task,"late")
            if not offline:self.add_usage(run,"prefix",.02)
            self.write(phase/"status.json",status)
            self.write(phase/"execution_receipt.json",{"protected_files_sha256":{
                str(p.relative_to(run)):runner.file_hash(p) for p in run.rglob("*") if p.is_file()}})
            return
        cp=runner.read_json(run/"checkpoint.json");cp_hash=runner.digest(cp)
        packet={"schema_version":2,"checkpoint_sha256":cp_hash,"task_key":task.key,"base_commit":task.base,
            "checkpoint_only":True,"agents":{a:{} for a in task.agents}}
        text=self.packet_message(packet);packet_hash=runner.digest(packet);text_hash=hashlib.sha256(text.encode()).hexdigest()
        self.write(phase/"handoff_packet.json",packet);(phase/"handoff_packet.txt").write_text(text)
        active=("agent1",) if condition=="consolidate" else tuple(task.agents)
        delivery={"condition":condition,"checkpoint_sha256":cp_hash,"packet_sha256":packet_hash,
            "source_checkpoint_verified":True,"code_automatically_transferred":False,"first_request_verified":True,"recipients":{}}
        for agent in active:
            delivery["recipients"][agent]={"packet_sha256":packet_hash,"message_sha256":text_hash,
                "message_index":2,"history_preserved":True,"all_packet_content_delivered":True,"role":"user"}
            self.write(phase/f"first_request_{agent}.json",{"verified":True,"recipient":agent,"full_packet_present":True,
                "message_sha256":text_hash,"actual_transport_input_verified":not offline})
            self.write(phase/f"{agent}.traj.json",{"messages":[{"role":"user","content":text}]})
            if not offline:
                self.add_usage(run,condition,.1,agent=agent)
                self.write(phase/"responses"/agent/"request_00001_input.json",{"request":{"messages":[{"role":"user","content":text}]}})
        self.write(phase/"handoff_delivery.json",delivery)
        self.write(phase/"restore_proof.json",{"checkpoint_hash":cp_hash,"broker_hash":runner.digest(cp["broker"]),
            "agents":{a:{"state_hash":runner.digest(saved["state"]),"filesystem_manifest_hash":runner.digest(saved["manifest"]),
                "source_image":saved["image"],"filesystem_verified":True} for a,saved in cp["agents"].items()}})
        status.update(handoff_variant=runner.VARIANT,handoff_packet_sha256=packet_hash,
            outcomes={a:{"status":"deadline" if self.unsubmitted else "Submitted","phase_turns":4} for a in active})
        if condition=="consolidate":
            (phase/"final.patch").write_text("combined fixture patch")
            status.update(outcome="Submitted",retired_agent_model_calls=0,final_patch_sha256=runner.file_hash(phase/"final.patch"))
            self.write(phase/"intervention.json",{"retired_agent":"agent2"})
        else:
            for f in task.features:(phase/f"agent{f}.patch").write_text(f"fixture feature {f}")
        self.write(phase/"status.json",status)

    def suite_run(self,offline=False):
        with contextlib.redirect_stdout(io.StringIO()):return runner.supervise(self.suite,offline)

    def summary(self):return runner.read_json(self.suite/"summary.json")

    def prepare(self,timing="early",condition="strongshare"):
        job=next(j for j in self.protocol["jobs"] if j["timing"]==timing and j["condition"]==condition)
        task=runner.get_task(job["task"]);early=self.references[task.key];source=Path(early["directory"]);record=early["record"]
        self.write(self.suite/"protocol.json",self.protocol)
        if timing=="late":
            source=runner.stage_suite(self.suite,"late")/task.key/"prefix"
            runner.configure_run(source,task,"prefix","late",self.protocol,300,self.suite)
            runner.model_phase(source,task,"prefix",self.protocol,self.suite,-1)
            record=runner.extension_record(source,task,self.protocol)
        run=runner.job_path(self.suite,job)
        runner.configure_run(run,task,condition,timing,self.protocol,runner.remaining_seconds(task.key,record),self.suite,record)
        runner.copy_checkpoint(source,run,task,record)
        return run,job,record

    def test_all_sixteen_fresh_branches_stage_isolation_and_no_historical_scores_or_spend(self):
        self.assertEqual(self.suite_run(),0)
        summary=self.summary();self.assertEqual(len(summary["rows"]),16)
        self.assertFalse(any(r["official_success"] for r in summary["rows"]))
        self.assertAlmostEqual(summary["actual_new_usage"]["known_cost_usd"],2.44)
        self.assertAlmostEqual(summary["historical_early_usage_not_recharged"]["known_cost_usd"],.02)
        self.assertEqual(summary["actual_new_usage"]["requests"],26)
        self.assertEqual(len(self.events),18)
        self.assertEqual([(r[k]) for r in summary["rows"] for k in ("task","timing","repeat","condition")],
                         [(r[k]) for r in runner.schedule() for k in ("task","timing","repeat","condition")])
        for key in runner.TASK_KEYS:
            by_timing={t:[r for r in summary["rows"] if r["task"]==key and r["timing"]==t] for t in runner.TIMINGS}
            early_hash={r["checkpoint_sha256"] for r in by_timing["early"]}
            late_hash={r["checkpoint_sha256"] for r in by_timing["late"]}
            self.assertEqual(len(early_hash),1);self.assertEqual(len(late_hash),1);self.assertNotEqual(early_hash,late_hash)
            self.assertEqual(by_timing["early"][0]["remaining_execution_allowance_seconds"]-
                by_timing["late"][0]["remaining_execution_allowance_seconds"],20)
            for timing in runner.TIMINGS:
                self.assertTrue((runner.stage_suite(self.suite,timing)/key/"packet_record.json").is_file())
        self.assertEqual(self.old_bytes,{str(p.relative_to(self.reference)):p.read_bytes() for p in self.reference.rglob("*") if p.is_file()})
        before=len(self.events);scored=len(self.scorings)
        self.assertEqual(self.suite_run(),0)
        self.assertEqual(len(self.events),before);self.assertEqual(len(self.scorings),scored)

    def test_fixed_schedule_and_frozen_actual_prefix_bounds(self):
        self.assertEqual(len(runner.schedule()),16);self.assertEqual(len(runner.schedule(True)),8)
        ids=[tuple(j.items()) for j in runner.schedule()];self.assertEqual(len(set(ids)),16)
        for key,expected in zip(runner.TASK_KEYS,(1370.059,1180.115)):
            record=self.references[key]["record"]
            self.assertEqual(runner.remaining_seconds(key,record),expected)
            self.assertLess(record["elapsed_seconds"]+runner.EXTENSION_HORIZON,400)
            self.assertLess(record["elapsed_seconds"]+runner.EXTENSION_HORIZON,1500)
        for value in (True,None,0,-1,float("nan"),float("inf")):
            with self.subTest(value=value),self.assertRaises(ValueError):runner.remaining_seconds(runner.TASK_KEYS[0],{"elapsed_seconds":value})

    def test_unreachable_late_is_terminal_skips_four_without_resampling_and_early_runs(self):
        self.unreachable.add(runner.TASK_KEYS[0]);self.assertEqual(self.suite_run(),0)
        summary=self.summary();self.assertEqual(len(summary["rows"]),12);self.assertEqual(len(summary["skipped_jobs"]),4)
        self.assertTrue(summary["pipeline_completed"])
        self.assertTrue(all(j["timing"]=="late" and j["task"]==runner.TASK_KEYS[0] for j in summary["skipped_jobs"]))
        self.assertFalse(summary["timing_effects"][0]["estimable"])
        self.assertAlmostEqual(summary["actual_new_usage"]["known_cost_usd"],1.84)
        before=len(self.events);self.assertEqual(self.suite_run(),0);self.assertEqual(len(self.events),before)

    def test_evaluation_pause_keeps_completed_model_and_task_failure(self):
        self.fail_evaluation_once=True
        self.assertEqual(self.suite_run(),2);first=list(self.events)
        self.assertEqual(len(first),2)
        self.assertEqual(self.suite_run(),0)
        self.assertEqual(self.events.count(first[0]),1);self.assertEqual(self.events.count(first[1]),1)
        self.assertEqual(len(self.events),18);self.assertEqual(len(self.scorings),16)
        self.assertFalse(any(r["official_success"] for r in self.summary()["rows"]))

    def test_checkpoint_swap_is_rejected_before_worker(self):
        early,job,record=self.prepare()
        late,_,_=self.prepare("late")
        (early/"checkpoint.json").write_bytes((late/"checkpoint.json").read_bytes())
        with self.assertRaisesRegex(RuntimeError,"checkpoint input changed"):
            runner.worker(early,self.suite)
        self.continuation_worker.assert_not_called()

    def test_actual_receipt_rejects_packet_tampering_without_resubmission(self):
        run,job,_=self.prepare()
        runner.model_phase(run,runner.get_task(job["task"]),job["condition"],self.protocol,self.suite,-1)
        path=run/job["condition"]/"handoff_packet.json";packet=runner.read_json(path)
        packet["checkpoint_sha256"]="0"*64;self.write(path,packet)
        before=len(self.events)
        with self.assertRaisesRegex(RuntimeError,"checkpoint"):
            runner.model_phase(run,runner.get_task(job["task"]),job["condition"],self.protocol,self.suite,-1)
        self.assertEqual(len(self.events),before)

    def test_worker_rejects_wrong_budget_model_task_or_timing_before_construction(self):
        run,job,_=self.prepare();config=runner.read_json(run/"task_config.json")
        mutations={"execution_seconds":config["execution_seconds"]+1,"model":"other",
            "task_key":runner.TASK_KEYS[1],"timing":"late","condition_version":"wrong",
            "initialization_included_in_budget":False,"checkpoint_sha256":"b"*64}
        for name,value in mutations.items():
            self.write(run/"task_config.json",{**config,name:value})
            with self.subTest(name=name),self.assertRaises((ValueError,RuntimeError)):runner.worker(run,self.suite)
        self.continuation_worker.assert_not_called()
        self.write(run/"task_config.json",config);self.assertEqual(runner.worker(run,self.suite),0)
        self.assertEqual(self.continuation_worker.call_args.kwargs["execution_seconds"],1370.059)

    def test_incomplete_extension_cost_and_time_retained_without_early_double_count(self):
        run,job,record=self.prepare("late");task=runner.get_task(job["task"])
        extension=runner.stage_suite(self.suite,"late")/task.key/"prefix"
        # Earlier failed attempt reports cumulative elapsed=early+12, but contributes only 12 more seconds.
        self.write(extension/"failed_attempts/prefix_old/status.json",{"elapsed_seconds":108.202,"extension_elapsed_seconds":12})
        (extension/"failed_attempts/prefix_lost_status").mkdir()
        self.add_usage(extension,"prefix",.03);self.add_usage(extension,"prefix",None,status="error")
        ledger=runner.read_json(extension/"usage_ledger.json")
        for row in ledger["requests"][1:]:row["excluded_from_baseline"]=True
        self.write(extension/"usage_ledger.json",ledger)
        self.add_usage(run,job["condition"],.1)
        self.write(run/"failed_attempts/strongshare_old/status.json",{"elapsed_seconds":7})
        self.add_usage(run,job["condition"],.04)
        ledger=runner.read_json(run/"usage_ledger.json");ledger["requests"][-1]["excluded_from_baseline"]=True
        self.write(run/"usage_ledger.json",ledger)
        row=runner.build_row(run,job,self.references[task.key],record,extension,{"elapsed_seconds":30},{"both_passed":False})
        self.assertAlmostEqual(row["full_task_known_cost_usd"],.20)
        self.assertEqual(row["full_task_unknown_usage_requests"],1)
        self.assertEqual(row["full_task_execution_seconds"],165.202)
        self.assertEqual(row["full_task_time_unknown_attempts"],1)
        report=runner.summary_for(self.suite,self.protocol,[row],[],{})
        self.assertAlmostEqual(report["actual_new_usage"]["known_cost_usd"],.19)
        self.assertEqual(report["actual_new_usage"]["unknown_usage_requests"],1)

    def test_infrastructure_archive_preserves_unknown_and_moves_partial_checkpoint(self):
        task=runner.get_task(runner.TASK_KEYS[0]);run=runner.stage_suite(self.suite,"late")/task.key/"prefix"
        runner.configure_run(run,task,"prefix","late",self.protocol,300,self.suite)
        self.write(run/"prefix/status.json",{"status":"error","extension_elapsed_seconds":11,"elapsed_seconds":107.202})
        self.checkpoint(run,task,"partial");self.add_usage(run,"prefix",None,status="in_flight")
        self.add_usage(run,"prefix",.03)
        runner.model_phase(run,task,"prefix",self.protocol,self.suite,-1)
        ledger=runner.read_json(run/"usage_ledger.json")
        self.assertTrue(all(r.get("excluded_from_baseline") for r in ledger["requests"][:2]))
        self.assertEqual(ledger["requests"][0]["error_type"],"InterruptedAttemptUsageUnknown")
        self.assertIsNone(ledger["requests"][0]["actual_cost_usd"])
        archives=list((run/"failed_attempts").glob("prefix_*"));self.assertEqual(len(archives),1)
        self.assertTrue((archives[0]/"prefix_artifacts/checkpoint.json").is_file())
        self.assertAlmostEqual(runner.usage(run)["known_cost_usd"],.05)
        self.assertAlmostEqual(runner.usage(run,"prefix")["known_cost_usd"],.02)
        self.assertEqual(runner.failed_extension_seconds(run),{"known_seconds":11.,"unknown_attempts":0})

    def test_changed_source_or_reference_rejects_resume_before_new_model(self):
        self.assertEqual(self.suite_run(),0);before=len(self.events)
        (self.root/"runtime.py").write_text("# Changed runtime\n")
        with self.assertRaises(RuntimeError):self.suite_run()
        self.assertEqual(len(self.events),before)

    def test_offline_proof_requires_eight_submissions_and_zero_new_requests(self):
        self.assertEqual(self.suite_run(True),0)
        proof=runner.read_json(runner.PROOF)
        self.assertEqual((proof["completed_jobs"],proof["completed_extensions"],proof["model_api_calls"]),(8,2,0))
        self.assertEqual(self.summary()["actual_new_usage"]["requests"],0)

    def test_offline_unsubmitted_outputs_cannot_create_proof(self):
        self.unsubmitted=True;self.assertEqual(self.suite_run(True),2)
        self.assertFalse(runner.PROOF.exists())

    def test_timing_estimator_preserves_task_groups_and_unknown_effects(self):
        rows=[]
        for timing,condition,success,cost,seconds in (
            ("early","strongshare",False,4.,40.),("early","consolidate",True,3.,30.),
            ("late","strongshare",True,2.,20.),("late","consolidate",True,1.5,15.)):
            for repeat in (1,2):
                rows.append({"task":runner.TASK_KEYS[0],"timing":timing,"condition":condition,"repeat":repeat,
                    "official_success":success,"full_task_known_cost_usd":cost,"full_task_unknown_usage_requests":0,
                    "full_task_execution_seconds":seconds,"full_task_time_unknown_attempts":0})
        result=runner.timing_effects(rows)[0]
        self.assertEqual(result["late_minus_early_effect"],{"success_rate":-1.,"known_cost_mean":.5,"execution_seconds_mean":5.})
        self.assertEqual(result["late_minus_early_by_condition"]["consolidate"]["known_cost_mean"],-1.5)
        self.assertEqual([g["n"] for g in result["groups"]],[2]*4)
        self.assertFalse(runner.timing_effects(rows)[1]["estimable"])
        rows[0]["full_task_unknown_usage_requests"]=1;rows[1]["full_task_time_unknown_attempts"]=1
        result=runner.timing_effects(rows)[0]
        self.assertFalse(result["cost_effect_complete"]);self.assertFalse(result["time_effect_complete"])
        self.assertIsNone(result["late_minus_early_effect"]["known_cost_mean"])
        self.assertIsNone(result["late_minus_early_effect"]["execution_seconds_mean"])
        self.assertEqual(result["late_minus_early_effect"]["success_rate"],-1)


if __name__=="__main__":unittest.main()
