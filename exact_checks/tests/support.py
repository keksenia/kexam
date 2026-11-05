"""Synthetic M01 contexts for exact-operation tests. No student data or photographs."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True  # never leave .pyc files inside the read-only K01 folder

from contracts.ege_v1 import validate as m01
from exact_checks import protocol as P
from exact_checks.operations import ExactOperation

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "planning/k01/examples.json").read_text())["examples"]
GATE_CLAIMS = {
    "unknown_assumption": "Read the denominator as x-1.",
    "series_operator_unresolved": "The input families are a union, not an intersection.",
    "angle_variable_unmapped": "All supplied families and interval have been mapped to the same variable.",
    "set_variables_unmapped": "All sets refer to the same coordinate variable.",
}


def base(exam_number=15, label="synthetic"):
    """A valid explicitly synthetic M01 input/result pair with one clear student unit u1."""
    inp = json.loads((ROOT / "contracts/ege_v1/examples/input_15.json").read_text())
    inp["id"] = "EXACT-" + label
    inp["task"] = {"exam_year": 2026, "exam_number": exam_number,
                   "task_type": "trigonometric_equation" if exam_number == 13 else "inequality",
                   "source_label": "Synthetic exact-operation test, not a student work",
                   "statement": {"text": label, "format": "plain_latex",
                                 "source": {"kind": "synthetic", "ref": "exact_checks/tests"},
                                 "completeness": "confirmed", "missing": []}}
    inp["pages"] = [{"id": "p1", "role": "solution", "order": 1, "asset_id": "synthetic-placeholder",
                     "sha256": m01.digest(label), "width": 100, "height": 100, "region": None,
                     "selection_note": "Synthetic placeholder, no photograph exists."}]
    inp["provenance"] = {"source": {"kind": "synthetic", "ref": "exact_checks/tests"},
                         "dataset_role": "synthetic", "duplicate_group": None}
    result = json.loads((ROOT / "contracts/ege_v1/examples/result_15.json").read_text())
    result["id"] = inp["id"] + "-result"
    result["input_ref"] = {"id": inp["id"], "revision": inp["revision"], "sha256": m01.digest(inp)}
    result["units"] = [unit("u1")]
    result["branches"][0]["unit_ids"] = ["u1"]
    result["coverage"] = [{"page_id": "p1", "status": "complete", "reason": "Synthetic context only.",
                           "score_scope": "unresolved", "score_scope_reason": "No whole-solution assessment."}]
    return inp, result


def unit(uid, literal="synthetic", reading_status="clear", activity="active"):
    return {"id": uid, "kind": "math", "literal": literal, "reading_status": reading_status, "activity": activity,
            "author": "student", "anchors": [{"page_id": "p1", "box": None}],
            "source": {"kind": "synthetic", "ref": "exact_checks/tests"}}


def context(arguments, *, branch="branch-main", unit_ids=("u1",), assumption_ids=(), sources=None, depends=()):
    sources = sources or {}
    return {"unit_ids": list(unit_ids), "interpretation_ids": [], "branch_id": branch,
            "assumption_ids": list(assumption_ids), "context_unit_ids": [], "depends_on_fact_ids": list(depends),
            "bindings": [{"argument_pointer": "/" + key, "source_kind": sources.get(key, ("unit", "u1"))[0],
                          "source_ref": sources.get(key, ("unit", "u1"))[1]} for key in arguments]}


def for_example(example):
    """Context mirroring K01 verify.py: gated examples carry one unknown hypothesis."""
    inp, result = base(example["exam_number"], "K01-" + example["id"])
    assumptions = []
    if example["gate"] != "resolved":
        result["units"][0]["reading_status"] = "ambiguous"
        result["assumptions"] = [{"id": "a1", "claim": GATE_CLAIMS[example["gate"]], "origin": "hypothesis",
                                  "source_ref": "u1", "status": "unknown", "branch_id": "branch-main"}]
        result["branches"][0]["assumption_ids"] = ["a1"]
        assumptions = ["a1"]
    sources, unit_ids = None, ("u1",)
    if example["operation"] == "m06.circle_point_equal":
        # K01: the checked label cannot be the only source of the independent point.
        # verify.py binds every argument to u1 for convenience; here the point gets its own unit.
        result["units"].append(unit("u2", "independent point source"))
        result["branches"][0]["unit_ids"].append("u2")
        sources, unit_ids = {"independent_point_angle": ("unit", "u2")}, ("u1", "u2")
    request = {"operation": example["operation"], "version": P.TOOL_VERSION, "arguments": deepcopy(example["arguments"]),
               "context": context(example["arguments"], assumption_ids=assumptions, sources=sources, unit_ids=unit_ids)}
    return inp, result, request


def run(operation_id, request, result, *, source_input=None):
    """Execute like the M04 pipeline does and return (fact, k01_request, k01_response)."""
    fact = ExactOperation(operation_id).execute(deepcopy(request), deepcopy(result),
                                               source_input=source_input)
    evidence = json.loads(fact["code_check"]["evidence"])
    return fact, evidence["k01_request"], evidence["k01_response"]


def attach(result, fact):
    """Publish a fact the way M04 does: the app adds basis_sha256 = reading digest."""
    published = deepcopy(result)
    fact = deepcopy(fact)
    fact["basis_sha256"] = m01.reading_digest(published)
    published["facts"].append(fact)
    return published


def args(operation_id, **arguments):
    return {"operation": operation_id, "version": P.TOOL_VERSION, "arguments": arguments,
            "context": context(arguments)}


def q(n, d=1):
    return {"numerator": str(n), "denominator": str(d)}


def pi(n, d=1):
    return {"basis": "pi", "coefficient": q(n, d)}


def real(n, d=1):
    return {"basis": "rational", "coefficient": q(n, d)}


def pi_interval(a, b, left_closed=True, right_closed=True):
    return {"left": pi(*a) if isinstance(a, tuple) else pi(a), "right": pi(*b) if isinstance(b, tuple) else pi(b),
            "left_closed": left_closed, "right_closed": right_closed}


def interval(a, b, left_closed=True, right_closed=True):
    end = lambda v: None if v is None else (real(*v) if isinstance(v, tuple) else real(v))
    return {"left": end(a), "right": end(b), "left_closed": left_closed, "right_closed": right_closed}


def exact_set(intervals=(), points=()):
    return {"intervals": list(intervals), "points": [real(*p) if isinstance(p, tuple) else real(p) for p in points]}
