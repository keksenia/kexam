"""M04-compatible trusted operations wrapping the K01 executor (M06/M07).

Pipeline(..., operations=exact_checks.operations.all_operations()) makes them
available; registering them in the default server is integration work (K03).

M04 request:  {operation, version, arguments, context}
  operation  — K01 id, e.g. "m06.select_pi_roots";
  version    — TOOL_VERSION: M04 matches the requested version with the tool that
               will execute, and copies it into fact.tool_version;
  arguments  — K01 arguments of that operation;
  context    — K01 context (unit/interpretation/branch/assumption/fact ids, bindings).
The K01 envelope (input_ref, reading_sha256, protocol and operation versions) is
filled from the current M01 result, so the reading layer cannot forge it.

M01 fact: code_check.arguments equals the M04 arguments (an M04 invariant), and
code_check.evidence is canonical JSON {"k01_request", "k01_response"} so the full
K01 request and its hash stay recoverable (K01 maps the whole request there).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import time

from contracts.ege_v1 import validate as m01

from . import protocol as P
from .rational import CheckError, show

# Feature flags that make an operation available to the M04 planner. Availability
# is not applicability: the operation still validates its arguments and premises.
FEATURES = {
    "m06.pi_interval_membership": {"pi_families", "explicit_interval", "explicit_root_list"},
    "m06.select_pi_roots": {"pi_families", "explicit_interval"},
    "m06.circle_point_equal": {"circle"},
    "m07.set_union": {"inequality", "explicit_set"},
    "m07.set_intersection": {"inequality", "explicit_set"},
    "m07.set_membership": {"inequality", "explicit_set"},
    "m07.set_equal": {"inequality", "explicit_set"},
    "m07.denominator_nonzero": {"rational_fraction"},
}
STATUS = {"verified": "supported", "contradicted": "contradicted", "unknown": "unknown"}


def source_fingerprint():
    """Identity of implementation, runtime and exact pinned contracts.

    Recomputed by the pipeline guard: changing code without changing the version
    string must invalidate an in-flight run too.
    """
    root = Path(__file__).resolve().parent.parent
    files = ["exact_checks/" + name for name in
             ("operations.py", "protocol.py", "rational.py", "trig.py", "sets.py", "executor.py", "worker.py")]
    files += ["planning/k01/protocol.schema.json", "planning/k01/registry.json",
              "contracts/ege_v1/schema.json", "contracts/ege_v1/validate.py"]
    return m01.digest(dict(files={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
                           python=list(sys.version_info[:3]), jsonschema=importlib.metadata.version("jsonschema")))


class OperationContextError(ValueError):
    """The request cannot be attached to the M01 result; a technical failure, not a fact."""
    code = "invalid_operation_context"


def build_request(operation_id, request, result):
    envelope = {"protocol_version": P.PROTOCOL_VERSION, "m01_version": "2.0.0",
                "input_ref": result["input_ref"], "reading_sha256": m01.reading_digest(result),
                "operation": operation_id, "operation_version": P.OPERATIONS[operation_id]["version"],
                "limits_profile": P.LIMITS["id"], "context": request["context"], "arguments": request["arguments"]}
    envelope["request_id"] = "k01-" + m01.digest(envelope)[:32]
    return envelope


def claim(operation_id, arguments):
    """The operation's own statement on its arguments; never 'the solution is right'."""
    try:
        if operation_id == "m06.pi_interval_membership":
            point, interval = P._pi(arguments["point"]), P._pi_interval(arguments["interval"])
            relation = "принадлежит" if arguments["expected_member"] else "не принадлежит"
            return f"Число {show(point, 'pi')} {relation} промежутку {P._pi_interval_text(interval)}."
        if operation_id == "m06.select_pi_roots":
            families = "; ".join(P._family_text(P.R.parse(f["offset"]), P.R.parse(f["period"])) for f in arguments["families"])
            claimed = P._values(sorted({P._pi(v) for v in arguments["claimed_roots"]}), "pi", 500)
            interval = P._pi_interval_text(P._pi_interval(arguments["interval"]))
            return P._cut(f"Список {claimed} как множество совпадает с отбором из объединения семейств {families} на {interval}.", 1500)
        if operation_id == "m06.circle_point_equal":
            return (f"Углы {show(P._pi(arguments['angle']), 'pi')} и {show(P._pi(arguments['independent_point_angle']), 'pi')} "
                    "задают одну точку единичной окружности (равенство по модулю 2π, не равенство чисел).")
        if operation_id in ("m07.set_union", "m07.set_intersection"):
            symbol = " ∪ " if operation_id == "m07.set_union" else " ∩ "
            inputs = symbol.join(P.set_text(P._set(s), 250) for s in arguments["sets"])
            return P._cut(f"Множество {P.set_text(P._set(arguments['claimed_set']), 400)} равно {inputs}.", 1500)
        if operation_id == "m07.set_membership":
            relation = "принадлежит" if arguments["expected_member"] else "не принадлежит"
            return P._cut(f"Число {show(P.R.parse(arguments['point']['coefficient']))} {relation} множеству {P.set_text(P._set(arguments['set']), 800)}.", 1500)
        if operation_id == "m07.set_equal":
            return P._cut(f"Множества {P.set_text(P._set(arguments['left']), 600)} и {P.set_text(P._set(arguments['right']), 600)} равны.", 1500)
        if operation_id == "m07.denominator_nonzero":
            e = arguments["expression"]
            q = P.polynomial_text([P.R.parse(c) for c in e["denominator"]], e["variable"])
            return f"Исходный знаменатель {q} не равен нулю при {e['variable']} = {show(P.R.parse(arguments['point']['coefficient']))}."
    except (CheckError, KeyError, TypeError, ValueError, AttributeError):
        pass
    return f"Утверждение операции {operation_id} на переданных аргументах (аргументы не прошли проверку формата)."


