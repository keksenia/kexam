"""Requested provider READ shape; full semantic validation remains local.

The provider schema subset does not express internal conditional constraints.
They are retained in compact_reading.schema and checked after decoding. Optional
fields may arrive omitted or with a null sentinel. Two observed packaging aliases
are normalized without inventing content. Required content never gets defaults.
Provider wire compliance is recorded on the original response, before acceptance.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator

from .m05.runtime import PromptError

VERSION = "strict-reading-wire-1.2.1"
LOCAL_ONLY = {"allOf", "pattern", "uniqueItems", "minLength", "maxLength"}
SUPPORTED = {"type", "properties", "required", "additionalProperties", "items",
             "anyOf", "enum", "const", "description", "minimum", "maximum",
             "minItems", "maxItems"}


def wire_schema(internal, *, require_optional_fields=True):
    """Project only the supported provider shape; never replace the local schema."""
    if not isinstance(internal, dict) or set(internal) - SUPPORTED - LOCAL_ONLY:
        raise PromptError("strict_reading_schema_unsupported")
    out = {k: deepcopy(v) for k, v in internal.items()
           if k not in LOCAL_ONLY | {"properties", "items", "anyOf", "const"}}
    if "const" in internal:
        out["enum"] = [deepcopy(internal["const"])]
    if "enum" in out and "type" not in out:
        if not all(isinstance(v, str) for v in out["enum"]):
            raise PromptError("strict_reading_schema_unsupported")
        out["type"] = "string"
    if "anyOf" in internal:
        out["anyOf"] = [wire_schema(s, require_optional_fields=require_optional_fields) for s in internal["anyOf"]]
    if "items" in internal:
        out["items"] = wire_schema(internal["items"], require_optional_fields=require_optional_fields)
    if "properties" in internal:
        required = set(internal.get("required", []))
        props = {}
        for key, child in internal["properties"].items():
            projected = wire_schema(child, require_optional_fields=require_optional_fields)
            props[key] = projected if key in required else dict(anyOf=[projected, dict(type="null")])
        out.update(properties=props,
            required=list(props) if require_optional_fields else list(internal.get("required", [])),
            additionalProperties=False)
    return out


def response_format(internal):
    schema = wire_schema(internal)
    Draft202012Validator.check_schema(schema)
    return dict(type="json_schema", json_schema=dict(name="ege_solution_reading", strict=True, schema=schema))


def normalize_packaging(value):
    """Two exact, content-preserving aliases; never repair arbitrary JSON.

    Only an empty features object means an empty feature collection. Nonempty
    objects, other container types and mixed/partial link field sets still fail.
    Link direction, references and relation values are copied, never inferred.
    """
    normalized = deepcopy(value)
    changes = []
    if not isinstance(normalized, dict):
        return normalized, changes
    if isinstance(normalized.get("features"), dict) and not normalized["features"]:
        normalized["features"] = []
        changes.append(dict(path=["features"], rule="empty_feature_object_to_list"))
    links = normalized.get("links")
    if isinstance(links, list):
        for n, link in enumerate(links):
            if isinstance(link, dict) and set(link) == {"source", "target", "type"}:
                links[n] = dict(zip(("from", "to", "kind"),
                                   (link["source"], link["target"], link["type"])))
                changes.append(dict(path=["links", n], rule="source_target_type_to_from_to_kind"))
    return normalized, changes


def reading_diagnostics(value, internal):
    """Report provider compliance without turning packaging into semantic evidence."""
    errors = list(Draft202012Validator(wire_schema(internal)).iter_errors(value))
    omitted = []

    def visit(item, rule, path):
        if isinstance(item, dict) and "properties" in rule:
            required = set(rule.get("required", []))
            for key, child in rule["properties"].items():
                if key not in item:
                    if key not in required: omitted.append(path + [key])
                else: visit(item[key], child, path + [key])
        elif isinstance(item, list) and "items" in rule:
            for n, child in enumerate(item): visit(child, rule["items"], path + [n])

    visit(value, internal, [])
    _, changes = normalize_packaging(value)
    return dict(strict_requested=True, strict_wire_compliant=not errors,
        packaging_normalizations=changes,
        omitted_optional_fields=omitted,
        wire_errors=[dict(path=list(e.absolute_path), constraint=e.validator) for e in errors])


def decode_reading(value, internal):
    """Accept original optional-field omission, then remove optional null sentinels.

No parsing of prose completeness claims or mutation of the raw response. Only
the explicit aliases in normalize_packaging are accepted before validation.
Missing optional fields stay absent; required content and the complete local
schema are still enforced. Wire compliance is a separate diagnostic.
    """
    value, _ = normalize_packaging(value)
    acceptance = wire_schema(internal, require_optional_fields=False)
    if list(Draft202012Validator(acceptance).iter_errors(value)):
        raise PromptError("strict_reading_response_invalid")

    def unpack(item, rule):
        if isinstance(item, dict) and "properties" in rule:
            required = set(rule.get("required", []))
            return {k: unpack(v, rule["properties"][k]) for k, v in item.items()
                    if v is not None or k in required}
        if isinstance(item, list) and "items" in rule:
            return [unpack(v, rule["items"]) for v in item]
        return deepcopy(item)

    decoded = unpack(value, internal)
    if list(Draft202012Validator(internal).iter_errors(decoded)):
        raise PromptError("compact_reading_schema")
    return decoded
