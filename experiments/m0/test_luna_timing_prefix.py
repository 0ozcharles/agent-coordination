"""Offline timing-gate, provenance and first-input checks; no Docker/API calls."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import luna_timing_prefix as timing


class Clock:
    def __init__(self): self.now = 1.0
    def __call__(self): return self.now


class Agent:
    def __init__(self, path, *, submitted_at=None, exited_at=None, error_at=None, callback=None):
        self.config = SimpleNamespace(output_path=path)
        self.messages = [{"role": "system", "content": "original"}]
        self.n_calls = 5
        self.steps = 0
        self.submitted_at, self.exited_at, self.error_at = submitted_at, exited_at, error_at
        self.callback = callback

    def step(self):
        self.steps += 1
        self.n_calls += 1
        if self.callback: self.callback(self)
        if self.error_at == self.steps: raise RuntimeError("synthetic infrastructure failure")
        self.messages.append({"role": "assistant", "content": "ordinary"})
        self.messages.append({"role": "tool", "content": "timing_ordinary_visibility_ok",
                              "extra": {"returncode": 0}})
        if self.exited_at == self.steps: self.messages.append({"role": "exit", "content": "done"})

    def _opened_pr(self): return self.submitted_at is not None and self.steps >= self.submitted_at
    def save(self, path): pass


def progress(_env, _task):
    return {"has_code_change": True, "changed_files": [{"path": "pkg/core.py"}],
            "head": "base", "git_status": " M pkg/core.py"}


class TimingGateTests(unittest.TestCase):
    def setUp(self):
        timing.configure()
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name); self.clock = Clock()
        self.agents = {a: Agent(self.path / (a + '.json')) for a in ('agent1', 'agent2')}

    def drive(self, deadline=100, reader=progress, offline=False):
        return timing.drive_two_rounds(self.agents, dict.fromkeys(self.agents), None, self.path,
                                      deadline=deadline, early_round=5, clock=self.clock,
                                      progress_reader=reader, offline=offline)

    def test_already_eligible_first_round_does_not_stop_before_second(self):
        outcomes, evidence = self.drive(offline=True)
        self.assertEqual([a.steps for a in self.agents.values()], [2, 2])
        self.assertEqual(evidence['paired_round'], 7)
        self.assertTrue(evidence['eligible'])
        self.assertEqual(evidence['additional_paired_rounds_completed'], 2)
        first = json.loads((self.path/'progress_round_006.json').read_text())
        self.assertFalse(first['eligible'])
        self.assertEqual(first['stop_reason'], 'continue_to_declared_boundary')
        self.assertEqual({v['phase_turns'] for v in outcomes.values()}, {2})
        self.assertTrue(all(a.messages[0] == {'role':'system','content':'original'} for a in self.agents.values()))

    def test_first_round_submission_is_terminal_not_a_replacement_checkpoint(self):
        self.agents['agent1'].submitted_at = 1
        _, evidence = self.drive()
        self.assertEqual([a.steps for a in self.agents.values()], [1, 1])
        self.assertFalse(evidence['eligible'])
        self.assertEqual(evidence['stop_reason'], 'early_submission_or_exit')

    def test_second_round_exit_disqualifies_even_with_source_changes(self):
        self.agents['agent2'].exited_at = 2
        _, evidence = self.drive()
        self.assertFalse(evidence['eligible'])
        self.assertEqual(evidence['stop_reason'], 'early_submission_or_exit')
        self.assertEqual(evidence['additional_paired_rounds_completed'], 2)

    def test_no_call_if_setup_already_exhausted_allowance(self):
        outcomes, evidence = self.drive(deadline=.5)
        self.assertEqual([a.steps for a in self.agents.values()], [0, 0])
        self.assertEqual(evidence['stop_reason'], 'deadline')
        self.assertEqual({v['status'] for v in outcomes.values()}, {'checkpoint_not_reached'})

    def test_deadline_crossed_in_final_progress_is_not_success(self):
        count = [0]
        def reader(env, task):
            count[0] += 1
            if count[0] == 4: self.clock.now = 101
            return progress(env, task)
        _, evidence = self.drive(reader=reader)
        self.assertFalse(evidence['eligible'])
        self.assertEqual(evidence['stop_reason'], 'deadline')

    def test_missing_source_change_at_fixed_boundary_has_no_third_round(self):
        def reader(env, task):
            return {'has_code_change': False, 'changed_files': []}
        _, evidence = self.drive(reader=reader)
        self.assertEqual([a.steps for a in self.agents.values()], [2, 2])
        self.assertFalse(evidence['eligible'])
        self.assertEqual(evidence['stop_reason'], 'ineligible_at_fixed_boundary')

    def test_infrastructure_error_is_not_checkpoint_unavailable(self):
        self.agents['agent1'].error_at = 1
        with self.assertRaisesRegex(RuntimeError, 'infrastructure failure'):
            self.drive()
        self.assertLessEqual(max(a.steps for a in self.agents.values()), 1)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.state = {'messages': [{'role': 'system', 'content':'original system'},
                                  {'role': 'user', 'content':'original task', 'extra':{'old':True}}]}

    def test_ordinary_mailbox_message_allowed_and_actual_input_verified(self):
        observed = []
        def query(messages, **kwargs):
            observed.append(copy.deepcopy(messages))
            out = self.path/'responses/agent1/request_00001_input.json';out.parent.mkdir(parents=True)
            out.write_text(json.dumps({'request': {'messages': timing.provider_messages(messages)}}))
            return 'response'
        model = SimpleNamespace(_query=query)
        timing.install_history_check(model,self.state,'agent1',self.path,offline=False)
        messages = self.state['messages'] + [{'role':'user','content':'[Message from agent2]: ordinary reply'}]
        self.assertEqual(model._query(messages), 'response')
        proof = json.loads((self.path/'first_request_agent1.json').read_text())
        self.assertTrue(proof['actual_transport_input_verified'])
        self.assertEqual(proof['ordinary_native_appended_messages'],1)
        self.assertEqual(observed[0], messages)
        self.assertEqual(self.state['messages'][1]['extra'], {'old':True})

    def test_changed_system_or_truncated_history_rejected_before_query(self):
        for messages in ([{'role':'system','content':'retired peer'},self.state['messages'][1]],
                         [self.state['messages'][0]]):
            model=SimpleNamespace(_query=lambda *_a,**_k:self.fail('transport was called'))
            timing.install_history_check(model,self.state,'agent1',self.path,offline=False)
            with self.assertRaisesRegex(RuntimeError,'history changed'):
                model._query(messages)

    def test_actual_transport_mutation_rejected(self):
        def query(messages, **kwargs):
            out=self.path/'responses/agent1/request_00001_input.json';out.parent.mkdir(parents=True)
            out.write_text(json.dumps({'request':{'messages':[{'role':'system','content':'wrong'}]}}))
        model=SimpleNamespace(_query=query)
        timing.install_history_check(model,self.state,'agent1',self.path,offline=False)
        with self.assertRaisesRegex(RuntimeError,'Actual first provider input differs'):
            model._query(self.state['messages'])

    def test_timeout_still_records_written_first_input(self):
        def query(messages, **kwargs):
            out=self.path/'responses/agent1/request_00001_input.json';out.parent.mkdir(parents=True)
            out.write_text(json.dumps({'request':{'messages':timing.provider_messages(messages)}}))
            raise TimeoutError('synthetic timeout')
        model=SimpleNamespace(_query=query)
        timing.install_history_check(model,self.state,'agent1',self.path,offline=False)
        with self.assertRaises(TimeoutError): model._query(self.state['messages'])
        self.assertTrue(json.loads((self.path/'first_request_agent1.json').read_text())['actual_transport_input_verified'])


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.early=self.root/'early';self.run=self.root/'late'
        self.early.mkdir();(self.early/'prefix').mkdir();self.run.mkdir();(self.run/'prefix').mkdir()
        self.task=SimpleNamespace(key='test',base='base',agents={'agent1':1,'agent2':3},task_dir=self.root/'task')
        self.task.as_dict=lambda:{'key':'test','base':'base','features':[1,3]}
        states={}
        for a,f in self.task.agents.items():
            d=self.task.task_dir/f'feature{f}';d.mkdir(parents=True);(d/'feature.md').write_text(f'original feature {f}')
            states[a]={'state':{'messages':[{'role':'system','content':'original'}], 'n_calls':5,
                'extra_template_vars':{'task':f'original feature {f}'}},'git_base':'base','manifest':{},'image':'sha256:fake'}
        self.cp={'task':self.task.as_dict(),'agents':states,'broker':{'blocked':{},'exited':{}},
            'prefix_steps_per_agent':5,'git_archive_sha256':timing.hashlib.sha256(b'archive').hexdigest(),
            'wip_checkpoint':{'rule':'first_paired_round_both_source_changes_and_unfinished_v1','paired_round':5,
                'selection_uses_test_results':False,'progress':{a:progress(None,None) for a in states},
                'outcomes':{a:{'exited':False,'submitted':False,'infrastructure_error':False} for a in states}}}
        self.write(self.early/'checkpoint.json',self.cp);(self.early/'shared_git.tar').write_bytes(b'archive')
        self.write(self.early/'prefix/status.json',{'status':'completed','elapsed_seconds':10.,'offline_selftest':True})
        self.write(self.early/'usage_ledger.json',{'model':timing.MODEL,'requests':[]})

    def write(self,path,value):path.write_text(json.dumps(value))

    def test_early_archive_task_model_and_mode_validated(self):
        cp,elapsed,files=timing.read_early(self.early,self.task,True)
        self.assertEqual(cp,self.cp);self.assertEqual(elapsed,10);self.assertEqual(len(files),4)
        with self.assertRaises(ValueError):timing.read_early(self.early,self.task,False)
        (self.early/'shared_git.tar').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError,'checksum'):timing.read_early(self.early,self.task,True)

    def test_historical_offline_prefix_without_ledger_is_valid(self):
        (self.early/'usage_ledger.json').unlink()
        _,_,files=timing.read_early(self.early,self.task,True)
        self.assertEqual(len(files),3)
        self.make_receipt_fixture()
        timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early)

    def test_offline_existing_nonzero_ledger_and_real_missing_ledger_rejected(self):
        self.write(self.early/'usage_ledger.json',{'model':timing.MODEL,'requests':[{}]})
        with self.assertRaisesRegex(ValueError,'zero provider requests'):
            timing.read_early(self.early,self.task,True)
        (self.early/'usage_ledger.json').unlink()
        self.write(self.early/'prefix/status.json',{'status':'completed','elapsed_seconds':10.,'offline_selftest':False})
        with self.assertRaisesRegex(ValueError,'requires a Luna accounting ledger'):
            timing.read_early(self.early,self.task,False)

    def test_new_accounting_explicitly_persists_zero_and_reuses_without_charge(self):
        guard=timing.initialize_accounting(self.run)
        before=(self.run/'usage_ledger.json').read_bytes()
        self.assertEqual(guard.data['requests'],[])
        self.assertEqual(guard.data['model'],timing.MODEL)
        self.assertEqual(timing.initialize_accounting(self.run).data,guard.data)
        self.assertEqual((self.run/'usage_ledger.json').read_bytes(),before)

    def test_earlier_late_checkpoint_cannot_be_extended_again(self):
        self.cp['wip_checkpoint']['rule']=timing.RULE
        self.write(self.early/'checkpoint.json',self.cp)
        with self.assertRaisesRegex(ValueError,'original first-eligible'):timing.read_early(self.early,self.task,True)

    def test_unavailable_terminal_cannot_be_resampled(self):
        from cross_task_specs import get_task
        task=get_task('llama_index17070_f1_f3')
        self.write(self.run/'prefix/status.json',{'status':'checkpoint_not_reached'})
        with self.assertRaisesRegex(ValueError,'never resampled'):
            timing.check_arguments(self.run,self.early,task,300,True,2)

    def make_receipt_fixture(self,completed=True):
        cp,elapsed,early_files=timing.read_early(self.early,self.task,True)
        phase=self.run/'prefix'
        config={'task_key':self.task.key,'task':self.task.as_dict(),'condition':'prefix','timing':'late',
                'offline_selftest':True,'execution_seconds':300.,'early_checkpoint_sha256':timing.digest(cp)}
        evidence={'rule':timing.RULE,'paired_round':7,'early_paired_round':5,
                  'additional_paired_rounds_required':2,'additional_paired_rounds_completed':2,
                  'selection_uses_test_results':False,'eligible':completed,
                  'progress':cp['wip_checkpoint']['progress'],'outcomes':cp['wip_checkpoint']['outcomes']}
        status={'status':'completed' if completed else 'checkpoint_not_reached','task':self.task.as_dict(),
                'offline_selftest':True,'timing_rule':timing.RULE,'additional_rounds':2,
                'early_checkpoint_sha256':timing.digest(cp),'early_elapsed_seconds':10.,'extension_elapsed_seconds':3.,
                'elapsed_seconds':13.,'extension_budget_seconds':300.,
                'outcomes':{a:{'phase_turns':2} for a in self.task.agents}}
        proof={'checkpoint_hash':timing.digest(cp),'broker_hash':timing.digest(cp['broker']),'agents':{
            a:{'state_hash':timing.digest(saved['state']),'filesystem_manifest_hash':timing.digest(saved['manifest']),
               'source_image':saved['image'],'state_verified':True,'filesystem_verified':True}
            for a,saved in cp['agents'].items()}}
        self.write(self.run/'task_config.json',config)
        self.write(self.run/'usage_ledger.json',{'model':timing.MODEL,'requests':[]})
        self.write(phase/'usage_summary.json',timing.usage(self.run,'prefix'))
        self.write(phase/'source_manifest.json',{'luna_timing_prefix.py':timing.file_hash(timing.__file__)})
        self.write(phase/'progress.json',evidence);self.write(phase/'restore_proof.json',proof)
        self.write(phase/'agent_outcomes.json',status['outcomes']);self.write(phase/'messages.json',cp['broker'])
        self.write(phase/'early_provenance.json',{'source_directory':str(self.early),'files_sha256':early_files,
                   'checkpoint_sha256':timing.digest(cp)})
        if completed:
            late=copy.deepcopy(cp);late.update(prefix_steps_per_agent=7,wip_checkpoint=evidence,
                timing_origin={'early_checkpoint_sha256':timing.digest(cp),'additional_paired_rounds':2})
            self.write(self.run/'checkpoint.json',late);(self.run/'shared_git.tar').write_bytes(b'archive')
            status['checkpoint_sha256']=timing.digest(late)
            for a,saved in cp['agents'].items():
                self.write(phase/f'{a}.traj.json',{})
                self.write(phase/f'first_request_{a}.json',{'early_state_sha256':timing.digest(saved['state']),
                    'history_sha256':timing.digest(timing.provider_messages(saved['state']['messages'])),
                    'history_message_count':1,'role_change_injected':False,'handoff_packet_injected':False})
        self.write(phase/'status.json',status)
        receipt={'schema':'luna_fixed_timing_prefix_receipt_v1','status':status['status'],'early_files_sha256':early_files,
                 'protected_files_sha256':{str(p.relative_to(self.run)):timing.file_hash(p)
                    for p in self.run.rglob('*') if p.is_file()}}
        self.write(phase/'execution_receipt.json',receipt)
        return receipt

    def test_completed_receipt_verifies_and_rejects_tampering(self):
        expected=self.make_receipt_fixture()
        self.assertEqual(timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early),expected)
        (self.run/'prefix/progress.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'differs from its receipt'):
            timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early)

    def test_terminal_unavailable_receipt_verified_and_rejects_invented_checkpoint(self):
        self.make_receipt_fixture(completed=False)
        timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early)
        self.write(self.run/'checkpoint.json',self.cp)
        with self.assertRaisesRegex(ValueError,'unexpectedly has a selectable checkpoint'):
            timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early)

    def test_receipt_missing_history_proof_is_rejected(self):
        r=self.make_receipt_fixture();del r['protected_files_sha256']['prefix/first_request_agent1.json']
        self.write(self.run/'prefix/execution_receipt.json',r)
        with self.assertRaisesRegex(ValueError,'omits mandatory'):
            timing.verify_receipt(self.run,self.task,early_prefix_dir=self.early)


if __name__=='__main__':unittest.main()
