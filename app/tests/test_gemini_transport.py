"""Synthetic native Gemini wire/accounting checks. Network is forbidden."""
from base64 import b64encode
from copy import deepcopy
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.call_guard import canonical, kopecks_to_micro
from app.gemini_transport import (GeminiTransport, NoRedirect, TransportError,
    build_native_request, endpoint_for_model, parse_response, read_credential,
    retry_delay, validate_pricing)


def pricing():
    return dict(model="gemini-3.8-flash", input_usd_per_million="0.75",
        output_usd_per_million="3.75", cached_input_usd_per_million="0.075",
        usd_to_rub="85.4173", source="https://ai.google.dev/gemini-api/docs/pricing",
        fx_source="https://www.cbr.ru/", valid_until="2099-01-01T00:00:00Z",
        tariff_ref="synthetic-test-tariff")


def payload():
    return dict(model="gemini-3.8-flash", max_tokens=6000, reasoning_effort="low",
        response_format={"type": "json_object"}, stream=False,
        messages=[dict(role="system", content="Synthetic instruction"),
            dict(role="user", content=[{"type": "text", "text": "Synthetic only"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64encode(b"synthetic-image").decode()}}])])


def envelope(**updates):
    data = dict(modelVersion="gemini-3.8-flash", responseId="synthetic-response",
        candidates=[dict(content=dict(role="model", parts=[{"text": '{"pages":[]}'}]), finishReason="STOP")],
        usageMetadata=dict(promptTokenCount=100, candidatesTokenCount=20,
            thoughtsTokenCount=10, cachedContentTokenCount=20, totalTokenCount=130,
            serviceTier="standard"))
    data.update(updates)
    return json.dumps(data).encode()


