"""Synthetic execution failures: local proof isolation, budgets and evidence reuse."""
from copy import deepcopy
import json
from pathlib import Path
import os
import signal
import tempfile
import unittest
from unittest.mock import patch

from app import pipeline as p
from app.diagnostic import SyntheticFixtureAdapter, fixture_criterion, fixture_form
from app.server import WorkStore
from contracts.ege_v1 import validate as contract


class ProbeOperation:
    id, version, features = "test.probe", "1", {"pi_families"}
    source_sha256 = "0" * 64

    def __init__(self):
        self.calls = []

    def execute(self, request, result):
        self.calls.append(deepcopy(request))
        value = request["arguments"]["value"]
        if value == "fail":
            raise ValueError("PRIVATE HANDWRITING DETAILS")
        status = "unknown" if value == "unknown" else "supported"
        return dict(id=f"probe-{value}", unit_ids=[result["units"][0]["id"]], interpretation_ids=[],
                    claim=f"Synthetic fixture assertion {value}.", method="exact_code", status=status,
                    reason="Synthetic failure-isolation fixture, no math quality claim.", tool_version=self.version,
                    depends_on_fact_ids=request["context"].get("depends_on_fact_ids", []),
                    code_check=dict(operation=self.id, arguments=request["arguments"], evidence="fixture evidence"),
                    branch_id="main", assumption_ids=[], context_unit_ids=[], claim_kind="membership", domain="Fixture only")


def query(value, **context):
    return dict(operation=ProbeOperation.id, version="1", arguments={"value": value}, context=context)


class ProbeAdapter(SyntheticFixtureAdapter):
    def __init__(self, requests):
        self.requests = requests
        self.assessment_request = None

    def read(self, request, images):
        response = super().read(request, images)
        response["operation_requests"] = deepcopy(self.requests)
        return response

    def assess(self, request):
        self.assessment_request = deepcopy(request)
        return super().assess(request)


class OperationIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.works = WorkStore(Path(self.tmp.name) / "works")
        self.work = self.works.save(fixture_form("trigonometric_equation"))

    def run_case(self, requests, operation=None, adapter=None):
        operation = operation or ProbeOperation()
        adapter = adapter or ProbeAdapter(requests)
        engine = p.Pipeline(self.works, adapter, operations=operation if isinstance(operation, list) else [operation],
                            criteria=[fixture_criterion("trigonometric_equation")], allow_synthetic=True)
        self.addCleanup(engine.close)
        run = engine.create(self.work["id"], background=False, synthetic=True)
        run = engine.execute(run["id"])
        for version in run["result_versions"]:
            self.assertEqual(contract.validate(version, run["input"]), [])
        return run, operation, adapter, engine

    def test_local_failure_keeps_prior_and_independent_facts_and_cannot_clear_issue(self):
        run, operation, adapter, _ = self.run_case([query("before"), query("fail"),
                                                    query("dependent", depends_on_fact_ids=["probe-fail"]),
                                                    query("after")])
        self.assertEqual(run["state"], "completed")
        self.assertEqual([q["arguments"]["value"] for q in operation.calls], ["before", "fail", "after"])
        self.assertEqual([f["id"] for f in run["result"]["facts"] if f["method"] == "exact_code"],
                         ["probe-before", "probe-after"])
        outcomes = adapter.assessment_request["plan"]["check_outcomes"]
        self.assertEqual([o["status"] for o in outcomes], ["supported", "unknown", "unknown", "supported"])
        self.assertEqual(outcomes[2]["reason"], "dependency_unavailable")
        self.assertEqual(run["result"]["score"]["value"], 2)  # Independent synthetic evidence still supports its score.
        self.assertTrue(run["result"]["issues"])
        self.assertTrue(all(i["blocks"] == [] for i in run["result"]["issues"]))
        self.assertNotIn("PRIVATE HANDWRITING", json.dumps(run))
        self.assertTrue(adapter.assessment_request["context"]["issues"])

    def test_unknown_parent_cannot_be_consumed_as_supported_by_another_operation(self):
        run, operation, adapter, _ = self.run_case([query("unknown"), query("dependent", depends_on_fact_ids=["probe-unknown"]), query("after")])
        self.assertEqual([q["arguments"]["value"] for q in operation.calls], ["unknown", "after"])
        self.assertEqual(adapter.assessment_request["plan"]["check_outcomes"][1]["reason"], "dependency_unavailable")
        self.assertEqual(run["state"], "completed")

    def test_dropped_dependency_is_local_invalid_response_not_accepted_fact(self):
        class DropDependency(ProbeOperation):
            def execute(self, request, result):
                fact = super().execute(request, result)
                fact["depends_on_fact_ids"] = []
                return fact
        run, _, adapter, _ = self.run_case([query("before"), query("dependent", depends_on_fact_ids=["probe-before"]), query("after")], DropDependency())
        self.assertEqual(run["state"], "completed")
        self.assertNotIn("probe-dependent", [f["id"] for f in run["result"]["facts"]])
        self.assertEqual(adapter.assessment_request["plan"]["check_outcomes"][1]["reason"], "operation_failed")

    def test_malformed_request_and_invalid_fact_do_not_suppress_other_checks(self):
        class BadFact(ProbeOperation):
            def execute(self, request, result):
                fact = super().execute(request, result)
                if request["arguments"]["value"] == "invalid":
                    fact["unit_ids"] = ["missing-source"]
                return fact
        run, _, adapter, _ = self.run_case([None, {"operation": []}, query("invalid"), query("after")], BadFact())
        self.assertEqual(run["state"], "completed")
        self.assertIn("probe-after", [f["id"] for f in run["result"]["facts"]])
        self.assertNotIn("probe-invalid", [f["id"] for f in run["result"]["facts"]])
        self.assertEqual(len(adapter.assessment_request["plan"]["check_outcomes"]), 4)
        self.assertTrue(run["result"]["issues"])

    def test_same_request_reuses_one_fact_even_after_unrelated_facts(self):
        run, operation, adapter, _ = self.run_case([query("one"), query("other"), query("one"), query("one")])
        self.assertEqual(len(operation.calls), 2)
        outcomes = adapter.assessment_request["plan"]["check_outcomes"]
        self.assertEqual(outcomes[0]["fact_id"], outcomes[2]["fact_id"])
        self.assertTrue(outcomes[2]["reused"])
        self.assertEqual(sum(f["id"] == "probe-one" for f in run["result"]["facts"]), 1)

    def test_failed_dependency_is_retried_when_its_fact_becomes_available(self):
        run, operation, adapter, _ = self.run_case([query("child", depends_on_fact_ids=["probe-parent"]), query("parent"),
                                                    query("child", depends_on_fact_ids=["probe-parent"])])
        self.assertEqual(run["state"], "completed")
        self.assertEqual([q["arguments"]["value"] for q in operation.calls], ["parent", "child"])
        self.assertEqual(adapter.assessment_request["plan"]["check_outcomes"][-1]["status"], "supported")

    def test_dependency_digest_follows_human_interpretation_premise_sources(self):
        # This closure test deliberately omits a request branch, as generic
        # injected operations may do. A branch's redundant assumption list must
        # not be required to discover an explicitly named premise's sources.
        request = query("dependent", assumption_ids=["a1"])
        result = dict(input_ref={"id": "synthetic-input"}, units=[], corrections=[], branches=[], relations=[],
                      assumptions=[dict(id="a1", origin="human_interpretation", source_ref="i1"),
                                   dict(id="a2", origin="derived_fact", source_ref="f1")],
                      interpretations=[dict(id="i1", assumption_ids=["a2"])], facts=[])
        before = p._operation_dependencies(request["context"], result)
        self.assertEqual(before, {"f1": None})
        reading_hash = contract.reading_digest(result)
        old_key = contract.digest(dict(request=request, reading_sha256=reading_hash, dependencies=before))
        result["facts"] = [dict(id="f1", status="supported", depends_on_fact_ids=[])]
        after = p._operation_dependencies(request["context"], result)
        self.assertEqual(contract.reading_digest(result), reading_hash)
        self.assertEqual(after["f1"]["status"], "supported")
        new_key = contract.digest(dict(request=request, reading_sha256=reading_hash, dependencies=after))
        self.assertNotEqual(old_key, new_key)
        result["facts"][0]["status"] = "unknown"
        changed = p._operation_dependencies(request["context"], result)
        self.assertNotEqual(new_key, contract.digest(dict(request=request, reading_sha256=reading_hash, dependencies=changed)))

    def test_one_work_budget_is_shared_and_passes_remaining_time_to_last_call(self):
        class Clock:
            value = 1000.0
            def monotonic(self):
                return self.value
        clock = Clock()
        class Budgeted(ProbeOperation):
            resource_limits = dict(wall_ms_per_operation=250, wall_ms_per_work=5000, memory_mib_worker=64)
            timeouts = []
            def execute_bounded(self, request, result, timeout_ms):
                self.timeouts.append(timeout_ms)
                clock.value += min(220, timeout_ms) / 1000
                return super().execute(request, result)
            def execute(self, request, result):
                raise AssertionError("A bounded operation must never use the unbounded path")
        operation = Budgeted()
        with patch.object(p.time, "monotonic", side_effect=clock.monotonic):
            run, _, adapter, _ = self.run_case([query(str(i)) for i in range(28)], operation)
        self.assertEqual(run["state"], "completed")
        self.assertLessEqual(len(operation.calls), 23)
        self.assertTrue(all(t <= 250 for t in operation.timeouts))
        self.assertLess(operation.timeouts[-1], 220)
        self.assertLessEqual(clock.value - 1000.0, 5.000001)
        self.assertEqual(adapter.assessment_request["plan"]["check_outcomes"][-1]["reason"], "work_budget_exhausted")
        self.assertTrue(any(i["code"] == "timeout" for i in run["result"]["issues"]))

    def test_shared_worker_prepares_once_and_startup_uses_work_budget(self):
        class Clock:
            value = 1000.0
            def monotonic(self):
                return self.value
        clock, preparations = Clock(), []
        class Warm(ProbeOperation):
            resource_limits = dict(wall_ms_per_operation=250, wall_ms_per_work=5000, memory_mib_worker=64)
            execution_group = "shared-fixture"
            def prepare_execution(self, timeout_ms):
                if not preparations:
                    preparations.append(timeout_ms)
                    clock.value += 0.8
            def execute_bounded(self, request, result, timeout_ms):
                clock.value += min(220, timeout_ms) / 1000
                return super().execute(request, result)
        operations = [Warm(), Warm()]
        operations[1].id = "test.second"
        requests = [query(str(i)) for i in range(28)]
        for i, request in enumerate(requests):
            request["operation"] = operations[i % 2].id
        with patch.object(p.time, "monotonic", side_effect=clock.monotonic):
            run, _, adapter, _ = self.run_case(requests, operations)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(len(preparations), 1)
        self.assertLessEqual(sum(len(o.calls) for o in operations), 20)
        self.assertLessEqual(clock.value - 1000.0, 5.000001)
        self.assertEqual(adapter.assessment_request["plan"]["check_outcomes"][-1]["reason"], "work_budget_exhausted")

    def test_failed_worker_startup_is_not_repeated_for_identical_request(self):
        class NoWorker(ProbeOperation):
            resource_limits = dict(wall_ms_per_operation=250, wall_ms_per_work=5000, memory_mib_worker=64)
            attempts = 0
            def prepare_execution(self, timeout_ms):
                self.attempts += 1
                raise TimeoutError("PRIVATE WORKER DETAILS")
        operation = NoWorker()
        run, _, adapter, _ = self.run_case([query("first"), query("first"), query("second")], operation)
        self.assertEqual(run["state"], "completed")
        self.assertEqual(operation.attempts, 2)
        self.assertFalse(operation.calls)
        self.assertTrue(all(o["status"] == "unknown" for o in adapter.assessment_request["plan"]["check_outcomes"]))
        self.assertNotIn("PRIVATE WORKER", json.dumps(run))

    def test_cancellation_still_fails_closed_after_local_operation(self):
        class Cancel(ProbeOperation):
            engine = None
            def execute(self, request, result):
                self.engine.store.cancel(result["run"]["id"])
                return super().execute(request, result)
        operation = Cancel()
        adapter = ProbeAdapter([query("cancel")])
        engine = p.Pipeline(self.works, adapter, operations=[operation], allow_synthetic=True)
        self.addCleanup(engine.close)
        operation.engine = engine
        run = engine.create(self.work["id"], synthetic=True, background=False)
        run = engine.execute(run["id"])
        self.assertEqual(run["state"], "cancelled")
        self.assertFalse(run["result"]["facts"])
        self.assertIsNone(adapter.assessment_request)

    def test_source_change_during_operation_is_fatal_and_discards_late_fact(self):
        works, work = self.works, self.work
        class ChangeSource(ProbeOperation):
            def execute(self, request, result):
                (works.root / work["id"] / "solution_1.png").write_bytes(b"changed")
                return super().execute(request, result)
        run, _, adapter, _ = self.run_case([query("changed"), query("after")], ChangeSource())
        self.assertEqual((run["state"], run["error_code"]), ("failed", "input_changed"))
        self.assertFalse(run["result"]["facts"])
        self.assertIsNone(adapter.assessment_request)

    def test_local_issues_survive_assessment_failure(self):
        class BrokenAssessment(ProbeAdapter):
            def assess(self, request):
                raise TimeoutError("PRIVATE")
        run, _, _, _ = self.run_case([query("fail")], adapter=BrokenAssessment([query("fail")]))
        self.assertEqual((run["state"], run["error_code"]), ("failed", "timeout"))
        self.assertEqual(run["result"]["issues"][0]["code"], "unsupported_math")
        self.assertIn("operation_failed", run["result"]["issues"][0]["message"])

    def test_source_fingerprint_change_invalidates_saved_run(self):
        operation = ProbeOperation()
        engine = p.Pipeline(self.works, ProbeAdapter([]), operations=[operation], allow_synthetic=True)
        self.addCleanup(engine.close)
        run = engine.create(self.work["id"], synthetic=True, background=False)
        self.assertEqual(run["configuration"]["operations"][0]["source_sha256"], "0" * 64)
        operation.source_sha256 = "1" * 64
        run = engine.execute(run["id"])
        self.assertEqual((run["state"], run["error_code"]), ("failed", "configuration_changed"))

    def test_exact_evidence_projection_keeps_math_and_recoverable_full_record(self):
        class Evidence(ProbeOperation):
            def execute(self, request, result):
                fact = super().execute(request, result)
                fact["code_check"]["evidence"] = json.dumps({"k01_request": {"arguments": request["arguments"],
                    "context": {"bindings": [{"source_kind": "unit", "source_ref": "solution-1-u1", "argument_pointer": "/value"}]}},
                    "k01_response": {"computed": {"exact_value": "1/3"}, "reason_code": "exact_true", "evidence": ["Exact arithmetic."]},
                    "execution": {"profile": "fixture-watchdog", "short_overshoot_possible": True}})
                return fact
        run, _, adapter, _ = self.run_case([query("math")], Evidence())
        saved = next(f for f in run["result"]["facts"] if f["method"] == "exact_code")
        compact = next(f for f in adapter.assessment_request["context"]["facts"] if f["method"] == "exact_code")
        self.assertIn("k01_request", json.loads(saved["code_check"]["evidence"]))
        evidence = compact["code_check"]["evidence"]
        self.assertEqual(evidence["computed"], {"exact_value": "1/3"})
        self.assertEqual(evidence["execution"]["profile"], "fixture-watchdog")
        self.assertEqual(evidence["bindings"][0]["source_ref"], "solution-1-u1")
        self.assertIn(run["id"], evidence["journal_ref"])
        self.assertIn(saved["id"], evidence["journal_ref"])
        self.assertNotIn("k01_request", json.dumps(compact))
        self.assertEqual(compact["code_check"]["arguments"], saved["code_check"]["arguments"])

    def exact_query(self, point=0):
        from exact_checks.operations import ExactOperation
        from exact_checks.tests import support as s
        operation = ExactOperation("m06.pi_interval_membership")
        arguments = dict(point=s.pi(point), interval=s.pi_interval(0, 1), expected_member=True)
        context = dict(unit_ids=["solution-1-u1"], interpretation_ids=[], branch_id="main", assumption_ids=[],
                       context_unit_ids=[], depends_on_fact_ids=[],
                       bindings=[dict(argument_pointer="/" + key, source_kind="unit", source_ref="solution-1-u1") for key in arguments])
        return operation, dict(operation=operation.id, version=operation.version, arguments=arguments, context=context)

    def test_real_exact_worker_invalid_context_does_not_block_independent_request(self):
        operation, good = self.exact_query()
        bad = deepcopy(good)
        bad["context"]["unit_ids"] = ["missing-unit"]
        run, _, adapter, _ = self.run_case([bad, good], operation)
        self.assertEqual(run["state"], "completed")
        outcomes = adapter.assessment_request["plan"]["check_outcomes"]
        self.assertEqual(outcomes[0]["reason"], "invalid_operation_context")
        self.assertEqual(outcomes[1]["status"], "supported")
        fact = next(f for f in run["result"]["facts"] if f["method"] == "exact_code")
        execution = json.loads(fact["code_check"]["evidence"])["execution"]
        self.assertEqual(execution["isolation"], "subprocess")
        self.assertEqual(execution["profile"], run["configuration"]["operations"][0]["execution_profile"])

    @unittest.skipUnless(hasattr(signal, "SIGSTOP"), "Requires POSIX process suspension")
    def test_real_exact_worker_timeout_restarts_for_independent_request_without_retry(self):
        operation, first = self.exact_query(0)
        _, second = self.exact_query(1)
        execute_bounded = operation.execute_bounded
        calls, worker_pids = [], []
        def stop_first_worker(request, result, timeout_ms):
            calls.append(deepcopy(request))
            worker_pids.append(operation._executor.process.pid)
            if len(calls) == 1:
                os.kill(operation._executor.process.pid, signal.SIGSTOP)
            return execute_bounded(request, result, timeout_ms)
        with patch.object(operation, "execute_bounded", side_effect=stop_first_worker):
            run, _, adapter, _ = self.run_case([first, first, second], operation)
        self.assertEqual(run["state"], "completed")
        outcomes = adapter.assessment_request["plan"]["check_outcomes"]
        self.assertEqual(outcomes[0]["reason"], "timeout")
        self.assertTrue(outcomes[1]["reused"])
        self.assertEqual(outcomes[2]["status"], "supported")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(worker_pids[0], worker_pids[1])
        self.assertTrue(any(i["code"] == "timeout" for i in run["result"]["issues"]))


if __name__ == "__main__":
    unittest.main()
