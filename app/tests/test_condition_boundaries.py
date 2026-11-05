"""Exact interval selection regressions; synthetic conditions, no model calls."""
import unittest

from app.live_condition import solve_condition
from app.tests.test_fresh_checking import condition


class ExactBoundaryTests(unittest.TestCase):
    def assert_selection(self, left, right, roots, **changes):
        for left_open in (False, True):
            for right_open in (False, True):
                with self.subTest(left=left, right=right,
                                  left_open=left_open, right_open=right_open):
                    result = solve_condition(condition(interval=dict(
                        left=left, right=right, left_open=left_open,
                        right_open=right_open), **changes))
                    self.assertEqual(result["status"], "solved", result)
                    selected = roots[int(left_open):len(roots)-int(right_open)]
                    expected = "{" + ", ".join(selected) + "}" if selected else "EmptySet"
                    self.assertEqual(result["answer"], expected)
                    self.assertEqual(result["empty"], not selected)
                    self.assertEqual(result["version"], "condition-solver-1.1.3")

    def test_unsimplified_positive_bounds_all_endpoint_flags(self):
        self.assert_selection("(2*pi)/2", "(6*pi)/2", ["pi", "2*pi", "3*pi"])

    def test_unsimplified_negative_bounds_all_endpoint_flags(self):
        self.assert_selection("(-6*pi)/2", "(-2*pi)/2", ["-3*pi", "-2*pi", "-pi"])

    def test_radical_bounds_remain_exact(self):
        self.assert_selection("sqrt(4*pi**2)", "sqrt(16*pi**2)",
                              ["2*pi", "3*pi", "4*pi"])

    def test_open_interval_can_have_proven_empty_selection(self):
        self.assert_selection("(2*pi)/2", "(4*pi)/2", ["pi", "2*pi"])

    def test_excluded_original_denominator_root_is_not_restored(self):
        for right_open, expected in ((False, "{pi, 2*pi}"), (True, "{pi}")):
            with self.subTest(right_open=right_open):
                result = solve_condition(condition(lhs="sin(x)/x", interval=dict(
                    left="(2-2)/2", right="(4*pi)/2", left_open=False,
                    right_open=right_open)))
                self.assertEqual(result["status"], "solved", result)
                self.assertEqual(result["answer"], expected)
                self.assertIn("x", result["original_denominators"])

    def test_explicit_restriction_still_excludes_closed_endpoint(self):
        result = solve_condition(condition(restrictions=["x != pi"], interval=dict(
            left="(2*pi)/2", right="(6*pi)/2", left_open=False, right_open=False)))
        self.assertEqual(result["status"], "solved", result)
        self.assertEqual(result["answer"], "{2*pi, 3*pi}")

    def test_inequality_interval_preserves_all_endpoint_flags(self):
        for left_open in (False, True):
            for right_open in (False, True):
                with self.subTest(left_open=left_open, right_open=right_open):
                    result = solve_condition(condition("inequality", lhs="x*x", rhs="4",
                        relation="<=", interval=dict(left="-(2+2)/2", right="(2+2)/2",
                        left_open=left_open, right_open=right_open)))
                    name = {(False, False): "Interval", (True, False): "Interval.Lopen",
                            (False, True): "Interval.Ropen", (True, True): "Interval.open"}[
                                left_open, right_open]
                    self.assertEqual(result["status"], "solved", result)
                    self.assertEqual(result["answer"], f"{name}(-2, 2)")

    def test_invalid_bounds_remain_suspicious(self):
        for left, right in (("(6*pi)/2", "pi"), ("(2*pi)/2", "pi"),
                            ("x-x", "pi")):
            with self.subTest(left=left, right=right):
                result = solve_condition(condition(interval=dict(
                    left=left, right=right, left_open=False, right_open=False)))
                self.assertEqual(result["status"], "suspicious", result)
                self.assertEqual(result["reason"], "interval_order")


if __name__ == "__main__":
    unittest.main()
