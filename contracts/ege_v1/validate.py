"""M01 boundary validation only. No model calls, file resolution or math evaluation."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads(Path(__file__).with_name("schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)


def digest(value):
    """Project canonical JSON v1: no mathematical/text normalization."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reading_digest(record):
    """Snapshot of literal reading, human reading corrections and semantic context."""
    return digest({key: record[key] for key in (
        "input_ref", "units", "corrections", "interpretations", "branches",
        "assumptions", "relations")})


def assessment_digest(record):
    """Grading dependencies; wording-only feedback edits do not require new OCR."""
    return digest({"reading_sha256": reading_digest(record), **{
        key: record[key] for key in ("facts", "criteria", "requirements",
                                    "case_checks", "scenarios", "ambiguity_groups", "findings", "coverage")}})


def validate(record, source_input=None):
    """Return errors; results require their exact input. Empty means valid structure.

    This cannot establish truth, rubric authority, image redaction or model quality.
    Error strings intentionally omit submitted text and provider responses.
    """
    errors = []
    kind = record.get("kind") if isinstance(record, dict) else None
    if kind not in ("input", "result"):
        return ["record: unknown kind"]
    schema = {"$ref": "#/$defs/" + kind, "$defs": SCHEMA["$defs"]}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for e in validator.iter_errors(record):
        errors.append("/" + "/".join(map(str, e.absolute_path)) + ": " + e.validator)
    if errors:
        return errors

    def need(condition, message):
        if not condition:
            errors.append(message)

    def unique(items, field, label):
        ids = [x[field] for x in items]
        need(len(ids) == len(set(ids)), label + ": duplicate identifiers")
        return set(ids)

    def box_valid(box, label):
        if box is not None:
            need(box[0] < box[2] and box[1] < box[3], label + ": empty/reversed box")

    if kind == "input":
        task = record["task"]
        need((task["exam_number"], task["task_type"]) in
             {(13, "trigonometric_equation"), (15, "inequality")}, "task: number/type mismatch")
        unique(record["pages"], "id", "pages")
        for role in ("solution", "task"):
            pages = [p for p in record["pages"] if p["role"] == role]
            need([p["order"] for p in pages] == list(range(1, len(pages) + 1)),
                 "pages: per-role order must be contiguous and in array order")
        assets = {}
        for page in record["pages"]:
            box_valid(page["region"], "page.region")
            signature = (page["sha256"], page["width"], page["height"])
            need(page["asset_id"] not in assets or assets[page["asset_id"]] == signature,
                 "pages: asset metadata conflict")
            assets[page["asset_id"]] = signature
        st = task["statement"]
        if st["completeness"] == "confirmed":
            need(not st["missing"], "statement: confirmed with missing parts")
            need(bool(st["text"].strip()) or any(p["role"] == "task" for p in record["pages"]),
                 "statement: confirmed without source content")
        if record["privacy"]["status"] == "reviewed":
            need(bool(record["privacy"]["reviewer_ref"]), "privacy: review needs provenance")
        return errors

    if source_input is None:
        return ["result: exact source input is required"]
    input_errors = validate(source_input)
    if input_errors or source_input.get("kind") != "input":
        return ["result: invalid source input"]
    inp = record["input_ref"]
    need(inp == {"id": source_input["id"], "revision": source_input["revision"],
                 "sha256": digest(source_input)}, "result: input identity/revision/hash mismatch")
    need(record["supersedes"] != record["id"], "result: cannot supersede itself")
    pages = {p["id"]: p for p in source_input["pages"]}
    ids = {name: unique(record[name], "id", name) for name in
           ("units", "corrections", "interpretations", "facts", "findings", "clarifications", "criteria", "feedback",
            "assumptions", "branches", "relations", "requirements", "case_checks", "scenarios",
            "ambiguity_groups", "human_actions")}
    all_ids = [x["id"] for name in ids for x in record[name]]
    need(len(all_ids) == len(set(all_ids)), "result: IDs must be unique across sections")

    def references(values, section, label):
        need(len(values) == len(set(values)), label + ": duplicate reference")
        need(set(values) <= ids[section], label + ": dangling reference")

    def anchors(items):
        for anchor in items:
            need(anchor["page_id"] in pages, "anchor: unknown page")
            box_valid(anchor["box"], "anchor")
            region = pages.get(anchor["page_id"], {}).get("region")
            box = anchor["box"]
            if region is not None and box is not None:
                need(region[0] <= box[0] < box[2] <= region[2] and
                     region[1] <= box[1] < box[3] <= region[3], "anchor: outside selected problem region")

    for unit in record["units"]:
        anchors(unit["anchors"])
        need(all(pages.get(a["page_id"], {}).get("role") == "solution" for a in unit["anchors"]),
             "unit: student evidence must refer to solution pages")
        if unit["kind"] != "diagram" and unit["reading_status"] == "clear":
            need(bool(unit["literal"]), "unit: clear text cannot be empty")
        if unit["kind"] == "diagram":
            need(unit["literal"] is None, "unit: diagram description belongs in interpretations")
    effective = {u["id"]: u["literal"] for u in record["units"]}
    for c in record["corrections"]:
        need(c["unit_id"] in effective, "correction: unknown unit")
        need(c["before"] == effective.get(c["unit_id"]), "correction: broken before/after chain")
        effective[c["unit_id"]] = c["after"]
    by_id = {name: {x["id"]: x for x in record[name]} for name in ids}
    reading_hash = reading_digest(record)
    grading_hash = assessment_digest(record)

    def acyclic(graph):
        # Iterative DFS avoids crashing on a long (otherwise valid) reasoning chain.
        done, active = set(), set()
        for root in graph:
            stack = [(root, False)]
            while stack:
                node, leaving = stack.pop()
                if leaving:
                    active.discard(node)
                    done.add(node)
                elif node not in done:
                    if node in active:
                        return False
                    active.add(node)
                    stack.append((node, True))
                    stack.extend((n, False) for n in reversed(graph.get(node, [])))
        return True

    branches = by_id["branches"]
    for branch in record["branches"]:
        references(branch["unit_ids"], "units", "branch.units")
        references(branch["assumption_ids"], "assumptions", "branch.assumptions")
        if branch["parent_id"] is not None:
            need(branch["parent_id"] in branches, "branch: unknown parent")
    need(acyclic({b["id"]: [b["parent_id"]] if b["parent_id"] else []
                  for b in record["branches"]}), "branches: cyclic parents")

    def lineage(branch_id):
        seen = set()
        while branch_id in branches and branch_id not in seen:
            seen.add(branch_id)
            branch_id = branches[branch_id]["parent_id"]
        return seen

    for assumption in record["assumptions"]:
        need(assumption["branch_id"] in branches, "assumption: unknown branch")
        target = {"student_unit": "units", "derived_fact": "facts",
                  "human_interpretation": "interpretations"}.get(assumption["origin"])
        if target:
            need(assumption["source_ref"] in ids[target], "assumption: missing source object")
        if assumption["origin"] == "student_unit":
            unit = by_id["units"].get(assumption["source_ref"], {})
            need(unit.get("author") == "student" and unit.get("activity") == "active",
                 "assumption: student premise requires active student writing")
        if assumption["origin"] == "human_interpretation" and assumption["status"] == "established":
            interpretation = by_id["interpretations"].get(assumption["source_ref"], {})
            need(interpretation.get("status") == "human_confirmed" and interpretation.get("source", {}).get("kind") == "human_review",
                 "assumption: human premise needs human-confirmed interpretation")
        if assumption["origin"] == "derived_fact":
            source_fact = by_id["facts"].get(assumption["source_ref"], {})
            need(source_fact.get("branch_id") in lineage(assumption["branch_id"]), "assumption: source fact from unrelated branch")
        if assumption["origin"] == "hypothesis":
            need(assumption["status"] != "established", "assumption: system hypothesis is not established")
        if assumption["origin"] == "task_statement":
            need(assumption["source_ref"] == source_input["id"], "assumption: task source mismatch")
            if assumption["status"] == "established":
                need(source_input["task"]["statement"]["completeness"] == "confirmed",
                     "assumption: task premise needs confirmed statement")
    for relation in record["relations"]:
        references([relation["from_unit_id"], relation["to_unit_id"]], "units", "relation.units")

    def context(item, label):
        need(item["branch_id"] in branches, label + ": unknown branch")
        references(item["assumption_ids"], "assumptions", label + ".assumptions")
        references(item["context_unit_ids"], "units", label + ".context")
        permitted = lineage(item["branch_id"])
        for aid in item["assumption_ids"]:
            a = by_id["assumptions"].get(aid)
            if a:
                need(a["branch_id"] in permitted, label + ": assumption from unrelated branch")
                declared = {aid for bid in permitted for aid in branches[bid]["assumption_ids"]}
                need(aid in declared, label + ": undeclared branch assumption")

    for interpretation in record["interpretations"]:
        references(interpretation["unit_ids"], "units", "interpretation.units")
        context(interpretation, "interpretation")
        if interpretation["status"] == "human_confirmed":
            need(interpretation["source"]["kind"] == "human_review", "interpretation: confirmation needs human source")
    graph = {}
    for fact in record["facts"]:
        references(fact["unit_ids"], "units", "fact.units")
        references(fact["interpretation_ids"], "interpretations", "fact.interpretations")
        context(fact, "fact")
        dependencies = fact["depends_on_fact_ids"]
        references(dependencies, "facts", "fact.dependencies")
        graph[fact["id"]] = dependencies[:]
        joined_branches = set()
        if "branch_join" in fact:
            join = fact["branch_join"]
            references(join["branch_ids"], "branches", "fact.join.branches")
            references([join["coverage_fact_id"]], "facts", "fact.join.coverage")
            coverage_fact = by_id["facts"].get(join["coverage_fact_id"], {})
            need(join["coverage_fact_id"] in dependencies, "fact: join needs explicit coverage dependency")
            need(coverage_fact.get("status") == "supported" and coverage_fact.get("branch_id") == fact["branch_id"],
                 "fact: branch join needs supported parent-scope coverage fact")
            need(all(branches.get(bid, {}).get("parent_id") == fact["branch_id"] and
                     branches.get(bid, {}).get("status") == "active" for bid in join["branch_ids"]),
                 "fact: only active child cases may be joined")
            joined_branches = set(join["branch_ids"])
            need(joined_branches <= {by_id["facts"].get(fid, {}).get("branch_id") for fid in dependencies},
                 "fact: branch join lacks evidence from a case")
        for aid in fact["assumption_ids"]:
            assumption = by_id["assumptions"].get(aid, {})
            if assumption.get("origin") == "derived_fact":
                graph[fact["id"]].append(assumption["source_ref"])
            if fact["status"] != "unknown":
                need(assumption.get("status") != "unknown", "fact: unresolved premise cannot prove claim")
                if assumption.get("origin") == "derived_fact":
                    need(by_id["facts"].get(assumption["source_ref"], {}).get("status") == "supported",
                         "fact: derived premise lacks support")
        if fact["status"] != "unknown":
            need(fact["basis_sha256"] == reading_hash, "fact: stale reading/context snapshot")
            need(branches.get(fact["branch_id"], {}).get("status") == "active", "fact: inactive branch")
            for dependency in dependencies:
                parent = by_id["facts"].get(dependency, {})
                need(parent.get("status") == "supported", "fact: dependency does not support conclusion")
                need(parent.get("branch_id") in lineage(fact["branch_id"]) | joined_branches, "fact: dependency from unrelated branch")
        if fact["method"] in ("exact_code", "counterexample"):
            need(bool(fact["tool_version"]) and "code_check" in fact, "fact: code needs operation, evidence and version")
    need(acyclic(graph), "facts: cyclic dependencies")
    for criterion in record["criteria"]:
        need(criterion["exam_year"] == source_input["task"]["exam_year"] and
             criterion["exam_number"] == source_input["task"]["exam_number"], "criterion: wrong year/task")
    coverage_ids = unique(record["coverage"], "page_id", "coverage")
    solution_pages = {p["id"] for p in pages.values() if p["role"] == "solution"}
    need(coverage_ids == solution_pages, "coverage: each solution page requires a status")
    fully_read = {c["page_id"] for c in record["coverage"] if c["status"] == "complete"}
    accounted = {c["page_id"] for c in record["coverage"] if c["score_scope"] == "accounted"}
    for coverage in record["coverage"]:
        if coverage["status"] in ("unreadable", "not_processed"):
            need(coverage["score_scope"] == "unresolved", "coverage: unread page cannot be declared immaterial")

    for requirement in record["requirements"]:
        references([requirement["criterion_id"]], "criteria", "requirement.criterion")
        references(requirement["unit_ids"], "units", "requirement.units")
        references(requirement["fact_ids"], "facts", "requirement.facts")
        need(requirement["branch_id"] in branches, "requirement: unknown branch")
        need(set(requirement["coverage_page_ids"]) <= solution_pages, "requirement: wrong coverage pages")
        if requirement["status"] != "unknown":
            need(branches.get(requirement["branch_id"], {}).get("status") == "active", "requirement: inactive branch")
            facts = [by_id["facts"][fid] for fid in requirement["fact_ids"] if fid in by_id["facts"]]
            need(all(f["status"] != "unknown" for f in facts), "requirement: unknown fact cannot establish fulfillment/absence")
            need(all(f["branch_id"] in lineage(requirement["branch_id"]) for f in facts), "requirement: evidence from unrelated branch")
            evidence = [by_id["units"][uid] for uid in requirement["unit_ids"] if uid in by_id["units"]]
            need(all(u["author"] == "student" and u["activity"] == "active" for u in evidence),
                 "requirement: active student evidence required")
            if requirement["status"] == "fulfilled":
                need(bool(evidence) and bool(facts), "requirement: fulfillment needs student evidence and checked facts")
                need(all(f["status"] == "supported" for f in facts), "requirement: fulfillment needs supported facts")
            else:
                absence_coverage = (bool(solution_pages) and set(requirement["coverage_page_ids"]) == solution_pages
                                    and solution_pages <= fully_read
                                    and source_input["capture"]["pages_complete"] == "confirmed")
                need(bool(facts) or absence_coverage, "requirement: absence needs checked facts or full inspected scope")
    for case in record["case_checks"]:
        references(case["requirement_ids"], "requirements", "case.requirements")
        references(case["fact_ids"], "facts", "case.facts")
        if case["status"] == "applicable":
            need(bool(case["matches"]) and not case["exclusions"] and bool(case["fact_ids"]),
                 "case: applicability requires matches, evidence and no excluding conditions")
            need(all(by_id["facts"].get(fid, {}).get("status") == "supported" for fid in case["fact_ids"]),
                 "case: applicability cannot rely on unknown/contradicted evidence")
    for finding in record["findings"]:
        references(finding["unit_ids"], "units", "finding.units")
        references(finding["fact_ids"], "facts", "finding.facts")
        references(finding["criterion_ids"], "criteria", "finding.criteria")
        references(finding["requirement_ids"], "requirements", "finding.requirements")
        if finding["primary_finding_id"] is not None:
            need(finding["primary_finding_id"] in ids["findings"] and finding["primary_finding_id"] != finding["id"],
                 "finding: invalid primary cause")
        if finding["category"] == "system_uncertainty":
            need(finding["affects_score"] != "yes", "finding: system uncertainty cannot penalize student")
        elif finding["status"] == "established":
            need(bool(finding["evidence_assertions"]) or
                 (finding["category"] == "solution_deficiency" and any(
                    by_id["requirements"].get(rid, {}).get("status") == "not_fulfilled"
                    for rid in finding["requirement_ids"])), "finding: student problem lacks established evidence")
        for assertion in finding["evidence_assertions"]:
            need(assertion["fact_id"] in finding["fact_ids"], "finding: assertion not listed in evidence")
            need(by_id["facts"].get(assertion["fact_id"], {}).get("status") == assertion["expected_status"],
                 "finding: assertion polarity is not supported")
        if finding["affects_score"] == "yes":
            need(finding["status"] == "established" and bool(finding["criterion_ids"]), "finding: penalty needs established criterion")
    need(acyclic({f["id"]: [f["primary_finding_id"]] if f["primary_finding_id"] else []
                  for f in record["findings"]}), "findings: cyclic root causes")

    for scenario in record["scenarios"]:
        need(scenario["branch_id"] in branches, "scenario: unknown branch")
        references(scenario["unit_ids"], "units", "scenario.units")
        references(scenario["requirement_ids"], "requirements", "scenario.requirements")
        references(scenario["criterion_ids"], "criteria", "scenario.criteria")
        established = scenario["status"] == "established"
        need(established == (scenario["score_value"] is not None), "scenario: unproved branch cannot carry score")
        if established:
            need(bool(scenario["requirement_ids"]) and bool(scenario["criterion_ids"]), "scenario: score lacks requirements/criteria")
            selected = [by_id["requirements"].get(rid, {}) for rid in scenario["requirement_ids"]]
            need(all(r.get("status") in ("fulfilled", "not_fulfilled") for r in selected), "scenario: unresolved requirement")
            need(all(r.get("branch_id") in lineage(scenario["branch_id"]) for r in selected), "scenario: unrelated requirements")
            need(set(scenario["criterion_ids"]) <= {r.get("criterion_id") for r in selected}, "scenario: criterion lacks assessment")
    for group in record["ambiguity_groups"]:
        references(group["unit_ids"], "units", "ambiguity.units")
        references(group["scenario_ids"], "scenarios", "ambiguity.scenarios")
        need(all(set(by_id["scenarios"].get(sid, {}).get("unit_ids", [])) <= set(group["unit_ids"])
                 for sid in group["scenario_ids"]), "ambiguity: scenario outside its source scope")
        need(set(group["unit_ids"]) == {uid for sid in group["scenario_ids"]
                                      for uid in by_id["scenarios"].get(sid, {}).get("unit_ids", [])},
             "ambiguity: scenarios do not cover every source unit")
    for clarification in record["clarifications"]:
        references(clarification["unit_ids"], "units", "clarification.units")
        anchors(clarification["anchors"])
        if clarification["status"] == "answered":
            need(bool(clarification["response"]) and bool(clarification["actor_ref"]), "clarification: answer needs provenance")
        else:
            need(clarification["response"] is None, "clarification: unanswered request carries answer")
        if clarification["ambiguity_group_id"] is not None:
            references([clarification["ambiguity_group_id"]], "ambiguity_groups", "clarification.group")
            group = by_id["ambiguity_groups"].get(clarification["ambiguity_group_id"], {})
            need(bool(clarification["unit_ids"]) and set(clarification["unit_ids"]) <= set(group.get("unit_ids", [])),
                 "clarification: question outside alternative-reading scope")
    readiness = record["readiness"]
    for feedback in record["feedback"]:
        references(feedback["unit_ids"], "units", "feedback.units")
        references(feedback["finding_ids"], "findings", "feedback.findings")
        if feedback["status"] != "ready":
            need(feedback["audience"] == "teacher", "feedback: unfinished claim cannot be sent to student")
        else:
            need(feedback["basis_sha256"] == grading_hash, "feedback: stale assessment basis")
            need(all(by_id["findings"].get(fid, {}).get("status") == "established" for fid in feedback["finding_ids"]),
                 "feedback: pending finding cannot be presented as established")
    if readiness["feedback"] == "ready":
        need(all(f["status"] == "ready" for f in record["feedback"]), "feedback readiness: unfinished entries remain")
        need(not any(q["status"] != "answered" and q["impact"] in ("substantive_feedback", "both")
                     for q in record["clarifications"]), "feedback readiness: open material question")
    elif readiness["feedback"] == "partial":
        need(any(f["status"] == "ready" for f in record["feedback"]), "feedback readiness: partial needs a ready part")

    score = record["score"]
    for key, section in (("criterion_ids", "criteria"), ("evidence_unit_ids", "units"), ("finding_ids", "findings"),
                         ("requirement_ids", "requirements"), ("case_check_ids", "case_checks"), ("ambiguity_group_ids", "ambiguity_groups")):
        references(score[key], section, "score." + key)
    penalty_findings = {f["id"] for f in record["findings"] if f["category"] != "system_uncertainty"
                        and f["status"] == "established" and f["affects_score"] == "yes"}
    need(set(score["finding_ids"]) <= penalty_findings, "score: penalty reasons cannot be learning notes or system doubts")
    assigned = score["value"] is not None
    need(assigned == (score["status"] in ("proposed", "human_confirmed")), "score: status/value mismatch")
    need(assigned == (readiness["score"] == "ready"), "score: readiness/value mismatch")
    need((score["status"] == "stale") == (readiness["score"] == "stale"), "score: stale status mismatch")
    need(len(score["ambiguity_group_ids"]) <= 1, "score: combine interacting ambiguities into one exhaustive scenario group")
    robust_groups = set()
    for group_id in score["ambiguity_group_ids"]:
        group = by_id["ambiguity_groups"].get(group_id, {})
        possibilities = [by_id["scenarios"].get(sid, {}) for sid in group.get("scenario_ids", [])]
        remaining = [scenario for scenario in possibilities if scenario.get("status") != "excluded"]
        robust = (group.get("exhaustive") is True and bool(remaining) and
                  all(scenario.get("status") == "established" and scenario.get("score_value") == score["value"] for scenario in remaining))
        if robust:
            robust_groups.add(group_id)
        if assigned:
            need(robust, "score: every remaining plausible reading must independently justify the same score")
    if assigned:
        need(score["basis_sha256"] == grading_hash, "score: stale assessment snapshot")
        need(source_input["task"]["statement"]["completeness"] == "confirmed" and
             source_input["capture"]["pages_complete"] == "confirmed", "score: input completeness unconfirmed")
        need(bool(solution_pages) and solution_pages <= accounted, "score: material reading scope unaccounted")
        blank_evidence = (score["value"] == 0 and source_input["capture"]["student_attempt"] == "blank" and not record["units"])
        need(bool(score["criterion_ids"]) and (bool(score["evidence_unit_ids"]) or blank_evidence), "score: lacks evidence/criteria")
        need(bool(score["requirement_ids"]), "score: assessed requirements required")
        selected = [by_id["requirements"].get(rid, {}) for rid in score["requirement_ids"]]
        need(all(r.get("status") in ("fulfilled", "not_fulfilled") for r in selected), "score: unresolved selected requirement")
        covered_scenario_requirements = {rid for gid in robust_groups
            for sid in by_id["ambiguity_groups"][gid]["scenario_ids"]
            if by_id["scenarios"].get(sid, {}).get("status") == "established"
            for rid in by_id["scenarios"][sid]["requirement_ids"]}
        def conditional_requirement(requirement):
            # Mathematical case joins discharge only local case conditions;
            # uncertainty about what the student wrote still needs scenarios.
            def unresolved(aid, discharged):
                assumption = by_id["assumptions"].get(aid, {})
                return (assumption.get("status") in ("conditional", "unknown") and
                        not (assumption.get("status") == "conditional" and
                             assumption.get("branch_id") in discharged and
                             assumption.get("origin") != "hypothesis"))
            for bid in lineage(requirement.get("branch_id")):
                if any(unresolved(aid, set()) for aid in branches[bid]["assumption_ids"]):
                    return True
            pending, seen = [(fid, frozenset()) for fid in requirement.get("fact_ids", [])], set()
            while pending:
                fid, discharged = pending.pop()
                if (fid, discharged) in seen:
                    continue
                seen.add((fid, discharged))
                fact = by_id["facts"].get(fid, {})
                premise_ids = set(fact.get("assumption_ids", []))
                premise_ids.update(aid for bid in lineage(fact.get("branch_id")) for aid in branches[bid]["assumption_ids"])
                if any(unresolved(aid, discharged) for aid in premise_ids):
                    return True
                joined = discharged | frozenset(fact.get("branch_join", {}).get("branch_ids", []))
                pending.extend((child, joined) for child in fact.get("depends_on_fact_ids", []))
                for aid in fact.get("assumption_ids", []):
                    assumption = by_id["assumptions"].get(aid, {})
                    if assumption.get("origin") == "derived_fact":
                        pending.append((assumption.get("source_ref"), discharged))
            return False
        need(all(not conditional_requirement(r) or r.get("id") in covered_scenario_requirements for r in selected),
             "score: conditional evidence needs exhaustive independently scored scenarios")
        need(set(score["criterion_ids"]) <= {r.get("criterion_id") for r in selected}, "score: criterion lacks assessed requirement")
        need(all(by_id["case_checks"].get(cid, {}).get("status") == "applicable" for cid in score["case_check_ids"]),
             "score: unestablished corner-case applicability")
        need(score["authority"] != "none" and bool(score["source_ref"]), "score: lacks authority/source")
        for clarification in record["clarifications"]:
            if clarification["status"] != "answered":
                if clarification["impact"] in ("score", "both"):
                    need(False, "score: unresolved score-changing clarification")
                if clarification["impact"] == "score_basis":
                    need(clarification["ambiguity_group_id"] in robust_groups, "score: alternative grounds not independently proved")
        need(not any(f["affects_score"] == "unresolved" for f in record["findings"]), "score: unresolved score effect")
        need(not any("score" in issue["blocks"] for issue in record["issues"]), "score: blocking issues remain")
        corrected = {c["unit_id"] for c in record["corrections"]}
        explained = {uid for f in record["findings"] if f["category"] == "system_uncertainty"
                     and f["affects_score"] == "no" for uid in f["unit_ids"]}
        alternatives = {uid for gid in robust_groups for uid in by_id["ambiguity_groups"][gid]["unit_ids"]}
        need(all(u["reading_status"] == "clear" or u["id"] in corrected | explained | alternatives
                 for u in record["units"]), "score: unexplained reading uncertainty")
    if score["status"] == "human_confirmed":
        need(score["authority"] in ("project_review", "exam_expert") and bool(score["source_ref"]), "score: human confirmation requires human provenance")
    if score["status"] == "proposed":
        need(score["authority"] in ("model", "exact_code"), "score: proposed score requires declared assessment authority")
    if record["status"] == "ready":
        need(readiness == {"score": "ready", "feedback": "ready"}, "ready: both parts must be ready")
        need(not record["issues"], "ready: processing issues remain")
        need(not any(q["status"] != "answered" for q in record["clarifications"]), "ready: open targeted request")
    if record["status"] == "needs_fragment":
        need(any(q["status"] != "answered" for q in record["clarifications"]), "needs_fragment: no open targeted request")
    if record["status"] in ("incomplete_input", "processing_error", "unsupported", "needs_review"):
        need(bool(record["issues"]), "result: non-ready state requires reason")
    if record["status"] == "incomplete_input":
        need(not assigned, "score: incomplete input cannot carry current grade")
    need(not any("feedback" in issue["blocks"] for issue in record["issues"]) or readiness["feedback"] != "ready",
         "feedback: blocking issue despite readiness")

    action_targets = {"reading_correction": "units", "interpretation_change": "interpretations",
                      "score_change": "score", "feedback_edit": "feedback", "report_used": "result"}
    for action in record["human_actions"]:
        section = action_targets[action["kind"]]
        need(action["target_section"] == section, "human action: wrong target type")
        expected_ids = {record["id"]} if section in ("score", "result") else ids[section]
        need(action["target_id"] in expected_ids, "human action: unknown target")
    action_chains = {}
    for action in record["human_actions"]:
        if action["kind"] in ("score_change", "feedback_edit", "interpretation_change"):
            key = (action["kind"], action["target_id"])
            if key in action_chains:
                need(action["before"] == action_chains[key]["after"], "human action: broken before/after chain")
            action_chains[key] = action
    for (action_kind, target_id), action in action_chains.items():
        if action_kind == "score_change":
            need(action["after"] == score["value"] and score["status"] == "human_confirmed", "human action: score change not reflected")
        elif action_kind == "feedback_edit":
            need(action["after"] == by_id["feedback"].get(target_id, {}).get("text"), "human action: wording edit not reflected")
        else:
            need(action["after"] == by_id["interpretations"].get(target_id, {}).get("meaning"), "human action: interpretation edit not reflected")
    for correction in record["corrections"]:
        action = by_id["human_actions"].get(correction["action_id"], {})
        need(action.get("kind") == "reading_correction" and action.get("target_id") == correction["unit_id"] and
             action.get("before") == correction["before"] and action.get("after") == correction["after"] and
             action.get("actor_ref") == correction["actor_ref"] and action.get("source_ref") == correction["source_ref"] and
             action.get("at") == correction["at"], "correction: matching scoped human action required")
    for action in record["human_actions"]:
        if action["kind"] == "reading_correction":
            need(any(c["action_id"] == action["id"] for c in record["corrections"]), "human action: missing reading correction")

    run = record["run"]
    call_ids = unique(run["calls"], "id", "calls")
    previous = set()
    for call in run["calls"]:
        if call["retry_of"] is not None:
            need(call["retry_of"] in previous, "call: retry must refer to earlier call")
        previous.add(call["id"])
        known = call["cost_basis"] != "unknown"
        need(known == (call["cost_rub"] is not None), "call: cost basis/value mismatch")
        if call["cost_basis"] == "token_estimate":
            need(bool(call["tariff_ref"]) and call["usage"]["input_tokens"] is not None and
                 call["usage"]["output_tokens"] is not None, "call: estimated cost lacks tariff/usage")
        if call["status"] == "succeeded":
            need(bool(call["response_ref"]), "call: successful response not preserved")
    cost = run["cost"]
    if run["calls"]:
        need(cost["status"] != "not_incurred", "cost: cannot claim no calls while calls exist")
        if any(c["cost_rub"] is None for c in run["calls"]):
            need(cost["status"] == "unknown" and cost["rub"] is None, "cost: unknown attempt hidden in total")
        else:
            total = sum((Decimal(c["cost_rub"]) for c in run["calls"]), Decimal(0))
            need(cost["status"] == "known" and cost["rub"] is not None and Decimal(cost["rub"]) == total,
                 "cost: total must include all attempts exactly")
    else:
        need((cost["status"], cost["rub"]) in (("unknown", None), ("not_incurred", "0")),
             "cost: no call ledger means unknown or explicitly no expense")
    if run["mode"] == "demo":
        need(not run["calls"], "run: demo cannot claim actual API calls")
    if run["mode"] == "live" and assigned:
        if score["authority"] == "model":
            need(any(c["stage"] == "grading" and c["status"] == "succeeded" for c in run["calls"]),
                 "run: live model assessment lacks a successful grading call")
        active_criteria = [c for c in record["criteria"] if c["id"] in score["criterion_ids"]]
        need(any(c["authority"] == "official" for c in active_criteria), "score: live result lacks official rubric")
        need(not any(c["authority"] == "synthetic" for c in active_criteria), "score: synthetic rubric in live result")
    for answer in record.get("answers", []):
        references(answer["unit_ids"], "units", "answer.units")
        expected = {"a", "b"} if source_input["task"]["exam_number"] == 13 else {"main"}
        need(answer["part"] in expected, "answer: invalid task part")
        if answer["exact_set"]:
            for interval in answer["exact_set"]["intervals"]:
                need(interval["left"] is not None or not interval["left_closed"], "answer: infinity cannot be closed")
                need(interval["right"] is not None or not interval["right_closed"], "answer: infinity cannot be closed")
        for family in answer["pi_families"]:
            need(int(family["period"]["numerator"]) != 0, "answer: zero family period")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text())
        source = json.loads(args.input.read_text()) if args.input else None
        errors = validate(record, source)
    except (OSError, ValueError):
        errors = ["Cannot read a valid UTF-8 JSON record"]
    print("\n".join(errors) if errors else "VALID: structure and cross-references only")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
