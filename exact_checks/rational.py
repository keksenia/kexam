"""Exact rationals of the K01 protocol 1.0.0: no floats, no expression parsing.

Inputs must already be canonical (reduced, positive denominator, zero as 0/1, no
"-0"): K01 rejects other spellings instead of normalising them, because a
non-canonical number signals a defect upstream and would make equal values hash
differently. Outputs are always canonical and bounded by the same digit limit.
"""
from __future__ import annotations

from fractions import Fraction
import math

INPUT_DIGITS = 40          # registry limits.integer_input_digits
OUTPUT_DIGITS = 40         # registry limits.integer_output_digits
INTERMEDIATE_BITS = 4096   # registry limits.intermediate_integer_bits


class CheckError(ValueError):
    """A reason why an exact answer is not available; never a student error."""
    code = "internal_error"


class InvalidInput(CheckError):
    code = "invalid_input"


class ResourceLimit(CheckError):
    code = "resource_limit"


def parse(value) -> Fraction:
    """Read a schema-valid K01 rational and enforce the canonical form."""
    numerator, denominator = value["numerator"], value["denominator"]
    if numerator == "-0":
        raise InvalidInput("Отрицательный ноль не является канонической записью рационального числа.")
    n, d = int(numerator), int(denominator)
    if d <= 0:
        raise InvalidInput("Знаменатель должен быть положительным.")
    if len(str(abs(n))) > INPUT_DIGITS or len(str(d)) > INPUT_DIGITS:
        raise InvalidInput("Входное число длиннее 40 цифр.")
    if math.gcd(n, d) != 1:
        # gcd(0, d) == d, so zero is accepted only as 0/1.
        raise InvalidInput("Дробь не сокращена либо ноль записан не как 0/1.")
    return Fraction(n, d)


def bounded(value: Fraction) -> Fraction:
    """Guard intermediate growth; exceeding it is a resource limit, not rounding."""
    if value.numerator.bit_length() > INTERMEDIATE_BITS or value.denominator.bit_length() > INTERMEDIATE_BITS:
        raise ResourceLimit("Промежуточное точное число превысило 4096 бит.")
    return value


def encode(value: Fraction) -> dict:
    """Canonical output; a value that does not fit is never truncated or rounded."""
    if len(str(abs(value.numerator))) > OUTPUT_DIGITS or len(str(value.denominator)) > OUTPUT_DIGITS:
        raise ResourceLimit("Точный результат не помещается в 40 цифр формата обмена.")
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def exact(value: Fraction, basis: str) -> dict:
    return {"basis": basis, "coefficient": encode(value)}


def show(value: Fraction, basis: str = "rational") -> str:
    """Human-readable value for claims and evidence, e.g. -19π/4, π, 3/4."""
    if basis == "pi":
        if value == 0:
            return "0"
        sign = "−" if value < 0 else ""
        n, d = abs(value.numerator), value.denominator
        head = "π" if n == 1 else f"{n}π"
        return sign + (head if d == 1 else f"{head}/{d}")
    text = str(abs(value.numerator)) if value.denominator == 1 else f"{abs(value.numerator)}/{value.denominator}"
    return ("−" if value < 0 else "") + text
