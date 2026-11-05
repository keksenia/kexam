"""M06 mathematics: exact k ranges, open ends, negative and zero periods, circle points.

Each property is also cross-checked by an independent naive method (enumerating k in
a wide window and testing membership directly), so a shared mistake in the k-range
algebra would be caught.
"""
from fractions import Fraction as F
import itertools
import random
import unittest

from exact_checks.tests import support as s
from exact_checks import trig as T
from exact_checks.rational import InvalidInput, ResourceLimit


def naive(families, interval, window=400):
    roots = set()
    for offset, period in families:
        for k in range(-window, window + 1):
            value = offset + period * k
            if interval.contains(value):
                roots.add(value)
    return sorted(roots)


class KRange(unittest.TestCase):
    def test_negative_period_reverses_bounds(self):
        # x = π(1/3 − 2k) on [−3π; 2π): −5/6 < k ≤ 5/3 → k = 0, 1.
        iv = T.PiInterval(F(-3), F(2), True, False)
        self.assertEqual(T.k_range(F(1, 3), F(-2), iv), (0, 1))
        self.assertEqual(T.select_roots([(F(1, 3), F(-2))], iv, []).selected, [F(-5, 3), F(1, 3)])

    def test_open_and_closed_ends_on_exact_hits(self):
        # x = πk on [0; 2π]: k = 0, 1, 2. Opening either end removes exactly that endpoint.
        for left_closed, right_closed, expected in [(True, True, [0, 1, 2]), (False, True, [1, 2]),
                                                    (True, False, [0, 1]), (False, False, [1])]:
            with self.subTest(left_closed=left_closed, right_closed=right_closed):
                iv = T.PiInterval(F(0), F(2), left_closed, right_closed)
                self.assertEqual(T.select_roots([(F(0), F(1))], iv, []).selected, [F(v) for v in expected])

    def test_degenerate_and_empty_intervals(self):
        point = T.PiInterval(F(1), F(1), True, True)
        empty = T.PiInterval(F(1), F(1), True, False)
        self.assertEqual(T.select_roots([(F(0), F(1))], point, []).selected, [F(1)])
        self.assertEqual(T.select_roots([(F(0), F(1))], empty, []).selected, [])
        with self.assertRaises(InvalidInput):
            T.PiInterval(F(2), F(1), True, True)  # reversed ends are never swapped silently

    def test_zero_period_is_one_value_not_many_roots(self):
        iv = T.PiInterval(F(0), F(2), True, True)
        self.assertEqual(T.select_roots([(F(1), F(0))], iv, []).selected, [F(1)])
        self.assertEqual(T.select_roots([(F(3), F(0))], iv, []).selected, [])

    def test_union_of_overlapping_families_without_duplicates(self):
        # sin x = 1 or cos x = 0 style overlap: π/2 + 2πk and π/2 + πk share points.
        iv = T.PiInterval(F(-2), F(2), True, True)
        result = T.select_roots([(F(1, 2), F(2)), (F(1, 2), F(1))], iv, [])
        self.assertEqual(result.selected, [F(-3, 2), F(-1, 2), F(1, 2), F(3, 2)])

    def test_union_is_not_intersection(self):
        # 2πk ∪ 3πk on [0; 6π] = {0, 2, 3, 4, 6}·π, whereas the intersection would be {0, 6}·π.
        iv = T.PiInterval(F(0), F(6), True, True)
        sel = T.select_roots([(F(0), F(2)), (F(0), F(3))], iv, [F(0), F(6)])
        self.assertEqual(sel.selected, [F(v) for v in (0, 2, 3, 4, 6)])
        self.assertEqual(sel.missing, [F(2), F(3), F(4)])
        self.assertFalse(sel.matches)

    def test_missing_extra_and_duplicates_are_reported_separately(self):
        iv = T.PiInterval(F(-5), F(-7, 2), True, True)
        # Synthetic family −π/3 + 2πk chosen so the numbers match the W003 №6 discussion
        # (−7π/3 written, −13π/3 needed: wrong turn, correct arithmetic). The real W003
        # family is not re-read here.
        sel = T.select_roots([(F(-1, 3), F(2))], iv, [F(-7, 3), F(-7, 3)])
        self.assertEqual(sel.selected, [F(-13, 3)])
        self.assertEqual(sel.missing, [F(-13, 3)])
        self.assertEqual(sel.extra, [F(-7, 3)])
        self.assertEqual(sel.duplicate_claims, 1)

    def test_candidate_limit_is_checked_before_enumeration(self):
        iv = T.PiInterval(F(0), F(10**6), True, True)
        with self.assertRaises(ResourceLimit):
            T.select_roots([(F(0), F(1))], iv, [])
        # Exactly 256 candidates is allowed; 257 is not.
        self.assertEqual(len(T.select_roots([(F(0), F(1))], T.PiInterval(F(0), F(255), True, True), []).selected), 256)
        with self.assertRaises(ResourceLimit):
            T.select_roots([(F(0), F(1))], T.PiInterval(F(0), F(256), True, True), [])

    def test_matches_naive_enumeration_on_random_cases(self):
        rng = random.Random(20261007)
        denominators = [1, 2, 3, 4, 6, 12]
        for _ in range(3000):
            def rnd(low, high):
                d = rng.choice(denominators)
                return F(rng.randint(low * d, high * d), d)
            left, right = sorted([rnd(-6, 6), rnd(-6, 6)])
            iv = T.PiInterval(left, right, rng.random() < 0.5, rng.random() < 0.5)
            families = []
            for _ in range(rng.randint(1, 3)):
                period = rng.choice([F(0), rnd(-3, 3) or F(1, 2)])
                families.append((rnd(-4, 4), period))
            fast = T.select_roots(families, iv, [])
            with self.subTest(families=families, interval=iv):
                self.assertEqual(fast.selected, naive(families, iv))


