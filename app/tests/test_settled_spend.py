"""User-selected actual-spend cutoff. Synthetic calls, no external API."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.call_guard import CallGuard, ProviderResponse
from app.guard_demo import fixture_request, fixture_policy, fixture_validator


def soft_policy(request):
    settings={k:v for k,v in request.payload.items() if k not in {"model","messages","max_tokens"}}
    rule=dict(stage=request.stage,provider=request.provider,model=request.payload["model"],versions=request.versions,
        settings=settings,max_request_bytes=8388608,max_output_tokens=128,evidence_ref="synthetic-explicit-user-spend-mode")
    return replace(fixture_policy([request]),ceilings={},budget_mode="settled_spend",dispatch_rules={"shape":rule})


class Transport:
    kind="fixture";single_attempt=True
    def __init__(self,cost="100"):self.cost=cost;self.calls=0
    def __call__(self,payload,**kwargs):
        self.calls+=1
        return ProviderResponse('{"text":"ok"}',usage={"completion_tokens":10},cost_kopecks=self.cost,model=payload["model"])


class SettledSpendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.request=fixture_request();self.policy=soft_policy(self.request)
        self.guard=CallGuard(Path(self.tmp.name),self.policy)
        self.block=patch("socket.socket.connect",side_effect=AssertionError("No network"));self.block.start();self.addCleanup(self.block.stop)
    def execute(self,label,cost):
        transport=Transport(cost)
        outcome=self.guard.execute(fixture_request(label),transport,fixture_validator)
        return outcome,transport
    def test_exact_limit_stops_next_call_and_survives_restart(self):
        self.assertEqual(self.execute("one","200")[0].status,"succeeded")
        self.assertEqual(self.execute("two","100")[0].status,"succeeded")
        outcome,transport=self.execute("three","1")
        self.assertEqual(outcome.status,"budget_limit");self.assertEqual(transport.calls,0)
        restored=CallGuard(Path(self.tmp.name),self.policy)
        self.assertEqual(restored.reserve(fixture_request("new")).status,"budget_limit")
    def test_last_call_may_cross_remaining_budget_then_stops(self):
        self.assertEqual(self.execute("one","290")[0].status,"succeeded")
        self.assertEqual(self.execute("two","50")[0].status,"succeeded")
        self.assertEqual(self.guard.snapshot()["cost"]["rub"],"3.4")
        outcome,transport=self.execute("three","50")
        self.assertEqual(outcome.status,"budget_limit");self.assertEqual(transport.calls,0)
    def test_unknown_cost_is_unknown_even_without_price_reserve_and_blocks_next(self):
        self.assertEqual(self.execute("one",None)[0].status,"cost_unknown")
        self.assertEqual(self.guard.snapshot()["cost"],dict(status="unknown",rub=None))
        outcome,transport=self.execute("two","1")
        self.assertEqual(outcome.status,"unresolved_spending");self.assertEqual(transport.calls,0)
    def test_only_one_in_flight_call_is_admitted(self):
        self.assertEqual(self.guard.reserve(self.request).status,"reserved")
        self.assertEqual(self.guard.reserve(fixture_request("two")).status,"request_in_flight")
    def test_request_shape_and_call_count_still_apply(self):
        self.assertEqual(self.guard.reserve(replace(self.request,payload={**self.request.payload,"model":"other"})).status,"billing_ceiling_unproven")
        for i in range(self.policy.max_total_calls):self.assertEqual(self.execute(str(i),"0")[0].status,"succeeded")
        outcome,transport=self.execute("extra","0")
        self.assertEqual(outcome.status,"total_call_limit");self.assertEqual(transport.calls,0)
    def test_live_mode_does_not_fabricate_verified_reasoning_bound(self):
        policy=replace(self.policy,mode="live");guard=CallGuard(Path(self.tmp.name)/"live",policy)
        transport=Transport("1");transport.kind="live"
        self.assertEqual(guard.execute(self.request,transport,fixture_validator).status,"succeeded")
        self.assertFalse(getattr(transport,"total_output_bound_verified",False))
