"""Synthetic failures only: safe causes, unchanged settlement and no retries."""
from contextlib import closing
from dataclasses import replace
from io import BytesIO
import json
import socket
import sqlite3
import ssl
import threading
import time
import unittest
from urllib.error import HTTPError, URLError

from app import independent_assessment as assessment
from app.call_guard import CallGuard, ProviderResponse, canonical, digest
from app.deepseek_transport import DeepSeekTransport
from app.gemini_transport import GeminiTransport
from app.guard_demo import FixtureTransport, fixture_policy, fixture_request, fixture_validator
from app.tests import test_call_guard as support
from app.tests.test_gemini_transport import FakeHTTP, FakeOpener, pricing
from app.transport_diagnostics import TransportFailure, exception_detail

SECRET = 'PRIVATE-CREDENTIAL-AND-PUPIL-TEXT'


class ClassifierTests(unittest.TestCase):
    def test_nested_known_exceptions_are_classified_without_messages(self):
        cases=[(URLError(TimeoutError(SECRET)),'connection_timeout'),
               (URLError(ssl.SSLError(SECRET)),'tls_error'),
               (ConnectionResetError(SECRET),'connection_error'),
               (socket.gaierror(1,SECRET),'dns_error'),(ValueError(SECRET),'unexpected_error')]
        for error,category in cases:
            with self.subTest(category=category):
                value=exception_detail(error)
                self.assertEqual(value,dict(category=category,http_status=None))
                self.assertNotIn(SECRET,canonical(value))

    def test_unknown_class_name_str_and_url_strings_are_not_inspected(self):
        def forbidden(self):raise AssertionError('Exception text accessed')
        unknown=type(SECRET,(Exception,),{'__str__':forbidden,'__repr__':forbidden})()
        for error in (unknown,URLError('https://example.test/?key='+SECRET)):
            self.assertEqual(exception_detail(error)['category'],'unexpected_error')
        cycle=URLError('unused');cycle.reason=cycle
        self.assertEqual(exception_detail(cycle)['category'],'unexpected_error')


class GuardDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.support=support.GuardTests();self.support.setUp();self.addCleanup(self.support.doCleanups)
        self.root=self.support.root;self.request=self.support.request;self.other=fixture_request('new')
        self.policy=fixture_policy([self.request,self.other])

    def test_failure_cause_is_durable_reused_and_never_changes_unknown_spend(self):
        class Broken(FixtureTransport):
            def __call__(self,payload,**kwargs):
                self.calls+=1;raise URLError(TimeoutError(SECRET))
        guard=CallGuard(self.root,self.policy);transport=Broken()
        out=guard.execute(self.request,transport,fixture_validator)
        self.assertEqual(out.status,'cost_unknown');self.assertIsNone(out.value)
        self.assertEqual(out.diagnostic['category'],'connection_timeout')
        self.assertEqual(out.diagnostic['provider'],'synthetic-fixture')
        self.assertEqual(out.diagnostic['stage'],'other');self.assertGreaterEqual(out.diagnostic['latency_ms'],0)
        again=CallGuard(self.root,self.policy).execute(self.request,transport,fixture_validator)
        self.assertEqual(again.diagnostic,out.diagnostic);self.assertTrue(again.reused)
        self.assertEqual(transport.calls,1)
        self.assertEqual(guard.snapshot()['held_micro_rub'],1_000_000)
        self.assertEqual(guard.execute(self.request,transport,fixture_validator,retry=True).status,'retry_forbidden')
        self.assertEqual(guard.reserve(self.other).status,'unresolved_spending')
        for path in self.root.rglob('*'):
            if path.is_file():self.assertNotIn(SECRET.encode(),path.read_bytes())

    def test_http_error_status_is_saved_without_headers_or_error_text(self):
        error=HTTPError('https://example.test/'+SECRET,503,SECRET,{'Authorization':SECRET},BytesIO())
        self.addCleanup(error.close)
        class Broken(FixtureTransport):
            def __call__(self,payload,**kwargs):
                raise error
        out=CallGuard(self.root,self.policy).execute(self.request,Broken(),fixture_validator)
        self.assertEqual(out.status,'cost_unknown')
        self.assertEqual(out.diagnostic['category'],'http_error');self.assertEqual(out.diagnostic['http_status'],503)
        self.assertNotIn(SECRET,(self.root/'spending.sqlite3').read_bytes().decode(errors='ignore'))

    def test_paid_invalid_json_is_distinct_from_validator_failure(self):
        class InvalidJSON(FixtureTransport):
            def __call__(self,payload,**kwargs):return ProviderResponse('not JSON',cost_kopecks='1',model=payload['model'])
        for label,transport,validator in [('response_json',InvalidJSON(),fixture_validator),
                ('response_validation',FixtureTransport(),lambda value:(_ for _ in ()).throw(ValueError(SECRET)))]:
            guard=CallGuard(self.root/label,self.policy)
            out=guard.execute(self.request,transport,validator)
            self.assertEqual(out.status,'invalid_response');self.assertEqual(out.diagnostic['category'],label)
            self.assertEqual(out.diagnostic['http_status'],200)
            self.assertGreater(guard.snapshot()['known_micro_rub'],0)

    def test_guard_deadline_and_late_failure_do_not_change_settlement_or_events(self):
        release=threading.Event();finished=threading.Event()
        class Late(FixtureTransport):
            def __call__(self,payload,**kwargs):
                release.wait(1)
                try:raise ConnectionResetError(SECRET)
                finally:finished.set()
        guard=CallGuard(self.root,replace(self.policy,timeout_seconds=.02))
        out=guard.execute(self.request,Late(),fixture_validator)
        self.assertEqual(out.diagnostic['category'],'guard_timeout')
        def state():
            with closing(sqlite3.connect(guard.db)) as con:
                return con.execute('SELECT * FROM calls').fetchall(),con.execute('SELECT * FROM events').fetchall()
        before=state();release.set();self.assertTrue(finished.wait(1));time.sleep(.01)
        self.assertEqual(state(),before);self.assertEqual(out.status,'cost_unknown')


