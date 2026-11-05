"""Fresh upload → condition → independent solver → READ → ASSESS → M01.

No archive reading/key is imported. Paid admission is separate and closed unless
an approved dynamic M14 policy and documented provider bounds are supplied.
"""
from base64 import b64encode
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

from contracts.ege_v1 import validate as contract
from .call_guard import CallGuard, Policy, StageRequest, canonical, digest, parse_model_json, accounting_summary
from .gemini_transport import GeminiTransport, read_credential, build_native_request, endpoint_for_model, VERSION as TRANSPORT_VERSION
from .gemini_config import load_config as gemini_config, live_profile, identity as gemini_identity
from .gemini_preflight import access_problem, check_access, ACCESS_PATH
from .vpn_transport import vpn_problem
from .live_condition import (SCHEMA, INSTRUCTIONS, VERSION as CONDITION_VERSION, validate_condition, solve_condition, runtime_health,
    condition_sources, classify_condition, condition_input)
from .m05.runtime import build_request, rubric, freeze_identity, PromptError
from .live_boundary import materialize_reading_attempt, materialize_assessment, validate_candidate
from .pipeline import input_snapshot, base_result
from .provider_requests import build_hubris_request, build_gemini_request, BODY_LIMIT, model_image
from .assessment_projection import project_assessment
from .compact_reading import build_reading, materialize_compact_reading, schema as reading_schema, STRICT_INSTRUCTIONS, VERSION as READING_VERSION
from .strict_reading import response_format as reading_response_format, decode_reading, reading_diagnostics
from .compact_assessment import project_assessment as project_compact_assessment
from .assessment_core import (build_assessment, materialize_core as materialize_compact_assessment,
    materialize_report, report_from_core, project_core, VERSION as ASSESSMENT_VERSION, REPORT_VERSION, DEFERRED_MESSAGE)

ROOT = Path(__file__).resolve().parent.parent
VERSION = "fresh-upload-pipeline-1.14.0"
JOB_ID = r"C[0-9a-f]{32}"
LEDGER = ROOT / "data_local/ege_checker/spending/l04-2026-10-08"
POLICY_PATH = ROOT / "planning/m17/LIVE_UPLOAD_POLICY.json"
PROFILES = {"G": ("G-ocr", "G-checker"), "D": ("D-ocr", "D-checker")}


def active_approval():
    original_path = ROOT / "planning/l05/AUTHORIZATION.json"
    original = json.loads(original_path.read_text())
    amendment_path = ROOT / "planning/m17/FRESH_UPLOAD_AUTHORIZATION.json"
    if not amendment_path.exists(): return original
    amendment = json.loads(amendment_path.read_text())
    # A larger budget needs the user's explicit recorded increase; it never shrinks silently.
    increase = amendment.get("budget_increase")
    budget_ok = amendment.get("budget_rub") == original["budget_rub"] or (
        isinstance(increase, dict) and type(amendment.get("budget_rub")) is int
        and amendment["budget_rub"] > original["budget_rub"] and increase.get("budget_rub") == amendment["budget_rub"]
        and isinstance(increase.get("user_message"), str) and increase["user_message"].strip())
    if (amendment.get("original_authorization_sha256") != digest(original)
            or amendment.get("id") != original["id"]
            or not budget_ok
            or amendment.get("technical_retry_reserve") != original["technical_retry_reserve"]):
        raise ValueError("authorization_amendment_mismatch")
    return amendment
MESSAGES = {
    "strict_reading_response_invalid":"Провайдер вернул чтение, нарушающее обязательную схему ответа. Балл не выставлен; исходный ответ сохранён.",
    "invalid_response":"Модель вернула ответ в неподходящем формате. Проверка остановлена; балл ученику не выставлен.",
    "provider_billing_bounds_unverified":"Проверка пока недоступна: не подтверждены верхние расходы запросов с фото и рассуждениями.",
    "condition_uncertain":"Не удалось надёжно прочитать условие. Уточните указанный знак или загрузите более чёткое фото условия.",
    "multiple_tasks":"На фото несколько задач. Оставьте одну выбранную задачу и её решение.",
    "solver_unsupported":"Код пока не умеет надёжно решить это условие. Это не означает ошибку в задаче или решении ученика.",
    "condition_precheck_question":"Нужно подтвердить или исправить условие. Запросы DeepSeek ещё не выполнены.",
    "condition_suspicious":"После повторного чтения условие всё ещё требует уточнения. Проверьте показанное уравнение, знак и границы.",
    "condition_nonstandard":"Условие отличается от стандартного шаблона ЕГЭ-2026 для №13/№15 или содержит дополнительный текст. Проверка остановлена до решения: такое условие не решается как стандартное. Сверьте прочитанные блоки и выделенный текст с оригиналом.",
    "condition_task_mismatch":"Прочитанное условие относится к другому типу задачи, чем выбран при загрузке. Проверьте выбранный номер задачи и фото условия; балл не выставлялся.",
    "condition_source":"Модель сослалась на источник условия, которого нет среди загруженных фото и текста. Ответ сохранён, условие не решалось.",
    "condition_blocks":"Модель вернула блоки условия, не совпадающие с заявленным шаблоном задачи. Ответ сохранён, условие не решалось.",
    "condition_expression":"Модель записала выражение условия вне синтаксиса независимого решателя. Ответ сохранён, условие не решалось.",
    "input_changed":"Материалы изменились во время проверки. Запустите новую проверку актуальной версии.",
    "primary_call_limit":"Основные запросы согласованного опыта исчерпаны.",
    "check_scope_not_authorized":"Этот запуск не входит в согласованный пакет проверок.",
    "insufficient_stage_calls":"Для новой проверки нужны минимум 3 основных запроса: условие, чтение решения и оценка. Оставшихся основных запросов недостаточно; новая проверка не начата.",
    "truncated_response":"Модель исчерпала лимит ответа до завершения чтения. Балл не выставлен; неполный ответ не используется для оценки.",
    "terminal_response_error":"Провайдер не вернул завершённый содержательный ответ. Балл не выставлен; расходы и полученные данные сохранены.",
    "technical_error":"Проверка прервалась. Балл не выставлен; сохранённые этапы и расходы остаются в истории.",
}

