"""M07 mathematics: canonical exact sets, set operations and the original denominator.

The canonical form is checked against an independent pointwise oracle: membership
is compared at every breakpoint, between breakpoints and beyond both ends, on
random sets with integer and half-integer ends.
"""
from fractions import Fraction as F
import random
import unittest

from exact_checks.tests import support as s
from exact_checks import sets as S
from exact_checks.rational import InvalidInput, ResourceLimit


def iv(a, b, lc=True, rc=True):
    return S.Interval(None if a is None else F(a), None if b is None else F(b), lc, rc)


def es(intervals=(), points=()):
    return S.ExactSet(tuple(intervals), tuple(F(p) for p in points))


def samples(*sets):
    values = sorted(set().union(*(x.breakpoints() for x in sets)) or {F(0)})
    probe = [values[0] - 1, values[-1] + 1, *values]
    probe += [(a + b) / 2 for a, b in zip(values, values[1:])]
    return probe


class CanonicalForm(unittest.TestCase):
    def test_touching_open_ends_keep_the_removed_point(self):
        value = S.normalize(es([iv(0, 1, False, False), iv(1, 2, False, False)]))
        self.assertEqual(value, es([iv(0, 1, False, False), iv(1, 2, False, False)]))
        self.assertFalse(value.contains(F(1)))

    def test_touching_with_one_closed_side_merges(self):
        self.assertEqual(S.normalize(es([iv(0, 1, False, True), iv(1, 2, False, False)])), es([iv(0, 2, False, False)]))

    def test_point_closes_an_open_end_or_is_absorbed(self):
        self.assertEqual(S.normalize(es([iv(0, 1, False, False)], [1])), es([iv(0, 1, False, True)]))
        self.assertEqual(S.normalize(es([iv(0, 2)], [1, 1])), es([iv(0, 2)]))
        self.assertEqual(S.normalize(es([], [3, 3, 1])), es([], [1, 3]))

    def test_infinite_and_empty_and_degenerate(self):
        self.assertEqual(S.normalize(es([iv(None, None, False, False)])), es([iv(None, None, False, False)]))
        self.assertEqual(S.normalize(es([iv(1, 1, True, False)])), es())          # empty
        self.assertEqual(S.normalize(es([iv(1, 1, True, True)])), es([], [1]))     # closed point
        self.assertEqual(S.normalize(es()), es())

    def test_invalid_shapes(self):
        with self.assertRaises(InvalidInput):
            iv(None, 1, True, True)    # −∞ cannot be included
        with self.assertRaises(InvalidInput):
            iv(2, 1)                   # reversed ends

    def test_random_sets_against_pointwise_oracle(self):
        rng = random.Random(7102026)
        def rnd_set():
            intervals = []
            for _ in range(rng.randint(0, 4)):
                a, b = sorted([F(rng.randint(-8, 8), rng.choice([1, 2])) for _ in range(2)])
                if rng.random() < 0.15:
                    a = None
                if rng.random() < 0.15:
                    b = None
                intervals.append(iv(a, b, a is not None and rng.random() < 0.5, b is not None and rng.random() < 0.5))
            return es(intervals, [F(rng.randint(-8, 8), rng.choice([1, 2])) for _ in range(rng.randint(0, 3))])
        for _ in range(1500):
            a, b, c = rnd_set(), rnd_set(), rnd_set()
            n = S.normalize(a)
            u, i = S.union([a, b, c]), S.intersection([a, b, c])
            d = S.symmetric_difference(a, b)
            for x in samples(a, b, c):
                self.assertEqual(n.contains(x), a.contains(x))
                self.assertEqual(u.contains(x), a.contains(x) or b.contains(x) or c.contains(x))
                self.assertEqual(i.contains(x), a.contains(x) and b.contains(x) and c.contains(x))
                self.assertEqual(d.contains(x), a.contains(x) != b.contains(x))
            # Uniqueness: normal form is a fixed point, and equality ⇔ empty difference.
            self.assertEqual(S.normalize(n), n)
            self.assertEqual(S.normalize(u) == S.normalize(S.union([c, b, a])), True)
            self.assertEqual(d.empty, S.normalize(a) == S.normalize(b))
            # Canonical intervals are sorted, disjoint and never mergeable.
            for left, right in zip(n.intervals, n.intervals[1:]):
                self.assertTrue(left.right is not None and right.left is not None and left.right <= right.left)
                if left.right == right.left:
                    self.assertFalse(left.right_closed or right.left_closed or left.right in n.points)


