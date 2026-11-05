"""M14 safety properties: synthetic requests, temporary ledgers, no HTTP."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.call_guard import CallGuard, Policy, ProviderResponse, StageRequest, kopecks_to_micro
from app.checking_service import CheckingService
from app.guard_demo import FixtureTransport, fixture_policy, fixture_request, fixture_validator


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = fixture_request()
        self.policy = fixture_policy([self.request])
        # All these tests must remain completely offline, including adapters.
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def guard(self, policy=None):
        return CallGuard(self.root, policy or self.policy)

    def run_request(self, transport=None, **kwargs):
        return self.guard().execute(self.request, transport or FixtureTransport(), fixture_validator, **kwargs)

    def test_default_closed_app_service_and_no_ledger(self):
        service = CheckingService(self.root)
        self.assertEqual(service.run_stage(self.request).status, "paid_calls_disabled")
        self.assertFalse(service.availability()["paid_api_enabled"])
        self.assertFalse((self.root / "spending.sqlite3").exists())

    def test_incomplete_condition_stops_before_transport(self):
        transport = FixtureTransport()
        outcome = self.guard().execute(replace(self.request, condition_complete=False), transport, fixture_validator)
        self.assertEqual(outcome.status, "incomplete_condition")
        self.assertIsNone(outcome.assessment)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(self.guard().snapshot()["calls"], 0)

    def test_unproven_exact_request_cost_ceiling_stops(self):
        request = fixture_request("changed input")
        self.assertEqual(self.guard().execute(request, FixtureTransport(), fixture_validator).status, "billing_ceiling_unproven")

    def test_paid_mode_cannot_use_fixture_or_unbounded_adapter(self):
        policy = replace(self.policy, mode="live")
        transport = FixtureTransport()
        self.assertEqual(self.guard(policy).execute(self.request, transport, fixture_validator).status, "transport_not_admitted")
        transport.kind = "live"
        self.assertEqual(self.guard(policy).execute(self.request, transport, fixture_validator).status, "output_bound_unproven")

    def test_duplicate_and_restart_reuse_without_repaying(self):
        transport = FixtureTransport()
        first = self.run_request(transport)
        again = self.run_request(transport)
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(first.value, again.value)
        self.assertEqual(first.attempt_id, again.attempt_id)
        self.assertTrue(again.reused)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(self.guard().snapshot()["cost"], {"rub": "0.1025", "status": "known"})
        self.assertEqual(self.guard().contract_calls(), [])  # Simulation is not API history.

    def test_parallel_duplicate_has_one_dispatch(self):
        transport = FixtureTransport("timeout")
        policy = replace(self.policy, timeout_seconds=0.2)
        guards = [self.guard(policy) for _ in range(10)]
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda g: g.execute(self.request, transport, fixture_validator), guards))
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len({r.attempt_id for r in results}), 1)

    def test_parallel_distinct_requests_share_one_budget(self):
        requests = [fixture_request(str(i)) for i in range(10)]
        policy = fixture_policy(requests, budget_micro_rub=2_000_000, max_total_calls=10)
        guards = [self.guard(policy) for _ in requests]
        barrier = threading.Barrier(10)
        def reserve(pair):
            barrier.wait()
            return pair[0].reserve(pair[1])
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(reserve, zip(guards, requests)))
        self.assertEqual(sum(r.status == "reserved" for r in results), 2)
        self.assertEqual(guards[0].snapshot()["committed_micro_rub"], 2_000_000)

    def test_immutable_budget_on_reopen(self):
        self.run_request()
        with self.assertRaises(ValueError):
            self.guard(replace(self.policy, budget_micro_rub=999_000_000))
        self.assertEqual(self.guard().snapshot()["calls"], 1)

    def test_timeout_holds_reserve_stops_new_calls_and_ignores_late_reply(self):
        other = fixture_request("other")
        policy = fixture_policy([self.request, other])
        guard = self.guard(policy)
        transport = FixtureTransport("timeout")
        outcome = guard.execute(self.request, transport, fixture_validator)
        self.assertEqual(outcome.status, "cost_unknown")
        self.assertIsNone(outcome.value)
        before = guard.snapshot()
        self.assertEqual(before["held_micro_rub"], 1_000_000)
        self.assertEqual(before["cost"], {"rub": None, "status": "unknown"})
        self.assertEqual(guard.execute(other, transport, fixture_validator).status, "unresolved_spending")
        time.sleep(0.12)
        self.assertEqual(guard.snapshot(), before)
        self.assertEqual(guard.execute(self.request, transport, fixture_validator, retry=True).status, "retry_forbidden")

    def test_exception_text_never_leaks_into_ledger(self):
        class Broken(FixtureTransport):
            def __call__(self, payload, **kwargs):
                raise RuntimeError("SECRET EXAMPLE AND PUPIL DATA")
        self.assertEqual(self.run_request(Broken()).status, "cost_unknown")
        self.assertNotIn(b"SECRET EXAMPLE", (self.root / "spending.sqlite3").read_bytes())

    def test_schema_rejection_is_paid_failed_attempt_and_retry_is_explicit(self):
        transport = FixtureTransport("invalid")
        self.assertEqual(self.run_request(transport).status, "invalid_response")
        self.assertIsNone(self.run_request(transport).value)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(self.run_request(transport, retry=True).status, "invalid_response")
        self.assertEqual(self.run_request(transport, retry=True).status, "attempt_limit")
        self.assertEqual(transport.calls, 2)
        self.assertEqual(self.guard().snapshot()["cost"]["rub"], "0.205")

    def test_missing_or_invalid_usage_cost_not_zero(self):
        for index, cost in enumerate((None, -1, True, float("nan"), float("inf"), "nonsense")):
            with self.subTest(cost=cost):
                guard = CallGuard(self.root / str(index), self.policy)
                class BadCost(FixtureTransport):
                    def __call__(self, payload, **kwargs):
                        return ProviderResponse('{"text":"ok"}', cost_kopecks=cost, model=payload["model"])
                outcome = guard.execute(self.request, BadCost(), fixture_validator)
                self.assertEqual(outcome.status, "cost_unknown")
                self.assertEqual(guard.snapshot()["held_micro_rub"], 1_000_000)

    def test_provider_cost_without_token_usage_is_known_not_invented(self):
        class ReportedCost(FixtureTransport):
            def __call__(self, payload, **kwargs):
                return ProviderResponse('{"text":"ok"}', cost_kopecks="0", model=payload["model"])
        self.assertEqual(self.run_request(ReportedCost()).status, "succeeded")
        self.assertEqual(self.guard().snapshot()["cost"], {"rub": "0", "status": "known"})

    def test_kopecks_exact_cost_and_conservative_fractional_reserve(self):
        self.assertEqual(kopecks_to_micro("12.345678"), 123457)
        class Precise(FixtureTransport):
            def __call__(self, payload, **kwargs):
                return ProviderResponse('{"text":"ok"}', cost_kopecks="12.345678", model=payload["model"])
        self.run_request(Precise())
        self.assertEqual(self.guard().snapshot()["cost"]["rub"], "0.12345678")

    def test_overrun_is_recorded_and_halts_not_discarded(self):
        other = fixture_request("other")
        policy = fixture_policy([self.request, other])
        guard = self.guard(policy)
        class Overrun(FixtureTransport):
            def __call__(self, payload, **kwargs):
                return ProviderResponse('{"text":"ok"}', cost_kopecks="150", model=payload["model"])
        self.assertEqual(guard.execute(self.request, Overrun(), fixture_validator).status, "ceiling_breached")
        self.assertEqual(guard.snapshot()["known_micro_rub"], 1_500_000)
        self.assertEqual(guard.execute(other, FixtureTransport(), fixture_validator).status, "unresolved_spending")

    def test_token_and_total_call_limits(self):
        too_large = replace(self.request, payload={**self.request.payload, "max_tokens": 129})
        self.assertEqual(self.guard().execute(too_large, FixtureTransport(), fixture_validator).status, "output_limit_invalid")
        # Separate ledger with a one-call allowance.
        policy = replace(self.policy, max_total_calls=1)
        guard = CallGuard(self.root / "one", policy)
        guard.execute(self.request, FixtureTransport("invalid"), fixture_validator)
        self.assertEqual(guard.execute(self.request, FixtureTransport(), fixture_validator, retry=True).status, "total_call_limit")

    def test_terminal_http_error_stops_and_retry_after_is_respected(self):
        for code, expected in ((400, "terminal_http_error"), (401, "terminal_http_error"), (402, "terminal_http_error"), (403, "terminal_http_error"), (429, "retryable_http_error"), (503, "retryable_http_error")):
            with self.subTest(code=code):
                guard = CallGuard(self.root / str(code), self.policy)
                class Http(FixtureTransport):
                    def __call__(self, payload, **kwargs):
                        return ProviderResponse("", cost_kopecks="0", model=payload["model"], http_status=code, retry_after_seconds=20)
                self.assertEqual(guard.execute(self.request, Http(), fixture_validator).status, expected)
                retried = guard.execute(self.request, Http(), fixture_validator, retry=True)
                self.assertEqual(retried.status, "retry_after_pending" if code in (429, 503) else "retry_forbidden")

    def test_operation_deadline_limits_explicit_retries(self):
        guard = self.guard(replace(self.policy, timeout_seconds=0.01, operation_timeout_seconds=0.02))
        guard.execute(self.request, FixtureTransport("invalid"), fixture_validator)
        time.sleep(0.03)
        self.assertEqual(guard.execute(self.request, FixtureTransport(), fixture_validator, retry=True).status, "operation_deadline")

    def test_truncation_does_not_increase_output_cap(self):
        seen = []
        class Truncated(FixtureTransport):
            def __call__(self, payload, **kwargs):
                seen.append(payload["max_tokens"])
                return ProviderResponse("{}", cost_kopecks="1", model=payload["model"], finish_reason="length")
        transport = Truncated()
        self.assertEqual(self.run_request(transport).status, "truncated_response")
        self.run_request(transport, retry=True)
        self.assertEqual(seen, [128, 128])

    def test_reconcile_is_exact_idempotent_no_result_or_retry(self):
        guard = self.guard()
        outcome = guard.execute(self.request, FixtureTransport("unknown_cost"), fixture_validator)
        guard.reconcile(outcome.attempt_id, cost_kopecks="12.345678", evidence_ref="synthetic-billing-report")
        guard.reconcile(outcome.attempt_id, cost_kopecks="12.345678", evidence_ref="synthetic-billing-report")
        self.assertEqual(guard.snapshot()["cost"]["rub"], "0.12345678")
        self.assertEqual(guard.snapshot()["held_micro_rub"], 0)
        with self.assertRaises(ValueError):
            guard.reconcile(outcome.attempt_id, cost_kopecks="13", evidence_ref="different")
        reused = guard.execute(self.request, FixtureTransport(), fixture_validator)
        self.assertIsNone(reused.value)
        self.assertEqual(reused.status, "reconciled_no_result")

    def test_versions_full_context_and_actual_payload_change_fingerprint(self):
        variants = [replace(self.request, versions={**self.request.versions, key: "v2"}) for key in self.request.versions]
        variants += [replace(self.request, context={**self.request.context, "corrections": ["changed"]}),
                     replace(self.request, payload={**self.request.payload, "temperature": 0.2})]
        self.assertTrue(all(v.fingerprint != self.request.fingerprint for v in variants))

    def test_mutating_adapter_cannot_change_persisted_identity(self):
        class Mutating(FixtureTransport):
            def __call__(self, payload, **kwargs):
                payload["messages"][0]["content"] = "MUTATED"
                return ProviderResponse('{"text":"ok"}', cost_kopecks="1", model=payload["model"])
        self.assertEqual(self.run_request(Mutating()).status, "succeeded")
        request_file = next((self.root / "private_artifacts").glob("*-request.json"))
        self.assertNotIn("MUTATED", request_file.read_text())
        self.assertNotIn("MUTATED", self.request.payload["messages"][0]["content"])

    def test_expense_journal_contains_no_input_or_generated_text(self):
        self.run_request()
        raw = (self.root / "spending.sqlite3").read_bytes()
        self.assertNotIn(b"Synthetic M14 fixture", raw)
        self.assertNotIn(b"synthetic output", raw)

    def test_ambiguous_json_duplicate_keys_are_rejected(self):
        class Ambiguous(FixtureTransport):
            def __call__(self, payload, **kwargs):
                return ProviderResponse('{"text":"first","text":"second"}', cost_kopecks="1", model=payload["model"])
        self.assertEqual(self.run_request(Ambiguous()).status, "invalid_response")

    def test_corrupt_cached_response_does_not_become_new_result(self):
        self.run_request()
        response_file = next((self.root / "private_artifacts").glob("*-response.json"))
        document = json.loads(response_file.read_text())
        document["validated"] = {"text": "tampered"}
        response_file.write_text(json.dumps(document))
        transport = FixtureTransport()
        result = self.run_request(transport)
        self.assertEqual(result.status, "cached_result_unavailable")
        self.assertEqual(transport.calls, 0)

    def test_malformed_versions_and_expiry_fail_before_dispatch(self):
        for versions in (None, [], "wrong"):
            self.assertEqual(self.guard().execute(replace(self.request, versions=versions), FixtureTransport(), fixture_validator).status, "versions_missing")
        self.assertEqual(replace(self.policy, expires_at="tomorrow").validate(), "approval_expired_or_invalid")

    def test_call_fragment_matches_m01_and_reasoning_is_not_double_counted(self):
        # Exercise live-format EXPORT with a local stub and blocked networking;
        # this is a schema fixture, not a real or approved live run.
        from jsonschema import Draft202012Validator
        class LocalOnly(FixtureTransport):
            kind = "live"
            total_output_bound_verified = True
        guard = self.guard(replace(self.policy, mode="live"))
        guard.execute(self.request, LocalOnly(), fixture_validator)
        guard.execute(fixture_request("unapproved"), LocalOnly(), fixture_validator)
        schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/ege_v1/schema.json").read_text())
        validator = Draft202012Validator({"$ref": "#/$defs/call", "$defs": schema["$defs"]})
        call, = guard.contract_calls()
        self.assertEqual(list(validator.iter_errors(call)), [])
        self.assertEqual(call["cost_rub"], "0.1025")
        self.assertEqual(call["usage"]["output_tokens"], 20)
        self.assertEqual(call["usage"]["reasoning_tokens"], 8)

    def test_dynamic_second_stage_uses_same_approved_budget_rule(self):
        # Content depends on the first response; its hash is not in ceilings.
        versions = self.request.versions
        rule = {"stage": "grading", "provider": self.request.provider, "model": "synthetic-model",
                "versions": versions, "settings": {}, "max_request_bytes": 1024,
                "max_output_tokens": 128, "micro_rub": 1_000_000,
                "evidence_ref": "SYNTHETIC-bound-for-entire-class"}
        policy = replace(self.policy, budget_micro_rub=1_200_000, ceiling_rules={"grading": rule})
        guard = self.guard(policy)
        first = guard.execute(self.request, FixtureTransport(), fixture_validator)
        dynamic = replace(fixture_request(first.value["text"]), stage="grading")
        second = guard.execute(dynamic, FixtureTransport(), fixture_validator)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(guard.snapshot()["calls"], 2)
        self.assertEqual(guard.snapshot(fingerprints=[dynamic.fingerprint])["calls"], 1)
        # A third distinct stage would exceed the one shared budget.
        third = replace(fixture_request("third"), stage="grading")
        self.assertEqual(guard.execute(third, FixtureTransport(), fixture_validator).status, "budget_limit")
        for altered in (replace(dynamic, provider="different"),
                        replace(dynamic, payload={**dynamic.payload, "temperature": 1}),
                        replace(dynamic, versions={**versions, "prompt": "changed"}),
                        replace(dynamic, payload={**dynamic.payload, "messages": [{"content": "x" * 2000}]})):
            self.assertEqual(guard.execute(altered, FixtureTransport(), fixture_validator).status, "billing_ceiling_unproven")


if __name__ == "__main__":
    unittest.main()
