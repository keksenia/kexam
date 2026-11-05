"""Failure/race/admission regressions: never dispatch an actual model request."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.live_checking import FreshChecking, atomic_json
from app.call_guard import CallGuard, ProviderResponse, canonical
from app.hubris_transport import parse_response
from app.tests.test_fresh_checking import Fixture, fixture_policy, condition
from app.tests.test_foundation import valid_form, image_data
from app.server import WorkStore


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=WorkStore(self.root/"works")
        self.fixture=Fixture(condition())
        self.service=FreshChecking(self.store,policy=fixture_policy(),ledger=self.root/"ledger",transport_factory=self.fixture.factory)
        self.addCleanup(self.service.close)
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect",side_effect=AssertionError("No external network")).start()
        self.work=self.store.save(valid_form())
        self.form=dict(work_id=self.work["id"],model="G",complete=True,single_task=True)

    def run_work(self): return self.service.create(self.form,background=False)["job"]

    def seed(self,n):
        from app.tests.test_fresh_checking import FreshTests
        FreshTests.seed_settled_calls(self,self.service,self.fixture,n)

    def test_bad_form_types_do_not_crash_or_read_key(self):
        for form in ([],None,{**self.form,"model":[]},{**self.form,"work_id":1},{**self.form,"complete":1}):
            with self.subTest(form=form),self.assertRaises(ValueError):self.service.create(form)
        self.assertFalse(self.fixture.requests)

    def test_completed_job_reused_after_allowance_is_exhausted(self):
        job=self.run_work();self.seed(13)
        count=len(self.fixture.requests)
        result=self.service.create(self.form,background=False)
        self.assertTrue(result["reused"]);self.assertEqual(result["job"]["id"],job["id"])
        self.assertEqual(len(self.fixture.requests),count)

    def test_duplicate_runtime_invocation_does_not_overwrite_completed_result(self):
        job=self.run_work();before=(self.service.directory/job["id"]/"job.json").read_bytes()
        self.service._run(job["id"])
        self.assertEqual(len(self.fixture.requests),3)
        self.assertEqual((self.service.directory/job["id"]/"job.json").read_bytes(),before)

    def test_large_solution_is_rejected_before_any_condition_charge(self):
        with patch("app.live_checking.BODY_LIMIT",100):
            response=self.service.create(self.form,background=False)
        self.assertFalse(response["accepted"]);self.assertEqual(response["reason"],"http_body_limit")
        self.assertFalse(self.fixture.requests);self.assertFalse((self.root/"ledger").exists())

    def test_missing_photo_is_detected_before_dispatch(self):
        (self.store.root/self.work["id"]/"solution_1.png").unlink()
        with self.assertRaises((FileNotFoundError,ValueError)):self.service.create(self.form)
        self.assertFalse(self.fixture.requests)

    def test_policy_drift_returns_clear_status_without_modifying_ledger(self):
        self.seed(1)
        before=(self.root/"ledger/spending.sqlite3").read_bytes()
        self.service.policy=replace(self.service.policy,timeout_seconds=4)
        status=self.service.availability()
        self.assertEqual(status["reason"],"ledger_policy_mismatch")
        self.assertEqual((self.root/"ledger/spending.sqlite3").read_bytes(),before)
        self.assertEqual(status["spending"]["calls"],1)

    def test_expiry_has_specific_explanation(self):
        self.service.policy=replace(self.service.policy,expires_at=1)
        self.assertEqual(self.service.availability()["reason"],"approval_expired_or_invalid")
        self.assertFalse(self.fixture.requests)

    def test_completed_authorized_scope_is_not_reopened_by_code_change(self):
        self.service.work_profiles={self.work["id"]:"G"}
        with patch.object(self.service,"_completed_scope",return_value={self.work["id"]}):
            out=self.service.create(self.form)
        self.assertEqual(out["reason"],"authorized_checks_completed");self.assertFalse(self.fixture.requests)

    def test_known_broken_deepseek_profile_is_not_blindly_retried(self):
        self.service.work_profiles={self.work["id"]:"D"}
        with patch.object(self.service,"_completed_scope",return_value=set()):
            out=self.service.create({**self.form,"model":"D"})
        self.assertEqual(out["reason"],"model_profile_unreliable");self.assertFalse(self.fixture.requests)

    def transform_reading(self, transform):
        original=self.fixture.factory
        def factory(request):
            delegate=original(request)
            class Transport:
                kind="fixture";single_attempt=True
                def __call__(inner,payload,*,timeout_seconds):
                    response=delegate(payload,timeout_seconds=timeout_seconds)
                    if request.stage=="solution_ocr":
                        value=json.loads(response.content);transform(value)
                        response=replace(response,content=canonical(value))
                    return response
            return Transport()
        self.service.transport_factory=factory

    def test_unresolved_page_saves_reading_and_stops_before_grading(self):
        self.transform_reading(lambda v:v["pages"][0]["coverage"].update(status="partial",score_scope="unresolved",reason="Нечитаем нижний край.",score_scope_reason="Там может быть итоговый ответ."))
        job=self.run_work()
        self.assertEqual(job["error_code"],"reading_needs_clarification")
        self.assertEqual(job["state"],"needs_clarification")
        self.assertEqual(len(self.fixture.requests),2)
        self.assertTrue(job["result"]["units"])
        self.assertIsNone(job["result"]["score"]["value"])
        self.assertIn("нижний край",job["result"]["clarifications"][0]["question"])

    def test_absent_coverage_is_not_inferred_and_reason_is_visible(self):
        self.transform_reading(lambda v:v["pages"][0]["coverage"].pop("status"))
        job=self.run_work()
        self.assertEqual(job["error_code"],"invalid_response")
        self.assertEqual(job["response_validation"]["code"],"strict_reading_response_invalid")
        self.assertIn("обязательную схему",job["message"])
        self.assertEqual(len(self.fixture.requests),2)

    def test_optional_interpretation_fields_do_not_trigger_paid_retry_or_hide_wire_violation(self):
        self.transform_reading(lambda v:v.update(interpretations=[dict(entries=['1:1'],meaning='Meaning already supplied by the model.')]))
        job=self.run_work()
        self.assertEqual(job['state'],'completed')
        self.assertEqual(job['accounting']['calls'],3)
        self.assertEqual(len(self.fixture.requests),3)
        self.assertFalse(job['reading_format_diagnostics']['strict_wire_compliant'])
        self.assertIn(['interpretations',0,'branch'],job['reading_format_diagnostics']['omitted_optional_fields'])
        saved=self.service.get(job['id'])
        self.assertEqual(saved['reading_format_diagnostics'],job['reading_format_diagnostics'])

    def test_local_assessment_recovery_creates_new_version_and_preserves_calls_and_original_job(self):
        with patch('app.live_checking.materialize_compact_assessment',side_effect=ValueError('assessment_required_element_skipped')):
            original=self.run_work()
        old_manifest=(self.service.directory/original['id']/'job.json').read_bytes()
        old_result=(self.service.directory/original['id']/original['result_ref']).read_bytes()
        import sqlite3
        with closing(sqlite3.connect(self.service.ledger/'spending.sqlite3')) as con:before=con.execute('SELECT * FROM calls ORDER BY id').fetchall()
        # Export full call provenance as for a live result, using only the
        # already-recorded synthetic invoices. Disabled policy cannot send.
        self.service.policy=replace(self.service.policy,enabled=False,mode='live')
        requests=len(self.fixture.requests)
        recovered=self.service.recover_saved_compact_response(original['id'])
        self.assertNotEqual(recovered['id'],original['id'])
        self.assertEqual(recovered['state'],'completed')
        self.assertEqual(recovered['result']['score']['value'],2)
        self.assertEqual(recovered['recovery']['model_calls'],0)
        grading=next(c for c in recovered['result']['run']['calls'] if c['stage']=='grading')
        self.assertEqual(grading['status'],'succeeded')
        self.assertEqual(grading['settings']['local_revalidation']['previous_status'],'failed')
        accepted=json.loads((self.service.ledger/grading['response_ref']).read_text())
        old_response=json.loads((self.service.ledger/accepted['local_revalidation']['original_response_ref']).read_text())
        self.assertEqual(accepted['envelope'],old_response['envelope'])
        self.assertEqual(len(self.fixture.requests),requests)
        self.assertEqual((self.service.directory/original['id']/'job.json').read_bytes(),old_manifest)
        self.assertEqual((self.service.directory/original['id']/original['result_ref']).read_bytes(),old_result)
        with closing(sqlite3.connect(self.service.ledger/'spending.sqlite3')) as con:self.assertEqual(con.execute('SELECT * FROM calls ORDER BY id').fetchall(),before)
        self.assertEqual(self.service.recover_saved_compact_response(original['id'])['id'],recovered['id'])
        self.assertEqual(self.service.latest(original['work_id'])['id'],recovered['id'])

    def test_local_reading_recovery_without_assessment_stays_unscored_and_never_continues(self):
        with patch('app.live_checking.decode_reading',side_effect=ValueError('strict_reading_response_invalid')):
            original=self.run_work()
        requests=len(self.fixture.requests)
        recovered=self.service.recover_saved_compact_response(original['id'])
        self.assertEqual(recovered['state'],'awaiting_assessment')
        self.assertIsNone(recovered['result']['score']['value'])
        self.assertTrue(recovered['result']['units'])
        self.assertFalse(recovered['recovery']['assessment_replayed'])
        self.assertEqual(len(self.fixture.requests),requests)
        restarted=FreshChecking(self.store,policy=self.service.policy,ledger=self.service.ledger,transport_factory=self.fixture.factory)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.latest(original['work_id'])['state'],'awaiting_assessment')
        self.assertEqual(len(self.fixture.requests),requests)

    def test_local_recovery_rejects_changed_input_or_response_artifact(self):
        with patch('app.live_checking.materialize_compact_assessment',side_effect=ValueError('assessment_required_element_skipped')):
            original=self.run_work()
        import sqlite3
        with closing(sqlite3.connect(self.service.ledger/'spending.sqlite3')) as con:
            ref=con.execute("SELECT response_ref FROM calls WHERE status='invalid_response'").fetchone()[0]
        path=self.service.ledger/ref;document=json.loads(path.read_text());document['envelope']['content']='{}';path.write_text(json.dumps(document))
        requests=len(self.fixture.requests)
        with self.assertRaisesRegex(ValueError,'response_artifact_changed'):
            self.service.recover_saved_compact_response(original['id'])
        self.assertEqual(len(self.fixture.requests),requests)
        with patch('app.live_checking.input_snapshot',return_value=(None,'changed-guard',None)):
            with self.assertRaisesRegex(ValueError,'input_changed'):
                self.service.recover_saved_compact_response(original['id'])

    def test_reading_checkpoint_survives_bad_assessment(self):
        original=self.fixture.factory
        def factory(request):
            self.fixture.malformed=request.stage=="grading"
            return original(request)
        self.service.transport_factory=factory
        job=self.run_work()
        self.assertTrue((self.service.directory/job["id"]/"reading.json").exists())
        self.assertTrue(job["result"]["units"])
        self.assertEqual(job["accounting"]["calls"],3)

    def test_other_running_process_is_not_marked_interrupted_on_startup(self):
        with patch.object(self.service.pool,"submit"):
            job=self.service.create(self.form)["job"]
        job["state"]="running";self.service._save(job)
        with (self.service.directory/job["id"]/"execution.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            another=FreshChecking(self.store,policy=self.service.policy,ledger=self.service.ledger,transport_factory=self.fixture.factory)
            self.addCleanup(another.close)
            self.assertEqual(another.get(job["id"])["state"],"running")
        self.assertFalse(self.fixture.requests)

    def test_restart_marks_unowned_run_interrupted_without_sending(self):
        with patch.object(self.service.pool,"submit"):
            job=self.service.create(self.form)["job"]
        self.service.close()  # The previous queue owner has actually exited.
        another=FreshChecking(self.store,policy=self.service.policy,ledger=self.service.ledger,transport_factory=self.fixture.factory)
        self.addCleanup(another.close)
        self.assertEqual(another.get(job["id"])["state"],"interrupted")
        self.service._run(job["id"])
        self.assertFalse(self.fixture.requests)

    def test_corrupt_unrelated_job_does_not_hide_valid_history(self):
        job=self.run_work()
        bad=self.service.directory/("C"+"0"*32)/"job.json";bad.parent.mkdir();bad.write_text("{")
        another=FreshChecking(self.store,policy=self.service.policy,ledger=self.service.ledger,transport_factory=self.fixture.factory)
        self.addCleanup(another.close)
        self.assertEqual(another.latest(self.work["id"])["id"],job["id"])
        self.assertEqual(another.history_errors,[bad.parent.name])
        self.assertEqual(bad.read_text(),"{")

    def test_atomic_writes_leave_complete_document_under_concurrency(self):
        target=self.root/"atomic.json"
        threads=[threading.Thread(target=atomic_json,args=(target,dict(n=n,text="x"*1000))) for n in range(10)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertIn(json.loads(target.read_text())["n"],range(10))
        self.assertFalse(list(self.root.glob(".write-*")))

    def test_invalid_usage_retains_original_provider_finish_reason(self):
        envelope=dict(model="deepseek/deepseek-v4.1-flash",usage=dict(prompt_tokens=10,completion_tokens=6000,cost=93,
            completion_tokens_details=dict(reasoning_tokens=6002)),choices=[dict(finish_reason="length",message=dict(content=None))])
        result=parse_response(json.dumps(envelope).encode())
        self.assertEqual(result.finish_reason,"invalid_usage")
        self.assertEqual(result.provider_finish_reason,"length")
        self.assertEqual(result.cost_kopecks,93)
