"""Nonsecret native Gemini configuration; credentials remain in their source file."""
from copy import deepcopy
import json
from pathlib import Path

from .call_guard import digest, unique_object
from .gemini_transport import validate_pricing, TransportError

VERSION = "gemini-direct-config-1.0.0"
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "planning/m17/GEMINI_DIRECT_CONFIG.json"
MODEL = "gemini-3.8-flash"


def load_config():
    try:
        value = json.loads(CONFIG_PATH.read_text(), object_pairs_hook=unique_object)
        if (not isinstance(value, dict) or value.get("provider") != "google"
                or value.get("model") != MODEL
                or value.get("base_url") != "https://generativelanguage.googleapis.com/v1beta"
                or value.get("credential_variable") != "GEMINI_API_KEY"
                or not isinstance(value.get("credential_file"), str)
                or not Path(value["credential_file"]).is_absolute()
                or value.get("automatic_retries") != 0
                or value.get("service_tier") != "standard"
                or value.get("network") != {"mode":"system_vpn", "allow_direct_fallback":False}):
            raise ValueError("gemini_configuration_invalid")
        validate_pricing(value["pricing"], model=MODEL)
        return value
    except TransportError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("gemini_configuration_invalid") from None


def live_profile(profile, config):
    value = deepcopy(profile)
    value.update(provider="google", model=config["model"])
    return value


def identity(config):
    return digest(config)
