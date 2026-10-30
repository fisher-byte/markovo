"""Public credit-amount wire primitives.

These helpers only parse and format the decimal credit amounts that travel
on the public API wire (``max_credits`` arguments, receipt fields). They are
*not* billing logic: rates, plan allowances, deductions, and reconciliation
live in the server-side ``pdf2md_core._internal`` package and are never
shipped in the customer wheel.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation


CREDIT_SCALE = 1_000
MAX_CREDITS = Decimal("1000000000")
MAX_CREDIT_UNITS = int(MAX_CREDITS * CREDIT_SCALE)

_CREDIT_PATTERN = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,3})?$")


class CreditAmountError(ValueError):
    pass


def parse_credit_units(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise CreditAmountError("Credit amount must be a decimal value.")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CreditAmountError("Credit amount must be finite.")
        raw = str(value)
    elif isinstance(value, (str, int, Decimal)):
        raw = str(value).strip()
    else:
        raise CreditAmountError("Credit amount must be a decimal value.")
    if not _CREDIT_PATTERN.fullmatch(raw):
        raise CreditAmountError("Credits support at most three decimal places without exponent notation.")
    try:
        credits = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regular expression
        raise CreditAmountError("Credit amount is invalid.") from exc
    minimum = Decimal(0) if allow_zero else Decimal("0.001")
    if credits < minimum or credits > MAX_CREDITS:
        raise CreditAmountError("Credit amount is outside the supported range.")
    units = credits * CREDIT_SCALE
    if units != units.to_integral_value():
        raise CreditAmountError("Credits support at most three decimal places.")
    return int(units)


def credits_from_units(units: int) -> Decimal:
    _require_safe_units(units, allow_zero=True)
    return Decimal(units) / CREDIT_SCALE


def format_credits(units: int) -> str:
    value = credits_from_units(units)
    rendered = f"{value:,.3f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _require_safe_units(units: int, *, allow_zero: bool) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(units, bool) or not isinstance(units, int) or units < minimum or units > MAX_CREDIT_UNITS:
        raise CreditAmountError("Credit units are outside the supported integer range.")
