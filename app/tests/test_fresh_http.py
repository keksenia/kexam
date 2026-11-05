"""Local HTTP integration with artificial provider answers, never real API."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import server
from app.live_checking import FreshChecking
from app.tests.test_fresh_checking import fixture_policy, Fixture, condition
from app.tests.test_foundation import valid_form, image_data


class FreshHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.httpd=server.make_server(0,cls.root/"works")
        cls.httpd.fresh.close()
        cls.fixture=Fixture(condition())
        cls.httpd.fresh=FreshChecking(cls.httpd.store,policy=fixture_policy(),ledger=cls.root/"ledger",transport_factory=cls.fixture.factory)
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True);cls.thread.start();cls.port=cls.httpd.server_port
    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown();cls.httpd.server_close();cls.thread.join(2);cls.tmp.cleanup()
    def request(self,method,path,data=None,origin=True):
        con=http.client.HTTPConnection("127.0.0.1",self.port,timeout=10)
        try:
            headers={"Content-Type":"application/json"}
            if origin: headers["Origin"]=f"http://127.0.0.1:{self.port}"
            con.request(method,path,body=json.dumps(data) if data is not None else None,headers=headers)
            response=con.getresponse();return response.status,json.loads(response.read())
        finally:con.close()
    def test_upload_check_poll_history_original_manifest_and_default_model(self):
        status,available=self.request("GET","/api/checking")
        self.assertEqual(status,200);self.assertEqual(available["default_model"],"G")
        form=valid_form();form['task_text']='';form["task"]=[image_data()]
        status,saved=self.request("POST","/api/works",form);self.assertEqual(status,201)
        work=saved["work"]
        status,started=self.request("POST","/api/check",dict(work_id=work["id"],model="G",complete=True,single_task=True));self.assertEqual(status,202)
        self.httpd.fresh.pool.submit(lambda:None).result(timeout=15)
        status,job=self.request("GET","/api/checks/"+started["job"]["id"])
        self.assertEqual(status,200);self.assertEqual(job["state"],"completed",job)
        self.assertEqual(job["result"]["score"]["value"],2)
        self.assertEqual(self.request("GET","/api/works/"+work["id"])[1],work)
        latest=self.request("GET","/api/works/"+work["id"]+"/check")[1]
        self.assertEqual(latest["id"],job["id"])
        self.assertEqual(self.request("POST","/api/check",dict(work_id=work["id"],model="G",complete=True,single_task=True))[1]["reused"],True)

    def test_report_is_explicit_local_and_origin_protected(self):
        old=self.httpd.fresh
        with tempfile.TemporaryDirectory() as directory:
            fixture=Fixture(condition())
            service=FreshChecking(self.httpd.store,policy=fixture_policy(),ledger=Path(directory)/'ledger',transport_factory=fixture.factory)
            self.httpd.fresh=service
            try:
                form=valid_form();form['task_text']='';form['task']=[image_data()];form['solution']=[image_data()]
                status,saved=self.request('POST','/api/works',form);self.assertEqual(status,201)
                status,started=self.request('POST','/api/check',dict(work_id=saved['work']['id'],model='G',complete=True,single_task=True))
                self.assertEqual(status,202);service.pool.submit(lambda:None).result(timeout=15)
                job_id=started['job']['id'];path='/api/checks/'+job_id+'/prepare-report'
                requests=len(fixture.requests)
                self.assertEqual(self.request('POST',path,{},origin=False)[0],403)
                self.assertEqual(self.request('POST',path,{'score':0})[0],400)
                status,prepared=self.request('POST',path,{})
                self.assertEqual(status,200);self.assertTrue(prepared['accepted'])
                self.assertEqual(prepared['job']['result']['readiness'],dict(score='ready',feedback='ready'))
                self.assertEqual(len(fixture.requests),requests)
                self.assertTrue(self.request('POST',path,{})[1]['reused'])
            finally:
                service.close();self.httpd.fresh=old
    def test_two_remaining_calls_allow_http_continuation_and_exhausted_job_is_reused(self):
        from app.tests.test_fresh_checking import FreshTests
        from app.server import WorkStore
        old=self.httpd.fresh
        with tempfile.TemporaryDirectory() as directory:
            store=WorkStore(Path(directory)/"works")
            fixture=Fixture(condition(rhs="2"))
            service=FreshChecking(store,policy=fixture_policy(),ledger=Path(directory)/"ledger",transport_factory=fixture.factory)
            self.httpd.fresh=service
            try:
                work=store.save(valid_form())
                form=dict(work_id=work["id"],model="G",complete=True,single_task=True)
                job=service.create(form,background=False)["job"]
                self.assertEqual(job["error_code"],"condition_suspicious")
                # Seed twelve unrelated, settled artificial calls: exactly two remain.
                self.store=store
                FreshTests.seed_settled_calls(self,service,fixture,12)
                self.assertFalse(service.availability()["paid_api_enabled"])
                status,response=self.request("POST","/api/checks/"+job["id"]+"/confirm-condition",
                    dict(literal=job["condition_versions"][-1]["solver_input"]["literal"],confirm_empty=True))
                self.assertEqual(status,202,response)
                service.pool.submit(lambda:None).result(timeout=15)
                self.assertEqual(service.get(job["id"])["state"],"completed")
                self.assertEqual(len(fixture.requests),2)
                status,reused=self.request("POST","/api/check",form)
                self.assertEqual(status,202,reused);self.assertTrue(reused["reused"])
                self.assertEqual(len(fixture.requests),2)
            finally:
                service.close();self.httpd.fresh=old

    def test_mutation_requires_local_origin_and_rejects_extra_transport_options(self):
        self.assertEqual(self.request("POST","/api/check",{},origin=False)[0],403)
        self.assertEqual(self.request("POST","/api/check",dict(work_id="unknown",model="G",complete=True,single_task=True,synthetic=True))[0],400)
        self.assertEqual(self.request("GET","/api/checks/../../secret")[0],404)
