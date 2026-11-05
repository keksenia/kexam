"""Prepare the approved 16-request experiment offline. Never sends requests."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from .call_guard import Policy, canonical, digest
from .m05.l04 import CASES, build_l04_case
from .m05.runtime import ROOT, freeze_identity
from .provider_requests import LEGACY_VERSION as VERSION, ENDPOINT, build_hubris_request

APPROVAL_PATH = ROOT / "planning/l05/AUTHORIZATION.json"
CONFIG_PATH = ROOT / "planning/l04/configurations.json"
DEFAULT_OUTPUT = ROOT / "data_local/m17/l04-2026-10-08-budget-200"


def _private_write(path, raw):
    """Immutable/repeatable preparation: changed bytes need a new version."""
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("prepared_artifact_drift")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def prepare_experiment(output=DEFAULT_OUTPUT):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / "data_local").resolve()):
        raise ValueError("private_output_required")
    approval_raw = APPROVAL_PATH.read_bytes()
    approval = json.loads(approval_raw)
    config_raw = CONFIG_PATH.read_bytes()
    config = json.loads(config_raw)
    if (approval.get("budget_approved") is not True or approval.get("variant") != "two_ocr"
            or type(approval.get("budget_rub")) is not int or approval.get("budget_rub") not in (150, 200)
            or approval.get("primary_calls") != 16
            or approval.get("technical_retry_reserve") != 4 or approval.get("max_total_calls") != 20
            or approval.get("prompt_package_sha256") != freeze_identity()
            or approval.get("configuration_sha256") != hashlib.sha256(config_raw).hexdigest()
            or config.get("provider_base_url") != "https://api.hubris.pw/v1"):
        raise ValueError("approval_scope_mismatch")
    profiles = {p["id"]: p for p in config["profiles"]}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    rows = []
    for case_id in CASES:
        case = build_l04_case(case_id)
        for stage, suffix in (("reading", "ocr"), ("assessment", "checker")):
            pair = []
            for candidate in ("G", "D"):
                profile = profiles[candidate + "-" + suffix]
                api = build_hubris_request(case[stage], profile, case["assets"],
                    asset_root=ROOT / "data_local/l03/model_inputs" / case_id)
                label = f"{case_id}-{stage}-{candidate}"
                _private_write(output / (label + "-body.json"), api.body_bytes)
                _private_write(output / (label + "-stage.json"), canonical(api.stage_request.document()).encode())
                row = dict(id=label, case_id=case_id, stage=stage, profile=profile["id"],
                    model=profile["model"], body_file=label + "-body.json", stage_file=label + "-stage.json",
                    body_sha256=api.body_sha256, body_bytes=len(api.body_bytes),
                    content_sha256=api.content_sha256, m14_fingerprint=api.stage_request.fingerprint,
                    max_tokens=api.stage_request.payload["max_tokens"],
                    timeout_seconds=profile["transport_timeout_seconds"], images=list(api.images))
                rows.append(row)
                # Settings differ by design; substantive instructions/data/PNG
                # must remain identical for both assigned configurations.
                pair.append(api.stage_request.payload["messages"])
            if digest(pair[0]) != digest(pair[1]):
                raise ValueError("comparison_input_mismatch")
    if len(rows) != 16 or len({r["m14_fingerprint"] for r in rows}) != 16:
        raise ValueError("request_count_mismatch")
    # Do not present the rough estimate as proven per-request billing ceilings.
    # This template binds the approved scope; live activation is a transport
    # task after documented bounds. No spending ledger is created by preparation.
    policy = Policy(enabled=False, mode="live", approval_ref=approval["id"], budget_micro_rub=approval["budget_rub"] * 1_000_000,
        max_attempts=2, max_total_calls=20, max_output_tokens=12000, max_response_bytes=512*1024,
        timeout_seconds=180, operation_timeout_seconds=300)
    # Preserve the frozen hard-ceiling dispatch format. New optional M14 modes
    # use their own policy document; omitted fields retain the hard default.
    policy_template=asdict(policy)
    policy_template.pop("budget_mode")
    policy_template.pop("dispatch_rules")
    dispatch = dict(version=VERSION, endpoint=ENDPOINT, http_method="POST",
        headers_without_credentials={"Content-Type": "application/json", "Accept": "application/json"},
        authorization_header="Injected in memory by transport; never saved",
        approval_ref=approval["id"], approval_sha256=hashlib.sha256(approval_raw).hexdigest(),
        primary_calls=16, technical_retry_reserve=4, max_total_calls=20, max_attempts_per_primary=2,
        budget_rub=approval["budget_rub"], common_ledger=str(ROOT / "data_local/ege_checker/spending/l04-2026-10-08"),
        no_sdk_automatic_retries=True, parallel_requests=1, requests=rows,
        retry_policy=config["technical_retry_policy"], policy_template=policy_template,
        request_preparation_complete=True, provider_acceptance_verified=False, launch_ready=False,
        remaining_gates=["single-attempt provider transport and response/accounting parsing through M14",
            "documented input/total-output/route-price bounds before per-request reservations"],
        network_model_calls=0, paid_calls=0)
    _private_write(output / "dispatch.json", (json.dumps(dispatch, ensure_ascii=False, indent=2)+"\n").encode())
    return dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = prepare_experiment(args.output)
    print(json.dumps({k: report[k] for k in ("version", "primary_calls", "technical_retry_reserve", "budget_rub",
        "request_preparation_complete", "launch_ready", "network_model_calls", "paid_calls")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
