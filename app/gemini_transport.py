"""One native Gemini Developer API attempt, with explicit token accounting.

The guard's small Chat Completions-shaped request is an internal representation.
The deterministic native body is independently hashed before dispatch. No SDK,
automatic retry, redirect, alternate host, remote image fetch, or fallback model
is used. A token-based cost estimate is never represented as a provider invoice.
"""
from base64 import b64decode
from binascii import Error as Base64Error
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request

from .call_guard import ProviderResponse, canonical, unique_object
from .provider_requests import BODY_LIMIT
from .vpn_transport import make_vpn_opener
from .transport_diagnostics import TransportFailure, detail, exception_detail

VERSION = "m17-gemini-transport-1.2.0"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
DEFAULT_MODELS = frozenset({"gemini-3.8-flash"})
BILLING_SOURCE = "https://ai.google.dev/gemini-api/docs/billing"
MODEL_PATTERN = re.compile(r"gemini-[a-z0-9]+(?:[.-][a-z0-9]+)*\Z")
REFUSAL_REASONS = frozenset({"SAFETY", "RECITATION", "BLOCKLIST",
    "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION", "ESCALATION", "PUP_LIMITED_DISABLED"})


class TransportError(TransportFailure):
    """Stable codes only; no private input or credentials in exceptions."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_credential(key):
    return (isinstance(key, str) and 16 <= len(key) <= 256
            and all(33 <= ord(c) <= 126 for c in key)
            and not any(c in "\"'" for c in key))


def read_credential(path):
    """Read only GEMINI_API_KEY from the explicitly configured file.

    Unrelated assignments are never parsed, collected, returned, or printed.
    Environment variables cannot silently override the user-selected file.
    """
    key = None
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for line in source:
                match = re.match(r"^\s*(?:export[ \t]+)?GEMINI_API_KEY[ \t]*=[ \t]*(.*)$", line)
                if not match:
                    continue
                if key is not None:
                    raise TransportError("credential_duplicate")
                value = match.group(1).strip()
                if value.startswith(("\"", "'")):
                    quote = value[0]
                    end = value.find(quote, 1)
                    if end < 0 or value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#"):
                        raise TransportError("credential_missing_or_invalid")
                    value = value[1:end]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                key = value
    except TransportError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        raise TransportError("credential_missing_or_invalid") from None
    if not _valid_credential(key):
        raise TransportError("credential_missing_or_invalid")
    return key


def endpoint_for_model(model):
    if not isinstance(model, str) or MODEL_PATTERN.fullmatch(model) is None:
        raise TransportError("native_model_invalid")
    return BASE_URL + "/models/" + model + ":generateContent"


def _message_parts(content, *, images_allowed):
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list) or not content:
        raise TransportError("native_message_invalid")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise TransportError("native_message_invalid")
        if set(part) == {"type", "text"} and part["type"] == "text" and isinstance(part["text"], str):
            parts.append({"text": part["text"]})
        elif images_allowed and set(part) == {"type", "image_url"} and part["type"] == "image_url":
            image = part["image_url"]
            if not isinstance(image, dict) or set(image) != {"url"} or not isinstance(image["url"], str):
                raise TransportError("native_image_invalid")
            match = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/]*={0,2})", image["url"])
            if not match:
                raise TransportError("native_image_invalid")
            try:
                if not b64decode(match.group(2), validate=True):
                    raise TransportError("native_image_invalid")
            except (Base64Error, ValueError):
                raise TransportError("native_image_invalid") from None
            parts.append({"inlineData": {"mimeType": match.group(1), "data": match.group(2)}})
        else:
            raise TransportError("native_message_option_unsupported")
    return parts


def build_native_request(payload, *, allowed_models=None):
    """Return exact native generateContent body; never read a credential."""
    required = {"model", "messages", "max_tokens", "reasoning_effort", "response_format"}
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"stream", "include_thoughts"}:
        raise TransportError("native_request_option_unsupported")
    if type(payload.get("include_thoughts", False)) is not bool:
        raise TransportError("native_request_option_unsupported")
    model = payload["model"]
    endpoint_for_model(model)
    if allowed_models is not None and model not in allowed_models:
        raise TransportError("native_model_not_allowed")
    if payload.get("stream", False) is not False:
        raise TransportError("native_request_option_unsupported")
    if type(payload["max_tokens"]) is not int or not 0 < payload["max_tokens"] <= 65536:
        raise TransportError("native_output_limit_invalid")
    effort = payload["reasoning_effort"]
    if effort not in {"low", "medium", "high"}:
        raise TransportError("native_thinking_level_invalid")
    response_format = payload["response_format"]
    if response_format == {"type": "json_object"}:
        text_format = {"mimeType": "APPLICATION_JSON"}
    elif isinstance(response_format, dict) and set(response_format) == {"type", "json_schema"} and response_format["type"] == "json_schema":
        spec = response_format["json_schema"]
        if (not isinstance(spec, dict) or set(spec) != {"name", "strict", "schema"}
                or not isinstance(spec["name"], str) or not spec["name"]
                or spec["strict"] is not True or not isinstance(spec["schema"], dict)):
            raise TransportError("native_response_format_invalid")
        text_format = {"mimeType": "APPLICATION_JSON", "schema": deepcopy(spec["schema"])}
    else:
        raise TransportError("native_response_format_invalid")
    messages = payload["messages"]
    if not isinstance(messages, list) or not messages:
        raise TransportError("native_message_invalid")
    system_parts, contents = [], []
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise TransportError("native_message_invalid")
        if message["role"] == "system" and not contents:
            system_parts.extend(_message_parts(message["content"], images_allowed=False))
        elif message["role"] == "user":
            contents.append({"role": "user", "parts": _message_parts(message["content"], images_allowed=True)})
        else:
            raise TransportError("native_message_role_unsupported")
    if not contents:
        raise TransportError("native_message_invalid")
    body = {"contents": contents, "serviceTier": "standard", "generationConfig": {
        "maxOutputTokens": payload["max_tokens"],
        "thinkingConfig": {"thinkingLevel": effort.upper(), "includeThoughts": payload.get("include_thoughts", False)},
        "responseFormat": {"text": text_format}}}
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    try:
        native_size = len(canonical(body).encode("utf-8"))
    except (TypeError, ValueError):
        raise TransportError("native_body_invalid") from None
    if native_size > BODY_LIMIT:
        raise TransportError("native_body_too_large")
    return body


def _pricing_numbers(pricing):
    names = ("input_usd_per_million", "output_usd_per_million",
             "cached_input_usd_per_million", "usd_to_rub")
    values = {}
    try:
        for name in names:
            value = pricing[name]
            if not isinstance(value, str) or not value or len(value) > 128:
                raise TransportError("pricing_invalid")
            number = Decimal(value)
            if not number.is_finite() or number < 0 or number > 1_000_000:
                raise TransportError("pricing_invalid")
            values[name] = number
    except (KeyError, TypeError, DecimalException):
        raise TransportError("pricing_invalid") from None
    if (values["input_usd_per_million"] <= 0 or values["output_usd_per_million"] <= 0
            or values["usd_to_rub"] <= 0
            or values["cached_input_usd_per_million"] > values["input_usd_per_million"]):
        raise TransportError("pricing_invalid")
    return values


def validate_pricing(pricing, *, model=None, now=None):
    """Validate the explicit accounting snapshot before reservation or keys."""
    if not isinstance(pricing, dict):
        raise TransportError("pricing_missing")
    _pricing_numbers(pricing)
    endpoint_for_model(pricing.get("model"))
    if model is not None and pricing["model"] != model:
        raise TransportError("pricing_model_mismatch")
    if not all(isinstance(pricing.get(name), str) and pricing[name].strip()
               for name in ("source", "fx_source", "tariff_ref", "valid_until")):
        raise TransportError("pricing_evidence_missing")
    try:
        expiry = datetime.fromisoformat(pricing["valid_until"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise TransportError("pricing_expiry_invalid")
        timestamp = expiry.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise TransportError("pricing_expiry_invalid") from None
    if not (time.time() if now is None else now) < timestamp:
        raise TransportError("pricing_expired")


def retry_delay(headers, *, now=None):
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return 0
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(raw).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


def _normalize_usage(usage):
    if not isinstance(usage, dict):
        return None
    fields = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
              "cachedContentTokenCount", "toolUsePromptTokenCount", "totalTokenCount")
    # Zero-valued optional protobuf fields can be omitted from JSON. The total
    # independently confirms that no candidate/thinking tokens were lost.
    counts = {name: usage.get(name, 0) for name in fields}
    if ("promptTokenCount" not in usage or "totalTokenCount" not in usage
            or any(type(value) is not int or not 0 <= value < 2**53 for value in counts.values())
            or counts["totalTokenCount"] != counts["promptTokenCount"] + counts["candidatesTokenCount"] + counts["thoughtsTokenCount"]
            or counts["cachedContentTokenCount"] > counts["promptTokenCount"]
            or counts["toolUsePromptTokenCount"] != 0
            or not isinstance(usage.get("serviceTier", "standard"), str)
            or usage.get("serviceTier", "standard") not in {"standard", "unspecified"}):
        return None
    return {"prompt_tokens": counts["promptTokenCount"],
        "completion_tokens": counts["candidatesTokenCount"] + counts["thoughtsTokenCount"],
        "total_tokens": counts["totalTokenCount"],
        "prompt_tokens_details": {"cached_tokens": counts["cachedContentTokenCount"]},
        "completion_tokens_details": {"reasoning_tokens": counts["thoughtsTokenCount"]}}


def _estimate_cost(usage, pricing):
    numbers = _pricing_numbers(pricing)
    cached = usage["prompt_tokens_details"]["cached_tokens"]
    with localcontext() as context:
        context.prec = 100
        usd = ((usage["prompt_tokens"] - cached) * numbers["input_usd_per_million"]
            + cached * numbers["cached_input_usd_per_million"]
            + usage["completion_tokens"] * numbers["output_usd_per_million"]) / Decimal(1_000_000)
        rub = (usd * numbers["usd_to_rub"]).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
        kopecks = rub * 100
    evidence = {name: pricing[name] for name in ("input_usd_per_million", "output_usd_per_million",
        "cached_input_usd_per_million", "usd_to_rub", "source", "fx_source", "valid_until", "tariff_ref")}
    evidence.update(kind="token_estimate", usd=format(usd, "f"), rub=format(rub, "f"),
                    rounding="up_to_8_fractional_rub_digits", input_includes_cached=True,
                    output_includes_thinking=True)
    return format(kopecks, "f"), evidence


def parse_response(raw, *, requested_model=None, pricing=None, status=200, headers=None):
    try:
        data = json.loads(raw, object_pairs_hook=unique_object,
            parse_float=lambda value: float(value) if math.isfinite(float(value)) else (_ for _ in ()).throw(ValueError("nonfinite")),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (TypeError, ValueError, UnicodeError):
        if status == 200:
            raise TransportError("provider_envelope_invalid",diagnostic=detail("invalid_envelope",status)) from None
        data = {}
    if not isinstance(data, dict):
        if status == 200:
            raise TransportError("provider_envelope_invalid",diagnostic=detail("invalid_envelope",status))
        data = {}
    native_usage = data.get("usageMetadata")
    usage = _normalize_usage(native_usage)
    metadata = {}
    if isinstance(native_usage, dict):
        # Accounting remains separate from the optional private thought summary.
        metadata["native_usage"] = deepcopy(native_usage)
    native_model = data.get("modelVersion")
    if isinstance(native_model, str):
        metadata["native_model_version"] = native_model
    model = native_model
    matched_model = (isinstance(requested_model, str) and isinstance(native_model, str)
        and (native_model == requested_model or native_model.startswith(requested_model + "-")))
    if matched_model:
        model = requested_model
    cost, basis, tariff = None, "unknown", None
    if usage is not None and pricing is not None and matched_model:
        try:
            validate_pricing(pricing, model=requested_model)
            cost, evidence = _estimate_cost(usage, pricing)
            basis, tariff = "token_estimate", pricing["tariff_ref"]
            metadata["accounting"] = evidence
        except TransportError:
            pass
    if status in {400, 500} and cost is None:
        # Published no-bill terms are retained as evidence. Without token
        # counts, M01 does not permit us to invent a token-based zero invoice.
        metadata["accounting"] = {"kind": "documented_uncharged_error", "source": BILLING_SOURCE}
    candidates = data.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 and isinstance(candidates[0], dict) else None
    original_finish, content, finish = None, "", "invalid_envelope"
    feedback = data.get("promptFeedback")
    if (candidate is None and isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str)
            and feedback["blockReason"] != "BLOCK_REASON_UNSPECIFIED"):
        original_finish, finish = feedback.get("blockReason"), "refusal"
    elif candidate is not None:
        original_finish = candidate.get("finishReason")
        if isinstance(original_finish, str):
            finish = {"STOP": "stop", "MAX_TOKENS": "length"}.get(original_finish,
                "refusal" if original_finish in REFUSAL_REASONS else "invalid_envelope")
        candidate_content = candidate.get("content")
        parts = candidate_content.get("parts") if isinstance(candidate_content, dict) else None
        texts, summaries = [], []
        summary_bytes, summary_truncated = 0, False
        if parts is not None:
            if not isinstance(parts, list):
                finish = "invalid_envelope"
            else:
                for part in parts:
                    if not isinstance(part, dict) or type(part.get("thought", False)) is not bool:
                        finish = "invalid_envelope"
                        break
                    if part.get("thought") is True:
                        # Optional provider summary, never JSON/student content or
                        # a prerequisite for accepting an otherwise valid READ.
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            raw_summary = text.encode("utf-8")
                            remaining = max(0, 131072 - summary_bytes)
                            kept = raw_summary[:remaining].decode("utf-8", errors="ignore")
                            if kept: summaries.append(kept)
                            summary_bytes += len(kept.encode("utf-8"))
                            summary_truncated |= len(raw_summary) > remaining
                        continue
                    if not isinstance(part.get("text"), str) or set(part) - {"text", "thought", "thoughtSignature"}:
                        finish = "invalid_envelope"
                        break
                    texts.append(part["text"])
        if finish == "stop" and not texts:
            finish = "invalid_envelope"
        content = "".join(texts)
        metadata["thought_summary"] = dict(status="available" if summaries else "unavailable",
            parts=summaries, truncated=summary_truncated)
    if status == 200 and usage is None:
        finish = "invalid_usage"
    return ProviderResponse(content, usage=usage, cost_kopecks=cost, cost_basis=basis,
        tariff_ref=tariff, provider_metadata=metadata, model=model,
        response_id=data.get("responseId"), finish_reason=finish, http_status=status,
        retry_after_seconds=retry_delay(headers),
        provider_finish_reason=original_finish if isinstance(original_finish, str) else None)


class GeminiTransport:
    kind = "live"
    single_attempt = True

    def __init__(self, credential, *, allowed_body_hashes, pricing,
                 allowed_native_body_hashes=None, allowed_models=None,
                 total_output_bound_verified=False, opener=None):
        if not _valid_credential(credential):
            raise TransportError("credential_missing_or_invalid")
        validate_pricing(pricing)
        self._credential = credential
        self._pricing = deepcopy(pricing)
        self.allowed_models = frozenset(DEFAULT_MODELS if allowed_models is None else allowed_models)
        if not self.allowed_models or any(not isinstance(model, str) or MODEL_PATTERN.fullmatch(model) is None for model in self.allowed_models):
            raise TransportError("native_model_invalid")
        self.allowed_body_hashes = frozenset(allowed_body_hashes)
        self.allowed_native_body_hashes = None if allowed_native_body_hashes is None else frozenset(allowed_native_body_hashes)
        self.total_output_bound_verified = total_output_bound_verified is True
        self._opener = opener or make_vpn_opener()

    def __repr__(self):
        return "GeminiTransport(credential=<redacted>, single_attempt=True)"

    def __call__(self, payload, *, timeout_seconds):
        try:
            approved = canonical(payload).encode("utf-8")
        except (TypeError, ValueError):
            raise TransportError("unapproved_body") from None
        if len(approved) > BODY_LIMIT or hashlib.sha256(approved).hexdigest() not in self.allowed_body_hashes:
            raise TransportError("unapproved_body")
        validate_pricing(self._pricing, model=payload.get("model"))
        native = build_native_request(payload, allowed_models=self.allowed_models)
        raw = canonical(native).encode("utf-8")
        native_sha = hashlib.sha256(raw).hexdigest()
        if self.allowed_native_body_hashes is not None and native_sha not in self.allowed_native_body_hashes:
            raise TransportError("unapproved_native_body")
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not 0 < timeout_seconds <= 180):
            raise TransportError("timeout_invalid")
        request = Request(endpoint_for_model(payload["model"]), data=raw, method="POST", headers={
            "x-goog-api-key": self._credential, "Content-Type": "application/json", "Accept": "application/json"})
        status=None
        try:
            try:
                response = self._opener.open(request, timeout=timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                status=response.getcode()
                body = response.read(MAX_ENVELOPE_BYTES + 1)
                if len(body) > MAX_ENVELOPE_BYTES:
                    raise TransportError("provider_envelope_too_large",diagnostic=detail("response_too_large",status))
                parsed = parse_response(body, requested_model=payload["model"], pricing=self._pricing,
                    status=status, headers=response.headers)
                parsed.provider_metadata.update(native_body_sha256=native_sha, native_body_bytes=len(raw))
                return parsed
        except TransportError:
            raise
        except Exception as error:
            raise TransportError("provider_transport_failed",diagnostic=exception_detail(error,http_status=status)) from None
