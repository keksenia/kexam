"""Run public offline regressions with explicit synthetic control-plane fixtures.

The historical tests also contain checks against private photos and exact
private authorizations. Those named tests are skipped, never silently passed.
No production function is patched outside the lifetime of this test runner.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRIVATE_TEST_NAMES = {
    "test_l04_no_private_case_labels_or_evaluation_metadata",
    "test_l04_r13_literal_and_confirmed_meaning_remain_separate",
    "test_l04_baselines_are_valid_and_coverage_honest",
    "test_l04_e212_retains_two_pages_and_independent_chains",
    "test_l04_same_bytes_and_hashes_for_g_and_d",
    "test_all_real_cases_have_paired_content_and_images",
    "test_preparation_repeat_does_not_overwrite_and_budget_matches",
    "test_real_examples_and_synthetic_states_validate",
    "test_infinite_endpoint_open_and_nonzero_pi_period",
}
PRIVATE_CLASSES = {"SchoolAuthorizationTests", "NextSchoolAuthorizationTests", "RunnerTests"}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def synthetic_controls(stack):
    # No real historical call qualifies for these invented identifiers.
    requests = [dict(provider=p, model=m, stage=s)
                for p, m, stages in (
                    ("deepseek", "deepseek-flash", ("other", "grading")),
                    ("google", "gemini-3.8-flash", ("statement_ocr", "solution_ocr")),
                ) for s in stages]
    reserve = dict(call_id="public-synthetic-no-historical-call", fingerprint="0" * 64,
                   original_row_sha256="0" * 64, reserve_micro_rub=0,
                   allowed_requests=requests)
    for name in ("app.w06_live_reserve.authorization", "app.independent_checking.w06_authorization"):
        stack.enter_context(patch(name, return_value=reserve))
    for name in ("app.live_reserves.authorization", "app.independent_checking.live_reserve_authorization"):
        stack.enter_context(patch(name, return_value={"reserves": []}))


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for folder in ("app/tests", "contracts/ege_v1/tests", "exact_checks/tests"):
        for case in flatten(loader.discover(str(ROOT / folder), top_level_dir=str(ROOT / folder))):
            if (case._testMethodName in PRIVATE_TEST_NAMES
                    or case.__class__.__name__ in PRIVATE_CLASSES):
                case = unittest.FunctionTestCase(
                    lambda: None, description=case.id())
                case._testFunc = unittest.skip("Private source photos or signed authorizations are excluded")(case._testFunc)
            suite.addTest(case)
    # This one regression tests historical 200→300 budget amendment semantics.
    # Supply the old synthetic starting value only within that test class.
    import test_condition_blocks
    original_approval = test_condition_blocks.BudgetIncreaseTests.approval
    def fixture_approval(self, **amendment):
        import app.live_checking as live
        original_file = Path.read_text
        def read(path, *args, **kwargs):
            value = original_file(path, *args, **kwargs)
            if path == ROOT / "planning/l05/AUTHORIZATION.json":
                data = json.loads(value)
                data["budget_rub"] = 200
                return json.dumps(data)
            return value
        with patch.object(Path, "read_text", read):
            return original_approval(self, **amendment)
    test_condition_blocks.BudgetIncreaseTests.approval = fixture_approval
    with ExitStack() as stack:
        synthetic_controls(stack)
        # Local HTTP tests may use loopback. External socket connections fail.
        original = socket.socket.connect

        def loopback_only(sock, address):
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("External network is forbidden in the offline suite")
            return original(sock, address)

        stack.enter_context(patch("socket.socket.connect", loopback_only))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(json.dumps(dict(tests=result.testsRun, skipped=len(result.skipped),
                          failures=len(result.failures), errors=len(result.errors),
                          external_model_calls=0), sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
