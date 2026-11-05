"""M07: exact finite unions of rational intervals and points, and one original denominator.

A set is described by membership. All interval ends and points of the sets
involved split the real line into "atoms": the breakpoints themselves and the open
gaps between consecutive breakpoints (plus the two unbounded gaps). Membership is
constant on every gap, so testing one interior sample per gap and every breakpoint
determines the set exactly. The canonical form is the list of maximal runs of
consecutive member atoms; this makes it unique, which is why two sets are equal
exactly when their symmetric difference has no atoms.

The functions do not know which variable a set describes (x or t): mixing
coordinates is a premise checked by the assembler before calling them.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Iterable, Optional

from .rational import InvalidInput, bounded


@dataclass(frozen=True)
class Interval:
    left: Optional[Fraction]    # None means −∞
    right: Optional[Fraction]   # None means +∞
    left_closed: bool
    right_closed: bool

    def __post_init__(self):
        if self.left is None and self.left_closed or self.right is None and self.right_closed:
            raise InvalidInput("Бесконечный конец промежутка не может быть включён.")
        if self.left is not None and self.right is not None and self.left > self.right:
            raise InvalidInput("Левая граница промежутка больше правой.")

    def contains(self, value: Fraction) -> bool:
        above = self.left is None or value > self.left or (value == self.left and self.left_closed)
        below = self.right is None or value < self.right or (value == self.right and self.right_closed)
        return above and below


@dataclass(frozen=True)
class ExactSet:
    intervals: tuple
    points: tuple

    def contains(self, value: Fraction) -> bool:
        return value in self.points or any(i.contains(value) for i in self.intervals)

    def breakpoints(self) -> set:
        values = set(self.points)
        for i in self.intervals:
            values.update(v for v in (i.left, i.right) if v is not None)
        return values

    @property
    def size(self) -> int:
        return len(self.intervals) + len(self.points)

    @property
    def empty(self) -> bool:
        return not self.intervals and not self.points


def canonical(member: Callable[[Fraction], bool], breakpoints: Iterable[Fraction]) -> ExactSet:
    """Unique normal form of the set {v : member(v)} whose boundary lies in breakpoints."""
    values = sorted(set(breakpoints))
    if not values:
        whole = member(Fraction(0))
        return ExactSet((Interval(None, None, False, False),) if whole else (), ())
    # Atom: (is_point, left_end, right_end, inside); None ends are infinite.
    atoms = [(False, None, values[0], member(values[0] - 1))]
    for index, value in enumerate(values):
        atoms.append((True, value, value, member(value)))
        if index + 1 < len(values):
            middle = bounded((value + values[index + 1]) / 2)
            atoms.append((False, value, values[index + 1], member(middle)))
    atoms.append((False, values[-1], None, member(values[-1] + 1)))
    intervals, points, index = [], [], 0
    while index < len(atoms):
        if not atoms[index][3]:
            index += 1
            continue
        end = index
        while end + 1 < len(atoms) and atoms[end + 1][3]:
            end += 1
        first, last = atoms[index], atoms[end]
        if index == end and first[0]:
            points.append(first[1])
        else:
            # A run starting at a gap is open at that gap's left breakpoint (which is
            # outside the set, otherwise the run would have started there).
            intervals.append(Interval(first[1], last[2], first[0], last[0]))
        index = end + 1
    return ExactSet(tuple(intervals), tuple(points))


def normalize(value: ExactSet) -> ExactSet:
    return canonical(value.contains, value.breakpoints())


def union(sets: list[ExactSet]) -> ExactSet:
    return canonical(lambda v: any(s.contains(v) for s in sets), _points(sets))


def intersection(sets: list[ExactSet]) -> ExactSet:
    return canonical(lambda v: all(s.contains(v) for s in sets), _points(sets))


def symmetric_difference(left: ExactSet, right: ExactSet) -> ExactSet:
    return canonical(lambda v: left.contains(v) != right.contains(v), _points([left, right]))


def _points(sets: list[ExactSet]) -> set:
    result = set()
    for s in sets:
        result |= s.breakpoints()
    return result


def evaluate_polynomial(coefficients: list[Fraction], point: Fraction) -> Fraction:
    """Horner evaluation; coefficients run from the constant term to the highest power."""
    value = Fraction(0)
    for coefficient in reversed(coefficients):
        value = bounded(bounded(value * point) + coefficient)
    return value
