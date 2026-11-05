"""Premise gate: when exact mathematics is not allowed to speak, and why.

Every gate yields `unknown` with a specific reason, never a student error. A
context that cannot be attached to the M01 result at all is a technical failure.
"""
from copy import deepcopy
import unittest

from exact_checks.tests import support as s
from exact_checks import protocol as P
from exact_checks.operations import ExactOperation, OperationContextError

MEMBERSHIP = dict(point=s.pi(1), interval=s.pi_interval(0, 2), expected_member=True)


class Gates(unittest.TestCase):
    def setUp(self):
        self.inp, self.result = s.base(13, "gates")

    def reason(self, request, result=None, *, source_input=None):
        fact, _, response = s.run(request["operation"], request, result or self.result,
                                  source_input=source_input)
        self.assertEqual(s.m01.validate(s.attach(result or self.result, fact), self.inp), [])
        return response["status"], response["reason_code"]

    def test_baseline_is_verified(self):
        self.assertEqual(self.reason(s.args("m06.pi_interval_membership", **MEMBERSHIP)), ("verified", "exact_true"))

    def test_ambiguous_reading_without_conditional_branch(self):
        self.result["units"][0]["reading_status"] = "ambiguous"
        self.assertEqual(self.reason(s.args("m06.pi_interval_membership", **MEMBERSHIP)), ("unknown", "unresolved_reading"))

    def test_ambiguous_reading_inside_explicit_conditional_branch_is_computed(self):
        """K01: a fact may hold 'under this reading' when the reading is a recorded condition."""
        self.result["units"][0]["reading_status"] = "ambiguous"
        self.result["branches"].append({"id": "reading-a", "parent_id": "branch-main", "label": "Чтение A", "unit_ids": ["u1"],
                                        "assumption_ids": ["a-read"], "status": "active"})
        self.result["assumptions"] = [{"id": "a-read", "claim": "Читаем запись как 1.", "origin": "student_unit",
                                       "source_ref": "u1", "status": "conditional", "branch_id": "reading-a"}]
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"].update(branch_id="reading-a", assumption_ids=["a-read"])
        self.assertEqual(self.reason(request), ("verified", "exact_true"))

    def test_unknown_premise(self):
        self.result["assumptions"] = [{"id": "a1", "claim": "θ = 2x уже переведено в x.", "origin": "hypothesis",
                                       "source_ref": "u1", "status": "unknown", "branch_id": "branch-main"}]
        self.result["branches"][0]["assumption_ids"] = ["a1"]
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["assumption_ids"] = ["a1"]
        self.assertEqual(self.reason(request), ("unknown", "unresolved_premise"))

    def test_inactive_branch(self):
        self.result["branches"][0]["status"] = "superseded"
        self.assertEqual(self.reason(s.args("m06.pi_interval_membership", **MEMBERSHIP)), ("unknown", "unresolved_premise"))

    def test_dependency_not_supported(self):
        self.result["facts"] = [{"id": "f0", "unit_ids": ["u1"], "interpretation_ids": [], "claim": "Неизвестно.",
                                 "method": "model", "status": "unknown", "reason": "Нет данных.", "tool_version": None,
                                 "depends_on_fact_ids": [], "branch_id": "branch-main", "assumption_ids": [],
                                 "context_unit_ids": [], "claim_kind": "other", "domain": "Синтетика",
                                 "basis_sha256": "0" * 64}]
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["depends_on_fact_ids"] = ["f0"]
        self.assertEqual(self.reason(request), ("unknown", "unresolved_premise"))

    def test_unbound_argument_and_wrong_pointer(self):
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["bindings"] = [b for b in request["context"]["bindings"] if b["argument_pointer"] != "/interval"]
        self.assertEqual(self.reason(request), ("unknown", "source_mismatch"))
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["bindings"][0]["argument_pointer"] = "/nowhere"
        self.assertEqual(self.reason(request), ("unknown", "source_mismatch"))

    def test_nested_pointer_binding_is_accepted(self):
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["bindings"] = [{"argument_pointer": p, "source_kind": "unit", "source_ref": "u1"}
                                          for p in ("/point/coefficient", "/interval/left", "/interval/right",
                                                    "/interval/left_closed", "/interval/right_closed", "/expected_member")]
        self.assertEqual(self.reason(request), ("verified", "exact_true"))

    def test_unit_source_outside_fact_context(self):
        self.result["units"].append(s.unit("u2"))
        self.result["branches"][0]["unit_ids"].append("u2")
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["bindings"][0]["source_ref"] = "u2"  # u2 not in unit_ids
        self.assertEqual(self.reason(request), ("unknown", "source_mismatch"))

    def test_input_pointer_only_for_task_statement(self):
        """Student values cannot bypass the reading gate by pointing at the input."""
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        for binding in request["context"]["bindings"]:
            if binding["argument_pointer"] == "/interval":
                binding.update(source_kind="input_pointer", source_ref="/task/statement/text")
        self.assertEqual(self.reason(request, source_input=self.inp), ("verified", "exact_true"))
        request["context"]["bindings"][0].update(source_kind="input_pointer", source_ref="/pages/0")
        self.assertEqual(self.reason(request, source_input=self.inp), ("unknown", "source_mismatch"))

    def test_interpretation_inherits_reading_doubt(self):
        self.result["units"][0]["reading_status"] = "ambiguous"
        self.result["interpretations"] = [{"id": "i1", "unit_ids": ["u1"], "meaning": "Число π.", "status": "proposed",
                                           "source": {"kind": "synthetic", "ref": "test"}, "branch_id": "branch-main",
                                           "assumption_ids": [], "context_unit_ids": []}]
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        request["context"]["interpretation_ids"] = ["i1"]
        for binding in request["context"]["bindings"]:
            binding.update(source_kind="interpretation", source_ref="i1")
        self.assertEqual(self.reason(request), ("unknown", "unresolved_reading"))
        self.result["units"][0]["reading_status"] = "clear"
        self.assertEqual(self.reason(request), ("verified", "exact_true"))

    def test_circle_point_cannot_come_from_the_checked_label(self):
        request = s.args("m06.circle_point_equal", angle=s.pi(-19, 4), independent_point_angle=s.pi(5, 4))
        self.assertEqual(self.reason(request), ("unknown", "source_mismatch"))

    def test_circle_point_cannot_be_laundered_through_an_interpretation_of_the_label(self):
        self.result["interpretations"] = [{"id": "i1", "unit_ids": ["u1"], "meaning": "Отмеченная точка — 5π/4.",
                                           "status": "proposed", "source": {"kind": "synthetic", "ref": "test"},
                                           "branch_id": "branch-main", "assumption_ids": [], "context_unit_ids": []}]
        request = s.args("m06.circle_point_equal", angle=s.pi(-19, 4), independent_point_angle=s.pi(5, 4))
        request["context"]["interpretation_ids"] = ["i1"]
        request["context"]["bindings"][1].update(source_kind="interpretation", source_ref="i1")
        self.assertEqual(self.reason(request), ("unknown", "source_mismatch"))
        # A genuinely independent unit (e.g. the drawn point's own label) is accepted.
        self.result["units"].append(s.unit("u2"))
        self.result["branches"][0]["unit_ids"].append("u2")
        self.result["interpretations"][0]["unit_ids"] = ["u2"]
        request["context"]["unit_ids"] = ["u1", "u2"]
        self.assertEqual(self.reason(request), ("verified", "exact_true"))

    def test_stale_snapshot(self):
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        k01 = s.run("m06.pi_interval_membership", request, self.result)[1]
        changed = deepcopy(self.result)
        changed["units"][0]["literal"] = "changed"
        response = P.execute(k01, changed)
        self.assertEqual((response["status"], response["reason_code"]), ("unknown", "stale_input"))

    def test_unknown_operation_and_version(self):
        k01 = s.run("m06.pi_interval_membership", s.args("m06.pi_interval_membership", **MEMBERSHIP), self.result)[1]
        for change, reason in [({"operation": "m08.solve_everything"}, "unsupported_operation"),
                               ({"operation_version": "2.0.0"}, "unsupported_operation"),
                               ({"limits_profile": "unbounded"}, "invalid_input")]:
            with self.subTest(change=change):
                response = P.execute({**k01, **change}, self.result)
                self.assertEqual((response["status"], response["reason_code"]), ("unknown", reason))
                self.assertEqual(list(P.RESPONSE_SCHEMA.iter_errors(response)), [])

    def test_oversized_request(self):
        k01 = s.run("m06.pi_interval_membership", s.args("m06.pi_interval_membership", **MEMBERSHIP), self.result)[1]
        k01["context"]["bindings"] = k01["context"]["bindings"] * 400
        response = P.execute(k01, self.result)
        self.assertEqual(response["reason_code"], "resource_limit")

    def test_unattachable_context_is_a_technical_error_not_a_fact(self):
        operation = ExactOperation("m06.pi_interval_membership")
        for change in ({"branch_id": "missing"}, {"unit_ids": ["ghost"]}, {"bindings": []}):
            with self.subTest(change=change):
                request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
                request["context"].update(change)
                with self.assertRaises(OperationContextError):
                    operation.execute(request, self.result)

    def test_wrong_version_or_operation_is_rejected(self):
        operation = ExactOperation("m06.pi_interval_membership")
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        with self.assertRaises(OperationContextError):
            operation.execute(dict(request, version="1.0.0"), self.result)
        with self.assertRaises(OperationContextError):
            operation.execute(dict(request, operation="m07.set_equal"), self.result)

    def test_repeated_identical_request_gets_unique_fact_ids(self):
        request = s.args("m06.pi_interval_membership", **MEMBERSHIP)
        first = ExactOperation("m06.pi_interval_membership").execute(deepcopy(request), self.result)
        published = s.attach(self.result, first)
        second = ExactOperation("m06.pi_interval_membership").execute(deepcopy(request), published)
        self.assertNotEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
