"""Build identical model-content payloads for L04, entirely offline.

Only the four frozen L03 input/transcript files and PNGs are read. Evaluator
files are never opened. Free text is not generically anonymized: these known
source files are hash-bound; new datasets need their own reviewed projection.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .runtime import (ROOT, VERSION, PromptError, build_request, canonical,
                      digest, read_json, rubric, response_schema, freeze_identity)

CASES = ("L03-13-01", "L03-13-02", "L03-15-01", "L03-15-02")


def _neutral_reading_source(line, transcript):
    text = line.get("source", transcript.get("authority", ""))
    # An integrated earlier human reading is not blanket expert verification.
    return ("prior_human_reading_integrated" if "human" in text and "agent visual" not in text
            else "agent_visual_reading")


def project_transcript(transcript, *, page_ids):
    """An explicit field projection: impact/source labels/notes never pass through."""
    context = {k: [] for k in ("units", "corrections", "interpretations", "branches", "assumptions", "relations", "facts", "coverage", "issues")}
    for line in transcript["lines"]:
        if type(line.get("page")) is not int or not 1 <= line["page"] <= len(page_ids):
            raise PromptError("transcript_page")
        unit_id = line["id"]
        kind = line["kind"]
        is_literal = kind in {"literal", "literal_math"}
        is_diagram = kind == "diagram_observation"
        is_unreadable = "unreadable" in kind
        # Editorial observations have their own layer, never a literal student quote.
        unit = dict(id=unit_id, kind="math" if is_literal else "diagram" if is_diagram else "text",
                    literal=None if is_diagram or is_unreadable else line["text"],
                    reading_status="unreadable" if is_unreadable else "clear",
                    activity="crossed_out" if kind == "unreadable_crossed_draft" else "active",
                    anchors=[dict(page_id=page_ids[line["page"] - 1], box=None)], author="student" if is_literal or is_diagram else "unknown" if is_unreadable else "system",
                    source=dict(kind="archive", ref=_neutral_reading_source(line, transcript)))
        context["units"].append(unit)
        if not is_literal:
            context["interpretations"].append(dict(id="reading-" + unit_id, unit_ids=[unit_id], meaning=line["text"],
                status="proposed", source=dict(kind="archive", ref="prepared_visual_observation"), branch_id="main",
                assumption_ids=[], context_unit_ids=[]))
    for index, meaning in enumerate(transcript.get("interpretations", []), 1):
        if meaning.get("literal_ocr_equivalence") is not False or "explicit user clarification" not in meaning.get("authority", ""):
            raise PromptError("unrecognized_prepared_interpretation")
        context["interpretations"].append(dict(id=f"accepted-meaning-{index}", unit_ids=[meaning["line_id"]],
            meaning=meaning["accepted_meaning"], status="human_confirmed",
            source=dict(kind="human_review", ref="prior_explicit_reading_clarification"), branch_id="main",
            assumption_ids=[], context_unit_ids=[]))
    # Keep neutral, materially relevant observations omitted by a raw allowlist.
    # These are reading notes only; correctness/diagnosis in `impact` is excluded.
    for line in transcript["lines"]:
        note = line.get("note")
        if note:
            note = note.replace("; не исправлять при чтении", "")
            context["interpretations"].append(dict(id="note-" + line["id"], unit_ids=[line["id"]], meaning=note,
                status="proposed", source=dict(kind="archive", ref="prepared_visual_observation"), branch_id="main",
                assumption_ids=[], context_unit_ids=[]))
    context["branches"] = [dict(id="main", parent_id=None, label="Выбранная попытка", unit_ids=[u["id"] for u in context["units"]], assumption_ids=[], status="active")]
    for page_id in page_ids:
        unresolved = any(u["reading_status"] in {"ambiguous", "unreadable"} and any(a["page_id"] == page_id for a in u["anchors"])
                         for u in context["units"])
        context["coverage"].append(dict(page_id=page_id, status="partial" if unresolved else "complete",
            reason="Подготовленное чтение; редакторские наблюдения отделены от буквальных строк.",
            score_scope="unresolved" if unresolved else "accounted",
            score_scope_reason="Существенность нечитаемых/редакторских участков должен установить проверяющий." if unresolved else "Все выбранные строки представлены; это не оценка решения."))
    return context


def _load_case(case_id):
    if case_id not in CASES:
        raise PromptError("unknown_l03_case")
    freeze_identity()
    source_dir = ROOT / "data_local/l03/model_inputs" / case_id
    frozen = read_json(ROOT / "planning/m05/l03_freeze.json")[case_id]
    for filename, expected in frozen.items():
        if hashlib.sha256((source_dir / filename).read_bytes()).hexdigest() != expected:
            raise PromptError("l03_source_drift")
    source = read_json(source_dir / "input.json")
    transcript = read_json(source_dir / "shared_transcript.json")
    source_images = [source["condition_image"]] + source["solution_images"]
    if any((ROOT / item["path"]).resolve().parent != source_dir.resolve() for item in source_images):
        raise PromptError("l03_asset_path")
    page_files = [Path(item["path"]).name for item in source_images]
    if any(item["sha256"] != frozen[name] for item, name in zip(source_images, page_files)):
        raise PromptError("l03_asset_hash")
    if [item["page_order"] for item in source["solution_images"]] != list(range(1, len(source["solution_images"]) + 1)):
        raise PromptError("l03_page_order")
    # L03 stores filenames; reject a newly changed external path even after refreeze.
    if any(Path(name).name != name or not name.endswith(".png") for name in page_files):
        raise PromptError("l03_asset_path")
    pages = [dict(id="task-1", role="task", asset_id="task-1", sha256=frozen[page_files[0]])]
    pages += [dict(id=f"solution-{i}", role="solution", asset_id=f"solution-{i}", sha256=frozen[name]) for i, name in enumerate(page_files[1:], 1)]
    task_type = "trigonometric_equation" if source["exam_type"] == 13 else "inequality"
    task = dict(exam_year=2026, exam_number=source["exam_type"], task_type=task_type,
        statement=dict(text=source["statement_text"], format="plain_latex", completeness="confirmed", missing=[]))
    return source, transcript, task, pages, [dict(asset_id=p["asset_id"], path=str(source_dir / name), sha256=p["sha256"]) for p, name in zip(pages, page_files)]


def build_l04_case(case_id):
    """One OCR request per attempt; E212 keeps both pages, not two OCR calls."""
    source, transcript, task, pages, assets = _load_case(case_id)
    manifest = [{k: p[k] for k in ("asset_id", "sha256", "role", "id")} for p in pages]
    empty = {k: [] for k in ("units", "corrections", "interpretations", "branches", "assumptions", "relations", "facts", "coverage", "issues")}
    read_request = dict(task=task, page=pages[1], task_pages=[pages[0]], context=empty)
    reading = build_request("read", read_request, image_manifest=manifest)
    # Same stage rules and per-page output contract as the app, wrapped for an
    # entire L04 attempt so its pre-agreed 8 OCR requests remain exactly eight.
    reading["payload"]["data"].pop("page")
    reading["payload"]["data"]["solution_pages"] = deepcopy(pages[1:])
    reading["payload"]["data"]["selection_scope"] = source["scope_note"]
    reading["payload"]["system"] += "\n\nРежим whole_attempt: текущая попытка включает все solution_pages. Верни объект pages: один ответ READ для каждой страницы в указанном порядке; это один запрос. Установи связи между страницами, не выдумывай хронологию повторной цепочки."
    per_page = reading["payload"]["response_schema"]
    definitions = per_page.pop("$defs")
    reading["payload"]["response_schema"] = dict(type="object", properties={"pages":dict(type="array", minItems=len(pages)-1, maxItems=len(pages)-1, items=per_page)}, required=["pages"], additionalProperties=False, **{"$defs":definitions})
    reading.update(payload_sha256=digest(reading["payload"]), content_bytes=len(canonical(reading["payload"]).encode()))
    context = project_transcript(transcript, page_ids=[p["id"] for p in pages[1:]])
    assessment_request = dict(task=task, capture=dict(pages_complete="confirmed", student_attempt="unknown"),
        context=context, criteria=rubric(task["task_type"]), plan=dict(features=[], check_outcomes=[]))
    key = read_json(ROOT / "planning/m05/reference_keys.json")[case_id]
    assessment = build_request("assess", assessment_request, reference_key=key, image_manifest=manifest)
    assessment["payload"]["data"]["selection_scope"] = source["scope_note"]
    assessment.update(payload_sha256=digest(assessment["payload"]), content_bytes=len(canonical(assessment["payload"]).encode()))
    return dict(case_id=case_id, reading=reading, assessment=assessment, assets=assets,
                content_identity=dict(transcript=digest(context), key=digest(key), rubric=digest(assessment_request["criteria"]), images=digest(manifest)),
                scope="Prepared dev inputs; no OCR/model/score-quality result", paid_calls=0)


def l04_input_and_baseline(case_id):
    """Local M01 materialization; private source IDs stay outside model content."""
    from app.pipeline import base_result
    from contracts.ege_v1 import validate as contract
    source, transcript, task, pages, _ = _load_case(case_id)
    descriptors = [source["condition_image"]] + source["solution_images"]
    full_pages = []
    for page, descriptor in zip(pages, descriptors):
        full_pages.append(dict(**page, order=1 if page["role"] == "task" else descriptor["page_order"],
            width=descriptor["size_px"][0], height=descriptor["size_px"][1], region=None,
            selection_note="Выбранная область L03; исходные пиксели сохранены."))
    full_task = deepcopy(task)
    full_task["source_label"] = "Prepared L03 dev attempt"
    full_task["statement"]["source"] = dict(kind="archive", ref="l03-prepared-condition")
    source_input = dict(schema_version="2.0.0", kind="input", id="m05-" + case_id, revision=1,
        created_at="2026-10-07T00:00:00Z", task=full_task, pages=full_pages,
        capture=dict(pages_complete="confirmed", student_attempt="unknown"),
        privacy=dict(status="unchecked", reviewer_ref=None),
        provenance=dict(source=dict(kind="archive", ref="l03-prepared-dev"), dataset_role="dev", duplicate_group=None))
    configuration = dict(package=freeze_identity())
    identity = digest(dict(source=source_input, configuration=configuration))[:16]
    result = base_result(source_input, dict(id="m05-l04-run-" + identity, configuration=configuration))
    result.update(id="m05-l04-baseline-" + identity, created_at="2026-10-07T00:00:00Z")
    result.update(project_transcript(transcript, page_ids=[p["id"] for p in pages[1:]]))
    result["issues"] = [dict(code="not_run", message="Подготовлено чтение; оценивание ещё не выполнено.", retryable=False, blocks=["score", "feedback"])]
    result["criteria"] = rubric(task["task_type"])
    errors = contract.validate(source_input) + contract.validate(result, source_input)
    if errors:
        raise PromptError("l04_baseline_contract")
    return source_input, result


def freeze_l04(output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case_id in CASES:
        case = build_l04_case(case_id)
        for stage in ("reading", "assessment"):
            # Both configurations reference exactly these same content bytes.
            target = output_dir / f"{case_id}-{stage}.json"
            target.write_text(canonical(case[stage]["payload"]) + "\n", encoding="utf-8")
            results.append(dict(case_id=case_id, stage=stage, file=str(target), payload_sha256=case[stage]["payload_sha256"],
                                file_sha256=hashlib.sha256(target.read_bytes()).hexdigest(), content_bytes=case[stage]["content_bytes"],
                                configurations=["G", "D"], assets=case["assets"], content_identity=case["content_identity"]))
    report = dict(version=VERSION, package_sha256=freeze_identity(), requests=results, content_payloads=8,
                  base_model_calls_if_authorized=16, network_enabled=False, paid_calls=0,
                  launch_ready=False, blockers=["L05: approved budget and verified provider limits", "M17: provider transport through M14", "L04/B03: evaluate real model outputs"])
    (output_dir / "dispatch.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description="Freeze private M05/L04 payloads locally; never send them.")
    parser.add_argument("--output", type=Path, default=ROOT / "data_local/m05/l04")
    args = parser.parse_args()
    result = freeze_l04(args.output)
    print(json.dumps({k: result[k] for k in ("version", "package_sha256", "content_payloads", "network_enabled", "paid_calls", "launch_ready")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
