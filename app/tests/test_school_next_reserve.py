"""The S052 exception retains all older reserves and blocks any new unknown."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app import live_reserves as risk
from app.call_guard import digest
from app.tests import test_school_reserve as support
from app.tests.test_foundation import valid_form


class NextSchoolReserveTests(support.SchoolReserveTests):
    def setUp(self):
        super().setUp()
        row=deepcopy(self.rows()[0])
        row.update(id='synthetic-S052',fingerprint=digest('S052'),reserve=0,
            failure_reason='transport_failure',created=row['created']+4)
        self.live_auth['reserves'].append(dict(case='S052',call_id=row['id'],fingerprint=row['fingerprint'],
            original_row_sha256=digest(row),reserve_micro_rub=35000000,failure_reason='transport_failure'))
        row['reserve']=35000000
        with closing(self.guard._connect()) as con,con:
            keys=list(row)
            con.execute('INSERT INTO calls('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])
        self.reserved=risk.LiveReservedGuard(self.root/'ledger',self.policy)

    def test_exact_w06_allowed_and_actual_cost_stays_unknown(self):
        before=self.rows()
        self.assertEqual(self.reserved.reserve(self.fresh_request()).status,'reserved')
        self.assertEqual([r for r in self.rows() if r['id'] in {b['id'] for b in before}],before)
        self.assertEqual(self.reserved.snapshot()['held_micro_rub'],175000000)
        self.assertTrue(all(r['cost'] is None for r in before))

    def test_availability_and_full_stages_use_the_same_exception(self):
        service,store=self.service();work=store.save(valid_form());before=self.rows()
        available=service.availability(work_id=work['id'])
        self.assertTrue(available['paid_api_enabled'],available)
        self.assertEqual(available['spending']['committed_micro_rub'],175000000)
        out=service.create(dict(work_id=work['id'],model='G',complete=True,single_task=True),background=False)
        self.assertEqual(out['job']['state'],'completed',out['job'])
        self.assertEqual([r.stage for r in self.fixture.requests],['other','solution_ocr','grading'])
        self.assertEqual([r for r in self.rows() if r['id'] in {b['id'] for b in before}],before)


class NextSchoolAuthorizationTests(unittest.TestCase):
    def test_next_authorization_is_exact_and_human_only(self):
        original=json.loads(risk.SCHOOL_NEXT_AUTH_PATH.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'authorization.json';path.write_text(json.dumps(original))
            with patch.object(risk,'SCHOOL_NEXT_AUTH_PATH',path):
                self.assertEqual({r['case'] for r in risk.authorization()['reserves']},{'G13-16','G13-10','S046','S052'})
                for field,value in [('agent_paid_launch_authorized',True),('automatic_retries',1),('prior_authorization_sha256','changed')]:
                    changed=deepcopy(original);changed[field]=value;path.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError,'school_next_reserve_authorization_changed'):risk.authorization()
                for field,value in [('call_id','different'),('reserve_micro_rub',0),('fingerprint','changed'),('original_row_sha256','changed')]:
                    changed=deepcopy(original);changed['reserve_target'][field]=value;path.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError,'school_next_reserve_authorization_changed'):risk.authorization()
