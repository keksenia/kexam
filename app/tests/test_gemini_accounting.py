"""Native Gemini accounting provenance: temporary ledgers and synthetic replies."""
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from app.call_guard import (CallGuard, ProviderResponse, accounting_summary,
    canonical, digest)
from app.guard_demo import fixture_policy, fixture_request, fixture_validator


TARIFF = "https://ai.google.dev/gemini-api/docs/pricing#synthetic-snapshot"


class LocalTransport:
    """Exercise real-call export format without a network-capable adapter."""
    kind = "live"
    single_attempt = True
    total_output_bound_verified = True

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, payload, *, timeout_seconds):
        self.calls += 1
        return replace(self.response, model=payload["model"])


class GeminiAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ege-gemini-accounting-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = replace(fixture_request(), provider="google")
        self.other = replace(fixture_request("second"), provider="google")
        self.policy = fixture_policy([self.request, self.other], mode="live")
        self.guard = CallGuard(self.root, self.policy)
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def estimate(self, **changes):
        values = dict(content='{"text":"synthetic output"}',
            usage={"prompt_tokens": 1000, "completion_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 20}},
            cost_kopecks="12.345678", cost_basis="token_estimate", tariff_ref=TARIFF,
            provider_metadata={"native_model_version": "synthetic-model-2026-10",
                "native_usage": {"promptTokenCount": 1000,
                    "candidatesTokenCount": 10, "thoughtsTokenCount": 20},
                "accounting": {"rounded_rub": "0.12345678", "tariff_ref": TARIFF}})
        values.update(changes)
        return ProviderResponse(**values)

    def execute(self, response=None, request=None):
        transport = LocalTransport(response or self.estimate())
        outcome = self.guard.execute(request or self.request, transport, fixture_validator)
        self.assertEqual(transport.calls, 1)
        return outcome

    def saved_response(self, call_id):
        with closing(sqlite3.connect(self.guard.db)) as con:
            row = con.execute("SELECT response_ref FROM calls WHERE id=?", (call_id,)).fetchone()
        return json.loads((self.root / row[0]).read_text())

    def rows(self):
        with closing(sqlite3.connect("file:" + str(self.guard.db) + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM calls ORDER BY created,id").fetchall()

    def assert_call_schema(self, call):
        schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/ege_v1/schema.json").read_text())
        validator = Draft202012Validator({"$ref": "#/$defs/call", "$defs": schema["$defs"]})
        self.assertEqual(list(validator.iter_errors(call)), [])

    def test_estimate_preserves_tariff_native_usage_and_thoughts_in_total(self):
        outcome = self.execute()
        self.assertEqual(outcome.status, "succeeded")
        envelope = self.saved_response(outcome.attempt_id)["envelope"]
        self.assertEqual(envelope["cost_basis"], "token_estimate")
        self.assertEqual(envelope["tariff_ref"], TARIFF)
        self.assertEqual(envelope["provider_metadata"], self.estimate().provider_metadata)
        call, = self.guard.contract_calls()
        self.assert_call_schema(call)
        self.assertEqual(call["cost_basis"], "token_estimate")
        self.assertEqual(call["tariff_ref"], TARIFF)
        self.assertEqual(call["cost_rub"], "0.12345678")
        self.assertEqual(call["usage"], {"input_tokens": 1000, "output_tokens": 30,
            "reasoning_tokens": 20, "reasoning_in_output": True})
        self.assertEqual(call["settings"]["native_model_version"], "synthetic-model-2026-10")
        snapshot = self.guard.snapshot()
        self.assertEqual(snapshot["known_micro_rub"], 123457)
        self.assertEqual(snapshot["cost"], {"rub": "0.12345678", "status": "known"})
        self.assertEqual(snapshot["cost_basis"], "token_estimate")
        self.assertEqual(snapshot["estimated_calls"], 1)
        self.assertEqual(snapshot["provider_report_calls"], 0)
        self.assertEqual(snapshot["estimated_rub"], "0.12345678")
        self.assertEqual(snapshot["provider_report_rub"], "0")
        self.assertEqual(snapshot["tariff_refs"], [TARIFF])

    def test_estimate_missing_tariff_or_token_counts_never_settles(self):
        changes = [{"tariff_ref": None}, {"tariff_ref": "  "}, {"usage": None},
            {"usage": {"completion_tokens": 30}}, {"usage": {"prompt_tokens": 1000}},
            {"usage": {"prompt_tokens": True, "completion_tokens": 30}},
            {"usage": {"prompt_tokens": 1000, "completion_tokens": -1}},
            {"cost_basis": "provider_terms"}, {"cost_basis": {"unsupported": "basis"}}]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                root = self.root / str(index)
                guard = CallGuard(root, self.policy)
                transport = LocalTransport(self.estimate(**change))
                outcome = guard.execute(self.request, transport, fixture_validator)
                self.assertEqual(outcome.status, "cost_unknown")
                blocked = guard.execute(self.other, transport, fixture_validator)
                self.assertEqual(blocked.status, "unresolved_spending")
                self.assertEqual(transport.calls, 1)
                self.assertEqual(guard.snapshot()["cost_basis"], "unknown")
                call, = guard.contract_calls()
                self.assertEqual(call["cost_basis"], "unknown")
                self.assertIsNone(call["cost_rub"])
                self.assertIsNone(call["tariff_ref"])
                self.assert_call_schema(call)

    def test_provider_report_without_tokens_remains_backward_compatible(self):
        outcome = self.execute(ProviderResponse('{"text":"reported"}', cost_kopecks="2.5"))
        self.assertEqual(outcome.status, "succeeded")
        call, = self.guard.contract_calls()
        self.assertEqual(call["cost_basis"], "provider_report")
        self.assertEqual(call["cost_rub"], "0.025")
        self.assertIsNone(call["tariff_ref"])
        self.assertEqual(self.guard.snapshot()["cost_basis"], "provider_report")

    def test_historical_envelopes_and_rows_are_not_migrated_by_reads(self):
        outcome = self.execute(ProviderResponse('{"text":"historical"}', cost_kopecks="2.5"))
        row, = self.rows()
        response_path = self.root / row["response_ref"]
        document = json.loads(response_path.read_text())
        for name in ("cost_basis", "tariff_ref", "provider_metadata"):
            document["envelope"].pop(name)
        response_path.write_text(canonical(document))
        with closing(sqlite3.connect(self.guard.db)) as con:
            with con:
                con.execute("UPDATE calls SET response_sha256=? WHERE id=?", (digest(document), outcome.attempt_id))
        before_rows = [dict(row) for row in self.rows()]
        before_bytes = response_path.read_bytes()
        summary = accounting_summary(self.rows(), self.root)
        self.assertEqual(summary["cost_basis"], "provider_report")
        self.assertEqual(summary["provider_report_rub"], "0.025")
        call, = self.guard.contract_calls()
        self.assertEqual(call["cost_basis"], "provider_report")
        self.guard.snapshot()
        self.assertEqual([dict(row) for row in self.rows()], before_rows)
        self.assertEqual(response_path.read_bytes(), before_bytes)

    def test_mixed_ledger_keeps_invoice_and_estimate_totals_separate(self):
        self.execute(ProviderResponse('{"text":"historical"}', cost_kopecks="2.5"))
        self.execute(request=self.other)
        snapshot = self.guard.snapshot()
        self.assertEqual(snapshot["cost"]["rub"], "0.14845678")
        self.assertEqual(snapshot["cost_basis"], "mixed")
        self.assertEqual(snapshot["provider_report_calls"], 1)
        self.assertEqual(snapshot["estimated_calls"], 1)
        self.assertEqual(snapshot["provider_report_rub"], "0.025")
        self.assertEqual(snapshot["estimated_rub"], "0.12345678")
        selected = self.guard.snapshot(fingerprints=[self.other.fingerprint])
        self.assertEqual(selected["cost_basis"], "token_estimate")
        self.assertEqual(selected["provider_report_calls"], 0)

    def test_invalid_model_output_still_records_tariff_estimate(self):
        outcome = self.execute(self.estimate(content='{"score":0}'))
        self.assertEqual(outcome.status, "invalid_response")
        call, = self.guard.contract_calls()
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["cost_basis"], "token_estimate")
        self.assertEqual(self.guard.snapshot()["estimated_calls"], 1)

    def test_thought_tokens_cannot_hide_total_output_limit_violation(self):
        outcome = self.execute(self.estimate(usage={"prompt_tokens": 1000,
            "completion_tokens": 130, "completion_tokens_details": {"reasoning_tokens": 120}}))
        self.assertEqual(outcome.status, "output_bound_breached")
        blocked = self.guard.execute(self.other, LocalTransport(self.estimate()), fixture_validator)
        self.assertEqual(blocked.status, "unresolved_spending")
        call, = self.guard.contract_calls()
        self.assertEqual(call["usage"]["output_tokens"], 130)
        self.assertEqual(call["usage"]["reasoning_tokens"], 120)
        self.assertEqual(call["cost_basis"], "token_estimate")

    def test_untransmitted_call_is_zero_without_provider_invoice(self):
        transport = LocalTransport(self.estimate())
        outcome = self.guard.execute(self.request, transport, fixture_validator, before_dispatch=lambda: False)
        self.assertEqual(outcome.status, "cancelled_before_dispatch")
        self.assertEqual(transport.calls, 0)
        summary = self.guard.snapshot()
        self.assertEqual(summary["cost_basis"], "not_incurred")
        self.assertEqual(summary["provider_report_calls"], 0)
        self.assertEqual(summary["estimated_calls"], 0)
        self.assertEqual(self.guard.contract_calls(), [])

    def test_unknown_error_cannot_claim_zero_cost_from_provider_terms(self):
        outcome = self.execute(self.estimate(content="", usage=None, cost_kopecks=None,
            cost_basis="unknown", tariff_ref=None, http_status=400,
            provider_metadata={"uncharged_terms_ref": "https://ai.google.dev/gemini-api/docs/billing"}))
        self.assertEqual(outcome.status, "cost_unknown")
        self.assertEqual(self.guard.snapshot()["cost"], {"rub": None, "status": "unknown"})
        self.assertEqual(self.guard.snapshot()["estimated_calls"], 0)

    def test_explicit_provider_reconciliation_does_not_rewrite_estimate_envelope(self):
        outcome = self.execute(self.estimate(usage=None))
        self.assertEqual(outcome.status, "cost_unknown")
        saved_before = self.saved_response(outcome.attempt_id)
        self.guard.reconcile(outcome.attempt_id, cost_kopecks="0", evidence_ref="synthetic-checked-provider-billing")
        self.assertEqual(self.saved_response(outcome.attempt_id), saved_before)
        call, = self.guard.contract_calls()
        self.assertEqual(call["cost_basis"], "provider_report")
        self.assertIsNone(call["tariff_ref"])
        self.assertEqual(call["cost_rub"], "0")
        self.assertEqual(self.guard.snapshot()["cost_basis"], "provider_report")
        self.assertEqual(accounting_summary(self.rows(), self.root,
            reconciled_call_ids=[outcome.attempt_id])["cost_basis"], "provider_report")

    def test_native_wire_fingerprint_and_settings_are_exported(self):
        context = {**self.request.context, "native_request_sha256": "a" * 64,
            "native_endpoint": "https://generativelanguage.googleapis.com/v1beta/models/synthetic-model:generateContent",
            "native_request_bytes": 1234, "native_adapter_version": "synthetic-native-1",
            "native_configuration_sha256": "b" * 64}
        request = replace(self.request, context=context)
        self.guard = CallGuard(self.root / "native", fixture_policy([request], mode="live"))
        outcome = self.execute(request=request)
        self.assertEqual(outcome.status, "succeeded")
        call, = self.guard.contract_calls()
        self.assert_call_schema(call)
        self.assertEqual(call["request_sha256"], context["native_request_sha256"])
        for name in ("native_endpoint", "native_request_bytes", "native_adapter_version", "native_configuration_sha256"):
            self.assertEqual(call["settings"][name], context[name])
        self.assertEqual(call["prompt_ref"], call["request_ref"] + "#/payload/messages")
        self.assertNotIn("input", call["settings"])

    def test_malformed_ancillary_metadata_does_not_lose_cost_settlement(self):
        outcome = self.execute(self.estimate(provider_metadata={"invalid": float("nan")}))
        self.assertEqual(outcome.status, "succeeded")
        self.assertIsNone(self.saved_response(outcome.attempt_id)["envelope"]["provider_metadata"])
        self.assertEqual(self.guard.snapshot()["cost_basis"], "token_estimate")

    def test_read_only_accounting_detects_changed_tariff_artifact(self):
        outcome = self.execute()
        row, = self.rows()
        response_path = self.root / row["response_ref"]
        document = self.saved_response(outcome.attempt_id)
        document["envelope"]["tariff_ref"] = "tampered-tariff"
        response_path.write_text(canonical(document))
        with self.assertRaisesRegex(ValueError, "integrity"):
            accounting_summary(self.rows(), self.root)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.guard.contract_calls()


if __name__ == "__main__":
    unittest.main()
