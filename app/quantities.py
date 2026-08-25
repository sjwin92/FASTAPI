"""Deterministic quantity normalization and retailer package-size parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Literal


RequestUnit = Literal["g", "kg", "ml", "cl", "l", "each"]
BaseUnit = Literal["g", "ml", "each"]
Dimension = Literal["mass", "volume", "count"]

_REQUEST_FACTORS: dict[str, tuple[Decimal, BaseUnit, Dimension]] = {
    "g": (Decimal("1"), "g", "mass"),
    "kg": (Decimal("1000"), "g", "mass"),
    "ml": (Decimal("1"), "ml", "volume"),
    "cl": (Decimal("10"), "ml", "volume"),
    "l": (Decimal("1000"), "ml", "volume"),
    "each": (Decimal("1"), "each", "count"),
}
_SOURCE_FACTORS: dict[str, tuple[Decimal, BaseUnit, Dimension, bool]] = {
    "g": (Decimal("1"), "g", "mass", True),
    "gram": (Decimal("1"), "g", "mass", True),
    "grams": (Decimal("1"), "g", "mass", True),
    "kg": (Decimal("1000"), "g", "mass", True),
    "kilogram": (Decimal("1000"), "g", "mass", True),
    "kilograms": (Decimal("1000"), "g", "mass", True),
    "oz": (Decimal("28.349523125"), "g", "mass", False),
    "ml": (Decimal("1"), "ml", "volume", True),
    "cl": (Decimal("10"), "ml", "volume", True),
    "l": (Decimal("1000"), "ml", "volume", True),
    "ltr": (Decimal("1000"), "ml", "volume", True),
    "litre": (Decimal("1000"), "ml", "volume", True),
    "litres": (Decimal("1000"), "ml", "volume", True),
    "pt": (Decimal("568.26125"), "ml", "volume", False),
    "pint": (Decimal("568.26125"), "ml", "volume", False),
    "pints": (Decimal("568.26125"), "ml", "volume", False),
}

_MEASURE_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*"
    r"(kilograms?|kg|grams?|g|litres?|liters?|ltr|ml|cl|l|pints?|pt|oz)\b",
    re.IGNORECASE,
)
_MULTIPACK_RE = re.compile(
    r"(?<!\w)(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*"
    r"(kilograms?|kg|grams?|g|litres?|liters?|ltr|ml|cl|l|pints?|pt|oz)\b",
    re.IGNORECASE,
)
_PACK_OF_RE = re.compile(r"\bpack\s+of\s+(\d+)\b", re.IGNORECASE)
_COUNT_PACK_RE = re.compile(r"\b(\d+)\s*(?:-|\s)?pack\b", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedQuantity:
    amount: Decimal
    unit: RequestUnit
    base_amount: Decimal
    base_unit: BaseUnit
    dimension: Dimension


@dataclass(frozen=True)
class PackageMeasure:
    amount: Decimal
    unit: BaseUnit
    dimension: Dimension


def as_decimal(value: int | float | str | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("quantity must be a number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("quantity must be positive and finite")
    return amount


def normalize_requested_quantity(
    value: int | float | str | Decimal,
    unit: RequestUnit,
) -> NormalizedQuantity:
    amount = as_decimal(value)
    factor, base_unit, dimension = _REQUEST_FACTORS[unit]
    if unit == "each" and amount != amount.to_integral_value():
        raise ValueError("quantity in 'each' must be a whole number")
    base_amount = amount * factor
    return NormalizedQuantity(amount, unit, base_amount, base_unit, dimension)


def amount_in_unit(base_amount: Decimal, unit: RequestUnit) -> Decimal:
    factor, _, _ = _REQUEST_FACTORS[unit]
    return base_amount / factor


def packs_required(requested_base: Decimal, package_base: Decimal) -> int:
    return int((requested_base / package_base).to_integral_value(rounding=ROUND_CEILING))


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _source_unit(unit: str) -> str:
    normalized = unit.casefold()
    if normalized == "liter" or normalized == "liters":
        return "litre" if normalized == "liter" else "litres"
    return normalized


def _measure(value: str, unit: str, multiplier: int = 1) -> tuple[PackageMeasure, bool] | None:
    source = _SOURCE_FACTORS.get(_source_unit(unit))
    if not source:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    factor, base_unit, dimension, is_metric = source
    base_amount = amount * factor * multiplier
    if base_amount <= 0:
        return None
    return PackageMeasure(base_amount, base_unit, dimension), is_metric


def _parse_text_measure_details(text: str) -> tuple[PackageMeasure, bool] | None:
    multipack = _MULTIPACK_RE.search(text)
    if multipack:
        parsed = _measure(multipack.group(2), multipack.group(3), int(multipack.group(1)))
        if parsed and not parsed[1]:
            # In a dual-labelled multipack, prefer the stated metric total.
            metric = [
                candidate
                for match in _MEASURE_RE.finditer(text)
                if (candidate := _measure(match.group(1), match.group(2)))
                and candidate[1]
            ]
            if metric:
                return metric[-1]
        return parsed

    measures: list[tuple[PackageMeasure, bool]] = []
    for match in _MEASURE_RE.finditer(text):
        parsed = _measure(match.group(1), match.group(2))
        if parsed:
            measures.append(parsed)
    if measures:
        # Dual-labelled packages such as "6 pints/3.41L" use the explicit metric value.
        metric = [entry for entry in measures if entry[1]]
        return metric[-1] if metric else measures[-1]

    pack_of = _PACK_OF_RE.search(text)
    count_pack = _COUNT_PACK_RE.search(text)
    count_match = pack_of or count_pack
    if count_match:
        return PackageMeasure(Decimal(count_match.group(1)), "each", "count"), True
    return None


def _parse_text_measure(text: str) -> PackageMeasure | None:
    parsed = _parse_text_measure_details(text)
    return parsed[0] if parsed else None


def parse_package_measure(
    size_text: str | None,
    product_name: str,
    retailer_unit: str | None = None,
) -> PackageMeasure | None:
    """Parse an explicit package size, preferring adapter data over the name."""
    if size_text:
        explicit = _parse_text_measure_details(size_text)
        if explicit:
            # A derived adapter field can contain only the imperial half of a
            # dual-labelled name. Preserve field precedence except when the
            # full name supplies the product's explicit metric equivalent.
            named = _parse_text_measure_details(product_name)
            if not explicit[1] and named and named[1]:
                return named[0]
            return explicit[0]
    parsed_name = _parse_text_measure(product_name)
    if parsed_name:
        return parsed_name
    if retailer_unit and "each" in retailer_unit.casefold():
        return PackageMeasure(Decimal("1"), "each", "count")
    return None
