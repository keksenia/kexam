"""Free native access gates: synthetic metadata, credentials and temporary files."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app import gemini_preflight as preflight
from app.call_guard import CallGuard, ProviderResponse, canonical
from app.gemini_config import identity
from app.guard_demo import fixture_policy, fixture_request
from app.live_checking import FreshChecking
from app.server import WorkStore
from app.tests.test_gemini_transport import FakeHTTP, FakeOpener, pricing


SYNTHETIC_KEY = "AIza-synthetic-preflight-key-only"


class GeminiPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ege-gemini-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credential = self.root / "selected-synthetic.env"
        self.credential.write_text("GEMINI_API_KEY=" + SYNTHETIC_KEY + "\n")
        self.receipt = self.root / "access.json"
        self.config = dict(provider="google", model="gemini-3.8-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            credential_file=str(self.credential), credential_variable="GEMINI_API_KEY",
            automatic_retries=0, service_tier="standard", pricing=pricing(),
            network={"mode": "system_vpn", "allow_direct_fallback": False})
        for name, options in (
                ("app.gemini_preflight.load_config", {"return_value": self.config}),
                ("app.gemini_preflight.ACCESS_PATH", {"new": self.receipt}),
                ("socket.socket.connect", {"side_effect": AssertionError("Network forbidden")}),
                ("socket.create_connection", {"side_effect": AssertionError("Network forbidden")}),
                ("socket.getaddrinfo", {"side_effect": AssertionError("DNS forbidden")}),
                ("app.gemini_preflight.make_vpn_opener", {"side_effect": AssertionError("Real opener forbidden")})):
            blocker = patch(name, **options)
            blocker.start()
            self.addCleanup(blocker.stop)

    def metadata(self, **changes):
        value = dict(name="models/" + self.config["model"],
            supportedGenerationMethods=["generateContent", "countTokens"],
            inputTokenLimit=1000000, outputTokenLimit=65536)
        value.update(changes)
        return canonical(value).encode()

    def probe(self, raw=None, *, error=None):
        if isinstance(error, HTTPError):
            self.addCleanup(error.close)
        opener = FakeOpener(FakeHTTP(self.metadata() if raw is None else raw), error=error)
        result = preflight.check_access(opener=opener)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url,
            self.config["base_url"] + "/models/" + self.config["model"])
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 25)
        self.assertEqual(request.get_header("X-goog-api-key"), SYNTHETIC_KEY)
        self.assertNotIn(SYNTHETIC_KEY, request.full_url)
        self.assertNotIn(":generateContent", request.full_url)
        self.assertEqual(result["generation_requests"], 0)
        self.assertEqual(result["model_requests"], 0)
        self.assertFalse(result["credentials_persisted"])
        self.assertNotIn(SYNTHETIC_KEY, canonical(result))
        self.assertFalse(self.receipt.exists())
        return result

    def save_receipt(self, result):
        self.receipt.write_text(canonical(result))

    def failure(self, message, *, status="INVALID_ARGUMENT", reason=None, http_status=400):
        details = [] if reason is None else [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
        raw = canonical({"error": {"message": message, "status": status, "details": details}}).encode()
        return HTTPError(self.config["base_url"] + "/models/" + self.config["model"],
            http_status, "synthetic error", {}, BytesIO(raw))

    def test_get_metadata_exact_model_succeeds_without_generation_or_saved_key(self):
        result = self.probe()
        self.assertEqual(result["http_status"], 200)
        self.assertTrue(result["model_available"])
        self.assertEqual(result["configuration_sha256"], identity(self.config))
        self.assertEqual(result["credential_file_mtime_ns"], self.credential.stat().st_mtime_ns)
        self.assertEqual(result["network_mode"], "system_vpn")
        self.assertEqual(result["inputTokenLimit"], 1000000)
        self.assertEqual(result["outputTokenLimit"], 65536)
        self.save_receipt(result)
        self.assertIsNone(preflight.access_problem(self.config))
        self.assertNotIn(SYNTHETIC_KEY, self.receipt.read_text())

    def test_default_missing_vpn_stops_before_credential_stat_read_or_socket(self):
        for reason in ("vpn_required", "vpn_route_unavailable"):
            with self.subTest(reason=reason), \
                    patch("app.gemini_preflight.vpn_problem", return_value=reason), \
                    patch("app.gemini_preflight.read_credential", side_effect=AssertionError("Key must not be read")) as reader, \
                    patch("app.gemini_preflight.Path.stat", side_effect=AssertionError("Key must not be inspected")) as stat, \
                    patch("socket.socket", side_effect=AssertionError("Socket must not be opened")) as socket:
                result = preflight.check_access()
                self.assertEqual(result["error"], reason)
                self.assertFalse(result["model_available"])
                self.assertEqual(result["generation_requests"], 0)
                self.assertNotIn("credential_file_mtime_ns", result)
                reader.assert_not_called()
                stat.assert_not_called()
                socket.assert_not_called()

    def test_wrong_model_or_missing_generate_method_closes_access(self):
        cases = [{"name": "models/gemini-3.8-pro"},
            {"name": "models/" + self.config["model"] + "-snapshot"},
            {"supportedGenerationMethods": ["countTokens"]},
            {"supportedGenerationMethods": []}]
        for change in cases:
            with self.subTest(change=change):
                result = self.probe(self.metadata(**change))
                self.assertFalse(result["model_available"])
                self.assertEqual(result["error"], "model_metadata_mismatch")
                self.assertNotIn("inputTokenLimit", result)
                self.save_receipt(result)
                self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")
                self.receipt.unlink()

    def test_location_unsupported_http400_exposes_only_stable_reason(self):
        private_message = "User location is not supported for the API use. " + SYNTHETIC_KEY
        result = self.probe(error=self.failure(private_message))
        self.assertEqual(result["http_status"], 400)
        self.assertEqual(result["provider_error_status"], "INVALID_ARGUMENT")
        self.assertEqual(result["provider_error_reasons"], ["LOCATION_UNSUPPORTED"])
        self.assertNotIn(private_message, canonical(result))
        self.save_receipt(result)
        self.assertEqual(preflight.access_problem(self.config), "gemini_location_unsupported")

    def test_invalid_expired_key_errors_never_publish_provider_message(self):
        for reason, message in (("API_KEY_INVALID", "API key not valid. " + SYNTHETIC_KEY),
                ("API_KEY_EXPIRED", "Expired selected key: " + SYNTHETIC_KEY)):
            with self.subTest(reason=reason):
                result = self.probe(error=self.failure(message, reason=reason))
                self.assertEqual(result["provider_error_reasons"], [reason])
                self.assertNotIn(message, canonical(result))
                self.save_receipt(result)
                self.assertEqual(preflight.access_problem(self.config), "credential_missing_or_invalid")
                self.receipt.unlink()

    def test_billing_http_error_preserves_safe_reason_without_raw_message(self):
        message = "Enable billing for selected project " + SYNTHETIC_KEY
        result = self.probe(error=self.failure(message, status="FAILED_PRECONDITION"))
        self.assertEqual(result["provider_error_reasons"], ["BILLING_REQUIRED"])
        self.assertNotIn(message, canonical(result))
        self.save_receipt(result)
        self.assertEqual(preflight.access_problem(self.config), "gemini_billing_required")

    def test_unknown_provider_status_reason_and_message_are_omitted(self):
        result = self.probe(error=self.failure("echoed secret " + SYNTHETIC_KEY,
            status="SYNTHETIC_PRIVATE_STATUS", reason="SYNTHETIC_PRIVATE_REASON"))
        serialized = canonical(result)
        self.assertNotIn("SYNTHETIC_PRIVATE_STATUS", serialized)
        self.assertNotIn("SYNTHETIC_PRIVATE_REASON", serialized)
        self.assertNotIn("provider_error_status", result)
        self.assertNotIn("provider_error_reasons", result)
        self.save_receipt(result)
        self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")

    def test_receipt_requires_configuration_endpoint_method_and_zero_generations(self):
        verified = self.probe()
        changes = [{"configuration_sha256": "a" * 64},
            {"endpoint": verified["endpoint"] + ":generateContent"}, {"method": "POST"},
            {"generation_requests": 1}, {"generation_requests": "0"},
            {"credential_file_mtime_ns": verified["credential_file_mtime_ns"] + 1}]
        for change in changes:
            with self.subTest(change=change):
                self.save_receipt({**verified, **change})
                self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")
        self.save_receipt(verified)
        changed_config = deepcopy(self.config)
        changed_config["pricing"]["usd_to_rub"] = "90"
        self.assertEqual(preflight.access_problem(changed_config), "gemini_access_unverified")

    def test_replacing_selected_synthetic_key_invalidates_previous_receipt(self):
        verified = self.probe()
        self.save_receipt(verified)
        self.assertIsNone(preflight.access_problem(self.config))
        previous = self.credential.stat()
        self.credential.write_text("GEMINI_API_KEY=AIza-synthetic-replacement-key\n")
        os.utime(self.credential, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
        self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")

    def test_receipt_reasons_require_list_of_exact_codes(self):
        verified = self.probe()
        for reasons in ("API_KEY_INVALID with extra text", None, {"reason": "API_KEY_INVALID"},
                [True], ["API_KEY_INVALID_EXTRA"]):
            with self.subTest(reasons=reasons):
                self.save_receipt({**verified, "http_status": 400, "model_available": False,
                    "provider_error_reasons": reasons})
                self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")

    def test_missing_or_malformed_receipt_fails_closed(self):
        self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")
        for text in ("{", "null", "[]", "42", "{}", '{"method":"GET","method":"POST"}'):
            with self.subTest(text=text):
                self.receipt.write_text(text)
                self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")

    def test_missing_or_invalid_selected_key_has_stable_error_without_request(self):
        for content in (None, "GEMINI_API_KEY=\n", "GEMINI_API_KEY=short\n", "OTHER_VARIABLE=synthetic\n"):
            with self.subTest(content=content):
                if content is None:
                    self.credential.unlink(missing_ok=True)
                else:
                    self.credential.write_text(content)
                opener = FakeOpener(FakeHTTP(self.metadata()))
                result = preflight.check_access(opener=opener)
                self.assertEqual(result["error"], "credential_missing_or_invalid")
                self.assertFalse(result["model_available"])
                self.assertEqual(result["generation_requests"], 0)
                self.assertEqual(opener.calls, [])

    def test_duplicate_oversized_or_invalid_metadata_never_opens_access(self):
        for raw in (b"{", b"[]", b"x" * 65537,
                b'{"name":"models/gemini-3.8-flash","name":"models/other"}'):
            with self.subTest(raw=raw[:40]):
                result = self.probe(raw)
                self.assertFalse(result["model_available"])
                self.assertIn(result["error"], {"metadata_network_or_format_error", "model_metadata_mismatch"})
                self.save_receipt(result)
                self.assertEqual(preflight.access_problem(self.config), "gemini_access_unverified")
                self.receipt.unlink()

    def test_failed_network_exception_does_not_echo_key_and_never_retries(self):
        result = self.probe(error=OSError("synthetic credential echo " + SYNTHETIC_KEY))
        self.assertFalse(result["model_available"])
        self.assertEqual(result["error"], "metadata_network_or_format_error")

    def test_availability_uses_reconciliation_event_without_rewriting_envelope(self):
        request = replace(fixture_request(), provider="google",
            payload={**fixture_request().payload, "model": self.config["model"]})
        rule = dict(stage=request.stage, provider=request.provider, model=request.payload["model"],
            versions=request.versions, settings={}, max_request_bytes=8388608,
            max_output_tokens=128, evidence_ref="SYNTHETIC-NO-MONEY")
        policy = fixture_policy([request], mode="live", budget_mode="settled_spend",
            ceilings={}, max_total_calls=20, dispatch_rules={"shape": rule})
        ledger = self.root / "synthetic-ledger"
        guard = CallGuard(ledger, policy)

        class UnknownEstimate:
            kind = "live"
            single_attempt = True
            def __call__(self, payload, *, timeout_seconds):
                return ProviderResponse("{}", model=payload["model"], usage=None,
                    cost_kopecks="1", cost_basis="token_estimate", tariff_ref="synthetic-tariff")

        with patch("app.live_checking.gemini_config", return_value=self.config), \
                patch("app.live_checking.read_credential", side_effect=AssertionError("Real keys forbidden")):
            service = FreshChecking(WorkStore(self.root / "works"), policy=policy, ledger=ledger,
                transport_factory=lambda request: UnknownEstimate(), solver=lambda condition: {})
            self.addCleanup(service.close)
            service.work_profiles = None
            service.upload_scope = None
            outcome = guard.execute(request, UnknownEstimate(), lambda value: value)
            self.assertEqual(outcome.status, "cost_unknown")
            self.assertEqual(service.availability()["reason"], "unresolved_spending")
            with closing(sqlite3.connect(guard.db.as_uri() + "?mode=ro", uri=True)) as con:
                response_ref, = con.execute("SELECT response_ref FROM calls WHERE id=?", (outcome.attempt_id,)).fetchone()
            response_path = ledger / response_ref
            before = response_path.read_bytes()
            guard.reconcile(outcome.attempt_id, cost_kopecks="0", evidence_ref="synthetic-verified-provider-invoice")
            available = service.availability()
            self.assertIsNone(available["reason"])
            self.assertTrue(available["paid_api_enabled"])
            self.assertEqual(available["spending"]["cost_basis"], "provider_report")
            self.assertEqual(available["spending"]["provider_report_calls"], 1)
            self.assertEqual(available["spending"]["estimated_calls"], 0)
            self.assertEqual(available["spending"]["provider_report_rub"], "0")
            self.assertEqual(response_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
