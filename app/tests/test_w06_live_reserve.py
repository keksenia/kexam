"""One exact historical unknown charge may use its approved budget reserve."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from app.call_guard import CallGuard, digest, canonical, ProviderResponse
from app import w06_live_reserve as risk
from app.independent_checking import IndependentChecking
from app.server import WorkStore
from app.pipeline import input_snapshot
from app.tests.test_foundation import valid_form
from app.tests import test_independent_assessment as support


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.policy=support.policy();self.guard=CallGuard(self.root/'ledger',self.policy)
        self.fixture=support.TwoStageFixture()
        self.request=support.a.request('solve','inequality','x>0',source_guard='synthetic-reserved-w06')
        # Create an unknown timeout in the temporary synthetic ledger.
        class Timeout:
            kind='live';single_attempt=True
            def __call__(self,payload,*,timeout_seconds):raise TimeoutError('synthetic')
        result=self.guard.execute(self.request,Timeout(),lambda value:value,response_text=True)
        self.assertEqual(result.status,'cost_unknown')
        with closing(self.guard._connect()) as con, con:
            con.row_factory=sqlite3.Row
            con.execute("UPDATE calls SET failure_reason='transport_timeout' WHERE id=?",(result.attempt_id,))
            row=dict(con.execute('SELECT * FROM calls WHERE id=?',(result.attempt_id,)).fetchone())
            self.auth=dict(call_id=row['id'],fingerprint=row['fingerprint'],original_row_sha256=digest(row),
                reserve_micro_rub=35_000_000,allowed_requests=risk.authorization()['allowed_requests'])
            con.execute('UPDATE calls SET reserve=? WHERE id=?',(self.auth['reserve_micro_rub'],row['id']))
        mock=patch('app.w06_live_reserve.authorization',return_value=self.auth);mock.start();self.addCleanup(mock.stop)
        mock=patch('app.independent_checking.w06_authorization',return_value=self.auth);mock.start();self.addCleanup(mock.stop)
        self.reserved=risk.ReservedLiveGuard(self.root/'ledger',self.policy)

    def rows(self):
        with closing(self.guard._connect()) as con, con:
            con.row_factory=sqlite3.Row
            return [dict(r) for r in con.execute('SELECT * FROM calls ORDER BY created,id')]

    def fresh_request(self,label='next'):
        return replace(self.request,context={**self.request.context,'synthetic_next':label})

    def test_exact_w06_allowed_and_actual_cost_stays_unknown(self):
        before=self.rows();out=self.reserved.reserve(self.fresh_request())
        self.assertEqual(out.status,'reserved')
        after=self.rows();self.assertEqual(after[0],before[0]);self.assertIsNone(after[0]['cost'])
        self.assertEqual(self.reserved.snapshot()['held_micro_rub'],35_000_000)
        self.assertEqual(self.reserved.snapshot()['cost']['status'],'unknown')
        self.assertEqual(CallGuard.admission_block(before,self.policy),'unresolved_spending')

    def fill_budget(self):
        spent=deepcopy(self.rows()[0])
        spent.update(id='synthetic-spent',fingerprint=digest('synthetic-spent'),state='settled',status='succeeded',reserve=0,
            cost=self.policy.budget_micro_rub-self.auth['reserve_micro_rub'],response_ref=None,response_sha256=None,
            reported_cost_kopecks=str((self.policy.budget_micro_rub-self.auth['reserve_micro_rub'])//10000),failure_reason=None)
        with closing(self.guard._connect()) as con, con:
            keys=list(spent)
            con.execute('INSERT INTO calls('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[spent[k] for k in keys])

    def test_reserve_consumes_budget(self):
        self.fill_budget()
        self.assertEqual(self.reserved.reserve(self.fresh_request()).status,'budget_limit')

    def test_different_unknown_call_still_blocks(self):
        out=self.reserved.execute(self.fresh_request('new-unknown'),
            type('Unknown',(),{'kind':'live','single_attempt':True,'__call__':lambda self,payload,timeout_seconds:ProviderResponse('{}',model=payload['model'])})(),lambda v:v,response_text=True)
        self.assertEqual(out.status,'cost_unknown')
        self.assertEqual(self.reserved.reserve(self.fresh_request('blocked')).status,'unresolved_spending')

    def test_changed_historical_row_or_missing_reserve_blocks(self):
        original=self.rows()[0]
        for field,value in [('reserve',0),('reserve',36_000_000),('fingerprint','modified'),('failure_reason','transport_failure')]:
            with self.subTest(field=field,value=value):
                with closing(self.guard._connect()) as con, con:con.execute(f'UPDATE calls SET {field}=? WHERE id=?',(value,self.auth['call_id']))
                self.assertEqual(self.reserved.reserve(self.fresh_request()).status,'unresolved_spending')
                with closing(self.guard._connect()) as con, con:con.execute(f'UPDATE calls SET {field}=? WHERE id=?',(original[field],self.auth['call_id']))

    def test_active_call_blocks(self):
        self.assertEqual(self.reserved.reserve(self.fresh_request('active')).status,'reserved')
        self.assertEqual(self.reserved.reserve(self.fresh_request('blocked')).status,'request_in_flight')

    def test_no_retry_and_no_other_scope(self):
        self.assertEqual(self.reserved.reserve(self.fresh_request(),retry=True).status,'retry_forbidden')
        forbidden=replace(self.fresh_request(),stage='feedback')
        self.assertEqual(self.reserved.reserve(forbidden).status,'w06_reserve_scope')

    def service(self):
        store=WorkStore(self.root/'works');store.root.mkdir(exist_ok=True)
        service=IndependentChecking(store,policy=self.policy,ledger=self.root/'ledger',transport_factory=self.fixture.factory)
        service.work_profiles=None;service.upload_scope=None;self.addCleanup(service.close)
        return service,store

    def test_availability_and_full_stages_use_the_same_exception(self):
        service,store=self.service();work=store.save(valid_form())
        before=self.rows()[0];availability=service.availability(work_id=work['id'])
        self.assertTrue(availability['paid_api_enabled'],availability)
        self.assertEqual(availability['spending']['committed_micro_rub'],35_000_000)
        out=service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)
        self.assertEqual(out['job']['state'],'completed',out['job'])
        self.assertEqual([r.stage for r in self.fixture.requests],['other','solution_ocr','grading'])
        self.assertEqual(self.rows()[0],before)

    def test_availability_includes_reserve_in_budget_stop(self):
        self.fill_budget();service,store=self.service()
        self.assertEqual(service.availability()['reason'],'budget_limit')
