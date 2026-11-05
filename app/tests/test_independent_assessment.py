"""No real accounts, photos, credentials or spending: complete staged synthetic path."""
from copy import deepcopy
from dataclasses import replace
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import independent_assessment as a
from app import independent_reading as reading_scope
from app.call_guard import Policy, ProviderResponse, canonical, digest
from app.deepseek_transport import DeepSeekTransport, parse_response, read_credential
from app.independent_checking import IndependentChecking, CONDITION_VERSION
from app.tests.test_gemini_integration import native_policy
from app.tests import test_fresh_checking as support
from app.tests.test_fresh_checking import Fixture, condition
from app.tests.test_gemini_transport import FakeHTTP, FakeOpener


def policy():
    old=native_policy();rules=deepcopy(old.dispatch_rules)
    for key in list(rules):
        if rules[key]['stage'] in {'statement_ocr','grading'}: del rules[key]
        elif rules[key]['stage']=='solution_ocr':
            rules[key]['settings']['include_thoughts']=True
            from app.strict_reading import response_format
            rules[key]['settings']['response_format']=response_format(reading_scope.schema(int(key.rsplit('-',1)[1])))
            rules[key]['versions'].update(prompt=reading_scope.VERSION,validator=reading_scope.VERSION)
    for stage in ('solve','compare'):
        req=a.request(stage,'trigonometric_equation','x',source_guard='synthetic',
            **(dict(solution={'solution':'x'},reading={'pages':[]}) if stage=='compare' else {}))
        rules[stage]=dict(stage=req.stage,provider=req.provider,model=req.payload['model'],versions=req.versions,
            settings={k:v for k,v in req.payload.items() if k not in {'model','messages','max_tokens'}},
            max_request_bytes=8388608,max_output_tokens=64000,evidence_ref='SYNTHETIC')
    # Photo condition uses the new prompt/version, with the old Gemini wire settings.
    from app.live_checking import VERSION
    rules['condition']=dict(stage='statement_ocr',provider='google',model='gemini-3.8-flash',
        versions=dict(contract='independent-1',pipeline=VERSION,prompt=CONDITION_VERSION,code=CONDITION_VERSION,validator=CONDITION_VERSION),
        settings=dict(stream=False,reasoning_effort='low',response_format={'type':'json_object'}),
        max_request_bytes=8388608,max_output_tokens=6000,evidence_ref='SYNTHETIC')
    return replace(old,max_output_tokens=64000,dispatch_rules=rules,timeout_seconds=300,operation_timeout_seconds=360)


class TwoStageFixture(Fixture):
    kind='live'
    def __init__(self,task_type='trigonometric_equation',failure=None):
        super().__init__(condition(task_type));self.failure=failure;self.raw_reading=None;self.timeouts=[]
        self.solution_text='x=pi*k, k integer; on [0;pi]: 0,pi'
        self.reading_transform=None
    def __call__(self,payload,*,timeout_seconds):
        self.timeouts.append(timeout_seconds)
        if self.stage=='solution_ocr':
            response=super().__call__(payload,timeout_seconds=timeout_seconds)
            value=json.loads(response.content)
            page_schema=payload['response_format']['json_schema']['schema']['properties']['pages']['items']
            if 'page_reading' not in page_schema['properties']:
                # Historical-runner tests explicitly retain their old wire shape.
                from app.compact_reading import schema
                from app.strict_reading import decode_reading
                self.raw_reading=decode_reading(value,schema(len(value['pages'])))
                return response
            for page in value['pages']:
                coverage=page.pop('coverage')
                page.update(page_reading=dict(status=coverage['status'],reason=coverage['reason'],unread_parts=[]),
                    student_work='Видимые ученические записи.')
            value['task_scope']=dict(status='single_task',reason='Одна выбранная задача.')
            if self.reading_transform:self.reading_transform(value)
            try:self.raw_reading=reading_scope.project(value)
            except ValueError:self.raw_reading=None
            return replace(response,content=canonical(value))
        if self.stage=='statement_ocr': value={'condition':
            'x > 0' if self.condition['task_type']=='inequality' else 'sin(x)=0; x in [0; pi]',
            'question':'','math':None}
        elif self.stage=='other': value={'solution':self.solution_text}
        else: value={'score':2,'reason':'Обоснованно решены оба пункта.','errors':[]}
        if self.stage=='grading':
            if self.failure=='missing_field': value.pop('reason')
            if self.failure=='bad_line': value['errors']=[dict(line='9:99',type='typo',explanation='x',criterion='x',hint='x')]
            if self.failure=='uncertainty': value.update(score=None,reason='Уточните знак в строке 1:1.')
        content=value['solution'] if self.stage=='other' else canonical(value)
        if self.stage=='other' and self.failure=='empty':content=''
        if self.stage=='other' and self.failure=='whitespace':content=' \n\t'
        if self.stage=='grading' and self.failure=='invalid_json':content='a score without JSON'
        response=ProviderResponse(content,model=payload['model'],usage={'prompt_tokens':10,'completion_tokens':50},cost_kopecks='1')
        if self.stage=='other' and self.failure=='truncated': response=replace(response,finish_reason='length')
        if self.stage=='other' and self.failure=='unknown_cost': response=replace(response,cost_kopecks=None)
        return response


