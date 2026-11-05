"""Local EGEChecker foundation with separate offline diagnostic orchestration.

HTTP/upload workflow adapted from HomeworkChecker app.py; see planning/m16.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pipeline import Pipeline, PipelineError, RUN_ID
from app.live_checking import FreshChecking, JOB_ID

try:
    from .checking_service import CheckingService
except ImportError:  # Direct local launcher.
    from checking_service import CheckingService

ROOT = Path(__file__).resolve().parent
DEFAULT_STORAGE = ROOT.parent / "data_local" / "ege_checker" / "works"
MAX_IMAGE_BYTES = 35 * 1024 * 1024
MAX_PIXELS = 30_000_000
MAX_PAGES = 10
MAX_BODY = 2 * MAX_PAGES * ((MAX_IMAGE_BYTES + 2) // 3 * 4) + 1024 * 1024
WORK_ID = r"E[0-9a-f]{32}"
TASK_TYPES = {"trigonometric_equation", "inequality"}


class InputError(ValueError):
    pass


def decode_image(value):
    if not isinstance(value, str):
        raise InputError("Каждая страница должна быть изображением.")
    match = re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=\r\n]+)", value)
    if not match:
        raise InputError("Поддерживаются фотографии JPEG, PNG и WebP.")
    if len(match[1]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise InputError("Одна фотография должна занимать не более 35 МБ.")
    try:
        raw = base64.b64decode(match[1], validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise InputError("Одна фотография должна занимать не более 35 МБ.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                if im.format not in {"JPEG", "PNG", "WEBP"} or getattr(im, "n_frames", 1) != 1:
                    raise InputError("Нужно обычное изображение JPEG, PNG или WebP без анимации.")
                if im.width * im.height > MAX_PIXELS:
                    raise InputError("Фотография слишком большая: предел 30 мегапикселей.")
                im.load()
                oriented = ImageOps.exif_transpose(im).convert("RGBA")
                # White background for transparent scans, original resolution, no metadata.
                clean = Image.new("RGB", oriented.size, "white")
                clean.paste(oriented, mask=oriented.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, format="PNG")
                return output.getvalue(), clean.size
    except InputError:
        raise
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise InputError("Не удалось прочитать фотографию. Выберите исправный JPEG, PNG или WebP.") from exc


class WorkStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def get(self, work_id):
        if not re.fullmatch(WORK_ID, work_id):
            raise FileNotFoundError
        path = self.root / work_id / "work.json"
        # Only directories published by save() have a manifest.
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("id") != work_id
                or not isinstance(record.get("created_at"), str)
                or not isinstance(record.get("task_text"), str)
                or not isinstance(record.get("task_type"), str)
                or record["task_type"] not in TASK_TYPES
                or record.get("exam_year") != 2026
                or record.get("state") != "saved"
                or "assessment" not in record or record["assessment"] is not None):
            raise ValueError("Invalid saved work manifest")
        for role in ("solution", "task"):
            pages = record.get(role)
            if not isinstance(pages, list) or (role == "solution" and not pages):
                raise ValueError("Invalid saved pages")
            for index, page in enumerate(pages, 1):
                if (not isinstance(page, dict) or page.get("page") != index
                        or page.get("url") != f"/media/{work_id}/{role}_{index}.png"
                        or type(page.get("width")) is not int or page["width"] <= 0
                        or type(page.get("height")) is not int or page["height"] <= 0):
                    raise ValueError("Invalid saved page")
        return record

    def list(self):
        works = []
        if self.root.exists():
            for path in self.root.glob("E*/work.json"):
                if re.fullmatch(WORK_ID, path.parent.name):
                    works.append(self.get(path.parent.name))
        return sorted(works, key=lambda w: (w["created_at"], w["id"]), reverse=True)

    def save(self, form):
        if not isinstance(form, dict):
            raise InputError("Ожидалась форма с условием и фотографиями.")
        if form.get("redacted") is not True:
            raise InputError("Подтвердите, что имена закрашены или отсутствуют.")
        if not isinstance(form.get("task_type"), str) or form["task_type"] not in TASK_TYPES:
            raise InputError("Выберите уравнение №13 или неравенство №15.")
        if type(form.get("exam_year")) is not int or form["exam_year"] != 2026:
            raise InputError("Сейчас поддерживается только нумерация ЕГЭ-2026.")
        task_text = form.get("task_text", "")
        if not isinstance(task_text, str) or len(task_text) > 20_000:
            raise InputError("Условие должно быть текстом длиной до 20 000 знаков.")
        task_text = task_text.strip()
        solution, task = form.get("solution"), form.get("task", [])
        if not isinstance(solution, list) or not solution or not isinstance(task, list):
            raise InputError("Добавьте хотя бы одну фотографию решения.")
        if len(solution) > MAX_PAGES or len(task) > MAX_PAGES:
            raise InputError("Можно сохранить до 10 страниц решения и до 10 фотографий условия.")
        if not task_text and not task:
            raise InputError("Добавьте полное условие текстом или фотографией, включая интервал и все пункты.")
        if task_text and task:
            raise InputError("Оставьте один способ ввода условия: текст или фотографии.")
        work_id = "E" + uuid.uuid4().hex
        record = {"schema_version": "m16-input-v1", "id": work_id,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "task_type": form["task_type"], "exam_year": form["exam_year"],
                  "task_text": task_text, "redaction_confirmed_by_user": True,
                  "state": "saved", "assessment": None, "solution": [], "task": []}
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / (".pending-" + work_id)
        staging.mkdir()
        try:
            for role, pages in (("solution", solution), ("task", task)):
                for index, data in enumerate(pages, 1):
                    image, (width, height) = decode_image(data)
                    filename = f"{role}_{index}.png"
                    (staging / filename).write_bytes(image)
                    record[role].append({"page": index, "url": f"/media/{work_id}/{filename}",
                                         "width": width, "height": height})
            (staging / "work.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.rename(self.root / work_id)
        except BaseException:
            # Only our unpublished partial upload is removed, never an existing work.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return record


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Avoid student text, images or file paths in logs.

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def send(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}",
                                            f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "Доступ только с этого компьютера."})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                return self.send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/health":
                fresh = self.server.fresh.availability()
                return self.send(200, {"stage": "foundation", "paid_api_enabled": fresh["paid_api_enabled"],
                                       "pipeline": "offline-diagnostic", "contract_version": "2.0.0",
                                       "task_types": sorted(TASK_TYPES),
                                       "fresh_checking": fresh})
            if path == "/api/checking":
                from urllib.parse import parse_qs,urlsplit
                query=parse_qs(urlsplit(self.path).query)
                return self.send(200, self.server.fresh.availability(work_id=query.get("work_id",[None])[0]))
            match = re.fullmatch(rf"/api/checks/({JOB_ID})/continuation", path)
            if match:
                if not hasattr(self.server.fresh,'continuation_plan'):
                    return self.send(200,dict(available=False,error='Продолжение этой схемы недоступно.'))
                return self.send(200,self.server.fresh.continuation_plan(match[1]))
            match = re.fullmatch(rf"/api/checks/({JOB_ID})", path)
            if match:
                return self.send(200, self.server.fresh.get(match[1]))
            match = re.fullmatch(rf"/api/works/({WORK_ID})/check", path)
            if match:
                self.server.store.get(match[1])
                return self.send(200, self.server.fresh.latest(match[1]))
            if path == "/api/diagnostics/runs":
                runs = self.server.pipeline.store.list()
                return self.send(200, [{key: r[key] for key in ("id", "work_id", "state", "created_at", "error_code", "diagnostic_only")} for r in runs])
            match = re.fullmatch(rf"/api/diagnostics/runs/({RUN_ID})", path)
            if match:
                return self.send(200, self.server.pipeline.view(match[1]))
            if path == "/api/works":
                return self.send(200, self.server.store.list())
            match = re.fullmatch(rf"/api/works/({WORK_ID})", path)
            if match:
                return self.send(200, self.server.store.get(match[1]))
            match = re.fullmatch(rf"/media/({WORK_ID})/((?:solution|task)_[1-9][0-9]*\.png)", path)
            if match:
                record = self.server.store.get(match[1])
                if path not in {p["url"] for p in record["solution"] + record["task"]}:
                    raise FileNotFoundError
                return self.send(200, (self.server.store.root / match[1] / match[2]).read_bytes(), "image/png")
            return self.send(404, {"error": "Страница не найдена."})
        except FileNotFoundError:
            self.send(404, {"error": "Работа или фотография не найдена."})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.send(500, {"error": "Не удалось прочитать сохранённые материалы. Проверьте локальную папку данных."})

    def do_POST(self):
        origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if not self.allowed_host() or self.headers.get("Origin") not in origins:
            return self.send(403, {"error": "Сохранение доступно только со страницы приложения."})
        confirm_condition = re.fullmatch(rf"/api/checks/({JOB_ID})/confirm-condition", self.path)
        prepare_report = re.fullmatch(rf"/api/checks/({JOB_ID})/prepare-report", self.path)
        continuation = re.fullmatch(rf"/api/checks/({JOB_ID})/continue", self.path)
        refresh_access = self.path == "/api/checking/refresh-access"
        fresh_check = self.path == "/api/check" or confirm_condition is not None or prepare_report is not None or continuation is not None or refresh_access
        cancel = re.fullmatch(rf"/api/diagnostics/runs/({RUN_ID})/cancel", self.path)
        diagnostic = self.path == "/api/diagnostics/runs" or cancel is not None
        if self.path != "/api/works" and not diagnostic and not fresh_check:
            return self.send(404, {"error": "Страница не найдена."})
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            return self.send(415, {"error": "Нужна форма в формате JSON."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise InputError("Пустая форма.")
            if length > MAX_BODY:
                return self.send(413, {"error": "Слишком большой набор фотографий: предел запроса 32 МБ. Сохраните меньше страниц."})
            if (diagnostic or fresh_check) and length > (262144 if continuation else 4096):
                return self.send(413, {"error": "Слишком большой диагностический запрос."})
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise InputError("Загрузка прервалась. Попробуйте ещё раз.")
            form = json.loads(raw)
            if fresh_check:
                if not isinstance(form, dict):
                    raise InputError("Нужен запрос проверки одной сохранённой работы.")
                if refresh_access:
                    if form: raise InputError("Для проверки доступа нужен пустой запрос.")
                    return self.send(200,self.server.fresh.refresh_provider_access())
                if prepare_report:
                    if form:raise InputError("Для подготовки сохранённых замечаний нужен пустой запрос.")
                    try:checked=self.server.fresh.prepare_saved_report(prepare_report[1])
                    except (ValueError,BlockingIOError):
                        return self.send(409,{"error":"Сохранённые замечания недоступны или работа изменилась. Новый модельный запрос не отправлен."})
                    return self.send(200,checked)
                if continuation:
                    if not hasattr(self.server.fresh,'continue_check'):
                        return self.send(409,{'error':'Продолжение этой схемы недоступно.'})
                    try: checked=self.server.fresh.continue_check(continuation[1],form)
                    except (ValueError,BlockingIOError) as error:
                        from app.check_continuation import ERRORS
                        return self.send(409,dict(accepted=False,reason=str(error),error=ERRORS.get(str(error),'Уточнение не принято или попытка изменилась. Запрос не отправлен.')))
                    return self.send(202 if checked['accepted'] else 409,checked)
                try:
                    checked = (self.server.fresh.confirm_empty_condition(confirm_condition[1], form)
                               if confirm_condition else self.server.fresh.create(form))
                except ValueError:
                    raise InputError("Выберите модель и подтвердите одну задачу с полным условием и всеми страницами решения.") from None
                return self.send(202 if checked["accepted"] else 409, checked)
            if diagnostic:
                if not isinstance(form, dict):
                    raise InputError("Ожидался диагностический запрос.")
                if cancel:
                    if form:
                        raise InputError("Для отмены нужен пустой объект.")
                    self.server.pipeline.store.cancel(cancel[1])
                    return self.send(200, {"run": self.server.pipeline.view(cancel[1])})
                if set(form) - {"work_id", "request_key", "retry_of"} or not isinstance(form.get("work_id"), str):
                    raise InputError("Укажите идентификатор сохранённой работы. Доступен только локальный diagnostic mock.")
                run = self.server.pipeline.create(form["work_id"], request_key=form.get("request_key"), parent_id=form.get("retry_of"))
                return self.send(202, {"run": run})
            # Serialize decoding to bound memory, but each saved work has its own ID.
            with self.server.upload_lock:
                work = self.server.store.save(form)
            self.send(201, {"work": work})
        except InputError as exc:
            self.send(400, {"error": str(exc)})
        except PipelineError as exc:
            self.send(409, {"error": "Диагностический запуск не создан.", "code": exc.code})
        except FileNotFoundError:
            self.send(404, {"error": "Работа или запуск не найдены."})
        except (ValueError, UnicodeError):
            self.send(400, {"error": "Форма повреждена. Обновите страницу и повторите сохранение."})
        except TimeoutError:
            self.send(408, {"error": "Загрузка прервалась по времени. Попробуйте ещё раз."})
        except (OSError, sqlite3.Error):
            self.send(500, {"error": "Не удалось сохранить файлы. Проверьте свободное место и доступ к папке данных."})


class LocalServer(ThreadingHTTPServer):
    def server_close(self):
        try:
            if hasattr(self, "pipeline"):
                self.pipeline.close()
            if hasattr(self, "fresh"):
                self.fresh.close()
        finally:
            super().server_close()


def make_server(port=8766, storage=DEFAULT_STORAGE):
    import threading
    server = LocalServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = WorkStore(storage)
    server.checking = CheckingService()
    server.upload_lock = threading.Lock()
    try:
        server.pipeline = Pipeline(server.store)
        from app.independent_checking import IndependentChecking
        server.fresh = IndependentChecking(server.store)
    except BaseException:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    args = parser.parse_args()
    with make_server(args.port, args.storage) as server:
        availability = server.fresh.availability()
        state = "проверка доступна" if availability["paid_api_enabled"] else "проверка подключена; платный запуск закрыт"
        print(f"EGEChecker: http://127.0.0.1:{server.server_port} — {state}.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
