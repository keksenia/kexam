"""Guarded L04 dispatch and M01 persistence. Default: inspect, never POST."""
import argparse
from copy import deepcopy
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from contracts.ege_v1 import validate as contract
from .call_guard import CallGuard, Policy, StageRequest, canonical
from .hubris_transport import HubrisTransport, read_credential
from .hubris_transport import VERSION as TRANSPORT_VERSION
from .m05.l04 import l04_input_and_baseline
from .m05.result_boundary import materialize_assessment, materialize_reading_attempt
from .m05.runtime import ROOT, freeze_identity
from .pipeline import base_result
from .prepare_l04 import DEFAULT_OUTPUT, APPROVAL_PATH, _private_write, prepare_experiment

VERSION = "m17-l04-runner-1.0.1"
BOUNDS_PATH = ROOT / "planning/m17/PROVIDER_BOUNDS.json"


def load_plan():
    # Rebuild/compare before use: altered inputs, settings or saved bytes stop.
    dispatch = prepare_experiment()
    approval = json.loads(APPROVAL_PATH.read_text())
    if freeze_identity() != approval["prompt_package_sha256"]:
        raise ValueError("prompt_package_drift")
    requests = {}
    for row in dispatch["requests"]:
        wire = (DEFAULT_OUTPUT / row["body_file"]).read_bytes()
        request = StageRequest(**json.loads((DEFAULT_OUTPUT / row["stage_file"]).read_text()))
        if (hashlib.sha256(wire).hexdigest() != row["body_sha256"]
                or canonical(request.payload).encode() != wire or request.fingerprint != row["m14_fingerprint"]):
            raise ValueError("prepared_request_drift")
        requests[row["id"]] = request
    return dispatch, requests


def billing_policy(dispatch, bounds):
    """Only documented bounds; a rough tariff/token estimate is insufficient."""
    required = ("image_input_bound_verified", "total_output_bound_verified", "route_price_bound_verified")
    if not isinstance(bounds, dict) or any(bounds.get(k) is not True for k in required):
        raise ValueError("provider_billing_bounds_unverified")
    if bounds.get("approval_ref") != dispatch["approval_ref"] or not time.time() < bounds.get("expires_at", 0):
        raise ValueError("provider_evidence_expired_or_mismatched")
    ceilings = bounds.get("ceilings", {})
    if set(ceilings) != {r["m14_fingerprint"] for r in dispatch["requests"]}:
        raise ValueError("request_ceilings_missing")
    for row in dispatch["requests"]:
        item = ceilings[row["m14_fingerprint"]]
        if (type(item.get("micro_rub")) is not int or not 0 < item["micro_rub"] <= dispatch["budget_rub"] * 1_000_000
                or not item.get("evidence_ref")):
            raise ValueError("request_ceiling_invalid")
    policy = Policy(**{**dispatch["policy_template"], "enabled": True, "expires_at": bounds["expires_at"], "ceilings": ceilings})
    if policy.validate():
        raise ValueError(policy.validate())
    return policy


def readiness():
    dispatch, _ = load_plan()
    bounds = json.loads(BOUNDS_PATH.read_text()) if BOUNDS_PATH.exists() else {}
    try:
        billing_policy(dispatch, bounds)
        blocked = []
    except ValueError as error:
        blocked = [str(error)]
    return dict(version=VERSION, transport_implemented=True, budget_approved=True, budget_rub=dispatch["budget_rub"],
        primary_calls=16, retry_reserve=4, launch_ready=not blocked, blockers=blocked,
        model_calls_performed_by_preflight=0, ordinary_server_connected=False)


def materialize(row, data):
    source, shared = l04_input_and_baseline(row["case_id"])
    if row["stage"] == "reading":
        baseline = base_result(source, dict(id="m17-reading-prevalidation", configuration={}))
        baseline["created_at"] = source["created_at"]
        candidate = materialize_reading_attempt(deepcopy(data), baseline, source)
    else:
        candidate = materialize_assessment(deepcopy(data), shared, source)
    return source, candidate


def validator_for(row):
    def validate(data):
        materialize(row, data)  # Full schema/semantic check before acceptance.
        return data  # Producer output, never zero-cost demo run metadata.
    return validate


