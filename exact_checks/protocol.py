"""Executor of the accepted K01 protocol 1.0.0 for the eight M06/M07 operations.

Order of work for one request, each step able to stop with an honest `unknown`:
size limit → known operation/version → K01 JSON Schema → premise gate against the
current M01 result (stale snapshot, inactive branch, unresolved premise, unbound
or unreadable source) → exact computation → output limits → self-check of the
response against the K01 schema.

`verified` / `contradicted` speak only about the operation's own claim on the
supplied arguments. Whether that is a student error, a missing step or affects
the score is decided by other layers (M02/M03/assessment). `unknown` never means
"wrong" or "zero points".

The K01 schema and registry are read from planning/k01 and pinned by SHA-256: if
the accepted specification changes, this module refuses to load instead of
silently computing under outdated rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import time

from jsonschema import Draft202012Validator

from contracts.ege_v1 import validate as m01

from . import rational as R
from . import sets as S
from . import trig as T
from .rational import CheckError, InvalidInput, ResourceLimit, show

TOOL_VERSION = "exact-checks-1.1.0"
PROTOCOL_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "planning" / "k01"
PINNED = {
    "protocol.schema.json": "d7cb471e9fc2138cfa43f8676970ea1e38267a30e9bb70fc7cf25016eafccdc2",
    "registry.json": "23002aa8ada95f1768dce07c162fdb822270cf7bfba63d5fc87f14036e56ca02",
}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _load(name):
    data = (SPEC / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != PINNED[name]:
        raise RuntimeError(f"K01 {name} differs from the version M06/M07 implement; review before use.")
    return json.loads(data)


SCHEMA = _load("protocol.schema.json")
REGISTRY = _load("registry.json")
LIMITS = REGISTRY["limits"]
OPERATIONS = {op["id"]: op for op in REGISTRY["operations"]}

if hashlib.sha256((ROOT / "contracts/ege_v1/schema.json").read_bytes()).hexdigest() != REGISTRY["m01_schema_sha256"]:
    raise RuntimeError("M01 schema differs from the version K01 1.0.0 was specified against.")
# The numeric constants of the math modules must be the registry values, not a private copy.
assert (R.INPUT_DIGITS, R.OUTPUT_DIGITS, R.INTERMEDIATE_BITS, T.CANDIDATE_LIMIT) == (
    LIMITS["integer_input_digits"], LIMITS["integer_output_digits"],
    LIMITS["intermediate_integer_bits"], LIMITS["enumerated_root_candidates_before_dedup"])


def _validator(name):
    return Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": SCHEMA["$defs"]})


REQUEST_SCHEMA = _validator("request")
RESPONSE_SCHEMA = _validator("response")
CONTEXT_SCHEMA = _validator("context")
ARGUMENT_SCHEMAS = {op: _validator(spec["arguments_ref"].rsplit("/", 1)[1]) for op, spec in OPERATIONS.items()}


def size(value) -> int:
    """Byte size as measured by the K01 verifier (UTF-8 JSON, default separators)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class ContextError(ValueError):
    """The context cannot be attached to the current M01 result at all."""


@dataclass
class Outcome:
    truth: bool
    computed: dict
    summary: str
    evidence: list


# ---------------------------------------------------------------- premise gate

def lineage(branches, branch_id):
    seen = []
    while branch_id in branches and branch_id not in seen:
        seen.append(branch_id)
        branch_id = branches[branch_id]["parent_id"]
    return seen


def check_context(context, result):
    """Structural attachability, mirroring the M01 rules that would reject a fact."""
    errors = sorted({"/".join(map(str, e.absolute_path)) or "context" for e in CONTEXT_SCHEMA.iter_errors(context)})
    if errors:
        raise ContextError("Контекст не соответствует K01: " + ", ".join(errors[:10]))
    by = {key: {x["id"]: x for x in result.get(key, [])} for key in ("units", "interpretations", "assumptions", "facts", "branches")}
    branches = by["branches"]
    if context["branch_id"] not in branches:
        raise ContextError("Ветвь контекста отсутствует в результате.")
    for key, section in (("unit_ids", "units"), ("context_unit_ids", "units"), ("interpretation_ids", "interpretations"),
                         ("assumption_ids", "assumptions"), ("depends_on_fact_ids", "facts")):
        values = context[key]
        if len(values) != len(set(values)):
            raise ContextError(f"Повторяющиеся ссылки в {key}.")
        missing = [v for v in values if v not in by[section]]
        if missing:
            raise ContextError(f"Ссылки {key} отсутствуют в результате.")
    permitted = lineage(branches, context["branch_id"])
    declared = {aid for bid in permitted for aid in branches[bid]["assumption_ids"]}
    for aid in context["assumption_ids"]:
        if by["assumptions"][aid]["branch_id"] not in permitted or aid not in declared:
            raise ContextError("Предпосылка не принадлежит ветви контекста.")
    return by, permitted