class IndependentTests(unittest.TestCase):
    def setUp(self):
        self.support=support.FreshTests();self.support.setUp()
        self.addCleanup(self.support.doCleanups);self.addCleanup(self.support.tearDown)
        for name in ('socket.create_connection','app.deepseek_transport.read_credential','app.independent_checking.deepseek_credential'):
            mock=patch(name,side_effect=AssertionError('Real network/secret access forbidden'));mock.start();self.addCleanup(mock.stop)

    def service(self,task_type='trigonometric_equation',failure=None):
        fixture=TwoStageFixture(task_type,failure)
        service=IndependentChecking(self.support.store,policy=policy(),ledger=self.support.root/f'new-ledger-{len(self.support.services)}',transport_factory=fixture.factory)
        service.work_profiles=None;service.upload_scope=None;self.support.services.append(service)
        return service,fixture

    def run_work(self,service,task_type='trigonometric_equation',pages=1,photo=False):
        from app.tests.test_foundation import valid_form,image_data
        form=valid_form();form.update(task_type=task_type,task_text='' if photo else
            'x > 0' if task_type=='inequality' else 'sin(x)=0; x in [0;pi]')
        form['solution']=[image_data() for _ in range(pages)]
        if photo: form['task']=[image_data()]
        work=self.support.store.save(form)
        return service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)

    def test_text_end_to_end_and_blind_first_request(self):
        service,fixture=self.service();out=self.run_work(service)
        self.assertTrue(out['accepted']);self.assertEqual(out['job']['state'],'completed',out['job'])
        self.assertEqual([r.stage for r in fixture.requests],['other','solution_ocr','grading'])
        solve=fixture.requests[0].payload; data=json.loads(solve['messages'][1]['content'])
        self.assertEqual(set(data),{'condition','task_number'})
        self.assertEqual(solve['response_format'],{'type':'text'})
        self.assertNotIn('Верни только JSON по схеме',solve['messages'][0]['content'])
        for req,timeout in zip(fixture.requests,fixture.timeouts):
            if req.provider=='deepseek':
                self.assertEqual(req.payload['reasoning_effort'],'high')
                self.assertEqual(req.payload['thinking'],{'type':'enabled'})
                self.assertGreater(timeout,299);self.assertLessEqual(timeout,300)
        self.assertNotIn('criteria',solve['messages'][0]['content'])
        compare=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(fixture.requests[-1].payload['response_format'],{'type':'json_object'})
        self.assertEqual(compare['independent_solution'],out['job']['independent_solution'])
        self.assertEqual(compare['student'],a.reading_input(fixture.raw_reading))
        self.assertEqual(set(out['job']['result']['response']),{'score','reason','errors'})
        self.assertEqual(service.get(out['job']['id'])['result'],out['job']['result'])
        again=service.create(dict(work_id=out['job']['work_id'],model='G',complete=True,single_task=True),background=False)
        self.assertTrue(again['reused']);self.assertEqual(len(fixture.requests),3)

    def test_photo_condition_and_two_pages(self):
        service,fixture=self.service();out=self.run_work(service,pages=2,photo=True)
        self.assertEqual(out['job']['state'],'completed',out['job'])
        self.assertEqual([r.stage for r in fixture.requests],['statement_ocr','other','solution_ocr','grading'])
        lines=out['job']['result']['reading']['lines'];self.assertTrue(any(l['line'].startswith('2:') for l in lines))

    def test_answer_only_is_graded_from_literal_entries_without_missing_page_question(self):
        service,fixture=self.service('inequality')
        def answer_only(value):
            page=value['pages'][0]
            page['entries']=[dict(kind='math',text='Ответ: x > 0',reading='clear',activity='active',author='student')]
            page['student_work']='Только итоговый ответ, ход решения отсутствует.'
            page['page_reading']['reason']='Весь лист просмотрен; видна только строка ответа.'
        fixture.reading_transform=answer_only
        out=self.run_work(service,'inequality');job=out['job']
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual([r.stage for r in fixture.requests],['other','solution_ocr','grading'])
        data=json.loads(fixture.requests[2].payload['messages'][1]['content'])
        self.assertEqual([line['text'] for line in data['student']['lines']],['Ответ: x > 0'])
        self.assertEqual(data['student']['pages'][0]['coverage']['score_scope'],'accounted')
        self.assertIsNone(job.get('clarification'))
        request_data=json.loads(fixture.requests[1].payload['messages'][1]['content'][0]['text'])
        self.assertEqual(request_data['pages_complete'],'confirmed')
        saved=json.loads((service.directory/job['id']/'reading-scope.json').read_text())
        self.assertEqual(saved['pages'][0]['student_work'],'Только итоговый ответ, ход решения отсутствует.')

    def test_blank_fully_read_page_reaches_grader_without_fabricated_entries(self):
        service,fixture=self.service()
        def blank(value):
            value['pages'][0].update(entries=[],student_work='Пустой лист.')
        fixture.reading_transform=blank
        job=self.run_work(service)['job']
        self.assertEqual(job['state'],'completed',job)
        data=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(data['student']['lines'],[])

    def test_essential_unread_region_stops_before_grading_without_retries(self):
        for impact in ('affects_score','unknown'):
            with self.subTest(impact=impact):
                service,fixture=self.service()
                def unread(value):
                    value['pages'][0]['page_reading'].update(status='partial',
                        unread_parts=[dict(location='Нижняя строка',impact=impact,reason='Не разобран знак; уточните этот фрагмент.')])
                fixture.reading_transform=unread
                job=self.run_work(service)['job']
                self.assertEqual(job['state'],'needs_clarification',job)
                self.assertEqual(job['error_code'],'reading_needs_clarification')
                self.assertIn('Нижняя строка',job['clarification'])
                self.assertEqual(len(fixture.requests),2);self.assertIsNone(job['result_ref'])

    def test_explicit_nonessential_unread_region_keeps_its_reason_and_reaches_grader(self):
        service,fixture=self.service()
        def unread(value):
            value['pages'][0]['page_reading'].update(status='partial',
                unread_parts=[dict(location='Поля листа',impact='nonessential',reason='Неразборчива дата, отдельно от всех математических записей.')])
        fixture.reading_transform=unread
        job=self.run_work(service)['job']
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['result']['reading']['pages'][0]['coverage']['status'],'partial')

    def test_multiple_tasks_or_visible_crop_stops_even_when_submission_confirmed(self):
        for status in ('multiple_tasks','uncertain'):
            with self.subTest(status=status):
                service,fixture=self.service()
                fixture.reading_transform=lambda value:value.update(task_scope=dict(status=status,reason='Уточните выбор задачи или обрезанный край.'))
                job=self.run_work(service)['job']
                self.assertEqual(job['state'],'needs_clarification',job)
                self.assertEqual(len(fixture.requests),2);self.assertIsNone(job['result_ref'])

    def test_missing_reading_status_cannot_be_replaced_by_student_work_note(self):
        service,fixture=self.service()
        fixture.reading_transform=lambda value:value['pages'][0]['page_reading'].pop('status')
        job=self.run_work(service)['job']
        self.assertEqual(job['state'],'failed',job)
        self.assertEqual(job['response_validation']['code'],'strict_reading_response_invalid')
        self.assertEqual(len(fixture.requests),2);self.assertIsNone(job['result_ref'])

    def test_legacy_two_sources_keep_text_priority_and_reopen_saved_result(self):
        from app.tests.test_foundation import valid_form, image_data
        from app.pipeline import input_snapshot
        service, fixture = self.service()
        form = valid_form()
        form.update(task_text='', task=[image_data()])
        work = self.support.store.save(form)
        # Recreate a legacy manifest without going through the new upload rule.
        work['task_text'] = 'sin(x)=0; x in [0;pi]'
        directory = self.support.store.root / work['id']
        (directory / 'work.json').write_text(json.dumps(work))
        materials = {p.name: p.read_bytes() for p in directory.iterdir()}
        guard = input_snapshot(self.support.store, work['id'])[1]
        self.assertIn(work, self.support.store.list())
        request = dict(work_id=work['id'], model='G', complete=True, single_task=True)
        result = service.create(request, background=False)
        self.assertEqual(result['job']['state'], 'completed', result['job'])
        self.assertEqual([r.stage for r in fixture.requests], ['other', 'solution_ocr', 'grading'])
        solve = json.loads(fixture.requests[0].payload['messages'][1]['content'])
        self.assertEqual(solve['condition'], work['task_text'])
        saved = service.get(result['job']['id'])['result']
        attempt = service.directory / result['job']['id']
        attempt_files = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
        service.configuration = digest('synthetic future configuration')
        reopened = service.create(request, background=False)
        self.assertTrue(reopened['reused'])
        self.assertEqual(reopened['job']['id'], result['job']['id'])
        self.assertEqual(reopened['job']['result'], saved)
        self.assertEqual(len(fixture.requests), 3)
        self.assertEqual(input_snapshot(self.support.store, work['id'])[1], guard)
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, materials)
        self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}, attempt_files)

    def test_15_uses_only_15_prompts(self):
        service,fixture=self.service('inequality');out=self.run_work(service,'inequality')
        self.assertEqual(out['job']['state'],'completed',out['job'])
        for req in (fixture.requests[0],fixture.requests[-1]):
            self.assertIn('№15',req.payload['messages'][0]['content']);self.assertNotIn('№13',req.payload['messages'][0]['content'])

    def test_invalid_answer_never_assigns_zero_or_retries(self):
        for failure in ('missing_field','bad_line'):
            with self.subTest(failure=failure):
                service,fixture=self.service(failure=failure);out=self.run_work(service)
                self.assertEqual(out['job']['state'],'failed',out['job']);self.assertIsNone(out['job']['result_ref'])
                self.assertEqual(len(fixture.requests),3)

    def test_first_stage_failure_stops_before_reading(self):
        for failure in ('truncated','unknown_cost','empty','whitespace'):
            with self.subTest(failure=failure):
                service,fixture=self.service(failure=failure);out=self.run_work(service)
                self.assertEqual(out['job']['state'],'failed');self.assertEqual(len(fixture.requests),1)

    def test_solution_text_is_preserved_exactly_in_storage_and_assessment(self):
        text='  Решение:\n'+r'\(x>0\), \frac{1}{x}, \ne, \nu; "кавычки" и \\'+ '\n\tОтвет: ∅.  '
        service,fixture=self.service('inequality');fixture.solution_text=text
        out=self.run_work(service,'inequality');job=out['job']
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['independent_solution'],text)
        stored=json.loads((service.directory/job['id']/'independent-solution.json').read_text())
        self.assertEqual(stored,{'solution':text})
        data=json.loads(fixture.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(data['independent_solution'],text)
        self.assertEqual(job['result']['independent_solution'],text)

    def test_json_looking_solution_is_literal_text_and_not_unwrapped(self):
        service,fixture=self.service();fixture.solution_text=r'{"solution":"\(x>0\)"}'
        job=self.run_work(service)['job']
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['independent_solution'],fixture.solution_text)

    def test_assessment_still_rejects_non_json_and_never_assigns_zero(self):
        service,fixture=self.service(failure='invalid_json');job=self.run_work(service)['job']
        self.assertEqual(job['state'],'failed',job);self.assertIsNone(job['result_ref'])
        self.assertEqual(len(fixture.requests),3)

    def test_nullable_score_and_minimal_error_fields(self):
        service,fixture=self.service(failure='uncertainty');out=self.run_work(service)
        self.assertEqual(out['job']['state'],'completed');self.assertIsNone(out['job']['result']['response']['score'])
        item=dict(line=None,type='typo',explanation='Обоснованная описка.',criterion='На балл не влияет.',hint='Исправьте знак.')
        value=dict(score=2,reason='Обоснованно.',errors=[item]);self.assertEqual(a.validate_assessment(value,set()),value)
        self.assertEqual(set(a.assessment_schema()['properties']['errors']['items']['properties']),{'line','type','explanation','criterion','hint'})

    def test_solver_rejects_reference_injection(self):
        with self.assertRaisesRegex(ValueError,'reference_forbidden'):
            a.request('solve','inequality','x>0',solution={'solution':'secret'},source_guard='x')

    def test_result_tamper_is_detected(self):
        service,_=self.service();out=self.run_work(service);p=service.directory/out['job']['id']/'result-1.json'
        v=json.loads(p.read_text());v['response']['score']=0;p.write_text(canonical(v))
        with self.assertRaisesRegex(ValueError,'artifact_changed'):service.get(out['job']['id'])


