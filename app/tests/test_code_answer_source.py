"""The optional code hint carries its exact input, never pupil proof or gold."""
from copy import deepcopy
import json
import unittest

from app import condition_precheck as precheck
from app import independent_assessment as assessment
from app.tests import test_independent_assessment as support
from app.tests.test_condition_precheck import math13


class CodeAnswerSourceTests(unittest.TestCase):
    def test_input_bound_before_solver_and_copied_without_simplifying(self):
        original=dict(lhs='(x-1)/(x-1)',rhs='0',relation='>',interval=None,restrictions=['x<5'])
        before=deepcopy(original)
        def solver(value,timeout):
            self.assertEqual(timeout,2)
            self.assertEqual({k:value[k] for k in before},before)
            value['lhs']='1';value['restrictions'].clear()
            return dict(status='solved',answer='synthetic answer',domain='synthetic domain')
        checked=precheck.check(original,'inequality',solver=solver)
        original['lhs']='x'
        ref=precheck.reference(checked)
        self.assertEqual(ref['math'],before)
        ref['math']['restrictions'].append('x>2')
        self.assertEqual(checked['input_math'],before)

    def test_periodic_equation_hint_keeps_interval_but_scope_is_only_13a(self):
        value=math13(left='-pi',right='2*pi');value['restrictions']=['cos(x)>=0']
        checked=precheck.check(value,'trigonometric_equation',solver=lambda *a,**k:dict(status='solved',answer='series'))
        ref=precheck.reference(checked)
        self.assertEqual(ref,dict(scope='13a',answer='series',math=value))

    def test_legacy_or_unsupported_results_do_not_create_unbound_code_hint(self):
        self.assertIsNone(precheck.reference(dict(status='solved',scope='15',answer='x>0')))
        self.assertIsNone(precheck.reference(dict(status='unsupported',scope='15',answer='x>0',input_math={})))

    def request(self,code_answer):
        return assessment.request('compare','inequality','x>0',solution={'solution':'synthetic'},reading={'pages':[]},
            code_answer=code_answer,source_guard='synthetic')

    def test_bound_hint_preserves_literal_condition_even_when_math_disagrees(self):
        hint=dict(scope='15',answer='x<0',math=dict(lhs='x',rhs='0',relation='<',interval=None,restrictions=[]))
        request=self.request(hint);data=json.loads(request.payload['messages'][1]['content'])
        self.assertEqual(data['condition'],'x>0');self.assertEqual(data['code_answer'],hint)
        self.assertEqual(data['student']['lines'],[])
        self.assertIn('не используй этот кодовый ориентир',request.payload['messages'][0]['content'])

    def test_missing_malformed_or_wrong_scope_hint_is_not_sent(self):
        good=dict(scope='15',answer='x>0',math=dict(lhs='x',rhs='0',relation='>',interval=None,restrictions=[]))
        for value in [dict(scope='15',answer='x>0'),dict(good,math=None),dict(good,math={'lhs':'x'}),dict(good,scope='13a')]:
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'independent_code_answer_invalid'):
                self.request(value)

    def test_hint_is_optional_and_never_accepted_by_independent_solver(self):
        self.assertNotIn('code_answer',json.loads(self.request(None).payload['messages'][1]['content']))
        with self.assertRaisesRegex(ValueError,'independent_solver_reference_forbidden'):
            assessment.request('solve','inequality','x>0',source_guard='synthetic',code_answer={})


class CodeHintPipelineTests(unittest.TestCase):
    setUp=support.IndependentTests.setUp
    service=support.IndependentTests.service
    run_work=support.IndependentTests.run_work

    def test_text_and_photo_forward_exact_current_code_input_to_both_graders(self):
        for task in ('inequality','trigonometric_equation'):
            for photo in (False,True):
                with self.subTest(task=task,photo=photo):
                    service,fixture=self.service(task);job=self.run_work(service,task,photo=photo)['job']
                    self.assertEqual(job['state'],'completed',job)
                    data=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
                    self.assertEqual(data['code_answer']['math'],job['condition_precheck']['input_math'])
                    self.assertEqual(data['code_answer']['math'],precheck.from_text(job['condition'],task))
                    solve=next(r for r in fixture.requests if r.stage=='other')
                    self.assertEqual(set(json.loads(solve.payload['messages'][1]['content'])),{'condition','task_number'})

    def test_continuation_recomputes_and_preserves_bound_input(self):
        service,fixture=self.service('inequality',failure='uncertainty');old=self.run_work(service,'inequality')['job']
        plan=service.continuation_plan(old['id']);fixture.failure=None
        new=service.continue_check(old['id'],dict(token=plan['token'],confirm_paid=True,answer='Знак больше нуля.',
            entries=[],pages=[],condition=''),background=False)['job']
        self.assertEqual(new['state'],'completed',new)
        data=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(data['code_answer']['math'],new['condition_precheck']['input_math'])
        self.assertEqual([r.stage for r in fixture.requests],['other','solution_ocr','grading','grading'])


if __name__=='__main__':unittest.main()