def _resolve(pointer, document):
    value = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(value):
            value = value[int(token)]
        else:
            raise KeyError(pointer)
    return value


class _Provenance:
    """Only the request's reachable evidence, including semantic context and premises.

    A branch join keeps its child evidence in child scope: flattening its local
    assumptions into a parent fact would undo the join and violate M01.
    """

    SECTIONS = {"unit": "units", "interpretation": "interpretations",
                "assumption": "assumptions", "fact": "facts"}

    def __init__(self, context, result):
        self.by, self.permitted = check_context(context, result)
        self.context, self.result = context, result
        self.edges, self.nodes, self.proof_nodes = {}, {}, {}
        self.roots = []
        for key, kind in (("unit_ids", "unit"), ("context_unit_ids", "unit"),
                          ("interpretation_ids", "interpretation"),
                          ("assumption_ids", "assumption"), ("depends_on_fact_ids", "fact")):
            self.roots.extend((kind, ref) for ref in context[key])
        self.roots.extend(("assumption", aid) for aid in self.branch_assumptions(context["branch_id"]))
        # Iterative DFS both bounds stack use and rejects cycles crossing object types.
        done, active = set(), set()
        for root in self.roots:
            pending = [(root, False)]
            while pending:
                node, leaving = pending.pop()
                if leaving:
                    active.remove(node)
                    done.add(node)
                elif node not in done:
                    if node in active:
                        raise ContextError("Цикл в происхождении источников запроса.")
                    active.add(node)
                    self.edges[node] = self.children(node)
                    pending.append((node, True))
                    pending.extend((child, False) for child in reversed(self.edges[node]))

    def branch_assumptions(self, branch_id):
        branches = self.by["branches"]
        permitted, seen = [], set()
        current = branch_id
        while current is not None:
            if current in seen or current not in branches:
                raise ContextError("Повреждено происхождение ветви контекста.")
            seen.add(current)
            permitted.extend(branches[current]["assumption_ids"])
            current = branches[current]["parent_id"]
        return permitted

    def established_proof_nodes(self, assumption):
        """Explicit proof ancestry, without inventing reverse scope dependencies."""
        aid = assumption["id"]
        if aid not in self.proof_nodes:
            source_kind = "fact" if assumption["origin"] == "derived_fact" else "interpretation"
            seen, pending = set(), [(source_kind, assumption["source_ref"])]
            while pending:
                node = pending.pop()
                if node in seen:
                    continue
                seen.add(node)
                kind, ref = node
                item = self.by[self.SECTIONS[kind]].get(ref, {})
                if kind in ("fact", "interpretation"):
                    pending.extend(("assumption", value) for value in item.get("assumption_ids", []))
                if kind == "fact":
                    pending.extend(("fact", value) for value in item.get("depends_on_fact_ids", []))
                    pending.extend(("interpretation", value) for value in item.get("interpretation_ids", []))
                if kind == "assumption":
                    source_kind = {"derived_fact": "fact", "human_interpretation": "interpretation"}.get(item.get("origin"))
                    if source_kind:
                        pending.append((source_kind, item["source_ref"]))
            self.proof_nodes[aid] = seen
        return self.proof_nodes[aid]

    def children(self, node):
        kind, ref = node
        item = self.by[self.SECTIONS[kind]].get(ref)
        if item is None:
            raise ContextError(f"Источник {kind}:{ref[:120]} отсутствует в результате.")
        self.nodes[node] = item
        if kind == "unit":
            return []
        branch = item["branch_id"]
        ancestors = lineage(self.by["branches"], branch)
        if not ancestors:
            raise ContextError("Ветвь источника отсутствует в результате.")
        if kind == "assumption":
            if ref not in self.branch_assumptions(branch):
                raise ContextError("Предпосылка источника не объявлена в его ветви.")
            source_kind = {"student_unit": "unit", "derived_fact": "fact",
                           "human_interpretation": "interpretation"}.get(item["origin"])
            if source_kind is None:
                return []
            child = (source_kind, item["source_ref"])
            source = self.by[self.SECTIONS[source_kind]].get(child[1])
            if source is None:
                raise ContextError("Источник предпосылки отсутствует в результате.")
            if source_kind != "unit" and source["branch_id"] not in ancestors:
                raise ContextError("Источник предпосылки относится к посторонней ветви.")
            return [child]
        children = [("unit", uid) for uid in item["unit_ids"] + item["context_unit_ids"]]
        assumptions = set(item["assumption_ids"]) | set(self.branch_assumptions(branch))
        for aid in sorted(assumptions):
            assumption = self.by["assumptions"].get(aid)
            if assumption is None or assumption["branch_id"] not in ancestors:
                raise ContextError("Предпосылка источника отсутствует или относится к посторонней ветви.")
            # An established premise cites an existing explicit proof. Merely
            # sharing its branch must not invent reverse edges into that proof.
            # Explicit premises (and unresolved/conditional ones) are not exempt.
            if (aid not in item["assumption_ids"]
                    and assumption["status"] == "established"
                    and assumption["origin"] in ("derived_fact", "human_interpretation")
                    and node in self.established_proof_nodes(assumption)):
                continue
            children.append(("assumption", aid))
        if kind == "fact":
            joins = set(item.get("branch_join", {}).get("branch_ids", []))
            if joins:
                join = item["branch_join"]
                coverage = self.by["facts"].get(join["coverage_fact_id"], {})
                if (join["coverage_fact_id"] not in item["depends_on_fact_ids"] or
                        coverage.get("branch_id") != branch or
                        any(self.by["branches"].get(bid, {}).get("parent_id") != branch for bid in joins) or
                        not joins <= {self.by["facts"].get(fid, {}).get("branch_id")
                                      for fid in item["depends_on_fact_ids"]}):
                    raise ContextError("Повреждено обоснование объединения ветвей.")
            for key, child_kind in (("interpretation_ids", "interpretation"), ("depends_on_fact_ids", "fact")):
                for child_ref in item[key]:
                    child = self.by[self.SECTIONS[child_kind]].get(child_ref)
                    allowed = set(ancestors) | (joins if child_kind == "fact" else set())
                    if child is None or child["branch_id"] not in allowed:
                        raise ContextError("Источник факта отсутствует или относится к посторонней ветви.")
                    children.append((child_kind, child_ref))
        return list(dict.fromkeys(children))

    def walk(self):
        """Yield each reachable source with the scope in which it is used."""
        seen, pending = set(), [(root, self.context["branch_id"]) for root in reversed(self.roots)]
        while pending:
            node, scope = pending.pop()
            item = self.nodes[node]
            if node[0] != "unit" and item["branch_id"] not in lineage(self.by["branches"], scope):
                # Only checked branch_join dependency edges may enter a child scope.
                scope = item["branch_id"]
            key = (node, scope)
            if key in seen:
                continue
            seen.add(key)
            yield node, scope
            pending.extend((child, scope) for child in reversed(self.edges[node]))

    def underlying(self, sources):
        seen, pending = set(), list(sources)
        while pending:
            node = pending.pop()
            if node not in seen:
                seen.add(node)
                pending.extend(self.edges.get(node, []))
        # Shared branch-condition identifiers are bookkeeping, not necessarily
        # shared evidence. Their actual unit/interpretation/fact sources remain.
        return {node for node in seen if node[0] != "assumption"}


