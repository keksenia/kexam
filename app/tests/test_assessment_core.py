"""Score-critical proof and deferred details, using artificial answers only."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.assessment_core import materialize_core, materialize_report, build_assessment
from app.tests import test_compact_assessment as legacy_tests
from contracts.ege_v1 import validate as contract
from app.tests import test_fresh_checking as live_tests
from app.live_checking import FreshChecking
from app.server import WorkStore


def core_fixture(task_type="trigonometric_equation", refs=None):
    old=legacy_tests.assessment_fixture(task_type,refs)
    checks=[]
    for fact in old['facts']:
        check=deepcopy(fact);check['statement']=check.pop('claim')
        check['holds']={'supported':True,'contradicted':False,'unknown':None}[check.pop('status')]
        checks.append(check)
    requirements=[]
    for old_requirement in old['requirements']:
        r=deepcopy(old_requirement);r['checks']=r.pop('facts')
        r['outcome']={'fulfilled':'yes','not_fulfilled':'no','unknown':'unclear','not_applicable':'unused'}[r.pop('status')]
        requirements.append(r)
    score=deepcopy(old['score']);score.pop('requirements')
    return dict(checks=checks,requirements=requirements,uncertainties=[],score=score)


class AssessmentCoreTests(unittest.TestCase):
    def setUp(self):
        # Reuse only input setup, not the legacy tests or validator.
        self.fixture=legacy_tests.CompactAssessmentTests(methodName='test_both_types_full_score_with_code_owned_links')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.source=self.fixture.source;self.baseline=self.fixture.baseline
        self.response=core_fixture()

    def materialize(self):
        result=materialize_core(self.response,self.baseline,self.source)
        self.assertEqual(contract.validate(result,self.source),[])
        return result

    def test_grade_links_owned_by_code_and_report_pending_for_both_types(self):
        for kind in ('trigonometric_equation','inequality'):
            self.fixture.setup_work(kind);self.source=self.fixture.source;self.baseline=self.fixture.baseline
            self.response=core_fixture(kind);before=deepcopy(self.response)
            result=self.materialize()
            self.assertEqual(result['score']['value'],2)
            self.assertTrue(result['score']['requirement_ids'])
            self.assertEqual(result['readiness'],dict(score='ready',feedback='pending'))
            self.assertEqual(result['feedback'],[])
            self.assertEqual(before,self.response)

    def test_arbitrary_optional_report_does_not_supply_or_block_grade(self):
        for draft in ('invalid structure',dict(feedback='bad'),dict(findings=[dict(primary=1,facts=[999])])):
            self.response['report']=draft
            self.assertEqual(self.materialize()['score']['value'],2)
        self.response['requirements'][0]['checks']=[]
        with self.assertRaises(ValueError):self.materialize()

    def test_unknown_or_refuted_check_cannot_fulfill_requirement(self):
        for holds in (None,False):
            self.response['checks'][0]['holds']=holds
            with self.subTest(holds=holds),self.assertRaises(ValueError):self.materialize()

    def test_score_doubt_is_not_deferred_as_optional_report(self):
        for draft in (dict(findings=[dict(affects_score='unresolved')]),
                      dict(uncertainties=[dict(effect='basis')]),dict(score=dict(value=0))):
            self.response['report']=draft
            with self.subTest(draft=draft),self.assertRaisesRegex(ValueError,'assessment_report_score_doubt'):self.materialize()

    def test_grade_critical_uncertainty_still_blocks_and_feedback_does_not(self):
        self.response['uncertainties']=[dict(entries=['1:1'],effect='score',reason='Sign changes the grade.',question='Какой знак?')]
        with self.assertRaises(ValueError):self.materialize()
        self.response['uncertainties'][0]['effect']='feedback'
        self.assertEqual(self.materialize()['score']['value'],2)

    def test_unknown_entry_check_dependency_and_forged_authority_rejected(self):
        original=deepcopy(self.response)
        for change in (dict(entries=['9:1']),dict(depends_on=[1]),dict(depends_on=[99]),dict(method='exact_code')):
            self.response=deepcopy(original);self.response['checks'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.materialize()

    def test_zero_requires_explicit_exclusion_of_every_partial_credit_branch(self):
        for kind in ('trigonometric_equation','inequality'):
            self.fixture.setup_work(kind);self.source=self.fixture.source;self.baseline=self.fixture.baseline
            self.response=core_fixture(kind)
            for r in self.response['requirements']:r['outcome']='no'
            self.response['score'].update(value=0,criterion='M02-FIPI13-0' if kind=='trigonometric_equation' else 'm03-score-0')
            self.assertEqual(self.materialize()['score']['value'],0)
            alternatives=[r for r in self.response['requirements'] if r['key']=='CALC-ONLY' or r['key'].endswith('exception')]
            for r in alternatives:
                r['outcome']='unused'
                with self.subTest(kind=kind,key=r['key']),self.assertRaises(ValueError):self.materialize()
                r['outcome']='no'

    def test_unused_partial_credit_branch_does_not_block_independent_full_score(self):
        for r in self.response['requirements']:
            if r['key']=='CALC-ONLY':r['outcome']='unused'
        self.assertEqual(self.materialize()['score']['value'],2)

    def test_report_self_cause_is_rejected_after_grade_passed(self):
        result=self.materialize()
        finding=dict(category='mathematical_error',entries=['1:1'],facts=[1],requirements=['A-ANSWER'],
            description='Synthetic detail',reason='Synthetic check.',affects_score='no',status='established',primary=1)
        draft=dict(findings=[finding],feedback=[dict(entries=['1:1'],findings=[1],text='Synthetic detail.',
            status='ready',audience='student',kind='explanation')])
        with self.assertRaisesRegex(ValueError,'full_m01_contract'):
            materialize_report(self.response,draft,self.baseline,self.source)
        self.assertEqual(result['readiness']['score'],'ready')

    def test_valid_report_does_not_change_grade_and_gets_full_validation(self):
        core=self.materialize()
        draft=dict(feedback=[dict(entries=['1:1'],findings=[],text='Synthetic optional learning note.',
            status='ready',audience='student',kind='learning_note')])
        result=materialize_report(self.response,draft,self.baseline,self.source)
        self.assertEqual(contract.validate(result,self.source),[])
        self.assertEqual(result['readiness'],dict(score='ready',feedback='ready'))
        for key in ('value','requirement_ids','criterion_ids','evidence_unit_ids'):
            self.assertEqual(result['score'][key],core['score'][key])
        with self.assertRaisesRegex(ValueError,'assessment_report_schema'):
            materialize_report(self.response,{**draft,'requirements':[]},self.baseline,self.source)

    def test_prompt_omits_grade_link_lists_and_report_graph(self):
        request=build_assessment(self.source,self.baseline,[],dict(answer='{0}'))
        props=request['response_schema']['properties']
        self.assertNotIn('requirements',props['score']['properties'])
        self.assertNotIn('findings',props)
        self.assertNotIn('status',props['checks']['items']['properties'])
        self.assertIn('holds',props['checks']['items']['properties'])
        self.assertIn('исходную ОДЗ',request['system'])


class ReportFixture(live_tests.Fixture):
    def __init__(self,draft=None):
        super().__init__(live_tests.condition());self.draft=draft
    def __call__(self,payload,*,timeout_seconds):
        answer=super().__call__(payload,timeout_seconds=timeout_seconds)
        if self.stage=='grading' and self.draft is not None:
            value=json.loads(answer.content);value['report']=deepcopy(self.draft)
            return replace(answer,content=json.dumps(value))
        return answer


class ReportLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=WorkStore(self.root/'works')
        blocker=patch('socket.socket.connect',side_effect=AssertionError('Network forbidden'))
        blocker.start();self.addCleanup(blocker.stop)

    def run_work(self,draft=None):
        fixture=ReportFixture(draft)
        service=FreshChecking(self.store,policy=live_tests.fixture_policy(),ledger=self.root/'ledger',transport_factory=fixture.factory)
        self.addCleanup(service.close)
        form=live_tests.valid_form();form['task_text']='';form['task']=[live_tests.image_data()];form['solution']=[live_tests.image_data()]
        work=self.store.save(form)
        job=service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)['job']
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['result']['readiness'],dict(score='ready',feedback='pending'))
        self.assertEqual(len(fixture.requests),3)
        return service,fixture,job

    def calls(self,service):
        with closing(sqlite3.connect(service.ledger/'spending.sqlite3')) as con:return con.execute('SELECT * FROM calls ORDER BY id').fetchall()

    def test_default_explanations_are_late_local_and_preserve_core_and_invoices(self):
        service,fixture,job=self.run_work();before=self.calls(service)
        original=(service.directory/job['id']/'result-1.json').read_bytes()
        # Report creation remains local even after paid admission closes.
        service.policy=replace(service.policy,enabled=False)
        response=service.prepare_saved_report(job['id']);self.assertTrue(response['accepted'])
        result=response['job']['result'];source=json.loads((service.directory/job['id']/'verified-input.json').read_text())
        self.assertEqual(contract.validate(result,source),[])
        self.assertEqual(result['readiness'],dict(score='ready',feedback='ready'))
        self.assertEqual(result['score']['value'],job['result']['score']['value'])
        for section in ('run','units','facts','requirements','coverage'):
            self.assertEqual(result[section],job['result'][section])
        self.assertEqual(response['job']['report']['source'],'core_requirements')
        self.assertEqual(self.calls(service),before);self.assertEqual(len(fixture.requests),3)
        self.assertEqual((service.directory/job['id']/'result-1.json').read_bytes(),original)
        self.assertTrue(service.prepare_saved_report(job['id'])['reused'])

    def test_bad_optional_report_leaves_accepted_grade_and_original_result_unchanged(self):
        finding=dict(category='mathematical_error',entries=['1:1'],facts=[1],requirements=['A-ANSWER'],
            description='Synthetic detail',reason='Synthetic check.',affects_score='no',status='established',primary=1)
        draft=dict(findings=[finding],feedback=[dict(entries=['1:1'],findings=[1],text='Synthetic detail.',
            status='ready',audience='student',kind='explanation')])
        service,fixture,job=self.run_work(draft);before=self.calls(service)
        response=service.prepare_saved_report(job['id']);self.assertFalse(response['accepted'])
        later=response['job'];self.assertEqual(later['result'],job['result'])
        self.assertEqual(later['state'],'completed');self.assertEqual(later['report']['state'],'failed')
        self.assertEqual(later['report']['validation_code'],'full_m01_contract')
        self.assertEqual(self.calls(service),before);self.assertEqual(len(fixture.requests),3)

    def test_tampered_core_and_changed_source_cannot_produce_report(self):
        service,fixture,job=self.run_work();before=self.calls(service)
        path=service.directory/job['id']/'assessment-core.json';doc=json.loads(path.read_text())
        doc['response']['score']['value']=0;path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError,'report_input_changed'):service.prepare_saved_report(job['id'])
        with patch('app.live_checking.input_snapshot',return_value=(None,'changed',None)):
            with self.assertRaisesRegex(ValueError,'input_changed'):service.prepare_saved_report(job['id'])
        self.assertEqual(service.get(job['id'])['result'],job['result'])
        self.assertEqual(self.calls(service),before);self.assertEqual(len(fixture.requests),3)

    def test_restart_and_missing_report_never_dispatch_model(self):
        service,fixture,job=self.run_work();before=self.calls(service)
        restarted=FreshChecking(self.store,policy=service.policy,ledger=service.ledger,transport_factory=fixture.factory)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(job['id'])['report']['state'],'deferred')
        self.assertEqual(len(fixture.requests),3);self.assertEqual(self.calls(service),before)
        manifest=service.directory/job['id']/'job.json';stored=json.loads(manifest.read_text());stored.pop('report');manifest.write_text(json.dumps(stored))
        with self.assertRaisesRegex(ValueError,'report_not_available'):service.prepare_saved_report(job['id'])
        self.assertEqual(len(fixture.requests),3)

    def test_edited_accepted_grade_cannot_be_restamped_as_a_new_report(self):
        service,fixture,job=self.run_work();before=self.calls(service)
        path=service.directory/job['id']/'result-1.json';record=json.loads(path.read_text())
        record['facts'][-1]['claim']='Changed after acceptance';path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'result_artifact_changed'):service.prepare_saved_report(job['id'])
        self.assertEqual(self.calls(service),before);self.assertEqual(len(fixture.requests),3)


if __name__=='__main__':unittest.main()
