"""Synthetic precheck/retry regressions. Never dispatch network requests."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app import condition_precheck as c
from app.call_guard import ProviderResponse, canonical
from app.tests import test_independent_assessment as support
from app.tests.test_independent_assessment import TwoStageFixture, policy
from app.independent_checking import IndependentChecking
from app.tests.test_foundation import valid_form, image_data


def math13(rhs='0', left='0', right='pi'):
    return dict(lhs='sin(x)',rhs=rhs,relation='=',interval=dict(left=left,right=right,
        left_open=False,right_open=False),restrictions=[])


class SymbolicTests(unittest.TestCase):
    def test_real_empty_is_suspicion_not_invalid_condition(self):
        result=c.check(math13('2'),'trigonometric_equation')
        self.assertEqual(result['reason'],'empty_real_answer');self.assertIsNone(c.reference(result))

    def test_empty_b_selection_does_not_trigger_reread(self):
        value=math13();value['interval'].update(left_open=True,right_open=True)
        result=c.check(value,'trigonometric_equation')
        self.assertEqual(result['status'],'solved');self.assertEqual(c.reference(result)['scope'],'13a')

    def test_original_domain_preserved_after_cancellation(self):
        value=dict(lhs='(x-1)/(x-1)',rhs='0',relation='>',interval=None,restrictions=[])
        result=c.check(value,'inequality')
        self.assertEqual(result['status'],'solved');self.assertNotEqual(result['domain'],'Reals')
        self.assertIn('1',result['answer']);self.assertIn('open',result['answer'])

    def test_unknown_or_hostile_syntax_falls_through(self):
        for value in (None, dict(math13(),lhs="__import__('os').system('false')"),
                dict(math13(),lhs='arcsin(x)')):
            self.assertEqual(c.check(value,'trigonometric_equation')['status'],'unsupported')
        self.assertIsNone(c.from_text('sin(x)=0; ещё дополнительное условие','trigonometric_equation'))

    def test_timeout_is_not_reason_to_reread(self):
        result=c.check(math13(),'trigonometric_equation',solver=lambda *a,**k:dict(status='unsupported',reason='solver_time_limit'))
        self.assertEqual(result['status'],'unsupported');self.assertNotIn('question',result)

    def test_structured_text_is_whole_statement_only(self):
        self.assertEqual(c.from_text('sin(x)=0; x in [−π;π)','trigonometric_equation')['interval'],
            dict(left='-pi',right='pi',left_open=False,right_open=True))
        self.assertIsNone(c.from_text('Решите произвольную систему: x=1 и y=2','trigonometric_equation'))

    def test_explicit_relation_mismatch_does_not_call_solver(self):
        solver=unittest.mock.Mock(side_effect=AssertionError('Mismatch must stop before solving'))
        for text,kind in [('sin(x)=0','inequality'),('x>0','trigonometric_equation')]:
            with self.subTest(kind=kind):
                result=c.check(c.from_text(text,kind),kind,solver=solver)
                self.assertEqual(result['status'],'task_type_mismatch')
                self.assertIn('question',result);self.assertIsNone(c.reference(result))
        solver.assert_not_called()

    def test_extra_interval_is_not_proof_of_wrong_task_number(self):
        result=c.check(c.from_text('x>0; x in [0;pi]','inequality'),'inequality')
        self.assertEqual(result['status'],'unsupported');self.assertNotIn('question',result)


class AddressedFixture(TwoStageFixture):
    def __init__(self,values,unknown_first=False):
        super().__init__();self.values=list(values);self.unknown_first=unknown_first;self.read_count=0
    def __call__(self,payload,*,timeout_seconds):
        if self.stage!='statement_ocr':return super().__call__(payload,timeout_seconds=timeout_seconds)
        value=self.values[self.read_count];self.read_count+=1
        self.timeouts.append(timeout_seconds)
        return ProviderResponse(canonical(value),model=payload['model'],usage={'prompt_tokens':10,'completion_tokens':50},
            cost_kopecks=None if self.unknown_first and self.read_count==1 else '1')


def read13(rhs='0',left='0',right='pi'):
    return dict(condition=f'sin(x)={rhs}; x in [{left};{right}]',question='',math=math13(rhs,left,right))


class RetryTests(unittest.TestCase):
    setUp = support.IndependentTests.setUp
    run_work = support.IndependentTests.run_work
    # Reuse the no-network setup, not inherited tests that would run twice.
    def make_service(self,values,unknown_first=False):
        fixture=AddressedFixture(values,unknown_first)
        service=IndependentChecking(self.support.store,policy=policy(),ledger=self.support.root/f'retry-ledger-{len(self.support.services)}',transport_factory=fixture.factory)
        service.work_profiles=None;service.upload_scope=None;self.support.services.append(service)
        return service,fixture

    def test_corrected_reread_before_first_deepseek(self):
        service,fixture=self.make_service([read13('2'),read13('0')]);out=self.run_work(service,photo=True)
        self.assertEqual(out['job']['state'],'completed',out['job'])
        self.assertEqual([r.stage for r in fixture.requests],['statement_ocr','statement_ocr','other','solution_ocr','grading'])
        parts=fixture.requests[1].payload['messages'][1]['content'];self.assertIn('targeted_recheck',parts[0]['text'])
        solve=json.loads(fixture.requests[2].payload['messages'][1]['content'])
        self.assertEqual(set(solve),{'condition','task_number'});self.assertIn('=0',solve['condition'])
        comparison=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(comparison['code_answer']['scope'],'13a');self.assertEqual(out['job']['condition_rereads'],1)

    def test_persistent_suspicion_stops_at_exactly_two_reads(self):
        service,fixture=self.make_service([read13('2'),read13('2')]);out=self.run_work(service,photo=True)
        job=out['job'];self.assertEqual(job['state'],'needs_clarification');self.assertEqual(len(fixture.requests),2)
        again=service.create(dict(work_id=job['work_id'],model='G',complete=True,single_task=True),background=False)
        self.assertTrue(again['reused']);self.assertEqual(len(fixture.requests),2)
        confirmed=service.confirm_empty_condition(job['id'],dict(literal=job['condition'],confirm_empty=True),background=False)
        self.assertEqual(confirmed['job']['state'],'completed',confirmed['job'])
        self.assertEqual([r.stage for r in fixture.requests],['statement_ocr','statement_ocr','other','solution_ocr','grading'])
        again=service.confirm_empty_condition(job['id'],dict(literal=job['condition'],confirm_empty=True),background=False)
        self.assertTrue(again['reused']);self.assertEqual(len(fixture.requests),5)

    def test_invalid_interval_can_be_corrected_but_not_confirmed_unchanged(self):
        service,fixture=self.make_service([read13(left='pi',right='0'),read13(left='pi',right='0')])
        job=self.run_work(service,photo=True)['job']
        with self.assertRaises(ValueError):service.confirm_empty_condition(job['id'],dict(literal=job['condition'],confirm_empty=True),background=False)
        out=service.confirm_empty_condition(job['id'],dict(literal='sin(x)=0; x in [0;pi]',confirm_empty=True),background=False)
        self.assertEqual(out['job']['state'],'completed');self.assertEqual(len(fixture.requests),5)

    def test_text_suspicion_has_no_model_calls(self):
        service,fixture=self.make_service([])
        form=valid_form();form.update(task_text='sin(x)=2; x in [0;pi]')
        work=self.support.store.save(form)
        job=service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)['job']
        self.assertEqual(job['state'],'needs_clarification');self.assertEqual(fixture.requests,[])

    def test_unsupported_photo_does_not_reread(self):
        value=read13();value['math']=None
        service,fixture=self.make_service([value]);out=self.run_work(service,photo=True)
        self.assertEqual(out['job']['state'],'completed');self.assertEqual(fixture.read_count,1)

    def test_unknown_cost_never_triggers_reread(self):
        service,fixture=self.make_service([read13('2')],unknown_first=True)
        out=self.run_work(service,photo=True)
        self.assertEqual(out['job']['state'],'failed');self.assertEqual(fixture.read_count,1)

    def test_unreadable_condition_one_addressed_reread(self):
        service,fixture=self.make_service([dict(condition=None,question='Уточните знак перед 2.',math=None),read13()])
        out=self.run_work(service,photo=True);self.assertEqual(out['job']['state'],'completed');self.assertEqual(fixture.read_count,2)

    def test_text_type_mismatch_stops_without_calls_and_can_be_corrected(self):
        service,fixture=self.make_service([])
        form=valid_form();form.update(task_type='inequality',task_text='sin(x)=0; x in [0;pi]')
        work=self.support.store.save(form)
        job=service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)['job']
        self.assertEqual(job['state'],'needs_clarification');self.assertEqual(fixture.requests,[])
        self.assertEqual(job['condition_precheck']['status'],'task_type_mismatch')
        with self.assertRaises(ValueError):
            service.confirm_empty_condition(job['id'],dict(literal=job['condition'],confirm_empty=True),background=False)
        fixed=service.confirm_empty_condition(job['id'],dict(literal='x>0',confirm_empty=True),background=False)
        self.assertEqual(fixed['job']['state'],'completed',fixed['job'])
        self.assertEqual([r.stage for r in fixture.requests],['other','solution_ocr','grading'])

    def test_photo_type_mismatch_stops_after_one_read_without_retry(self):
        service,fixture=self.make_service([read13()])
        job=self.run_work(service,task_type='inequality',photo=True)['job']
        self.assertEqual(job['state'],'needs_clarification',job)
        self.assertEqual(job['condition_precheck']['status'],'task_type_mismatch')
        self.assertEqual([r.stage for r in fixture.requests],['statement_ocr'])

    def test_uncertain_photo_still_gets_addressed_reread(self):
        unknown=dict(condition=None,question='Уточните знак сравнения.',math=dict(math13(),relation='>'))
        service,fixture=self.make_service([unknown,read13()])
        job=self.run_work(service,photo=True)['job']
        self.assertEqual(job['state'],'completed',job);self.assertEqual(fixture.read_count,2)
