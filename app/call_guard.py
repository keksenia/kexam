"""M14: durable, shared admission control. No network client or credentials.

The transport is ONE request with SDK retries disabled. A live approval must bind
each exact request to an independently established billing ceiling. This module
cannot establish a provider's ceiling or prevent a provider violating it.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid
from .transport_diagnostics import detail as diagnostic_detail, exception_detail

VERSION = "m14-1.3.1"
MICRO = 1_000_000
STAGES = {"statement_ocr", "solution_ocr", "grading", "feedback", "other"}
VERSION_KEYS = {"contract", "pipeline", "prompt", "code", "validator"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_model_json(content):
    """Accept one complete JSON document, optionally in one Markdown fence.

    No extraction from prose, repairs, duplicate-key removal or schema bypass.
    The original response remains in the immutable accounting artifact.
    """
    raw=content.strip()
    if raw.startswith("```"):
        lines=raw.splitlines()
        if len(lines)<3 or lines[0].lower() not in {"```json","```"} or lines[-1]!="```":
            raise ValueError("invalid_json_wrapper")
        raw="\n".join(lines[1:-1])
    return json.loads(raw,object_pairs_hook=unique_object,
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def money_string(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def positive_int(value):
    return type(value) is int and 0 < value < 2**53


def kopecks_to_micro(value):
    """Convert reported or estimated kopecks; missing values never imply zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value)
        if len(text) > 128:
            return None
        kopecks = Decimal(text)
        if not kopecks.is_finite() or kopecks < 0 or kopecks >= Decimal(2**53) / 10_000:
            return None
        if kopecks == 0:
            return 0
        # M01 accepts eight fractional RUB digits (six in kopecks). Reject
        # unsupported precision instead of underflowing or rounding actual cost.
        _, digits, exponent = kopecks.as_tuple()
        while digits and digits[-1] == 0:
            digits, exponent = digits[:-1], exponent + 1
        if exponent < -6:
            return None
        with localcontext() as context:
            context.prec = 160
            amount = kopecks * 10_000
            return int(amount.to_integral_value(rounding=ROUND_CEILING))
    except (DecimalException, ValueError):
        return None


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    mode: str = "live"
    approval_ref: str | None = None
    budget_micro_rub: int = 0
    max_attempts: int = 1
    max_total_calls: int = 0
    max_output_tokens: int = 0
    max_response_bytes: int = 0
    timeout_seconds: float = 0
    operation_timeout_seconds: float = 0
    expires_at: float = 0
    # Exact request fingerprint -> {micro_rub, evidence_ref}. No tariff defaults.
    ceilings: dict = field(default_factory=dict)
    # Rules cover an approved bounded class, allowing OCR-dependent requests
    # under the SAME immutable global approval. Bytes alone do not prove price.
    ceiling_rules: dict = field(default_factory=dict)
    # Explicit user override: stop on settled spend, allow the last call to
    # exceed the remainder. This is not a verified per-call billing ceiling.
    budget_mode: str = "hard_ceiling"
    dispatch_rules: dict = field(default_factory=dict)

    def validate(self):
        if self.enabled is not True:
            return "paid_calls_disabled"
        if not isinstance(self.mode, str) or self.mode not in {"live", "fixture"} or not isinstance(self.approval_ref, str) or not self.approval_ref.strip():
            return "approval_missing"
        if (self.budget_mode not in {"hard_ceiling", "settled_spend"}
                or not isinstance(self.dispatch_rules, dict)
                or not isinstance(self.ceilings, dict) or not isinstance(self.ceiling_rules, dict)):
            return "billing_ceiling_unproven"
        if not all(positive_int(x) for x in (self.budget_micro_rub,
                self.max_attempts, self.max_total_calls, self.max_output_tokens,
                self.max_response_bytes)):
            return "limits_missing"
        if not isinstance(self.timeout_seconds, (float, int)) or isinstance(self.timeout_seconds, bool):
            return "limits_missing"
        if not isinstance(self.operation_timeout_seconds, (int, float)) or isinstance(self.operation_timeout_seconds, bool):
            return "limits_missing"
        if not isinstance(self.expires_at, (int, float)) or isinstance(self.expires_at, bool):
            return "approval_expired_or_invalid"
        if not 0 < self.timeout_seconds <= self.operation_timeout_seconds <= 3600 or not time.time() < self.expires_at < float("inf"):
            return "approval_expired_or_invalid"
        return None


