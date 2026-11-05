"""Synthetic ledger verifies all three retained reserves in the ordinary path."""
from copy import deepcopy
from contextlib import closing
from unittest.mock import patch
from app import live_reserves as risk
from app.call_guard import digest
from app.tests import test_w06_live_reserve as support


class LiveReserveTests(support.ReserveTests):
    def setUp(self):
        super().setUp()
        targets = []
        for index, case in enumerate(('G13-16', 'G13-10'), 1):
            row = deepcopy(self.rows()[0])
            row.update(id='synthetic-' + case, fingerprint=digest(case), reserve=0,
                       failure_reason='transport_failure', created=row['created'] + index)
            targets.append(dict(case=case, call_id=row['id'], fingerprint=row['fingerprint'],
                                original_row_sha256=digest(row), reserve_micro_rub=35_000_000,
                                failure_reason='transport_failure'))
            row['reserve'] = 35_000_000
            with closing(self.guard._connect()) as con, con:
                keys = list(row)
                con.execute('INSERT INTO calls(' + ','.join(keys) + ') VALUES('
                            + ','.join('?' for _ in keys) + ')', [row[k] for k in keys])
        self.live_auth = dict(reserves=targets)
        for name in ('app.live_reserves.authorization', 'app.independent_checking.live_reserve_authorization'):
            mock = patch(name, return_value=self.live_auth)
            mock.start(); self.addCleanup(mock.stop)
        self.reserved = risk.LiveReservedGuard(self.root/'ledger', self.policy)

    def test_exact_w06_allowed_and_actual_cost_stays_unknown(self):
        before = self.rows()
        self.assertEqual(self.reserved.reserve(self.fresh_request()).status, 'reserved')
        self.assertEqual([r for r in self.rows() if r['id'] in {b['id'] for b in before}], before)
        self.assertEqual(self.reserved.snapshot()['held_micro_rub'], 105_000_000)
        self.assertTrue(all(r['cost'] is None for r in self.rows() if r['id'] in {b['id'] for b in before}))

    def test_availability_and_full_stages_use_the_same_exception(self):
        from app.tests.test_foundation import valid_form
        service, store = self.service(); work = store.save(valid_form())
        before = self.rows()
        availability = service.availability(work_id=work['id'])
        self.assertTrue(availability['paid_api_enabled'], availability)
        self.assertEqual(availability['spending']['committed_micro_rub'], 105_000_000)
        out = service.create(dict(work_id=work['id'], model='G', complete=True, single_task=True), background=False)
        self.assertEqual(out['job']['state'], 'completed', out['job'])
        self.assertEqual([r.stage for r in self.fixture.requests], ['other', 'solution_ocr', 'grading'])
        self.assertEqual([r for r in self.rows() if r['id'] in {b['id'] for b in before}], before)

    def test_each_additional_row_is_verified_in_full(self):
        for target in self.live_auth['reserves']:
            for field, value in [('reserve', 0), ('fingerprint', 'modified'), ('latency_ms', 999), ('usage', '{}')]:
                with self.subTest(case=target['case'], field=field):
                    original = next(r for r in self.rows() if r['id'] == target['call_id'])
                    with closing(self.guard._connect()) as con, con:
                        con.execute(f'UPDATE calls SET {field}=? WHERE id=?', (value, target['call_id']))
                    self.assertEqual(self.reserved.reserve(self.fresh_request()).status, 'unresolved_spending')
                    with closing(self.guard._connect()) as con, con:
                        con.execute(f'UPDATE calls SET {field}=? WHERE id=?', (original[field], target['call_id']))

    def test_changed_authorization_stops_dispatch(self):
        with patch('app.live_reserves.authorization', return_value={'reserves': []}):
            self.assertEqual(self.reserved.reserve(self.fresh_request()).status, 'live_reserve_authorization_changed')
