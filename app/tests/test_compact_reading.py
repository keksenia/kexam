"""Content-preserving READ boundary tests; no model or mathematical oracle."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from app.compact_reading import build_reading, materialize_compact_reading, project_reading, schema
from app.m05.runtime import freeze_identity
from app.pipeline import base_result, input_snapshot
from app.server import WorkStore
from app.tests.test_foundation import valid_form, image_data
from contracts.ege_v1 import validate as contract


def entry(text="x = πn", **changes):
    return dict(kind="math",text=text,reading="clear",activity="active",author="student",**changes)


def page(number=1, **changes):
    value=dict(page=number,entries=[entry()],coverage=dict(status="complete",
        reason="Synthetic: every visible item was supplied.",score_scope="accounted",
        score_scope_reason="Synthetic explicit scope, not real OCR."))
    value.update(changes)
    return value


class CompactReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=WorkStore(Path(self.tmp.name)/"works")
        form=valid_form();form["solution"]=[image_data(),image_data()]
        work=self.store.save(form)
        self.source,_,_=input_snapshot(self.store,work["id"])
        self.source["task"]["statement"]["completeness"]="confirmed"
        self.source["task"]["statement"]["missing"]=[]
        self.baseline=base_result(self.source,dict(id="synthetic-compact",configuration={}))
        self.reading=dict(pages=[page(1),page(2)])

    def materialize(self, value=None):
        result=materialize_compact_reading(value or self.reading,self.baseline,self.source)
        self.assertEqual(contract.validate(result,self.source),[])
        return result

    def test_two_pages_order_and_app_owned_anchors(self):
        before=deepcopy(self.reading);result=self.materialize()
        self.assertEqual(self.reading,before)
        self.assertEqual([u["id"] for u in result["units"]],["solution-1-u1","solution-2-u1"])
        self.assertEqual(result["units"][1]["anchors"],[dict(page_id="solution-2",box=None)])
        self.assertEqual(result["facts"],[]);self.assertIsNone(result["score"]["value"])

    def test_diagram_description_is_retained_without_declaring_math_truth(self):
        self.reading["pages"][1]["entries"]=[dict(kind="diagram",text="Точка с подписью −5π; дуга зачёркнута.",
            reading="ambiguous",activity="uncertain",author="unknown")]
        result=self.materialize();u=result["units"][1];i=result["interpretations"][0]
        self.assertIsNone(u["literal"]);self.assertEqual(u["reading_status"],"ambiguous")
        self.assertEqual(i["meaning"],self.reading["pages"][1]["entries"][0]["text"])
        self.assertEqual(i["status"],"proposed");self.assertEqual(i["unit_ids"],[u["id"]])
        self.assertFalse(result["facts"])

    def test_wrong_math_and_crossed_out_writing_are_not_repaired(self):
        self.reading["pages"][0]["entries"][0].update(text="2 + 2 = 5",activity="crossed_out")
        result=self.materialize();self.assertEqual(result["units"][0]["literal"],"2 + 2 = 5")
        self.assertEqual(result["units"][0]["activity"],"crossed_out")

    def test_global_and_branch_premises_and_forward_page_links(self):
        self.reading.update(branches=[dict(label="sin x = 0",parent=0,entries=["2:1"],status="active")],
            assumptions=[dict(claim="x ∈ R",origin="task_statement",entry=None,branch=0,status="established"),
                         dict(claim="sin x = 0",origin="student_unit",entry="2:1",branch=1,status="conditional")],
            interpretations=[dict(entries=["2:1"],meaning="Вторая ветвь продолжает запись на первой странице.",
                                  branch=1,assumptions=[1,2],context=["1:1"])],
            links=[{"from":"1:1","to":"2:1","kind":"continues"}])
        result=self.materialize()
        self.assertEqual(result["assumptions"][0]["source_ref"],self.source["id"])
        self.assertEqual(result["branches"][0]["assumption_ids"],["read-a1"])
        self.assertEqual(result["branches"][1]["assumption_ids"],["read-a2"])
        self.assertEqual(result["relations"][0]["to_unit_id"],"solution-2-u1")

    def test_all_plausible_readings_stay_proposed(self):
        self.reading["pages"][0]["entries"][0].update(text=None,reading="ambiguous")
        self.reading["pages"][0]["coverage"].update(status="partial",score_scope="unresolved")
        self.reading["interpretations"]=[dict(entries=["1:1"],meaning=t) for t in ("x > 1","x ≥ 1")]
        result=self.materialize()
        self.assertEqual([i["meaning"] for i in result["interpretations"]],["x > 1","x ≥ 1"])
        self.assertTrue(all(i["status"]=="proposed" for i in result["interpretations"]))
        self.assertEqual(result["coverage"][0]["score_scope"],"unresolved")

    def test_absent_completeness_is_never_filled_in(self):
        for key in ("status","reason","score_scope","score_scope_reason"):
            value=deepcopy(self.reading);del value["pages"][1]["coverage"][key]
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,"reading_coverage_missing"):
                self.materialize(value)

    def test_missing_duplicate_and_reordered_pages_rejected(self):
        for pages in ([page(1)],[page(1),page(1)],[page(2),page(1)],[page(1),page(3)]):
            with self.subTest(pages=[p["page"] for p in pages]),self.assertRaises(ValueError):
                self.materialize(dict(pages=pages))

    def test_nonexistent_entry_branch_or_premise_rejected(self):
        for interpretation in (dict(entries=["2:9"],meaning="Missing"),
                               dict(entries=["1:1"],meaning="Missing",branch=8),
                               dict(entries=["1:1"],meaning="Missing",assumptions=[1])):
            value=deepcopy(self.reading);value["interpretations"]=[interpretation]
            with self.subTest(interpretation=interpretation),self.assertRaises(ValueError):self.materialize(value)

    def test_cyclic_branches_and_self_links_rejected(self):
        for change in (dict(branches=[dict(label="Cycle",parent=1,entries=["1:1"],status="active")]),
                       dict(links=[{"from":"1:1","to":"1:1","kind":"supports"}])):
            with self.subTest(change=change),self.assertRaises(ValueError):self.materialize({**self.reading,**change})

    def test_hypothesis_cannot_be_established_and_teacher_not_student_premise(self):
        for origin,status in (("hypothesis","established"),("student_unit","established")):
            value=deepcopy(self.reading);value["pages"][0]["entries"][0]["author"]="teacher"
            value["assumptions"]=[dict(claim="x > 0",origin=origin,status=status,branch=0,
                                        entry="1:1" if origin=="student_unit" else None)]
            with self.subTest(origin=origin),self.assertRaises(ValueError):self.materialize(value)

    def test_incompatible_branch_premise_is_not_silently_reassigned(self):
        value=deepcopy(self.reading)
        value["branches"]=[dict(label=str(n),parent=0,entries=[f"{n}:1"],status="active") for n in (1,2)]
        value["assumptions"]=[dict(claim="x > 0",origin="student_unit",entry="1:1",branch=1,status="conditional")]
        value["interpretations"]=[dict(entries=["2:1"],meaning="Conflict",branch=2,assumptions=[1])]
        with self.assertRaisesRegex(ValueError,"full_m01_contract"):self.materialize(value)

    def test_unread_page_cannot_claim_accounted_scope(self):
        self.reading["pages"][1]["coverage"]["status"]="unreadable"
        with self.assertRaisesRegex(ValueError,"full_m01_contract"):self.materialize()

    def test_clear_empty_diagram_is_rejected_by_advertised_schema(self):
        for text in (None,""):
            value=deepcopy(self.reading);value["pages"][0]["entries"][0].update(kind="diagram",text=text)
            self.assertTrue(list(Draft202012Validator(schema(2)).iter_errors(value)))

    def test_compact_prompt_contains_no_score_key_ids_or_coordinates(self):
        before=freeze_identity();payload=build_reading(self.source,[])
        self.assertEqual(payload["data"]["context"],{})
        self.assertNotIn("reference_key",payload["data"]);self.assertNotIn("criteria",payload["data"])
        entry_props=payload["response_schema"]["properties"]["pages"]["items"]["properties"]["entries"]["items"]["properties"]
        self.assertNotIn("id",entry_props);self.assertNotIn("anchors",entry_props)
        self.assertEqual(freeze_identity(),before)

    def test_unsupported_fields_and_forged_authority_rejected(self):
        for change in (dict(score=2),dict(facts=[]),dict(pages=[{**page(1),"source":"human"},page(2)])):
            with self.subTest(change=list(change)),self.assertRaisesRegex(ValueError,"compact_reading_schema"):
                self.materialize({**self.reading,**change})

    def test_projection_never_adds_relations_or_interpretations_to_plain_text(self):
        projected=project_reading(self.reading,self.source)
        self.assertTrue(all(not p["relations"] and not p["interpretations"] for p in projected["pages"]))
        self.assertFalse(self.baseline["units"])
