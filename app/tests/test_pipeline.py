"""M04 risk tests: synthetic bytes only, no credentials or external services."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import pipeline as p
from app.server import WorkStore
from app.diagnostic import SyntheticFixtureAdapter, fixture_criterion, fixture_form
from contracts.ege_v1 import validate as contract


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.works = WorkStore(Path(self.tmp.name) / "works")
        self.work = self.works.save(fixture_form("trigonometric_equation"))
        self.engines = []

    def tearDown(self):
        for engine in reversed(self.engines):
            engine.close()

    def engine(self, adapter=None, **kwargs):
        engine = p.Pipeline(self.works, adapter, **kwargs)
        self.engines.append(engine)
        return engine

    def synthetic_engine(self, adapter=None, **kwargs):
        return self.engine(adapter or SyntheticFixtureAdapter(), criteria=[fixture_criterion("trigonometric_equation")], allow_synthetic=True, **kwargs)

    def run_sync(self, engine, **kwargs):
        run = engine.create(self.work["id"], background=False, **kwargs)
        return engine.execute(run["id"])

    def assert_versions_valid(self, run):
        previous = None
        for result in run["result_versions"]:
            self.assertEqual(contract.validate(result, run["input"]), [])
            self.assertEqual(result["supersedes"], previous)
            previous = result["id"]
        self.assertEqual(len({v["id"] for v in run["result_versions"]}), len(run["result_versions"]))

    def test_real_upload_is_unchecked_and_diagnostic_cannot_assign_grade(self):
        engine = self.engine()
        before = self.works.get(self.work["id"])
        run = self.run_sync(engine)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["input"]["task"]["statement"]["completeness"], "unchecked")
        self.assertEqual(run["input"]["capture"]["pages_complete"], "unknown")
        self.assertIsNone(run["result"]["score"]["value"])
        self.assertEqual(run["result"]["run"]["calls"], [])
        self.assertEqual(run["result"]["run"]["cost"], {"status": "not_incurred", "rub": "0"})
        self.assertTrue(run["diagnostic_only"])
        self.assertEqual(self.works.get(self.work["id"]), before)
        self.assert_versions_valid(run)

    def test_both_types_have_separate_complete_synthetic_scenarios(self):
        for task_type in ("trigonometric_equation", "inequality"):
            with self.subTest(task_type=task_type):
                self.work = self.works.save(fixture_form(task_type))
                engine = self.engine(SyntheticFixtureAdapter(), criteria=[fixture_criterion(task_type)], allow_synthetic=True)
                run = self.run_sync(engine, synthetic=True)
                self.assertEqual(run["state"], "completed")
                self.assertEqual(run["result"]["score"]["value"], 2)
                self.assertEqual(run["result"]["status"], "ready")
                self.assertEqual(run["input"]["provenance"]["dataset_role"], "synthetic")
                self.assertEqual(run["result"]["run"]["mode"], "demo")
                self.assert_versions_valid(run)
                engine.close()

    def test_idempotency_binds_exact_input_config_and_run_issued_once(self):
        engine = self.synthetic_engine()
        first = engine.create(self.work["id"], request_key="same", background=False)
        again = engine.create(self.work["id"], request_key="same", background=False)
        self.assertEqual(first["id"], again["id"])
        with self.assertRaisesRegex(p.PipelineError, "idempotency_conflict"):
            engine.create(self.work["id"], request_key="same", synthetic=True, background=False)
        final = engine.execute(first["id"])
        repeated = engine.execute(first["id"])
        self.assertEqual(final["result_versions"], repeated["result_versions"])
        self.assertEqual(len(engine.store.list()), 1)

    def test_concurrent_duplicate_requests_share_one_durable_run(self):
        engine = self.engine()
        results = []
        errors = []
        def create():
            try:
                results.append(engine.create(self.work["id"], request_key="concurrent", background=False))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({run["id"] for run in results}), 1)

    def test_unknown_method_and_unknown_operation_keep_general_path(self):
        class Unknown(SyntheticFixtureAdapter):
            def read(self, request, image):
                result = super().read(request, image)
                result["features"] = ["mixed", "unknown_method"]
                result["operation_requests"] = [dict(operation="not.installed", version="9", arguments={}, context={})]
                return result
            def assess(self, request):
                self.saw = deepcopy(request)
                return super().assess(request)
        adapter = Unknown()
        engine = self.synthetic_engine(adapter)
        run = self.run_sync(engine, synthetic=True)
        self.assertEqual(run["state"], "completed")
        self.assertTrue(adapter.saw["plan"]["general_assessment"])
        self.assertEqual(adapter.saw["plan"]["selected"], [])
        self.assertEqual(adapter.saw["plan"]["check_outcomes"][0]["status"], "unknown")
        self.assertEqual(adapter.saw["rules"], p.COMMON_RULES)
        self.assertFalse(any(f["status"] == "contradicted" for f in run["result"]["facts"]))
        self.assertNotIn("configuration_sha256", json.dumps(adapter.saw))
        self.assertNotIn("cost_rub", json.dumps(adapter.saw))
        self.assertNotIn("basis_sha256", json.dumps(adapter.saw))

    def test_interpretation_stays_separate_from_literal_and_provenance_app_owned(self):
        class Reading(SyntheticFixtureAdapter):
            def read(self, request, image):
                response = super().read(request, image)
                response["interpretations"] = [dict(id="i1", unit_ids=[response["units"][0]["id"]], meaning="Предлагаемая трактовка записи",
                                                      status="proposed", branch_id="main", assumption_ids=[], context_unit_ids=[])]
                return response
        run = self.run_sync(self.synthetic_engine(Reading()), synthetic=True)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["result"]["units"][0]["literal"], "sin x = 0")
        self.assertEqual(run["result"]["interpretations"][0]["status"], "proposed")
        self.assertEqual(run["result"]["interpretations"][0]["source"]["kind"], "synthetic")
        self.assert_versions_valid(run)

    def test_malformed_or_forged_assessment_does_not_replace_valid_reading(self):
        bad_values = ['{"facts":[],"facts":[]}', '{"facts":NaN}', "null", {"run": {"cost": 0}},
                      {"score": {"status": "human_confirmed", "value": 2}}, {"score": {"status": "proposed", "value": 2, "authority": "exam_expert"}}]
        for bad in bad_values:
            with self.subTest(bad=bad):
                class Bad(SyntheticFixtureAdapter):
                    def assess(self, request):
                        return bad
                engine = self.synthetic_engine(Bad())
                run = self.run_sync(engine, synthetic=True)
                self.assertEqual(run["state"], "failed")
                self.assertTrue(run["result"]["units"])
                self.assertIsNone(run["result"]["score"]["value"])
                self.assert_versions_valid(run)
                engine.close()

    def test_second_page_failure_preserves_first_page_and_unprocessed_coverage(self):
        form = fixture_form("trigonometric_equation")
        form["solution"] *= 2
        self.work = self.works.save(form)
        class Partial(SyntheticFixtureAdapter):
            def read(self, request, image):
                if request["page"]["order"] == 2:
                    raise TimeoutError("PRIVATE SOURCE MUST NOT APPEAR IN JOURNAL")
                return super().read(request, image)
        run = self.run_sync(self.synthetic_engine(Partial()), synthetic=True)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["error_code"], "timeout")
        self.assertEqual([c["status"] for c in run["result"]["coverage"]], ["complete", "not_processed"])
        self.assertNotIn("PRIVATE SOURCE", json.dumps(run))
        self.assert_versions_valid(run)

    def test_score_ready_is_independent_of_open_optional_feedback(self):
        class OptionalFeedback(SyntheticFixtureAdapter):
            def assess(self, request):
                response = super().assess(request)
                uid = request["context"]["units"][0]["id"]
                response["clarifications"] = [dict(id="q1", unit_ids=[uid], anchors=request["context"]["units"][0]["anchors"],
                                                  question="Нужно ли пояснение к дополнительной записи?", options=[],
                                                  impact="substantive_feedback", reason="Баллу это не мешает.", status="open", response=None,
                                                  actor_ref=None, ambiguity_group_id=None)]
                return response
        run = self.run_sync(self.synthetic_engine(OptionalFeedback()), synthetic=True)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["result"]["score"]["value"], 2)
        self.assertEqual(run["result"]["readiness"], {"score": "ready", "feedback": "partial"})
        self.assertEqual(run["result"]["status"], "needs_fragment")
        self.assert_versions_valid(run)

    def test_input_change_during_reading_rejects_response_and_keeps_snapshot(self):
        class Change(SyntheticFixtureAdapter):
            def read(adapter, request, image):
                response = super(Change, adapter).read(request, image)
                (self.works.root / self.work["id"] / "solution_1.png").write_bytes(b"changed")
                return response
        engine = self.synthetic_engine(Change())
        run = self.run_sync(engine, synthetic=True)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["error_code"], "input_changed")
        self.assertIsNone(run["result"])
        self.assertFalse(run["source_matches"])
        # Original bytes are still stored by content hash, even after on-disk change.
        old = engine.store.assets(run["input"])["solution-1"]
        self.assertTrue(old.startswith(b"\x89PNG"))

    def test_later_change_marks_existing_result_historical_without_rehashing_it(self):
        engine = self.synthetic_engine()
        run = self.run_sync(engine, synthetic=True)
        old_result = deepcopy(run["result"])
        manifest = self.works.root / self.work["id"] / "work.json"
        data = json.loads(manifest.read_text())
        data["task_text"] += " Изменение условия."
        manifest.write_text(json.dumps(data), encoding="utf-8")
        view = engine.view(run["id"])
        self.assertFalse(view["source_matches"])
        self.assertFalse(view["result_current"])
        self.assertEqual(view["result"], old_result)
        self.assert_versions_valid(view)

    def test_queue_and_running_cancellation_do_not_publish_late_response(self):
        engine = self.engine()
        run = engine.create(self.work["id"], background=False)
        engine.store.cancel(run["id"])
        self.assertEqual(engine.execute(run["id"])["state"], "cancelled")
        engine.close()
        entered, release = threading.Event(), threading.Event()
        class Slow(SyntheticFixtureAdapter):
            def assess(self, request):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
                return super().assess(request)
        engine = self.synthetic_engine(Slow())
        run = engine.create(self.work["id"], synthetic=True)
        self.assertTrue(entered.wait(5))
        engine.store.cancel(run["id"])
        release.set()
        engine.executor.shutdown(wait=True)
        result = engine.view(run["id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["result"]["units"])
        self.assertIsNone(result["result"]["score"]["value"])
        self.assert_versions_valid(result)

    def test_restart_recovers_orphans_without_automatic_rerun_and_preserves_history(self):
        engine = self.synthetic_engine()
        completed = self.run_sync(engine, synthetic=True)
        orphan = engine.create(self.work["id"], background=False)
        self.assertTrue(engine.store.start(orphan["id"]))
        # Simulate abrupt process exit: release ownership without orderly cancellation.
        engine.executor.shutdown(wait=True)
        p.fcntl.flock(engine._lock_file.fileno(), p.fcntl.LOCK_UN)
        engine._lock_file.close()
        engine.closed = True
        reopened = self.engine()
        interrupted = reopened.view(orphan["id"])
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertIsNone(interrupted["result"])
        self.assertEqual(reopened.view(completed["id"])["result"], completed["result"])
        retry = reopened.create(self.work["id"], parent_id=orphan["id"], background=False)
        self.assertNotEqual(retry["id"], orphan["id"])
        self.assertEqual(retry["parent_id"], orphan["id"])

    def test_second_worker_cannot_interrupt_active_worker(self):
        engine = self.engine()
        run = engine.create(self.work["id"], background=False)
        with self.assertRaisesRegex(p.PipelineError, "worker_already_running"):
            p.Pipeline(self.works)
        self.assertEqual(engine.store.get(run["id"])["state"], "queued")

    def test_network_adapter_and_synthetic_flag_fail_closed(self):
        class Network(p.OfflineMockAdapter):
            network_enabled = True
        with self.assertRaisesRegex(p.PipelineError, "network_adapter_disabled"):
            self.engine(Network())
        engine = self.engine()
        with self.assertRaisesRegex(p.PipelineError, "synthetic_fixture_disabled"):
            engine.create(self.work["id"], synthetic=True)
        self.assertEqual(engine.store.list(), [])

    def test_ready_score_survives_failure_after_accepted_result_commit(self):
        engine = self.synthetic_engine()
        original = engine._guard
        def fail_after_result(run):
            current = engine.store.get(run["id"])["result"]
            if current and current["score"]["value"] is not None:
                raise p.PipelineError("postprocess_failed")
            original(run)
        with patch.object(engine, "_guard", side_effect=fail_after_result):
            run = self.run_sync(engine, synthetic=True)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["result"]["readiness"]["score"], "ready")
        self.assertEqual(run["result"]["score"]["value"], 2)
        self.assert_versions_valid(run)

    def test_retry_parent_type_and_identity_are_validated_before_sql(self):
        engine = self.engine()
        for value in ([], {}, False, 1, "invalid", "../wrong"):
            with self.subTest(value=value), self.assertRaisesRegex(p.PipelineError, "invalid_retry_parent"):
                engine.create(self.work["id"], parent_id=value, background=False)
        parent = self.run_sync(engine)
        child = engine.create(self.work["id"], parent_id=parent["id"], request_key="retry-key", background=False)
        self.assertEqual(child["parent_id"], parent["id"])
        with self.assertRaisesRegex(p.PipelineError, "idempotency_conflict"):
            engine.create(self.work["id"], parent_id=None, request_key="retry-key", background=False)

    def test_exact_operation_fact_reaches_assessment_without_model_recheck(self):
        class Operation:
            id, version, features = "test.fixture_membership", "1", {"pi_families"}
            calls = 0
            def execute(self, request, context):
                self.calls += 1
                if request["arguments"] != {"value": 0}:
                    raise ValueError("unsupported fixture")
                return dict(id="code-fact", unit_ids=[context["units"][0]["id"]], interpretation_ids=[],
                            claim="0 входит в искусственный набор {0}.", method="exact_code", status="supported",
                            reason="Тестовый точный локальный предикат, не операция M06/M07.", tool_version=self.version,
                            depends_on_fact_ids=[], code_check=dict(operation=self.id, arguments=request["arguments"], evidence="0 == 0"),
                            branch_id="main", assumption_ids=[], context_unit_ids=[], claim_kind="membership", domain="Только {0}")
        operation = Operation()
        class WithCode(SyntheticFixtureAdapter):
            reads = assessments = 0
            def read(self, request, image):
                self.reads += 1
                response = super().read(request, image)
                response["operation_requests"] = [dict(operation=operation.id, version="1", arguments={"value": 0}, context={})]
                return response
            def assess(self, request):
                self.assessments += 1
                self.context = request
                return super().assess(request)
        adapter = WithCode()
        run = self.run_sync(self.synthetic_engine(adapter, operations=[operation]), synthetic=True)
        self.assertEqual(run["state"], "completed")
        self.assertEqual((operation.calls, adapter.reads, adapter.assessments), (1, 1, 1))
        self.assertEqual(adapter.context["context"]["facts"][0]["id"], "code-fact")
        self.assertEqual(adapter.context["plan"]["check_outcomes"][0]["fact_id"], "code-fact")
        self.assert_versions_valid(run)

    def test_cancel_after_ready_snapshot_retains_the_ready_score(self):
        engine = self.synthetic_engine()
        original = engine._publish
        def cancel_after_publish(run, result, stage, started):
            result = original(run, result, stage, started)
            if stage == "assessment":
                engine.store.cancel(run["id"])
            return result
        with patch.object(engine, "_publish", side_effect=cancel_after_publish):
            run = self.run_sync(engine, synthetic=True)
        self.assertEqual(run["state"], "cancelled")
        self.assertEqual(run["result"]["score"]["value"], 2)
        self.assert_versions_valid(run)

    def test_snapshot_write_failure_does_not_publish_invalid_success(self):
        engine = self.synthetic_engine()
        original = engine.store.record
        def fail_record(run_id, kind, stage, payload):
            if kind == "result" and stage == "assessment":
                raise OSError("PRIVATE DISK DETAIL")
            return original(run_id, kind, stage, payload)
        with patch.object(engine.store, "record", side_effect=fail_record):
            run = self.run_sync(engine, synthetic=True)
        self.assertEqual(run["state"], "failed")
        self.assertIsNone(run["result"]["score"]["value"])
        self.assertEqual(len(run["result_versions"]), 1)
        self.assertNotIn("PRIVATE DISK DETAIL", json.dumps(run))
        self.assert_versions_valid(run)


if __name__ == "__main__":
    unittest.main()
