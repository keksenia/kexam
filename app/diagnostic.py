"""Run an explicitly synthetic M04 smoke scenario in separate temporary storage.

python3 -m app.diagnostic --task-type trigonometric_equation --output /tmp/m04-13.json
This demonstrates transport/state handling, never OCR or grading accuracy.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw

from app.pipeline import OfflineMockAdapter, Pipeline
from app.server import WorkStore
from contracts.ege_v1 import validate as contract


class SyntheticFixtureAdapter(OfflineMockAdapter):
    id = "synthetic-fixture"
    version = "1"

    def read(self, request, image):
        if not request["synthetic"]:
            return super().read(request, image)
        number = request["task"]["exam_number"]
        page = request["page"]["id"]
        literals = ["sin x = 0", "x = πn, n ∈ Z", "0 ≤ πn ≤ π ⇒ n ∈ {0; 1}, x ∈ {0; π}"] if number == 13 else ["x - 1 > 0", "x > 1"]
        return dict(units=[dict(id=f"{page}-u{i}", kind="math", literal=value, reading_status="clear", activity="active",
                               anchors=[dict(page_id=page, box=None)], author="student") for i, value in enumerate(literals, 1)],
                    interpretations=[], features=["pi_families", "explicit_interval"] if number == 13 else ["inequality", "unknown_method"],
                    coverage=dict(page_id=page, status="complete", reason="Заранее заданная синтетическая страница; OCR не выполнялся.",
                                  score_scope="accounted", score_scope_reason="Полный искусственный сценарий задан автором теста."))

    def assess(self, request):
        if not request["synthetic"]:
            return super().assess(request)
        uids = [u["id"] for u in request["context"]["units"]]
        pages = [c["page_id"] for c in request["context"]["coverage"]]
        return dict(facts=[dict(id="fixture-fact", unit_ids=uids, interpretation_ids=[], claim="Все требования искусственного сценария выполнены.",
                               method="model", status="supported", reason="Фиктивный заранее заданный ответ; не проверка математики.",
                               tool_version=None, depends_on_fact_ids=[], branch_id="main", assumption_ids=[], context_unit_ids=[],
                               claim_kind="completeness", domain="Искусственный пример целиком")],
                    requirements=[dict(id="fixture-requirement", criterion_id="fixture-criterion", branch_id="main",
                                       requirement="Полностью решить искусственное задание.", status="fulfilled", unit_ids=uids,
                                       fact_ids=["fixture-fact"], coverage_page_ids=pages, reason="Заранее заданное условие диагностического сценария.")],
                    score=dict(status="proposed", value=2, criterion_ids=["fixture-criterion"], evidence_unit_ids=uids, finding_ids=[],
                               requirement_ids=["fixture-requirement"], case_check_ids=[], ambiguity_group_ids=[],
                               rationale="2/2 — фиктивный ответ для проверки каркаса; не результат чтения или математического оценивания."),
                    feedback=[dict(id="fixture-feedback", finding_ids=[], unit_ids=uids, text="Диагностический пример: передача результата работает.",
                                   status="ready", audience="teacher", kind="explanation")], issues=[])


def fixture_criterion(task_type):
    text = "Искусственная шкала для проверки передачи: заданный полный ответ получает 2. Не критерий ФИПИ."
    return dict(id="fixture-criterion", package_version="m04-synthetic-1", exam_year=2026,
                exam_number=13 if task_type == "trigonometric_equation" else 15,
                source_ref="m04:synthetic-fixture", source_sha256=contract.digest(text), locator="Explicit synthetic fixture",
                rule_text=text, authority="synthetic")


def fixture_form(task_type):
    image = Image.new("RGB", (640, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "SYNTHETIC M04 FIXTURE - NOT A STUDENT WORK", fill="black")
    draw.text((20, 60), "Predetermined fake response. No OCR or math evaluation.", fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return dict(redacted=True, exam_year=2026, task_type=task_type,
                task_text="а) Решите sin x = 0. б) Отберите корни на [0; π]." if task_type == "trigonometric_equation" else "Решите x - 1 > 0.",
                solution=["data:image/png;base64," + base64.b64encode(output.getvalue()).decode()], task=[])


def run_fixture(task_type, storage):
    works = WorkStore(storage)
    work = works.save(fixture_form(task_type))
    pipeline = Pipeline(works, SyntheticFixtureAdapter(), criteria=[fixture_criterion(task_type)], allow_synthetic=True)
    try:
        run = pipeline.create(work["id"], background=False, synthetic=True)
        run = pipeline.execute(run["id"])
        result = run["result"]
        return dict(diagnostic_only=True, demonstrates="M04 transport and lifecycle only; no OCR/math quality evidence",
                    task_type=task_type, state=run["state"], error_code=run["error_code"], source_matches=run["source_matches"],
                    input=run["input"], result=result, events=run["events"],
                    result_versions=run["result_versions"],
                    validation_errors=contract.validate(result, run["input"]) if result else ["No result"], paid_calls=0)
    finally:
        pipeline.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-type", choices=["trigonometric_equation", "inequality"], default="trigonometric_equation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ege-m04-synthetic-") as directory:
        evidence = run_fixture(args.task_type, Path(directory) / "works")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: evidence[key] for key in ("diagnostic_only", "task_type", "state", "error_code", "validation_errors", "paid_calls")}, ensure_ascii=False))
    if evidence["state"] != "completed" or evidence["validation_errors"] or evidence["result"]["score"]["value"] != 2:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
