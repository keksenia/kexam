"""One HTTPS attempt; credentials in memory, exact wire bytes, no redirects."""
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .call_guard import ProviderResponse, canonical, unique_object
from .provider_requests import ENDPOINT, BODY_LIMIT

VERSION = "m17-hubris-transport-1.0.2-retired"
BASE_URL = "https://api.hubris.pw/v1"
MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
# Explicitly uncharged in the provider pricing documentation. Unknown statuses
# and failures after transmission never acquire a fabricated zero charge.
UNCHARGED_HTTP = {400, 401, 402, 403, 404, 502, 503, 504}


class TransportError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_credential(path=None):
    """Historical test helper; production Hubris credentials are retired."""
    if path is None or Path(path).resolve() == Path("/var/empty/kexam-not-configured.env"):
        raise TransportError("hubris_retired")
    values = {}
    if path is not None and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                if name.strip() in {"LLM_BASE_URL", "LLM_API_KEY"}:
                    values[name.strip()] = value.strip().strip("\"'")
    key = os.environ.get("HUBRIS_API_KEY") or os.environ.get("LLM_API_KEY") or values.get("LLM_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL") or values.get("LLM_BASE_URL") or BASE_URL
    if base.rstrip("/") != BASE_URL:
        raise TransportError("credential_host_mismatch")
    if not isinstance(key, str) or not key.startswith("sk-gw-") or any(c.isspace() for c in key):
        raise TransportError("credential_missing_or_invalid")
    return key


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
    # Preserve long delays: controller may decline a retry rather than shorten
    # what the provider requested. Guard tracks the not-before timestamp.
    return max(0, delay) if math.isfinite(delay) else 0


def parse_response(raw, *, status=200, headers=None):
    try:
        data = json.loads(raw, object_pairs_hook=unique_object, parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeError):
        if status in UNCHARGED_HTTP:
            return ProviderResponse("", cost_kopecks="0", http_status=status,
                                    retry_after_seconds=retry_delay(headers))
        raise TransportError("provider_envelope_invalid") from None
    if not isinstance(data, dict):
        raise TransportError("provider_envelope_invalid")
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else None
    cost = usage.get("cost") if usage is not None else None
    if cost is None and status in UNCHARGED_HTTP:
        cost = "0"
    if isinstance(cost, Decimal):
        cost = str(cost)  # kopecks, exact; never multiply reasoning twice
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    finish = "refusal" if message.get("refusal") else choice.get("finish_reason")
    # Missing/multiple choices or a non-string content cannot be accepted as a
    # normal completion; accounting may still be settled from its invoice.
    if content is None and finish in {"length", "refusal"}:
        # Reasoning models can exhaust the shared output budget before emitting
        # content. Refusals can also have null content. Keep the explicit
        # provider signal and invoice in both cases.
        content = ""
    elif not isinstance(content, str) or not isinstance(finish, str):
        content, finish = "", "invalid_envelope"
    original_finish = finish
    if status == 200:
        counts = [usage.get(k) if usage else None for k in ("prompt_tokens", "completion_tokens")]
        details = usage.get("completion_tokens_details", {}) if usage else {}
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if (any(type(n) is not int or n < 0 for n in counts)
                or reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= counts[1])):
            finish = "invalid_usage"
    return ProviderResponse(content, usage=usage, cost_kopecks=cost,
        model=data.get("model"), response_id=data.get("id"), finish_reason=finish,
        http_status=status, retry_after_seconds=retry_delay(headers),provider_finish_reason=original_finish if isinstance(original_finish,str) else None)


class HubrisTransport:
    kind = "live"
    single_attempt = True

    def __init__(self, credential, *, allowed_body_hashes, total_output_bound_verified=False, opener=None):
        if opener is None:
            raise TransportError("hubris_retired")
        if not isinstance(credential, str) or not credential.startswith("sk-gw-") or any(c.isspace() for c in credential):
            raise TransportError("credential_missing_or_invalid")
        self._credential = credential
        self.allowed_body_hashes = frozenset(allowed_body_hashes)
        self.total_output_bound_verified = total_output_bound_verified is True
        self._opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def __repr__(self):
        return "HubrisTransport(credential=<redacted>, single_attempt=True)"

    def __call__(self, payload, *, timeout_seconds):
        raw = canonical(payload).encode("utf-8")
        if len(raw) > BODY_LIMIT or hashlib.sha256(raw).hexdigest() not in self.allowed_body_hashes:
            raise TransportError("unapproved_body")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 180:
            raise TransportError("timeout_invalid")
        request = Request(ENDPOINT, data=raw, method="POST", headers={
            "Authorization": "Bearer " + self._credential,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            try:
                response = self._opener.open(request, timeout=timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(MAX_ENVELOPE_BYTES + 1)
                if len(body) > MAX_ENVELOPE_BYTES:
                    raise TransportError("provider_envelope_too_large")
                # Redirects are not followed, so no credential or POST body is
                # ever sent to an alternative host by this adapter.
                status = response.getcode()
                return parse_response(body, status=status, headers=response.headers)
        except TransportError:
            raise
        except Exception:
            raise TransportError("provider_transport_failed") from None