def effective_context(context, result):
    """Copy K01 context with all reachable same-scope dependencies made explicit.

    Unknown premises remain ordinary premises, so the executor can publish an
    honest unknown fact. Broken references/cycles raise ContextError instead.
    Conditions inside a valid child-case join remain on its source facts.
    """
    graph = _Provenance(context, result)
    output = {key: list(value) if isinstance(value, list) else value for key, value in context.items()}
    fields = {"unit": "context_unit_ids", "interpretation": "interpretation_ids",
              "assumption": "assumption_ids", "fact": "depends_on_fact_ids"}
    for node, scope in graph.walk():
        kind, ref = node
        if scope != context["branch_id"]:
            continue
        if kind != "unit" and graph.nodes[node]["branch_id"] not in graph.permitted:
            continue
        if kind == "unit" and ref in output["unit_ids"]:
            continue
        field = fields[kind]
        if ref not in output[field]:
            output[field].append(ref)
    return output


def _argument_leaves(value, schema, pointer=""):
    """Value-bearing leaves; schema constants are types, not student claims.

    A whole-object binding covers its descendants. Empty containers and null
    endpoints themselves carry information and therefore still need a source.
    """
    while "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    if "anyOf" in schema:
        choices = [part for part in schema["anyOf"] if not list(
            Draft202012Validator({**part, "$defs": SCHEMA["$defs"]}).iter_errors(value))]
        if len(choices) == 1:
            yield from _argument_leaves(value, choices[0], pointer)
            return
    if "const" in schema:
        return
    if isinstance(value, dict) and value:
        for key, item in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _argument_leaves(item, schema.get("properties", {}).get(key, {}), pointer + "/" + token)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _argument_leaves(item, schema.get("items", {}), pointer + "/" + str(index))
    else:
        yield pointer


