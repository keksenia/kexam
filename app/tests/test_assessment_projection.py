"""Synthetic zero-score reference projection and offline revalidation."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import json
import tempfile
import unittest
from app.assessment_projection import project_assessment
from app.call_guard import CallGuard
from app.guard_demo import fixture_request,fixture_policy,FixtureTransport,fixture_validator


def zero_response():
    sample=json.loads((Path(__file__).resolve().parents[2]/"planning/m05/examples/synthetic_13.json").read_text())["assessment"]
    original=sample["requirements"][0];sample["requirements"]=[]
    for rid in ("A-ANSWER","A-JUSTIFICATION","B-ANSWER","B-JUSTIFICATION","CALC-ONLY"):
        r=deepcopy(original);r.update(id=rid,criterion_id="M02-FIPI13-1",status="not_fulfilled");sample["requirements"].append(r)
    sample["score"].update(value=0,criterion_ids=["M02-FIPI13-0"],requirement_ids=[r["id"] for r in sample["requirements"]])
    return sample


class ProjectionTests(unittest.TestCase):
    def test_zero_basis_keeps_score_facts_original_requirements_and_is_idempotent(self):
        v=zero_response();before=deepcopy(v);out=project_assessment(v,"trigonometric_equation")
        self.assertEqual(v,before);self.assertEqual(out["score"]["value"],0)
        self.assertEqual(out["facts"],v["facts"]);self.assertEqual(out["requirements"][:5],v["requirements"])
        self.assertEqual(len(out["requirements"]),10);self.assertEqual(project_assessment(out,"trigonometric_equation"),out)
    def test_unknown_missing_or_positive_evidence_cannot_be_projected(self):
        for kind in ("unknown","missing","positive","clarification"):
            v=zero_response()
            if kind=="unknown":v["requirements"][0]["status"]="unknown"
            if kind=="missing":v["requirements"].pop()
            if kind=="positive":
                for r in v["requirements"][:2]:r["status"]="fulfilled"
            if kind=="clarification":v["clarifications"]=[dict(id="unclear")]
            self.assertEqual(project_assessment(v,"trigonometric_equation"),v)
    def test_other_scores_and_subjects_are_not_rewritten(self):
        v=zero_response();self.assertEqual(project_assessment(v,"inequality"),v)
        v["score"]["value"]=1;self.assertEqual(project_assessment(v,"trigonometric_equation"),v)
    def test_local_revalidation_preserves_fee_original_artifact_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            request=fixture_request();guard=CallGuard(directory,fixture_policy([request]));transport=FixtureTransport()
            def broken(v):raise ValueError("Old controller defect")
            first=guard.execute(request,transport,broken);self.assertEqual(first.status,"invalid_response")
            before=guard.snapshot()
            with closing(guard._connect()) as con:row=con.execute("SELECT * FROM calls WHERE id=?",(first.attempt_id,)).fetchone()
            raw=(Path(directory)/row["response_ref"]).read_bytes()
            out=guard.revalidate_response(first.attempt_id,fixture_validator,validator_ref="fixed-local-validator")
            self.assertEqual(out.status,"succeeded");self.assertTrue(out.reused);self.assertEqual(transport.calls,1)
            self.assertEqual(guard.snapshot()["cost"],before["cost"]);self.assertEqual((Path(directory)/row["response_ref"]).read_bytes(),raw)
            with closing(guard._connect()) as con:self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE kind='response_revalidated'").fetchone()[0],1)
            with self.assertRaises(ValueError):guard.revalidate_response(first.attempt_id,fixture_validator,validator_ref="again")
