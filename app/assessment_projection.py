"""Controller projection of existing evidence, without changing model scores."""
from copy import deepcopy
import json
from pathlib import Path

VERSION = "assessment-projection-1.0.0"
CATALOGUE = Path(__file__).resolve().parent.parent / "planning/m02/requirements.json"


def project_assessment(response, task_type):
    value=deepcopy(response)
    score=value.get("score",{})
    if (task_type!="trigonometric_equation" or score.get("status")!="proposed" or score.get("value")!=0
            or score.get("criterion_ids")!=["M02-FIPI13-0"]):return value
    requirements={r["id"]:r for r in value.get("requirements",[])}
    selected=set(score.get("requirement_ids",[]))
    if any(requirements.get(rid,{}).get("criterion_id")=="M02-FIPI13-0" for rid in selected):return value
    catalogue=json.loads(CATALOGUE.read_text())
    expected={r["id"] for r in catalogue}
    # Complete assessed alternatives are required. Never turn missing/unknown
    # evidence or a reading ambiguity into a zero-score justification.
    if (not expected<=selected or not expected<=requirements.keys()
            or any(requirements[rid]["status"] not in {"fulfilled","not_fulfilled"} for rid in expected)
            or requirements["CALC-ONLY"]["status"]!="not_fulfilled"
            or all(requirements[rid]["status"]=="fulfilled" for rid in ("A-ANSWER","A-JUSTIFICATION"))
            or any(value.get(k) for k in ("ambiguity_groups","scenarios","case_checks","clarifications"))):return value
    used={item["id"] for section in value.values() if isinstance(section,list) for item in section if isinstance(item,dict) and "id" in item}
    additions=[]
    for descriptor in catalogue:
        original=requirements[descriptor["id"]]
        if ("M02-FIPI13-0" not in descriptor["source_criterion_ids"]
                or original["criterion_id"] not in descriptor["source_criterion_ids"]):return value
        projected=deepcopy(original);projected["id"]="zero-basis-"+original["id"]
        if projected["id"] in used:raise ValueError("projection_id_collision")
        projected["criterion_id"]="M02-FIPI13-0"
        # Same assessed requirement/evidence, applied also to its explicitly
        # permitted fallback criterion. Original positive-criterion links stay.
        additions.append(projected)
    value["requirements"].extend(additions)
    value["score"]["requirement_ids"].extend(r["id"] for r in additions)
    return value
