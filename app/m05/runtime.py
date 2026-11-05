"""No model client, network, automatic retry, generated code or credential access.

The compact output is a producer projection of M01, not a replacement contract.
M04 adds provenance and validates the full result with its exact input. A JSON
schema establishes shape, never the mathematical truth or visual transcription.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

VERSION = "m05-prompts-1.1.0"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
LIMIT = 512 * 1024  # Local rejection bound, not a provider output-token promise.
CONTEXT_KEYS = {"units", "corrections", "interpretations", "branches", "assumptions",
                "relations", "facts", "coverage", "issues"}


class PromptError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def freeze_identity():
    """Identity includes code, instruction/schema bytes and frozen upstream data."""
    manifest = read_json(ROOT / "planning/m05/manifest.json")
    for relative, expected in manifest["files"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise PromptError("package_drift")
    return digest(manifest)


def response_schema(stage):
    if stage not in {"read", "assess"}:
        raise PromptError("invalid_stage")
    return read_json(HERE / f"{stage}.schema.json")


def decode_response(value):
    """Reject duplicate keys, fences, non-finite numbers and excessive output."""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise PromptError("duplicate_json_key")
            result[key] = item
        return result
    try:
        raw = value if isinstance(value, str) else canonical(value)
        if len(raw.encode()) > LIMIT:
            raise PromptError("response_too_large")
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(PromptError("non_finite_json")))
        if not isinstance(result, dict):
            raise PromptError("response_not_object")
        return result
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PromptError):
            raise
        raise PromptError("invalid_json") from exc


def validate_response(stage, value, request):
    result = decode_response(value)
    if list(Draft202012Validator(response_schema(stage)).iter_errors(result)):
        raise PromptError("response_schema")
    context = request.get("context", {})
    previous_ids = {x["id"] for key in CONTEXT_KEYS for x in context.get(key, []) if "id" in x}
    new_ids = []
    for section, values in result.items():
        if isinstance(values, list):
            new_ids.extend(x["id"] for x in values if isinstance(x, dict) and "id" in x)
    if len(new_ids) != len(set(new_ids)) or set(new_ids) & previous_ids:
        raise PromptError("duplicate_or_existing_id")
    if stage == "read":
        page_id = request["page"]["id"]
        if result["coverage"]["page_id"] != page_id:
            raise PromptError("coverage_page_mismatch")
        for unit in result["units"]:
            if any(a["page_id"] != page_id for a in unit["anchors"]):
                raise PromptError("anchor_page_mismatch")
            if unit["kind"] != "diagram" and unit["reading_status"] == "clear" and not unit["literal"]:
                raise PromptError("empty_clear_reading")
            if unit["kind"] == "diagram" and unit["literal"] is not None:
                raise PromptError("diagram_literal")
        if any(b["id"] == "main" for b in result.get("branches", [])):
            raise PromptError("reserved_branch")
        if any(a["origin"] == "hypothesis" and a["status"] == "established"
               for a in result.get("assumptions", [])):
            raise PromptError("established_hypothesis")
    else:
        score = result["score"]
        if (score["status"] == "withheld") != (score["value"] is None):
            raise PromptError("score_state")
        if score["value"] is not None:
            if not score["criterion_ids"] or not score["requirement_ids"]:
                raise PromptError("score_without_requirements")
            supplied = {c["id"] for c in request.get("criteria", [])}
            if not set(score["criterion_ids"]) <= supplied:
                raise PromptError("unknown_criterion")
        for finding in result["findings"]:
            if finding["category"] == "system_uncertainty" and finding["affects_score"] == "yes":
                raise PromptError("uncertainty_is_not_penalty")
    return result


def _methodology(task_type):
    if task_type == "trigonometric_equation":
        rules = read_json(ROOT / "planning/m02/rules.json")
        return dict(version=rules["package_version"], rules=[{k: r[k] for k in ("id", "rule", "authority")} for r in rules["rules"]], warnings=rules["warnings"],
                    requirements=read_json(ROOT / "planning/m02/requirements.json"))
    rules = read_json(ROOT / "planning/m03/rules.json")
    return {k: rules[k] for k in ("version", "checks", "requirements_catalogue", "decision_policy", "feedback_policy")}


def rubric(task_type):
    if task_type == "trigonometric_equation":
        return read_json(ROOT / "planning/m02/criteria.json")
    if task_type == "inequality":
        return read_json(ROOT / "planning/m03/rules.json")["criteria"]
    raise PromptError("unsupported_task")


def selected_modules(task_type, features=()):
    routing = read_json(ROOT / "planning/k05/routing.json")
    vocabulary = {v["id"] for v in routing["feature_vocabulary"]}
    features = set(features)
    # The general fallback is always present; a bad early label cannot remove it.
    chosen = set(routing["mandatory_core"]) | {"k05.core.general_method"}
    features.add("trigonometry" if task_type == "trigonometric_equation" else "inequality")
    if task_type == "trigonometric_equation":
        features.add("explicit_interval")
    for rule in routing["rules"]:
        if features & set(rule["when"].get("any_feature", [])):
            chosen.update(rule["container_ids"])
    catalog = read_json(ROOT / "planning/k05/catalog.json")["mechanisms"]
    mechanisms = [{k: m[k] for k in ("id", "container_id", "title", "classification", "claim",
                                    "evidence_requirements", "classification_guard", "correct_contrast")}
                  for m in catalog if m["container_id"] in chosen]
    for projected, original in zip(mechanisms, (m for m in catalog if m["container_id"] in chosen)):
        projected["limits"] = ("Спорное влияние ошибки на балл требует методического основания. Балл сходной работы не переносится."
                               if "R11" in original["limits"] else original["limits"])
    return dict(containers=sorted(chosen), mechanisms=mechanisms,
                unknown_features=sorted(features - vocabulary), executable_requests=[],
                limits="Это инструкции и кандидаты проверок; K03 ещё не строит запросы по этому каталогу.")


def candidate_cases(task_type, case_ids):
    if task_type == "trigonometric_equation":
        cases = read_json(ROOT / "planning/m02/case_rules.json")["cases"]
        fields = ("id", "case_id", "pdf_pages", "required_matches", "excluding_features", "contrast_ref", "project_rule", "unknown_policy")
    else:
        raw = read_json(ROOT / "planning/m03/cases.json")
        cases = raw["cases"] if isinstance(raw, dict) else raw
        fields = ("case_id", "pdf_pages", "mechanism", "necessary_conditions", "excluding_signs", "contrast_case_id", "application")
    index = {c["case_id"]: c for c in cases}
    if not set(case_ids) <= set(index):
        raise PromptError("unknown_methodology_case")
    chosen = set(case_ids)
    for identity in case_ids:
        contrast = index[identity].get("contrast_ref", index[identity].get("contrast_case_id"))
        if contrast in index:
            chosen.add(contrast)
    source = read_json(ROOT / "planning/m02/sources.json")["sources"][0]
    result = []
    for identity in sorted(chosen):
        case = index[identity]
        item = {k: case[k] for k in fields if k in case}
        item.update(source_ref=case.get("source_ref", source["path"]),
                    source_sha256=case.get("source_sha256", source["sha256"]), exam_year=2026,
                    locator="ФИПИ " + identity + "; PDF pages " + ",".join(map(str, case["pdf_pages"])))
        result.append(item)
    return result


def build_request(stage, request, *, features=(), case_ids=(), reference_key=None, image_manifest=()):
    """Common deterministic builder for local M04 and L04. Provider settings stay outside."""
    identity = freeze_identity()
    response_schema(stage)
    allowed = ({"task", "page", "task_pages", "context", "rules", "synthetic"} if stage == "read" else
               {"task", "capture", "context", "criteria", "plan", "rules", "synthetic"})
    if set(request) - allowed or not {"task", "context"} <= set(request):
        raise PromptError("request_fields")
    task_type = request["task"].get("task_type")
    if task_type not in {"trigonometric_equation", "inequality"}:
        raise PromptError("unsupported_task")
    if request["task"].get("exam_year") != 2026 or request["task"].get("exam_number") != (13 if task_type == "trigonometric_equation" else 15):
        raise PromptError("task_number_or_year")
    if set(request["context"]) - CONTEXT_KEYS:
        raise PromptError("context_fields")
    if stage == "read" and (reference_key is not None or case_ids):
        raise PromptError("reading_reference_forbidden")
    if reference_key is not None:
        required = {"version", "statement_sha256", "domain", "answer", "derivation", "authority"}
        if (set(reference_key) != required or reference_key["statement_sha256"] != digest(request["task"]["statement"]["text"])
                or reference_key["authority"] != "agent_mathematical_review"):
            raise PromptError("reference_key_fields_or_task")
    instruction = read_json(HERE / "instructions.json")
    paragraphs = instruction["common"] + instruction[stage] + instruction[task_type]
    observed = tuple(features) + tuple(request.get("plan", {}).get("features", []))
    if task_type == "trigonometric_equation" or "circle" in observed:
        paragraphs += instruction["circle"]
    # Only task content enters the model; source labels and evaluation datasets do not.
    task = {k: deepcopy(request["task"][k]) for k in ("exam_year", "exam_number", "task_type")}
    statement = request["task"]["statement"]
    task["statement"] = {k: deepcopy(statement[k]) for k in ("text", "format", "completeness", "missing")}
    data = dict(task=task, context=deepcopy(request["context"]), images=deepcopy(list(image_manifest)))
    if stage == "read":
        data.update(page=deepcopy(request["page"]), task_pages=deepcopy(request.get("task_pages", [])))
    else:
        data.update(capture=deepcopy(request.get("capture", {})), criteria=deepcopy(request.get("criteria", [])),
                    methodology=_methodology(task_type), modules=selected_modules(task_type, observed),
                    candidate_cases=candidate_cases(task_type, case_ids), reference_key=deepcopy(reference_key),
                    check_outcomes=deepcopy(request.get("plan", {}).get("check_outcomes", [])))
    payload = dict(version=VERSION, stage=stage, package_sha256=identity, system="\n\n".join(paragraphs),
                   data=data, response_schema=response_schema(stage))
    return dict(payload=payload, payload_sha256=digest(payload), content_bytes=len(canonical(payload).encode()),
                network_enabled=False)


class PreparedFixtureAdapter:
    """Only explicitly synthetic, task/image-bound saved responses; never OCR.

    Requests are retained for local inspection. No callable transport can be
    smuggled in: fixtures are copied JSON and hashes bind every page's bytes.
    Full M01 semantic validation/publication remains in the existing Pipeline.
    """
    id = "m05-prepared-fixture"
    version = VERSION
    network_enabled = False

    def __init__(self, *, statement, image_hashes, readings, assessment):
        self._statement = statement
        self._image_hashes = deepcopy(image_hashes)
        self._readings = deepcopy(readings)
        self._assessment = deepcopy(assessment)
        self._settings = dict(package_sha256=freeze_identity(), diagnostic_only=True,
                             fixture_sha256=digest(dict(statement=statement, images=image_hashes,
                                                        readings=readings, assessment=assessment)))
        self.requests = []

    @property
    def settings(self):
        return deepcopy(self._settings)

    def _guard(self, request):
        current = digest(dict(statement=self._statement, images=self._image_hashes,
                              readings=self._readings, assessment=self._assessment))
        if current != self._settings["fixture_sha256"]:
            raise PromptError("fixture_drift")
        if not request.get("synthetic"):
            raise PromptError("synthetic_fixture_only")
        if request["task"]["statement"]["text"] != self._statement:
            raise PromptError("fixture_statement_mismatch")

    def read(self, request, images):
        self._guard(request)
        hashes = {k: hashlib.sha256(v).hexdigest() for k, v in images.items()}
        expected = {p["asset_id"]: p["sha256"] for p in request.get("task_pages", []) + [request["page"]]}
        if hashes != expected or any(self._image_hashes.get(k) != v for k, v in hashes.items()):
            raise PromptError("fixture_image_mismatch")
        self.requests.append(build_request("read", request, image_manifest=[dict(asset_id=k, sha256=v) for k, v in sorted(hashes.items())]))
        return validate_response("read", self._readings[request["page"]["id"]], request)

    def assess(self, request):
        self._guard(request)
        self.requests.append(build_request("assess", request))
        return validate_response("assess", self._assessment, request)
