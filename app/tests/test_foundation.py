"""Foundation acceptance tests: synthetic images, temporary storage, loopback HTTP.

Run from the project root: python3 -m unittest discover -s app/tests -v
No model, provider, real pupil material or external connection is used.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from app import server


def image_data(size=(19, 23), color="red", *, fmt="PNG", metadata=False):
    """Generate distinguishable pages without using personal materials."""
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    options = {}
    if metadata:
        exif = Image.Exif()
        exif[274] = 6  # Phone orientation: rotate 90 degrees clockwise.
        exif[315] = "SYNTHETIC PRIVATE AUTHOR"
        exif[37510] = b"SYNTHETIC PRIVATE COMMENT"
        options["exif"] = exif
        if fmt == "PNG":
            info = PngImagePlugin.PngInfo()
            info.add_text("Author", "SYNTHETIC PRIVATE AUTHOR")
            options["pnginfo"] = info
    image.save(output, format=fmt, **options)
    mime = "jpeg" if fmt == "JPEG" else fmt.lower()
    return f"data:image/{mime};base64," + base64.b64encode(output.getvalue()).decode("ascii")


def valid_form(task_type="trigonometric_equation"):
    return {"redacted": True, "exam_year": 2026, "task_type": task_type,
            "task_text": "  Синтетическое условие: решить на [-π; π].  ",
            "solution": [image_data()], "task": []}


class WorkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "works"
        self.store = server.WorkStore(self.root)

    def test_both_task_types_persist_after_store_recreation_without_assessment(self):
        saved = [self.store.save(valid_form(task_type)) for task_type in
                 ("trigonometric_equation", "inequality")]
        recreated = server.WorkStore(self.root)
        self.assertEqual(len(recreated.list()), 2)
        self.assertEqual(recreated.list()[0]["id"], saved[1]["id"])
        for work in saved:
            with self.subTest(task_type=work["task_type"]):
                self.assertEqual(recreated.get(work["id"]), work)
                self.assertEqual(work["state"], "saved")
                self.assertIsNone(work["assessment"])
                self.assertEqual(work["exam_year"], 2026)
                self.assertEqual(work["task_text"], "Синтетическое условие: решить на [-π; π].")
                self.assertTrue(work["redaction_confirmed_by_user"])

    def test_page_order_full_dimensions_and_separate_condition_images(self):
        form = valid_form()
        form["task_text"] = ""
        form["solution"] = [image_data((2101, 17), "red"), image_data((7, 31), "blue")]
        form["task"] = [image_data((13, 11), "green")]
        work = self.store.save(form)
        for role, expected in (("solution", [((2101, 17), (255, 0, 0)), ((7, 31), (0, 0, 255))]),
                               ("task", [((13, 11), (0, 128, 0))])):
            self.assertEqual([page["page"] for page in work[role]], list(range(1, len(expected) + 1)))
            for page, (size, color) in zip(work[role], expected):
                with Image.open(self.root / work["id"] / Path(page["url"]).name) as image:
                    self.assertEqual(image.size, size)
                    self.assertEqual((page["width"], page["height"]), size)
                    self.assertEqual(image.getpixel((0, 0)), color)

    def test_orientation_preserved_and_exif_and_png_text_removed(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                form = valid_form()
                form["solution"] = [image_data((19, 23), fmt=fmt, metadata=True)]
                work = self.store.save(form)
                data = (self.root / work["id"] / "solution_1.png").read_bytes()
                self.assertNotIn(b"SYNTHETIC PRIVATE", data)
                with Image.open(io.BytesIO(data)) as clean:
                    self.assertEqual(clean.size, (23, 19))
                    self.assertEqual(dict(clean.getexif()), {})
                    self.assertEqual(clean.info, {})
                    self.assertEqual(clean.format, "PNG")

    def test_two_condition_sources_rejected_before_creating_files(self):
        form = valid_form()
        form['task'] = [image_data()]
        with patch.object(server, 'decode_image') as decode:
            with self.assertRaisesRegex(server.InputError, 'один способ'):
                self.store.save(form)
            decode.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_whitespace_text_allows_photo_condition(self):
        form = valid_form()
        form.update(task_text=' \n ', task=[image_data()])
        work = self.store.save(form)
        self.assertEqual(work['task_text'], '')
        self.assertEqual(len(work['task']), 1)

    def test_transparent_scan_gets_white_background(self):
        source = Image.new("RGBA", (2, 2), (255, 0, 0, 0))
        source.putpixel((1, 1), (0, 0, 0, 255))
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        raw, size = server.decode_image(data)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(size, (2, 2))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(image.getpixel((1, 1)), (0, 0, 0))

    def test_invalid_second_image_rolls_back_only_its_work(self):
        previous = self.store.save(valid_form())
        form = valid_form("inequality")
        form["solution"].append("data:image/png;base64,bm90LWFuLWltYWdl")
        with self.assertRaises(server.InputError):
            self.store.save(form)
        self.assertEqual(self.store.list(), [previous])
        self.assertEqual([path.name for path in self.root.iterdir()], [previous["id"]])
        self.assertTrue((self.root / previous["id"] / "solution_1.png").is_file())

    def test_rejects_bad_form_types_and_missing_required_inputs(self):
        mutations = [
            {"redacted": False}, {"redacted": 1},
            {"task_type": "geometry"}, {"task_type": []}, {"task_type": {}},
            {"exam_year": 2025}, {"exam_year": "2026"}, {"exam_year": True},
            {"task_text": None}, {"task_text": "x" * 20_001},
            {"task_text": "   ", "task": []}, {"solution": []},
            {"solution": "not a list"}, {"task": None},
            {"solution": [None]}, {"solution": ["data:image/svg+xml;base64,AA=="]},
            {"solution": ["data:image/png;base64,invalid-base64!"]},
            {"solution": [image_data()] * 11},
        ]
        for change in mutations:
            form = valid_form()
            form.update(change)
            with self.subTest(change=repr(change)[:100]), self.assertRaises(server.InputError):
                self.store.save(form)
        for form in (None, [], "bad", 1):
            with self.subTest(form=form), self.assertRaises(server.InputError):
                self.store.save(form)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(list(self.root.glob(".pending-*")), [])

    def test_size_limits_reject_without_large_test_allocations(self):
        data = image_data((10, 10))
        with patch.object(server, "MAX_PIXELS", 99), self.assertRaises(server.InputError):
            server.decode_image(data)
        with patch.object(server, "MAX_IMAGE_BYTES", 10), self.assertRaises(server.InputError):
            server.decode_image(data)

    def test_animated_images_rejected(self):
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(
            buffer, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (4, 4), "blue")], duration=100, loop=0)
        data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        with self.assertRaises(server.InputError):
            server.decode_image(data)

    def test_bad_work_ids_cannot_traverse_storage(self):
        for work_id in ("../secret", "/etc/passwd", "E" + "a" * 32 + "/..", "Eshort"):
            with self.subTest(work_id=work_id), self.assertRaises(FileNotFoundError):
                self.store.get(work_id)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        try:
            with patch("app.live_checking.configured_policy",return_value=None), \
                    patch("app.independent_checking.load_policy",return_value=None):
                cls.httpd = server.make_server(0, Path(cls.temp.name) / "works")
        except OSError:
            cls.temp.cleanup()
            raise  # A forbidden loopback bind is an unverified test, not a pass.
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        merged = {"Content-Type": "application/json", "Origin": self.origin}
        if headers:
            for key, value in headers.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        try:
            conn.request(method, path, body=body, headers=merged)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_loopback_binding_and_health_are_honest(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        status, headers, raw = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(raw)
        self.assertEqual(health["stage"], "foundation")
        self.assertIs(health["paid_api_enabled"], False)
        self.assertEqual(set(health["task_types"]), server.TASK_TYPES)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_save_history_detail_media_and_restart_persistence(self):
        status, _, raw = self.request("POST", "/api/works", valid_form("inequality"))
        self.assertEqual(status, 201)
        work = json.loads(raw)["work"]
        status, _, raw = self.request("GET", "/api/works")
        self.assertEqual(status, 200)
        self.assertIn(work, json.loads(raw))
        # Recreate exactly the storage object a restarted process creates.
        self.httpd.store = server.WorkStore(self.httpd.store.root)
        status, _, raw = self.request("GET", f"/api/works/{work['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), work)
        status, headers, raw = self.request("GET", work["solution"][0]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (19, 23))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))

    def test_host_and_origin_guards_reject_foreign_websites(self):
        for method, path, headers in (
            ("GET", "/api/works", {"Host": "evil.example"}),
            ("GET", "/api/health", {"Host": f"evil.example:{self.port}"}),
            ("POST", "/api/works", {"Host": "evil.example"}),
            ("POST", "/api/works", {"Origin": "https://evil.example"}),
            ("POST", "/api/works", {"Origin": "null"}),
            ("POST", "/api/works", {"Origin": None}),
        ):
            with self.subTest(method=method, headers=headers):
                self.assertEqual(self.request(method, path, valid_form(), headers)[0], 403)
        self.assertEqual(self.request("GET", "/api/health",
                                    headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_traversal_and_unlisted_media_are_not_exposed(self):
        work = self.httpd.store.save(valid_form())
        for path in ("/../server.py", "/%2e%2e/server.py", "/data_local/",
                     f"/media/{work['id']}/../work.json", f"/media/{work['id']}/work.json",
                     f"/media/{work['id']}/solution_2.png", "/api/works/../secret",
                     f"/media/{work['id']}/%2e%2e%2fwork.json"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_check_endpoint_cannot_start_assessment_without_paid_admission(self):
        before = self.httpd.store.list()
        status, _, raw = self.request("POST", "/api/check", valid_form())
        self.assertEqual(status, 400)
        self.assertIn("error",json.loads(raw))
        self.assertEqual(before, self.httpd.store.list())

    def test_malformed_json_and_wrong_form_shape_return_errors(self):
        bodies = [b"{broken", b"\xff", b"[]", b"null", b"1", b'"plain text"']
        for body in bodies:
            with self.subTest(body=body):
                status, _, raw = self.request("POST", "/api/works", body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(raw))
        for task_type in ([], {}):
            form = valid_form()
            form["task_type"] = task_type
            with self.subTest(task_type=task_type):
                self.assertEqual(self.request("POST", "/api/works", form)[0], 400)

    def test_corrupt_saved_manifest_returns_bounded_error(self):
        original = self.httpd.store
        with tempfile.TemporaryDirectory() as directory:
            self.httpd.store = server.WorkStore(directory)
            try:
                work = self.httpd.store.save(valid_form())
                manifest = Path(directory) / work["id"] / "work.json"
                for corrupted in ("{broken", "null", "[]"):
                    manifest.write_text(corrupted, encoding="utf-8")
                    with self.subTest(corrupted=corrupted):
                        status, _, raw = self.request("GET", "/api/works")
                        self.assertEqual(status, 500)
                        self.assertIn("error", json.loads(raw))
                        self.assertNotIn(directory.encode(), raw)
            finally:
                self.httpd.store = original

    def test_content_type_length_and_transfer_encoding_limits(self):
        cases = [({"Content-Type": "text/plain"}, 415),
                 ({"Transfer-Encoding": "chunked"}, 415),
                 ({"Content-Length": "0"}, 400),
                 ({"Content-Length": "-1"}, 400),
                 ({"Content-Length": "bad"}, 400),
                 ({"Content-Length": str(server.MAX_BODY + 1)}, 413)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/works", b"{}", headers)[0], expected)


if __name__ == "__main__":
    unittest.main()
