"""Authorized upload expansion and financial regressions; synthetic data only."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from app.call_guard import canonical
from app.live_checking import FreshChecking
from app.live_condition import solve_condition
from app.pipeline import input_snapshot
from app.provider_requests import model_image
from app.server import WorkStore, MAX_IMAGE_BYTES, MAX_BODY
from app.tests.test_foundation import valid_form, image_data
from app.tests.test_fresh_checking import Fixture, condition, fixture_policy


class UploadExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.store=WorkStore(self.root/'works')
        self.fixture=Fixture(condition())
        self.service=FreshChecking(self.store,policy=fixture_policy(),ledger=self.root/'ledger',transport_factory=self.fixture.factory)
        self.addCleanup(self.service.close)
        self.network=patch('socket.socket.connect',side_effect=AssertionError('No network')); self.network.start()
        self.addCleanup(self.network.stop)

    def work(self, pages=1):
        form=valid_form(); form['solution']=[image_data()]*pages; form['task_text']='';form['task']=[image_data()]
        return self.store.save(form)

    def launch(self, work):
        return self.service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)

    def test_all_ten_page_counts_admitted_before_any_call(self):
        for count in range(1,11):
            work=self.work(count); source,guard,_=input_snapshot(self.store,work['id'])
            self.service._preflight_assets(work['id'],'G',source,guard)
        self.assertEqual(self.fixture.requests,[])
        self.assertFalse((self.root/'ledger').exists())

    def test_policy_drift_rejected_before_condition_is_paid(self):
        policy=fixture_policy(); rules=deepcopy(policy.ceiling_rules)
        rules={key:rule for key,rule in rules.items() if key!='solution_ocr-1'}
        self.service.policy=replace(policy,ceiling_rules=rules)
        response=self.launch(self.work())
        self.assertFalse(response['accepted']);self.assertEqual(response['reason'],'billing_ceiling_unproven')
        self.assertEqual(self.fixture.requests,[])

    def test_code_upgrade_reopens_saved_attempt_without_cost(self):
        work=self.work(); first=self.launch(work)
        self.assertEqual(first['job']['state'],'completed')
        self.service.configuration={**self.service.configuration,'version':'upgraded-for-test'}
        second=self.launch(work)
        self.assertTrue(second['reused']);self.assertEqual(first['job']['id'],second['job']['id'])
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual(self.service.availability(work_id=work['id'])['models'][0]['reason'],'authorized_checks_completed')

    def test_unlisted_upload_gets_gemini_admission_with_dynamic_scope(self):
        self.service.work_profiles={'Eold':'G'}
        self.service.upload_scope={'kind':'all_saved_uploads','model':'G'}
        work=self.work()
        self.assertTrue(self.service.availability()['paid_api_enabled'])
        self.assertTrue(self.service.availability(work_id=work['id'])['models'][0]['available'])
        self.assertFalse(self.service.availability(work_id=work['id'])['models'][1]['available'])

    def test_read_has_one_schema_and_assessment_uses_medium(self):
        self.assertEqual(self.launch(self.work())['job']['state'],'completed')
        read=self.fixture.requests[1].payload
        schema=read['response_format']['json_schema']['schema']
        self.assertNotIn(canonical(schema),read['messages'][0]['content'])
        self.assertNotIn('"additionalProperties":false',read['messages'][0]['content'])
        self.assertEqual(self.fixture.requests[2].payload['reasoning_effort'],'medium')

    def test_m01_rule_survives_failed_read_in_job(self):
        class BadCoverage(Fixture):
            def __call__(self,payload,**options):
                response=super().__call__(payload,**options)
                if self.stage=='solution_ocr':
                    value=json.loads(response.content)
                    value['pages'][0]['coverage']['status']='unreadable'
                    response=replace(response,content=canonical(value))
                return response
        bad=BadCoverage(condition());self.service.transport_factory=bad.factory
        job=self.launch(self.work())['job']
        self.assertEqual(job['response_validation']['code'],'full_m01_contract')
        self.assertTrue(job['response_validation']['m01_errors'])
        self.assertIn('coverage',str(job['response_validation']['m01_errors']))
        self.assertEqual(len(bad.requests),2)

    def test_35_mib_photo_fits_upload_envelope(self):
        self.assertEqual(MAX_IMAGE_BYTES,35*1024*1024)
        self.assertGreater(MAX_BODY,20*((MAX_IMAGE_BYTES+2)//3*4))

    def test_separate_model_copy_is_bounded_deterministic_and_preserves_source(self):
        # Noise PNG approximates the byte size of a phone photo; no private image.
        image=Image.effect_noise((2200,2200),40).convert('RGB'); output=BytesIO();image.save(output,format='PNG')
        raw=output.getvalue();sha=hashlib.sha256(raw).hexdigest()
        first=model_image(raw,max_bytes=1500000);second=model_image(raw,max_bytes=1500000)
        self.assertEqual(first,second);self.assertEqual(hashlib.sha256(raw).hexdigest(),sha)
        self.assertLessEqual(len(first[0]),1500000);self.assertEqual(first[1],'image/jpeg')
        self.assertEqual(first[2]['source_size'],[2200,2200])


class ExpandedSolverTests(unittest.TestCase):
    def test_exact_new_families_and_original_domains(self):
        cases=[('log(x,2)','2','>=','Interval(4, oo)'),
            ('(log(x,2)-1)*(log(x,2)-3)','0','<=','Interval(2, 8)'),
            ('log(x,1/2)','-1','>','Interval.open(0, 2)'),
            ('2**(2*x)-5*2**x+4','0','<=','Interval(0, 2)'),
            ('4**x-5*2**x+4','0','<=','Interval(0, 2)'),
            ('exp(2*x)-3*exp(x)+2','0','<=','Interval(0, log(2))'),
            ('(1/2)**x','4','<','Interval.open(-2, oo)'),
            ('sqrt(x-1)','2','<=','Interval(1, 5)'),
            ('sqrt(x)**2','1','<=','Interval(0, 1)'),
            ('sqrt(x)/sqrt(x)','1','>=','Interval.open(0, oo)'),
            ('log(x-1,2)-log(x-1,2)','0','>=','Interval.open(1, oo)'),
            ('log(x,x-1)','0','>','Interval.open(2, oo)'),
            ('x**8-1','0','<=','Interval(-1, 1)')]
        for lhs,rhs,relation,expected in cases:
            with self.subTest(lhs=lhs):
                answer=solve_condition(condition('inequality',lhs=lhs,rhs=rhs,relation=relation))
                self.assertEqual(answer['status'],'solved',answer)
                self.assertEqual(answer['answer'],expected)

    def test_restrictions_interval_and_excluded_point_are_kept(self):
        c=condition('inequality',lhs='x**2-4',relation='<=',restrictions=['0 < x <= 3','x != 1'],
            interval=dict(left='0',right='2',left_open=True,right_open=True))
        answer=solve_condition(c)
        self.assertEqual(answer['answer'],'Union(Interval.open(0, 1), Interval.open(1, 2))')

    def test_unsupported_restrictions_and_unsafe_ast_never_get_a_key(self):
        for changes in (dict(restrictions=['x in [0,1]']),dict(lhs="__import__('os').system('x')"),
                dict(lhs='x**(1/3)'),dict(lhs='x**x'),dict(lhs='x**1000')):
            self.assertEqual(solve_condition(condition('inequality',**changes))['status'],'unsupported')
