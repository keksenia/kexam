"""M04 offline orchestration. No provider client, credentials, eval, or network path.

The input and every accepted result are immutable M01 records. Run lifecycle is
separate from the mathematical result. SQLite transactions publish journal,
artifacts and state together; a process lock protects restart recovery.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from contracts.ege_v1 import validate as contract

VERSION = "m04-offline-3"
RUN_ID = r"R[0-9a-f]{32}"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
READ_SECTIONS = {"units", "interpretations", "branches", "assumptions", "relations"}
GRADE_SECTIONS = {"facts", "findings", "clarifications", "requirements", "case_checks",
                  "scenarios", "ambiguity_groups", "feedback", "score", "issues"}
COMMON_RULES = [
    "Literal reading, interpretation and established facts are different layers.",
    "Unknown reading or unsupported check does not establish a student error.",
    "Assess requirements and evidence under the supplied rubric, not answer equality alone.",
    "Keep a justified score independent of uncertain optional feedback.",
    "Mixed and unknown methods retain the general assessment path.",
]


class PipelineError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strict_json(value):
    """Reject duplicate keys/non-JSON values, bounding the untrusted response."""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise PipelineError("invalid_adapter_response")
            result[key] = item
        return result
    try:
        raw = value if isinstance(value, str) else encoded(value)
        if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
            raise PipelineError("invalid_adapter_response")
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (TypeError, ValueError, RecursionError) as exc:
        raise PipelineError("invalid_adapter_response") from exc


def _validate(result, source=None):
    try:
        errors = contract.validate(result, source)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise PipelineError("invalid_contract") from exc
    if errors:
        raise PipelineError("invalid_contract")


def input_snapshot(work_store, work_id):
    """Adapt M16 without inventing completeness or changing the old manifest."""
    work = work_store.get(work_id)
    pages, images = [], {}
    for role in ("task", "solution"):
        for p in work[role]:
            data = (work_store.root / work_id / f"{role}_{p['page']}.png").read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            page_id = f"{role}-{p['page']}"
            pages.append(dict(id=page_id, role=role, order=p["page"], asset_id=page_id,
                              sha256=sha, width=p["width"], height=p["height"], region=None,
                              selection_note="Вся сохранённая страница M16; полнота попытки не подтверждена."))
            images[sha] = data
    guard = contract.digest({"work": work, "pages": pages})
    source = dict(schema_version="2.0.0", kind="input", id=f"{work_id}-{guard[:16]}",
                  revision=1, created_at=work["created_at"],
                  task=dict(exam_year=work["exam_year"], exam_number=13 if work["task_type"] == "trigonometric_equation" else 15,
                            task_type=work["task_type"], source_label="Локальная загрузка M16",
                            statement=dict(text=work["task_text"], format="plain_latex",
                                           source=dict(kind="user_input", ref=work_id), completeness="unchecked", missing=[])),
                  pages=pages, capture=dict(pages_complete="unknown", student_attempt="unknown"),
                  privacy=dict(status="user_confirmed" if work["redaction_confirmed_by_user"] else "unchecked", reviewer_ref=None),
                  provenance=dict(source=dict(kind="user_input", ref=work_id), dataset_role="dev", duplicate_group=None))
    _validate(source)
    return source, guard, images


class RunStore:
    """One durable DB per M16 storage; each method opens/closes its connection."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / ".m04-runs.sqlite3"
        with self.connection(True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, state TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, input_json TEXT NOT NULL, guard TEXT NOT NULL,
                    configuration_json TEXT NOT NULL, request_key TEXT UNIQUE,
                    parent_id TEXT, error_code TEXT);
                CREATE TABLE IF NOT EXISTS artifacts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    at TEXT NOT NULL, stage TEXT NOT NULL, event TEXT NOT NULL, code TEXT);
                CREATE TABLE IF NOT EXISTS assets (sha256 TEXT PRIMARY KEY, data BLOB NOT NULL);
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, run_id, stage, event, code=None):
        db.execute("INSERT INTO events(run_id,at,stage,event,code) VALUES(?,?,?,?,?)",
                   (run_id, now(), stage, event, code))

    @staticmethod
    def _artifact(db, run_id, kind, stage, payload):
        db.execute("INSERT INTO artifacts(run_id,kind,stage,created_at,sha256,payload_json) VALUES(?,?,?,?,?,?)",
                   (run_id, kind, stage, now(), contract.digest(payload), encoded(payload)))

    def create(self, work_id, source, guard, images, configuration, request_key=None, parent_id=None):
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key)):
            raise PipelineError("invalid_request_key")
        if parent_id is not None and (not isinstance(parent_id, str) or not re.fullmatch(RUN_ID, parent_id)):
            raise PipelineError("invalid_retry_parent")
        with self.connection(True) as db:
            if request_key:
                existing = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
                if existing:
                    if (existing["work_id"] != work_id or existing["guard"] != guard
                            or existing["input_json"] != encoded(source)
                            or existing["parent_id"] != parent_id
                            or existing["configuration_json"] != encoded(configuration)):
                        raise PipelineError("idempotency_conflict")
                    return existing["id"], False
            if parent_id:
                parent = db.execute("SELECT * FROM runs WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent["work_id"] != work_id or parent["state"] not in TERMINAL:
                    raise PipelineError("invalid_retry_parent")
            run_id, at = "R" + uuid.uuid4().hex, now()
            db.execute("INSERT INTO runs(id,work_id,state,created_at,updated_at,input_json,guard,configuration_json,request_key,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (run_id, work_id, "queued", at, at, encoded(source), guard, encoded(configuration), request_key, parent_id))
            for sha, data in images.items():
                db.execute("INSERT OR IGNORE INTO assets VALUES(?,?)", (sha, data))
            self._event(db, run_id, "run", "queued")
            return run_id, True

    def get(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(RUN_ID, run_id):
            raise FileNotFoundError
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise FileNotFoundError
            result = dict(row)
            result["input"] = json.loads(result.pop("input_json"))
            result["configuration"] = json.loads(result.pop("configuration_json"))
            result["cancel_requested"] = bool(result["cancel_requested"])
            result["events"] = [dict(r) for r in db.execute("SELECT at,stage,event,code FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            snapshots = db.execute("SELECT payload_json FROM artifacts WHERE run_id=? AND kind='result' ORDER BY seq", (run_id,)).fetchall()
            result["result_versions"] = [json.loads(r[0]) for r in snapshots]
            result["result"] = result["result_versions"][-1] if snapshots else None
            result["diagnostic_only"] = True
            return result

    def list(self, work_id=None):
        with self.connection() as db:
            query = "SELECT id FROM runs" + (" WHERE work_id=?" if work_id else "") + " ORDER BY created_at DESC,id DESC"
            rows = db.execute(query, (work_id,) if work_id else ()).fetchall()
        return [self.get(row[0]) for row in rows]

    def assets(self, source):
        with self.connection() as db:
            result = {}
            for p in source["pages"]:
                row = db.execute("SELECT data FROM assets WHERE sha256=?", (p["sha256"],)).fetchone()
                if row is None or hashlib.sha256(row[0]).hexdigest() != p["sha256"]:
                    raise PipelineError("asset_integrity")
                result[p["asset_id"]] = row[0]
            return result

    def start(self, run_id):
        with self.connection(True) as db:
            changed = db.execute("UPDATE runs SET state='running',updated_at=? WHERE id=? AND state='queued' AND cancel_requested=0", (now(), run_id)).rowcount
            if changed:
                self._event(db, run_id, "run", "running")
            return bool(changed)

    def record(self, run_id, kind, stage, payload):
        with self.connection(True) as db:
            row = db.execute("SELECT state,cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["state"] != "running" or row["cancel_requested"]:
                raise PipelineError("cancelled")
            self._artifact(db, run_id, kind, stage, payload)
            self._event(db, run_id, stage, kind + "_saved")
            db.execute("UPDATE runs SET updated_at=? WHERE id=?", (now(), run_id))

    def finish(self, run_id, state, code=None):
        with self.connection(True) as db:
            row = db.execute("SELECT state,cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["state"] in TERMINAL:
                return
            if row["cancel_requested"]:
                state, code = "cancelled", "cancelled"
            db.execute("UPDATE runs SET state=?,error_code=?,updated_at=? WHERE id=?", (state, code, now(), run_id))
            self._event(db, run_id, "run", state, code)

    def cancel(self, run_id):
        self.get(run_id)
        with self.connection(True) as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row["state"] in TERMINAL:
                return
            db.execute("UPDATE runs SET cancel_requested=1,updated_at=? WHERE id=?", (now(), run_id))
            self._event(db, run_id, "run", "cancel_requested")
            if row["state"] == "queued":
                db.execute("UPDATE runs SET state='cancelled',error_code='cancelled' WHERE id=?", (run_id,))
                self._event(db, run_id, "run", "cancelled", "cancelled")

    def recover(self):
        with self.connection(True) as db:
            rows = db.execute("SELECT id FROM runs WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                db.execute("UPDATE runs SET state='interrupted',error_code='process_interrupted',updated_at=? WHERE id=?", (now(), row[0]))
                self._event(db, row[0], "run", "interrupted", "process_interrupted")
            return len(rows)


class OfflineMockAdapter:
    """Honest fallback on arbitrary uploaded pages: it does not pretend to read them."""
    id = "offline-mock"
    version = "1"
    network_enabled = False

    def read(self, request, image):
        return {"units": [], "interpretations": [], "features": ["unknown_method"],
                "coverage": {"page_id": request["page"]["id"], "status": "not_processed",
                             "reason": "Диагностический mock не читает рукопись.", "score_scope": "unresolved",
                             "score_scope_reason": "Реальное чтение не подключено."}}

    def assess(self, request):
        return {"issues": [{"code": "not_run", "message": "Диагностический запуск завершён; реальное чтение и оценивание не подключены.",
                            "retryable": False, "blocks": ["score", "feedback"]}]}


def base_result(source, run):
    result = {key: [] for key in ("units", "corrections", "interpretations", "facts", "findings", "clarifications",
              "criteria", "feedback", "assumptions", "branches", "relations", "requirements", "case_checks",
              "scenarios", "ambiguity_groups", "human_actions")}
    result.update(schema_version="2.0.0", kind="result", id="pending", supersedes=None, created_at=now(),
                  input_ref=dict(id=source["id"], revision=source["revision"], sha256=contract.digest(source)),
                  status="needs_review", readiness=dict(score="pending", feedback="pending"),
                  run=dict(id=run["id"], mode="demo", source_ref="m04:diagnostic-only", pipeline_version=VERSION,
                           configuration_sha256=contract.digest(run["configuration"]), configuration_ref="run:configuration",
                           calls=[], cost=dict(status="not_incurred", rub="0"), timing=dict(wall_ms=None, human_active_ms=None)),
                  score=dict(status="withheld", value=None, max_value=2, criterion_ids=[], rationale="Оценивание не выполнено.",
                             evidence_unit_ids=[], finding_ids=[], authority="none", source_ref=None, requirement_ids=[],
                             case_check_ids=[], ambiguity_group_ids=[], basis_sha256=None),
                  issues=[dict(code="not_run", message="Проверка ещё не завершена.", retryable=False, blocks=["score", "feedback"])],
                  coverage=[dict(page_id=p["id"], status="not_processed", reason="Страница ещё не прочитана.",
                                 score_scope="unresolved", score_scope_reason="Объём не установлен.")
                            for p in source["pages"] if p["role"] == "solution"])
    result["branches"] = [dict(id="main", parent_id=None, label="Выбранная попытка", unit_ids=[], assumption_ids=[], status="active")]
    return result


def compact_context(result):
    """Keep mathematical inputs/output once; full exact evidence stays in the journal.

    This is a model-facing projection, never an M01 replacement or a new source
    of authority. A journal reference resolves through the saved run/result and
    stable fact ID; the accepted result itself retains the unabridged evidence.
    """
    keys = ("units", "corrections", "interpretations", "branches", "assumptions", "relations", "facts", "coverage", "issues")
    context = deepcopy({key: result[key] for key in keys})
    for fact in context["facts"]:
        fact.pop("basis_sha256", None)
        check = fact.get("code_check")
        if not isinstance(check, dict):
            continue
        try:
            evidence = json.loads(check.get("evidence", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict):
            continue
        request, response = evidence.get("k01_request"), evidence.get("k01_response")
        if (not isinstance(request, dict) or not isinstance(response, dict)
                or not isinstance(request.get("context"), dict)):
            continue
        check["evidence"] = dict(computed=response.get("computed"), reason_code=response.get("reason_code"),
                                 details=response.get("evidence", []), bindings=request["context"].get("bindings", []),
                                 execution=deepcopy(evidence.get("execution")),
                                 journal_ref=f"run:{result['run']['id']}/facts/{fact['id']}/code_check/evidence")
    return context


def _operation_dependencies(context, result):
    """Find the fact closure used by explicit context, bindings and premises.

    Reading objects are already part of reading_digest. Facts are not, so a
    cache key must separately include the facts reached through that reading.
    Missing IDs stay in the closure and can never silently become evidence.
    """
    maps = {key: {item["id"]: item for item in result[key]}
            for key in ("facts", "interpretations", "assumptions", "branches")}
    pending = []
    for key, section in (("depends_on_fact_ids", "facts"), ("interpretation_ids", "interpretations"),
                         ("assumption_ids", "assumptions")):
        ids = context.get(key, [])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise PipelineError("invalid_operation_context")
        pending.extend((section, i) for i in ids)
    branch = context.get("branch_id")
    if isinstance(branch, str):
        pending.append(("branches", branch))
    bindings = context.get("bindings", [])
    if not isinstance(bindings, list):
        raise PipelineError("invalid_operation_context")
    for binding in bindings:
        if not isinstance(binding, dict):
            raise PipelineError("invalid_operation_context")
        section = {"fact": "facts", "interpretation": "interpretations", "assumption": "assumptions"}.get(binding.get("source_kind"))
        if section:
            if not isinstance(binding.get("source_ref"), str):
                raise PipelineError("invalid_operation_context")
            pending.append((section, binding["source_ref"]))
    seen, facts = set(), {}
    while pending:
        section, identity = pending.pop()
        if (section, identity) in seen:
            continue
        seen.add((section, identity))
        item = maps[section].get(identity)
        if section == "facts":
            facts[identity] = item
        if item is None:
            continue
        pending.extend(("facts", i) for i in item.get("depends_on_fact_ids", []))
        pending.extend(("interpretations", i) for i in item.get("interpretation_ids", []))
        pending.extend(("assumptions", i) for i in item.get("assumption_ids", []))
        source_section = {"derived_fact": "facts", "human_interpretation": "interpretations"}.get(item.get("origin"))
        if source_section:
            pending.append((source_section, item["source_ref"]))
        if section == "branches" and item.get("parent_id"):
            pending.append(("branches", item["parent_id"]))
    return facts


def _operation_issue(operation_id, reason):
    """A missing local proof is a system limitation, never a student error."""
    timed_out = reason in {"timeout", "operation_timeout", "work_budget_exhausted"}
    return dict(code="timeout" if timed_out else "unsupported_math",
                message=f"Проверка {operation_id} не установила факт ({reason}); зависимые выводы требуют другого обоснования.",
                retryable=timed_out, blocks=[])


def _operation_failure_reason(exc):
    """Preserve known machine codes without logging arbitrary exception text."""
    if isinstance(exc, TimeoutError):
        return "operation_timeout"
    code = getattr(exc, "code", None)
    known = {"timeout", "resource_limit", "invalid_operation_context", "invalid_input",
             "worker_failed", "worker_unavailable", "invalid_worker_response", "configuration_changed"}
    return code if isinstance(code, str) and code in known else "operation_failed"


class Pipeline:
    """Trusted local adapters/operations are injected by code, never by request text.

    Operations advertise id/version/features and implement execute(request,
    context) -> a fresh M01 fact without basis_sha256. A resource-limited
    operation instead supplies resource_limits and execute_bounded(...,
    timeout_ms), with optional prepare_execution/close lifecycle hooks and a
    shared execution_group. source_sha256 records executable identity.
    The default registry stays empty; exact operations require explicit trusted
    injection. A supported fact reaches assessment without a model recheck.
    """
    def __init__(self, work_store, adapter=None, operations=(), criteria=(), allow_synthetic=False):
        self.work_store = work_store
        self.adapter = adapter or OfflineMockAdapter()
        if self.adapter.network_enabled is not False:
            raise PipelineError("network_adapter_disabled")
        self.operations = {operation.id: operation for operation in operations}
        if len(self.operations) != len(operations):
            raise PipelineError("duplicate_operation")
        self.criteria = deepcopy(list(criteria))
        self.allow_synthetic = allow_synthetic
        self.store = RunStore(work_store.root)
        self._lock_file = (work_store.root / ".m04-worker.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise PipelineError("worker_already_running") from exc
        self.store.recover()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="m04-offline")
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for run in self.store.list():
            if run["state"] not in TERMINAL:
                self.store.cancel(run["id"])
        self.executor.shutdown(wait=True, cancel_futures=True)
        for operation in self.operations.values():
            close = getattr(operation, "close", None)
            if callable(close):
                # Shared resource owners expose idempotent close. Cleanup cannot
                # prevent durable run finalization or release of the store lock.
                try:
                    close()
                except Exception:
                    pass
        for run in self.store.list():
            if run["state"] not in TERMINAL:
                self.store.finish(run["id"], "cancelled", "shutdown")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()

    def configuration(self):
        return dict(pipeline=VERSION, contract="2.0.0", adapter=dict(id=self.adapter.id, version=self.adapter.version),
                    adapter_settings=deepcopy(getattr(self.adapter, "settings", {})),
                    engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    contract_schema_sha256=hashlib.sha256(Path(contract.__file__).with_name("schema.json").read_bytes()).hexdigest(),
                    operations=[dict(id=o.id, version=o.version, features=sorted(o.features),
                                     source_sha256=getattr(o, "source_sha256", None),
                                     resource_limits=deepcopy(getattr(o, "resource_limits", {})),
                                     execution_group=getattr(o, "execution_group", o.id),
                                     execution_profile=getattr(o, "execution_profile", None)) for o in self.operations.values()],
                    criteria=self.criteria, common_rules=COMMON_RULES, network_enabled=False, diagnostic_only=True)

    def create(self, work_id, *, request_key=None, parent_id=None, background=True, synthetic=False):
        if self.closed:
            raise PipelineError("worker_closed")
        source, guard, images = input_snapshot(self.work_store, work_id)
        if synthetic:
            if not self.allow_synthetic:
                raise PipelineError("synthetic_fixture_disabled")
            source["id"] += "-synthetic"
            source["task"]["source_label"] = "SYNTHETIC M04 fixture — not a student work"
            source["task"]["statement"]["completeness"] = "confirmed"
            source["task"]["statement"]["source"] = dict(kind="synthetic", ref="m04:generated-fixture")
            source["capture"] = dict(pages_complete="confirmed", student_attempt="finished")
            source["provenance"] = dict(source=dict(kind="synthetic", ref="m04:generated-fixture"), dataset_role="synthetic", duplicate_group=None)
            _validate(source)
        run_id, fresh = self.store.create(work_id, source, guard, images, self.configuration(), request_key, parent_id)
        if fresh and background:
            self.executor.submit(self.execute, run_id)
        return self.view(run_id)

    def view(self, run_id):
        run = self.store.get(run_id)
        try:
            _, guard, _ = input_snapshot(self.work_store, run["work_id"])
            run["source_matches"] = guard == run["guard"]
        except (OSError, ValueError, KeyError):
            run["source_matches"] = False
        # Historical snapshots stay accessible; never present them as a current live assessment.
        run["result_current"] = bool(run["result"] is not None and run["source_matches"])
        return run

    def _guard(self, run):
        current = self.store.get(run["id"])
        if current["cancel_requested"]:
            raise PipelineError("cancelled")
        _, guard, _ = input_snapshot(self.work_store, run["work_id"])
        if guard != run["guard"]:
            raise PipelineError("input_changed")
        if run["configuration"] != self.configuration():
            raise PipelineError("configuration_changed")

    def _publish(self, run, result, stage, started):
        self._guard(run)
        candidate = deepcopy(result)
        previous = self.store.get(run["id"])["result"]
        candidate.update(id="V" + uuid.uuid4().hex, supersedes=previous["id"] if previous else None, created_at=now())
        candidate["run"]["timing"]["wall_ms"] = round((time.monotonic() - started) * 1000)
        _validate(candidate, run["input"])
        self.store.record(run["id"], "result", stage, candidate)
        return candidate

    def _invoke(self, run, stage, request, call):
        self._guard(run)
        self.store.record(run["id"], "request", stage, request)
        response = _strict_json(call(deepcopy(request)))
        self._guard(run)
        self.store.record(run["id"], "response", stage, response)
        return response

    def execute(self, run_id):
        if not self.store.start(run_id):
            return self.view(run_id)
        started, run = time.monotonic(), self.store.get(run_id)
        source = run["input"]
        result = base_result(source, run)
        result["criteria"] = deepcopy(self.criteria)
        features, operation_requests = set(), []
        try:
            self._guard(run)
            assets = self.store.assets(source)
            for page in source["pages"]:
                if page["role"] != "solution":
                    continue
                stage = "reading:" + page["id"]
                request = dict(task=source["task"], page=page, task_pages=[p for p in source["pages"] if p["role"] == "task"],
                               context=compact_context(result), rules=COMMON_RULES,
                               synthetic=source["provenance"]["dataset_role"] == "synthetic")
                # Exact saved bytes; condition images are also available without lossy preprocessing.
                attached = {p["asset_id"]: assets[p["asset_id"]] for p in source["pages"] if p["role"] == "task" or p["id"] == page["id"]}
                response = self._invoke(run, stage, request, lambda r: self.adapter.read(r, attached))
                allowed = READ_SECTIONS | {"coverage", "features", "operation_requests"}
                if set(response) - allowed or "coverage" not in response:
                    raise PipelineError("invalid_adapter_response")
                candidate = deepcopy(result)
                for key in READ_SECTIONS:
                    values = response.get(key, [])
                    if not isinstance(values, list):
                        raise PipelineError("invalid_adapter_response")
                    if key == "branches":
                        # Explicit branch IDs permit multi-page/case context; main is app-owned.
                        if any(b.get("id") == "main" for b in values):
                            raise PipelineError("reserved_branch")
                    candidate[key].extend(values)
                for key in ("units", "interpretations", "relations"):
                    for item in candidate[key][len(result[key]):]:
                        if "source" in item:
                            raise PipelineError("adapter_provenance_override")
                        item["source"] = dict(kind="synthetic" if request["synthetic"] else "model_output", ref=stage)
                candidate["branches"][0]["unit_ids"] = [u["id"] for u in candidate["units"]]
                cov = response["coverage"]
                if not isinstance(cov, dict) or cov.get("page_id") != page["id"]:
                    raise PipelineError("invalid_adapter_response")
                candidate["coverage"] = [cov if c["page_id"] == page["id"] else c for c in candidate["coverage"]]
                flags = response.get("features", [])
                requests = response.get("operation_requests", [])
                if not isinstance(flags, list) or not all(isinstance(f, str) and len(f) <= 80 for f in flags) or not isinstance(requests, list):
                    raise PipelineError("invalid_adapter_response")
                # Claiming human confirmation from an adapter would invent authority.
                if any(i["status"] != "proposed" for i in candidate["interpretations"]):
                    raise PipelineError("adapter_provenance_override")
                result = self._publish(run, candidate, stage, started)
                features.update(flags)
                operation_requests.extend(requests)
            selected = [o.id for o in self.operations.values() if set(o.features) & features]
            plan = dict(features=sorted(features), selected=selected, check_outcomes=[], general_assessment=True,
                        reason="Признаки выбирают доступные операции, но не балл и не вид ошибки.")
            self.store.record(run_id, "plan", "checks", plan)
            # Startup, execution and intervening validation/journal overhead all
            # consume this run's shared check budget. Final storage/assessment are
            # not computation and may finish after it; no later check is dispatched.
            # Retrying creates a new, explicitly logged run.
            profiles = [getattr(self.operations[oid], "resource_limits", {}) for oid in selected]
            work_limits = [profile["wall_ms_per_work"] for profile in profiles if "wall_ms_per_work" in profile]
            checks_deadline = time.monotonic() + min(work_limits) / 1000 if work_limits else None
            completed_checks, operation_issues = {}, []
            for request_index, request in enumerate(operation_requests):
                if (not isinstance(request, dict) or set(request) != {"operation", "version", "arguments", "context"}
                        or not isinstance(request["operation"], str) or not isinstance(request["version"], str)
                        or not isinstance(request["arguments"], dict) or not isinstance(request["context"], dict)):
                    # The invalid individual request is logged without inventing an
                    # operation identity or a fact; independent requests still run.
                    outcome = dict(operation=None, request_index=request_index, status="unknown", reason="invalid_operation_request")
                    self.store.record(run_id, "check", "checks", outcome)
                    plan["check_outcomes"].append(outcome)
                    issue = _operation_issue("неопределённая", "invalid_operation_request")
                    if issue not in operation_issues:
                        operation_issues.append(issue)
                    candidate = deepcopy(result)
                    candidate["issues"] = deepcopy(operation_issues)
                    result = self._publish(run, candidate, "checks", started)
                    continue
                operation = self.operations.get(request["operation"])
                reason, cache_key, fact = None, None, None
                if operation is None or operation.id not in selected or request["version"] != operation.version:
                    reason = "operation_unavailable"
                else:
                    try:
                        dependencies = _operation_dependencies(request["context"], result)
                        cache_key = contract.digest(dict(request=request, reading_sha256=contract.reading_digest(result),
                                                         dependencies=dependencies))
                        if cache_key in completed_checks:
                            outcome = deepcopy(completed_checks[cache_key])
                            outcome.update(request_index=request_index, reused=True)
                            plan["check_outcomes"].append(outcome)
                            self.store.record(run_id, "check", "checks", outcome)
                            continue
                        if any(f is None or f.get("status") != "supported" for f in dependencies.values()):
                            reason = "dependency_unavailable"
                    except (PipelineError, TypeError, ValueError):
                        reason = "invalid_operation_context"
                if reason is None:
                    limits = getattr(operation, "resource_limits", {})
                    prepare = getattr(operation, "prepare_execution", None)
                    group = getattr(operation, "execution_group", operation.id)
                    if limits and callable(prepare):
                        # A living shared worker returns immediately. A worker
                        # killed by an earlier failure may be replaced for this
                        # different request, within the SAME remaining budget.
                        # Identical failures were already deduplicated above.
                        self._guard(run)
                        try:
                            remaining_ms = (checks_deadline - time.monotonic()) * 1000
                            if remaining_ms <= 0:
                                raise TimeoutError()
                            prepare(timeout_ms=remaining_ms)
                        except Exception as exc:
                            reason = _operation_failure_reason(exc)
                        self._guard(run)
                        self.store.record(run_id, "check", "checks:prepare", dict(execution_group=group,
                                          status="ready" if reason is None else "unknown", reason=reason))
                    remaining_ms = (checks_deadline - time.monotonic()) * 1000 if checks_deadline is not None else None
                    if limits and remaining_ms is not None and remaining_ms <= 0:
                        reason = "work_budget_exhausted"
                    if reason is None:
                        stage = "check:" + operation.id
                        # Guards and storage failures stay fatal. Only operation
                        # execution/JSON/contract failures are local and recoverable.
                        self._guard(run)
                        self.store.record(run_id, "request", stage, request)
                        try:
                            if limits:
                                remaining_ms = (checks_deadline - time.monotonic()) * 1000
                                timeout_ms = min(limits["wall_ms_per_operation"], remaining_ms)
                                if timeout_ms <= 0:
                                    raise TimeoutError()
                                fact = operation.execute_bounded(deepcopy(request), deepcopy(result), timeout_ms=timeout_ms)
                            else:
                                fact = operation.execute(deepcopy(request), deepcopy(result))
                            fact = _strict_json(fact)
                            if limits and time.monotonic() >= checks_deadline:
                                reason = "work_budget_exhausted"
                            if fact.get("method") not in {"exact_code", "counterexample"} or "basis_sha256" in fact:
                                raise PipelineError("invalid_operation_response")
                            if (fact.get("tool_version") != operation.version or fact.get("code_check", {}).get("operation") != operation.id
                                    or fact.get("code_check", {}).get("arguments") != request["arguments"]
                                    or not set(request["context"].get("depends_on_fact_ids", [])) <= set(fact.get("depends_on_fact_ids", []))):
                                raise PipelineError("invalid_operation_response")
                            fact["basis_sha256"] = contract.reading_digest(result)
                            candidate = deepcopy(result)
                            candidate["facts"].append(fact)
                            _validate(candidate, run["input"])
                            if limits and time.monotonic() >= checks_deadline:
                                reason = "work_budget_exhausted"
                        except Exception as exc:
                            reason = _operation_failure_reason(exc)
                        self._guard(run)
                        if reason is None:
                            raw_fact = deepcopy(fact)
                            raw_fact.pop("basis_sha256")
                            self.store.record(run_id, "response", stage, raw_fact)
                            result = self._publish(run, candidate, "checks", started)
                if reason is None:
                    outcome = dict(operation=operation.id, status=fact["status"], fact_id=fact["id"], request_index=request_index)
                else:
                    outcome = dict(operation=request["operation"], status="unknown", reason=reason, request_index=request_index)
                    issue = _operation_issue(request["operation"], reason)
                    if issue not in operation_issues:
                        operation_issues.append(issue)
                if cache_key is not None:
                    completed_checks[cache_key] = deepcopy(outcome)
                plan["check_outcomes"].append(outcome)
                self.store.record(run_id, "check", "checks", outcome)
                if reason is not None:
                    candidate = deepcopy(result)
                    candidate["issues"] = deepcopy(operation_issues)
                    result = self._publish(run, candidate, "checks", started)
            # Malformed/unavailable requests must remain visible even when the
            # assessor supplies issues=[] or fails before publishing its response.
            if operation_issues and result["issues"] != operation_issues:
                candidate = deepcopy(result)
                candidate["issues"] = deepcopy(operation_issues)
                result = self._publish(run, candidate, "checks", started)
            self.store.record(run_id, "plan", "checks", plan)
            request = dict(task=source["task"], capture=source["capture"], context=compact_context(result),
                           criteria=self.criteria, plan=plan, rules=COMMON_RULES,
                           synthetic=source["provenance"]["dataset_role"] == "synthetic")
            response = self._invoke(run, "assessment", request, self.adapter.assess)
            if set(response) - GRADE_SECTIONS:
                raise PipelineError("invalid_adapter_response")
            candidate = deepcopy(result)
            candidate["issues"] = deepcopy(operation_issues)
            for key, value in response.items():
                if key == "facts":
                    for fact in value:
                        if fact.get("method") != "model" or "basis_sha256" in fact:
                            raise PipelineError("adapter_provenance_override")
                        fact["basis_sha256"] = contract.reading_digest(candidate)
                    candidate["facts"].extend(value)
                elif key == "issues":
                    if not isinstance(value, list):
                        raise PipelineError("invalid_adapter_response")
                    candidate["issues"].extend(issue for issue in value if issue not in candidate["issues"])
                elif key == "score":
                    forbidden = {"basis_sha256", "authority", "source_ref", "max_value"}
                    if set(value) & forbidden or value.get("status") not in {"proposed", "withheld"}:
                        raise PipelineError("adapter_provenance_override")
                    candidate["score"].update(value)
                    if candidate["score"]["value"] is not None:
                        candidate["score"].update(authority="model", source_ref="m04:synthetic-assessment")
                else:
                    candidate[key] = value
            # This offline build may exercise a full score only on its synthetic fixture.
            if source["provenance"]["dataset_role"] != "synthetic" and candidate["score"]["value"] is not None:
                raise PipelineError("diagnostic_score_forbidden")
            basis = contract.assessment_digest(candidate)
            if candidate["score"]["value"] is not None:
                candidate["score"]["basis_sha256"] = basis
            for feedback in candidate["feedback"]:
                if "basis_sha256" in feedback:
                    raise PipelineError("adapter_provenance_override")
                feedback["basis_sha256"] = basis if feedback["status"] == "ready" else None
            score_ready = candidate["score"]["value"] is not None and candidate["score"]["status"] == "proposed"
            ready_feedback = [f for f in candidate["feedback"] if f["status"] == "ready"]
            open_questions = [q for q in candidate["clarifications"] if q["status"] != "answered"]
            feedback_ready = (len(ready_feedback) == len(candidate["feedback"])
                              and not any("feedback" in i["blocks"] for i in candidate["issues"])
                              and not any(q["impact"] in {"substantive_feedback", "both"} for q in open_questions))
            candidate["readiness"] = dict(score="ready" if score_ready else "pending",
                                           feedback="ready" if feedback_ready else "partial" if ready_feedback else "pending")
            if open_questions:
                candidate["status"] = "needs_fragment"
            elif candidate["readiness"] == {"score": "ready", "feedback": "ready"} and not candidate["issues"]:
                candidate["status"] = "ready"
            else:
                candidate["status"] = "needs_review"
                if not candidate["issues"]:
                    candidate["issues"] = [dict(code="not_run", message="Не все части проверки готовы.", retryable=False,
                                                blocks=[])]
            self._publish(run, candidate, "assessment", started)
            self._guard(run)
            self.store.finish(run_id, "completed")
        except Exception as exc:
            code = exc.code if isinstance(exc, PipelineError) else "timeout" if isinstance(exc, TimeoutError) else "stage_failed"
            self.store.finish(run_id, "cancelled" if code == "cancelled" else "failed", code)
        return self.view(run_id)
