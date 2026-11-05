"""Full periodic root families and original domain; synthetic, no model calls."""
import unittest

from app import condition_precheck as precheck
from app.live_condition import solve_condition
from app.tests.test_fresh_checking import condition


class PeriodicDomainTests(unittest.TestCase):
    def selected(self, lhs='sin(2*x)', restrictions=(), left='0', right='4*pi', **changes):
        return solve_condition(condition(lhs=lhs, restrictions=list(restrictions),
            interval=dict(left=left, right=right, left_open=False, right_open=False), **changes))

    def test_radical_domain_keeps_negative_and_later_periods(self):
        result = self.selected('sqrt(sin(x))', left='-4*pi')
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{0, -4*pi, -3*pi, -2*pi, -pi, pi, 2*pi, 3*pi, 4*pi}')
        checked = precheck.check(precheck.from_text('sqrt(sin(x))=0; x in [0;4*pi]',
            'trigonometric_equation'), 'trigonometric_equation')
        self.assertEqual(checked['status'], 'solved', checked)
        self.assertEqual(precheck.reference(checked)['scope'], '13a')
        self.assertIn('Integers', checked['answer'])
        self.assertNotEqual(checked['answer'], '{0, pi}')

    def test_strict_constraint_keeps_only_admissible_residue_class(self):
        result = self.selected(restrictions=['sin(x)>0'])
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{pi/2, 5*pi/2}')

    def test_closed_constraint_preserves_zero_boundaries(self):
        result = self.selected(restrictions=['sin(x)>=0'])
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{0, pi/2, pi, 2*pi, 5*pi/2, 3*pi, 4*pi}')

    def test_distinct_constraint_periods_are_all_applied(self):
        result = self.selected(restrictions=['sin(x)>=0', 'cos(2*x)>0'])
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{0, pi, 2*pi, 3*pi, 4*pi}')

    def test_negative_integer_parameters_and_negative_frequency(self):
        result = self.selected('cos(x)', ['sin(-x)>0'], left='-4*pi')
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{-5*pi/2, -pi/2, 3*pi/2, 7*pi/2}')

    def test_shift_and_fractional_frequency(self):
        result = self.selected('sqrt(sin(x/2+pi/3))', right='8*pi')
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{4*pi/3, 10*pi/3, 16*pi/3, 22*pi/3}')

    def test_original_denominator_excludes_roots_even_when_radical_vanishes(self):
        result = self.selected('sqrt(sin(x))/sin(x)')
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], 'EmptySet')

    def test_nonperiodic_constraint_is_preserved(self):
        result = self.selected('sqrt(sin(x))', ['x>pi'], left='-4*pi')
        self.assertEqual(result['status'], 'solved', result)
        self.assertEqual(result['answer'], '{2*pi, 3*pi, 4*pi}')

    def test_many_residue_classes_do_not_become_a_partial_key(self):
        value = dict(lhs='sin(x)', rhs='0', relation='=', interval=None,
            restrictions=['sin(x/33)>0'])
        result = precheck.check(value, 'trigonometric_equation')
        self.assertEqual(result['status'], 'unsupported', result)
        self.assertIsNone(precheck.reference(result))

    def test_standalone_periodic_inequality_is_not_a_single_interval_key(self):
        result = precheck.check(precheck.from_text('sin(x)>0', 'inequality'), 'inequality')
        self.assertEqual(result['status'], 'unsupported', result)
        self.assertEqual(result['reason'], 'periodic_inequality_not_complete')
        self.assertIsNone(precheck.reference(result))


if __name__ == '__main__':
    unittest.main()
