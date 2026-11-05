"""Free M14 experiment harness: built-in synthetic fixtures ONLY, no networking."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import tempfile
import time

from .call_guard import CallGuard, Policy, ProviderResponse, StageRequest
from .checking_service import CheckingService


def fixture_request(label="good", *, complete=True):
    return StageRequest("other", "synthetic-fixture", {
        "model": "synthetic-model", "max_tokens": 128,
        "messages": [{"role": "user", "content": "Synthetic M14 fixture: " + label}],
    }, {"input": "synthetic", "reading": [], "criteria": "none", "images": [],
        "corrections": [], "interpretations": []},
        {"contract": "2.0.0", "pipeline": "fixture-1", "prompt": "fixture-1",
         "code": "fixture-1", "validator": "fixture-1"}, complete)


def fixture_policy(requests, **changes):
    values = dict(enabled=True, mode="fixture", approval_ref="SYNTHETIC-NO-MONEY",
        budget_micro_rub=3_000_000, max_attempts=2, max_total_calls=6,
        max_output_tokens=128, max_response_bytes=4096, timeout_seconds=0.05,
        operation_timeout_seconds=5, expires_at=time.time() + 60,
        ceilings={r.fingerprint: {"micro_rub": 1_000_000, "evidence_ref": "synthetic-fixture-ceiling"} for r in requests})
    values.update(changes)
    return Policy(**values)


def fixture_validator(data):
    if not isinstance(data, dict) or set(data) != {"text"} or not isinstance(data["text"], str):
        raise ValueError("Invalid synthetic stage output")
    return data


class FixtureTransport:
    kind = "fixture"
    single_attempt = True

    def __init__(self, scenario="good"):
        self.scenario = scenario
        self.calls = 0

    def __call__(self, payload, *, timeout_seconds):
        self.calls += 1
        if self.scenario == "timeout":
            time.sleep(timeout_seconds * 2)
        return ProviderResponse('{"text":"synthetic output"}' if self.scenario != "invalid" else '{"score":0}',
            usage={"prompt_tokens": 10, "completion_tokens": 20,
                   "completion_tokens_details": {"reasoning_tokens": 8}},
            cost_kopecks=None if self.scenario == "unknown_cost" else "10.25",
            model=payload["model"], response_id="synthetic-response")


def run_demo(root):
    results = {}
    for scenario in ("good", "invalid", "timeout", "unknown_cost", "incomplete"):
        request = fixture_request(scenario, complete=scenario != "incomplete")
        policy = fixture_policy([request])
        transport = FixtureTransport(scenario)
        location = Path(root) / scenario
        service = CheckingService(location, policy=policy, transport=transport, validator=fixture_validator)
        first = service.run_stage(request)
        duplicate = service.run_stage(request)
        results[scenario] = {"first": asdict(first), "duplicate": asdict(duplicate),
                             "transport_calls": transport.calls,
                             "simulated_accounting": CallGuard(location, policy).snapshot()}
    return {"mode": "synthetic_only", "real_api_calls": 0, "real_cost_rub": "0", "scenarios": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save synthetic evidence JSON; ledger remains temporary")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ege-m14-") as directory:
        result = run_demo(directory)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