def _source_content(value):
    """A defined source value; empty metadata cannot ground an argument."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_source_content(v) for v in value.values())
    if isinstance(value, list):
        return any(_source_content(v) for v in value)
    return True


def premise_gate(request, result, *, source_input=None):
    """Return (reason_code, explanation) when no exact verdict may be given, else None."""
    if request["input_ref"] != result.get("input_ref") or request["reading_sha256"] != m01.reading_digest(result):
        return "stale_input", "Запрос относится к другому снимку входа или чтения."
    context = request["context"]
    try:
        graph = _Provenance(context, result)
    except ContextError as exc:
        return "invalid_input", str(exc)
    by, permitted = graph.by, graph.permitted
    if any(by["branches"][bid]["status"] != "active" for bid in permitted):
        return "unresolved_premise", "Ветвь контекста неактивна (superseded)."
    for kind, ids in (("interpretation", context["interpretation_ids"]), ("fact", context["depends_on_fact_ids"])):
        if any(by[graph.SECTIONS[kind]][ref]["branch_id"] not in permitted for ref in ids):
            return "unresolved_premise", "Источник не установлен в этой ветви."
    reachable = list(graph.walk())
    assumptions = [item for (kind, _), item in graph.nodes.items() if kind == "assumption"]
    for (kind, ref), scope in sorted(reachable, key=lambda entry: entry[0][0] == "unit"):
        item = graph.nodes[(kind, ref)]
        if any(by["branches"][bid]["status"] != "active" for bid in lineage(by["branches"], scope)):
            return "unresolved_premise", "Зависимый источник относится к неактивной ветви."
        if kind == "assumption" and item["status"] == "unknown":
            return "unresolved_premise", f"Предпосылка {ref} не установлена: «{item['claim'][:300]}»."
        if kind == "fact":
            if item["status"] != "supported":
                return "unresolved_premise", f"Зависимый факт {ref} не подтверждён."
            if item["basis_sha256"] != request["reading_sha256"]:
                return "stale_input", f"Зависимый факт {ref} относится к другому снимку чтения."
        if kind == "unit" and item["reading_status"] != "clear":
            scoped = set(lineage(by["branches"], scope))
            permitted_reading = any(a["origin"] == "student_unit" and a["source_ref"] == ref
                                    and a["status"] in ("established", "conditional")
                                    and a["branch_id"] in scoped for a in assumptions)
            if not permitted_reading:
                return "unresolved_reading", f"Чтение существенной записи {ref[:120]} неоднозначно; нет условной ветви для выбранного чтения."
    arguments, bound = request["arguments"], {}
    for binding in context["bindings"]:
        pointer = binding["argument_pointer"]
        try:
            _resolve(pointer, arguments)
        except KeyError:
            return "source_mismatch", f"Привязка {pointer[:200]} не указывает на аргумент запроса."
        top = pointer.split("/")[1].replace("~1", "/").replace("~0", "~")
        bound.setdefault(top, set()).add((binding["source_kind"], binding["source_ref"]))
    schema = SCHEMA["$defs"][OPERATIONS[request["operation"]]["arguments_ref"].rsplit("/", 1)[1]]
    pointers = {binding["argument_pointer"] for binding in context["bindings"]}
    unbound = [leaf for leaf in _argument_leaves(arguments, schema)
               if not any(leaf == pointer or leaf.startswith(pointer + "/") for pointer in pointers)]
    if unbound:
        return "source_mismatch", "Значимые части аргументов без источника: " + ", ".join(unbound[:10]) + "."
    listed_units = set(context["unit_ids"]) | set(context["context_unit_ids"])
    sources = {source for values in bound.values() for source in values}
    input_sources = {ref for kind, ref in sources if kind == "input_pointer"}
    if input_sources:
        # The snapshot is supplied by trusted calling code, never by a model's
        # operation request. A result hash alone cannot resolve a JSON Pointer.
        if not isinstance(source_input, dict) or m01.validate(source_input):
            return "source_mismatch", "Для источника условия нужен проверенный снимок входа M01."
        identity = {"id": source_input["id"], "revision": source_input["revision"],
                    "sha256": m01.digest(source_input)}
        if identity != result.get("input_ref"):
            return "stale_input", "Источник условия относится к другому снимку входа."
    for kind, ref in sorted(sources):
        if kind == "unit" and (ref not in by["units"] or ref not in listed_units):
            return "source_mismatch", f"Запись {ref[:120]} не входит в контекст факта."
        if kind == "interpretation" and (ref not in by["interpretations"] or ref not in context["interpretation_ids"]):
            return "source_mismatch", f"Интерпретация {ref[:120]} не входит в контекст ветви."
        if kind == "fact" and (ref not in by["facts"] or ref not in context["depends_on_fact_ids"]):
            return "source_mismatch", f"Факт-источник {ref[:120]} не объявлен зависимостью этой ветви."
        if kind == "input_pointer":
            if not ref.startswith("/task/") or re.search(r"~(?![01])", ref):
                return "source_mismatch", "Из входа допускаются только корректные указатели условия задачи (/task/...)."
            try:
                source_value = _resolve(ref, source_input)
            except KeyError:
                return "source_mismatch", "Указанный источник отсутствует в снимке условия."
            if not _source_content(source_value):
                return "source_mismatch", "Указанный источник условия не содержит данных."
    if request["operation"] == "m06.circle_point_equal":
        if graph.underlying(bound["angle"]) & graph.underlying(bound["independent_point_angle"]):
            return "source_mismatch", "Точка окружности должна быть установлена независимо от проверяемой подписи."
    return None


# ------------------------------------------------------------ text helpers

def _cut(text, limit=2000):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _values(values, basis, limit=1500):
    parts, length = [], 0
    for index, value in enumerate(values):
        part = show(value, basis)
        if length + len(part) > limit:
            parts.append(f"… ещё {len(values) - index}")
            break
        parts.append(part)
        length += len(part) + 2
    return "{" + "; ".join(parts) + "}" if parts else "∅"


def _pi_interval_text(i: T.PiInterval):
    return ("[" if i.left_closed else "(") + show(i.left, "pi") + "; " + show(i.right, "pi") + ("]" if i.right_closed else ")")


def _interval_text(i: S.Interval):
    left = "−∞" if i.left is None else show(i.left)
    right = "+∞" if i.right is None else show(i.right)
    return ("[" if i.left_closed else "(") + left + "; " + right + ("]" if i.right_closed else ")")


def set_text(value: S.ExactSet, limit=900):
    parts = [_interval_text(i) for i in value.intervals]
    if value.points:
        parts.append(_values(sorted(value.points), "rational", 400))
    text = " ∪ ".join(parts) if parts else "∅"
    return _cut(text, limit)


def _family_text(offset, period):
    if period == 0:
        return f"x = {show(offset, 'pi')} (b = 0)"
    sign = "+" if period > 0 else "−"
    step = abs(period)
    step_text = "k" if step == 1 else f"{show(step)}k"
    return f"x = π({show(offset)} {sign} {step_text}), k ∈ ℤ"


def polynomial_text(coefficients, variable):
    powers = "⁰¹²³⁴⁵⁶⁷⁸⁹"
    terms = []
    for degree in range(len(coefficients) - 1, -1, -1):
        c = coefficients[degree]
        if c == 0:
            continue
        sign = "−" if c < 0 else "+"
        magnitude = abs(c)
        if degree == 0:
            body = show(magnitude)
        else:
            body = ("" if magnitude == 1 else show(magnitude)) + variable
            if degree > 1:
                body += "".join(powers[int(d)] for d in str(degree))
        terms.append((sign, body))
    if not terms:
        return "0"
    first_sign, first = terms[0]
    text = ("−" if first_sign == "−" else "") + first
    for sign, body in terms[1:]:
        text += f" {sign} {body}"
    return _cut(text, 600)


# --------------------------------------------------------------- argument parsing

def _pi(value):
    return R.parse(value["coefficient"])


def _pi_interval(value):
    return T.PiInterval(_pi(value["left"]), _pi(value["right"]), value["left_closed"], value["right_closed"])


def _real(value):
    return None if value is None else R.parse(value["coefficient"])


def _set(value):
    intervals = tuple(S.Interval(_real(i["left"]), _real(i["right"]), i["left_closed"], i["right_closed"])
                      for i in value["intervals"])
    return S.ExactSet(intervals, tuple(R.parse(p["coefficient"]) for p in value["points"]))


def _encode_set(value: S.ExactSet):
    def end(v):
        return None if v is None else R.exact(v, "rational")
    return {"intervals": [{"left": end(i.left), "right": end(i.right), "left_closed": i.left_closed,
                           "right_closed": i.right_closed} for i in value.intervals],
            "points": [R.exact(p, "rational") for p in sorted(value.points)]}


def _input_components(sets):
    total = sum(len(s["intervals"]) + len(s["points"]) for s in sets)
    if total > LIMITS["total_input_set_components"]:
        raise ResourceLimit(f"Во входных множествах {total} компонентов, предел {LIMITS['total_input_set_components']}.")


def _output_components(*values):
    total = sum(v.size for v in values)
    if total > LIMITS["output_set_components"]:
        raise ResourceLimit(f"В точном ответе {total} компонентов, предел {LIMITS['output_set_components']}.")


# ------------------------------------------------------------------ operations

SCOPE_M06 = "Проверено только это точное утверждение на переданных числах; чтение записи, вывод семейств и обоснование отбора не проверялись."
SCOPE_M07 = "Проверено только это точное утверждение на переданных множествах; переменная множеств, ОДЗ целиком и равносильность переходов не проверялись."


def op_pi_interval_membership(args) -> Outcome:
    point, interval = _pi(args["point"]), _pi_interval(args["interval"])
    member = interval.contains(point)
    expected = args["expected_member"]
    sign = "∈" if member else "∉"
    if point == interval.left == interval.right:
        detail = ("Точка совпадает с обоими концами: левый "
                  + ("включён" if interval.left_closed else "не включён") + ", правый "
                  + ("включён" if interval.right_closed else "не включён")
                  + ("; промежуток состоит из этой точки." if member else "; промежуток пуст."))
    elif point == interval.left or point == interval.right:
        end = "левым" if point == interval.left else "правым"
        closed = interval.left_closed if point == interval.left else interval.right_closed
        detail = f"Точка совпадает с {end} концом, который {'включён' if closed else 'не включён'}."
    else:
        detail = f"Сравнение коэффициентов при π (π > 0): {show(interval.left)} {'<' if point > interval.left else '>'} {show(point)} и {show(point)} {'<' if point < interval.right else '>'} {show(interval.right)}."
    summary = f"{show(point, 'pi')} {sign} {_pi_interval_text(interval)}; заявлено {'∈' if expected else '∉'}."
    return Outcome(member == expected, {"member": member}, summary, [detail, SCOPE_M06])


def op_select_pi_roots(args) -> Outcome:
    families = [(R.parse(f["offset"]), R.parse(f["period"])) for f in args["families"]]
    interval = _pi_interval(args["interval"])
    claimed = [_pi(v) for v in args["claimed_roots"]]
    selection = T.select_roots(families, interval, claimed)
    computed = {"selected_roots": [R.exact(v, "pi") for v in selection.selected],
                "missing_roots": [R.exact(v, "pi") for v in selection.missing],
                "extra_roots": [R.exact(v, "pi") for v in selection.extra],
                "duplicate_claim_count": selection.duplicate_claims}
    evidence = ["Семейства объединяются (логическое «или»); каждое со своим целым k."]
    for offset, period, first, last in selection.per_family:
        if period == 0:
            inside = interval.contains(offset)
            evidence.append(f"{_family_text(offset, period)}: при любом k одно значение; {'входит' if inside else 'не входит'} в {_pi_interval_text(interval)}.")
        elif last < first:
            evidence.append(f"{_family_text(offset, period)}: на {_pi_interval_text(interval)} нет ни одного целого k.")
        else:
            evidence.append(f"{_family_text(offset, period)}: k от {first} до {last} ({last - first + 1} знач.).")
    evidence.append("Отбор: " + _values(selection.selected, "pi"))
    if selection.missing:
        evidence.append("Пропущены: " + _values(selection.missing, "pi"))
    if selection.extra:
        evidence.append("Лишние (вне промежутка или не из семейств): " + _values(selection.extra, "pi"))
    if selection.duplicate_claims:
        evidence.append(f"Повторов в заявленном списке: {selection.duplicate_claims}; как множество список от этого не меняется.")
    evidence.append(SCOPE_M06)
    if selection.matches:
        summary = f"Заявленный список совпадает с отбором из семейств на {_pi_interval_text(interval)}."
    else:
        summary = (f"Заявленный список не совпадает с отбором на {_pi_interval_text(interval)}: "
                   f"пропущено {len(selection.missing)}, лишних {len(selection.extra)}.")
    return Outcome(selection.matches, computed, summary, evidence)


def op_circle_point_equal(args) -> Outcome:
    angle, point = _pi(args["angle"]), _pi(args["independent_point_angle"])
    c = T.circle_points(angle, point)
    computed = {"same_point": c.same_point, "same_real_number": c.same_real_number,
                "difference_over_two_pi": R.encode(c.turns),
                "angle_residue": R.exact(c.angle_residue, "pi"), "point_residue": R.exact(c.point_residue, "pi")}
    turns = f"({show(angle)} − {show(point)})/2 = {show(c.turns)}"
    if c.same_point:
        summary = f"{show(angle, 'pi')} и {show(point, 'pi')} — одна точка окружности: {turns} — целое число оборотов."
    else:
        summary = f"{show(angle, 'pi')} и {show(point, 'pi')} — разные точки окружности: {turns} не целое."
    evidence = [summary,
                f"Остатки по модулю 2π в [0; 2π): {show(c.angle_residue, 'pi')} и {show(c.point_residue, 'pi')}.",
                "Как действительные числа углы " + ("равны." if c.same_real_number else "различны: одна точка окружности не означает один корень на промежутке."),
                "Точный угол точки взят из независимого источника; положение на фото и сама проверяемая подпись им не служат.",
                SCOPE_M06]
    return Outcome(c.same_point, computed, summary, evidence)


def _set_operation(args, combine, name, dative, symbol) -> Outcome:
    _input_components([*args["sets"], args["claimed_set"]])
    inputs = [_set(s) for s in args["sets"]]
    claimed = _set(args["claimed_set"])
    result = combine(inputs)
    difference = S.symmetric_difference(result, claimed)
    _output_components(result, difference)
    computed = {"computed_set": _encode_set(result), "symmetric_difference": _encode_set(difference)}
    expression = f" {symbol} ".join(set_text(s, 300) for s in inputs)
    evidence = [_cut(f"{name}: {expression} = {set_text(result)}."),
                _cut("Заявлено: " + set_text(claimed) + "."),
                _cut("Расхождение (симметрическая разность): " + set_text(difference) + "."),
                SCOPE_M07]
    if difference.empty:
        summary = f"Заявленное множество равно {dative} данных множеств."
    else:
        summary = _cut(f"Заявленное множество не равно {dative}: расхождение {set_text(difference, 600)}.")
    return Outcome(difference.empty, computed, summary, evidence)


def op_set_union(args) -> Outcome:
    return _set_operation(args, S.union, "Объединение", "объединению", "∪")


def op_set_intersection(args) -> Outcome:
    return _set_operation(args, S.intersection, "Пересечение", "пересечению", "∩")


def op_set_membership(args) -> Outcome:
    _input_components([args["set"]])
    value, point = _set(args["set"]), R.parse(args["point"]["coefficient"])
    member, expected = value.contains(point), args["expected_member"]
    summary = _cut(f"{show(point)} {'∈' if member else '∉'} {set_text(value, 900)}; заявлено {'∈' if expected else '∉'}.")
    return Outcome(member == expected, {"member": member}, summary, [summary, SCOPE_M07])


def op_set_equal(args) -> Outcome:
    _input_components([args["left"], args["right"]])
    left, right = _set(args["left"]), _set(args["right"])
    difference = S.symmetric_difference(left, right)
    _output_components(difference)
    computed = {"equal": difference.empty, "symmetric_difference": _encode_set(difference)}
    summary = ("Множества равны." if difference.empty else
               _cut("Множества различаются; расхождение: " + set_text(difference, 900) + "."))
    evidence = [_cut("Слева: " + set_text(S.normalize(left)) + "."), _cut("Справа: " + set_text(S.normalize(right)) + "."),
                summary, SCOPE_M07]
    return Outcome(difference.empty, computed, summary, evidence)


def op_denominator_nonzero(args) -> Outcome:
    expression = args["expression"]
    variable = expression["variable"]
    [R.parse(c) for c in expression["numerator"]]  # the whole original quotient must be well formed
    denominator = [R.parse(c) for c in expression["denominator"]]
    point = R.parse(args["point"]["coefficient"])
    value = S.evaluate_polynomial(denominator, point)
    computed = {"denominator_value": R.encode(value), "nonzero": value != 0}
    q = polynomial_text(denominator, variable)
    if value != 0:
        summary = f"Исходный знаменатель {q} при {variable} = {show(point)} равен {show(value)} ≠ 0."
        meaning = "Это только допустимость этой дроби в этой точке, а не вся ОДЗ и не истинность неравенства."
    else:
        summary = f"Исходный знаменатель {q} при {variable} = {show(point)} равен 0: дробь в этой точке не определена."
        meaning = "Сокращение дроби не возвращает точку в область определения исходного выражения."
    return Outcome(value != 0, computed, _cut(summary), [_cut(summary), meaning,
                   "Проверяется исходный знаменатель в записанном виде; сокращённая форма не используется.", SCOPE_M07])


HANDLERS = {
    "m06.pi_interval_membership": op_pi_interval_membership,
    "m06.select_pi_roots": op_select_pi_roots,
    "m06.circle_point_equal": op_circle_point_equal,
    "m07.set_union": op_set_union,
    "m07.set_intersection": op_set_intersection,
    "m07.set_membership": op_set_membership,
    "m07.set_equal": op_set_equal,
    "m07.denominator_nonzero": op_denominator_nonzero,
}
assert set(HANDLERS) == set(OPERATIONS)


# -------------------------------------------------------------------- executor

def _response(request, status, reason, summary, computed, evidence):
    operation = request.get("operation") if isinstance(request.get("operation"), str) and ID.fullmatch(request.get("operation")) else "invalid.operation"
    request_id = request.get("request_id")
    if not (isinstance(request_id, str) and ID.fullmatch(request_id)):
        request_id = "k01-" + m01.digest(request)[:32]  # transport-assigned correlation id
    return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "request_sha256": m01.digest(request),
            "operation": operation, "operation_version": PROTOCOL_VERSION, "tool_version": TOOL_VERSION,
            "status": status, "reason_code": reason, "summary": _cut(summary, 2048),
            "computed": computed, "evidence": [_cut(e, 2048) for e in (evidence or [summary])][:40]}


def unknown(request, reason, summary, evidence=None):
    return _response(request, "unknown", reason, summary, None, evidence or [summary])


def execute(request, result, *, source_input=None) -> dict:
    """Execute one K01 request against the M01 result that supplies its context."""
    if not isinstance(request, dict) or not isinstance(result, dict):
        raise TypeError("request and result must be JSON objects")
    started = time.monotonic()
    if size(request) > LIMITS["request_bytes"]:
        return unknown(request, "resource_limit", "Запрос больше 64 КиБ.")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        return unknown(request, "unsupported_operation", "Операция не зарегистрирована в K01 1.0.0.")
    if request.get("operation_version") != OPERATIONS[operation]["version"]:
        return unknown(request, "unsupported_operation", "Неизвестная версия операции.")
    errors = sorted({"/" + "/".join(map(str, e.absolute_path)) for e in REQUEST_SCHEMA.iter_errors(request)})
    if errors:
        # Paths only: no submitted text is echoed into the evidence.
        return unknown(request, "invalid_input", "Запрос не соответствует схеме K01: " + ", ".join(errors[:10]))
    gate = premise_gate(request, result, source_input=source_input)
    if gate:
        return unknown(request, *gate)
    try:
        outcome = HANDLERS[operation](request["arguments"])
    except CheckError as exc:
        return unknown(request, exc.code, str(exc) or "Точный ответ недоступен.")
    except RecursionError:
        return unknown(request, "resource_limit", "Превышен предел вычисления.")
    if (time.monotonic() - started) * 1000 > LIMITS["wall_ms_per_operation"]:
        # Python cannot pre-empt the computation; inputs are bounded so this is a safety net.
        return unknown(request, "timeout", "Операция превысила 250 мс; результат не используется.")
    response = _response(request, "verified" if outcome.truth else "contradicted",
                         "exact_true" if outcome.truth else "exact_false", outcome.summary, outcome.computed, outcome.evidence)
    if size(response) > LIMITS["response_bytes"]:
        return unknown(request, "resource_limit", "Ответ больше 64 КиБ; точный результат не выдаётся частично.")
    if list(RESPONSE_SCHEMA.iter_errors(response)):
        return unknown(request, "internal_error", "Ответ исполнителя не прошёл собственную проверку формата K01.")
    return response