@dataclass(frozen=True)
class StageRequest:
    stage: str
    provider: str
    payload: dict
    # Full relevant source/context, including reading/corrections, criteria and
    # transformed image bytes or their hashes. Paths alone are not an identity.
    context: dict
    versions: dict
    condition_complete: bool

    def document(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return digest(self.document())

    def validate(self, policy):
        if self.condition_complete is not True:
            return "incomplete_condition"
        if not isinstance(self.stage, str) or self.stage not in STAGES or not isinstance(self.provider, str) or not self.provider or not isinstance(self.context, dict) or not self.context:
            return "invalid_request"
        if not isinstance(self.versions, dict) or set(self.versions) != VERSION_KEYS or not all(isinstance(v, str) and v for v in self.versions.values()):
            return "versions_missing"
        if not isinstance(self.payload, dict) or not isinstance(self.payload.get("model"), str) or not self.payload["model"]:
            return "invalid_request"
        if not positive_int(self.payload.get("max_tokens")) or self.payload["max_tokens"] > policy.max_output_tokens:
            return "output_limit_invalid"
        # A model call cannot introduce other separately billed tools, streaming,
        # fallback models, or an alternative limit behind the admission boundary.
        if any(k in self.payload for k in ("tools", "models", "max_completion_tokens")) or self.payload.get("stream", False) is not False:
            return "unsupported_request_option"
        if not isinstance(self.payload.get("messages"), list) or not self.payload["messages"]:
            return "invalid_request"
        return None


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    usage: dict | None = None
    cost_kopecks: object = None
    model: str | None = None
    response_id: str | None = None
    finish_reason: str = "stop"
    http_status: int = 200
    retry_after_seconds: float = 0
    provider_finish_reason: str | None = None
    # Native APIs may expose token usage without a per-request invoice. Keep
    # the tariff estimate distinct from the historical provider-reported cost.
    cost_basis: str = "provider_report"
    tariff_ref: str | None = None
    provider_metadata: dict | None = None


@dataclass(frozen=True)
class Outcome:
    status: str
    attempt_id: str | None = None
    value: object = None
    reused: bool = False
    # M14 does not assign student scores. Its value is a validated stage output.
    assessment: None = None
    diagnostic: dict | None = None


class CallGuard:
    def __init__(self, root, policy=None):
        self.root = Path(root).resolve()
        # Snapshot nested config too: callers cannot mutate the active approval.
        self._policy_text = canonical(asdict(policy or Policy()))
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.root / "private_artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.db = self.root / "spending.sqlite3"
        with closing(self._connect()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, number INTEGER NOT NULL,
                    state TEXT NOT NULL, status TEXT NOT NULL, reserve INTEGER NOT NULL,
                    cost INTEGER, usage TEXT, retry_of TEXT, created REAL NOT NULL,
                    latency_ms INTEGER, retry_after REAL NOT NULL DEFAULT 0,
                    request_ref TEXT NOT NULL, response_ref TEXT,
                    evidence_ref TEXT, reported_cost_kopecks TEXT, failure_reason TEXT, response_sha256 TEXT,
                    UNIQUE(fingerprint, number));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, call_id TEXT, kind TEXT NOT NULL,
                    created REAL NOT NULL, detail TEXT NOT NULL);
            """)
        # A disabled app does not bind a spending approval or replace an existing one.
        if self.policy.enabled:
            with self._transaction() as con:
                saved = con.execute("SELECT policy FROM config WHERE id=1").fetchone()
                policy_text = canonical(asdict(self.policy))
                if saved and saved[0] != policy_text:
                    raise ValueError("This ledger is bound to a different immutable approval")
                con.execute("INSERT OR IGNORE INTO config VALUES(1,?)", (policy_text,))

    @property
    def policy(self):
        # Even access to nested ceiling mappings gives a copy, never the active
        # approval. frozen dataclasses alone do not freeze their dict fields.
        return Policy(**json.loads(self._policy_text))

    def _connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextmanager
    def _transaction(self):
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _event(con, call_id, kind, detail):
        con.execute("INSERT INTO events(call_id,kind,created,detail) VALUES(?,?,?,?)",
                    (call_id, kind, time.time(), canonical(detail)))

    @staticmethod
    def admission_block(rows, policy):
        """Shared non-budget blockers for dispatch and read-only availability."""
        if any(row["state"]=="unknown" or row["status"]=="ceiling_breached"
                or row["failure_reason"] in {"model_mismatch","output_bound_breached","terminal_http_error"}
                for row in rows):
            return "unresolved_spending"
        if policy.budget_mode=="settled_spend" and any(row["state"] in {"reserved","started"} for row in rows):
            return "request_in_flight"
        return None

    def _write_private(self, name, document):
        target = self.artifacts / name
        # UUID path + exclusive creation prevents replacing the request/result.
        with target.open("x", encoding="utf-8") as out:
            out.write(canonical(document))
            out.flush()
            import os
            os.fsync(out.fileno())
        return str(target.relative_to(self.root))

    def _existing_outcome(self, row):
        value = None
        if row["status"] == "succeeded" and row["response_ref"]:
            try:
                saved = json.loads((self.root / row["response_ref"]).read_text())
                if digest(saved) != row["response_sha256"]:
                    raise ValueError("Cached response digest changed")
                value = saved["validated"]
            except (OSError, KeyError, ValueError):
                return Outcome("cached_result_unavailable", row["id"], reused=True)
        with closing(self._connect()) as con:
            event=con.execute("SELECT detail FROM events WHERE call_id=? AND kind='failure_diagnostic' ORDER BY seq DESC LIMIT 1",(row["id"],)).fetchone()
        return Outcome(row["status"], row["id"], value, reused=True,
            diagnostic=json.loads(event[0]) if event else None)

    def reserve(self, request, *, retry=False):
        """Durably reserve once. Returned call id is an unguessable dispatch token."""
        error = self.policy.validate() or request.validate(self.policy)
        if error:
            return Outcome(error)
        fingerprint = request.fingerprint
        soft = self.policy.budget_mode == "settled_spend"
        ceiling = self._dispatch_for(request) if soft else self._ceiling_for(request)
        if not isinstance(ceiling, dict) or (not soft and not positive_int(ceiling.get("micro_rub"))) or not ceiling.get("evidence_ref"):
            return Outcome("billing_ceiling_unproven")
        reserve = 0 if soft else ceiling["micro_rub"]
        with self._transaction() as con:
            previous = con.execute("SELECT * FROM calls WHERE fingerprint=? ORDER BY number DESC LIMIT 1", (fingerprint,)).fetchone()
            if previous:
                if not retry or previous["status"] == "succeeded":
                    return self._existing_outcome(previous)
                first = con.execute("SELECT MIN(created) FROM calls WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
                if time.time() >= first + self.policy.operation_timeout_seconds:
                    return Outcome("operation_deadline", previous["id"])
                if previous["state"] != "settled" or previous["status"] not in {"invalid_response", "truncated_response", "retryable_http_error"}:
                    return Outcome("retry_forbidden", previous["id"])
                if time.time() < previous["retry_after"]:
                    return Outcome("retry_after_pending", previous["id"])
                if previous["number"] >= self.policy.max_attempts:
                    return Outcome("attempt_limit", previous["id"])
            block=self.admission_block(con.execute("SELECT state,status,failure_reason FROM calls").fetchall(),self.policy)
            if block: return Outcome(block)
            totals = con.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN cost IS NULL THEN reserve ELSE cost END),0) FROM calls").fetchone()
            if totals[0] >= self.policy.max_total_calls:
                return Outcome("total_call_limit")
            if (totals[1] >= self.policy.budget_micro_rub if soft else totals[1] + reserve > self.policy.budget_micro_rub):
                return Outcome("budget_limit")
            call_id = "C" + uuid.uuid4().hex
            request_ref = self._write_private(call_id + "-request.json", request.document())
            con.execute("INSERT INTO calls(id,fingerprint,number,state,status,reserve,retry_of,created,request_ref,evidence_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (call_id, fingerprint, previous["number"] + 1 if previous else 1,
                         "reserved", "reserved", reserve, previous["id"] if previous else None,
                         time.time(), request_ref, ceiling["evidence_ref"]))
            self._event(con, call_id, "reserved", {"micro_rub": reserve, "budget_mode":self.policy.budget_mode, "versions": request.versions})
            return Outcome("reserved", call_id)

    def _ceiling_for(self, request):
        exact = self.policy.ceilings.get(request.fingerprint)
        if exact is not None:
            return exact
        matches = []
        for rule in self.policy.ceiling_rules.values():
            if not isinstance(rule, dict):
                continue
            required = {"stage", "provider", "model", "versions", "settings",
                        "max_request_bytes", "max_output_tokens", "micro_rub", "evidence_ref"}
            if set(rule) != required:
                continue
            if (rule["stage"] != request.stage or rule["provider"] != request.provider
                    or rule["model"] != request.payload["model"]
                    or rule["versions"] != request.versions):
                continue
            settings = {k: v for k, v in request.payload.items() if k not in {"model", "messages", "max_tokens"}}
            if not isinstance(rule["settings"], dict) or rule["settings"] != settings:
                continue
            if not positive_int(rule["max_request_bytes"]) or not positive_int(rule["max_output_tokens"]):
                continue
            if len(canonical(request.payload).encode()) > rule["max_request_bytes"] or request.payload["max_tokens"] > rule["max_output_tokens"]:
                continue
            if positive_int(rule["micro_rub"]) and isinstance(rule["evidence_ref"], str) and rule["evidence_ref"].strip():
                matches.append(rule)
        # Multiple applicable approvals must never accidentally choose the
        # cheapest reserve. Use the largest conservative ceiling.
        return max(matches, key=lambda r: r["micro_rub"]) if matches else None

    def _dispatch_for(self, request):
        """Approved request shape only; deliberately no invented price bound."""
        required={"stage","provider","model","versions","settings","max_request_bytes","max_output_tokens","evidence_ref"}
        settings={k:v for k,v in request.payload.items() if k not in {"model","messages","max_tokens"}}
        for rule in self.policy.dispatch_rules.values():
            if not isinstance(rule,dict) or set(rule)!=required: continue
            if (rule["stage"]==request.stage and rule["provider"]==request.provider and rule["model"]==request.payload["model"]
                    and rule["versions"]==request.versions and rule["settings"]==settings
                    and positive_int(rule["max_request_bytes"]) and positive_int(rule["max_output_tokens"])
                    and len(canonical(request.payload).encode())<=rule["max_request_bytes"]
                    and request.payload["max_tokens"]<=rule["max_output_tokens"]
                    and isinstance(rule["evidence_ref"],str) and rule["evidence_ref"].strip()): return rule
        return None

    def _start(self, call_id):
        with self._transaction() as con:
            row = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if not row or row["state"] != "reserved":
                return False
            first = con.execute("SELECT MIN(created) FROM calls WHERE fingerprint=?", (row["fingerprint"],)).fetchone()[0]
            if self.policy.validate() or time.time() >= first + self.policy.operation_timeout_seconds:
                con.execute("UPDATE calls SET state='settled',status='expired_before_dispatch',cost=0,reported_cost_kopecks='0' WHERE id=?", (call_id,))
                self._event(con, call_id, "expired_before_dispatch", {})
                return False
            changed = con.execute("UPDATE calls SET state='started',status='in_flight' WHERE id=? AND state='reserved'", (call_id,)).rowcount
            if changed:
                self._event(con, call_id, "dispatch_started", {})
            return changed == 1

    def _finish(self, call_id, status, response=None, value=None, latency_ms=None, *, diagnostic=None):
        cost = kopecks_to_micro(response.cost_kopecks) if response else None
        usage = normalized_usage(response.usage if response else None)
        cost_basis = response.cost_basis if response and isinstance(response.cost_basis, str) and response.cost_basis in {"provider_report", "token_estimate", "unknown"} else "unknown"
        tariff_ref = response.tariff_ref if response and isinstance(response.tariff_ref, str) and response.tariff_ref.strip() else None
        # A number alone does not establish a tariff-based estimate. Missing
        # tariff evidence or usage preserves the existing unknown-spend stop.
        if cost_basis == "unknown" or (cost_basis == "token_estimate" and
                (tariff_ref is None or usage["input_tokens"] is None or usage["output_tokens"] is None)):
            cost = None
        with self._transaction() as con:
            row = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if row["state"] != "started":
                return self._existing_outcome(row)
            reason = status
            state = "settled" if cost is not None else "unknown"
            if cost is None:
                status, value = "cost_unknown", None
            elif self.policy.budget_mode == "hard_ceiling" and cost > row["reserve"]:
                status, value = "ceiling_breached", None
            response_ref = None
            response_sha256 = None
            if response:
                # Keep exact reported or estimated cost as text, including
                # sub-microrouble precision. Admission rounds integer micro-RUB up.
                envelope = asdict(response)
                envelope["cost_kopecks"] = str(response.cost_kopecks) if response.cost_kopecks is not None else None
                if not isinstance(envelope["content"], str):
                    envelope["content"] = None
                elif len(envelope["content"].encode()) > self.policy.max_response_bytes:
                    envelope["content"] = None
                    envelope["content_omitted"] = "response_exceeded_local_size_limit"
                # Do not allow malformed ancillary provider fields to prevent
                # durable settlement after an already potentially charged call.
                envelope["usage"] = usage
                envelope["cost_basis"] = cost_basis
                envelope["tariff_ref"] = tariff_ref
                try:
                    envelope["provider_metadata"] = json.loads(canonical(response.provider_metadata)) if isinstance(response.provider_metadata, dict) else None
                except (TypeError, ValueError, RecursionError):
                    envelope["provider_metadata"] = None
                for field_name in ("model", "response_id", "finish_reason", "provider_finish_reason"):
                    if not isinstance(envelope[field_name], str):
                        envelope[field_name] = None
                envelope["retry_after_seconds"] = None
                response_document = {"envelope": envelope, "validated": value}
                response_ref = self._write_private(call_id + "-response.json", response_document)
                response_sha256 = digest(response_document)
            retry_after = 0
            if response and isinstance(response.retry_after_seconds, (int, float)) and 0 <= response.retry_after_seconds < float("inf"):
                retry_after = time.time() + response.retry_after_seconds
            reported = str(response.cost_kopecks) if response and cost is not None else None
            con.execute("UPDATE calls SET state=?,status=?,cost=?,usage=?,latency_ms=?,response_ref=?,retry_after=?,reported_cost_kopecks=?,failure_reason=?,response_sha256=? WHERE id=?",
                        (state, status, cost, canonical(usage), latency_ms, response_ref, retry_after, reported, reason, response_sha256, call_id))
            self._event(con, call_id, status, {"cost_micro_rub": cost,
                "cost_basis": cost_basis if cost is not None else "unknown", "tariff_ref": tariff_ref, "reason": reason})
            if diagnostic is not None or status != "succeeded":
                safe=diagnostic_detail(**diagnostic) if diagnostic else diagnostic_detail("unknown_cost" if cost is None else "unexpected_error",response.http_status if response else None)
                # Diagnostic metadata must never prevent durable settlement.
                try:
                    request=json.loads((self.root/row["request_ref"]).read_text())
                    provider=request.get('provider');stage=request.get('stage');operation=request.get('context',{}).get('operation')
                except (OSError,ValueError,TypeError,AttributeError):provider=stage=operation=None
                safe.update(provider=provider if isinstance(provider,str) and provider in {'google','deepseek','hubris','synthetic-fixture'} else 'other',
                    stage=stage if isinstance(stage,str) and stage in STAGES else 'other',
                    operation=operation if isinstance(operation,str) and operation in {'solve','compare'} else None,
                    latency_ms=latency_ms,call_id=call_id)
                self._event(con,call_id,"failure_diagnostic",safe)
            else:safe=None
            if self.policy.budget_mode=="settled_spend" and con.execute("SELECT COALESCE(SUM(cost),0) FROM calls").fetchone()[0]>=self.policy.budget_micro_rub:
                self._event(con,call_id,"spending_limit_reached",{"budget_micro_rub":self.policy.budget_micro_rub})
        return Outcome(status, call_id, value if status == "succeeded" else None, diagnostic=safe)

    def _cancel_untransmitted(self, call_id):
        """A local dispatch gate proved transport was never invoked.

        No provider response/invoice is fabricated. This cannot be used for an
        already invoked transport, whose actual charge may still be unknown.
        """
        with self._transaction() as con:
            row=con.execute("SELECT * FROM calls WHERE id=?",(call_id,)).fetchone()
            if not row or row["state"]!="started":
                return self._existing_outcome(row) if row else Outcome("dispatch_not_admitted",call_id)
            con.execute("UPDATE calls SET state='settled',status='cancelled_before_dispatch',cost=0,reported_cost_kopecks='0',failure_reason='cancelled_before_dispatch' WHERE id=?",(call_id,))
            self._event(con,call_id,"cancelled_before_dispatch",dict(transport_invoked=False,cost_micro_rub=0))
        return Outcome("cancelled_before_dispatch",call_id)

    def execute(self, request, transport, validator, *, retry=False, before_dispatch=None, response_text=False):
        """No automatic retry, including SDK/network retry or schema downgrade.

        transport.kind is 'fixture' or 'live'; single_attempt must be True. A
        live adapter must establish a total-output bound including reasoning.
        validator must raise on rejection and return a JSON serializable value.
        """
        # Freeze exact request before hashing, persistence and dispatch.
        try:
            request = StageRequest(**json.loads(canonical(request.document())))
        except (TypeError, ValueError):
            return Outcome("invalid_request")
        error = self.policy.validate() or request.validate(self.policy)
        if error:
            return Outcome(error)
        # Raw-text acceptance is explicit and bound to the admitted API body.
        # JSON stages cannot opt out of parsing through this local flag.
        if type(response_text) is not bool or response_text != (request.payload.get("response_format") == {"type": "text"}):
            return Outcome("response_format_mismatch")
        if getattr(transport, "kind", None) != self.policy.mode or getattr(transport, "single_attempt", False) is not True:
            return Outcome("transport_not_admitted")
        if self.policy.mode == "live" and self.policy.budget_mode == "hard_ceiling" and getattr(transport, "total_output_bound_verified", False) is not True:
            return Outcome("output_bound_unproven")
        admitted = self.reserve(request, retry=retry)
        if admitted.status != "reserved" or admitted.reused:
            return admitted
        if not self._start(admitted.attempt_id):
            return Outcome("dispatch_not_admitted", admitted.attempt_id)
        result_queue = queue.Queue(maxsize=1)
        started = time.monotonic()
        with closing(self._connect()) as con:
            first = con.execute("SELECT MIN(created) FROM calls WHERE fingerprint=?", (request.fingerprint,)).fetchone()[0]
        remaining = max(0.001, min(self.policy.timeout_seconds,
                        first + self.policy.operation_timeout_seconds - time.time(),
                        self.policy.expires_at - time.time()))

        def run_once():
            try:
                payload=json.loads(canonical(request.payload))
                # Check after reservation/startup and immediately before the
                # actual transport invocation, including time spent waiting
                # for SQLite. Once invoked, preserve its actual response and
                # separately identified reported or tariff-estimated cost.
                if before_dispatch is not None and before_dispatch() is not True:
                    result_queue.put((None,None))
                    return
                # The adapter receives the exact frozen body and must send it
                # without adding paid tools, increasing caps, or hidden retries.
                response = transport(payload, timeout_seconds=remaining)
                result_queue.put((True, response))
            except BaseException as error:
                # Exception strings can contain credentials or pupil material.
                result_queue.put((False, exception_detail(error)))

        worker = threading.Thread(target=run_once, daemon=True)
        worker.start()
        try:
            ok, response = result_queue.get(timeout=remaining)
            failure = "transport_failure"
        except queue.Empty:
            ok, response = False, diagnostic_detail("guard_timeout")
            failure = "transport_timeout"
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        if ok is None:
            return self._cancel_untransmitted(admitted.attempt_id)
        if not ok:
            return self._finish(admitted.attempt_id, failure, latency_ms=elapsed,diagnostic=response)
        if not isinstance(response, ProviderResponse):
            return self._finish(admitted.attempt_id, failure, latency_ms=elapsed,diagnostic=diagnostic_detail("invalid_transport_response"))
        if type(response.http_status) is not int:
            return self._finish(admitted.attempt_id, "invalid_response", latency_ms=elapsed,diagnostic=diagnostic_detail("invalid_envelope"))
        if response.http_status != 200:
            status = "retryable_http_error" if response.http_status == 429 or 500 <= response.http_status < 600 else "terminal_http_error"
            return self._finish(admitted.attempt_id, status, response, latency_ms=elapsed,diagnostic=diagnostic_detail("http_error",response.http_status))
        if response.model != request.payload["model"]:
            category="invalid_envelope" if response.model is None else "model_mismatch"
            return self._finish(admitted.attempt_id, "model_mismatch", response, latency_ms=elapsed,diagnostic=diagnostic_detail(category,response.http_status))
        if response.finish_reason != "stop":
            status = "truncated_response" if response.finish_reason == "length" else "terminal_response_error"
            category=response.finish_reason if response.finish_reason in {'invalid_envelope','invalid_usage'} else status
            return self._finish(admitted.attempt_id, status, response, latency_ms=elapsed,diagnostic=diagnostic_detail(category,response.http_status))
        output_tokens = normalized_usage(response.usage)["output_tokens"]
        if output_tokens is not None and output_tokens > request.payload["max_tokens"]:
            return self._finish(admitted.attempt_id, "output_bound_breached", response, latency_ms=elapsed,diagnostic=diagnostic_detail("output_bound_breached",response.http_status))
        category="response_too_large"
        try:
            if not isinstance(response.content, str) or len(response.content.encode()) > self.policy.max_response_bytes:
                raise ValueError("response_limit")
            category="response_json"
            data = response.content if response_text else parse_model_json(response.content)
            category="response_validation"
            value = validator(data)
            canonical(value)
        except Exception:
            return self._finish(admitted.attempt_id, "invalid_response", response, latency_ms=elapsed,diagnostic=diagnostic_detail(category,response.http_status))
        return self._finish(admitted.attempt_id, "succeeded", response, value, elapsed)

    def reconcile(self, call_id, *, cost_kopecks, evidence_ref):
        """Administrative accounting only, NEVER resend or revive a result.

        Use after the original transport has ended and provider billing is
        checked. A stuck/reserved call is conservatively treated as dispatched;
        it cannot be reclaimed merely because a worker/process disappeared.
        """
        cost = kopecks_to_micro(cost_kopecks)
        if cost is None or not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("Reconciliation requires known provider cost and evidence")
        with self._transaction() as con:
            row = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if not row:
                raise KeyError(call_id)
            if row["cost"] is not None:
                if Decimal(row["reported_cost_kopecks"]) != Decimal(str(cost_kopecks)):
                    raise ValueError("A settled amount cannot be overwritten")
                return
            status = "ceiling_breached" if self.policy.budget_mode == "hard_ceiling" and cost > row["reserve"] else "reconciled_no_result"
            con.execute("UPDATE calls SET state='settled',status=?,cost=?,reported_cost_kopecks=? WHERE id=?", (status, cost, str(cost_kopecks), call_id))
            self._event(con, call_id, "reconciled", {"cost_micro_rub": cost, "evidence_ref": evidence_ref})

    def revalidate_response(self,call_id,validator,*,validator_ref):
        """Recheck a saved paid response locally; no transport, fee or retry.

        Preserve the original rejected artifact and history. Only a settled
        format/contract rejection with known billing can be revalidated.
        Caller must verify the original source is unchanged.
        """
        if not isinstance(validator_ref,str) or not validator_ref.strip():raise ValueError("validator_reference_required")
        with self._transaction() as con:
            row=con.execute("SELECT * FROM calls WHERE id=?",(call_id,)).fetchone()
            if not row or row["state"]!="settled" or row["status"]!="invalid_response" or row["cost"] is None:
                raise ValueError("response_not_revalidatable")
            original=json.loads((self.root/row["response_ref"]).read_text())
            if digest(original)!=row["response_sha256"]:raise ValueError("response_artifact_changed")
            request=StageRequest(**json.loads((self.root/row["request_ref"]).read_text()))
            if request.fingerprint!=row["fingerprint"]:raise ValueError("request_artifact_changed")
            envelope=original["envelope"]
            if (envelope["http_status"]!=200 or envelope["finish_reason"]!="stop" or envelope["model"]!=request.payload["model"]
                    or not isinstance(envelope["content"],str) or len(envelope["content"].encode())>self.policy.max_response_bytes
                    or envelope["usage"].get("output_tokens") is not None and envelope["usage"]["output_tokens"]>request.payload["max_tokens"]):
                raise ValueError("response_not_revalidatable")
            value=validator(parse_model_json(envelope["content"]));canonical(value)
            revised={"envelope":envelope,"validated":value}
            ref=self._write_private(call_id+"-revalidated-"+uuid.uuid4().hex+".json",revised)
            con.execute("UPDATE calls SET status='succeeded',response_ref=?,response_sha256=? WHERE id=?",(ref,digest(revised),call_id))
            self._event(con,call_id,"response_revalidated",dict(previous_status=row["status"],previous_response_ref=row["response_ref"],
                previous_response_sha256=row["response_sha256"],validator_ref=validator_ref,new_response_ref=ref,cost_unchanged=True))
            return Outcome("succeeded",call_id,value,reused=True)

    def snapshot(self, *, fingerprints=None):
        with closing(self._connect()) as con:
            rows = con.execute("SELECT * FROM calls ORDER BY created,id").fetchall()
            reconciled = {row[0] for row in con.execute("SELECT call_id FROM events WHERE kind='reconciled'")}
        if fingerprints is not None:
            rows = [row for row in rows if row["fingerprint"] in set(fingerprints)]
        known = sum(row["cost"] for row in rows if row["cost"] is not None)
        held = sum(row["reserve"] for row in rows if row["cost"] is None)
        exact = sum((Decimal(row["reported_cost_kopecks"]) / 100 for row in rows if row["reported_cost_kopecks"] is not None), Decimal(0))
        unknown = any(row["cost"] is None for row in rows)
        return {"version": VERSION, "mode": self.policy.mode, "calls": len(rows),
                "known_micro_rub": known, "held_micro_rub": held,
                "committed_micro_rub": known + held,
                "cost": {"rub": None if unknown else money_string(exact),
                         "status": "unknown" if unknown else "known" if rows else "not_incurred"},
                "unresolved": [r["id"] for r in rows if r["cost"] is None],
                "statuses": [r["status"] for r in rows],
                **accounting_summary(rows, self.root, reconciled_call_ids=reconciled)}

    def contract_calls(self, *, fingerprints=None):
        """M01 real calls with all retries for selected stage fingerprints.

        No filter means an audit of the entire ledger, NOT one student's run.
        Simulated attempts must never become real API history.
        """
        if self.policy.mode == "fixture":
            return []
        with closing(self._connect()) as con:
            rows = con.execute("SELECT * FROM calls WHERE state!='reserved' AND status NOT IN ('expired_before_dispatch','cancelled_before_dispatch') ORDER BY created,id").fetchall()
            reconciled = {row[0] for row in con.execute("SELECT call_id FROM events WHERE kind='reconciled'")}
        if fingerprints is not None:
            rows = [row for row in rows if row["fingerprint"] in set(fingerprints)]
        calls = []
        for row in rows:
            request = json.loads((self.root / row["request_ref"]).read_text())
            if StageRequest(**request).fingerprint != row["fingerprint"]:
                raise ValueError("request_artifact_changed")
            payload = request["payload"]
            settings = {k: v for k, v in payload.items() if k not in {"model", "messages"}}
            settings.update({"versions": request["versions"], "guard_version": VERSION})
            response = None
            if row["response_ref"]:
                response = json.loads((self.root / row["response_ref"]).read_text())
                if digest(response) != row["response_sha256"]:
                    raise ValueError("Response artifact integrity failed during export")
                settings.update({"actual_model": response["envelope"]["model"],
                                 "provider_response_id": response["envelope"]["response_id"]})
                metadata = response["envelope"].get("provider_metadata")
                if isinstance(metadata, dict) and isinstance(metadata.get("native_model_version"), str):
                    settings["native_model_version"] = metadata["native_model_version"]
            basis, tariff_ref = accounting_provenance(row, self.root,
                reconciled=row["id"] in reconciled, envelope=response["envelope"] if response else None)
            native_hash = request["context"].get("native_request_sha256")
            if request["provider"] == "google" and isinstance(native_hash, str) and len(native_hash) == 64:
                for name in ("native_endpoint", "native_request_bytes", "native_adapter_version", "native_configuration_sha256"):
                    if name in request["context"]:
                        settings[name] = request["context"][name]
            calls.append({"id": row["id"], "stage": request["stage"], "provider": request["provider"],
                "model_id": payload["model"], "settings": settings,
                "prompt_sha256": digest(payload["messages"]),
                "request_sha256": native_hash if request["provider"] == "google" and isinstance(native_hash, str) and len(native_hash) == 64 else digest(payload),
                "response_ref": row["response_ref"],
                "status": "succeeded" if row["status"] == "succeeded" else "unknown" if row["cost"] is None else "failed",
                "retry_of": row["retry_of"], "usage": json.loads(row["usage"]) if row["usage"] else normalized_usage(None),
                "cost_rub": money_string(Decimal("0" if row["cost"] == 0 else row["reported_cost_kopecks"]) / 100) if row["cost"] is not None else None,
                "cost_basis": basis, "tariff_ref": tariff_ref, "latency_ms": row["latency_ms"],
                "request_ref": row["request_ref"], "prompt_ref": row["request_ref"] + "#/payload/messages"})
        return calls


def accounting_provenance(row, artifact_root, *, reconciled=False, envelope=None):
    """Derive billing provenance without changing historical ledger rows.

    Legacy saved envelopes predate these fields and contain provider invoices.
    Administrative reconciliation likewise uses checked provider billing.
    """
    if row["cost"] is None:
        return "unknown", None
    if row["status"] in {"expired_before_dispatch", "cancelled_before_dispatch"}:
        return "not_incurred", None
    if reconciled:
        return "provider_report", None
    if envelope is None and row["response_ref"]:
        response = json.loads((Path(artifact_root) / row["response_ref"]).read_text())
        if digest(response) != row["response_sha256"]:
            raise ValueError("Response artifact integrity failed during accounting")
        envelope = response["envelope"]
    envelope = envelope if isinstance(envelope, dict) else {}
    # A previous explicitly reconciled Google HTTP 400 kept the original
    # unknown envelope. Its saved provider billing rule establishes zero charge;
    # accept that provenance without rewriting the receipt or ledger row.
    accounting = envelope.get("provider_metadata", {}).get("accounting", {}) if isinstance(envelope.get("provider_metadata"), dict) else {}
    if (row["status"] == "http_error_uncharged" and row["cost"] == 0
            and (row["reported_cost_kopecks"] is None or kopecks_to_micro(row["reported_cost_kopecks"]) == 0) and envelope.get("http_status") == 400
            and isinstance(accounting, dict) and accounting.get("kind") == "documented_uncharged_error"
            and accounting.get("source") == "https://ai.google.dev/gemini-api/docs/billing"):
        return "provider_report", accounting["source"]
    basis = envelope.get("cost_basis", "provider_report")
    tariff_ref = envelope.get("tariff_ref")
    if basis == "token_estimate":
        usage = envelope.get("usage", {})
        if (not isinstance(tariff_ref, str) or not tariff_ref.strip() or not isinstance(usage, dict)
                or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 0
                or type(usage.get("output_tokens")) is not int or usage["output_tokens"] < 0):
            raise ValueError("Estimated cost lacks saved tariff and usage evidence")
        return basis, tariff_ref
    if basis != "provider_report":
        raise ValueError("Settled cost lacks supported billing provenance")
    return basis, tariff_ref if isinstance(tariff_ref, str) and tariff_ref.strip() else None


def accounting_summary(rows, artifact_root, *, reconciled_call_ids=()):
    """Read-only breakdown for snapshots and availability, without pupil data."""
    totals = {"provider_report": Decimal(0), "token_estimate": Decimal(0)}
    counts = {"provider_report": 0, "token_estimate": 0}
    bases, tariffs = set(), set()
    reconciled_call_ids = set(reconciled_call_ids)
    for row in rows:
        basis, tariff_ref = accounting_provenance(row, artifact_root,
            reconciled=row["id"] in reconciled_call_ids)
        if basis != "not_incurred":
            bases.add(basis)
        if basis in totals:
            totals[basis] += Decimal("0" if row["cost"] == 0 else row["reported_cost_kopecks"]) / 100
            counts[basis] += 1
        if tariff_ref:
            tariffs.add(tariff_ref)
    basis = ("unknown" if "unknown" in bases else "mixed" if len(bases) > 1
             else next(iter(bases)) if bases else "not_incurred")
    return {"cost_basis": basis, "estimated_calls": counts["token_estimate"],
        "provider_report_calls": counts["provider_report"],
        "estimated_rub": money_string(totals["token_estimate"]),
        "provider_report_rub": money_string(totals["provider_report"]),
        "tariff_refs": sorted(tariffs)}


def normalized_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    def count(key):
        value = usage.get(key)
        return value if type(value) is int and value >= 0 else None
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    reasoning = details.get("reasoning_tokens")
    return {"input_tokens": count("prompt_tokens"), "output_tokens": count("completion_tokens"),
            "reasoning_tokens": reasoning if type(reasoning) is int and reasoning >= 0 else None,
            "reasoning_in_output": True if type(reasoning) is int and reasoning >= 0 else None}