class Circle(unittest.TestCase):
    def test_same_point_different_real_numbers(self):
        c = T.circle_points(F(-19, 4), F(5, 4))
        self.assertTrue(c.same_point)
        self.assertFalse(c.same_real_number)
        self.assertEqual((c.turns, c.angle_residue, c.point_residue), (F(-3), F(5, 4), F(5, 4)))

    def test_half_turn_is_a_different_point(self):
        c = T.circle_points(F(1, 2), F(-1, 2))
        self.assertFalse(c.same_point)  # π/2 and −π/2 differ by π

    def test_residues_lie_in_zero_to_two(self):
        for a, b in itertools.product([F(n, 6) for n in range(-30, 31)], repeat=2):
            c = T.circle_points(a, b)
            self.assertTrue(0 <= c.angle_residue < 2 and 0 <= c.point_residue < 2)
            self.assertEqual(c.same_point, c.angle_residue == c.point_residue)


class ThroughProtocol(unittest.TestCase):
    def run_op(self, request):
        inp, result = s.base(13, "m06-protocol")
        fact, _, response = s.run(request["operation"], request, result)
        self.assertEqual(s.m01.validate(s.attach(result, fact), inp), [])
        return response

    def test_non_canonical_numbers_are_invalid_input(self):
        for bad in (s.q(2, 4), s.q(0, 5), {"numerator": "-0", "denominator": "1"}):
            with self.subTest(bad=bad):
                request = s.args("m06.pi_interval_membership", point={"basis": "pi", "coefficient": bad},
                                 interval=s.pi_interval(0, 2), expected_member=True)
                response = self.run_op(request)
                self.assertEqual((response["status"], response["reason_code"]), ("unknown", "invalid_input"))

    def test_reversed_interval_and_wrong_basis_are_invalid_input(self):
        reversed_iv = s.args("m06.select_pi_roots", families=[{"offset": s.q(0), "period": s.q(1)}],
                             interval=s.pi_interval(2, 0), claimed_roots=[])
        self.assertEqual(self.run_op(reversed_iv)["reason_code"], "invalid_input")
        rational_end = s.pi_interval(0, 2)
        rational_end["left"]["basis"] = "rational"
        wrong_basis = s.args("m06.select_pi_roots", families=[{"offset": s.q(0), "period": s.q(1)}],
                             interval=rational_end, claimed_roots=[])
        self.assertEqual(self.run_op(wrong_basis)["reason_code"], "invalid_input")

    def test_output_wider_than_40_digits_is_resource_limit(self):
        big = 10**39 + 7
        # Restrict the interval to exactly k=0,1. A wide interval would hit the
        # 256-candidate limit before exercising output-fraction serialization.
        interval = T.PiInterval(F(1, big), F(2, big), True, True)
        selected = T.select_roots([(F(1, big), F(1, big + 2))], interval, []).selected
        self.assertEqual(len(selected), 2)
        self.assertGreater(len(str(selected[1].denominator)), 40)
        request = s.args("m06.select_pi_roots", families=[{"offset": s.q(1, big), "period": s.q(1, 10**39 + 9)}],
                         interval=s.pi_interval((1, big), (2, big)), claimed_roots=[])
        response = self.run_op(request)
        self.assertEqual((response["status"], response["reason_code"], response["computed"]),
                         ("unknown", "resource_limit", None))
        self.assertIn("40 цифр", " ".join(response["evidence"]))

    def test_wrong_turn_selection_is_contradicted_with_exact_evidence(self):
        request = s.args("m06.select_pi_roots", families=[{"offset": s.q(-1, 3), "period": s.q(2)}],
                         interval=s.pi_interval(-5, (-7, 2)), claimed_roots=[s.pi(-7, 3)])
        response = self.run_op(request)
        self.assertEqual(response["status"], "contradicted")
        self.assertEqual(response["computed"]["missing_roots"], [s.pi(-13, 3)])
        self.assertEqual(response["computed"]["extra_roots"], [s.pi(-7, 3)])
        self.assertIn("−13π/3", " ".join(response["evidence"]))


if __name__ == "__main__":
    unittest.main()
