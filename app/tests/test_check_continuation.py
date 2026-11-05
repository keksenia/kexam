"""Stage accounting and provenance, using synthetic images and isolated ledgers."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import sqlite3
import tempfile
import http.client
import socket
import threading
import time
REAL_SOCKET_CONNECT = socket.socket.connect

from app.call_guard import digest
from app.tests import test_independent_assessment as support_tests


class ContinuationTests(unittest.TestCase):
    setUp = support_tests.IndependentTests.setUp
    service = support_tests.IndependentTests.service
    run_work = support_tests.IndependentTests.run_work

    def form(self, plan, **values):
        result=dict(token=plan['token'],confirm_paid=True,answer='',entries=[],pages=[],condition='')
        result.update(values);return result

    def test_answer_repeats_only_grading_and_keeps_original_result(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];directory=s.directory/old['id']
        before={p.name:p.read_bytes() for p in directory.glob('*.json')}
        plan=s.continuation_plan(old['id']);self.assertTrue(plan['available'],plan);self.assertEqual(plan['requests'],1)
        f.failure=None;form=self.form(plan,answer='В строке 1:1 написан знак равенства.')
        out=s.continue_check(old['id'],form,background=False);new=out['job']
        self.assertEqual(new['state'],'completed',new);self.assertEqual(new['result']['response']['score'],2)
        self.assertEqual([r.stage for r in f.requests],['other','solution_ocr','grading','grading'])
        self.assertEqual(new['continuation']['parent_id'],old['id'])
        for name,raw in before.items():self.assertEqual((directory/name).read_bytes(),raw,name)
        data=json.loads(f.requests[-1].payload['messages'][1]['content'])
        self.assertEqual(data['human_reading_clarifications'][-1]['answer'],form['answer'])
        self.assertEqual(data['student'],json.loads(f.requests[2].payload['messages'][1]['content'])['student'])
        again=s.continue_check(old['id'],form,background=False)
        self.assertTrue(again['reused']);self.assertEqual(again['job']['id'],new['id']);self.assertEqual(len(f.requests),4)
        plan_again=s.continuation_plan(old['id']);self.assertEqual(plan_again['existing_job_id'],new['id'])
        blocked=s.continue_check(old['id'],{**form,'answer':'Другое уточнение старой попытки'},background=False)
        self.assertFalse(blocked['accepted']);self.assertEqual(blocked['reason'],'continuation_exists');self.assertEqual(len(f.requests),4)

    def test_explicit_partial_page_confirmation_and_literal_patch(self):
        s,f=self.service()
        def partial(v):
            v['pages'][0]['page_reading'].update(status='partial',unread_parts=[dict(location='знак первой строки',impact='unknown',reason='Неясен знак.')])
        f.reading_transform=partial;old=self.run_work(s)['job'];self.assertEqual(old['error_code'],'reading_needs_clarification')
        plan=s.continuation_plan(old['id']);self.assertEqual(plan['unresolved_pages'],[1])
        edits=[dict(line='1:1',text='sin(x)=0',activity='active')]
        out=s.continue_check(old['id'],self.form(plan,entries=edits,pages=[1]),background=False)['job']
        self.assertEqual(out['state'],'completed',out)
        self.assertEqual([r.stage for r in f.requests],['other','solution_ocr','grading'])
        self.assertEqual(out['result']['reading']['lines'][0]['text'],'sin(x)=0')
        self.assertEqual(s.get(old['id'])['state'],'needs_clarification')

    def test_known_grading_format_failure_reuses_two_accepted_stages(self):
        s,f=self.service(failure='invalid_json');old=self.run_work(s)['job'];self.assertEqual(old['state'],'failed')
        p=s.continuation_plan(old['id']);self.assertEqual(p['stage'],'assessment');f.failure=None
        new=s.continue_check(old['id'],self.form(p),background=False)['job']
        self.assertEqual(new['state'],'completed',new);self.assertEqual(len(f.requests),4)

    def test_known_solver_format_failure_starts_at_solver(self):
        s,f=self.service(failure='empty');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        self.assertEqual(p['stage'],'solving');self.assertEqual(p['requests'],3);f.failure=None
        new=s.continue_check(old['id'],self.form(p),background=False)['job']
        self.assertEqual(new['state'],'completed',new)
        self.assertEqual([r.stage for r in f.requests],['other','other','solution_ocr','grading'])

    def test_unknown_spend_never_becomes_retry_permission(self):
        s,f=self.service(failure='unknown_cost');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        self.assertFalse(p['available']);self.assertEqual(p['reason'],'unresolved_spending')
        # A caller cannot bypass the plan by supplying a valid-looking token.
        snap=s._continuation_snapshot(old['id'])
        out=s.continue_check(old['id'],self.form(dict(token=snap['token'])),background=False)
        self.assertFalse(out['accepted']);self.assertEqual(len(f.requests),1)

    def test_cached_output_tamper_and_changed_input_block_without_calls(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];d=s.directory/old['id']
        p=s.continuation_plan(old['id']);(d/'independent-solution.json').write_text('{"solution":"invented"}')
        self.assertEqual(s.continuation_plan(old['id'])['reason'],'continuation_artifact_changed')
        self.assertEqual(len(f.requests),3)

    def test_stale_plan_and_missing_paid_confirmation_block(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        with self.assertRaisesRegex(ValueError,'continuation_invalid'):
            s.continue_check(old['id'],self.form(p,confirm_paid=False),background=False)
        with self.assertRaisesRegex(ValueError,'continuation_stale'):
            s.continue_check(old['id'],self.form(p,token='0'*64,answer='Знак ='),background=False)
        self.assertEqual(len(f.requests),3)

    def test_no_mass_ocr_edit_and_no_empty_answer_to_question(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        for form in [self.form(p),self.form(p,entries=[dict(line='9:999',text='x',activity='active')])]:
            with self.assertRaisesRegex(ValueError,'continuation_invalid'):s.continue_check(old['id'],form,background=False)
        self.assertEqual(len(f.requests),3)

    def test_completed_grade_has_no_repeat_action(self):
        s,f=self.service();old=self.run_work(s)['job']
        self.assertEqual(s.continuation_plan(old['id'])['reason'],'continuation_not_applicable')
        self.assertEqual(len(f.requests),3)

    def test_second_clarification_keeps_first_literal_correction(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        first=s.continue_check(old['id'],self.form(p,entries=[dict(line='1:1',text='sin(x)=0',activity='active')]),background=False)['job']
        p2=s.continuation_plan(first['id']);self.assertTrue(p2['available'],p2);f.failure=None
        final=s.continue_check(first['id'],self.form(p2,answer='Подтверждаю знак.'),background=False)['job']
        self.assertEqual(final['state'],'completed',final)
        self.assertEqual(final['result']['reading']['lines'][0]['text'],'sin(x)=0')
        self.assertEqual(len(f.requests),5)

    def test_child_uses_current_configuration_after_upgrade(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];d=s.directory/old['id']
        value=json.loads((d/'job.json').read_text());value['configuration']['version']='older-pipeline'
        (d/'job.json').write_text(json.dumps(value));p=s.continuation_plan(old['id']);f.failure=None
        new=s.continue_check(old['id'],self.form(p,answer='Подтверждаю знак.'),background=False)['job']
        self.assertEqual(new['state'],'completed',new);self.assertEqual(new['configuration'],s.configuration)

    def test_reading_failure_repeats_reading_and_grading_only(self):
        s,f=self.service()
        f.reading_transform=lambda v:v['pages'][0].update(entries=[dict(kind='math',text=None,reading='clear',activity='active',author='student')])
        old=self.run_work(s)['job'];self.assertEqual(old['state'],'failed',old)
        p=s.continuation_plan(old['id']);self.assertEqual(p['stage'],'reading');self.assertEqual(p['requests'],2)
        f.reading_transform=None
        new=s.continue_check(old['id'],self.form(p),background=False)['job']
        self.assertEqual(new['state'],'completed',new)
        self.assertEqual([r.stage for r in f.requests],['other','solution_ocr','solution_ocr','grading'])

    def test_school_batch_limit_counts_ocr_without_source_guard(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);index=root/'data_local/m17/stobal-82-2026-10-26/index.json';index.parent.mkdir(parents=True)
            index.write_text(json.dumps(dict(cases=[dict(work_id=old['work_id'],input_guard=old['guard'])])))
            auth=root/'planning/m17/stobal_82_2026-10-26/S052_RECOVERY_AUTHORIZATION.json';auth.parent.mkdir(parents=True)
            auth.write_text('{"max_total_batch_requests":3}')
            with patch('app.check_continuation.ROOT',root):
                p=s.continuation_plan(old['id']);self.assertFalse(p['available']);self.assertEqual(p['reason'],'continuation_batch_limit')
        self.assertEqual(len(f.requests),3)

    def test_changed_photo_input_blocks_plan(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job']
        path=s.work_store.root/old['work_id']/'solution_1.png';path.write_bytes(path.read_bytes()+b'changed')
        self.assertEqual(s.continuation_plan(old['id'])['reason'],'input_changed');self.assertEqual(len(f.requests),3)

    def test_original_ledger_response_is_verified_before_reuse(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job']
        with sqlite3.connect(s.ledger/'spending.sqlite3') as c:
            ref=c.execute('SELECT response_ref FROM calls ORDER BY rowid LIMIT 1').fetchone()[0]
        path=s.ledger/ref;value=json.loads(path.read_text());value['validated']['solution']='forged';path.write_text(json.dumps(value))
        self.assertEqual(s.continuation_plan(old['id'])['reason'],'continuation_artifact_changed');self.assertEqual(len(f.requests),3)

    def test_budget_and_other_inflight_owner_block_without_calls(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job']
        with sqlite3.connect(s.ledger/'spending.sqlite3') as c:c.execute('UPDATE calls SET cost=? WHERE rowid=1',(s.policy.budget_micro_rub,))
        self.assertEqual(s.continuation_plan(old['id'])['reason'],'budget_limit');self.assertEqual(len(f.requests),3)

    def test_partial_page_is_not_cleared_by_freeform_answer(self):
        s,f=self.service()
        def partial(v):v['pages'][0]['page_reading'].update(status='partial',unread_parts=[dict(location='знак',impact='unknown',reason='Неясен знак.')])
        f.reading_transform=partial;old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        with self.assertRaisesRegex(ValueError,'continuation_invalid'):
            s.continue_check(old['id'],self.form(p,answer='Всё нормально'),background=False)
        self.assertEqual(len(f.requests),2)

    def test_http_continuation_is_explicit_and_idempotent(self):
        from app import server
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];f.failure=None
        with patch('app.independent_checking.load_policy',return_value=None),patch('app.live_checking.configured_policy',return_value=None):
            httpd=server.make_server(0,s.work_store.root)
        httpd.fresh.close();httpd.fresh=s
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def call(method,path,body=None,origin=True):
            conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            # The test's global outbound-network prohibition remains in force.
            try:
                def local_connect(sock,address):
                    self.assertEqual(address,('127.0.0.1',httpd.server_port));return REAL_SOCKET_CONNECT(sock,address)
                conn.sock=socket.socket();conn.sock.settimeout(5)
                with patch('socket.socket.connect',new=local_connect):conn.sock.connect(('127.0.0.1',httpd.server_port))
                headers={'Content-Type':'application/json'}
                if origin:headers['Origin']=f'http://127.0.0.1:{httpd.server_port}'
                conn.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
                response=conn.getresponse();return response.status,json.loads(response.read())
            finally:conn.close()
        try:
            code,p=call('GET','/api/checks/'+old['id']+'/continuation');self.assertEqual(code,200);self.assertEqual(p['requests'],1)
            path='/api/checks/'+old['id']+'/continue';form=self.form(p,answer='Видно равенство.')
            self.assertEqual(call('POST',path,form,origin=False)[0],403)
            self.assertEqual(call('POST',path,self.form(p,confirm_paid=False))[0],409)
            code,out=call('POST',path,form);self.assertEqual(code,202,out)
            for _ in range(100):
                new=s.get(out['job']['id'])
                if new['state'] not in {'queued','running'}:break
                time.sleep(.02)
            self.assertEqual(new['state'],'completed',new)
            self.assertTrue(call('POST',path,form)[1]['reused']);self.assertEqual(len(f.requests),4)
        finally:httpd.shutdown();httpd.server_close();thread.join(timeout=2)

    def test_whole_unreadable_page_requires_reading_not_checkbox(self):
        s,f=self.service()
        def unread(v):
            v['pages'][0].update(entries=[])
            v['pages'][0]['page_reading'].update(status='unreadable',unread_parts=[dict(location='Вся страница',impact='unknown',reason='Страница не прочитана.')])
        f.reading_transform=unread;old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        self.assertEqual(p['stage'],'reading');self.assertEqual(p['requests'],2);self.assertIsNone(p['reading'])
        with self.assertRaisesRegex(ValueError,'continuation_invalid'):
            s.continue_check(old['id'],self.form(p,pages=[1]),background=False)
        f.reading_transform=None;new=s.continue_check(old['id'],self.form(p),background=False)['job']
        self.assertEqual(new['state'],'completed',new)
        self.assertEqual([r.stage for r in f.requests],['other','solution_ocr','solution_ocr','grading'])

    def test_accepted_grade_is_restored_locally_after_result_write_interruption(self):
        s,f=self.service();old=self.run_work(s)['job'];path=s.directory/old['id']/'job.json'
        value=json.loads(path.read_text());value.update(state='interrupted',stage='interrupted',result_ref=None);value.pop('result_sha256',None);path.write_text(json.dumps(value))
        plan=s.continuation_plan(old['id']);self.assertTrue(plan['local_recovery']);self.assertEqual(plan['requests'],0)
        restored=s.continue_check(old['id'],self.form(plan,confirm_paid=False),background=False)['job']
        self.assertEqual(restored['result']['response'],old['result']['response']);self.assertEqual(len(f.requests),3)
        self.assertEqual(s.get(old['id'])['state'],'interrupted')

    def test_queued_child_interruption_keeps_reused_stages(self):
        s,f=self.service(failure='uncertainty');old=self.run_work(s)['job'];plan=s.continuation_plan(old['id'])
        with patch.object(s.pool,'submit'):
            child=s.continue_check(old['id'],self.form(plan,answer='Знак ='),background=True)['job']
        path=s.directory/child['id']/'job.json';value=json.loads(path.read_text());value.update(state='interrupted',stage='interrupted');path.write_text(json.dumps(value))
        plan=s.continuation_plan(child['id']);self.assertEqual(plan['stage'],'assessment');self.assertEqual(plan['requests'],1)
        f.failure=None;final=s.continue_check(child['id'],self.form(plan),background=False)['job']
        self.assertEqual(final['state'],'completed',final);self.assertEqual(len(f.requests),4)

    def test_new_continuation_id_cannot_bypass_unchanged_request_attempt_limit(self):
        s,f=self.service(failure='invalid_json');old=self.run_work(s)['job'];p=s.continuation_plan(old['id'])
        first=s.continue_check(old['id'],self.form(p),background=False)['job'];self.assertEqual(first['state'],'failed')
        p=s.continuation_plan(first['id']);second=s.continue_check(first['id'],self.form(p),background=False)['job']
        self.assertEqual(second['error_code'],'attempt_limit',second);self.assertEqual(len(f.requests),4)


if __name__=='__main__':unittest.main()
