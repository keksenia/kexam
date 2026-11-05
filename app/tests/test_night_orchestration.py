"""Shutdown, real process queue ownership and immutable provenance regressions.

All inputs/ledgers are temporary, all provider responses synthetic, no network.
"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.call_guard import CallGuard, canonical, digest
from app.live_checking import FreshChecking
from app.live_condition import solve_condition
from app.server import WorkStore
from app.tests.test_foundation import valid_form
from app.tests.test_fresh_checking import Fixture, fixture_policy, condition

ROOT=Path(__file__).resolve().parents[2]


class NightOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=WorkStore(self.root/"works")
        self.work=self.store.save(valid_form())
        self.form=dict(work_id=self.work["id"],model="G",complete=True,single_task=True)
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect",side_effect=AssertionError("No network")).start()
        patch("app.live_checking.read_credential",side_effect=AssertionError("No credentials")).start()

    def service(self, *, solver=solve_condition):
        fixture=Fixture(condition())
        service=FreshChecking(self.store,policy=fixture_policy(),ledger=self.root/"ledger",
                              transport_factory=fixture.factory,solver=solver)
        self.addCleanup(service.close)
        return service,fixture

    def test_close_after_condition_stops_following_stages_and_preserves_invoice(self):
        entered,resume=threading.Event(),threading.Event()
        def solver(value):
            entered.set();self.assertTrue(resume.wait(5))
            return solve_condition(value)
        service,fixture=self.service(solver=solver)
        submitted=[];submit=service.pool.submit
        def capture(*args,**kwargs):
            future=submit(*args,**kwargs);submitted.append(future);return future
        try:
            with patch.object(service.pool,"submit",side_effect=capture):job=service.create(self.form)["job"]
            self.assertTrue(entered.wait(5))
            service.close();resume.set();submitted[0].result(timeout=10)
        finally:resume.set()
        saved=service.get(job["id"])
        self.assertEqual([r.stage for r in fixture.requests],["statement_ocr"])
        self.assertEqual(saved["state"],"cancelled")
        self.assertEqual(saved["error_code"],"operation_cancelled")
        self.assertEqual(saved["accounting"]["calls"],1)
        self.assertEqual(saved["accounting"]["cost"],dict(status="known",rub="0.1"))
        self.assertTrue(saved["condition_versions"])
        with self.assertRaisesRegex(ValueError,"operation_cancelled"):service.create(self.form)

    def test_close_during_assessment_preserves_real_response_cost_and_reading_without_score(self):
        service,fixture=self.service()
        entered,resume=threading.Event(),threading.Event()
        original=fixture.factory
        def factory(request):
            delegate=original(request)
            class Transport:
                kind="fixture";single_attempt=True
                def __call__(inner,payload,*,timeout_seconds):
                    if request.stage=="grading":
                        entered.set();self.assertTrue(resume.wait(5))
                    return delegate(payload,timeout_seconds=timeout_seconds)
            return Transport()
        service.transport_factory=factory
        submitted=[];submit=service.pool.submit
        def capture(*args,**kwargs):
            future=submit(*args,**kwargs);submitted.append(future);return future
        try:
            with patch.object(service.pool,"submit",side_effect=capture):job=service.create(self.form)["job"]
            self.assertTrue(entered.wait(5))
            service.close();resume.set();submitted[0].result(timeout=10)
        finally:resume.set()
        saved=service.get(job["id"])
        self.assertEqual(saved["state"],"cancelled")
        self.assertEqual(saved["accounting"]["cost"],dict(status="known",rub="0.3"))
        self.assertEqual(saved["accounting"]["statuses"],3*["succeeded"])
        self.assertTrue(saved["result"]["units"])
        self.assertIsNone(saved["result"]["score"]["value"])
        self.assertTrue((service.directory/job["id"]/"reading.json").exists())

    def test_close_is_idempotent_and_cannot_continue_confirmed_empty_condition(self):
        service,fixture=self.service();fixture.condition=condition(rhs="2")
        job=service.create(self.form,background=False)["job"]
        service.close();service.close()
        self.assertEqual(service.availability()["reason"],"operation_cancelled")
        with self.assertRaisesRegex(ValueError,"operation_cancelled"):
            service.confirm_empty_condition(job["id"],dict(literal=job["condition_versions"][-1]["solver_input"]["literal"],confirm_empty=True))
        self.assertEqual(len(fixture.requests),2)
        self.assertEqual(service.get(job["id"])["state"],"needs_clarification")

    def test_closed_executor_cannot_start_a_queued_run_or_create_a_ledger(self):
        service,fixture=self.service()
        with patch.object(service.pool,"submit"):job=service.create(self.form)["job"]
        service.close();service._run(job["id"])
        self.assertEqual(service.get(job["id"])["state"],"cancelled")
        self.assertFalse(fixture.requests)
        self.assertFalse((self.root/"ledger").exists())

    def test_close_between_reserve_and_transport_cancels_only_guaranteed_untransmitted_attempt(self):
        service,fixture=self.service();entered,resume=threading.Event(),threading.Event()
        invoked=[];reserve=CallGuard.reserve;original=fixture.factory
        def paused_reserve(guard,*args,**kwargs):
            outcome=reserve(guard,*args,**kwargs)
            entered.set();self.assertTrue(resume.wait(5));return outcome
        def factory(request):
            delegate=original(request)
            class Transport:
                kind="fixture";single_attempt=True
                def __call__(inner,payload,*,timeout_seconds):
                    invoked.append(request.stage);return delegate(payload,timeout_seconds=timeout_seconds)
            return Transport()
        service.transport_factory=factory
        submitted=[];submit=service.pool.submit
        def capture(*args,**kwargs):
            future=submit(*args,**kwargs);submitted.append(future);return future
        try:
            with patch.object(CallGuard,"reserve",paused_reserve),patch.object(service.pool,"submit",side_effect=capture):
                job=service.create(self.form)["job"]
                self.assertTrue(entered.wait(5))
                service.close();resume.set();submitted[0].result(timeout=10)
        finally:resume.set()
        self.assertFalse(invoked)
        saved=service.get(job["id"])
        self.assertEqual(saved["state"],"cancelled")
        self.assertEqual(saved["accounting"]["known_micro_rub"],0)
        self.assertEqual(saved["accounting"]["statuses"],["cancelled_before_dispatch"])
        guard=CallGuard(service.ledger,replace(service.policy,enabled=False,mode="live"))
        self.assertEqual(guard.contract_calls(),[])
        with closing(guard._connect()) as con:
            row=con.execute("SELECT response_ref,state FROM calls").fetchone()
        self.assertIsNone(row["response_ref"])
        self.assertEqual(row["state"],"settled")

    def test_other_instance_does_not_interrupt_a_live_queued_work(self):
        service,fixture=self.service()
        occupied,resume=threading.Event(),threading.Event()
        def occupy():occupied.set();self.assertTrue(resume.wait(5))
        service.pool.submit(occupy)
        try:
            self.assertTrue(occupied.wait(5))
            job=service.create(self.form)["job"]
            other=FreshChecking(self.store,policy=service.policy,ledger=service.ledger,transport_factory=fixture.factory)
            self.addCleanup(other.close)
            self.assertEqual(other.get(job["id"])["state"],"queued")
            resume.set();service.pool.submit(lambda:None).result(timeout=10)
            self.assertEqual(service.get(job["id"])["state"],"completed")
            self.assertEqual(len(fixture.requests),3)
        finally:resume.set()

    def test_queue_owner_is_live_across_processes_and_abandoned_after_exit(self):
        # The owning queue runs in a different process; PID inference/in-process
        # registries cannot satisfy this test. Reopening after exit must recover.
        script='''
import sys
from pathlib import Path
from unittest.mock import patch
from app.live_checking import FreshChecking
from app.server import WorkStore
from app.tests.test_fresh_checking import Fixture, fixture_policy, condition
fixture=Fixture(condition())
service=FreshChecking(WorkStore(Path(sys.argv[1])/"works"),policy=fixture_policy(),ledger=Path(sys.argv[1])/"ledger",transport_factory=fixture.factory)
with patch.object(service.pool,"submit"):
    job=service.create(dict(work_id=sys.argv[2],model="G",complete=True,single_task=True))["job"]
print(job["id"],flush=True)
sys.stdin.readline()
service.close()
'''
        child=subprocess.Popen([sys.executable,"-c",script,str(self.root),self.work["id"]],cwd=ROOT,
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            job_id=child.stdout.readline().strip()
            self.assertRegex(job_id,r"^C[0-9a-f]{32}$")
            service,_=self.service()
            self.assertEqual(service.get(job_id)["state"],"queued")
            child.communicate("\n",timeout=5)
            self.assertEqual(child.returncode,0)
            restarted,_=self.service()
            self.assertEqual(restarted.get(job_id)["state"],"interrupted")
            self.assertFalse((self.root/"ledger/spending.sqlite3").exists())
        finally:
            if child.poll() is None:child.kill();child.communicate()

    def test_request_artifact_drift_cannot_be_exported_as_paid_provenance(self):
        service,fixture=self.service();job=service.create(self.form,background=False)["job"]
        guard=CallGuard(service.ledger,service.policy)
        with closing(guard._connect()) as con:
            row=con.execute("SELECT request_ref FROM calls LIMIT 1").fetchone()
        request=service.ledger/row["request_ref"]
        value=json.loads(request.read_text());value["payload"]["model"]="altered/never-dispatched"
        request.write_text(canonical(value))
        # Export the recorded fixture invoices via the live interface, with
        # paid dispatch disabled. This creates no real model requests.
        guard=CallGuard(service.ledger,replace(service.policy,enabled=False,mode="live"))
        with self.assertRaisesRegex(ValueError,"request_artifact_changed"):
            guard.contract_calls(fingerprints=job["call_fingerprints"])
        self.assertEqual(len(fixture.requests),3)

    def test_known_cost_model_failure_blocks_availability_and_new_job_before_dispatch(self):
        service,fixture=self.service();original=fixture.factory
        def factory(request):
            delegate=original(request)
            class WrongModel:
                kind="fixture";single_attempt=True
                def __call__(inner,payload,*,timeout_seconds):
                    return replace(delegate(payload,timeout_seconds=timeout_seconds),model="wrong-model")
            return WrongModel()
        service.transport_factory=factory
        job=service.create(self.form,background=False)["job"]
        self.assertEqual(job["error_code"],"model_mismatch")
        self.assertEqual(job["accounting"]["cost"]["status"],"known")
        status=service.availability()
        self.assertFalse(status["paid_api_enabled"]);self.assertEqual(status["reason"],"unresolved_spending")
        work=self.store.save(valid_form())
        response=service.create({**self.form,"work_id":work["id"]},background=False)
        self.assertFalse(response["accepted"]);self.assertEqual(response["reason"],"unresolved_spending")
        self.assertEqual(len(fixture.requests),1)

    def run_work(self):
        service,fixture=self.service()
        return service,fixture,service.create(self.form,background=False)["job"]

    def remove_result_pins(self,service,job):
        path=service.directory/job["id"]/"job.json"
        manifest=json.loads(path.read_text());manifest.pop("result_sha256",None)
        for key in ("accepted_result_sha256","result_sha256"):manifest.get("report",{}).pop(key,None)
        path.write_text(canonical(manifest))
        return path

    def test_corrupt_accepted_grade_is_never_returned_to_ui(self):
        service,fixture,job=self.run_work()
        path=service.directory/job["id"]/job["result_ref"]
        document=json.loads(path.read_text());document["score"]["value"]=0
        path.write_text(canonical(document))
        with self.assertRaisesRegex(ValueError,"result_artifact_changed"):service.get(job["id"])
        with self.assertRaisesRegex(ValueError,"result_artifact_changed"):service.latest(job["work_id"])
        self.assertEqual(len(fixture.requests),3)

    def test_ready_report_updates_both_pins_and_corruption_is_rejected(self):
        service,fixture,job=self.run_work()
        prepared=service.prepare_saved_report(job["id"])["job"]
        self.assertEqual(prepared["result_sha256"],digest(prepared["result"]))
        self.assertEqual(prepared["result_sha256"],prepared["report"]["accepted_result_sha256"])
        self.assertEqual(service.get(job["id"])["result"]["score"]["value"],2)
        path=service.directory/job["id"]/prepared["result_ref"]
        document=json.loads(path.read_text());document["score"]["rationale"]="Corrupted explanation"
        path.write_text(canonical(document))
        with self.assertRaisesRegex(ValueError,"result_artifact_changed"):service.get(job["id"])
        self.assertEqual(len(fixture.requests),3)

    def test_valid_historical_unpinned_result_remains_readable_without_manifest_writes(self):
        service,fixture,job=self.run_work()
        manifest=self.remove_result_pins(service,job)
        before=manifest.read_bytes()
        self.assertEqual(service.get(job["id"])["result"],job["result"])
        self.assertEqual(manifest.read_bytes(),before)
        self.assertNotIn("result_sha256",json.loads(manifest.read_text()))
        source=service.directory/job["id"]/"verified-input.json"
        original=json.loads(source.read_text());original["task"]["statement"]["text"]="Changed condition"
        source.write_text(canonical(original))
        with self.assertRaisesRegex(ValueError,"result_contract"):service.get(job["id"])
        self.assertEqual(manifest.read_bytes(),before)
        self.assertEqual(len(fixture.requests),3)

    def test_conflicting_pins_and_result_symlink_cannot_hide_corruption(self):
        service,fixture,job=self.run_work()
        path=service.directory/job["id"]/"job.json"
        original=path.read_text();manifest=json.loads(original)
        manifest["report"]["accepted_result_sha256"]="0"*64;path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError,"result_artifact_changed"):service.get(job["id"])
        path.write_text(original)
        result=service.directory/job["id"]/job["result_ref"]
        outside=self.root/"outside-result.json";outside.write_bytes(result.read_bytes())
        result.unlink();result.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"result_path"):service.get(job["id"])
        self.assertEqual(len(fixture.requests),3)

    def test_withheld_result_is_pinned_and_historical_corrupt_basis_fails_m01(self):
        service,fixture=self.service();original=fixture.factory
        def factory(request):
            fixture.malformed=request.stage=="grading"
            return original(request)
        service.transport_factory=factory
        job=service.create(self.form,background=False)["job"]
        self.assertIsNone(job["result"]["score"]["value"])
        self.assertEqual(job["result_sha256"],digest(job["result"]))
        path=service.directory/job["id"]/job["result_ref"]
        value=json.loads(path.read_text());value["score"]["rationale"]="Changed"
        path.write_text(canonical(value))
        with self.assertRaisesRegex(ValueError,"result_artifact_changed"):service.get(job["id"])
        self.remove_result_pins(service,job)
        value["units"][0]["literal"]="Changed after assessment";path.write_text(canonical(value))
        with self.assertRaisesRegex(ValueError,"result_contract"):service.get(job["id"])


if __name__=="__main__":unittest.main()