class Denominator(unittest.TestCase):
    def test_horner_exact(self):
        self.assertEqual(S.evaluate_polynomial([F(-1), F(1)], F(1)), 0)
        self.assertEqual(S.evaluate_polynomial([F(-1), F(0), F(1)], F(1, 2)), F(-3, 4))
        self.assertEqual(S.evaluate_polynomial([F(0)], F(5)), 0)                 # Q ≡ 0
        self.assertEqual(S.evaluate_polynomial([F(2), F(0), F(0)], F(7)), 2)     # zero high terms


class ThroughProtocol(unittest.TestCase):
    def run_op(self, request):
        inp, result = s.base(15, "m07-protocol")
        fact, _, response = s.run(request["operation"], request, result)
        self.assertEqual(s.m01.validate(s.attach(result, fact), inp), [])
        return response

    def test_union_point_closes_bracket(self):
        r = self.run_op(s.args("m07.set_union", sets=[s.exact_set([s.interval(0, 1, False, False)]), s.exact_set(points=[1])],
                               claimed_set=s.exact_set([s.interval(0, 1, False, True)])))
        self.assertEqual(r["status"], "verified")

    def test_strictness_error_is_a_single_point_difference(self):
        r = self.run_op(s.args("m07.set_equal", left=s.exact_set([s.interval(None, 1, False, True)]),
                               right=s.exact_set([s.interval(None, 1, False, False)])))
        self.assertEqual(r["status"], "contradicted")
        self.assertEqual(r["computed"]["symmetric_difference"], s.exact_set(points=[1]))

    def test_lost_isolated_point_is_detected(self):
        # (−∞; −1] ∪ {2} versus (−∞; −1]: the isolated admissible point 2 is missing.
        r = self.run_op(s.args("m07.set_equal", left=s.exact_set([s.interval(None, -1, False, True)], [2]),
                               right=s.exact_set([s.interval(None, -1, False, True)])))
        self.assertEqual(r["computed"]["symmetric_difference"], s.exact_set(points=[2]))

    def test_closed_infinite_end_is_invalid_input(self):
        r = self.run_op(s.args("m07.set_membership", set=s.exact_set([s.interval(None, 1, True, True)]),
                               point=s.real(0), expected_member=True))
        self.assertEqual((r["status"], r["reason_code"]), ("unknown", "invalid_input"))

    def test_membership_expected_false(self):
        r = self.run_op(s.args("m07.set_membership", set=s.exact_set([s.interval(0, 1, True, False)]),
                               point=s.real(1), expected_member=False))
        self.assertEqual((r["status"], r["computed"]), ("verified", {"member": False}))

    def test_input_component_limit(self):
        many = s.exact_set(points=list(range(65)))
        r = self.run_op(s.args("m07.set_equal", left=many, right=many))
        self.assertEqual((r["status"], r["reason_code"]), ("unknown", "resource_limit"))

    def test_output_component_limit(self):
        # 60 + 60 isolated points pass the input limit (120 ≤ 128) but their
        # symmetric difference has 120 components; with the computed union it exceeds 128.
        a, b = s.exact_set(points=list(range(0, 120, 2))), s.exact_set(points=list(range(1, 120, 2)))
        r = self.run_op(s.args("m07.set_union", sets=[a], claimed_set=b))
        self.assertEqual((r["status"], r["reason_code"]), ("unknown", "resource_limit"))

    def test_denominator_cancellation_does_not_restore_point(self):
        expression = {"representation": "original_polynomial_quotient", "variable": "x",
                      "numerator": [s.q(-1), s.q(0), s.q(1)], "denominator": [s.q(-1), s.q(1)]}
        r = self.run_op(s.args("m07.denominator_nonzero", expression=expression, point=s.real(1)))
        self.assertEqual((r["status"], r["computed"]["nonzero"]), ("contradicted", False))
        self.assertIn("x − 1", r["summary"])

    def test_denominator_degree_16_with_large_values(self):
        coefficients = [s.q(1)] + [s.q(0)] * 15 + [s.q(1)]          # 1 + t^16
        expression = {"representation": "original_polynomial_quotient", "variable": "t",
                      "numerator": [s.q(1)], "denominator": coefficients}
        ok = self.run_op(s.args("m07.denominator_nonzero", expression=expression, point=s.real(10)))
        self.assertEqual(ok["computed"]["denominator_value"], s.q(10**16 + 1))
        wide = self.run_op(s.args("m07.denominator_nonzero", expression=expression, point=s.real(10**3)))
        self.assertEqual((wide["status"], wide["reason_code"]), ("unknown", "resource_limit"))  # 10^48 > 40 digits

    def test_malformed_numerator_is_invalid(self):
        expression = {"representation": "original_polynomial_quotient", "variable": "x",
                      "numerator": [s.q(2, 4)], "denominator": [s.q(1)]}
        r = self.run_op(s.args("m07.denominator_nonzero", expression=expression, point=s.real(0)))
        self.assertEqual(r["reason_code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
