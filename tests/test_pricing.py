"""Price arithmetic and savings-list tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.pricing import (
    PriceError,
    calculate_savings_list,
    compare_prices,
    format_inr,
    parse_money_to_paise,
    parse_positive_decimal,
    parse_positive_int,
)


def medicine(**overrides):
    values = {
        "id": "paracetamol-500-tablet",
        "pack_size": 15.0,
        "pack_unit": "tablet",
        "jan_price_paise": 800,
        "jan_price_verified": 0,
        "is_sample": 1,
    }
    values.update(overrides)
    return values


def test_parse_money_uses_integer_paise_and_rejects_bad_values():
    assert parse_money_to_paise("₹1,234.56") == 123456
    assert parse_money_to_paise("12") == 1200
    assert parse_money_to_paise("0.005") == 1
    assert parse_money_to_paise("") is None
    for bad in (True, "abc", "-1", "NaN", "Infinity"):
        with pytest.raises(PriceError):
            parse_money_to_paise(bad)


def test_positive_parsers():
    assert parse_positive_int("3") == 3
    assert parse_positive_decimal("2.5") == Decimal("2.5")
    for bad in (True, "0", "-1", "x"):
        with pytest.raises(PriceError):
            parse_positive_int(bad)
    for bad in (True, "0", "-1", "x"):
        with pytest.raises(PriceError):
            parse_positive_decimal(bad)


def test_compare_prices_normalises_both_packs_and_marks_sample_values():
    result = compare_prices(medicine(), "30.00", "10", quantity=10, purchases_per_month=2)
    assert result["comparison_available"] is True
    assert result["brand_total_paise"] == 3000
    assert result["catalogue_total_paise"] == 533
    assert result["difference_paise"] == 2467
    assert result["monthly_difference_paise"] == 4934
    assert result["yearly_difference_paise"] == 59208
    assert result["price_verified"] is False
    assert result["data_is_sample"] is True
    assert result["price_label"] == "sample_or_unverified_price"
    assert any("not an official MRP" in warning for warning in result["warnings"])
    assert result["brand_pack_size_assumed"] is False
    assert result["assumptions"]


def test_missing_brand_pack_size_is_explicitly_assumed():
    result = compare_prices(medicine(), "30.00", None, quantity=15)
    assert result["comparison_available"] is True
    assert result["brand_pack_size_assumed"] is True
    assert result["brand_total_paise"] == 3000
    assert result["catalogue_total_paise"] == 800
    assert any("pack size was not supplied" in item for item in result["assumptions"])


def test_verified_non_sample_medicine_has_verified_label():
    result = compare_prices(
        medicine(jan_price_verified=1, is_sample=0), "20.00", "15", quantity=15
    )
    assert result["price_verified"] is True
    assert result["price_label"] == "verified_catalogue_price"
    assert not any("official MRP" in warning for warning in result["warnings"])


def test_missing_catalogue_price_returns_an_unavailable_comparison():
    result = compare_prices(medicine(jan_price_paise=None), "20.00", "15")
    assert result["comparison_available"] is False
    assert result["reason"] == "catalogue_price_missing"
    assert result["brand_price_paise"] == 2000


def test_generic_costing_more_is_warned_not_called_a_saving():
    result = compare_prices(
        medicine(jan_price_paise=5000), "10.00", "15", quantity=15
    )
    assert result["difference_paise"] < 0
    assert any("costs more" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    "brand_price,pack_size,quantity,purchases",
    [
        ("0", "15", 1, 1),
        ("-1", "15", 1, 1),
        ("10", "0", 1, 1),
        ("10", "15", 0, 1),
        ("10", "15", 1, 0),
    ],
)
def test_invalid_comparison_inputs_raise(brand_price, pack_size, quantity, purchases):
    with pytest.raises(PriceError):
        compare_prices(medicine(), brand_price, pack_size, quantity, purchases)


def test_savings_list_keeps_valid_items_and_reports_bad_ones():
    catalogue = {
        "a": medicine(id="a"),
        "b": medicine(id="b", jan_price_paise=1000),
        "missing": medicine(id="missing", jan_price_paise=None),
    }
    result = calculate_savings_list(
        [
            {"medicine_id": "a", "brand_price": "30", "brand_pack_size": "10", "quantity": 10},
            {"medicine_id": "b", "brand_price": "40", "brand_pack_size": "10", "quantity": 10},
            {"medicine_id": "missing", "brand_price": "20"},
            {"medicine_id": "does-not-exist", "brand_price": "20"},
            {"medicine_id": "a", "brand_price": "not-money"},
        ],
        catalogue,
    )
    assert result["valid_items"] == 2
    assert result["estimate_only"] is True
    assert result["totals"]["brand_total_paise"] == 7000
    assert result["totals"]["catalogue_total_paise"] == 1200
    assert result["totals"]["difference_paise"] == 5800
    assert [item["ok"] for item in result["items"]] == [True, True, False, False, False]
    assert result["items"][2]["error"] == "catalogue_price_missing"
    assert result["items"][3]["error"] == "unknown_medicine"


def test_savings_list_requires_at_least_one_valid_item():
    with pytest.raises(PriceError):
        calculate_savings_list([], {})
    with pytest.raises(PriceError):
        calculate_savings_list(
            [{"medicine_id": "a", "brand_price": "0"}], {"a": medicine(id="a")}
        )


@pytest.mark.parametrize(
    "paise,expected",
    [
        (0, "₹0.00"),
        (5, "₹0.05"),
        (100, "₹1.00"),
        (123456, "₹1,234.56"),
        (12345678, "₹1,23,456.78"),
        (-123456, "-₹1,234.56"),
        (None, "—"),
    ],
)
def test_inr_formatting(paise, expected):
    assert format_inr(paise) == expected
