"""Fresh M01 materializers with rule diagnostics; frozen M05 stays unchanged."""
from copy import deepcopy
from contracts.ege_v1 import validate as contract
from .m05.runtime import PromptError, validate_response, digest, decode_response

VERSION = "live-boundary-1.0.0"

def validate_candidate(candidate, source, code="full_m01_contract"):
    errors = contract.validate(candidate, source)
    if errors:
        error = PromptError(code)
        error.m01_errors = list(errors)
        raise error


def materialize_assessment(response, baseline, source):
    """Merge model-owned fields only, then validate shape AND M01 semantics."""
    request = dict(task=source["task"], context={k: baseline[k] for k in
        ("units", "corrections", "interpretations", "branches", "assumptions", "relations", "facts", "coverage", "issues")},
        criteria=baseline["criteria"])
    value = validate_response("assess", response, request)
    candidate = deepcopy(baseline)
    candidate["id"] = "m05-assessment-" + digest(dict(source=source, baseline=baseline, response=value))[:16]
    candidate["supersedes"] = baseline["id"]
    # Pending initial-stage markers can end; actual technical issues cannot vanish.
    placeholders = {"Проверка ещё не завершена.", "Подготовлено чтение; оценивание ещё не выполнено."}
    candidate["issues"] = [i for i in candidate["issues"] if not (i["code"] == "not_run" and i["message"] in placeholders)]
    for key, items in value.items():
        if key == "facts":
            for fact in items:
                fact["basis_sha256"] = contract.reading_digest(candidate)
            candidate["facts"].extend(items)
        elif key == "score":
            candidate["score"].update(items)
            candidate["score"].update(authority="model" if items["value"] is not None else "none",
                source_ref="m05:offline-model-response" if items["value"] is not None else None)
        elif key == "issues":
            candidate["issues"].extend(i for i in items if i not in candidate["issues"])
        else:
            candidate[key] = items
    basis = contract.assessment_digest(candidate)
    score_ready = candidate["score"]["value"] is not None and candidate["score"]["status"] == "proposed"
    candidate["score"]["basis_sha256"] = basis if score_ready else None
    for item in candidate["feedback"]:
        item["basis_sha256"] = basis if item["status"] == "ready" else None
    questions = [q for q in candidate["clarifications"] if q["status"] != "answered"]
    ready = [f for f in candidate["feedback"] if f["status"] == "ready"]
    feedback_ready = (len(ready) == len(candidate["feedback"]) and
        not any("feedback" in i["blocks"] for i in candidate["issues"]) and
        not any(q["impact"] in {"substantive_feedback", "both"} for q in questions))
    candidate["readiness"] = dict(score="ready" if score_ready else "pending",
        feedback="ready" if feedback_ready else "partial" if ready else "pending")
    candidate["status"] = ("needs_fragment" if questions else "ready" if score_ready and feedback_ready and not candidate["issues"] else "needs_review")
    validate_candidate(candidate, source)
    return candidate


def materialize_reading_attempt(response, baseline, source):
    """Atomically validate all L04 pages, allowing forward cross-page links.

    No partial intermediate result is published: a relationship on page 1 may
    refer to a unit first described on page 2. Every page must occur exactly once
    in input order; IDs and full M01 references are checked on the union.
    """
    value = decode_response(response)
    pages = sorted((p for p in source["pages"] if p["role"] == "solution"), key=lambda p: p["order"])
    if set(value) != {"pages"} or not isinstance(value["pages"], list) or len(value["pages"]) != len(pages):
        raise PromptError("reading_attempt_pages")
    if baseline["facts"] or baseline["score"]["value"] is not None:
        raise PromptError("reading_requires_unassessed_baseline")
    candidate = deepcopy(baseline)
    for response_page, page in zip(value["pages"], pages):
        context = {k: candidate[k] for k in ("units", "corrections", "interpretations", "branches", "assumptions", "relations", "facts", "coverage", "issues")}
        fragment = validate_response("read", response_page, dict(page=page, context=context))
        for section in ("units", "interpretations", "relations", "branches", "assumptions"):
            items = fragment.get(section, [])
            if section in {"units", "interpretations", "relations"}:
                for item in items:
                    item["source"] = dict(kind="model_output", ref="m05:offline-reading-response")
            candidate[section].extend(items)
        candidate["coverage"] = [fragment["coverage"] if old["page_id"] == page["id"] else old for old in candidate["coverage"]]
    main = next((b for b in candidate["branches"] if b["id"] == "main"), None)
    if main is None:
        raise PromptError("missing_main_branch")
    main["unit_ids"] = [u["id"] for u in candidate["units"]]
    candidate["id"] = "m05-reading-attempt-" + digest(dict(source=source, baseline=baseline, response=value))[:16]
    candidate["supersedes"] = baseline["id"]
    validate_candidate(candidate, source)
    return candidate
