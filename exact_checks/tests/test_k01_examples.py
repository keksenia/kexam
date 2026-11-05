"""All 20 accepted K01 fixtures through the same path the M04 pipeline uses."""
from copy import deepcopy
import importlib.util
import json
import sys
import unittest

from exact_checks.tests import support as s  # sets dont_write_bytecode first
from contracts.ege_v1 import validate as m01
from exact_checks import protocol as P

# K01's own independent oracle (Fraction-based, written by another agent) loaded read-only.
_spec = importlib.util.spec_from_file_location("k01_oracle", s.ROOT / "planning/k01/check_fixtures.py")
oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oracle)
STATUS = {"verified": "supported", "contradicted": "contradicted", "unknown": "unknown"}


class K01Examples(unittest.TestCase):
    def test_examples_cover_every_operation_and_status(self):
        self.assertEqual({e["operation"] for e in s.EXAMPLES}, set(P.OPERATIONS))
        self.assertEqual(len(s.EXAMPLES), 20)

    def test_each_example_exactly(self):
        for example in s.EXAMPLES:
            with self.subTest(example["id"]):
                inp, result, request = s.for_example(example)
                fact, k01_request, response = s.run(example["operation"], request, result)
                # 1. Exact agreement with the accepted expected answer.
                self.assertEqual(response["status"], example["expected_status"])
                self.assertEqual(response["reason_code"], example["reason_code"])
                self.assertEqual(response["computed"], example["expected_computed"])
                # 2. Typed K01 request/response.
                self.assertEqual(list(P.REQUEST_SCHEMA.iter_errors(k01_request)), [])
                self.assertEqual(list(P.RESPONSE_SCHEMA.iter_errors(response)), [])
                self.assertEqual(response["request_sha256"], m01.digest(k01_request))
                self.assertEqual(k01_request["reading_sha256"], m01.reading_digest(result))
                # 3. The fact is valid M01 inside the full synthetic result.
                self.assertEqual(fact["status"], STATUS[response["status"]])
                self.assertEqual(fact["code_check"]["arguments"], example["arguments"])
                self.assertEqual(m01.validate(s.attach(result, fact), inp), [])
                # 4. The independent K01 oracle accepts our output in place of the fixture.
                mine = deepcopy(example)
                mine.update(expected_status=response["status"], reason_code=response["reason_code"],
                            expected_computed=response["computed"])
                self.assertTrue(oracle.fixture_valid(mine, P.LIMITS))

    def test_promoting_a_gated_fact_is_rejected_by_m01(self):
        """The gate agrees with M01: a non-unknown fact under an unknown premise is invalid."""
        for example in s.EXAMPLES:
            if example["gate"] == "resolved":
                continue
            with self.subTest(example["id"]):
                inp, result, request = s.for_example(example)
                fact, _, response = s.run(example["operation"], request, result)
                self.assertEqual(response["reason_code"], "unresolved_premise")
                for promoted in ("supported", "contradicted"):
                    bad = dict(fact, status=promoted)
                    self.assertTrue(m01.validate(s.attach(result, bad), inp))

    def test_same_math_without_gate_is_computed(self):
        """Gated fixtures become exact once the premise is established, proving the gate caused unknown."""
        for example in s.EXAMPLES:
            if example["gate"] == "resolved":
                continue
            with self.subTest(example["id"]):
                inp, result, request = s.for_example(example)
                result["units"][0]["reading_status"] = "clear"
                result["assumptions"][0].update(origin="student_unit", status="established")
                fact, _, response = s.run(example["operation"], request, result)
                self.assertIn(response["status"], {"verified", "contradicted"})
                self.assertEqual(m01.validate(s.attach(result, fact), inp), [])


if __name__ == "__main__":
    unittest.main()
