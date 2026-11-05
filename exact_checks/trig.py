"""M06: exact real-root selection and circle points, all values as coefficients of π.

Every number here is a rational coefficient c of the real value cπ. Because π > 0,
comparing cπ values is exactly comparing the coefficients, so no approximation of
π is ever needed. The functions check only the supplied families and interval:
they do not prove that the families solve the equation, that they were obtained
equivalently, or that the student justified the selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

from .rational import InvalidInput, ResourceLimit, bounded

CANDIDATE_LIMIT = 256  # registry limits.enumerated_root_candidates_before_dedup


@dataclass(frozen=True)
class PiInterval:
    """Finite interval of coefficients of π; equal ends are a point or the empty set."""
    left: Fraction
    right: Fraction
    left_closed: bool
    right_closed: bool

    def __post_init__(self):
        if self.left > self.right:
            # Reversed ends are a malformed input, never silently swapped.
            raise InvalidInput("Левая граница промежутка больше правой.")

    def contains(self, value: Fraction) -> bool:
        above = value > self.left or (value == self.left and self.left_closed)
        below = value < self.right or (value == self.right and self.right_closed)
        return above and below


def membership(point: Fraction, interval: PiInterval) -> bool:
    return interval.contains(point)


def k_range(offset: Fraction, period: Fraction, interval: PiInterval) -> tuple[int, int]:
    """Exact inclusive range of integers k with offset + period·k in the interval.

    For period > 0:  left ≤ a + bk ≤ right  ⇔  (left−a)/b ≤ k ≤ (right−a)/b.
    For period < 0 the division by b reverses both inequalities, so the bound that
    comes from the right end becomes the lower bound for k. Strictness travels with
    the end it came from. An empty range is returned as (first, first−1).
    """
    if period == 0:
        raise ValueError("Constant families have no k range.")
    from_left = bounded((interval.left - offset) / period)
    from_right = bounded((interval.right - offset) / period)
    if period > 0:
        low, low_strict = from_left, not interval.left_closed
        high, high_strict = from_right, not interval.right_closed
    else:
        low, low_strict = from_right, not interval.right_closed
        high, high_strict = from_left, not interval.left_closed
    first = math.floor(low) + 1 if low_strict else math.ceil(low)
    last = math.ceil(high) - 1 if high_strict else math.floor(high)
    return first, max(last, first - 1)


@dataclass(frozen=True)
class Selection:
    selected: list          # sorted distinct coefficients of selected real roots
    missing: list           # selected but absent from the claimed list
    extra: list             # claimed but not selected (outside, or not in the families)
    duplicate_claims: int   # repeated entries in the claimed list
    per_family: list        # evidence: (offset, period, first_k, last_k | None for constant)

    @property
    def matches(self) -> bool:
        return not self.missing and not self.extra


def select_roots(families: list[tuple[Fraction, Fraction]], interval: PiInterval,
                 claimed: list[Fraction]) -> Selection:
    """Selection from the UNION of independently parameterised families.

    The candidate count is computed exactly before any enumeration, so a huge
    k range yields a resource limit instead of a partial list that could be
    mistaken for a complete selection.
    """
    plan, total = [], 0
    for offset, period in families:
        if period == 0:
            # Every k gives the same single value; infinitely many k ≠ many roots.
            inside = interval.contains(offset)
            plan.append((offset, period, None, None, inside))
            total += 1 if inside else 0
        else:
            first, last = k_range(offset, period, interval)
            plan.append((offset, period, first, last, None))
            total += last - first + 1
        if total > CANDIDATE_LIMIT:
            raise ResourceLimit(f"Кандидатов больше {CANDIDATE_LIMIT}; частичный перебор не выдаётся за полный отбор.")
    roots = set()
    for offset, period, first, last, inside in plan:
        if period == 0:
            if inside:
                roots.add(offset)
            continue
        for k in range(first, last + 1):
            root = bounded(offset + period * k)
            # Defensive re-check of the derived k range against the original interval.
            if not interval.contains(root):
                raise AssertionError("k range produced a value outside the interval")
            roots.add(root)
    claimed_set = set(claimed)
    return Selection(selected=sorted(roots), missing=sorted(roots - claimed_set),
                     extra=sorted(claimed_set - roots), duplicate_claims=len(claimed) - len(claimed_set),
                     per_family=[(o, p, f, l) for o, p, f, l, _ in plan])


@dataclass(frozen=True)
class CirclePoints:
    same_point: bool
    same_real_number: bool
    turns: Fraction            # (angle − point)/2: number of full turns 2π between them
    angle_residue: Fraction    # angle mod 2 in [0, 2), i.e. angle mod 2π in [0, 2π)
    point_residue: Fraction


def circle_points(angle: Fraction, point_angle: Fraction) -> CirclePoints:
    """Same point on the unit circle ⇔ the angles differ by an integer number of 2π.

    Equality modulo 2π is reported separately from equality of real numbers: −19π/4
    and 5π/4 are one point but different roots on a real interval.
    """
    turns = bounded((angle - point_angle) / 2)
    return CirclePoints(same_point=turns.denominator == 1, same_real_number=angle == point_angle,
                        turns=turns, angle_residue=angle % 2, point_residue=point_angle % 2)
