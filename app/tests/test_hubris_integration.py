"""Observed provider packaging regressions, synthetic inputs and no network."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import unittest

from app.call_guard import canonical
from app.compact_reading import schema
from app.hubris_transport import HubrisTransport, parse_response
from app.strict_reading import decode_reading, reading_diagnostics
from app.tests.test_compact_reading import page
from app.tests.test_hubris_transport import envelope, FakeHTTP, FakeOpener
from app.tests import test_fresh_checking as support


class PackagingTests(unittest.TestCase):
    def test_observed_aliases_preserve_content_direction_and_original(self):
        raw = dict(pages=[page()], features={}, links=[
            dict(source="1:1", target="1:2", type="continues")])
        before = deepcopy(raw)
        decoded = decode_reading(raw, schema(1))
        self.assertEqual(raw, before)
        self.assertEqual(decoded["pages"], raw["pages"])
        self.assertEqual(decoded["features"], [])
        self.assertEqual(decoded["links"], [dict(**{"from": "1:1", "to": "1:2", "kind": "continues"})])
        diagnostics = reading_diagnostics(raw, schema(1))
        self.assertFalse(diagnostics["strict_wire_compliant"])
        self.assertEqual(len(diagnostics["packaging_normalizations"]), 2)
        self.assertNotIn("1:2", canonical(diagnostics))

    def test_nonempty_objects_and_other_wrong_containers_are_not_erased(self):
        for key, value in (("features", {"circle": True}), ("features", "circle"),
                           ("features", [1]), ("links", {}), ("interpretations", {})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                decode_reading(dict(pages=[page()], **{key: value}), schema(1))

    def test_partial_mixed_or_invalid_aliases_remain_invalid(self):
        correct = dict(source="1:1", target="1:2", type="continues")
        for link in ({k: v for k, v in correct.items() if k != "target"},
                     {**correct, "from": "1:3"}, {**correct, "type": "deduced"},
                     {**correct, "source": "unknown"}, {**correct, "target": None}):
            with self.subTest(link=link), self.assertRaises(ValueError):
                decode_reading(dict(pages=[page()], links=[link]), schema(1))

    def test_packaging_never_fills_missing_coverage_or_changes_clear_null(self):
        for field in ("status", "reason", "score_scope", "score_scope_reason"):
            raw = dict(pages=[page()], features={})
            raw["pages"][0]["coverage"].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_reading(raw, schema(1))
        raw = dict(pages=[page()], features={})
        raw["pages"][0]["entries"][0]["text"] = None
        with self.assertRaisesRegex(ValueError, "compact_reading_schema"):
            decode_reading(raw, schema(1))

    def test_null_refusal_preserves_explicit_signal_and_invoice(self):
        response = parse_response(envelope(choices=[dict(
            message=dict(content=None, refusal="Synthetic refusal"), finish_reason="stop")]))
        self.assertEqual(response.finish_reason, "refusal")
        self.assertEqual(response.provider_finish_reason, "refusal")
        self.assertEqual(response.content, "")
        self.assertEqual(response.cost_kopecks, "10.25")


class FullPathTests(unittest.TestCase):
    def run_fixture(self, *, malformed=False):
        fixture = support.FreshTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        service, _ = fixture.service()

        class HubrisShape(support.Fixture):
            def __call__(self, payload, **kwargs):
                response = super().__call__(payload, **kwargs)
                if self.stage == "solution_ocr":
                    value = json.loads(response.content)
                    value["features"] = {}
                    value["links"] = [dict(source="2:1", target="1:1", type="continues")]
                    if malformed:
                        value["pages"][0]["coverage"].pop("status")
                    response = replace(response, content=canonical(value))
                # Exercise the actual serialized HTTPS adapter and envelope
                # parser too, through an in-memory opener with a fake key.
                raw = envelope(model=response.model, usage={**response.usage, "cost": response.cost_kopecks},
                               choices=[dict(message=dict(content=response.content), finish_reason="stop")])
                opener = FakeOpener(FakeHTTP(raw))
                digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
                parsed = HubrisTransport("sk-gw-synthetic-test-key", allowed_body_hashes=[digest],
                                         opener=opener)(payload, timeout_seconds=3)
                if len(opener.calls) != 1 or opener.calls[0][0].data != canonical(payload).encode():
                    raise AssertionError("The guarded body must be sent exactly once")
                return parsed

        transport = HubrisShape(support.condition())
        service.transport_factory = transport.factory
        work, job = fixture.run_work(service, pages=2)
        return fixture, service, transport, work, job

    def test_alias_response_reaches_assessment_and_persisted_m01_without_retry(self):
        fixture, service, transport, work, job = self.run_fixture()
        self.assertEqual(job["state"], "completed")
        self.assertEqual([r.stage for r in transport.requests], ["statement_ocr", "solution_ocr", "grading"])
        self.assertEqual(job["accounting"]["calls"], 3)
        self.assertEqual(len(job["reading_format_diagnostics"]["packaging_normalizations"]), 2)
        self.assertFalse(job["reading_format_diagnostics"]["strict_wire_compliant"])
        candidate = json.loads((service.directory / job["id"] / "reading.json").read_text())
        self.assertTrue(candidate["relations"])
        again = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)
        self.assertTrue(again["reused"])
        self.assertEqual(len(transport.requests), 3)

    def test_missing_content_stops_before_assessment_without_retry(self):
        _, service, transport, _, job = self.run_fixture(malformed=True)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["response_validation"]["code"], "strict_reading_response_invalid")
        self.assertEqual(len(transport.requests), 2)
        result = json.loads((service.directory / job["id"] / job["result_ref"]).read_text())
        self.assertEqual(result["score"]["status"], "withheld")
        self.assertIsNone(result["score"]["value"])
