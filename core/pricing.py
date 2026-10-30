"""Price-comparison and savings arithmetic.

All money is handled as integer paise to avoid floating-point drift. Every
result carries explicit verification flags and assumptions so the interface
can avoid presenting an estimate as a verified saving.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


class PriceError(ValueError):
    """Raised for invalid price, pack size or quantity input."""


def parse_money_to_paise(value: Any) -> int | None:
    """Parse rupees into paise. Empty input returns ``None``.

    Accepts ints, Decimals, floats (converted through str) and strings such as
    ``"12.50"`` or ``"₹12.50"``. Rejects booleans and non-finite values.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise PriceError("invalid_money")
    if isinstance(value, float):
        value = format(value, ".2f")
    text = str(value).strip().replace("₹", "").replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise PriceError("invalid_money") from exc
    if not amount.is_finite() or amount < 0:
        raise PriceError("invalid_money")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_positive_int(value: Any, *, maximum: int = 10_000) -> int:
    if isinstance(value, bool):
        raise PriceError("invalid_quantity")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PriceError("invalid_quantity") from exc
    if number <= 0 or number > maximum:
        raise PriceError("invalid_quantity")
    return number


def parse_positive_decimal(value: Any, *, maximum: Decimal = Decimal("100000")) -> Decimal:
    if isinstance(value, bool):
        raise PriceError("invalid_pack_size")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PriceError("invalid_pack_size") from exc
    if not number.is_finite() or number <= 0 or number > maximum:
        raise PriceError("invalid_pack_size")
    return number