class DirectTransportTests(unittest.TestCase):
    def pricing(self):return a.config()['pricing']
    def envelope(self):
        return dict(model='deepseek-flash',id='synthetic',choices=[dict(finish_reason='stop',message={'content':'x'})],
            usage=dict(prompt_tokens=100,prompt_cache_hit_tokens=20,prompt_cache_miss_tokens=80,completion_tokens=50,total_tokens=150,
                completion_tokens_details={'reasoning_tokens':30}))
    def test_exact_wire_single_attempt_300s_and_accounting(self):
        req=a.request('solve','inequality','x>0',source_guard='x'); opener=FakeOpener(FakeHTTP(canonical(self.envelope()).encode()))
        transport=DeepSeekTransport('synthetic-key-123456',allowed_body_hashes=[digest(req.payload)],pricing=self.pricing(),opener=opener)
        response=transport(req.payload,timeout_seconds=300)
        self.assertEqual(len(opener.calls),1);self.assertEqual(opener.calls[0][0].data,canonical(req.payload).encode())
        self.assertEqual(opener.calls[0][0].full_url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(response.cost_basis,'token_estimate');self.assertEqual(response.usage['completion_tokens'],50)
        self.assertNotIn('synthetic-key',repr(transport))
    def test_bad_usage_is_unknown_not_zero(self):
        v=self.envelope();v['usage']['prompt_cache_miss_tokens']=81
        response=parse_response(canonical(v),self.pricing())
        self.assertIsNone(response.cost_kopecks);self.assertEqual(response.finish_reason,'invalid_usage')
    def test_missing_usage_and_http_error_are_unknown(self):
        response=parse_response('{"error":{}}',self.pricing(),status=400)
        self.assertIsNone(response.cost_kopecks)
    def test_credential_reads_only_selected_variable(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key.env';p.write_text('OTHER_API_KEY=forbidden\nDEPSEEK_API_KEY="synthetic-only-key-123456"\n')
            self.assertEqual(read_credential(dict(credential_file=str(p),credential_variable='DEPSEEK_API_KEY')),'synthetic-only-key-123456')
    def test_wrong_body_blocked_before_http(self):
        transport=DeepSeekTransport('synthetic-only-key-123',allowed_body_hashes=[],pricing=self.pricing(),opener=object())
        with self.assertRaisesRegex(ValueError,'unapproved_body'):transport({'model':'deepseek-flash'},timeout_seconds=300)


class HistoricalAccountingTests(unittest.TestCase):
    def test_saved_documented_zero_charge_without_rewriting_unknown_envelope(self):
        from app.call_guard import accounting_provenance
        row=dict(status='http_error_uncharged',cost=0,reported_cost_kopecks='0',response_ref=None)
        envelope=dict(http_status=400,cost_basis='unknown',provider_metadata={'accounting':dict(
            kind='documented_uncharged_error',source='https://ai.google.dev/gemini-api/docs/billing')})
        before=deepcopy(envelope)
        self.assertEqual(accounting_provenance(row,'.',envelope=envelope),('provider_report','https://ai.google.dev/gemini-api/docs/billing'))
        self.assertEqual(envelope,before)
        for changed in ({**row,'cost':1},{**row,'reported_cost_kopecks':'1'}):
            with self.assertRaises(ValueError):accounting_provenance(changed,'.',envelope=envelope)
        with self.assertRaises(ValueError):accounting_provenance(row,'.',envelope={**envelope,'http_status':200})

    def test_documented_zero_with_missing_original_invoice_remains_zero(self):
        from app.call_guard import accounting_summary
        document=dict(envelope=dict(http_status=400,cost_basis='unknown',provider_metadata={'accounting':dict(
            kind='documented_uncharged_error',source='https://ai.google.dev/gemini-api/docs/billing')}),validated=None)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'receipt.json';p.write_text(canonical(document))
            row=dict(id='synthetic',status='http_error_uncharged',cost=0,reported_cost_kopecks=None,
                response_ref=p.name,response_sha256=digest(document))
            value=accounting_summary([row],Path(d))
            self.assertEqual(value['cost_basis'],'provider_report')
            self.assertIsNone(row['reported_cost_kopecks'])
