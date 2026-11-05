"""Fresh synthetic uploads and exact solver, no model/network calls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.call_guard import CallGuard, Policy, ProviderResponse, canonical
from app.pipeline import input_snapshot
from app.live_checking import FreshChecking, VERSION, PROFILES, ASSESSMENT_VERSION
from app.live_condition import solve_condition, validate_condition, VERSION as CONDITION_VERSION
from app.provider_requests import VERSION as BUILDER_VERSION
from app.m05.runtime import ROOT, rubric, freeze_identity
from app.compact_reading import VERSION as READING_VERSION
from app.compact_reading import schema as reading_schema
from app.strict_reading import response_format as reading_response_format
from app.tests.test_foundation import image_data, valid_form
from app.server import WorkStore
from contracts.ege_v1 import validate as contract


def condition(task_type="trigonometric_equation", **changes):
    c=dict(status="clear",task_type=task_type,literal="sin(x)=0; x ∈ [0; π]",lhs="sin(x)",rhs="0",relation="=",
        interval=dict(left="0",right="pi",left_open=False,right_open=False),restrictions=[],uncertainties=[])
    if task_type=="inequality": c.update(literal="x-1>0",lhs="x-1",relation=">",interval=None)
    c.update(changes); return c


def reading(c, source="text"):
    """condition-reading-1.2.0 model response for an old-style solver condition."""
    if "blocks" in c: return deepcopy(c)
    if c["task_type"]=="trigonometric_equation":
        interval=c["interval"]
        blocks=[dict(id="a",kind="equation",literal="а) Решите уравнение "+c["lhs"]+" = "+c["rhs"]+".",source=source,
                     lhs=c["lhs"],rhs=c["rhs"],relation=c["relation"]),
                dict(id="b",kind="select_roots",literal="б) Найдите все корни этого уравнения, принадлежащие отрезку ["+interval["left"]+"; "+interval["right"]+"].",
                     source=source,interval=deepcopy(interval))]
    else:
        blocks=[dict(id="a",kind="inequality",literal="Решите неравенство "+c["lhs"]+" "+c["relation"]+" "+c["rhs"]+".",source=source,
                     lhs=c["lhs"],rhs=c["rhs"],relation=c["relation"])]
    return dict(status=c["status"],template_id="ege2026-13" if c["task_type"]=="trigonometric_equation" else "ege2026-15",
        blocks=blocks,restrictions=list(c["restrictions"]),
        extra_text=[dict(literal=r,source=source) for r in c["restrictions"]],uncertainties=list(c["uncertainties"]))


def fixture_policy(model="G"):
    profiles=json.loads((ROOT/"planning/l04/configurations.json").read_text())["profiles"]
    profiles={p["id"]:p for p in profiles}
    rules={}
    for stage in ("statement_ocr","solution_ocr","grading"):
        p=profiles[PROFILES[model][1 if stage=="grading" else 0]]
        versions=dict(contract="2.0.0",pipeline=VERSION,prompt=CONDITION_VERSION,code=VERSION,validator=CONDITION_VERSION) if stage=="statement_ocr" else dict(contract="2.0.0",pipeline="m04-offline-3",prompt="m05-prompts-1.1.0",code=BUILDER_VERSION,validator="m05-result-boundary-1")
        if stage=="solution_ocr": versions.update(pipeline=VERSION,prompt=READING_VERSION,validator=READING_VERSION)
        if stage=="grading": versions.update(pipeline=VERSION,prompt=ASSESSMENT_VERSION,validator=ASSESSMENT_VERSION)
        settings={k:v for k,v in p["requested_parameters"].items() if k!="max_tokens"}; settings["stream"]=False
        if stage=="grading" and model=="G": settings["reasoning_effort"]="medium"
        rules[stage]=dict(stage=stage,provider="hubris",model=p["model"],versions=versions,settings=settings,
            max_request_bytes=8388608,max_output_tokens=12000,micro_rub=2_000_000,evidence_ref="SYNTHETIC-NO-MONEY")
        if stage=="solution_ocr" and model=="G":
            template=rules.pop(stage)
            for count in range(1,11):
                rule=deepcopy(template)
                rule["settings"]["response_format"]=reading_response_format(reading_schema(count))
                rules[f"{stage}-{count}"]=rule
    return Policy(enabled=True,mode="fixture",approval_ref="SYNTHETIC-FRESH-NO-MONEY",budget_micro_rub=200_000_000,
        max_attempts=2,max_total_calls=20,max_output_tokens=12000,max_response_bytes=524288,
        timeout_seconds=3,operation_timeout_seconds=30,expires_at=time.time()+300,ceiling_rules=rules)


class Fixture:
    kind="fixture"; single_attempt=True
    def __init__(self, c): self.condition=c; self.requests=[]; self.cost="10"; self.malformed=False; self.change_work=None
    def factory(self, request):
        self.stage=request.stage
        self.requests.append(request)
        return self
    def __call__(self,payload,*,timeout_seconds):
        data=json.loads(payload["messages"][1]["content"][0]["text"])
        if self.stage=="statement_ocr": value=reading(self.condition, source="text" if data['text'] else "photo:1")
        else:
            example=json.loads((ROOT/"planning/m05/examples"/("synthetic_13.json" if data["task"]["task_type"]=="trigonometric_equation" else "synthetic_15.json")).read_text())
            if self.stage=="solution_ocr":
                pages=[]
                for page in data["solution_pages"]:
                    v=deepcopy(example["reading"])
                    coverage={k:x for k,x in v["coverage"].items() if k!="page_id"}
                    entries=[dict(kind=u["kind"],text=u["literal"],reading=u["reading_status"],
                                  activity=u["activity"],author=u["author"]) for u in v["units"]]
                    pages.append(dict(page=page["page"],coverage=coverage,entries=entries))
                value=dict(pages=pages)
            else:
                from app.tests.test_assessment_core import core_fixture
                value=core_fixture(data["task"]["task_type"],list(data["entry_refs"]))
        if self.change_work: self.change_work(); self.change_work=None
        if self.stage=="solution_ocr" and payload["response_format"]["type"]=="json_schema":
            # Test transport simulates an optional-field null sentinel; it never
            # supplies missing required semantic fields such as coverage.status.
            for key in ("interpretations","branches","assumptions","links","features"):
                value.setdefault(key,None)
        content="bad json" if self.malformed else canonical(value)
        return ProviderResponse(content,model=payload["model"],response_id="synthetic",finish_reason="stop",
            usage=dict(prompt_tokens=10,completion_tokens=50),cost_kopecks=self.cost,http_status=200)


class SolverTests(unittest.TestCase):
    def test_trig_exact_roots_and_open_endpoints(self):
        result=solve_condition(condition())
        self.assertEqual(result["status"],"solved"); self.assertEqual(result["answer"],"{0, pi}")
        c=condition(); c["interval"]["right_open"]=True
        self.assertEqual(solve_condition(c)["answer"],"{0}")
    def test_cancelled_original_denominator_is_preserved(self):
        r=solve_condition(condition("inequality",lhs="(x*x-1)/(x-1)",relation=">="))
        self.assertEqual(r["status"],"solved")
        self.assertIn("Interval.Ropen(-1, 1)",r["answer"]); self.assertEqual(r["original_denominators"],["x - 1"])
    def test_rational_irrational_and_isolated_points(self):
        r=solve_condition(condition("inequality",lhs="(x*x-2)**2",relation="<="))
        self.assertEqual(r["status"],"solved"); self.assertEqual(r["answer"],"{-sqrt(2), sqrt(2)}")
    def test_empty_is_a_proven_answer_not_a_solver_failure(self):
        r=solve_condition(condition(rhs="2")); self.assertEqual(r["status"],"solved"); self.assertTrue(r["empty"])
    def test_variable_radicals_unknown_functions_and_python_are_unsupported(self):
        for expr in ("sqrt(x)","log(x)","__import__('os').system('x')","x.real","x**1000"):
            with self.subTest(expr=expr): self.assertEqual(solve_condition(condition(lhs=expr))["status"],"unsupported")
    def test_invalid_interval_and_undefined_expression_are_suspicious(self):
        c=condition();c["interval"]["left"]="2*pi"
        self.assertEqual(solve_condition(c)["status"],"suspicious")
        self.assertEqual(solve_condition(condition("inequality",lhs="1/0"))["status"],"suspicious")
    def test_explicit_extra_restriction_is_not_silently_dropped(self):
        answer=solve_condition(condition(restrictions=["x>0"]))
        self.assertEqual(answer["status"],"solved")
        self.assertEqual(answer["answer"],"{pi}")
    def test_timeout_and_nonclear_reading_never_return_answer(self):
        self.assertEqual(solve_condition(condition(status="uncertain"))["status"],"needs_clarification")
        self.assertEqual(solve_condition(condition(),timeout=0.0001)["reason"],"solver_time_limit")
    def test_old_format_and_clear_with_uncertainty_are_rejected(self):
        with self.assertRaises(ValueError):validate_condition(condition(),"trigonometric_equation",sources={"text"})
        with self.assertRaises(ValueError):validate_condition(reading(condition(uncertainties=["sign"])),"trigonometric_equation",sources={"text"})


class FreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=WorkStore(self.root/"works");self.services=[]
        blocker=patch("socket.socket.connect",side_effect=AssertionError("Network forbidden"));blocker.start();self.addCleanup(blocker.stop)
    def tearDown(self):
        for service in self.services: service.close()
    def service(self,c=None,**kwargs):
        fixture=Fixture(c or condition())
        service=FreshChecking(self.store,policy=fixture_policy(),ledger=self.root/"ledger",transport_factory=fixture.factory,**kwargs)
        self.services.append(service);return service,fixture
    def run_work(self,service,task_type="trigonometric_equation",pages=1):
        form=valid_form(task_type);form['task_text']='';form["task"]=[image_data()];form["solution"]=[image_data() for _ in range(pages)]
        work=self.store.save(form); original=(self.store.root/work["id"]/"work.json").read_bytes()
        checked=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True),background=False)
        self.assertEqual((self.store.root/work["id"]/"work.json").read_bytes(),original)
        return work,checked["job"]

    def seed_settled_calls(self,service,fixture,count):
        work=self.store.save(valid_form())
        source,identity,_=input_snapshot(self.store,work["id"])
        request=service._condition_request(dict(work_id=work["id"],guard=identity,model="G"),source)
        guard=CallGuard(service.ledger,service.policy)
        for n in range(count):
            item=replace(request,context={**request.context,"synthetic_seed":n})
            result=guard.execute(item,fixture.factory(item),lambda v:v)
            self.assertEqual(result.status,"succeeded")
        fixture.requests.clear()

    def test_two_remaining_primary_calls_do_not_start_a_new_three_stage_run(self):
        service,fixture=self.service();self.seed_settled_calls(service,fixture,14)
        work=self.store.save(valid_form())
        result=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True),background=False)
        self.assertFalse(result["accepted"]);self.assertEqual(result["reason"],"insufficient_stage_calls")
        self.assertEqual(result["spending"]["primary_remaining"],2);self.assertEqual(fixture.requests,[])
        # A separately validated continuation requiring two stages is still possible.
        self.assertTrue(service.availability(required_calls=2)["paid_api_enabled"])

    def test_condition_recheck_cannot_spend_the_assessment_slot(self):
        service,fixture=self.service();self.seed_settled_calls(service,fixture,13)
        fixture.condition=condition(status="uncertain",uncertainties=["synthetic unreadable sign"])
        _,job=self.run_work(service)
        self.assertEqual(job["error_code"],"insufficient_stage_calls")
        self.assertEqual(len(fixture.requests),1);self.assertEqual(len(job["condition_versions"]),1)

    def test_amended_primary_allowance_preserves_four_technical_slots(self):
        fixture=Fixture(condition())
        service=FreshChecking(self.store,policy=replace(fixture_policy(),max_total_calls=33),
            ledger=self.root/"amended-ledger",transport_factory=fixture.factory)
        self.services.append(service)
        self.seed_settled_calls(service,fixture,14)
        self.assertEqual(service.availability()["spending"]["primary_remaining"],15)
        _,job=self.run_work(service)
        self.assertEqual(job["state"],"completed")
        self.seed_settled_calls(service,fixture,10)
        # 27 primary calls leave two; the four retry slots cannot cover grading.
        self.assertEqual(service.availability()["reason"],"insufficient_stage_calls")
        self.assertEqual(service.availability()["spending"]["primary_remaining"],2)

    def test_explicit_work_scope_does_not_authorize_another_work_or_model(self):
        service,fixture=self.service();work=self.store.save(valid_form())
        service.work_profiles={work["id"]:"G"}
        form=dict(work_id=work["id"],model="D",complete=True,single_task=True)
        self.assertEqual(service.create(form)["reason"],"check_scope_not_authorized")
        other=self.store.save(valid_form())
        self.assertEqual(service.create({**form,"work_id":other["id"],"model":"G"})["reason"],"check_scope_not_authorized")
        self.assertFalse(fixture.requests)

    def test_queue_rechecks_remaining_calls_under_lock_before_first_send(self):
        service,fixture=self.service();work=self.store.save(valid_form())
        with patch.object(service.pool,"submit"):
            job=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True))["job"]
        self.seed_settled_calls(service,fixture,14)
        service._run(job["id"])
        self.assertEqual(service.get(job["id"])["error_code"],"insufficient_stage_calls")
        self.assertEqual(fixture.requests,[])
    def test_fresh_both_types_full_m01_and_no_archive_import(self):
        for task_type in ("trigonometric_equation","inequality"):
            with self.subTest(task_type=task_type):
                # Separate fixtures share no real budget, production always shares one ledger.
                service,fixture=self.service(condition(task_type))
                if self.root.joinpath('ledger').exists(): service.ledger=self.root/task_type
                before=freeze_identity()
                work,job=self.run_work(service,task_type)
                self.assertEqual(job["state"],"completed",job)
                source=json.loads((service.directory/job["id"]/"verified-input.json").read_text())
                self.assertEqual(contract.validate(job["result"],source),[])
                self.assertEqual(job["result"]["score"]["value"],2)
                self.assertEqual([r.stage for r in fixture.requests],["statement_ocr","solution_ocr","grading"])
                # Condition request contains no pupil solution photo.
                self.assertEqual(sum(p["type"]=="image_url" for p in fixture.requests[0].payload["messages"][1]["content"]),1)
                self.assertNotIn("reference_key",fixture.requests[0].payload["messages"][1]["content"][0]["text"])
                key=next(f for f in job["result"]["facts"] if f["id"]=="condition-key")
                self.assertEqual(key["method"],"exact_code");self.assertEqual(freeze_identity(),before)
                self.assertEqual(job["accounting"]["cost"]["rub"],"0.3")
                self.assertNotIn("с конкретным запросом выбора/кадрирования и affects_score=yes;",fixture.requests[-1].payload["messages"][0]["content"])
    def test_two_pages_are_read_in_one_call_with_complete_coverage(self):
        service,fixture=self.service();_,job=self.run_work(service,pages=2)
        self.assertEqual(job["state"],"completed",job)
        self.assertEqual(len(job["result"]["coverage"]),2);self.assertEqual(len(fixture.requests),3)
    def test_second_model_uses_its_own_reasoning_profile_for_all_stages(self):
        fixture=Fixture(condition())
        service=FreshChecking(self.store,policy=fixture_policy("D"),ledger=self.root/"ledger",transport_factory=fixture.factory)
        self.services.append(service)
        work=self.store.save(valid_form())
        job=service.create(dict(work_id=work["id"],model="D",complete=True,single_task=True),background=False)["job"]
        self.assertEqual(job["state"],"completed",job)
        self.assertEqual(len(fixture.requests),3)
        self.assertTrue(all(r.payload["model"]=="deepseek/deepseek-v4.1-flash" and r.payload["reasoning_effort"]=="high" for r in fixture.requests))
    def test_malformed_paid_configuration_fails_closed(self):
        from app.live_checking import configured_policy
        path=self.root/"invalid-policy.json";path.write_text("{")
        with patch("app.live_checking.POLICY_PATH",path):self.assertIsNone(configured_policy())
    def test_duplicate_click_sends_once_and_history_survives_service_restart(self):
        service,fixture=self.service();work,job=self.run_work(service)
        result=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True),background=False)
        self.assertTrue(result["reused"]);self.assertEqual(len(fixture.requests),3)
        reloaded=FreshChecking(self.store,policy=service.policy,ledger=service.ledger,transport_factory=fixture.factory)
        self.services.append(reloaded);self.assertEqual(reloaded.latest(work["id"])["id"],job["id"])
    def test_empty_answer_one_targeted_recheck_then_specific_question(self):
        service,fixture=self.service(condition(rhs="2"));_,job=self.run_work(service)
        self.assertEqual(job["state"],"needs_clarification");self.assertEqual(job["error_code"],"condition_suspicious")
        self.assertEqual(len(fixture.requests),2);self.assertEqual(len(job["condition_versions"]),2)
        self.assertIsNone(job["result_ref"]);self.assertTrue(job["solver"]["empty"])
    def test_human_confirmed_empty_answer_continues_without_third_condition_call(self):
        service,fixture=self.service(condition(rhs="2"));_,job=self.run_work(service)
        result=service.confirm_empty_condition(job["id"],dict(literal=job["condition_versions"][-1]["solver_input"]["literal"],confirm_empty=True),background=False)
        self.assertEqual(result["job"]["state"],"completed",result)
        self.assertEqual([r.stage for r in fixture.requests],["statement_ocr","statement_ocr","solution_ocr","grading"])
        result2=service.confirm_empty_condition(job["id"],dict(literal=job["condition_versions"][-1]["solver_input"]["literal"],confirm_empty=True),background=False)
        self.assertTrue(result2["reused"]);self.assertEqual(len(fixture.requests),4)
    def test_empty_confirmation_cannot_approve_changed_or_unsupported_condition(self):
        service,fixture=self.service(condition(rhs="2"));_,job=self.run_work(service)
        with self.assertRaises(ValueError):service.confirm_empty_condition(job["id"],dict(literal="different",confirm_empty=True),background=False)
        self.assertEqual(len(fixture.requests),2)
    def test_simulated_live_interface_exports_three_real_journal_calls(self):
        service,fixture=self.service()
        service.policy=replace(service.policy,mode="live");fixture.kind="live";fixture.total_output_bound_verified=True
        _,job=self.run_work(service)
        self.assertEqual(job["state"],"completed",job)
        self.assertEqual(job["result"]["run"]["mode"],"live")
        self.assertEqual(len(job["result"]["run"]["calls"]),3)
        self.assertEqual(job["result"]["run"]["cost"]["rub"],"0.3")
        self.assertTrue(all("offline" not in u["source"]["ref"] for u in job["result"]["units"]))
    def test_unsupported_solver_and_mixed_tasks_do_not_grade(self):
        for c,code in ((condition(lhs="log(x)"),"solver_unsupported"),(condition(status="multiple_tasks"),"multiple_tasks")):
            service,fixture=self.service(c);service.ledger=self.root/code
            _,job=self.run_work(service);self.assertEqual(job["error_code"],code);self.assertEqual(len(fixture.requests),1)
    def test_uncertain_condition_one_recheck_no_student_penalty(self):
        service,fixture=self.service(condition(status="uncertain",uncertainties=["Не виден знак равенства."]))
        _,job=self.run_work(service);self.assertEqual(job["error_code"],"condition_uncertain");self.assertEqual(len(fixture.requests),2)
    def test_unknown_cost_stops_after_first_send(self):
        service,fixture=self.service();fixture.cost=None
        _,job=self.run_work(service);self.assertEqual(job["error_code"],"cost_unknown");self.assertEqual(len(fixture.requests),1)
        self.assertEqual(job["accounting"]["cost"]["status"],"unknown")
    def test_invalid_response_not_automatically_retried(self):
        service,fixture=self.service();fixture.malformed=True
        _,job=self.run_work(service);self.assertEqual(job["error_code"],"invalid_response");self.assertEqual(len(fixture.requests),1)
    def test_grading_failure_preserves_reading_without_publishing_score(self):
        service,fixture=self.service()
        original=fixture.factory
        def factory(request):
            fixture.malformed=request.stage=="grading"
            return original(request)
        service.transport_factory=factory
        _,job=self.run_work(service)
        self.assertEqual(job["error_code"],"invalid_response")
        self.assertEqual(len(fixture.requests),3)
        self.assertTrue(job["result"]["units"]);self.assertIsNone(job["result"]["score"]["value"])
    def test_changed_input_stops_and_cannot_publish_score(self):
        service,fixture=self.service()
        form=valid_form();form['task_text']='';form["task"]=[image_data()];work=self.store.save(form)
        file=self.store.root/work["id"]/"work.json"
        def change():
            record=json.loads(file.read_text());record["task_text"]="changed";file.write_text(json.dumps(record))
        fixture.change_work=change
        result=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True),background=False)["job"]
        self.assertEqual(result["error_code"],"input_changed");self.assertEqual(len(fixture.requests),1)
    def test_default_paid_gate_closed_without_key_or_ledger(self):
        with patch("app.live_checking.configured_policy",return_value=None):
            service=FreshChecking(self.store,ledger=self.root/"closed")
        self.services.append(service)
        work=self.store.save(valid_form())
        with patch("app.live_checking.read_credential",side_effect=AssertionError("No key before gate")):
            response=service.create(dict(work_id=work["id"],model="G",complete=True,single_task=True))
        self.assertFalse(response["accepted"]);self.assertFalse((self.root/"closed").exists())
    def test_confirmation_and_request_options_are_not_silent(self):
        service,_=self.service();work=self.store.save(valid_form())
        for change in ({"complete":False},{"single_task":False},{"model":"unknown"},{"budget":999}):
            with self.assertRaises(ValueError):service.create({**dict(work_id=work["id"],model="G",complete=True,single_task=True),**change})


if __name__=="__main__": unittest.main()
