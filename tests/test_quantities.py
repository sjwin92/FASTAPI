from decimal import Decimal

import pytest

from app.quantities import (
    amount_in_unit,
    money,
    normalize_requested_quantity,
    packs_required,
    parse_package_measure,
)


@pytest.mark.parametrize(
    ("quantity", "unit", "base_amount", "base_unit", "dimension"),
    [
        (500, "g", Decimal("500"), "g", "mass"),
        (1.5, "kg", Decimal("1500.0"), "g", "mass"),
        (25, "cl", Decimal("250"), "ml", "volume"),
        (2, "l", Decimal("2000"), "ml", "volume"),
        (3, "each", Decimal("3"), "each", "count"),
    ],
)
def test_request_units_normalize_to_canonical_base(
    quantity, unit, base_amount, base_unit, dimension
):
    normalized = normalize_requested_quantity(quantity, unit)
    assert normalized.base_amount == base_amount
    assert normalized.base_unit == base_unit
    assert normalized.dimension == dimension


def test_fractional_each_is_rejected():
    with pytest.raises(ValueError, match="whole number"):
        normalize_requested_quantity(1.5, "each")


@pytest.mark.parametrize("quantity", [0, -1, float("inf"), float("nan")])
def test_non_positive_or_non_finite_quantity_is_rejected(quantity):
    with pytest.raises(ValueError, match="positive and finite"):
        normalize_requested_quantity(quantity, "g")


@pytest.mark.parametrize(
    ("size_text", "name", "retailer_unit", "amount", "unit", "dimension"),
    [
        ("500g", "Pasta", None, Decimal("500"), "g", "mass"),
        ("2.27 ltr", "Milk", None, Decimal("2270.00"), "ml", "volume"),
        ("4 x 400g", "Tomatoes", None, Decimal("1600"), "g", "mass"),
        ("6 pints/3.41L", "Milk", None, Decimal("3410.00"), "ml", "volume"),
        ("Pack of 6", "Onions", None, Decimal("6"), "each", "count"),
        (None, "Apples 5 Pack", None, Decimal("5"), "each", "count"),
        (None, "Loose Cucumber", "each", Decimal("1"), "each", "count"),
    ],
)
def test_package_parser_handles_recorded_retailer_forms(
    size_text, name, retailer_unit, amount, unit, dimension
):
    package = parse_package_measure(size_text, name, retailer_unit)
    assert package is not None
    assert package.amount == amount
    assert package.unit == unit
    assert package.dimension == dimension


def test_explicit_size_wins_over_product_name_fallback():
    package = parse_package_measure("750g", "Example Rice 1kg")
    assert package is not None
    assert package.amount == Decimal("750")


def test_metric_dual_label_wins_when_derived_size_contains_only_imperial():
    package = parse_package_measure("2 pint", "British Milk 1.13L (2 pint)")
    assert package is not None
    assert package.amount == Decimal("1130.00")


def test_unknown_size_is_not_inferred_from_unit_price_measure():
    assert parse_package_measure(None, "Example Rice", "kg") is None


@pytest.mark.parametrize("size", ["", "family size", "400 gg", "0g"])
def test_malformed_or_absent_sizes_fail_closed(size):
    assert parse_package_measure(size, "Example Product") is None


def test_each_is_inferred_only_from_explicit_retailer_wording():
    assert parse_package_measure(None, "Loose Cucumber", "price each") is not None
    assert parse_package_measure(None, "Loose Cucumber", "unit") is None


def test_pack_count_and_money_rounding_are_deterministic():
    assert packs_required(Decimal("1000"), Decimal("400")) == 3
    assert money(Decimal("1.005")) == Decimal("1.01")
    assert amount_in_unit(Decimal("1500"), "kg") == Decimal("1.5")
