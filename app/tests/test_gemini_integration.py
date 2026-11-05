"""Native Gemini -> guarded persistence -> full M01, synthetic data only.

Every HTTPS operation uses an in-memory opener. No real credentials, pupil
images or production ledger are read, and socket entry points are blocked.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.call_guard import CallGuard, canonical, digest
from app.compact_reading import schema as reading_schema
from app.gemini_transport import GeminiTransport, build_native_request
from app.live_checking import FreshChecking
from app.pipeline import input_snapshot
from app.strict_reading import response_format as reading_response_format
from app.tests import test_fresh_checking as support
from app.tests.test_foundation import image_data, valid_form
from app.tests.test_gemini_transport import FakeHTTP, FakeOpener, pricing
from contracts.ege_v1 import validate as contract


SYNTHETIC_KEY = "AIza-synthetic-integration-key-only"
SNAPSHOT_MODEL = "gemini-3.8-flash-2026-10-01"


def native_policy():
    """Retain exact stage/schema admission with a temporary native ledger."""
    old = support.fixture_policy()
    rules = deepcopy(old.ceiling_rules)
    for rule in rules.values():
        rule.update(provider="google", model="gemini-3.8-flash")
        rule.pop("micro_rub")
    return replace(old, mode="live", budget_mode="settled_spend",
        ceiling_rules={}, dispatch_rules=rules)


class NativeFixture(support.Fixture):
    kind = "live"

    def __init__(self, condition, *, failure=None, failure_stage="solution_ocr"):
        super().__init__(condition)
        self.failure, self.failure_stage = failure, failure_stage
        self.posts, self.responses = [], []

    def factory(self, request):
        self.request = request
        return super().factory(request)

    def __call__(self, payload, *, timeout_seconds):
        response = super().__call__(payload, timeout_seconds=timeout_seconds)
        value = json.loads(response.content)
        envelope = dict(modelVersion=SNAPSHOT_MODEL, responseId="synthetic-" + self.stage,
            candidates=[dict(content=dict(role="model", parts=[{"text": canonical(value)}]),
                finishReason="STOP")],
            usageMetadata=dict(promptTokenCount=100, candidatesTokenCount=20,
                thoughtsTokenCount=10, cachedContentTokenCount=20,
                totalTokenCount=130, serviceTier="standard"))
        if self.stage == self.failure_stage:
            if self.failure == "missing_coverage":
                value["pages"][0]["coverage"].pop("status")
                envelope["candidates"][0]["content"]["parts"][0]["text"] = canonical(value)
            elif self.failure == "assessment_authority":
                value["checks"][0]["method"] = "exact_code"
                envelope["candidates"][0]["content"]["parts"][0]["text"] = canonical(value)
            elif self.failure == "truncated":
                envelope["candidates"] = [dict(finishReason="MAX_TOKENS")]
            elif self.failure == "unknown_usage":
                envelope["usageMetadata"].pop("totalTokenCount")
            elif self.failure == "model_mismatch":
                envelope["modelVersion"] = "gemini-3.8-pro"
        native = build_native_request(payload)
        opener = FakeOpener(FakeHTTP(canonical(envelope).encode()))
        parsed = GeminiTransport(SYNTHETIC_KEY,
            allowed_body_hashes=[digest(payload)],
            allowed_native_body_hashes=[self.request.context["native_request_sha256"]],
            pricing=pricing(), opener=opener)(payload, timeout_seconds=timeout_seconds)
        self.posts.extend(opener.calls)
        self.responses.append(parsed)
        if len(opener.calls) != 1 or opener.calls[0][0].data != canonical(native).encode():
            raise AssertionError("Native admitted body must be sent exactly once")
        return parsed


class GeminiIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Use the established synthetic upload setup without inheriting its
        # legacy Hubris tests or reading any existing work/ledger.
        self.fixture = support.FreshTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        for name in ("socket.create_connection", "socket.socket.connect_ex"):
            blocker = patch(name, side_effect=AssertionError("Network forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)
        secret = patch("app.live_checking.read_credential",
            side_effect=AssertionError("Real credential reads are forbidden"))
        secret.start()
        self.addCleanup(secret.stop)
        # Access/quota probing belongs to its own tests; these full-path
        # fixtures must never inspect an actual account or preflight report.
        access = patch("app.live_checking.access_problem", return_value=None, create=True)
        access.start()
        self.addCleanup(access.stop)

    def service(self, task_type="trigonometric_equation", **options):
        transport = NativeFixture(support.condition(task_type), **options)
        number = len(self.fixture.services)
        service = FreshChecking(self.fixture.store, policy=native_policy(),
            ledger=self.fixture.root / ("native-ledger-" + str(number)),
            transport_factory=transport.factory)
        # The synthetic approval is scoped solely to the temporary store.
        service.work_profiles = None
        service.upload_scope = None
        self.fixture.services.append(service)
        self.assertTrue(service.direct_gemini)
        return service, transport

    def run_work(self, service, *, task_type="trigonometric_equation", pages=1):
        return self.fixture.run_work(service, task_type, pages)

    def source(self, service, job):
        return json.loads((service.directory / job["id"] / "verified-input.json").read_text())

    def rows(self, service):
        with closing(sqlite3.connect((service.ledger / "spending.sqlite3").as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM calls ORDER BY created,id").fetchall()

    def assert_withheld(self, service, job):
        self.assertEqual(job["state"], "failed", job)
        self.assertEqual(job["result"]["score"]["status"], "withheld")
        self.assertIsNone(job["result"]["score"]["value"])
        self.assertEqual(contract.validate(job["result"], self.source(service, job)), [])

    def test_native_three_stages_persist_full_m01_and_estimate_provenance(self):
        service, transport = self.service()
        _, job = self.run_work(service, pages=2)
        self.assertEqual(job["state"], "completed", job)
        result = job["result"]
        self.assertEqual(contract.validate(result, self.source(service, job)), [])
        self.assertEqual(result["score"]["value"], 2)
        self.assertEqual([r.stage for r in transport.requests], ["statement_ocr", "solution_ocr", "grading"])
        self.assertEqual(len(transport.posts), 3)
        self.assertEqual(job["accounting"]["cost_basis"], "token_estimate")
        self.assertEqual(job["accounting"]["estimated_calls"], 3)
        self.assertEqual(job["accounting"]["provider_report_calls"], 0)
        self.assertEqual(job["accounting"]["tariff_refs"], [pricing()["tariff_ref"]])
        for call, row in zip(result["run"]["calls"], self.rows(service)):
            self.assertEqual(call["cost_basis"], "token_estimate")
            self.assertEqual(call["tariff_ref"], pricing()["tariff_ref"])
            self.assertEqual(call["usage"], dict(input_tokens=100, output_tokens=30,
                reasoning_tokens=10, reasoning_in_output=True))
            saved = json.loads((service.ledger / row["response_ref"]).read_text())
            self.assertEqual(saved["envelope"]["cost_basis"], "token_estimate")
            metadata = saved["envelope"]["provider_metadata"]
            self.assertEqual(metadata["native_usage"]["totalTokenCount"], 130)
            self.assertEqual(metadata["accounting"]["tariff_ref"], pricing()["tariff_ref"])

    def test_inequality_native_full_score_keeps_low_low_medium_and_photos(self):
        service, transport = self.service("inequality")
        _, job = self.run_work(service, task_type="inequality")
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(contract.validate(job["result"], self.source(service, job)), [])
        self.assertEqual(job["result"]["score"]["value"], 2)
        bodies = [json.loads(request.data) for request, _ in transport.posts]
        self.assertEqual([b["generationConfig"]["thinkingConfig"]["thinkingLevel"] for b in bodies],
            ["LOW", "LOW", "MEDIUM"])
        self.assertEqual([b["generationConfig"]["maxOutputTokens"] for b in bodies], [6000, 6000, 12000])
        self.assertEqual([sum("inlineData" in part for part in b["contents"][0]["parts"]) for b in bodies], [1, 2, 2])
        self.assertNotIn("reference_key", bodies[0]["contents"][0]["parts"][0]["text"])
        for body in bodies:
            self.assertEqual(body["serviceTier"], "standard")
            self.assertEqual(body["generationConfig"]["thinkingConfig"]["includeThoughts"], False)

    def test_saved_contract_hashes_bind_native_bytes_and_snapshot_models(self):
        service, transport = self.service()
        _, job = self.run_work(service)
        self.assertEqual(job["state"], "completed", job)
        for request, call, (post, _) in zip(transport.requests, job["result"]["run"]["calls"], transport.posts):
            native = json.loads(post.data)
            self.assertEqual(call["request_sha256"], digest(native))
            self.assertEqual(request.context["native_request_sha256"], digest(native))
            self.assertNotEqual(call["request_sha256"], digest(request.payload))
            self.assertEqual(call["settings"]["native_request_bytes"], len(post.data))
            self.assertEqual(call["settings"]["actual_model"], "gemini-3.8-flash")
            self.assertEqual(call["settings"]["native_model_version"], SNAPSHOT_MODEL)
            self.assertEqual(call["provider"], "google")
            self.assertEqual(call["model_id"], "gemini-3.8-flash")
            self.assertEqual(post.get_header("X-goog-api-key"), SYNTHETIC_KEY)
            self.assertNotIn(SYNTHETIC_KEY, post.full_url)
            self.assertNotIn(SYNTHETIC_KEY, post.data.decode())
        for path in service.ledger.rglob("*.json"):
            self.assertNotIn(SYNTHETIC_KEY, path.read_text())

    def test_completed_native_job_reopens_without_new_calls_or_rewriting(self):
        service, transport = self.service()
        work, job = self.run_work(service)
        before_rows = [dict(r) for r in self.rows(service)]
        path = service.directory / job["id"] / "result-1.json"
        before_result = path.read_bytes()
        again = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)
        self.assertTrue(again["reused"])
        self.assertEqual(again["job"]["id"], job["id"])
        self.assertEqual(len(transport.posts), 3)
        self.assertEqual([dict(r) for r in self.rows(service)], before_rows)
        self.assertEqual(path.read_bytes(), before_result)

    def test_missing_read_coverage_withholds_and_failed_reopen_never_retries(self):
        service, transport = self.service(failure="missing_coverage")
        work, job = self.run_work(service)
        self.assert_withheld(service, job)
        self.assertEqual(job["response_validation"]["code"], "strict_reading_response_invalid")
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(job["accounting"]["estimated_calls"], 2)
        again = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)
        self.assertTrue(again["reused"])
        self.assertEqual(again["job"]["id"], job["id"])
        self.assertEqual(len(transport.posts), 2)

    def test_assessment_forged_authority_preserves_reading_and_withholds(self):
        service, transport = self.service(failure="assessment_authority", failure_stage="grading")
        _, job = self.run_work(service)
        self.assert_withheld(service, job)
        self.assertEqual(job["response_validation"]["stage"], "grading")
        self.assertEqual(len(transport.posts), 3)
        self.assertEqual(job["accounting"]["estimated_calls"], 3)
        saved_reading = json.loads((service.directory / job["id"] / "reading.json").read_text())
        self.assertTrue(saved_reading["units"])
        self.assertFalse((service.directory / job["id"] / "assessment-core.json").exists())

    def test_native_read_truncation_is_charged_estimate_without_assessment(self):
        service, transport = self.service(failure="truncated")
        _, job = self.run_work(service)
        self.assert_withheld(service, job)
        self.assertEqual(job["error_code"], "truncated_response")
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(job["accounting"]["cost_basis"], "token_estimate")
        self.assertEqual(job["accounting"]["estimated_calls"], 2)
        saved_call = job["result"]["run"]["calls"][-1]
        self.assertEqual(saved_call["status"], "failed")
        self.assertIsNotNone(saved_call["cost_rub"])

    def test_unverifiable_native_usage_blocks_next_stage_and_next_work(self):
        service, transport = self.service(failure="unknown_usage")
        _, job = self.run_work(service)
        self.assert_withheld(service, job)
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(job["accounting"]["cost_basis"], "unknown")
        self.assertEqual(len(job["accounting"]["unresolved"]), 1)
        self.assertEqual(service.availability()["reason"], "unresolved_spending")
        other = self.fixture.store.save(valid_form())
        denied = service.create(dict(work_id=other["id"], model="G", complete=True, single_task=True), background=False)
        self.assertFalse(denied["accepted"])
        self.assertEqual(denied["reason"], "unresolved_spending")
        self.assertEqual(len(transport.posts), 2)

    def test_native_model_mismatch_stops_without_grade_or_estimated_price(self):
        service, transport = self.service(failure="model_mismatch")
        _, job = self.run_work(service)
        self.assert_withheld(service, job)
        self.assertEqual(job["error_code"], "cost_unknown")
        self.assertEqual(self.rows(service)[-1]["failure_reason"], "model_mismatch")
        self.assertEqual(len(transport.posts), 2)
        call = job["result"]["run"]["calls"][-1]
        self.assertEqual(call["settings"]["actual_model"], "gemini-3.8-pro")
        self.assertEqual(call["cost_basis"], "unknown")
        self.assertIsNone(call["cost_rub"])

    def test_all_ten_read_page_counts_admit_exact_native_schema_before_calls(self):
        service, transport = self.service()
        for count in range(1, 11):
            with self.subTest(pages=count):
                form = valid_form()
                form['task_text']='';form["task"] = [image_data()]
                form["solution"] = [image_data() for _ in range(count)]
                work = self.fixture.store.save(form)
                source, guard, _ = input_snapshot(self.fixture.store, work["id"])
                service._preflight_assets(work["id"], "G", source, guard)
                manifest = [{k: p[k] for k in ("id", "asset_id", "sha256", "role")} for p in source["pages"]]
                request = service._reading_request(dict(work_id=work["id"], model="G", guard=guard), source, manifest)
                expected = reading_response_format(reading_schema(count))
                self.assertEqual(request.payload["response_format"], expected)
                admitted = CallGuard._dispatch_for(SimpleNamespace(policy=service.policy), request)
                self.assertIsNotNone(admitted)
                native = build_native_request(request.payload)
                self.assertEqual(native["generationConfig"]["responseFormat"]["text"]["schema"], expected["json_schema"]["schema"])
                self.assertEqual(request.context["native_request_sha256"], digest(native))
                self.assertEqual(sum("inlineData" in part for part in native["contents"][0]["parts"]), count + 1)
        self.assertEqual(transport.requests, [])
        self.assertFalse((service.ledger / "spending.sqlite3").exists())

    def test_changed_read_schema_policy_denies_before_any_native_request(self):
        service, transport = self.service()
        rules = deepcopy(service.policy.dispatch_rules)
        rules["solution_ocr-1"]["settings"]["response_format"] = reading_response_format(reading_schema(2))
        service.policy = replace(service.policy, dispatch_rules=rules)
        work = self.fixture.store.save(valid_form())
        denied = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)
        self.assertFalse(denied["accepted"])
        self.assertEqual(denied["reason"], "billing_ceiling_unproven")
        self.assertEqual(transport.posts, [])
        self.assertFalse((service.ledger / "spending.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
