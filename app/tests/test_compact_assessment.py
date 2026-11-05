"""Adversarial semantic boundaries for the compact assessment, without network."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from app.compact_assessment import catalogue, build_assessment, materialize_compact_assessment
from app.compact_reading import materialize_compact_reading
from app.m05.runtime import rubric, freeze_identity
from app.pipeline import base_result, input_snapshot
from app.server import WorkStore
from app.tests.test_foundation import valid_form, image_data
from app.tests.test_compact_reading import page
from contracts.ege_v1 import validate as contract


def assessment_fixture(task_type="trigonometric_equation", refs=None):
    refs=refs or ["1:1"]
    requirements=[]
    for r in catalogue(task_type):
        key=r["id"]
        exception=key=="CALC-ONLY" or key.endswith("exception")
        requirements.append(dict(key=key,status="not_fulfilled" if exception else "fulfilled",entries=refs,
            facts=[1],reason="Synthetic explicit analysis of this requirement."))
    return dict(facts=[dict(claim="Synthetic supported student result and justification.",entries=refs,
        status="supported",reason="Synthetic evidence, not an actual mathematical assessment.",kind="completeness",domain="R")],
        requirements=requirements,uncertainties=[],score=dict(value=2,
        criterion="M02-FIPI13-2" if task_type=="trigonometric_equation" else "m03-score-2",
        requirements=[r["key"] for r in requirements if r["status"]=="fulfilled"],rationale="Synthetic complete solution."))


class CompactAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=WorkStore(Path(self.tmp.name)/"works")
        self.setup_work()

    def setup_work(self, task_type="trigonometric_equation"):
        form=valid_form();form["task_type"]=task_type;form["solution"]=[image_data()]
        work=self.store.save(form);self.source,_,_=input_snapshot(self.store,work["id"])
        self.source["task"]["statement"].update(completeness="confirmed",missing=[])
        self.source["capture"]["pages_complete"]="confirmed"
        self.reading=dict(pages=[page()])
        self.response=assessment_fixture(task_type)
        self.rebuild()

    def rebuild(self):
        base=base_result(self.source,dict(id="compact-assessment-test",configuration={}))
        self.baseline=materialize_compact_reading(self.reading,base,self.source)
        self.baseline["criteria"]=rubric(self.source["task"]["task_type"])

    def materialize(self):
        result=materialize_compact_assessment(self.response,self.baseline,self.source)
        self.assertEqual(contract.validate(result,self.source),[])
        return result

    def test_both_types_full_score_with_code_owned_links(self):
        before=freeze_identity()
        for kind in ("trigonometric_equation","inequality"):
            self.setup_work(kind);original=deepcopy(self.response)
            result=self.materialize()
            self.assertEqual(result["score"]["value"],2)
            self.assertTrue(all(r["criterion_id"]==result["score"]["criterion_ids"][0] for r in result["requirements"]))
            self.assertTrue(all(f["method"]=="model" for f in result["facts"]))
            self.assertEqual(self.response,original)
        self.assertEqual(freeze_identity(),before)

    def test_fulfilled_requirement_needs_student_records_and_facts(self):
        for key in ("entries","facts"):
            self.response=assessment_fixture();self.response["requirements"][0][key]=[]
            with self.subTest(key=key),self.assertRaises(ValueError):self.materialize()

    def test_forged_code_authority_and_unknown_fields_rejected(self):
        self.response["facts"][0]["method"]="exact_code"
        with self.assertRaisesRegex(ValueError,"compact_assessment_schema"):self.materialize()

    def test_score_must_match_criterion_and_all_required_parts(self):
        self.response["score"]["criterion"]="M02-FIPI13-0"
        with self.assertRaisesRegex(ValueError,"assessment_score_criterion"):self.materialize()
        self.response=assessment_fixture();self.response["score"]["requirements"].remove("B-JUSTIFICATION")
        with self.assertRaisesRegex(ValueError,"assessment_score_basis"):self.materialize()

    def test_missing_duplicate_and_nonexistent_requirement_rejected(self):
        self.response["requirements"][1]=deepcopy(self.response["requirements"][0])
        with self.assertRaisesRegex(ValueError,"assessment_requirement_coverage"):self.materialize()

    def test_nonexistent_entry_fact_and_self_dependency_rejected(self):
        for change in (dict(entries=["2:8"]),dict(depends_on=[9]),dict(depends_on=[1])):
            self.response=assessment_fixture();self.response["facts"][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.materialize()

    def test_active_teacher_or_crossed_out_work_cannot_fulfill_requirement(self):
        for change in (dict(author="teacher"),dict(activity="crossed_out"),dict(author="unknown")):
            self.reading["pages"][0]["entries"][0].update(change);self.rebuild()
            with self.subTest(change=change),self.assertRaises(ValueError):self.materialize()

    def test_ambiguity_requires_explicit_impact_explanation(self):
        self.reading["pages"][0]["entries"][0]["reading"]="ambiguous";self.rebuild()
        with self.assertRaisesRegex(ValueError,"full_m01_contract"):self.materialize()
        self.response["uncertainties"]=[dict(entries=["1:1"],effect="none",reason="Only a crossed margin label is unclear; the score has independent evidence.")]
        self.assertEqual(self.materialize()["score"]["value"],2)

    def test_feedback_question_does_not_block_independent_score(self):
        self.response["uncertainties"]=[dict(entries=["1:1"],effect="feedback",reason="Only optional explanation is uncertain.",question="Уточните подпись для дополнительного комментария.")]
        result=self.materialize();self.assertEqual(result["score"]["value"],2)
        self.assertEqual(result["readiness"]["feedback"],"pending")

    def test_score_affecting_question_cannot_be_hidden_by_proposed_score(self):
        self.response["uncertainties"]=[dict(entries=["1:1"],effect="score",reason="The sign changes the score.",question="Какой знак в строке 1?")]
        with self.assertRaises(ValueError):self.materialize()
        self.response["score"].update(value=None,criterion=None)
        result=self.materialize();self.assertIsNone(result["score"]["value"])
        self.assertEqual(result["clarifications"][0]["anchors"],[dict(page_id="solution-1",box=None)])

    def test_missing_question_or_invented_case_is_rejected(self):
        self.response["uncertainties"]=[dict(entries=["1:1"],effect="score",reason="Uncertain.")]
        with self.assertRaisesRegex(ValueError,"compact_assessment_schema"):self.materialize()
        self.response=assessment_fixture();self.response["cases"]=[dict(case="invented",requirements=["A-ANSWER"],facts=[1],status="applicable",matches=["same"],exclusions=[],reason="Same.")]
        with self.assertRaisesRegex(ValueError,"assessment_unknown_case"):self.materialize()

    def test_unknown_fact_or_conditional_hypothesis_cannot_prove_full_score(self):
        self.response["facts"][0]["status"]="unknown"
        with self.assertRaises(ValueError):self.materialize()
        self.response=assessment_fixture()
        self.baseline["assumptions"].append(dict(id="hypothesis",claim="x>0",origin="hypothesis",source_ref="test",branch_id="main",status="conditional"))
        self.baseline["branches"][0]["assumption_ids"].append("hypothesis")
        self.response["facts"][0]["assumptions"]=["hypothesis"]
        with self.assertRaises(ValueError):self.materialize()

    def test_zero_requires_excluding_partial_credit_and_supported_absence(self):
        self.response["score"].update(value=0,criterion="M02-FIPI13-0",requirements=["A-ANSWER","CALC-ONLY"])
        with self.assertRaisesRegex(ValueError,"assessment_score_basis"):self.materialize()
        self.response["requirements"][0].update(status="not_fulfilled",facts=[],entries=[],inspected_pages=[1])
        result=self.materialize();self.assertEqual(result["score"]["value"],0)
        self.reading["pages"][0]["coverage"].update(status="partial",score_scope="unresolved");self.rebuild()
        with self.assertRaises(ValueError):self.materialize()

    def test_partial_score_requires_exact_applicable_branch(self):
        for kind,criterion,key in (("trigonometric_equation","M02-FIPI13-1","CALC-ONLY"),
            ("inequality","m03-score-1-boundary","m03-boundary-exception"),
            ("inequality","m03-score-1-calculation","m03-calculation-exception")):
            self.setup_work(kind)
            self.response["score"].update(value=1,criterion=criterion,requirements=[key])
            with self.assertRaises(ValueError):self.materialize()
            next(r for r in self.response["requirements"] if r["key"]==key)["status"]="fulfilled"
            if kind=="trigonometric_equation": self.response["requirements"][0]["status"]="not_fulfilled"
            else: next(r for r in self.response["requirements"] if r["key"]=="m03-answer")["status"]="not_fulfilled"
            self.assertEqual(self.materialize()["score"]["value"],1)

    def test_required_inequality_elements_cannot_be_declared_inapplicable(self):
        self.setup_work("inequality")
        self.response["requirements"][0]["status"]="not_applicable"
        with self.assertRaisesRegex(ValueError,"assessment_required_element_skipped"):self.materialize()

    def test_one_point_from_part_a_does_not_require_the_unused_calculation_branch(self):
        self.response['facts'].append(dict(claim='The selected roots do not satisfy the required interval.',
            entries=['1:1'],status='contradicted',reason='Explicitly checked interval error.',kind='membership',domain='R'))
        for r in self.response['requirements']:
            if r['key'].startswith('B-'):r.update(status='not_fulfilled',facts=[2])
            if r['key']=='CALC-ONLY':r.update(status='not_applicable',entries=[],facts=[],reason='The point is justified independently by part a.')
        self.response['score'].update(value=1,criterion='M02-FIPI13-1',requirements=['A-ANSWER','A-JUSTIFICATION','B-ANSWER','B-JUSTIFICATION'])
        before=deepcopy(self.response)
        result=self.materialize()
        self.assertEqual(result['score']['value'],1)
        self.assertEqual(self.response,before)
        self.assertNotIn('assess-r-CALC-ONLY',[r['id'] for r in result['requirements']])
        next(r for r in self.response['requirements'] if r['key']=='B-ANSWER')['status']='unknown'
        with self.assertRaisesRegex(ValueError,'assessment_score_basis'):self.materialize()

    def test_full_score_does_not_require_unused_special_one_point_branches(self):
        for kind in ('trigonometric_equation','inequality'):
            self.setup_work(kind)
            for r in self.response['requirements']:
                if r['key']=='CALC-ONLY' or r['key'].endswith('exception'):
                    r.update(status='not_applicable',entries=[],facts=[],reason='Both-point criterion independently fulfilled.')
            with self.subTest(kind=kind):self.assertEqual(self.materialize()['score']['value'],2)

    def test_inapplicable_branch_cannot_prove_partial_credit_or_exclude_it_for_zero(self):
        for kind,criterion,key in (('trigonometric_equation','M02-FIPI13-1','CALC-ONLY'),
            ('inequality','m03-score-1-boundary','m03-boundary-exception'),
            ('inequality','m03-score-1-calculation','m03-calculation-exception')):
            self.setup_work(kind)
            next(r for r in self.response['requirements'] if r['key']==key)['status']='not_applicable'
            self.response['score'].update(value=1,criterion=criterion,requirements=[key])
            with self.subTest(criterion=criterion),self.assertRaisesRegex(ValueError,'assessment_inapplicable_evidence'):self.materialize()
            for r in self.response['requirements']:
                if r['key']!=key:r['status']='not_fulfilled'
            main='A-ANSWER' if kind=='trigonometric_equation' else 'm03-answer'
            alternatives=['CALC-ONLY'] if kind=='trigonometric_equation' else ['m03-boundary-exception','m03-calculation-exception']
            self.response['score'].update(value=0,criterion='M02-FIPI13-0' if kind=='trigonometric_equation' else 'm03-score-0',
                requirements=[main]+[k for k in alternatives if k!=key])
            with self.subTest(zero=key),self.assertRaisesRegex(ValueError,'assessment_score_basis'):self.materialize()

    def test_selected_special_inequality_branch_has_independent_evidence(self):
        for criterion,key,unused in (('m03-score-1-boundary','m03-boundary-exception','m03-calculation-exception'),
            ('m03-score-1-calculation','m03-calculation-exception','m03-boundary-exception')):
            self.setup_work('inequality')
            next(r for r in self.response['requirements'] if r['key']==key)['status']='fulfilled'
            next(r for r in self.response['requirements'] if r['key']==unused)['status']='not_applicable'
            next(r for r in self.response['requirements'] if r['key']=='m03-answer')['status']='not_fulfilled'
            self.response['score'].update(value=1,criterion=criterion,requirements=[key])
            with self.subTest(criterion=criterion):self.assertEqual(self.materialize()['score']['value'],1)
            next(r for r in self.response['requirements'] if r['key']==key)['facts']=[]
            with self.assertRaises(ValueError):self.materialize()

    def test_core_trig_requirement_cannot_be_skipped_even_with_valid_part_a(self):
        next(r for r in self.response['requirements'] if r['key']=='B-ANSWER')['status']='not_applicable'
        self.response['score'].update(value=1,criterion='M02-FIPI13-1',requirements=['A-ANSWER','A-JUSTIFICATION'])
        with self.assertRaisesRegex(ValueError,'assessment_required_element_skipped'):self.materialize()

    def test_explicit_mathematical_case_join_preserves_coverage_dependencies(self):
        from app.tests.test_compact_reading import entry
        self.reading["pages"][0]["entries"]=[entry("Два случая"),entry("x=0"),entry("x=pi")]
        self.reading["branches"]=[dict(label="first",parent=0,entries=["1:2"],status="active"),dict(label="second",parent=0,entries=["1:3"],status="active")]
        self.reading["assumptions"]=[dict(claim="first condition",origin="student_unit",entry="1:2",branch=1,status="conditional"),dict(claim="second condition",origin="student_unit",entry="1:3",branch=2,status="conditional")]
        self.rebuild()
        template=deepcopy(self.response["facts"][0])
        self.response["facts"]=[{**template,"claim":"Both cases cover all solutions."},
            {**template,"entries":["1:2"],"branch":"read-b1","assumptions":["read-a1"]},
            {**template,"entries":["1:3"],"branch":"read-b2","assumptions":["read-a2"]},
            {**template,"entries":["1:1","1:2","1:3"],"depends_on":[2,3],"join":dict(branches=["read-b1","read-b2"],coverage=1)}]
        for r in self.response["requirements"]:r["facts"]=[4]
        result=self.materialize()
        self.assertEqual(result["score"]["value"],2)
        self.assertEqual(result["facts"][-1]["branch_join"]["coverage_fact_id"],"assess-f1")
        self.response["facts"][-1].pop("join")
        with self.assertRaisesRegex(ValueError,"full_m01_contract"):self.materialize()

    def test_prompt_explicitly_lists_uncertainty_and_keeps_subject_safeguards(self):
        self.reading["pages"][0]["entries"][0]["reading"]="ambiguous";self.rebuild()
        payload=build_assessment(self.source,self.baseline,[],dict(answer="{0,pi}"))
        self.assertEqual(payload["data"]["uncertain_entries"],["1:1"])
        self.assertNotIn('"basis_sha256"',str(payload["response_schema"]))
        self.assertIn("иной грамотный способ",payload["system"])
        self.assertTrue(payload["data"]["methodology"])