MESSAGES.update({
    "vpn_required":"Для прямого Gemini API нужен включённый VPN. Подключите VPN и проверьте доступ; запрос к модели не отправлен.",
    "vpn_route_unavailable":"Не удалось подтвердить маршрут Google через VPN. Проверка остановлена до отправки.",
    "gemini_access_unverified":"Доступ к прямому Gemini API ещё не подтверждён. Проверьте доступ через включённый VPN; это бесплатно.",
    "gemini_location_unsupported":"Google отклоняет текущий регион подключения даже через VPN. Проверка остановлена до отправки; сохранённые результаты доступны.",
    "gemini_billing_required":"Google требует подключённый биллинг для этого ключа. Запрос к модели не отправлен.",
    "gemini_configuration_invalid":"Настройки прямого Gemini API требуют проверки. Запрос не отправлен.",
    "pricing_invalid":"Тариф прямого Gemini API требует проверки. Запрос не отправлен.",
    "pricing_expired":"Срок тарифного снимка Gemini API истёк. Обновите тариф перед новой проверкой.",
    "hubris_retired":"Маршрут Hubris отключён. Новые проверки используют прямой Gemini API.",
    "operation_cancelled":"Проверка остановлена при закрытии приложения. Полученные данные и фактические расходы сохранены; следующие запросы не отправлены.",
    "reading_coverage_missing":"Модель не сообщила полноту чтения одной из страниц. Считать страницу полностью прочитанной нельзя; нужен новый ответ чтения.",
    "compact_reading_schema":"Чтение решения получено в неполном формате. Исходный ответ сохранён, оценка не выполнялась.",
    "assessment_score_basis":"Модель предложила балл, который не подтверждается её оценкой обязательных требований. Балл не опубликован.",
    "assessment_student_proof_link":"Выполнение требования не связано с проверенными действующими записями ученика. Чтение сохранено; балл не опубликован.",
    "assessment_reading_alternatives":"Основание балла зависит от неподтверждённого варианта чтения. Чтение и ответ модели сохранены; балл не опубликован.",
    "assessment_core_schema":"Ответ модели не содержит обязательных данных для проверки основания балла. Балл не опубликован.",
    "assessment_report_score_doubt":"В ответе осталось существенное сомнение в балле или его основании. Балл не опубликован.",
    "assessment_score_criterion":"Модель указала балл и несовместимый с ним критерий. Балл не опубликован.",
    "assessment_required_element_skipped":"Модель объявила обязательное требование неприменимым. Чтение сохранено; основание балла требует проверки.",
    "assessment_inapplicable_evidence":"Модель сослалась для балла на неприменимое требование. Чтение сохранено; балл не опубликован.",
    "compact_assessment_schema":"Модель не передала все обязательные основания оценки. Чтение сохранено, балл не опубликован.",
    "full_m01_contract":"Ответ модели не прошёл проверку связности доказательств или существенных сомнений. Чтение сохранено, балл не опубликован.",
    "reading_needs_clarification":"Есть существенная неопределённость чтения или выбора задачи. Оценивание не запускалось; уточните указанный фрагмент.",
    "configuration_changed":"Приложение обновлено. Настройки запуска ещё не соответствуют этой версии; платные запросы остановлены.",
    "approval_expired_or_invalid":"Срок согласованного запуска истёк. Сохранённые результаты доступны, новые запросы не отправляются.",
    "paid_calls_disabled":"Платные запросы отключены в настройках запуска.",
    "policy_invalid":"Настройки проверки повреждены или неполны; новые запросы остановлены.",
    "ledger_policy_mismatch":"Настройки запуска не совпадают с общим журналом расходов. Проверка остановлена до согласованного обновления настроек.",
    "ledger_unavailable":"Не удалось прочитать журнал расходов. Новые запросы остановлены, чтобы не потерять учёт.",
    "authorized_checks_completed":"Для этой работы уже есть попытка проверки. Откройте сохранённый запуск; обновление приложения не запускает новую оплату.",
    "solution_page_limit":"Добавьте от 1 до 10 страниц решения одной задачи.",
    "model_profile_unreliable":"Этот профиль DeepSeek несколько раз исчерпал ответ на рассуждениях. Повтор с теми же настройками остановлен до контрольной проверки нового профиля.",
    "http_body_limit":"Фотографии вместе с инструкциями превышают предел модельного запроса. Обрежьте лишние поля, сохранив всю задачу; запрос не отправлен.",
    "image_size_limit":"Одна из фотографий превышает предел модельного запроса. Обрежьте лишние поля, сохранив записи; запрос не отправлен.",
    "cost_unknown":"Провайдер не сообщил достоверную стоимость запроса. Новые запросы остановлены до сверки расходов.",
    "unresolved_spending":"Есть запрос с незавершённым учётом расходов. Новые запросы остановлены до сверки.",
    "request_in_flight":"Предыдущий запрос ещё выполняется. Новые запросы остановлены до получения ответа и стоимости.",
    "budget_limit":"Достигнут общий лимит расходов. Следующий запрос не отправлен.",
    "total_call_limit":"Достигнут согласованный предел количества запросов.",
    "transport_timeout":"Ответ провайдера не получен вовремя. Автоматического повтора нет; расход требует сверки.",
    "transport_failure":"Соединение с провайдером прервалось. Автоматического повтора нет; расход требует сверки.",
    "retryable_http_error":"Провайдер временно отклонил запрос. Автоматического платного повтора нет; состояние и расход сохранены.",
    "output_bound_breached":"Ответ провайдера превысил согласованный предел. Проверка остановлена, счёт сохранён.",
    "credential_missing_or_invalid":"Ключ провайдера недоступен или некорректен. Платный запрос не отправлен.",
    "credential_host_mismatch":"Адрес провайдера не соответствует разрешённому. Платный запрос не отправлен.",
    "terminal_http_error":"Провайдер отклонил запрос. Балл не выставлен; причина и расходы сохранены.",
    "model_mismatch":"Ответ пришёл от модели, отличающейся от выбранной. Проверка остановлена.",
    "billing_ceiling_unproven":"Этот запрос не соответствует разрешённым настройкам запуска. Отправка остановлена.",
    "solver_runtime_version":"Версии библиотек независимого решателя не совпадают с проверенной конфигурацией. Запросы остановлены до исправления среды.",
    "solver_runtime_missing":"Среда независимого решателя недоступна. Проверка остановлена до отправки фотографий.",
})