class ExperimentRunner:
    """One process at a time, one ledger, retry count separate from total calls."""
    def __init__(self, dispatch, requests, policy, transport):
        self.dispatch, self.requests = dispatch, requests
        self.rows = {r["id"]: r for r in dispatch["requests"]}
        if (type(dispatch.get("budget_rub")) is not int or dispatch["budget_rub"] not in (150, 200)
                or policy.budget_micro_rub != dispatch["budget_rub"] * 1_000_000
                or policy.max_total_calls != 20 or policy.max_attempts != 2
                or policy.approval_ref != dispatch["approval_ref"]):
            raise ValueError("experiment_policy_mismatch")
        ledger = Path(dispatch["common_ledger"])
        ledger.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(ledger, 0o700)
        self.guard = CallGuard(ledger, policy)
        os.chmod(self.guard.db, 0o600)
        self.transport = transport
        self.execution_config = dict(controller_version=VERSION,transport_version=TRANSPORT_VERSION,
            approval_ref=dispatch["approval_ref"],dispatch_sha256=contract.digest(dispatch),
            policy_sha256=contract.digest(asdict(policy)),
            code_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                ("app/hubris_transport.py","app/l04_runner.py","app/provider_requests.py")})
        _private_write(ledger/"execution_config.json",canonical(self.execution_config).encode())

    def _latest(self, request):
        with closing(sqlite3.connect(self.guard.db)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM calls WHERE fingerprint=? ORDER BY number DESC LIMIT 1", (request.fingerprint,)).fetchone()
            retries = con.execute("SELECT COUNT(*) FROM calls WHERE retry_of IS NOT NULL").fetchone()[0]
        return row, retries

    def execute(self, request_id, *, retry=False):
        if request_id not in self.rows:
            raise ValueError("request_not_in_approved_plan")
        row, request = self.rows[request_id], self.requests[request_id]
        with (self.guard.root / "experiment.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous, retries = self._latest(request)
            if retry:
                if retries >= 4:
                    return dict(status="retry_reserve_exhausted")
                # L04 is stricter than generic M14: no malformed/truncated/
                # mathematical or uncertain-cost answer is retried here.
                if not previous or previous["state"] != "settled" or previous["status"] != "retryable_http_error":
                    return dict(status="experiment_retry_forbidden")
                if previous["retry_after"] > time.time()+30:
                    return dict(status="provider_retry_delay_too_long")
                delay = max(previous["created"]+5-time.time(), previous["retry_after"]-time.time(), 0)
                if delay:
                    time.sleep(delay)
            elif previous is None:
                # A rejected configuration remains a failed primary outcome;
                # stop further requests of that profile, no silent downgrade.
                for other in self.dispatch["requests"]:
                    if other["profile"] == row["profile"]:
                        state, _ = self._latest(self.requests[other["id"]])
                        if state and state["failure_reason"] in {"terminal_http_error", "model_mismatch", "output_bound_breached"}:
                            return dict(status="profile_stopped")
            outcome = self.guard.execute(request, self.transport, validator_for(row), retry=retry)
            report = dict(id=request_id, **asdict(outcome))
            report.pop("value", None)
            if outcome.status == "succeeded":
                source, candidate = materialize(row, outcome.value)
                calls = self.guard.contract_calls(fingerprints=[request.fingerprint])
                if self.guard.policy.mode == "live":
                    settled, _ = self._latest(request)
                    candidate["id"] = "m17-result-"+contract.digest(dict(request=request.fingerprint,
                        attempt=outcome.attempt_id,response=outcome.value))[:24]
                    candidate["created_at"] = datetime.fromtimestamp(settled["created"],timezone.utc).isoformat().replace("+00:00","Z")
                    cost = self.guard.snapshot(fingerprints=[request.fingerprint])["cost"]
                    candidate["run"] = dict(id="m17-"+request.fingerprint[:16], mode="live",
                        source_ref="m17:provider-through-m14", pipeline_version=VERSION,
                        configuration_sha256=contract.digest(self.execution_config),
                        configuration_ref="execution_config.json", calls=calls, cost=cost,
                        timing=dict(wall_ms=sum(c["latency_ms"] or 0 for c in calls),human_active_ms=None))
                    for collection in (candidate["units"], candidate["interpretations"], candidate["relations"]):
                        for item in collection:
                            if item.get("source", {}).get("ref", "").startswith("m05:offline-"):
                                item["source"]["ref"] = "m17:response:"+outcome.attempt_id
                    for call in calls:
                        call["settings"].update(transport_version=TRANSPORT_VERSION,controller_version=VERSION)
                    if candidate["score"]["value"] is not None:
                        candidate["score"]["source_ref"] = "m17:response:"+outcome.attempt_id
                    if contract.validate(candidate, source):
                        raise ValueError("live_result_contract_invalid")
                    target = self.guard.artifacts / (outcome.attempt_id+"-m01.json")
                    _private_write(target, canonical(candidate).encode())
                    report["result_ref"] = str(target.relative_to(self.guard.root))
                else:
                    report["synthetic_only"] = True
            report["accounting"] = self.guard.snapshot()
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Execute only if all billing evidence is verified")
    parser.add_argument("--retry", metavar="APPROVED_REQUEST_ID", help="One eligible retry from the shared reserve")
    parser.add_argument("--credential-file", type=Path, default=Path("/var/empty/kexam-not-configured.env"))
    args = parser.parse_args()
    status = readiness()
    if not args.run or not status["launch_ready"]:
        print(json.dumps(status,ensure_ascii=False)); return
    dispatch, requests = load_plan()
    bounds = json.loads(BOUNDS_PATH.read_text())
    policy = billing_policy(dispatch, bounds)
    key = read_credential(args.credential_file)
    transport = HubrisTransport(key, allowed_body_hashes=[r["body_sha256"] for r in dispatch["requests"]],
                                total_output_bound_verified=True)
    runner = ExperimentRunner(dispatch, requests, policy, transport)
    for request_id in ([args.retry] if args.retry else list(requests)):
        report = runner.execute(request_id, retry=bool(args.retry))
        print(json.dumps(report,ensure_ascii=False))
        if report["status"] in {"cost_unknown", "unresolved_spending", "ceiling_breached", "budget_limit", "total_call_limit"}:
            break


if __name__ == "__main__":
    main()
