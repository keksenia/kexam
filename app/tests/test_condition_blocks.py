"""condition-reading-1.2: template blocks, code checks and stops; synthetic responses, no calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from app.live_condition import (SCHEMA, INSTRUCTIONS, classify_condition, condition_input, condition_sources,
    parse_expression, solve_condition, validate_condition)
from app.live_checking import FreshChecking, MESSAGES
from app.tests.test_foundation import image_data, valid_form
from app.tests import test_fresh_checking as fresh
from app.tests.test_fresh_checking import Fixture, condition, fixture_policy, reading

ROOT = Path(__file__).resolve().parents[2]


def standard_13(source="photo:1"):
    return dict(status="clear", template_id="ege2026-13", blocks=[
        dict(id="a", kind="equation", literal="а) Решите уравнение 2sin²x − sin x = 0.", source=source,
             lhs="2*sin(x)**2-sin(x)", rhs="0", relation="="),
        dict(id="b", kind="select_roots", literal="б) Найдите все корни этого уравнения, принадлежащие отрезку [−π; π].",
             source=source, interval=dict(left="-pi", right="pi", left_open=False, right_open=False))],
        restrictions=[], extra_text=[], uncertainties=[])


def standard_15(source="photo:1"):
    return dict(status="clear", template_id="ege2026-15", blocks=[
        dict(id="a", kind="inequality", literal="Решите неравенство (x² − 1)/(x − 2) ≥ 0.", source=source,
             lhs="(x**2-1)/(x-2)", rhs="0", relation=">=")],
        restrictions=[], extra_text=[], uncertainties=[])


class BlockValidationTests(unittest.TestCase):
    sources = {"text", "photo:1"}

    def test_standard_13_translates_to_solver_fields(self):
        value = validate_condition(standard_13(), "trigonometric_equation", sources=self.sources)
        stop, solver = classify_condition(value, "trigonometric_equation")
        self.assertIsNone(stop)
        self.assertEqual(solver, dict(status="clear", task_type="trigonometric_equation",
            literal="а) Решите уравнение 2sin²x − sin x = 0.\nб) Найдите все корни этого уравнения, принадлежащие отрезку [−π; π].",
            lhs="2*sin(x)**2-sin(x)", rhs="0", relation="=",
            interval=dict(left="-pi", right="pi", left_open=False, right_open=False), restrictions=[], uncertainties=[]))
        solved = solve_condition(solver)
        self.assertEqual(solved["status"], "solved", solved)
        self.assertEqual(solved["answer"], "{0, -pi, pi/6, 5*pi/6, pi}")

    def test_standard_15_has_no_interval(self):
        value = validate_condition(standard_15(), "inequality", sources=self.sources)
        stop, solver = classify_condition(value, "inequality")
        self.assertIsNone(stop)
        self.assertIsNone(solver["interval"])
        self.assertEqual((solver["lhs"], solver["rhs"], solver["relation"]), ("(x**2-1)/(x-2)", "0", ">="))
        self.assertEqual(solve_condition(solver)["status"], "solved")

    def test_template_requires_exact_blocks(self):
        broken = []
        value = standard_13(); value["blocks"].pop(); broken.append(("trigonometric_equation", value))
        value = standard_13(); value["blocks"].reverse(); broken.append(("trigonometric_equation", value))
        value = standard_15(); value["blocks"].append(deepcopy(standard_13()["blocks"][1])); broken.append(("inequality", value))
        value = standard_15(); value["blocks"][0]["relation"] = "="; broken.append(("inequality", value))
        value = standard_13(); value["blocks"][0]["relation"] = "<"; broken.append(("trigonometric_equation", value))
        for task_type, value in broken:
            with self.subTest(value=value["blocks"]), self.assertRaisesRegex(ValueError, "^condition_blocks$"):
                validate_condition(value, task_type, sources=self.sources)

    def test_source_must_be_a_sent_photo_or_the_form_text(self):
        for value, sources in ((standard_13("photo:2"), {"text", "photo:1"}), (standard_15("text"), {"photo:1"}),
                               ({**standard_15(), "extra_text": [dict(literal="при x > 0", source="photo:3")]}, {"photo:1"})):
            with self.subTest(sources=sources), self.assertRaisesRegex(ValueError, "^condition_source$"):
                validate_condition(value, "inequality" if value["template_id"] == "ege2026-15" else "trigonometric_equation", sources=sources)
        with self.assertRaisesRegex(ValueError, "^condition_schema$"):
            validate_condition(standard_15("photo:0"), "inequality", sources={"photo:0"})
        self.assertEqual(condition_sources("  ", [1, 2]), {"photo:1", "photo:2"})
        self.assertEqual(condition_sources("Решите", []), {"text"})

    def test_expressions_must_parse_with_solver_syntax(self):
        for lhs in ("2x", "x^2", "abs(x)", "sin(x", "x.real", "__import__('os')", "0.5*x", "sin(x, 2)", "ctg(x)"):
            value = standard_15(); value["blocks"][0]["lhs"] = lhs
            with self.subTest(lhs=lhs), self.assertRaisesRegex(ValueError, "^condition_expression$"):
                validate_condition(value, "inequality", sources=self.sources)
        value = standard_13(); value["blocks"][1]["interval"]["right"] = "pi/"
        with self.assertRaisesRegex(ValueError, "^condition_expression$"):
            validate_condition(value, "trigonometric_equation", sources=self.sources)
        value = standard_15(); value["restrictions"] = ["x in [0,1]"]; value["extra_text"] = [dict(literal="x ∈ [0; 1]", source="photo:1")]
        with self.assertRaisesRegex(ValueError, "^condition_expression$"):
            validate_condition(value, "inequality", sources=self.sources)

    def test_parser_copy_matches_solver_parser(self):
        accepted = ("sin(x)", "log(x+3,2)", "exp(-x)", "2**x", "sqrt(x**2+1)", "+x", "-(x-1)/(x+2)", "tan(2*x-pi/3)")
        rejected = ("2x", "x^2", "abs(x)", "x.real", "sin(x,1)", "x if 1 else 2", "y", "10001*x", "[x]")
        for lhs in accepted + rejected:
            with self.subTest(lhs=lhs):
                try: parse_expression(lhs); ok = True
                except ValueError: ok = False
                worker = solve_condition(condition("inequality", lhs=lhs, relation="<="))
                syntax_failure = worker.get("reason") in {"expression_syntax_not_supported", "solver_not_supported"}
                self.assertEqual(ok, lhs in accepted)
                self.assertEqual(ok, not syntax_failure, worker)
        for text in ("0 < x <= 3", "x != 1", "x = 2"): parse_expression(text, restriction=True)
        for text in ("x", "x in [0,1]", "0 < x < 1 < 2 < 3"):
            with self.assertRaises(ValueError): parse_expression(text, restriction=True)

    def test_nonstandard_mismatch_multiple_and_uncertain_are_never_solved(self):
        part_b = standard_13(); part_b["template_id"] = None
        part_b["blocks"][1]["literal"] = "б) Найдите наибольший отрицательный корень этого уравнения."
        extra = standard_15(); extra["extra_text"] = [dict(literal="Укажите наименьшее целое решение.", source="photo:1")]
        restricted = standard_15(); restricted["restrictions"] = ["x != 3"]
        cases = (("trigonometric_equation", part_b, "condition_nonstandard"), ("inequality", extra, "condition_nonstandard"),
                 ("inequality", restricted, "condition_nonstandard"), ("trigonometric_equation", standard_15(), "condition_task_mismatch"),
                 ("inequality", {**standard_15(), "status": "multiple_tasks", "blocks": []}, "multiple_tasks"),
                 ("inequality", {**standard_15(), "status": "uncertain", "uncertainties": ["не виден знак"]}, "condition_uncertain"))
        for task_type, value, code in cases:
            with self.subTest(code=code):
                validate_condition(value, task_type, sources=self.sources)
                self.assertEqual(classify_condition(value, task_type), (code, None))

    def test_bare_task_label_is_not_extra_condition_text(self):
        # Live run 9.10: Gemini put the heading «Задание 6» into extra_text of a standard №13.
        for label in ("Задание 6", "задача 13.", "№ 13", "13", "6)"):
            value = standard_13(); value["extra_text"] = [dict(literal=label, source="photo:1")]
            with self.subTest(label=label):
                stop, solver = classify_condition(validate_condition(value, "trigonometric_equation", sources=self.sources), "trigonometric_equation")
                self.assertIsNone(stop); self.assertEqual(solver["lhs"], "2*sin(x)**2-sin(x)")
        for text in ("Задание 6. Укажите наименьший корень.", "2026", "x > 0", "Вариант 6"):
            value = standard_13(); value["extra_text"] = [dict(literal=text, source="photo:1")]
            with self.subTest(text=text):
                self.assertEqual(classify_condition(value, "trigonometric_equation"), ("condition_nonstandard", None))

    def test_old_saved_versions_keep_their_solver_input(self):
        old = dict(reading=condition(), attempt_id="A1")
        self.assertIs(condition_input(old), old["reading"])
        new = dict(reading=standard_15(), solver_input=None, attempt_id="A2")
        self.assertIsNone(condition_input(new))

    def test_instruction_and_schema_cover_the_task(self):
        for phrase in ("Не решай задачу", "не смотри в решение ученика", "extra_text", "template_id=null", "status=uncertain", "без Markdown"):
            self.assertIn(phrase, INSTRUCTIONS)
        self.assertEqual(set(SCHEMA["required"]), {"status", "template_id", "blocks", "restrictions", "extra_text", "uncertainties"})
        for code in ("condition_nonstandard", "condition_task_mismatch", "condition_source", "condition_blocks", "condition_expression"):
            self.assertIn(code, MESSAGES)


class BlockRunTests(unittest.TestCase):
    """Full fresh path with a synthetic model; the condition photo is photo:1."""
    # Shared isolation: temporary stores and a socket blocker; FreshTests itself is not re-run here.
    setUp, tearDown = fresh.FreshTests.setUp, fresh.FreshTests.tearDown

    def service(self, response, **kwargs):
        fixture = Fixture(response)
        service = FreshChecking(self.store, policy=fixture_policy(), ledger=self.root / ("ledger-" + uuid.uuid4().hex),
            transport_factory=fixture.factory, **kwargs)
        self.services.append(service); return service, fixture

    def run_blocks(self, response, task_type="trigonometric_equation", text=True):
        service, fixture = self.service(response)
        form = valid_form(task_type); form['task_text']='';form["task"] = [image_data()]
        if not text: form["task_text"] = ""
        work = self.store.save(form)
        job = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)["job"]
        return service, fixture, job

    def test_standard_13_and_15_complete_with_translated_condition(self):
        for task_type, response in (("trigonometric_equation", standard_13()), ("inequality", standard_15())):
            with self.subTest(task_type=task_type):
                service, fixture, job = self.run_blocks(response, task_type)
                self.assertEqual(job["state"], "completed", job)
                version = job["condition_versions"][-1]
                self.assertEqual(version["reading"], response)
                self.assertEqual(version["solver_input"]["lhs"], response["blocks"][0]["lhs"])
                source = json.loads((service.directory / job["id"] / "verified-input.json").read_text())
                self.assertEqual(source["task"]["statement"]["text"], "\n".join(b["literal"] for b in response["blocks"]))
                key = next(f for f in job["result"]["facts"] if f["id"] == "condition-key")
                self.assertEqual(key["code_check"]["arguments"], version["solver_input"])
                data = json.loads(fixture.requests[0].payload["messages"][1]["content"][0]["text"])
                self.assertEqual(data["sources"], ["photo:1"])
                labels = [json.loads(p["text"]) for p in fixture.requests[0].payload["messages"][1]["content"][1:] if p["type"] == "text"]
                self.assertEqual(labels, [dict(id="task-1", role="task", source="photo:1")])

    def test_nonstandard_part_b_and_extra_text_stop_before_solving(self):
        part_b = standard_13(); part_b["template_id"] = None
        part_b["blocks"][1]["literal"] = "б) Найдите наибольший отрицательный корень этого уравнения."
        extra = standard_15(); extra["extra_text"] = [dict(literal="Укажите наименьшее целое решение.", source="photo:1")]
        for task_type, response in (("trigonometric_equation", part_b), ("inequality", extra)):
            with self.subTest(task_type=task_type):
                solver = Mock(side_effect=AssertionError("Nonstandard condition must not be solved"))
                service, fixture = self.service(response, solver=solver)
                form = valid_form(task_type); form['task_text']='';form["task"] = [image_data()]
                work = self.store.save(form)
                job = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)["job"]
                self.assertEqual((job["state"], job["error_code"]), ("needs_clarification", "condition_nonstandard"))
                self.assertEqual(job["message"], MESSAGES["condition_nonstandard"])
                self.assertEqual(len(fixture.requests), 1); self.assertIsNone(job["result_ref"])
                self.assertEqual(job["solver"], dict(status="not_run", reason="condition_nonstandard"))
                self.assertIsNone(job["condition_versions"][-1]["solver_input"])
                self.assertEqual(job["condition_versions"][-1]["reading"], response)

    def test_wrong_source_is_an_invalid_response(self):
        service, fixture, job = self.run_blocks(standard_15("text"), "inequality", text=False)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "invalid_response"))
        self.assertEqual(job["response_validation"], dict(stage="statement_ocr", code="condition_source"))
        self.assertEqual(job["message"], MESSAGES["condition_source"]); self.assertEqual(len(fixture.requests), 1)

    def test_task_type_mismatch_stops_for_the_user(self):
        service, fixture, job = self.run_blocks(standard_15(), "trigonometric_equation")
        self.assertEqual((job["state"], job["error_code"]), ("needs_clarification", "condition_task_mismatch"))
        self.assertEqual(len(fixture.requests), 1); self.assertIsNone(job["result_ref"])

    def test_multiple_tasks_and_uncertain(self):
        service, fixture, job = self.run_blocks({**standard_13(), "status": "multiple_tasks", "blocks": []})
        self.assertEqual((job["state"], job["error_code"]), ("needs_clarification", "multiple_tasks"))
        self.assertEqual(len(fixture.requests), 1)
        uncertain = {**standard_13(), "status": "uncertain", "uncertainties": ["Не виден знак перед sin x."]}
        service, fixture, job = self.run_blocks(uncertain)
        self.assertEqual((job["state"], job["error_code"]), ("needs_clarification", "condition_uncertain"))
        self.assertEqual(len(fixture.requests), 2); self.assertEqual(len(job["condition_versions"]), 2)
        recheck = json.loads(fixture.requests[1].payload["messages"][1]["content"][0]["text"])["targeted_recheck"]
        self.assertEqual(recheck["blocks"], uncertain["blocks"]); self.assertEqual(recheck["uncertainties"], uncertain["uncertainties"])

    def test_old_job_format_still_opens_and_confirms(self):
        service, fixture = self.service(condition(rhs="2"))
        form = valid_form(); form['task_text']='';form["task"] = [image_data()]
        work = self.store.save(form)
        job = service.create(dict(work_id=work["id"], model="G", complete=True, single_task=True), background=False)["job"]
        self.assertEqual(job["error_code"], "condition_suspicious")
        # Rewrite the manifest as condition-reading-1.1.0 saved it: reading was the solver input.
        path = service.directory / job["id"] / "job.json"
        saved = json.loads(path.read_text())
        saved["condition_versions"] = [dict(reading=v["solver_input"], attempt_id=v["attempt_id"]) for v in saved["condition_versions"]]
        path.write_text(json.dumps(saved, ensure_ascii=False))
        opened = service.get(job["id"])
        self.assertEqual(opened["condition_versions"][-1]["reading"]["lhs"], "sin(x)")
        result = service.confirm_empty_condition(job["id"], dict(literal=opened["condition_versions"][-1]["reading"]["literal"],
            confirm_empty=True), background=False)
        self.assertEqual(result["job"]["state"], "completed", result)

    def test_card_renders_blocks_and_old_format(self):
        page = (ROOT / "app/static/index.html").read_text()
        for fragment in ("Array.isArray(latest.blocks)", "'#photo-'+work.id+'-task-'", "набранный текст условия",
                         "Не вошло в шаблон", "Заголовок задания", "latest.literal", "version.solver_input",
                         "'После списания '+availability.budget_rub+' ₽"):
            self.assertIn(fragment, page)
        self.assertNotIn("После списания 200 ₽", page)

    def test_task_label_in_a_standard_condition_still_completes(self):
        response = standard_13(); response["extra_text"] = [dict(literal="Задание 6", source="photo:1")]
        service, fixture, job = self.run_blocks(response)
        self.assertEqual(job["state"], "completed", job)
        self.assertEqual(job["condition_versions"][-1]["reading"]["extra_text"], response["extra_text"])


class BudgetIncreaseTests(unittest.TestCase):
    """A larger amendment budget is accepted only with the user's recorded increase."""
    def approval(self, **amendment):
        from app.call_guard import digest
        from app import live_checking
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = json.loads((ROOT / "planning/l05/AUTHORIZATION.json").read_text())
            (root / "planning/l05").mkdir(parents=True); (root / "planning/m17").mkdir(parents=True)
            (root / "planning/l05/AUTHORIZATION.json").write_text(json.dumps(original))
            value = dict(id=original["id"], original_authorization_sha256=digest(original), budget_rub=original["budget_rub"],
                         technical_retry_reserve=original["technical_retry_reserve"])
            value.update(amendment)
            (root / "planning/m17/FRESH_UPLOAD_AUTHORIZATION.json").write_text(json.dumps(value, ensure_ascii=False))
            with patch.object(live_checking, "ROOT", root):
                return live_checking.active_approval()

    def test_increase_requires_explicit_matching_record(self):
        self.assertEqual(self.approval()["budget_rub"], 200)
        increase = dict(budget_rub=300, user_message="лимит до 300 увеличь")
        self.assertEqual(self.approval(budget_rub=300, budget_increase=increase)["budget_rub"], 300)
        for changes in (dict(budget_rub=300), dict(budget_rub=300, budget_increase={**increase, "budget_rub": 400}),
                        dict(budget_rub=300, budget_increase={**increase, "user_message": " "}),
                        dict(budget_rub=150, budget_increase={**increase, "budget_rub": 150}), dict(budget_rub="300", budget_increase=increase)):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "authorization_amendment_mismatch"):
                self.approval(**changes)


if __name__ == "__main__": unittest.main()
