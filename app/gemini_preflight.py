"""Read-only authenticated model metadata; never generate content or print a key."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from .call_guard import unique_object
from .gemini_config import load_config, identity, ROOT
from .gemini_transport import read_credential, TransportError
from .vpn_transport import vpn_problem, make_vpn_opener

ACCESS_PATH = ROOT / "planning/m17/gemini_direct_2026-10-09/access.json"


def access_problem(config):
    """Bind the read-only access receipt to the selected configuration/file."""
    try:
        value = json.loads(ACCESS_PATH.read_text(), object_pairs_hook=unique_object)
        if not isinstance(value, dict): return "gemini_access_unverified"
        if (value.get("configuration_sha256") != identity(config)
                or value.get("credential_file_mtime_ns") != Path(config["credential_file"]).stat().st_mtime_ns
                or value.get("endpoint") != config["base_url"]+"/models/"+config["model"]
                or value.get("method") != "GET" or value.get("generation_requests") != 0):
            return "gemini_access_unverified"
        if value.get("http_status") == 200 and value.get("model_available") is True:
            return None
        reasons = value.get("provider_error_reasons", [])
        if not isinstance(reasons, list) or not all(isinstance(r,str) for r in reasons):
            return "gemini_access_unverified"
        if "LOCATION_UNSUPPORTED" in reasons: return "gemini_location_unsupported"
        if "API_KEY_INVALID" in reasons or "API_KEY_EXPIRED" in reasons: return "credential_missing_or_invalid"
        if "BILLING_REQUIRED" in reasons or "BILLING_DISABLED" in reasons: return "gemini_billing_required"
        return value.get("error") if value.get("error") in {"vpn_required", "vpn_route_unavailable"} else "gemini_access_unverified"
    except (OSError, ValueError, TypeError, KeyError):
        return "gemini_access_unverified"


def check_access(*, opener=None):
    config = load_config()
    result = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
        provider="google", model=config["model"], method="GET", model_requests=0,
        generation_requests=0, credentials_persisted=False,
        configuration_sha256=identity(config), network_mode="system_vpn",
        endpoint=config["base_url"]+"/models/"+config["model"])
    if opener is None:
        problem = vpn_problem()
        if problem:
            result.update(model_available=False, error=problem)
            return result
    try:
        result["credential_file_mtime_ns"] = Path(config["credential_file"]).stat().st_mtime_ns
        key = read_credential(config["credential_file"])
    except (OSError, TransportError):
        result.update(model_available=False,error="credential_missing_or_invalid")
        return result
    request = Request(result["endpoint"], method="GET", headers={
        "x-goog-api-key": key, "Accept": "application/json"})
    try:
        with (opener or make_vpn_opener()).open(request, timeout=25) as response:
            raw = response.read(65537)
            if len(raw) > 65536: raise ValueError("metadata_too_large")
            value = json.loads(raw, object_pairs_hook=unique_object)
            result["http_status"] = response.getcode()
        expected = "models/"+config["model"]
        result["model_available"] = (isinstance(value, dict) and value.get("name") == expected
            and "generateContent" in value.get("supportedGenerationMethods", []))
        if result["model_available"]:
            for field in ("inputTokenLimit", "outputTokenLimit"):
                if type(value.get(field)) is int: result[field] = value[field]
        else: result["error"] = "model_metadata_mismatch"
    except HTTPError as error:
        result.update(http_status=error.code, model_available=False, error="provider_http_error")
        try:
            raw = error.read(65537)
            failure = json.loads(raw, object_pairs_hook=unique_object).get("error", {}) if len(raw) <= 65536 else {}
            status = failure.get("status")
            if status in {"INVALID_ARGUMENT", "PERMISSION_DENIED", "NOT_FOUND", "UNAUTHENTICATED", "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION"}:
                result["provider_error_status"] = status
            allowed = {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
                "API_KEY_HTTP_REFERRER_BLOCKED", "CONSUMER_INVALID", "BILLING_DISABLED", "SERVICE_DISABLED"}
            reasons = sorted({d.get("reason") for d in failure.get("details", [])
                if isinstance(d, dict) and d.get("reason") in allowed})
            if reasons: result["provider_error_reasons"] = reasons
            # Never print raw messages: a provider could echo the credential.
            if str(failure.get("message", "")).startswith("API key not valid."):
                result["provider_error_reasons"] = ["API_KEY_INVALID"]
            elif "User location is not supported" in str(failure.get("message", "")):
                result["provider_error_reasons"] = ["LOCATION_UNSUPPORTED"]
            elif "billing" in str(failure.get("message", "")).lower() and status == "FAILED_PRECONDITION":
                result["provider_error_reasons"] = ["BILLING_REQUIRED"]
        except Exception:
            pass
    except Exception:
        result.update(model_available=False, error="metadata_network_or_format_error")
    return result


if __name__ == "__main__":
    value = check_access()
    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCESS_PATH.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(value, ensure_ascii=False))