class ExactOperation:
    network_enabled = False

    def __init__(self, operation_id, executor=None, *, source_input=None):
        if operation_id not in P.OPERATIONS:
            raise ValueError(operation_id)
        self.id = operation_id
        self.version = P.TOOL_VERSION
        self.features = frozenset(FEATURES[operation_id])
        self.claim_kind = P.OPERATIONS[operation_id]["claim_kind"]
        self.resource_limits = {key: P.LIMITS[key] for key in
                                ("wall_ms_per_operation", "wall_ms_per_work", "memory_mib_worker")}
        self.execution_group = "exact-checks"
        self.execution_profile = "exact-worker-rss-watchdog-1"
        self._executor = executor
        self._source_input = deepcopy(source_input)

    @property
    def source_sha256(self):
        return source_fingerprint()

    def prepare_execution(self, timeout_ms):
        from .executor import IsolatedExecutor
        if self._executor is None:
            self._executor = IsolatedExecutor()
        self._executor.prepare(timeout_ms, self.source_sha256)

    def execute_bounded(self, request, result, timeout_ms, *, source_input=None):
        from .executor import ExecutionFailure
        started = time.monotonic()
        if self._executor is None:
            raise ExecutionFailure("worker_unavailable")
        fingerprint = self.source_sha256
        remaining = timeout_ms - (time.monotonic() - started) * 1000
        if remaining <= 0:
            raise ExecutionFailure("timeout")
        snapshot = self._source_input if source_input is None else source_input
        return self._executor.execute(self.id, request, result, remaining, fingerprint,
                                      source_input=snapshot)

    def close(self):
        if self._executor is not None:
            self._executor.close()

    def execute(self, request, result, *, source_input=None):
        if request.get("operation") != self.id or request.get("version") != self.version:
            raise OperationContextError("Запрос адресован другой операции или версии.")
        context = request.get("context")
        try:
            context = P.effective_context(context, result)
        except P.ContextError as exc:
            # K01: an envelope without usable context is a technical problem, not an invented fact.
            raise OperationContextError(str(exc)) from None
        # The caller may omit repeated IDs already present on an interpretation.
        # Publish the actual dependency closure, including conditional premises.
        effective_request = dict(request, context=context)
        k01_request = build_request(self.id, effective_request, result)
        snapshot = self._source_input if source_input is None else source_input
        response = P.execute(k01_request, result, source_input=snapshot)
        existing = {f["id"] for f in result.get("facts", [])}
        base = "code-" + self.id.split(".", 1)[1].replace("_", "-") + "-" + response["request_sha256"][:12]
        fact_id, n = base, 2
        while fact_id in existing:
            fact_id, n = f"{base}-{n}", n + 1
        branch = context["branch_id"]
        return {
            "id": fact_id,
            "unit_ids": list(context["unit_ids"]),
            "interpretation_ids": list(context["interpretation_ids"]),
            "claim": claim(self.id, request["arguments"]),
            "method": "exact_code",
            "status": STATUS[response["status"]],
            "reason": response["summary"] if response["status"] != "unknown"
                      else f"Не установлено ({response['reason_code']}): {response['summary']}",
            "tool_version": self.version,
            "depends_on_fact_ids": list(context["depends_on_fact_ids"]),
            "code_check": {"operation": self.id, "arguments": request["arguments"],
                           "evidence": json.dumps({"k01_request": k01_request, "k01_response": response,
                                                   "execution": {"mode": "in_process_diagnostic", "hard_deadline": False,
                                                                 "memory_enforced": False}},
                                                  ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
            "branch_id": branch,
            "assumption_ids": list(context["assumption_ids"]),
            "context_unit_ids": list(context["context_unit_ids"]),
            "claim_kind": self.claim_kind,
            "domain": (f"Ветвь {branch}: только аргументы операции {self.id}, вычисленные точно. "
                       "Чтение записей, вывод семейств/множеств, ОДЗ целиком и полнота решения этой операцией не устанавливаются."),
        }


def all_operations(*, source_input=None):
    from .executor import IsolatedExecutor
    executor = IsolatedExecutor()
    return [ExactOperation(operation_id, executor, source_input=source_input)
            for operation_id in P.OPERATIONS]
