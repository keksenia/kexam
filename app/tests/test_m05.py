"""M05 transport/prompt regressions. Synthetic responses are not model quality."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.diagnostic import SyntheticFixtureAdapter, fixture_form, fixture_criterion
from app.pipeline import Pipeline, base_result, compact_context
from app.server import WorkStore
from app.m05.runtime import (ROOT, PromptError, PreparedFixtureAdapter, build_request, candidate_cases,
    canonical, decode_response, digest, freeze_identity, read_json, rubric, selected_modules, validate_response)
from app.m05.l04 import CASES, build_l04_case, l04_input_and_baseline, project_transcript, freeze_l04
from app.m05.result_boundary import materialize_assessment, materialize_reading, materialize_reading_attempt
from contracts.ege_v1 import validate as contract


def raw_synthetic(task_type="trigonometric_equation"):
    example = read_json(ROOT / "planning/m05/examples" / ("synthetic_13.json" if task_type == "trigonometric_equation" else "synthetic_15.json"))
    return example


def synthetic_baseline():
    example = read_json(ROOT / "contracts/ege_v1/examples/principles_states.json")["examples"][0]
    source, result = deepcopy(example["input"]), deepcopy(example["result"])
    baseline = base_result(source, dict(id="test-m05", configuration={}))
    for key in ("units", "interpretations", "assumptions", "relations", "branches", "corrections", "coverage", "criteria"):
        baseline[key] = deepcopy(result[key])
    raw = {k: deepcopy(result[k]) for k in ("facts", "findings", "clarifications", "requirements", "case_checks", "scenarios", "ambiguity_groups", "feedback", "score", "issues")}
    for fact in raw["facts"]:
        fact.pop("basis_sha256"); fact["method"] = "model"
    for feedback in raw["feedback"]:
        feedback.pop("basis_sha256")
    for key in ("basis_sha256", "authority", "source_ref", "max_value"):
        raw["score"].pop(key)
    raw["score"]["status"] = "proposed"
    return source, baseline, raw


class M05Tests(unittest.TestCase):
    def assertRejected(self, operation, *args, code=None, **kwargs):
        with self.assertRaises(PromptError) as caught:
            operation(*args, **kwargs)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_freeze_exists_and_is_verified(self):
        self.assertEqual(len(freeze_identity()), 64)

    def test_duplicate_nan_fenced_and_array_json_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '```json\n{}\n```', '[]', '{"x":Infinity}'):
            with self.subTest(raw=raw): self.assertRejected(decode_response, raw)

    def test_output_limit_is_explicit(self):
        self.assertRejected(decode_response, {"x":"a"*600000}, code="response_too_large")

    def test_reading_never_repairs_wrong_literal(self):
        example = raw_synthetic()
        response = deepcopy(example["reading"])
        response["units"][0]["literal"] = "sin x = 2; x = πn"
        result = validate_response("read", response, {"page":{"id":"solution-1"},"context":{}})
        self.assertEqual(result["units"][0]["literal"], "sin x = 2; x = πn")
        self.assertEqual(response, result)

    def test_diagram_null_literal_accepted_text_literal_rejected(self):
        response = deepcopy(raw_synthetic()["reading"])
        response["units"][0].update(kind="diagram", literal=None)
        req = {"page":{"id":"solution-1"},"context":{}}
        validate_response("read",response,req)
        response["units"][0]["literal"]="круг"
        self.assertRejected(validate_response,"read",response,req,code="diagram_literal")

    def test_empty_clear_math_rejected(self):
        response=deepcopy(raw_synthetic()["reading"]); response["units"][0]["literal"]=None
        self.assertRejected(validate_response,"read",response,{"page":{"id":"solution-1"},"context":{}},code="empty_clear_reading")

    def test_model_cannot_create_human_or_runtime_provenance(self):
        response=deepcopy(raw_synthetic()["reading"]); response["units"][0]["source"]={"kind":"human_review","ref":"invented"}
        self.assertRejected(validate_response,"read",response,{"page":{"id":"solution-1"},"context":{}})
        _,baseline,raw=synthetic_baseline()
        for method in ("exact_code","human","counterexample"):
            value=deepcopy(raw);value["facts"][0]["method"]=method
            self.assertRejected(validate_response,"assess",value,{"context":compact_context(baseline),"criteria":baseline["criteria"]})

    def test_read_score_and_operation_requests_rejected(self):
        for field,value in (("score",{}),("operation_requests",[]),("chain_of_thought","hidden")):
            response=deepcopy(raw_synthetic()["reading"]);response[field]=value
            self.assertRejected(validate_response,"read",response,{"page":{"id":"solution-1"},"context":{}})

    def test_read_cross_page_anchor_duplicate_and_hypothesis_rejected(self):
        req={"page":{"id":"solution-1"},"context":{}}
        wrong=deepcopy(raw_synthetic()["reading"]);wrong["units"][0]["anchors"][0]["page_id"]="solution-2"
        self.assertRejected(validate_response,"read",wrong,req,code="anchor_page_mismatch")
        wrong=deepcopy(raw_synthetic()["reading"]);wrong["units"][1]["id"]=wrong["units"][0]["id"]
        self.assertRejected(validate_response,"read",wrong,req,code="duplicate_or_existing_id")
        wrong=deepcopy(raw_synthetic()["reading"]);wrong["assumptions"]=[dict(id="hyp",claim="x≠0",origin="hypothesis",source_ref="guess",status="established",branch_id="main")]
        self.assertRejected(validate_response,"read",wrong,req,code="established_hypothesis")

    def test_assessment_full_semantics_and_no_input_mutation(self):
        source,baseline,raw=synthetic_baseline(); before=deepcopy((source,baseline,raw))
        result=materialize_assessment(raw,baseline,source)
        self.assertEqual(result["score"]["value"],2)
        self.assertEqual(contract.validate(result,source),[])
        self.assertEqual((source,baseline,raw),before)

    def test_full_semantics_catches_unknown_unit_reference(self):
        source,baseline,raw=synthetic_baseline();raw["facts"][0]["unit_ids"]=["missing-unit"]
        self.assertRejected(materialize_assessment,raw,baseline,source,code="full_m01_contract")

    def test_full_semantics_catches_false_student_accusation(self):
        source,baseline,raw=synthetic_baseline()
        raw["findings"]=[dict(id="false",category="mathematical_error",unit_ids=["u1"],fact_ids=[],description="Ошибка",reason="Предположение",affects_score="no",criterion_ids=[],primary_finding_id=None,status="established",evidence_assertions=[],requirement_ids=[])]
        self.assertRejected(materialize_assessment,raw,baseline,source,code="full_m01_contract")

    def test_uncertain_optional_feedback_does_not_hide_score(self):
        source,baseline,raw=synthetic_baseline()
        raw["feedback"][0].update(status="pending",audience="teacher")
        raw["clarifications"]=[dict(id="optional-question",unit_ids=["u3"],anchors=[dict(page_id="page-1",box=None)],question="Что означает дополнительная пометка?",options=[],impact="substantive_feedback",reason="Балл независимо обоснован полной цепочкой.",status="open",response=None,actor_ref=None,ambiguity_group_id=None)]
        result=materialize_assessment(raw,baseline,source)
        self.assertEqual(result["score"]["value"],2)
        self.assertEqual(result["readiness"],dict(score="ready",feedback="pending"))

    def test_score_affecting_unknown_blocks_score(self):
        source,baseline,raw=synthetic_baseline()
        raw["clarifications"]=[dict(id="question",unit_ids=["u3"],anchors=[dict(page_id="page-1",box=None)],question="Какая граница?",options=[],impact="score",reason="Балл зависит от чтения.",status="open",response=None,actor_ref=None,ambiguity_group_id=None)]
        self.assertRejected(materialize_assessment,raw,baseline,source,code="full_m01_contract")

    def test_unsupported_is_unknown_not_zero(self):
        source,baseline,raw=synthetic_baseline()
        raw["score"].update(status="withheld",value=None,criterion_ids=[],requirement_ids=[],evidence_unit_ids=[],rationale="Неподдержанный способ требует проверки.")
        raw["issues"]=[dict(code="unsupported_math",message="Неподдержанный способ",retryable=False,blocks=["score"])]
        result=materialize_assessment(raw,baseline,source)
        self.assertIsNone(result["score"]["value"])

    def test_exact_facts_cannot_be_overwritten_or_forged(self):
        source,baseline,raw=synthetic_baseline()
        baseline["facts"]=[deepcopy(raw["facts"][0])]
        baseline["facts"][0]["basis_sha256"]=contract.reading_digest(baseline)
        self.assertRejected(materialize_assessment,raw,baseline,source,code="duplicate_or_existing_id")

    def test_model_cannot_answer_for_human(self):
        source,baseline,raw=synthetic_baseline()
        raw["clarifications"]=[dict(id="fake",unit_ids=["u1"],anchors=[dict(page_id="page-1",box=None)],question="?",options=[],impact="score",reason="?",status="answered",response="yes",actor_ref="teacher",ambiguity_group_id=None)]
        self.assertRejected(materialize_assessment,raw,baseline,source,code="response_schema")

    def test_core_and_fallback_survive_wrong_feature(self):
        core={"k05.core.provenance","k05.core.constraints","k05.core.completeness","k05.core.consistency","k05.core.general_method"}
        for typ in ("trigonometric_equation","inequality"):
            modules=selected_modules(typ,["unsupported_method"])
            self.assertTrue(core <= set(modules["containers"]))
            self.assertEqual(modules["executable_requests"],[])

    def test_feature_module_and_case_contrast_selection(self):
        modules=selected_modules("inequality",["circle","substitution","division"])
        self.assertTrue({"k05.circle","k05.subst","k05.div"} <= set(modules["containers"]))
        self.assertEqual({c["case_id"] for c in candidate_cases("inequality",["15.2.3"])},{"15.2.3","15.2.4"})
        self.assertRejected(candidate_cases,"inequality",["invented"])

    def test_reading_key_and_evaluator_fields_rejected(self):
        _,baseline,_=synthetic_baseline()
        request=dict(task=read_json(ROOT/'contracts/ege_v1/examples/principles_states.json')["examples"][0]["input"]["task"],context=compact_context(baseline),page={"id":"page-1"})
        self.assertRejected(build_request,"read",request,reference_key={})
        request["expected_score"]=2
        self.assertRejected(build_request,"read",request,code="request_fields")

    def test_l04_no_private_case_labels_or_evaluation_metadata(self):
        for identity in CASES:
            built=build_l04_case(identity)
            for stage in ("reading","assessment"):
                text=canonical(built[stage]["payload"])
                for forbidden in ("R01","R13","R11","W020","E212","expected_facts","reference_score","must_not_do","first_material_error","source_case"):
                    self.assertNotIn(forbidden,text)
            self.assertTrue(all(not v for v in built["reading"]["payload"]["data"]["context"].values()))
            self.assertNotIn("reference_key",built["reading"]["payload"]["data"])

    def test_l04_r13_literal_and_confirmed_meaning_remain_separate(self):
        context=build_l04_case("L03-13-02")["assessment"]["payload"]["data"]["context"]
        unit=next(u for u in context["units"] if u["id"]=="L04")
        self.assertIn(r"\sin x^{2}",unit["literal"])
        meaning=next(i for i in context["interpretations"] if i["status"]=="human_confirmed")
        self.assertEqual(meaning["meaning"],r"(\sin x)^2")
        self.assertEqual(meaning["source"]["kind"],"human_review")

    def test_l04_baselines_are_valid_and_coverage_honest(self):
        for identity in CASES:
            source,result=l04_input_and_baseline(identity)
            self.assertEqual(contract.validate(result,source),[])
            expected="unresolved" if identity=="L03-15-02" else "accounted"
            self.assertEqual({p["score_scope"] for p in result["coverage"]},{expected})

    def test_l04_e212_retains_two_pages_and_independent_chains(self):
        built=build_l04_case("L03-15-02")
        self.assertEqual(len(built["reading"]["payload"]["data"]["solution_pages"]),2)
        self.assertEqual(len(built["assets"]),3)
        self.assertEqual(built["reading"]["payload"]["response_schema"]["properties"]["pages"]["minItems"],2)
        self.assertIn("хронология",canonical(built["assessment"]["payload"]["data"]["context"]))

    def test_l04_same_bytes_and_hashes_for_g_and_d(self):
        with tempfile.TemporaryDirectory() as directory:
            report=freeze_l04(directory)
            self.assertEqual(len(report["requests"]),8)
            self.assertEqual(report["base_model_calls_if_authorized"],16)
            for row in report["requests"]:
                raw=Path(row["file"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),row["file_sha256"])
                self.assertEqual(digest(json.loads(raw)),row["payload_sha256"])
                self.assertEqual(row["configurations"],["G","D"])

    def test_projection_drops_evaluator_only_fields(self):
        raw=dict(lines=[dict(id="u1",page=1,kind="literal",text="x=1",source="R01 private",impact="first_material_error",reference_score=0)],interpretations=[])
        text=canonical(project_transcript(raw,page_ids=["solution-1"]))
        for bad in ("R01","first_material_error","reference_score"):
            self.assertNotIn(bad,text)

    def test_m04_runs_both_types_with_same_version_and_valid_results(self):
        for task_type in ("trigonometric_equation","inequality"):
            example=raw_synthetic(task_type);form=fixture_form(task_type)
            image=base64.b64decode(form["solution"][0].split(",",1)[1])
            adapter=PreparedFixtureAdapter(statement=form["task_text"],image_hashes={"solution-1":hashlib.sha256(image).hexdigest()},readings={"solution-1":example["reading"]},assessment=example["assessment"])
            with tempfile.TemporaryDirectory() as directory:
                work_store=WorkStore(directory);work=work_store.save(form)
                with self.subTest(task_type=task_type):
                    pipeline=Pipeline(work_store,adapter,criteria=[fixture_criterion(task_type)],allow_synthetic=True)
                    try:
                        run=pipeline.create(work["id"],background=False,synthetic=True);run=pipeline.execute(run["id"])
                        self.assertEqual(run["state"],"completed",run["error_code"])
                        self.assertEqual(run["result"]["score"]["value"],2)
                        self.assertEqual(contract.validate(run["result"],run["input"]),[])
                        self.assertEqual(len(adapter.requests),2)
                    finally:pipeline.close()

    def test_fixture_is_synthetic_only_and_detects_mutation(self):
        example=raw_synthetic();adapter=PreparedFixtureAdapter(statement="s",image_hashes={},readings={},assessment=example["assessment"])
        request={"synthetic":False,"task":{"statement":{"text":"s"}}}
        self.assertRejected(adapter.assess,request,code="synthetic_fixture_only")
        adapter._assessment["score"]["rationale"]="mutated"
        request["synthetic"]=True
        self.assertRejected(adapter.assess,request,code="fixture_drift")

    def test_result_ids_bind_work_and_prior_revision(self):
        source,baseline,raw=synthetic_baseline()
        first=materialize_assessment(raw,baseline,source)
        other_source=deepcopy(source);other_source["id"]="other-work"
        other_baseline=deepcopy(baseline)
        other_baseline["input_ref"]={"id":other_source["id"],"revision":other_source["revision"],"sha256":contract.digest(other_source)}
        second=materialize_assessment(raw,other_baseline,other_source)
        self.assertNotEqual(first["id"],second["id"])
        other_baseline["id"]="new-prior-version"
        third=materialize_assessment(raw,other_baseline,other_source)
        self.assertNotEqual(second["id"],third["id"])

    def test_read_materialization_preserves_child_branch_order(self):
        source,baseline,_=synthetic_baseline()
        baseline["units"]=[];baseline["branches"]=[dict(id="child",parent_id="main",label="Случай",unit_ids=[],assumption_ids=[],status="active"),dict(id="main",parent_id=None,label="Вся попытка",unit_ids=[],assumption_ids=[],status="active")]
        response=deepcopy(raw_synthetic()["reading"])
        response["coverage"]["page_id"]="page-1"
        for unit in response["units"]:unit["anchors"][0]["page_id"]="page-1"
        result=materialize_reading(response,baseline,source,"page-1")
        self.assertEqual(result["branches"][0]["unit_ids"],[])
        self.assertEqual(len(result["branches"][1]["unit_ids"]),3)

    def test_fixture_settings_are_a_copy(self):
        adapter=PreparedFixtureAdapter(statement="s",image_hashes={},readings={},assessment={})
        settings=adapter.settings;settings["fixture_sha256"]="a"*64
        self.assertNotEqual(adapter.settings["fixture_sha256"],settings["fixture_sha256"])

    def test_whole_attempt_atomic_forward_links_and_page_guards(self):
        source,_,_=synthetic_baseline()
        second=deepcopy(source["pages"][0]);second.update(id="page-2",asset_id="image-2",order=2)
        source["pages"].append(second)
        baseline=base_result(source,dict(id="whole-attempt",configuration={}))
        responses=[]
        for n in (1,2):
            reading=deepcopy(raw_synthetic()["reading"])
            reading["coverage"]["page_id"]=f"page-{n}"
            for unit in reading["units"]:
                unit["id"]=f"p{n}-"+unit["id"]
                unit["anchors"][0]["page_id"]=f"page-{n}"
            responses.append(reading)
        responses[0]["relations"]=[dict(id="cross-page",from_unit_id=responses[0]["units"][-1]["id"],to_unit_id=responses[1]["units"][0]["id"],kind="continues")]
        result=materialize_reading_attempt(dict(pages=responses),baseline,source)
        self.assertEqual(len(result["units"]),6)
        self.assertEqual(contract.validate(result,source),[])
        self.assertRejected(materialize_reading_attempt,dict(pages=responses[:1]),baseline,source,code="reading_attempt_pages")
        self.assertRejected(materialize_reading_attempt,dict(pages=list(reversed(responses))),baseline,source,code="coverage_page_mismatch")
        repeated=deepcopy(responses);repeated[1]["units"][0]["id"]=repeated[0]["units"][0]["id"]
        self.assertRejected(materialize_reading_attempt,dict(pages=repeated),baseline,source,code="duplicate_or_existing_id")


if __name__ == "__main__":
    unittest.main()