def _round_paise(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _total(price_paise: int, quantity: int, pack_size: Decimal) -> int:
    return _round_paise(Decimal(price_paise) * Decimal(quantity) / pack_size)


def _per_unit(price_paise: int, pack_size: Decimal) -> int:
    return _round_paise(Decimal(price_paise) / pack_size)


def compare_prices(
    medicine: Mapping[str, Any],
    brand_price: Any,
    brand_pack_size: Any = None,
    quantity: Any = 1,
    purchases_per_month: Any = 1,
) -> dict[str, Any]:
    """Compare one catalogue price with a user-entered branded price.

    Returns a structured result instead of raising for *business* conditions
    such as a missing catalogue price. Invalid user input raises ``PriceError``.
    """
    result: dict[str, Any] = {
        "comparison_available": False,
        "reason": None,
        "warnings": [],
        "assumptions": [],
        "currency": "INR",
    }

    pack_size = parse_positive_decimal(medicine.get("pack_size") or 0)
    unit = str(medicine.get("pack_unit") or "unit")
    qty = parse_positive_int(quantity)

    if brand_pack_size in (None, ""):
        brand_pack_size_value = pack_size
        result["brand_pack_size_assumed"] = True
    else:
        brand_pack_size_value = parse_positive_decimal(brand_pack_size)
        result["brand_pack_size_assumed"] = False

    brand_paise = parse_money_to_paise(brand_price)
    if brand_paise is None or brand_paise <= 0:
        raise PriceError("invalid_brand_price")

    catalogue_paise = medicine.get("jan_price_paise")
    price_verified = bool(medicine.get("jan_price_verified")) and not bool(
        medicine.get("is_sample")
    )
    result.update(
        {
            "unit": unit,
            "quantity": qty,
            "catalogue_pack_size": float(pack_size),
            "brand_pack_size": float(brand_pack_size_value),
            "brand_price_paise": brand_paise,
            "catalogue_price_paise": catalogue_paise,
            "price_verified": price_verified,
            "data_is_sample": bool(medicine.get("is_sample")),
            "price_label": (
                "verified_catalogue_price"
                if price_verified
                else "sample_or_unverified_price"
            ),
        }
    )
    result["assumptions"].append(
        "Both pack prices are divided by their pack sizes to obtain a price per unit before multiplying by quantity."
    )
    if result["brand_pack_size_assumed"]:
        result["assumptions"].append(
            "The branded pack size was not supplied, so the catalogue pack size was used."
        )
    if not price_verified:
        result["warnings"].append(
            "The catalogue price is sample or unverified data, not an official MRP. The difference is illustrative only."
        )

    if catalogue_paise is None:
        result["reason"] = "catalogue_price_missing"
        return result
    if catalogue_paise <= 0:
        result["reason"] = "catalogue_price_invalid"
        return result

    purchases = parse_positive_int(purchases_per_month, maximum=100)
    brand_total = _total(brand_paise, qty, brand_pack_size_value)
    catalogue_total = _total(catalogue_paise, qty, pack_size)
    difference = brand_total - catalogue_total
    percent = (
        float((Decimal(difference) * 100 / Decimal(brand_total)).quantize(Decimal("0.1")))
        if brand_total
        else 0.0
    )

    result.update(
        {
            "comparison_available": True,
            "brand_total_paise": brand_total,
            "catalogue_total_paise": catalogue_total,
            "difference_paise": difference,
            "difference_percent": percent,
            "brand_per_unit_paise": _per_unit(brand_paise, brand_pack_size_value),
            "catalogue_per_unit_paise": _per_unit(catalogue_paise, pack_size),
            "purchases_per_month": purchases,
            "monthly_difference_paise": difference * purchases,
            "yearly_difference_paise": difference * purchases * 12,
        }
    )
    if difference < 0:
        result["warnings"].append(
            "At these entered prices the generic option costs more. This is not a saving."
        )
    result["assumptions"].append(
        "Monthly and yearly figures assume the same quantity and price for every purchase."
    )
    result["assumptions"].append(
        "Actual local selling price, taxes, stock and pack size may differ. This is not a quotation."
    )
    return result


def calculate_savings_list(
    items: list[Mapping[str, Any]], catalogue: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Calculate a temporary multi-medicine comparison list.

    Each item is ``{medicine_id, brand_price, brand_pack_size?, quantity,
    purchases_per_month?}``. Per-item problems are reported without discarding
    valid items.
    """
    if not isinstance(items, list) or not items:
        raise PriceError("empty_list")
    if len(items) > 50:
        raise PriceError("too_many_items")

    results: list[dict[str, Any]] = []
    totals = {
        "brand_total_paise": 0,
        "catalogue_total_paise": 0,
        "difference_paise": 0,
        "monthly_difference_paise": 0,
        "yearly_difference_paise": 0,
    }
    valid_count = 0
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            results.append({"index": index, "ok": False, "error": "invalid_item"})
            continue
        medicine_id = str(item.get("medicine_id") or "").strip()
        medicine = catalogue.get(medicine_id)
        if medicine is None:
            results.append(
                {"index": index, "medicine_id": medicine_id, "ok": False, "error": "unknown_medicine"}
            )
            continue
        try:
            comparison = compare_prices(
                medicine,
                item.get("brand_price"),
                item.get("brand_pack_size"),
                item.get("quantity", 1),
                item.get("purchases_per_month", 1),
            )
        except PriceError as exc:
            results.append(
                {
                    "index": index,
                    "medicine_id": medicine_id,
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue

        entry = {
            "index": index,
            "medicine_id": medicine_id,
            "ok": comparison["comparison_available"],
            "comparison": comparison,
        }
        if comparison["comparison_available"]:
            valid_count += 1
            for key in totals:
                totals[key] += int(comparison.get(key, 0))
        else:
            entry["error"] = comparison.get("reason") or "comparison_unavailable"
        results.append(entry)

    if valid_count == 0:
        raise PriceError("no_valid_items")

    totals["difference_percent"] = (
        round(totals["difference_paise"] * 100 / totals["brand_total_paise"], 1)
        if totals["brand_total_paise"]
        else 0.0
    )
    return {
        "items": results,
        "totals": totals,
        "valid_items": valid_count,
        "estimate_only": True,
        "warnings": [
            "These totals are estimates built from the prices shown. They are not verified savings or a quotation."
        ],
    }


def format_inr(paise: int | None) -> str:
    """Format integer paise as an Indian-style rupee string."""
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    value = abs(int(paise))
    rupees, fraction = divmod(value, 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = ",".join(
            [head[max(0, i - 2) : i] for i in range(len(head), 0, -2)][::-1]
        )
        digits = f"{head},{tail}"
    return f"{sign}₹{digits}.{fraction:02d}"