class AdapterDiagnosticTests(unittest.TestCase):
    def deepseek(self,opener):
        request=assessment.request('solve','inequality','x>0',source_guard='synthetic')
        return DeepSeekTransport('synthetic-secret-123456',allowed_body_hashes=[digest(request.payload)],
            pricing=assessment.config()['pricing'],opener=opener),request.payload

    def gemini(self,opener):
        from app.tests.test_gemini_transport import NativeWireTests
        helper=NativeWireTests();helper.setUp();self.addCleanup(helper.doCleanups)
        return helper.transport(opener),helper.body

    def test_both_adapters_keep_nested_network_cause_without_retry_or_leak(self):
        for factory in (self.deepseek,self.gemini):
            for error,category in [(URLError(TimeoutError(SECRET)),'connection_timeout'),
                                   (ssl.SSLError(SECRET),'tls_error'),(ConnectionResetError(SECRET),'connection_error')]:
                with self.subTest(adapter=factory.__name__,category=category):
                    opener=FakeOpener(error=error);transport,payload=factory(opener)
                    with self.assertRaises(TransportFailure) as caught:transport(payload,timeout_seconds=10)
                    self.assertEqual(exception_detail(caught.exception)['category'],category)
                    self.assertNotIn(SECRET,str(caught.exception));self.assertEqual(len(opener.calls),1)

    def test_status_received_before_read_failure_is_retained_by_both_adapters(self):
        class BrokenBody(FakeHTTP):
            def read(self,*args):raise ConnectionResetError(SECRET)
        for factory in (self.deepseek,self.gemini):
            opener=FakeOpener(BrokenBody(b'',503));transport,payload=factory(opener)
            with self.assertRaises(TransportFailure) as caught:transport(payload,timeout_seconds=10)
            self.assertEqual(exception_detail(caught.exception),dict(category='connection_error',http_status=503))
            self.assertEqual(len(opener.calls),1)


class JobDiagnosticTests(unittest.TestCase):
    def test_work_provider_stage_and_safe_message_reach_saved_job(self):
        from app.tests.test_independent_assessment import IndependentTests
        helper=IndependentTests();helper.setUp();self.addCleanup(helper.doCleanups)
        service,fixture=helper.service()
        class Broken:
            kind='live';single_attempt=True
            def __call__(self,*args,**kwargs):raise URLError(TimeoutError(SECRET))
        calls=[]
        def factory(request):calls.append(request);return Broken()
        service.transport_factory=factory
        job=helper.run_work(service)['job']
        self.assertEqual(job['state'],'failed');self.assertEqual(job['error_code'],'cost_unknown')
        detail=job['failure_diagnostic']
        self.assertEqual(detail['work_id'],job['work_id']);self.assertEqual(detail['check_id'],job['id'])
        self.assertEqual(detail['provider'],'deepseek');self.assertEqual(detail['operation'],'solve')
        self.assertIn('Тип сбоя:',job['message']);self.assertNotIn(SECRET,canonical(job))
        self.assertEqual(len(calls),1);self.assertIsNone(job['result_ref'])
