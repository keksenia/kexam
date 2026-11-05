"""M04 diagnostic API on loopback; all input is generated synthetic media."""
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import server
from app.diagnostic import fixture_form


class DiagnosticHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        try:
            with patch("app.live_checking.configured_policy",return_value=None):
                cls.httpd = server.make_server(0, Path(cls.tmp.name) / "works")
        except OSError:
            cls.tmp.cleanup()
            raise
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(2)
        cls.tmp.cleanup()

    def request(self, method, path, payload=None, **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            opts = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
            opts.update(headers)
            conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=opts)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_diagnostic_roundtrip_has_no_current_student_grade_and_check_stays_disabled(self):
        status, saved = self.request("POST", "/api/works", fixture_form("inequality"))
        self.assertEqual(status, 201)
        work = saved["work"]
        status, response = self.request("POST", "/api/diagnostics/runs", {"work_id": work["id"], "request_key": "http-demo"})
        self.assertEqual(status, 202)
        run_id = response["run"]["id"]
        # Synchronize through the worker queue, no arbitrary sleep or polling.
        self.httpd.pipeline.executor.submit(lambda: None).result(timeout=5)
        status, run = self.request("GET", "/api/diagnostics/runs/" + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(run["state"], "completed")
        self.assertTrue(run["diagnostic_only"])
        self.assertIsNone(run["result"]["score"]["value"])
        self.assertEqual(run["result"]["run"]["mode"], "demo")
        self.assertEqual(self.request("GET", "/api/works/" + work["id"])[1], work)
        self.assertEqual(self.request("POST", "/api/check", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/diagnostics/runs/" + run_id + "/cancel", {})[0], 200)

    def test_invalid_types_foreign_origin_and_remote_adapter_are_bounded_errors(self):
        work = self.httpd.store.save(fixture_form("inequality"))
        for value in ([], {}, False, 1, "wrong"):
            with self.subTest(value=value):
                status, data = self.request("POST", "/api/diagnostics/runs", {"work_id": work["id"], "retry_of": value})
                self.assertEqual(status, 409)
                self.assertEqual(data["code"], "invalid_retry_parent")
        for extra in ({"adapter": "network"}, {"synthetic": True}, {"provider": "hubris"}):
            self.assertEqual(self.request("POST", "/api/diagnostics/runs", dict(work_id=work["id"], **extra))[0], 400)
        self.assertEqual(self.request("POST", "/api/diagnostics/runs", {"work_id": work["id"]}, Origin="https://foreign.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/diagnostics/runs/../file")[0], 404)

    def test_database_failures_have_safe_http_error_without_private_details(self):
        with patch.object(self.httpd.pipeline.store, "list", side_effect=sqlite3.DatabaseError("PRIVATE DATABASE PATH")):
            status, body = self.request("GET", "/api/diagnostics/runs")
            self.assertEqual(status, 500)
            self.assertNotIn("PRIVATE", json.dumps(body))
        with patch.object(self.httpd.pipeline, "create", side_effect=sqlite3.DatabaseError("PRIVATE DATABASE PATH")):
            status, body = self.request("POST", "/api/diagnostics/runs", {"work_id": "E" + "a" * 32})
            self.assertEqual(status, 500)
            self.assertNotIn("PRIVATE", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