class FakeHTTP(BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status, self.headers = status, headers or {}
    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class NativeWireTests(unittest.TestCase):
    def setUp(self):
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.body = payload()
        self.key = "AIza-synthetic-test-key-only"
        self.approved_sha = hashlib.sha256(canonical(self.body).encode()).hexdigest()
        self.native = build_native_request(self.body)
        self.native_sha = hashlib.sha256(canonical(self.native).encode()).hexdigest()

    def transport(self, opener, **options):
        return GeminiTransport(self.key, allowed_body_hashes=[self.approved_sha],
            allowed_native_body_hashes=[self.native_sha], pricing=pricing(), opener=opener, **options)

    def test_native_one_post_exact_hash_and_key_only_in_header(self):
        opener = FakeOpener(FakeHTTP(envelope()))
        transport = self.transport(opener)
        response = transport(self.body, timeout_seconds=10)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-goog-api-key"), self.key)
        self.assertEqual(request.data, canonical(self.native).encode())
        self.assertNotIn(self.key, repr(transport))
        self.assertNotIn(self.key, request.full_url)
        self.assertNotIn(self.key, request.data.decode())
        self.assertEqual(response.provider_metadata["native_body_sha256"], self.native_sha)
        self.assertEqual(response.provider_metadata["native_body_bytes"], len(request.data))
        self.assertEqual(timeout, 10)

    def test_image_system_text_thinking_and_cap_preserved(self):
        body = self.native
        self.assertEqual(body["systemInstruction"], {"parts": [{"text": "Synthetic instruction"}]})
        self.assertEqual(body["contents"][0]["parts"][0], {"text": "Synthetic only"})
        self.assertEqual(body["contents"][0]["parts"][1]["inlineData"],
            {"mimeType": "image/png", "data": b64encode(b"synthetic-image").decode()})
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "LOW", "includeThoughts": False})
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 6000)
        self.assertNotIn("candidateCount", body["generationConfig"])
        self.assertEqual(body["generationConfig"]["responseFormat"], {"text": {"mimeType": "APPLICATION_JSON"}})
        self.assertEqual(body["serviceTier"], "standard")

    def test_strict_schema_kept_exactly_and_input_not_mutated(self):
        schema = {"type": "object", "properties": {"pages": {"type": "array", "items": {"type": "string"}}},
            "required": ["pages"], "additionalProperties": False}
        self.body["response_format"] = {"type": "json_schema", "json_schema": {"name": "read", "strict": True, "schema": schema}}
        before = deepcopy(self.body)
        native = build_native_request(self.body)
        self.assertEqual(native["generationConfig"]["responseFormat"]["text"]["schema"], schema)
        self.assertEqual(self.body, before)

    def test_internal_changed_body_never_opens_network(self):
        opener = FakeOpener()
        with self.assertRaisesRegex(TransportError, "unapproved_body"):
            self.transport(opener)({**self.body, "max_tokens": 12000}, timeout_seconds=10)
        self.assertEqual(opener.calls, [])

    def test_native_changed_body_never_opens_network(self):
        opener = FakeOpener()
        transport = self.transport(opener)
        transport.allowed_native_body_hashes = frozenset({"0" * 64})
        with self.assertRaisesRegex(TransportError, "unapproved_native_body"):
            transport(self.body, timeout_seconds=10)
        self.assertEqual(opener.calls, [])

    def test_no_redirect_or_retry_and_unknown_error_cost(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/"))
        error = HTTPError(endpoint_for_model(self.body["model"]), 429, "error",
            {"Retry-After": "300"}, BytesIO(b'{"error":{"code":429}}'))
        opener = FakeOpener(error=error)
        response = self.transport(opener)(self.body, timeout_seconds=10)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(response.http_status, 429)
        self.assertEqual(response.retry_after_seconds, 300)
        self.assertIsNone(response.cost_kopecks)
        self.assertEqual(response.cost_basis, "unknown")

    def test_redirect_response_is_returned_without_following_or_cost(self):
        opener = FakeOpener(FakeHTTP(b"", 302))
        response = self.transport(opener)(self.body, timeout_seconds=10)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(response.http_status, 302)
        self.assertIsNone(response.cost_kopecks)

    def test_transport_timeout_does_not_leak_exception_or_retry(self):
        opener = FakeOpener(error=TimeoutError("credential or pupil data"))
        with self.assertRaisesRegex(TransportError, "provider_transport_failed") as caught:
            self.transport(opener)(self.body, timeout_seconds=10)
        self.assertNotIn("pupil", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_response_size_limit_prevents_parse(self):
        opener = FakeOpener(FakeHTTP(b"x" * 200))
        with patch("app.gemini_transport.MAX_ENVELOPE_BYTES", 100):
            with self.assertRaisesRegex(TransportError, "provider_envelope_too_large"):
                self.transport(opener)(self.body, timeout_seconds=10)
        self.assertEqual(len(opener.calls), 1)

    def test_expired_tariff_stops_before_network(self):
        opener = FakeOpener()
        transport = self.transport(opener)
        with patch("app.gemini_transport.time.time", return_value=5_000_000_000):
            with self.assertRaisesRegex(TransportError, "pricing_expired"):
                transport(self.body, timeout_seconds=10)
        self.assertEqual(opener.calls, [])

    def test_configuration_can_allow_native_ids_but_not_redirect_paths(self):
        native_body = {**self.body, "model": "gemini-3.6-flash"}
        self.assertIn("contents", build_native_request(native_body, allowed_models={"gemini-3.6-flash"}))
        for model in ("google/gemini-3.8-flash", "../gemini-3.8-flash", "gemini-3.8-flash?key=secret", "https://other.example/"):
            with self.assertRaisesRegex(TransportError, "native_model_invalid"):
                endpoint_for_model(model)
        with self.assertRaisesRegex(TransportError, "native_model_not_allowed"):
            build_native_request(native_body, allowed_models={self.body["model"]})

    def test_paid_tools_streaming_remote_images_or_extra_options_rejected(self):
        for changed in ({**self.body, "tools": []}, {**self.body, "stream": True}, {**self.body, "temperature": 1}):
            with self.assertRaises(TransportError):
                build_native_request(changed)
        for url in ("https://other.example/image.png", "data:image/png;base64,!", "data:image/png;base64,"):
            changed = deepcopy(self.body)
            changed["messages"][1]["content"][1]["image_url"]["url"] = url
            with self.assertRaisesRegex(TransportError, "native_image_invalid"):
                build_native_request(changed)


class ResponseTests(unittest.TestCase):
    def response(self, raw=None, **options):
        return parse_response(envelope() if raw is None else raw, requested_model="gemini-3.8-flash", pricing=pricing(), **options)

    def test_tokens_include_thinking_once_and_cached_input_not_double_counted(self):
        response = self.response()
        self.assertEqual(response.usage["prompt_tokens"], 100)
        self.assertEqual(response.usage["completion_tokens"], 30)
        self.assertEqual(response.usage["completion_tokens_details"]["reasoning_tokens"], 10)
        self.assertEqual(response.usage["prompt_tokens_details"]["cached_tokens"], 20)
        self.assertEqual(response.cost_basis, "token_estimate")
        self.assertEqual(response.tariff_ref, "synthetic-test-tariff")
        self.assertEqual(Decimal(response.cost_kopecks), Decimal("1.486262"))
        self.assertIsNotNone(kopecks_to_micro(response.cost_kopecks))
        self.assertEqual(response.provider_metadata["accounting"]["usd"], "0.000174")

    def test_thought_text_never_enters_model_content(self):
        raw = envelope(candidates=[dict(content=dict(parts=[{"text": "private thought", "thought": True},
            {"text": '{"pages":[]}', "thought": False}]), finishReason="STOP")])
        response = self.response(raw)
        self.assertEqual(response.content, '{"pages":[]}')
        self.assertEqual(response.provider_metadata['thought_summary']['parts'], ['private thought'])

    def test_refusal_and_thinking_only_truncation_preserve_explicit_signals(self):
        usage = dict(promptTokenCount=100, thoughtsTokenCount=6000, totalTokenCount=6100)
        response = self.response(envelope(usageMetadata=usage, candidates=[dict(finishReason="MAX_TOKENS")]))
        self.assertEqual((response.content, response.finish_reason), ("", "length"))
        self.assertEqual(response.usage["completion_tokens"], 6000)
        self.assertIsNotNone(response.cost_kopecks)
        response = self.response(envelope(candidates=[dict(finishReason="SAFETY")]))
        self.assertEqual(response.finish_reason, "refusal")
        response = self.response(envelope(candidates=[], promptFeedback={"blockReason": "SAFETY"}))
        self.assertEqual(response.finish_reason, "refusal")

    def test_snapshot_model_normalized_with_native_version_retained(self):
        response = self.response(envelope(modelVersion="gemini-3.8-flash-2026-10-01"))
        self.assertEqual(response.model, "gemini-3.8-flash")
        self.assertEqual(response.provider_metadata["native_model_version"], "gemini-3.8-flash-2026-10-01")
        mismatch = self.response(envelope(modelVersion="gemini-3.8-pro"))
        self.assertEqual(mismatch.model, "gemini-3.8-pro")
        self.assertIsNone(mismatch.cost_kopecks)

    def test_invalid_counts_cache_tier_or_missing_total_never_invent_cost(self):
        normal = json.loads(envelope())["usageMetadata"]
        bad = [None, {**normal, "promptTokenCount": True}, {**normal, "candidatesTokenCount": -1},
            {**normal, "totalTokenCount": 140}, {**normal, "cachedContentTokenCount": 101},
            {**normal, "toolUsePromptTokenCount": 1}, {**normal, "serviceTier": "priority"},
            {**normal, "serviceTier": []}, {k: v for k, v in normal.items() if k != "totalTokenCount"}]
        for usage in bad:
            response = self.response(envelope(usageMetadata=usage))
            self.assertEqual(response.finish_reason, "invalid_usage")
            self.assertIsNone(response.cost_kopecks)

    def test_multiple_candidates_unknown_finish_and_nontext_content_not_accepted(self):
        for candidates in ([{}, {}], [dict(finishReason="OTHER")], [dict(finishReason=[])],
            [dict(finishReason="STOP", content={"parts": [{"functionCall": {"name": "unapproved"}}]})]):
            self.assertEqual(self.response(envelope(candidates=candidates)).finish_reason, "invalid_envelope")

    def test_unknown_cost_is_not_zero_even_documented_uncharged_error(self):
        for status in (400, 401, 429, 500, 503):
            response = self.response(b'{"error":{"code":400}}', status=status)
            self.assertIsNone(response.cost_kopecks)
            self.assertEqual(response.cost_basis, "unknown")
            if status in {400, 500}:
                self.assertEqual(response.provider_metadata["accounting"]["kind"], "documented_uncharged_error")

    def test_no_cost_without_explicit_valid_pricing(self):
        response = parse_response(envelope(), requested_model="gemini-3.8-flash")
        self.assertIsNone(response.cost_kopecks)
        old = pricing()
        old["valid_until"] = "2000-01-01T00:00:00Z"
        response = parse_response(envelope(), requested_model="gemini-3.8-flash", pricing=old)
        self.assertIsNone(response.cost_kopecks)

    def test_duplicate_keys_nan_nonfinite_or_nonjson_envelopes_rejected(self):
        for raw in (b'{"usageMetadata":{},"usageMetadata":{}}', b'{"n":NaN}', b'{"n":1e309}', b'<html>error</html>'):
            with self.assertRaisesRegex(TransportError, "provider_envelope_invalid"):
                self.response(raw)


class ConfigurationTests(unittest.TestCase):
    def test_read_only_selected_key_and_ignore_environment_override(self):
        key = "AIza-synthetic-selected-key"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GEMINI_API_KEY": "ignored-environment-key"}):
            path = Path(tmp) / "synthetic.env"
            path.write_text("OTHER_SYNTHETIC=unrelated\nexport GEMINI_API_KEY='" + key + "' # comment\nLLM_API_KEY=unrelated\n")
            self.assertEqual(read_credential(path), key)
            path.write_text('GEMINI_API_KEY="' + key + '"\n')
            self.assertEqual(read_credential(path), key)

    def test_missing_duplicate_or_malformed_selected_key_has_stable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.env"
            for contents in ("OTHER=unrelated", "GEMINI_API_KEY=short", "GEMINI_API_KEY='broken",
                "GEMINI_API_KEY=first-synthetic-key\nGEMINI_API_KEY=second-synthetic-key"):
                path.write_text(contents)
                with self.assertRaises(TransportError) as caught:
                    read_credential(path)
                self.assertNotIn("synthetic-key", str(caught.exception))

    def test_pricing_positive_finite_valid_and_evidenced(self):
        validate_pricing(pricing(), model="gemini-3.8-flash", now=0)
        for name, value in (("usd_to_rub", "0"), ("input_usd_per_million", "NaN"),
            ("output_usd_per_million", "-1"), ("cached_input_usd_per_million", "1"),
            ("source", ""), ("valid_until", "2099-01-01")):
            bad = {**pricing(), name: value}
            with self.assertRaises(TransportError):
                validate_pricing(bad, now=0)
        with self.assertRaisesRegex(TransportError, "pricing_model_mismatch"):
            validate_pricing(pricing(), model="gemini-3.8-pro")

    def test_retry_after_invalid_delay_never_becomes_hidden_retry(self):
        self.assertEqual(retry_delay({"Retry-After": "300"}), 300)
        self.assertEqual(retry_delay({"Retry-After": "NaN"}), 0)
        self.assertEqual(retry_delay({"Retry-After": "garbage"}), 0)


if __name__ == "__main__":
    unittest.main()