def policy_problem():
    try:
        value=json.loads(POLICY_PATH.read_text())
        policy=Policy(**value["policy"])
        problem=policy.validate()
        if problem: return problem
        if value.get("configuration_sha256")!=digest(configuration_identity()): return "configuration_changed"
        return "policy_invalid"
    except (OSError,ValueError,TypeError,KeyError): return "policy_invalid"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (canonical(value)+"\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".write-",dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def configuration_identity():
    names = ("app/live_checking.py", "app/live_condition.py", "app/condition_worker.py",
        "app/condition-requirements.txt", "app/hubris_transport.py", "app/gemini_transport.py", "app/gemini_config.py", "app/gemini_preflight.py", "app/vpn_transport.py", "app/provider_requests.py",
        "app/call_guard.py", "app/pipeline.py", "app/m05/result_boundary.py", "app/live_boundary.py", "app/server.py", "app/static/index.html", "app/assessment_projection.py", "app/compact_reading.py", "app/compact_assessment.py", "app/assessment_core.py", "app/strict_reading.py")
    names += ("app/condition_precheck.py", "app/independent_assessment.py", "app/independent_checking.py", "app/w06_live_reserve.py", "planning/m17/W06_LIVE_RESERVE_AUTHORIZATION.json", "app/deepseek_transport.py", "planning/m17/DEEPSEEK_DIRECT_CONFIG.json", "planning/error_types_2026-10-10/catalog.json", "planning/m02/criteria.json", "planning/m03/rules.json")
    names += ("app/live_reserves.py", "planning/m17/live_reserves_2026-10-13/AUTHORIZATION.json", "app/independent_reading.py", "app/transport_diagnostics.py")
    names += ("app/check_continuation.py", "app/reading_trace.py")
    names += tuple(str(p.relative_to(ROOT)) for p in sorted((ROOT/"app/independent_prompts").glob("*.txt")))
    names += tuple(str(p.relative_to(ROOT)) for p in sorted((ROOT/"contracts/ege_v1").glob("*.py")))
    names += ("contracts/ege_v1/schema.json", "planning/l04/configurations.json", "planning/m17/GEMINI_DIRECT_CONFIG.json")
    return dict(version=VERSION, prompts=freeze_identity(), files={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in names})


def configured_policy():
    if not POLICY_PATH.exists(): return None
    try:
        return _validated_policy(json.loads(POLICY_PATH.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None  # Invalid/obsolete configuration closes admission, never enables it.


def _validated_policy(value):
    approval = active_approval()
    config = gemini_config()
    transmission = approval.get("transmission_authorization", {})
    if (transmission.get("provider") != "Google Gemini API"
            or transmission.get("models") != [config["model"]]
            or transmission.get("endpoint") != endpoint_for_model(config["model"])
            or value.get("gemini_configuration_sha256") != gemini_identity(config)):
        return None
    policy = Policy(**value["policy"])
    if any(r.get("provider") != "google" or r.get("model") != config["model"]
           for r in policy.dispatch_rules.values()): return None
    soft = policy.budget_mode == "settled_spend"
    if soft:
        override=json.loads((ROOT/"planning/l05/SETTLED_SPEND_OVERRIDE.json").read_text())
        if (override.get("approval_ref")!=approval["id"] or override.get("budget_rub")!=200
                or override.get("settled_spend_selected") is not True
                or value.get("override_sha256")!=digest(override)): return None
    elif (value.get("image_input_bound_verified") is not True or value.get("total_output_bound_verified") is not True
            or value.get("route_price_bound_verified") is not True): return None
    if (type(approval.get("primary_calls")) is not int or approval["primary_calls"] <= 0
            or approval.get("technical_retry_reserve") != 4
            or approval.get("max_total_calls") != approval["primary_calls"] + 4): return None
    if (policy.validate() or policy.mode != "live" or policy.approval_ref != approval["id"]
            or policy.budget_micro_rub != approval["budget_rub"]*1_000_000 or policy.max_total_calls != approval["max_total_calls"]
            or policy.max_attempts != 2 or not (policy.dispatch_rules if soft else policy.ceiling_rules) or policy.max_output_tokens > 12000
            or policy.max_response_bytes > 524288 or policy.timeout_seconds > 180): return None
    if value.get("configuration_sha256") != digest(configuration_identity()): return None
    return policy


class FreshChecking:
    def __init__(self, work_store, *, policy=None, ledger=LEDGER, transport_factory=None, solver=solve_condition):
        self.work_store, self.ledger = work_store, Path(ledger)
        self.policy = policy if policy is not None else configured_policy()
        # The four technical retries stay separate from the explicitly approved
        # primary allowance, including an amendment in this same ledger.
        self.primary_limit = self.policy.max_total_calls - 4 if self.policy else 0
        self.work_profiles = active_approval().get("work_profiles") if self.policy and self.policy.mode == "live" else None
        self.upload_scope = active_approval().get("upload_scope") if self.policy and self.policy.mode == "live" else None
        self.transport_factory, self.solver = transport_factory, solver
        self.directory = work_store.root / ".live-checks"
        self.configuration = configuration_identity()
        self.runtime_problem=runtime_health() if solver is solve_condition else None
        profiles = json.loads((ROOT/"planning/l04/configurations.json").read_text())["profiles"]
        self.profiles = {p["id"]:p for p in profiles}
        self.profiles["G-checker"]["requested_parameters"]["reasoning_effort"] = "medium"
        # Historical profiles are retained only for explicitly injected offline
        # fixtures. Production and native-API fixtures use the live config.
        rules = (self.policy.dispatch_rules or self.policy.ceiling_rules) if self.policy else {}
        self.direct_gemini = transport_factory is None or any(r.get("provider") == "google" for r in rules.values())
        self.provider_problem = None
        self.gemini = None
        if self.direct_gemini:
            try:
                self.gemini = gemini_config()
                for name in PROFILES["G"]:
                    self.profiles[name] = live_profile(self.profiles[name], self.gemini)
            except ValueError as error:
                self.provider_problem = str(error)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.queue_owner = "O" + uuid.uuid4().hex
        self.queue_owner_lock = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fresh-check")
        # Interrupted executions are never silently resent after restart.
        self.history_errors=[]
        if self.directory.exists():
            for path in self.directory.glob("C*/job.json"):
                try:
                    with (path.parent/"execution.lock").open("a") as execution:
                        try: fcntl.flock(execution,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        except BlockingIOError: continue
                        value=json.loads(path.read_text())
                        if value["state"] in {"queued","running"}:
                            if self._queue_owner_alive(value): continue
                            value.update(state="interrupted",stage="interrupted",error_code="technical_error",message=MESSAGES["technical_error"])
                            atomic_json(path,value)
                except (OSError,ValueError,KeyError,TypeError): self.history_errors.append(path.parent.name)

    def close(self):
        # An already dispatched request still needs its real response/invoice.
        # Stop the next stage and publication without pretending to cancel it.
        with self.lock:
            self.closed.set()
            self.pool.shutdown(wait=False, cancel_futures=True)
            if self.queue_owner_lock is not None:
                self.queue_owner_lock.close()
                self.queue_owner_lock = None

    def _ensure_open(self):
        if self.closed.is_set(): raise ValueError("operation_cancelled")

    def _claim_queue_owner(self):
        if self.queue_owner_lock is not None: return
        directory=self.directory/".queue-owners"
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock=(directory/(self.queue_owner+".lock")).open("a")
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
        self.queue_owner_lock=lock

    def _queue_owner_alive(self,job):
        owner=job.get("queue_owner")
        if not isinstance(owner,str) or not re.fullmatch(r"O[0-9a-f]{32}",owner): return False
        path=self.directory/".queue-owners"/(owner+".lock")
        try:
            with path.open("r+") as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: return True
        except FileNotFoundError: return False
        return False

    def _completed_scope(self):
        if self.work_profiles is None: return set()
        completed=set()
        for name in ("FIVE_WORK_RECHECKS.json", "GEMINI_CONTROL.json", "STRICT_GEMINI_CONTROL.json", "INEQUALITY_ASSESSMENT.json", "CORE_GEMINI_CONTROL.json"):
            path=ROOT/"planning/m17"/name
            if not path.exists(): continue
            report=json.loads(path.read_text())
            completed.update(c["work_id"] for c in report.get("checks",[])
                             if c.get("state") in {"failed","completed","needs_clarification"})
            if report.get("outcome",{}).get("state") in {"failed","completed","needs_clarification"}:
                completed.add(report["scope"]["work_id"])
        return completed

    def _spending_admission_block(self, rows):
        return CallGuard.admission_block(rows,self.policy) or ("unresolved_spending" if any(r["cost"] is None for r in rows) else None)

    def availability(self, *, required_calls=3, work_id=None):
        approval = active_approval()
        reason=self.policy.validate() if self.policy is not None else policy_problem()
        if self.closed.is_set(): reason="operation_cancelled"
        if self.configuration!=configuration_identity(): reason="configuration_changed"
        spending=None
        db=self.ledger/"spending.sqlite3"
        if db.exists():
            try:
                with closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)) as con:
                    con.row_factory=sqlite3.Row
                    rows=con.execute("SELECT * FROM calls ORDER BY created,id").fetchall()
                    saved=con.execute("SELECT policy FROM config WHERE id=1").fetchone()
                    reconciled={r[0] for r in con.execute("SELECT call_id FROM events WHERE kind='reconciled'")}
                unresolved=[r["id"] for r in rows if r["cost"] is None]
                known=sum(r["cost"] or 0 for r in rows)
                held=sum(r["reserve"] for r in rows if r["cost"] is None)
                primary=sum(r["retry_of"] is None for r in rows)
                exact=sum((Decimal(r["reported_cost_kopecks"])/100 for r in rows if r["reported_cost_kopecks"] is not None),Decimal(0))
                spending=dict(calls=len(rows),known_micro_rub=known,held_micro_rub=held,committed_micro_rub=known+held,unresolved=unresolved,
                    cost=dict(status="unknown" if unresolved else "known",rub=None if unresolved else str(exact)),
                    primary_remaining=max(0,(self.primary_limit if self.policy else approval["primary_calls"])-primary))
                spending.update(accounting_summary(rows, self.ledger, reconciled_call_ids=reconciled))
                if not reason:
                    block=self._spending_admission_block(rows)
                    if saved and saved["policy"]!=canonical(asdict(self.policy)): reason="ledger_policy_mismatch"
                    elif block: reason=block
                    elif known+held>=self.policy.budget_micro_rub: reason="budget_limit"
                    elif len(rows)>=self.policy.max_total_calls: reason="total_call_limit"
                    elif primary+required_calls>self.primary_limit or len(rows)+required_calls>self.policy.max_total_calls: reason="insufficient_stage_calls"
            except (OSError,sqlite3.Error,ValueError,TypeError,KeyError): reason="ledger_unavailable"
        completed=self._completed_scope()
        models=[]
        for key,profile_ids in PROFILES.items():
            blocked="model_profile_unreliable" if self.direct_gemini and key != "G" else None
            if self.work_profiles is not None:
                allowed = self.work_profiles.get(work_id) == key or self.upload_scope == {"kind":"all_saved_uploads", "model":"G"} and key == "G"
                if work_id is not None and not allowed: blocked="check_scope_not_authorized"
                elif work_id in completed or self.upload_scope is None and work_id is None and set(self.work_profiles)<=completed: blocked="authorized_checks_completed"
                elif key=="D": blocked="model_profile_unreliable"
            if work_id is not None and self._existing_attempt(work_id, key) is not None:
                blocked = "authorized_checks_completed"
            models.append(dict(id=key,model=self.profiles[profile_ids[0]]["model"],
                reasoning_effort=self.profiles[profile_ids[0]]["requested_parameters"]["reasoning_effort"],
                assessment_reasoning_effort=self.profiles[profile_ids[1]]["requested_parameters"]["reasoning_effort"],
                available=blocked is None,reason=blocked,error=MESSAGES.get(blocked)))
        if not reason and all(not m["available"] for m in models):
            reason=models[0]["reason"]
        if not reason and self.runtime_problem: reason=self.runtime_problem
        if not reason and self.provider_problem: reason=self.provider_problem
        if not reason and self.direct_gemini:
            try:
                config=gemini_config()
                if self.transport_factory is None: reason=vpn_problem() or access_problem(config)
            except ValueError as error: reason=str(error)
        return dict(paid_api_enabled=reason is None,upload_pipeline_connected=True,budget_rub=approval["budget_rub"],
            reason=reason,spending=spending,budget_mode=self.policy.budget_mode if self.policy else None,
            error=MESSAGES.get(reason,"Настройки запуска требуют проверки.") if reason else None,
            models=models,default_model="G",assessment=None,pipeline_version=VERSION,
            provider="google" if self.direct_gemini else "fixture")

    def refresh_provider_access(self):
        """Explicit free metadata request; never change VPN, photos or spending."""
        with self.lock:
            try:
                result=check_access()
                atomic_json(ACCESS_PATH,result)
            except ValueError:
                result=dict(model_available=False,error="credential_missing_or_invalid",generation_requests=0)
        return dict(access=result,checking=self.availability())

    def get(self, job_id):
        if not isinstance(job_id,str) or not re.fullmatch(JOB_ID,job_id): raise FileNotFoundError
        directory=self.directory/job_id
        value=json.loads((directory/"job.json").read_text())
        if not isinstance(value,dict) or value.get("id") != job_id: raise ValueError("job_manifest")
        if value.get("result_ref"):
            result_name=value["result_ref"]
            if not isinstance(result_name,str) or not re.fullmatch(r"result-[0-9]+\.json",result_name): raise ValueError("result_path")
            path=directory/result_name
            if path.resolve().parent!=directory.resolve(): raise ValueError("result_path")
            result=json.loads(path.read_text())
            report=value.get("report",{})
            if not isinstance(report,dict): raise ValueError("job_manifest")
            pins=[value["result_sha256"]] if "result_sha256" in value else []
            pins += [report[key] for key in ("accepted_result_sha256","result_sha256") if key in report]
            if (any(not isinstance(pin,str) or not re.fullmatch(r"[0-9a-f]{64}",pin) for pin in pins)
                    or any(pin!=digest(result) for pin in pins)):
                raise ValueError("result_artifact_changed")
            # Historical unpinned records stay immutable. Check their exact
            # saved input and the unchanged M01 contract before showing them.
            source_path=directory/("verified-input.json" if (directory/"verified-input.json").exists() else "original-input.json")
            if source_path.resolve().parent!=directory.resolve(): raise ValueError("result_path")
            source=json.loads(source_path.read_text())
            if contract.validate(result,source): raise ValueError("result_contract")
            value["result"]=result
        return value

    def latest(self, work_id):
        if not re.fullmatch(r"E[0-9a-f]{32}",work_id): raise FileNotFoundError
        jobs=[]
        if self.directory.exists():
            for p in self.directory.glob("C*/job.json"):
                try: value=json.loads(p.read_text())
                except (OSError,ValueError): continue
                if isinstance(value,dict) and value.get("work_id")==work_id: jobs.append(value)
        if not jobs: return None
        return self.get(max(jobs,key=lambda v:v["created_at"])["id"])

    def _existing_attempt(self, work_id, model, guard=None):
        """Reopen an attempt with unchanged input across code upgrades; never reuse its score as a new assessment."""
        if guard is None:
            try: _, guard, _ = input_snapshot(self.work_store, work_id)
            except (OSError, ValueError): return None
        matches = []
        for path in self.directory.glob("C*/job.json"):
            value = json.loads(path.read_text())
            if value.get("work_id") == work_id and value.get("model") == model and value.get("guard") == guard:
                matches.append(value)
        return max(matches, key=lambda v:v["created_at"]) if matches else None

    def create(self, form, *, background=True):
        if (not isinstance(form,dict) or set(form)!={"work_id","model","complete","single_task"}
                or not isinstance(form["model"],str) or form["model"] not in PROFILES
                or not isinstance(form["work_id"],str) or not re.fullmatch(r"E[0-9a-f]{32}",form["work_id"])
                or form["complete"] is not True or form["single_task"] is not True):
            raise ValueError("check_input_confirmation")
        source, guard, _=input_snapshot(self.work_store,form["work_id"])
        identity=dict(guard=guard,model=form["model"],configuration=self.configuration)
        job_id="C"+digest(identity)[:32]
        with self.lock, (self.work_store.root/".fresh-create.lock").open("a") as creation:
            fcntl.flock(creation,fcntl.LOCK_EX)
            self._ensure_open()
            previous = self._existing_attempt(form["work_id"], form["model"], guard)
            if previous is not None:
                return dict(accepted=True,reused=True,job=self.get(previous["id"]))
            path=self.directory/job_id/"job.json"
            if path.exists(): return dict(accepted=True,reused=True,job=self.get(job_id))
            availability=self.availability(work_id=form["work_id"])
            model=next(m for m in availability["models"] if m["id"]==form["model"])
            if not model["available"]: return dict(accepted=False,reason=model["reason"],error=model["error"])
            if not availability["paid_api_enabled"]: return dict(accepted=False,**availability)
            try: self._preflight_assets(form["work_id"],form["model"],source,guard)
            except ValueError as error:
                code=str(error)
                return dict(accepted=False,reason=code,error=MESSAGES.get(code,"Не удалось подготовить фотографии к проверке. Запросы не отправлены."))
            directory=path.parent; directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            self._ensure_open()
            self._claim_queue_owner()
            os.chmod(self.directory,0o700); os.chmod(directory,0o700)
            source["capture"]["pages_complete"]="confirmed"  # explicit per-run human confirmation
            atomic_json(directory/"original-input.json",source)
            value=dict(id=job_id,work_id=form["work_id"],model=form["model"],guard=guard,
                created_at=datetime.now(timezone.utc).isoformat(),state="queued",stage="queued",condition_versions=[],
                solver=None,result_ref=None,message="Работа в очереди проверки.",error_code=None,call_fingerprints=[],configuration=self.configuration,queue_owner=self.queue_owner)
            atomic_json(path,value)
            if background: self.pool.submit(self._run,job_id)
        if not background: self._run(job_id)
        return dict(accepted=True,reused=False,job=self.get(job_id))

    def _preflight_assets(self,work_id,model,source,guard):
        job=dict(work_id=work_id,model=model,guard=guard)
        manifest=[{k:p[k] for k in ("id","asset_id","sha256","role")} for p in source["pages"]]
        if not 1 <= sum(p["role"] == "solution" for p in source["pages"]) <= 10:
            raise ValueError("solution_page_limit")
        condition_request=self._condition_request(job,source)
        request=self._reading_request(job,source,manifest)
        baseline=base_result(source,dict(id="preflight",configuration=self.configuration))
        baseline["criteria"]=rubric(source["task"]["task_type"])
        assessment,_=self._assessment_request(job,source,baseline,manifest,dict(status="not_run",answer="preflight"))
        from types import SimpleNamespace
        probe=SimpleNamespace(policy=self.policy)
        for stage_request in (condition_request, request, assessment):
            admitted = CallGuard._dispatch_for(probe,stage_request) if self.policy.budget_mode == "settled_spend" else CallGuard._ceiling_for(probe,stage_request)
            if admitted is None: raise ValueError("billing_ceiling_unproven")
        # Reserve room for the largest accepted reading and fixed assessment
        # instructions. This is a conservative admission bound, not OCR quality.
        if len(canonical(request.payload).encode()) + 2*self.policy.max_response_bytes + 262144 > BODY_LIMIT:
            raise ValueError("http_body_limit")

    def _save(self,job): atomic_json(self.directory/job["id"]/"job.json",job)

    def recover_saved_compact_response(self,job_id):
        """Publish a separate local version; preserve old jobs, calls and raw data.

        Replays only settled compact READ/ASSESS format rejections. It never
        dispatches, continues an assessment, imports another work or supplies
        missing content. An accepted READ without an ASSESS remains unscored.
        """
        with self.lock:
            original=self.get(job_id);original.pop("result",None)
            if (original["state"]!="failed" or original["error_code"]!="invalid_response"
                    or original.get("response_validation",{}).get("stage") not in {"solution_ocr","grading"}):
                raise ValueError("recovery_not_applicable")
            _,current_guard,_=input_snapshot(self.work_store,original["work_id"])
            if current_guard!=original["guard"]:raise ValueError("input_changed")
            if original["configuration"]["prompts"]!=self.configuration["prompts"]:
                raise ValueError("recovery_prompt_package_changed")
            if self.policy is None:raise ValueError("policy_invalid")
            old_directory=self.directory/job_id
            source=json.loads((old_directory/"verified-input.json").read_text())
            with (self.ledger/"experiment.lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                guard=CallGuard(self.ledger,self.policy)
                with closing(guard._connect()) as con:
                    rows=con.execute("SELECT * FROM calls ORDER BY created,id").fetchall()
                saved={}
                for row in rows:
                    if row["fingerprint"] not in original["call_fingerprints"]:continue
                    request=StageRequest(**json.loads((self.ledger/row["request_ref"]).read_text()))
                    if request.fingerprint!=row["fingerprint"]:raise ValueError("request_artifact_changed")
                    if request.stage not in {"solution_ocr","grading"}:continue
                    data=json.loads(request.payload["messages"][1]["content"][0]["text"])
                    expected_task={k:deepcopy(source["task"][k]) for k in ("exam_year","exam_number","task_type")}
                    expected_task["statement"]={k:source["task"]["statement"][k] for k in ("text","format","completeness","missing")}
                    expected_images={(p["asset_id"],p["sha256"]) for p in source["pages"]}
                    actual_images={(p["asset_id"],p["sha256"]) for p in request.context["images"]}
                    if data["task"]!=expected_task or actual_images!=expected_images:raise ValueError("input_changed")
                    if request.stage in saved or row["state"]!="settled" or row["cost"] is None or row["status"] not in {"succeeded","invalid_response"}:
                        raise ValueError("recovery_not_applicable")
                    document=json.loads((self.ledger/row["response_ref"]).read_text())
                    if digest(document)!=row["response_sha256"]:raise ValueError("response_artifact_changed")
                    envelope=document["envelope"]
                    if (envelope["http_status"]!=200 or envelope["finish_reason"]!="stop"
                            or envelope["model"]!=request.payload["model"] or not isinstance(envelope["content"],str)
                            or len(envelope["content"].encode())>self.policy.max_response_bytes
                            or envelope["usage"].get("output_tokens") is not None and envelope["usage"]["output_tokens"]>request.payload["max_tokens"]
                            or not request.versions["prompt"].startswith(("compact-","assessment-core-"))):
                        raise ValueError("recovery_not_applicable")
                    saved[request.stage]=(row,request,parse_model_json(envelope["content"]))
                if "solution_ocr" not in saved:raise ValueError("recovery_not_applicable")
                if original["response_validation"]["stage"]=="grading" and "grading" not in saved:
                    raise ValueError("recovery_not_applicable")
                failed_stage=original["response_validation"]["stage"]
                if (saved[failed_stage][0]["status"]!="invalid_response"
                        or failed_stage=="grading" and saved["solution_ocr"][0]["status"]!="succeeded"
                        or failed_stage=="solution_ocr" and "grading" in saved):
                    raise ValueError("recovery_not_applicable")
                identity=dict(kind="saved-compact-local-revalidation",original_job=job_id,
                    configuration=self.configuration,guard=current_guard,
                    responses={stage:row["response_sha256"] for stage,(row,_,_) in saved.items()})
                new_id="C"+digest(identity)[:32]
                directory=self.directory/new_id
                if (directory/"job.json").exists():return self.get(new_id)
                candidate=base_result(source,dict(id=new_id,configuration=self.configuration))
                reading_row,reading_request,raw=saved["solution_ocr"]
                internal=reading_schema(sum(p["role"]=="solution" for p in source["pages"]))
                diagnostics=reading_diagnostics(raw,internal) if original["model"]=="G" else None
                decoded=decode_reading(raw,internal) if original["model"]=="G" else raw
                candidate=materialize_compact_reading(decoded,candidate,source)
                candidate["criteria"]=rubric(source["task"]["task_type"])
                if any(c["score_scope"]=="unresolved" for c in candidate["coverage"]):
                    raise ValueError("reading_needs_clarification")
                condition=condition_input(original["condition_versions"][-1]);solved=original["solver"]
                if solved.get("status")!="solved":raise ValueError("recovery_not_applicable")
                candidate["facts"].append(dict(id="condition-key",unit_ids=[],interpretation_ids=[],claim="Ответ для прочитанного условия: "+solved["answer"],
                    method="exact_code",status="supported",reason="Независимое решение прочитанного условия; не подтверждает верность OCR фотографии.",
                    tool_version=solved["version"],depends_on_fact_ids=[],branch_id="main",assumption_ids=[],context_unit_ids=[],claim_kind="completeness",
                    domain=solved["domain"],basis_sha256=contract.reading_digest(candidate),code_check=dict(operation="condition.solve",arguments=condition,evidence=canonical(solved))))
                checkpoint=deepcopy(candidate)
                local_revalidations={reading_row["id"]:decoded}
                assessed="grading" in saved
                if assessed:
                    row,request,value=saved["grading"]
                    data=json.loads(request.payload["messages"][1]["content"][0]["text"])
                    projector=project_core if "checks" in value else project_compact_assessment
                    projected=projector(value,candidate,source,cases=data.get("candidate_cases",[]))
                    candidate=materialize_assessment(deepcopy(projected),candidate,source)
                    local_revalidations[row["id"]]=projected
                else:
                    candidate["score"]["rationale"]="Чтение восстановлено из сохранённого ответа. Оценивание ещё не выполнено."
                candidate["supersedes"]=json.loads((old_directory/original["result_ref"]).read_text())["id"]
                job=deepcopy(original)
                job.update(id=new_id,configuration=self.configuration,created_at=datetime.now(timezone.utc).isoformat(),
                    state="completed" if assessed else "awaiting_assessment",stage="completed" if assessed else "reading_completed",
                    error_code=None,result_ref="result-1.json",reading_format_diagnostics=diagnostics,
                    message="Оценка восстановлена из сохранённого ответа; новых платных запросов не было." if assessed else
                        "Чтение восстановлено из сохранённого ответа. Оценивание ещё не выполнено; новых платных запросов не было.")
                job.pop("response_validation",None);job.pop("failure_stage",None)
                job["recovery"]=dict(method="saved-compact-local-revalidation",original_job_id=job_id,
                    original_failure=original.get("response_validation"),original_configuration=original["configuration"],
                    at=job["created_at"],model_calls=0,assessment_replayed=assessed)
                if assessed and "checks" in saved["grading"][2]:
                    document=dict(response=deepcopy(saved["grading"][2]),reading_sha256=digest(checkpoint),
                        source_sha256=digest(source),cases=data.get("candidate_cases",[]))
                    atomic_json(directory/"assessment-core.json",document)
                    job["report"]=dict(version=REPORT_VERSION,state="deferred",available=True,draft_available="report" in document["response"],
                        core_sha256=digest(document),message=DEFERRED_MESSAGE,model_calls=0)
                self._stamp(job,candidate,source,guard,local_revalidations=local_revalidations)
                job["result_sha256"]=digest(candidate)
                if job.get("report"):job["report"]["accepted_result_sha256"]=digest(candidate)
                job["accounting"]=guard.snapshot(fingerprints=job["call_fingerprints"])
                # Write the manifest last: a partial save never publishes a result.
                atomic_json(directory/"original-input.json",json.loads((old_directory/"original-input.json").read_text()))
                atomic_json(directory/"verified-input.json",source)
                atomic_json(directory/"reading.json",checkpoint)
                atomic_json(directory/"result-1.json",candidate)
                self._save(job)
                return self.get(new_id)

    def recover_saved_assessment(self,job_id):
        """Local recovery after a controller fix; never calls a model.

        Keeps original rejection/configuration and raw billing artifacts in the
        audit. Only settled assessment rejections with unchanged inputs qualify.
        """
        with self.lock:
            job=self.get(job_id);job.pop("result",None)
            if job["state"]!="failed" or job["error_code"]!="invalid_response":raise ValueError("recovery_not_applicable")
            _,current_guard,_=input_snapshot(self.work_store,job["work_id"])
            if current_guard!=job["guard"]:raise ValueError("input_changed")
            directory=self.directory/job_id
            source=json.loads((directory/"verified-input.json").read_text())
            with (self.ledger/"experiment.lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                guard=CallGuard(self.ledger,self.policy)
                with closing(guard._connect()) as con:
                    rows=con.execute("SELECT * FROM calls ORDER BY created").fetchall()
                stages={}
                for row in rows:
                    if row["fingerprint"] not in job["call_fingerprints"]:continue
                    request=json.loads((self.ledger/row["request_ref"]).read_text())
                    stages[request["stage"]]=row
                reading=stages.get("solution_ocr");assessment=stages.get("grading")
                if not reading or reading["status"]!="succeeded" or not assessment or assessment["status"]!="invalid_response":raise ValueError("recovery_not_applicable")
                document=json.loads((self.ledger/reading["response_ref"]).read_text())
                if digest(document)!=reading["response_sha256"]:raise ValueError("response_artifact_changed")
                candidate=base_result(source,dict(id=job_id,configuration=job["configuration"]))
                candidate["created_at"]=job["created_at"]
                reading_request=json.loads((self.ledger/reading["request_ref"]).read_text())
                materializer=(materialize_compact_reading if reading_request["versions"]["prompt"].startswith("compact-reading-")
                              else materialize_reading_attempt)
                candidate=materializer(deepcopy(document["validated"]),candidate,source)
                candidate["criteria"]=rubric(source["task"]["task_type"])
                condition=condition_input(job["condition_versions"][-1]);solved=job["solver"]
                candidate["facts"].append(dict(id="condition-key",unit_ids=[],interpretation_ids=[],claim="Ответ для прочитанного условия: "+solved["answer"],
                    method="exact_code",status="supported",reason="Независимое решение прочитанного условия; не подтверждает верность OCR фотографии.",
                    tool_version=solved["version"],depends_on_fact_ids=[],branch_id="main",assumption_ids=[],context_unit_ids=[],claim_kind="completeness",
                    domain=solved["domain"],basis_sha256=contract.reading_digest(candidate),code_check=dict(operation="condition.solve",arguments=condition,evidence=canonical(solved))))
                def validate(v):
                    if any(f.get("method") in {"exact_code","counterexample","human"} for f in v.get("facts",[])):raise ValueError("model_claimed_code_authority")
                    projected=project_assessment(v,source["task"]["task_type"])
                    materialize_assessment(deepcopy(projected),candidate,source)
                    return projected
                out=guard.revalidate_response(assessment["id"],validate,validator_ref="assessment-projection-1.0.0:"+digest(self.configuration))
                result=materialize_assessment(deepcopy(out.value),candidate,source)
                job["recovery"]=dict(previous_state=job["state"],previous_error=job["error_code"],previous_configuration=job["configuration"],
                    configuration=self.configuration,at=datetime.now(timezone.utc).isoformat(),method="saved-response-local-revalidation",model_calls=0)
                job["configuration"]=self.configuration
                self._stamp(job,result,source,guard)
                atomic_json(directory/"result-2.json",result)
                job.update(state="completed",stage="completed",result_ref="result-2.json",result_sha256=digest(result),error_code=None,
                    accounting=guard.snapshot(fingerprints=job["call_fingerprints"]),message="Проверка завершена по сохранённым ответам; новых платных запросов не было.")
                self._save(job)
            return self.get(job_id)

    def confirm_empty_condition(self,job_id,form,*,background=True):
        """Explicit human verification of a twice-read, independently empty condition.

        Never approves an unsupported solver or an unreadable expression. The
        same run/budget continues without sending the condition stage again.
        """
        if not isinstance(form,dict) or set(form)!={"literal","confirm_empty"} or form["confirm_empty"] is not True:
            raise ValueError("condition_confirmation_invalid")
        with self.lock:
            self._ensure_open()
            job=self.get(job_id); job.pop("result",None)
            latest=(condition_input(job["condition_versions"][-1]) if job["condition_versions"] else None) or {}
            if (latest.get("status")!="clear" or latest.get("literal")!=form["literal"]
                    or job.get("solver",{}).get("status")!="solved" or job["solver"].get("empty") is not True):
                raise ValueError("condition_confirmation_invalid")
            self._unchanged(job)
            if job.get("human_confirmed_condition"):
                return dict(accepted=True,reused=True,job=self.get(job_id))
            if job.get("error_code")!="condition_suspicious" or job["state"]!="needs_clarification":
                raise ValueError("condition_confirmation_invalid")
            availability=self.availability(required_calls=2,work_id=job["work_id"])
            if not availability["paid_api_enabled"]: return dict(accepted=False,**availability)
            self._ensure_open()
            job["human_confirmed_condition"]=dict(literal=form["literal"],at=datetime.now(timezone.utc).isoformat(),source="local-user-explicit-confirmation")
            self._claim_queue_owner()
            job["queue_owner"]=self.queue_owner
            job.update(state="queued",stage="queued",error_code=None,message="Прочитанное условие подтверждено. Продолжаем проверку при доказанном пустом ответе.")
            self._save(job)
            if background: self.pool.submit(self._run,job_id)
        if not background: self._run(job_id)
        return dict(accepted=True,reused=False,job=self.get(job_id))

    def prepare_saved_report(self,job_id):
        """Explicit local detail stage. No credential, provider, guard or spending mutation."""
        if not isinstance(job_id,str) or not re.fullmatch(JOB_ID,job_id):raise ValueError("job_id")
        with self.lock:
            directory=self.directory/job_id
            with (directory/"execution.lock").open("a") as execution:
                fcntl.flock(execution,fcntl.LOCK_EX|fcntl.LOCK_NB)
                job=self.get(job_id); current=job.pop("result",None)
                if (job["state"]!="completed" or not current or current["readiness"]["score"]!="ready"
                        or not job.get("report",{}).get("available")):
                    raise ValueError("report_not_available")
                if digest(current)!=job["report"].get("accepted_result_sha256"):raise ValueError("report_input_changed")
                if job["report"]["state"]=="ready":return dict(accepted=True,reused=True,job=self.get(job_id))
                self._unchanged(job)
                source=json.loads((directory/"verified-input.json").read_text())
                reading=json.loads((directory/"reading.json").read_text())
                document=json.loads((directory/"assessment-core.json").read_text())
                if (digest(document)!=job["report"]["core_sha256"]
                        or digest(source)!=document["source_sha256"] or digest(reading)!=document["reading_sha256"]):
                    raise ValueError("report_input_changed")
                try:
                    core=materialize_compact_assessment(document["response"],reading,source,cases=document["cases"])
                    keys=("value","criterion_ids","requirement_ids","evidence_unit_ids","case_check_ids","finding_ids","ambiguity_group_ids")
                    if any(core["score"][k]!=current["score"][k] for k in keys):raise ValueError("report_grade_changed")
                    draft=(document["response"]["report"] if "report" in document["response"] else
                        report_from_core(document["response"],source["task"]["task_type"]))
                    candidate=materialize_report(document["response"],draft,reading,source,cases=document["cases"])
                    # Preserve the accepted core and actual paid-run provenance exactly.
                    for section in ("run","units","interpretations","relations","facts","requirements","branches",
                                    "assumptions","case_checks","coverage","clarifications","corrections"):
                        candidate[section]=deepcopy(current[section])
                    candidate["score"]=deepcopy(current["score"])
                    candidate["issues"]=[deepcopy(i) for i in current["issues"] if i["message"]!=DEFERRED_MESSAGE]
                    candidate["supersedes"]=current["id"]
                    basis=contract.assessment_digest(candidate)
                    candidate["score"]["basis_sha256"]=basis
                    for feedback in candidate["feedback"]:feedback["basis_sha256"]=basis if feedback["status"]=="ready" else None
                    if contract.validate(candidate,source):raise ValueError("report_contract")
                except ValueError as error:
                    code=str(error)
                    if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}",code):code="report_contract"
                    job["report"].update(state="failed",validation_code=code,
                        message="Подробные замечания не прошли проверку. Сохранённый балл доступен.")
                    if hasattr(error,"m01_errors"):
                        job["report"]["m01_errors"]=list(error.m01_errors)
                    self._save(job)
                    return dict(accepted=False,reused=False,job=self.get(job_id))
                versions=[int(p.stem.split('-')[1]) for p in directory.glob('result-*.json')
                    if re.fullmatch(r'result-[0-9]+\.json',p.name)]
                ref="result-"+str(max(versions,default=0)+1)+".json"
                atomic_json(directory/ref,candidate)
                job["result_ref"]=ref
                job["result_sha256"]=digest(candidate)
                job["report"].update(state="ready",validation_code=None,message="Пояснения подготовлены.",model_calls=0,
                    source="saved_draft" if "report" in document["response"] else "core_requirements",
                    accepted_result_sha256=digest(candidate),result_sha256=digest(candidate))
                self._save(job)
                return dict(accepted=True,reused=False,job=self.get(job_id))

    def _unchanged(self,job):
        _,guard,_=input_snapshot(self.work_store,job["work_id"])
        if guard != job["guard"] or configuration_identity()!=job["configuration"]: raise ValueError("input_changed")

    def _stamp(self,job,candidate,source,guard,*,local_revalidations=None):
        calls=guard.contract_calls(fingerprints=job["call_fingerprints"])
        # The new result records the current local acceptance verdict with its
        # own artifact. Original ledger verdicts and invoices stay immutable.
        for call in calls:
            if not local_revalidations or call["id"] not in local_revalidations:continue
            original_ref=call["response_ref"]
            document=json.loads((self.ledger/original_ref).read_text())
            local=dict(original_response_ref=original_ref,original_response_sha256=digest(document),
                previous_status=call["status"],validator_ref=digest(job["configuration"]),
                method="saved-compact-local-revalidation",model_calls=0)
            revised=dict(envelope=deepcopy(document["envelope"]),
                validated=deepcopy(local_revalidations[call["id"]]),local_revalidation=local)
            ref="private_artifacts/"+job["id"]+"-"+call["id"]+"-locally-validated.json"
            path=self.ledger/ref
            if path.exists():
                if json.loads(path.read_text())!=revised:raise ValueError("response_artifact_changed")
            else:atomic_json(path,revised)
            call["response_ref"]=ref
            call["status"]="succeeded"
            call["settings"]["local_revalidation"]={**local,"artifact_sha256":digest(revised)}
        mode="live" if self.policy.mode=="live" else "demo"
        candidate["run"]=dict(id=job["id"],mode=mode,source_ref="fresh-upload:m14" if mode=="live" else "fresh-upload:synthetic-fixture",
            pipeline_version=VERSION,configuration_sha256=digest(job["configuration"]),configuration_ref="job:configuration",
            calls=calls,cost=guard.snapshot(fingerprints=job["call_fingerprints"])["cost"] if mode=="live" else dict(status="not_incurred",rub="0"),
            timing=dict(wall_ms=sum(c["latency_ms"] or 0 for c in calls),human_active_ms=None))
        for section in ("units","interpretations","relations"):
            for item in candidate[section]:
                if item.get("source",{}).get("ref","").startswith("m05:offline-"):
                    item["source"]["ref"]="fresh-upload:"+job["id"]+":reading"
        if candidate["score"]["value"] is not None: candidate["score"]["source_ref"]="fresh-upload:"+job["id"]+":assessment"
        # Runtime provenance replaces the offline materializer's placeholders.
        # Restamp its exact input snapshot after this metadata-only replacement.
        reading_hash=contract.reading_digest(candidate)
        for fact in candidate["facts"]:
            if fact["status"]!="unknown": fact["basis_sha256"]=reading_hash
        assessment_hash=contract.assessment_digest(candidate)
        candidate["score"]["basis_sha256"]=assessment_hash if candidate["score"]["value"] is not None else None
        for feedback in candidate["feedback"]:
            feedback["basis_sha256"]=assessment_hash if feedback["status"]=="ready" else None
        validate_candidate(candidate,source,"result_contract")

    def _call(self,job,guard,request,validator,*,response_text=False):
        self._ensure_open()
        self._unchanged(job)
        # This limit applies to the common ledger, including earlier L04 calls.
        with closing(sqlite3.connect(guard.db)) as con:
            exists=con.execute("SELECT 1 FROM calls WHERE fingerprint=?",(request.fingerprint,)).fetchone()
            primaries=con.execute("SELECT COUNT(*) FROM calls WHERE retry_of IS NULL").fetchone()[0]
        if not exists and primaries>=self.primary_limit: raise ValueError("primary_call_limit")
        if request.fingerprint not in job["call_fingerprints"]: job["call_fingerprints"].append(request.fingerprint)
        self._save(job)
        # Credentials are read only after the bounded rule has admitted this body.
        soft=self.policy.budget_mode=="settled_spend"
        if (guard._dispatch_for(request) if soft else guard._ceiling_for(request)) is None: raise ValueError("billing_ceiling_unproven")
        if self.transport_factory is not None:
            transport=self.transport_factory(request)
        else:
            config=gemini_config()
            if request.provider != "google" or request.payload["model"] != config["model"]:
                raise ValueError("hubris_retired")
            problem=vpn_problem() or access_problem(config)
            if problem: raise ValueError(problem)
            key=read_credential(Path(config["credential_file"]))
            transport=GeminiTransport(key,
                allowed_body_hashes=[digest(request.payload)],
                allowed_native_body_hashes=[request.context["native_request_sha256"]],
                allowed_models=[config["model"]], pricing=config["pricing"],
                total_output_bound_verified=not soft)
        def validate(value):
            try:
                return validator(value)
            except ValueError as error:
                # Stable internal code only; never expose private JSON or an
                # arbitrary exception string in the browser/job diagnostics.
                safe_code=error.code if isinstance(error,PromptError) else str(error)
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}",safe_code): safe_code="invalid_response"
                job["response_validation"]=dict(stage=request.stage,code=safe_code)
                if hasattr(error, "m01_errors"):
                    job["response_validation"]["m01_errors"] = list(error.m01_errors)
                self._save(job)
                raise
        self._ensure_open()
        outcome=guard.execute(request,transport,validate,before_dispatch=lambda:not self.closed.is_set(),response_text=response_text)
        if outcome.diagnostic:
            job['failure_diagnostic']={**outcome.diagnostic,'work_id':job['work_id'],'check_id':job['id']}
        job["accounting"]=guard.snapshot(fingerprints=job["call_fingerprints"])
        self._save(job)
        if outcome.status=="cancelled_before_dispatch":raise ValueError("operation_cancelled")
        if outcome.status!="succeeded": raise ValueError(outcome.status)
        self._ensure_open()
        self._unchanged(job)
        return outcome

    def _condition_request(self,job,source,previous=None):
        work=self.work_store.get(job["work_id"])
        profile=self.profiles[PROFILES[job["model"]][0]]
        data=dict(task_type=work["task_type"],text=work["task_text"],selection="One explicitly selected task; human confirms all condition pages are present.",
            sources=sorted(self._condition_sources(work,source)))
        if previous: data["targeted_recheck"]=previous
        parts=[dict(type="text",text=canonical(data))]
        photos=[]
        for p in source["pages"]:
            if p["role"]!="task": continue
            raw=(self.work_store.root/job["work_id"]/f"task_{p['order']}.png").read_bytes()
            if hashlib.sha256(raw).hexdigest() != p["sha256"]: raise ValueError("input_changed")
            raw,mime,transformation=model_image(raw,max_bytes=4*1024*1024//max(1,len(source["pages"])))
            parts.extend([dict(type="text",text=canonical(dict(id=p["id"],role="task",source=f"photo:{p['order']}"))),
                dict(type="image_url",image_url=dict(url="data:"+mime+";base64,"+b64encode(raw).decode()))])
            photos.append(dict(id=p["id"],sha256=p["sha256"],delivered_sha256=hashlib.sha256(raw).hexdigest(),
                delivered_bytes=len(raw),mime_type=mime,transformation=transformation))
        payload=dict(model=profile["model"],stream=False,**deepcopy(profile["requested_parameters"]),
            messages=[dict(role="system",content=INSTRUCTIONS+"\nВерни только JSON по схеме:\n"+canonical(SCHEMA)),dict(role="user",content=parts)])
        if len(canonical(payload).encode())>BODY_LIMIT: raise ValueError("http_body_limit")
        request = StageRequest(stage="statement_ocr",provider=profile["provider"],payload=payload,
            context=dict(source_guard=job["guard"],images=photos,prompt_schema=digest(SCHEMA),stage_version=CONDITION_VERSION),
            versions=dict(contract="2.0.0",pipeline=VERSION,prompt=CONDITION_VERSION,code=VERSION,validator=CONDITION_VERSION),
            condition_complete=True)  # human confirms supplied condition, not mathematical correctness
        return self._native_request(request)

    def _native_request(self, request):
        if request.provider != "google": return request
        config=gemini_config()
        native=build_native_request(request.payload, allowed_models=[config["model"]])
        wire=canonical(native).encode()
        if len(wire)>BODY_LIMIT: raise ValueError("http_body_limit")
        return replace(request,context={**request.context,
            "native_endpoint":endpoint_for_model(request.payload["model"]),
            "native_request_sha256":hashlib.sha256(wire).hexdigest(),
            "native_request_bytes":len(wire),"native_adapter_version":TRANSPORT_VERSION,
            "native_configuration_sha256":gemini_identity(config)})

    def _condition_sources(self,work,source):
        return condition_sources(work["task_text"],[p["order"] for p in source["pages"] if p["role"]=="task"])

    def _assets(self,job,source):
        return [dict(asset_id=p["asset_id"],sha256=p["sha256"],path=str(self.work_store.root/job["work_id"]/f"{p['role']}_{p['order']}.png")) for p in source["pages"]]

    def _prepared(self,payload):
        return dict(payload=payload,payload_sha256=digest(payload),content_bytes=len(canonical(payload).encode()))

    def _reading_payload(self,source,manifest):
        return build_reading(source,manifest),STRICT_INSTRUCTIONS

    def _reading_request(self,job,source,manifest):
        payload,strict_instructions=self._reading_payload(source,manifest)
        strict=reading_response_format(payload["response_schema"]) if job["model"] == "G" else None
        if strict: payload["system"] += "\n\n"+strict_instructions
        builder=build_gemini_request if self.direct_gemini else build_hubris_request
        api=builder(self._prepared(payload),self.profiles[PROFILES[job["model"]][0]],
            self._assets(job,source),asset_root=self.work_store.root/job["work_id"],strict_format=strict,model_copies=True)
        request=api.stage_request
        if job["model"]=="G":
            body=deepcopy(request.payload)
            body["response_format"]=reading_response_format(payload["response_schema"])
            if len(canonical(body).encode())>BODY_LIMIT: raise ValueError("http_body_limit")
            request=replace(request,payload=body,context={**request.context,
                "strict_response_schema_sha256":digest(body["response_format"]["json_schema"]["schema"])})
        return self._native_request(replace(request,versions={**request.versions,
            "pipeline":VERSION,"validator":payload["version"]}))

    def _assessment_request(self,job,source,candidate,manifest,solved,*,features=()):
        assessment=build_assessment(source,candidate,manifest,solved,features=features)
        builder=build_gemini_request if self.direct_gemini else build_hubris_request
        api=builder(self._prepared(assessment),self.profiles[PROFILES[job["model"]][1]],
            self._assets(job,source),asset_root=self.work_store.root/job["work_id"],model_copies=True)
        request=replace(api.stage_request,versions={**api.stage_request.versions,
            "pipeline":VERSION,"validator":ASSESSMENT_VERSION})
        return self._native_request(request),assessment["data"]["candidate_cases"]

    def _run(self,job_id):
        with (self.directory/job_id/"execution.lock").open("a") as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return
            if self.get(job_id)["state"]!="queued": return
            self._run_locked(job_id)

    def _run_locked(self,job_id):
        job=self.get(job_id); job.pop("result",None)
        source=json.loads((self.directory/job_id/"original-input.json").read_text())
        candidate=None; guard=None
        try:
            self._ensure_open()
            self.ledger.mkdir(parents=True,exist_ok=True,mode=0o700)
            with (self.ledger/"experiment.lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                guard=CallGuard(self.ledger,self.policy)
                os.chmod(guard.db,0o600)
                # Check under the common experiment lock before paying for a
                # prefix that cannot finish. Retries are a separate allowance.
                self._unchanged(job)
                self._ensure_open()
                self._preflight_assets(job["work_id"],job["model"],source,job["guard"])
                needed=2 if job.get("human_confirmed_condition") else 3
                self._require_stage_capacity(guard,needed)
                job.update(state="running",stage="statement",message="Читаем условие."); self._save(job)
                confirmed=job.get("human_confirmed_condition")
                condition=deepcopy(condition_input(job["condition_versions"][-1])) if confirmed else None
                solved=job["solver"] if confirmed else None
                condition_ref="human:"+job_id if confirmed else None
                task_type=source["task"]["task_type"]
                sources=self._condition_sources(self.work_store.get(job["work_id"]),source)
                for number in range(0 if confirmed else 2):
                    if number:
                        self._require_stage_capacity(guard,3)
                    previous=None if number==0 else dict(blocks=reading["blocks"],extra_text=reading["extra_text"],uncertainties=reading["uncertainties"],reason=solved.get("reason","empty_answer"),instruction="Повторно проверь только условие, спорные знаки, коэффициенты и границы. Не меняй его ради непустого ответа.")
                    request=self._condition_request(job,source,previous)
                    out=self._call(job,guard,request,lambda v:validate_condition(v,task_type,sources=sources))
                    reading=deepcopy(out.value); stop,condition=classify_condition(reading,task_type)
                    job["condition_versions"].append(dict(reading=reading,solver_input=deepcopy(condition),attempt_id=out.attempt_id)); self._save(job)
                    condition_ref="m14:"+out.attempt_id
                    if stop in {"multiple_tasks","condition_task_mismatch","condition_nonstandard"}:
                        # Never solve a different or extended condition as the standard one.
                        job["solver"]=dict(status="not_run",reason=stop); self._save(job)
                        raise ValueError(stop)
                    if stop: solved=dict(status="suspicious",reason="condition_uncertain")
                    else:
                        job.update(stage="solving",message="Независимо решаем прочитанное условие кодом."); self._save(job)
                        solved=self.solver(condition)
                    job["solver"]=solved; self._save(job)
                    if solved["status"] in {"unsupported","technical_error"}: raise ValueError("solver_unsupported" if solved["status"]=="unsupported" else "technical_error")
                    if solved["status"]=="solved" and not solved.get("empty"): break
                    if number==1: raise ValueError("condition_uncertain" if stop else "condition_suspicious")
                    job.update(stage="statement_recheck",message="Условие требует адресного повторного чтения."); self._save(job)
                source["id"]="fresh-input-"+job_id[1:]; source["revision"]=2
                source["task"]["statement"].update(text=condition["literal"],completeness="confirmed",missing=[],source=dict(kind="human_review" if confirmed else "model_output",ref=condition_ref))
                atomic_json(self.directory/job_id/"verified-input.json",source)
                candidate=base_result(source,dict(id=job_id,configuration=job["configuration"]))
                candidate["created_at"]=job["created_at"]
                manifest=[{k:p[k] for k in ("id","asset_id","sha256","role")} for p in source["pages"]]
                empty={k:[] for k in ("units","corrections","interpretations","branches","assumptions","relations","facts","coverage","issues")}
                reading_request=self._reading_request(job,source,manifest)
                job.update(stage="reading",message="Читаем решение ученика, включая все страницы и рисунки."); self._save(job)
                def accept_reading(value):
                    if job["model"]=="G":
                        internal=reading_schema(sum(p["role"]=="solution" for p in source["pages"]))
                        job["reading_format_diagnostics"]=reading_diagnostics(value,internal)
                        value=decode_reading(value,internal)
                    materialize_compact_reading(value,candidate,source)
                    return value
                out=self._call(job,guard,reading_request,accept_reading)
                candidate=materialize_compact_reading(out.value,candidate,source)
                candidate["criteria"]=rubric(source["task"]["task_type"])
                unresolved=[c for c in candidate["coverage"] if c["score_scope"]=="unresolved"]
                if unresolved:
                    for n,coverage in enumerate(unresolved,1):
                        candidate["clarifications"].append(dict(id=f"read-scope-{n}",unit_ids=[],anchors=[dict(page_id=coverage["page_id"],box=None)],
                            question="Уточните страницу "+coverage["page_id"]+": "+coverage["reason"],options=[],impact="score",
                            reason=coverage["score_scope_reason"],status="open",response=None,actor_ref=None,ambiguity_group_id=None))
                    raise ValueError("reading_needs_clarification")
                candidate["facts"].append(dict(id="condition-key",unit_ids=[],interpretation_ids=[],
                    claim="Ответ для прочитанного условия: "+solved["answer"],method="exact_code",status="supported",
                    reason="Независимое решение прочитанного условия; не подтверждает верность OCR фотографии.",tool_version=solved["version"],
                    depends_on_fact_ids=[],branch_id="main",assumption_ids=[],context_unit_ids=[],claim_kind="completeness",
                    domain=solved["domain"],basis_sha256=contract.reading_digest(candidate),
                    code_check=dict(operation="condition.solve",arguments=condition,evidence=canonical(solved))))
                # Persist the completed reading before asking for a grade.
                atomic_json(self.directory/job_id/"reading.json",candidate)
                reading_features=out.value.get("features",[])
                assessment_request,cases=self._assessment_request(job,source,candidate,manifest,solved,features=reading_features)
                job.update(stage="assessment",message="Проверяем решение по критериям и независимому ключу."); self._save(job)
                out=self._call(job,guard,assessment_request,
                    lambda v:(materialize_compact_assessment(v,candidate,source,cases=cases),v)[1])
                document=dict(response=deepcopy(out.value),reading_sha256=digest(candidate),source_sha256=digest(source),cases=cases)
                atomic_json(self.directory/job_id/"assessment-core.json",document)
                job["report"]=dict(version=REPORT_VERSION,state="deferred",available=True,draft_available="report" in out.value,
                    core_sha256=digest(document),message=DEFERRED_MESSAGE,model_calls=0)
                candidate=materialize_compact_assessment(out.value,candidate,source,cases=cases)
                with self.lock:
                    self._ensure_open()
                    self._unchanged(job)
                    self._stamp(job,candidate,source,guard)
                    job["report"]["accepted_result_sha256"]=digest(candidate)
                    job["result_sha256"]=digest(candidate)
                    atomic_json(self.directory/job_id/"result-1.json",candidate)
                    job.update(state="completed",stage="completed",result_ref="result-1.json",message="Балл определён. Подробные замечания ещё не подготовлены." if candidate["readiness"]["score"]=="ready" else "Проверка выполнена; для балла требуется уточнение.")
        except Exception as error:
            if hasattr(error, "m01_errors"):
                job["m01_errors"] = list(error.m01_errors)
            code=str(error) if isinstance(error,ValueError) else "technical_error"
            if code not in MESSAGES and code not in {"budget_limit","cost_unknown","unresolved_spending","billing_ceiling_unproven","invalid_response","model_mismatch","http_body_limit","terminal_http_error","truncated_response","transport_failure","transport_timeout"}: code="technical_error"
            detail=job.get("response_validation",{}).get("code") if code=="invalid_response" else None
            job["failure_stage"]=job["stage"]
            job.update(state="cancelled" if code=="operation_cancelled" else "needs_clarification" if code in {"multiple_tasks","condition_uncertain","condition_suspicious","condition_nonstandard","condition_task_mismatch","solver_unsupported","reading_needs_clarification"} else "failed",
                stage="stopped",error_code=code,message=MESSAGES.get(detail, MESSAGES.get(code,"Проверка остановлена. Балл не выставлен; сведения сохранены.")))
            from .transport_diagnostics import message_suffix
            job['message']+=message_suffix(job.get('failure_diagnostic'))
            if candidate is not None and guard is not None:
                # Keep the fresh reading and already acquired evidence, never a score on failure.
                candidate["score"].update(status="withheld",value=None,authority="none",source_ref=None,basis_sha256=None)
                candidate["score"]["rationale"]=job["message"]
                candidate["readiness"]=dict(score="pending",feedback="pending"); candidate["status"]="needs_review"
                candidate["issues"].append(dict(code="invalid_model_json" if code=="invalid_response" else "other",message=job["message"],retryable=False,blocks=["score","feedback"]))
                try:
                    self._stamp(job,candidate,source,guard)
                    job["result_sha256"]=digest(candidate)
                    if isinstance(job.get("report"),dict):
                        job["report"].pop("accepted_result_sha256",None)
                        job["report"].pop("result_sha256",None)
                    atomic_json(self.directory/job_id/"result-1.json",candidate); job["result_ref"]="result-1.json"
                except Exception:
                    job["result_save_error"]="result_preservation_failed"
        finally:
            if guard is not None:
                try: job["accounting"]=guard.snapshot(fingerprints=job["call_fingerprints"])
                except (OSError,sqlite3.Error,ValueError): job["accounting_error"]="ledger_unavailable"
            self._save(job)

    def _require_stage_capacity(self,guard,needed):
        with closing(guard._connect()) as con:
            primary=con.execute("SELECT COUNT(*) FROM calls WHERE retry_of IS NULL").fetchone()[0]
            total=con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        if primary+needed>self.primary_limit or total+needed>self.policy.max_total_calls:
            raise ValueError("insufficient_stage_calls")
