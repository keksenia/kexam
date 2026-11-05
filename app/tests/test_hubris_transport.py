"""HTTP/accounting boundaries and guarded runner, with no real model calls."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.call_guard import Policy, ProviderResponse, canonical
from app.hubris_transport import HubrisTransport, NoRedirect, TransportError, parse_response, read_credential, retry_delay
from app.l04_runner import ExperimentRunner, billing_policy, load_plan, materialize, readiness, validator_for


class FakeHTTP(BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw); self.status=status; self.headers=headers or {}
    def getcode(self): return self.status


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response=response; self.error=error; self.calls=[]
    def open(self, request, timeout):
        self.calls.append((request,timeout))
        if self.error: raise self.error
        return self.response


def envelope(**updates):
    data=dict(id="fake-response",model="google/gemini-3.8-flash",choices=[dict(message=dict(content='{"pages":[]}'),finish_reason="stop")],
        usage=dict(prompt_tokens=100,completion_tokens=20,cost="10.25",completion_tokens_details=dict(reasoning_tokens=10)))
    data.update(updates)
    return json.dumps(data).encode()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        blocker=patch("socket.socket.connect",side_effect=AssertionError("Network forbidden"))
        blocker.start();self.addCleanup(blocker.stop)
        self.payload=dict(model="google/gemini-3.8-flash",max_tokens=6000,messages=[dict(role="user",content="synthetic only")])
        self.sha=hashlib.sha256(canonical(self.payload).encode()).hexdigest()
        self.key="sk-gw-"+"a"*32

    def transport(self, opener):
        return HubrisTransport(self.key,allowed_body_hashes=[self.sha],opener=opener)

    def test_exact_one_post_and_credential_stays_in_header(self):
        opener=FakeOpener(FakeHTTP(envelope()));transport=self.transport(opener)
        response=transport(self.payload,timeout_seconds=10)
        self.assertEqual(len(opener.calls),1)
        request,timeout=opener.calls[0]
        self.assertEqual(request.data,canonical(self.payload).encode())
        self.assertEqual(request.full_url,"https://api.hubris.pw/v1/chat/completions")
        self.assertEqual(request.get_method(),"POST")
        self.assertEqual(request.get_header("Authorization"),"Bearer "+self.key)
        self.assertNotIn(self.key,repr(transport)); self.assertNotIn(self.key,request.data.decode())
        self.assertEqual(response.cost_kopecks,"10.25")
        self.assertEqual(response.usage["completion_tokens"],20)

    def test_changed_body_never_opens_network(self):
        opener=FakeOpener();transport=self.transport(opener)
        with self.assertRaisesRegex(TransportError,"unapproved_body"):
            transport({**self.payload,"max_tokens":12000},timeout_seconds=10)
        self.assertEqual(opener.calls,[])

    def test_http_error_not_retried_and_invoice_preserved(self):
        error=HTTPError("https://api.hubris.pw/v1/chat/completions",503,"error",{"Retry-After":"7"},BytesIO(b'{"error":{"code":"upstream_error"}}'))
        opener=FakeOpener(error=error); response=self.transport(opener)(self.payload,timeout_seconds=10)
        self.assertEqual((response.http_status,response.cost_kopecks,response.retry_after_seconds),(503,"0",7))
        self.assertEqual(len(opener.calls),1)

    def test_timeout_is_unknown_charge_and_no_retry(self):
        opener=FakeOpener(error=TimeoutError("secret in exception should not escape"))
        with self.assertRaisesRegex(TransportError,"provider_transport_failed") as caught:
            self.transport(opener)(self.payload,timeout_seconds=10)
        self.assertNotIn("secret",str(caught.exception));self.assertEqual(len(opener.calls),1)

    def test_redirect_is_never_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,"redirect",{},"https://other.example/"))
        opener=FakeOpener(FakeHTTP(b"",302))
        with self.assertRaisesRegex(TransportError,"provider_envelope_invalid"):
            self.transport(opener)(self.payload,timeout_seconds=10)
        self.assertEqual(len(opener.calls),1)

    def test_float_kopecks_remain_exact(self):
        raw=envelope().replace(b'"10.25"',b'10.250001')
        self.assertEqual(parse_response(raw).cost_kopecks,"10.250001")

    def test_missing_cost_is_not_zero(self):
        usage=dict(prompt_tokens=1,completion_tokens=1)
        self.assertIsNone(parse_response(envelope(usage=usage)).cost_kopecks)
        self.assertIsNone(parse_response(b'{}',status=500).cost_kopecks)
        self.assertIsNone(parse_response(b'{}',status=429).cost_kopecks)

    def test_missing_invalid_or_double_counted_usage_is_rejected(self):
        for usage in (None,dict(prompt_tokens=True,completion_tokens=20,cost=1),
            dict(prompt_tokens=1,completion_tokens=10,cost=1,completion_tokens_details=dict(reasoning_tokens=11))):
            self.assertEqual(parse_response(envelope(usage=usage)).finish_reason,"invalid_usage")

    def test_refusal_truncation_and_multiple_choices_not_accepted(self):
        for choice,expected in [(dict(message=dict(content="",refusal="no"),finish_reason="stop"),"refusal"),
            (dict(message=dict(content="{}"),finish_reason="length"),"length")]:
            self.assertEqual(parse_response(envelope(choices=[choice])).finish_reason,expected)
        self.assertEqual(parse_response(envelope(choices=[{},{}])).finish_reason,"invalid_envelope")

    def test_reasoning_exhaustion_preserves_length_and_cost_with_null_content(self):
        usage=dict(prompt_tokens=5318,completion_tokens=6000,cost="67",
                   completion_tokens_details=dict(reasoning_tokens=6000))
        response=parse_response(envelope(usage=usage,choices=[dict(message=dict(content=None),finish_reason="length")]))
        self.assertEqual(response.finish_reason,"length")
        self.assertEqual(response.content,"")
        self.assertEqual(response.cost_kopecks,"67")
        self.assertEqual(response.usage["completion_tokens_details"]["reasoning_tokens"],6000)

    def test_duplicate_keys_nan_and_non_json_fail(self):
        for raw in (b'{"usage":{},"usage":{}}',b'{"n":NaN}',b'<html>error</html>'):
            with self.assertRaisesRegex(TransportError,"provider_envelope_invalid"): parse_response(raw)

    def test_retry_after_long_delay_preserved(self):
        self.assertEqual(retry_delay({"Retry-After":"300"}),300)
        self.assertEqual(retry_delay({"Retry-After":"garbage"}),0)
        self.assertEqual(retry_delay({"Retry-After":"NaN"}),0)

    def test_envelope_size_stops_without_parse_or_retry(self):
        opener=FakeOpener(FakeHTTP(b"x"*200))
        with patch("app.hubris_transport.MAX_ENVELOPE_BYTES",100):
            with self.assertRaisesRegex(TransportError,"provider_envelope_too_large"):
                self.transport(opener)(self.payload,timeout_seconds=10)
        self.assertEqual(len(opener.calls),1)

    def test_credential_host_and_format_checked_without_leaking(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{},clear=True):
            path=Path(tmp)/"config";path.write_text("LLM_BASE_URL=https://api.hubris.pw/v1\nLLM_API_KEY="+self.key)
            self.assertEqual(read_credential(path),self.key)
            path.write_text("LLM_BASE_URL=https://other.example\nLLM_API_KEY="+self.key)
            with self.assertRaisesRegex(TransportError,"credential_host_mismatch"):read_credential(path)


class FakeStageTransport:
    kind="fixture";single_attempt=True
    def __init__(self,status=200,cost="10.25"):
        self.status=status;self.cost=cost;self.calls=0
    def __call__(self,payload,*,timeout_seconds):
        self.calls+=1
        content=canonical(dict(pages=[dict(units=[],interpretations=[],features=[],
            coverage=dict(page_id="solution-1",status="partial",reason="Synthetic transport test, no OCR.",
                score_scope="unresolved",score_scope_reason="No actual reading."))]))
        return ProviderResponse(content,model=payload["model"],response_id="synthetic",usage=dict(prompt_tokens=10,completion_tokens=20),
            cost_kopecks=self.cost,finish_reason="stop",http_status=self.status)


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.dispatch,cls.requests=load_plan()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.dispatch=deepcopy(self.__class__.dispatch);self.dispatch["common_ledger"]=str(Path(self.tmp.name)/"ledger")
        self.transport=FakeStageTransport()
        self.policy=Policy(**{**self.dispatch["policy_template"],"enabled":True,"mode":"fixture","expires_at":time.time()+300,
            "ceilings":{r.fingerprint:dict(micro_rub=1_000_000,evidence_ref="SYNTHETIC-NO-MONEY") for r in self.requests.values()}})
        self.runner=ExperimentRunner(self.dispatch,self.requests,self.policy,self.transport)
        self.id=next(r["id"] for r in self.dispatch["requests"] if r["stage"]=="reading" and r["case_id"]=="L03-13-01")
        block=patch("socket.socket.connect",side_effect=AssertionError("Network forbidden"));block.start();self.addCleanup(block.stop)

    def test_closed_billing_evidence_and_ready_status(self):
        with self.assertRaisesRegex(ValueError,"provider_billing_bounds_unverified"):billing_policy(self.dispatch,{})
        self.assertFalse(readiness()["launch_ready"])

    def test_validator_checks_full_m01_and_does_not_mutate_producer_output(self):
        data=json.loads(self.transport(self.requests[self.id].payload,timeout_seconds=1).content)
        before=deepcopy(data)
        self.assertEqual(validator_for(self.runner.rows[self.id])(data),before)
        self.assertEqual(data,before)
        with self.assertRaises(ValueError):validator_for(self.runner.rows[self.id])({"pages":[]})

    def test_deduplication_prevents_second_send(self):
        first=self.runner.execute(self.id);second=self.runner.execute(self.id)
        self.assertEqual(first["status"],"succeeded");self.assertTrue(second["reused"])
        self.assertEqual(self.transport.calls,1)
        self.assertEqual(first["accounting"]["cost"]["rub"],"0.1025")
        self.assertEqual(self.runner.guard.contract_calls(),[])

    def test_unknown_cost_blocks_next_request_and_retry(self):
        self.transport.cost=None
        self.assertEqual(self.runner.execute(self.id)["status"],"cost_unknown")
        self.assertEqual(self.runner.execute(self.id,retry=True)["status"],"experiment_retry_forbidden")
        other=next(i for i in self.requests if i!=self.id)
        self.assertEqual(self.runner.execute(other)["status"],"unresolved_spending")
        self.assertEqual(self.transport.calls,1)

    def test_four_retries_are_separate_limit_with_skipped_primaries(self):
        self.transport.status=503;self.transport.cost="0"
        ids=list(self.requests)[:5]
        with patch("app.l04_runner.time.sleep"):
            for item in ids[:4]:
                self.assertEqual(self.runner.execute(item)["status"],"retryable_http_error")
                self.assertEqual(self.runner.execute(item,retry=True)["status"],"retryable_http_error")
            self.assertEqual(self.runner.execute(ids[4])["status"],"retryable_http_error")
            self.assertEqual(self.runner.execute(ids[4],retry=True)["status"],"retry_reserve_exhausted")
        self.assertEqual(self.transport.calls,9)

    def test_invalid_json_cannot_consume_retry_reserve(self):
        with patch("app.l04_runner.validator_for",return_value=lambda _: (_ for _ in ()).throw(ValueError("reject"))):
            self.assertEqual(self.runner.execute(self.id)["status"],"invalid_response")
        self.assertEqual(self.runner.execute(self.id,retry=True)["status"],"experiment_retry_forbidden")

    def test_unknown_plan_id_or_changed_budget_rejected(self):
        with self.assertRaisesRegex(ValueError,"request_not_in_approved_plan"):self.runner.execute("unlisted-probe")
        with self.assertRaisesRegex(ValueError,"experiment_policy_mismatch"):
            ExperimentRunner(self.dispatch,self.requests,replace(self.policy,budget_micro_rub=self.policy.budget_micro_rub+1_000_000),self.transport)

    def test_live_result_uses_actual_journal_not_offline_zero(self):
        # Simulated live interface ONLY, socket is blocked: verifies live result
        # export without claiming this test generated a real model response.
        self.transport.kind="live";self.transport.total_output_bound_verified=True
        runner=ExperimentRunner({**self.dispatch,"common_ledger":str(Path(self.tmp.name)/"simulated-live")},
            self.requests,replace(self.policy,mode="live"),self.transport)
        result=runner.execute(self.id)
        self.assertEqual(result["status"],"succeeded")
        saved=json.loads((runner.guard.root/result["result_ref"]).read_text())
        self.assertEqual(saved["run"]["mode"],"live")
        self.assertEqual(saved["run"]["cost"],dict(status="known",rub="0.1025"))
        self.assertEqual(len(saved["run"]["calls"]),1)
        self.assertEqual(saved["run"]["calls"][0]["usage"]["output_tokens"],20)
        self.assertEqual(saved["score"]["value"],None)
        duplicate=runner.execute(self.id)
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["result_ref"],result["result_ref"])
        self.assertEqual(self.transport.calls,1)
