"""Text decoding is explicit, billed once, and cannot bypass a JSON rule."""
from dataclasses import replace
import unittest
from app.call_guard import CallGuard, ProviderResponse
from app.guard_demo import fixture_request,fixture_policy,FixtureTransport
from app.tests import test_call_guard as support


class PlainTextGuardTests(unittest.TestCase):
    def setUp(self):
        self.support=support.GuardTests();self.support.setUp();self.addCleanup(self.support.doCleanups)
        old=fixture_request('text-solution')
        self.request=replace(old,payload={**old.payload,'response_format':{'type':'text'}})
        self.guard=CallGuard(self.support.root,fixture_policy([self.request]))

    def test_raw_text_round_trips_and_repeated_execution_does_not_pay(self):
        text='  '+r'\(x\) \frac{1}{2} \ne 0 "quote"'+'\nОтвет: ℝ. '
        class Transport(FixtureTransport):
            def __call__(self,payload,**kwargs):
                self.calls+=1
                return ProviderResponse(text,model=payload['model'],cost_kopecks='1')
        transport=Transport()
        first=self.guard.execute(self.request,transport,lambda s:{'solution':s},response_text=True)
        again=self.guard.execute(self.request,transport,lambda s:{'solution':s},response_text=True)
        self.assertEqual(first.status,'succeeded');self.assertEqual(first.value,{'solution':text})
        self.assertTrue(again.reused);self.assertEqual(transport.calls,1)

    def test_text_request_without_explicit_decoder_stops_before_payment(self):
        transport=FixtureTransport()
        self.assertEqual(self.guard.execute(self.request,transport,lambda s:s).status,'response_format_mismatch')
        self.assertEqual(transport.calls,0);self.assertEqual(self.guard.snapshot()['calls'],0)

    def test_json_request_cannot_select_text_decoder(self):
        request=fixture_request('json-stage');guard=CallGuard(self.support.root/'json',fixture_policy([request]))
        transport=FixtureTransport()
        self.assertEqual(guard.execute(request,transport,lambda s:s,response_text=True).status,'response_format_mismatch')
        self.assertEqual(transport.calls,0);self.assertEqual(guard.snapshot()['calls'],0)

    def test_output_mode_alone_cannot_bypass_admission(self):
        changed=replace(self.request,payload={**self.request.payload,'model':'unapproved-model'})
        transport=FixtureTransport()
        self.assertEqual(self.guard.execute(changed,transport,lambda s:s,response_text=True).status,'billing_ceiling_unproven')
        self.assertEqual(transport.calls,0)
