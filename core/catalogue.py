"""Catalogue schema validation shared by the loader and the import CLI.

The rules enforce honest provenance: a price cannot be marked verified unless
it has a source, a verification date, and is not part of a sample dataset.
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .medicine import parse_strengths, split_ingredients
from .pricing import PriceError, parse_money_to_paise

MEDICINE_FIELDS = [
    "id",
    "generic_name",
    "synonyms",
    "common_brands",
    "composition",
    "strength",
    "dosage_form",
    "route",
    "release_type",
    "pack_size",
    "pack_unit",
    "category",
    "product_code",
    "jan_price",
    "jan_price_source",
    "jan_price_verified",
    "is_sample",
    "source_name",
    "source_url",
    "last_verified",
]

KENDRA_FIELDS = [
    "id",
    "kendra_code",
    "name",
    "owner_name",
    "address",
    "locality",
    "city",
    "district",
    "state",
    "pincode",
    "phone",
    "latitude",
    "longitude",
    "coordinate_precision",
    "is_sample",
    "source_name",
    "source_url",
    "last_verified",
]

RELEASE_TYPES = {"immediate", "extended", "modified", "delayed", "unknown"}
COORDINATE_PRECISIONS = {"rooftop", "street", "city", "unknown"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PIN_RE = re.compile(r"^\d{6}$")
PHONE_RE = re.compile(r"^[0-9+\-() ]{6,20}$")


class CatalogueError(ValueError):
    def __init__(self, message: str, errors: Iterable[str] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])


def _text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def _flag(row: dict[str, Any], key: str, default: int = 0) -> int:
    raw = _text(row, key) or str(default)
    if raw not in {"0", "1"}:
        raise ValueError(f"{key} must be 0 or 1")
    return int(raw)


def _url_ok(value: str) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_medicine(row: dict[str, Any], line: int = 0) -> dict[str, Any]:
    errors: list[str] = []

    def require(key: str) -> str:
        value = _text(row, key)
        if not value:
            errors.append(f"line {line}: {key} is required")
        return value

    medicine_id = require("id")
    if medicine_id and not ID_RE.match(medicine_id):
        errors.append(f"line {line}: id must be a lowercase slug (a-z, 0-9, hyphen)")
    generic_name = require("generic_name")
    composition = require("composition")
    strength = require("strength")
    dosage_form = require("dosage_form")
    route = require("route")
    release_type = require("release_type").lower()
    pack_unit = require("pack_unit")
    category = require("category")
    if release_type and release_type not in RELEASE_TYPES:
        errors.append(f"line {line}: unsupported release_type '{release_type}'")

    strengths = parse_strengths(strength)
    if strength and not strengths:
        errors.append(f"line {line}: strength must contain a number and unit")
    ingredients = split_ingredients(composition)
    if composition and not ingredients:
        errors.append(f"line {line}: composition could not be parsed")
    # A liquid concentration such as "125 mg/5 ml" is a valid single-ingredient
    # record with two numeric parts; every other mismatch is a data error.
    concentration = len(ingredients) == 1 and len(strengths) == 2 and strengths[1].unit == "ml"
    if ingredients and strengths and len(ingredients) != len(strengths) and not concentration:
        errors.append(
            f"line {line}: combination composition needs one strength per ingredient "
            f"({len(ingredients)} ingredients, {len(strengths)} strengths)"
        )

    try:
        pack_size = Decimal(_text(row, "pack_size"))
        if not pack_size.is_finite() or pack_size <= 0:
            raise ValueError
    except Exception:
        pack_size = Decimal(0)
        errors.append(f"line {line}: pack_size must be greater than zero")

    is_sample = 0
    verified = 0
    try:
        is_sample = _flag(row, "is_sample", 1)
        verified = _flag(row, "jan_price_verified", 0)
    except ValueError as exc:
        errors.append(f"line {line}: {exc}")

    price = None
    if _text(row, "jan_price"):
        try:
            price = parse_money_to_paise(_text(row, "jan_price"))
        except PriceError:
            errors.append(f"line {line}: jan_price is not a valid rupee amount")
    if verified and price is None:
        errors.append(f"line {line}: verified price requires jan_price")
    if verified and not _text(row, "jan_price_source"):
        errors.append(f"line {line}: verified price requires jan_price_source")
    if verified and not _text(row, "last_verified"):
        errors.append(f"line {line}: verified price requires last_verified")
    if verified and is_sample:
        errors.append(f"line {line}: sample rows cannot mark a price as verified")
    if verified and not _url_ok(_text(row, "source_url")):
        errors.append(f"line {line}: source_url must be an http(s) URL")

    last_verified = _text(row, "last_verified")
    if last_verified and not DATE_RE.match(last_verified):
        errors.append(f"line {line}: last_verified must be YYYY-MM-DD")
    if not _url_ok(_text(row, "source_url")):
        errors.append(f"line {line}: source_url must be an http(s) URL")
    if not is_sample:
        # A row outside the sample set is presented as sourced catalogue data.
        # It therefore needs provenance even when its price is not verified.
        if not _text(row, "source_name"):
            errors.append(f"line {line}: non-sample row requires source_name")
        if not _text(row, "source_url"):
            errors.append(f"line {line}: non-sample row requires source_url")
        if not last_verified:
            errors.append(f"line {line}: non-sample row requires last_verified")

    common_brands = _text(row, "common_brands")
    synonyms = _text(row, "synonyms")
    product_code = _text(row, "product_code")
    search_blob = " ".join(
        part
        for part in (
            generic_name,
            common_brands,
            synonyms,
            composition,
            strength,
            dosage_form,
            category,
            product_code,
        )
        if part
    ).lower()

    if errors:
        raise CatalogueError("invalid medicine row", errors)

    return {
        "id": medicine_id,
        "generic_name": generic_name,
        "synonyms": synonyms,
        "common_brands": common_brands,
        "composition": composition,
        "strength": strengths and strength,
        "dosage_form": dosage_form,
        "route": route,
        "release_type": release_type,
        "pack_size": float(pack_size),
        "pack_unit": pack_unit,
        "category": category,
        "product_code": product_code,
        "jan_price_paise": price,
        "jan_price_source": _text(row, "jan_price_source"),
        "jan_price_verified": verified,
        "is_sample": is_sample,
        "source_name": _text(row, "source_name"),
        "source_url": _text(row, "source_url"),
        "last_verified": last_verified,
        "search_blob": search_blob,
    }


def validate_kendra(row: dict[str, Any], line: int = 0) -> dict[str, Any]:
    errors: list[str] = []

    def require(key: str) -> str:
        value = _text(row, key)
        if not value:
            errors.append(f"line {line}: {key} is required")
        return value

    kendra_id = require("id")
    if kendra_id and not ID_RE.match(kendra_id):
        errors.append(f"line {line}: id must be a lowercase slug")
    kendra_code = require("kendra_code")
    name = require("name")
    address = require("address")
    city = require("city")
    district = require("district")
    state = require("state")
    pincode = require("pincode")
    if pincode and not PIN_RE.match(pincode):
        errors.append(f"line {line}: pincode must be six digits")

    latitude_raw = _text(row, "latitude")
    longitude_raw = _text(row, "longitude")
    latitude = longitude = None
    if bool(latitude_raw) != bool(longitude_raw):
        errors.append(f"line {line}: latitude and longitude must be supplied together")
    elif latitude_raw:
        try:
            latitude = float(latitude_raw)
            longitude = float(longitude_raw)
        except ValueError:
            errors.append(f"line {line}: latitude/longitude must be numbers")
        else:
            if not -90 <= latitude <= 90:
                errors.append(f"line {line}: latitude out of range")
            if not -180 <= longitude <= 180:
                errors.append(f"line {line}: longitude out of range")

    phone = _text(row, "phone")
    if phone and not PHONE_RE.match(phone):
        errors.append(f"line {line}: phone contains unsupported characters")

    coordinate_precision = _text(row, "coordinate_precision").lower() or "unknown"
    if coordinate_precision not in COORDINATE_PRECISIONS:
        errors.append(f"line {line}: unsupported coordinate_precision")

    try:
        is_sample = _flag(row, "is_sample", 1)
    except ValueError as exc:
        errors.append(f"line {line}: {exc}")
        is_sample = 1

    last_verified = _text(row, "last_verified")
    if last_verified and not DATE_RE.match(last_verified):
        errors.append(f"line {line}: last_verified must be YYYY-MM-DD")
    if not _url_ok(_text(row, "source_url")):
        errors.append(f"line {line}: source_url must be an http(s) URL")
    if not is_sample:
        # Same provenance rule as medicines: a non-sample Kendra must be
        # traceable before it is described as sourced rather than illustrative.
        if not _text(row, "source_name"):
            errors.append(f"line {line}: non-sample row requires source_name")
        if not _text(row, "source_url"):
            errors.append(f"line {line}: non-sample row requires source_url")
        if not last_verified:
            errors.append(f"line {line}: non-sample row requires last_verified")

    if errors:
        raise CatalogueError("invalid kendra row", errors)

    return {
        "id": kendra_id,
        "kendra_code": kendra_code,
        "name": name,
        "owner_name": _text(row, "owner_name"),
        "address": address,
        "locality": _text(row, "locality"),
        "city": city,
        "district": district,
        "state": state,
        "pincode": pincode,
        "phone": phone,
        "latitude": latitude,
        "longitude": longitude,
        "coordinate_precision": coordinate_precision,
        "is_sample": is_sample,
        "source_name": _text(row, "source_name"),
        "source_url": _text(row, "source_url"),
        "last_verified": last_verified,
    }


def _read(path: Path, fields: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        raise CatalogueError(f"catalogue file not found: {path.name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [field for field in fields if field not in headers]
        if missing:
            raise CatalogueError(
                f"{path.name} is missing columns: {', '.join(missing)}",
                [f"missing column: {field}" for field in missing],
            )
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(reader, start=2):
            rows.append({key: (row.get(key) or "") for key in fields})
    return rows, []


def load_medicines(path: Path) -> list[dict[str, Any]]:
    raw, _ = _read(path, MEDICINE_FIELDS)
    output: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(raw, start=2):
        try:
            medicine = validate_medicine(row, index)
        except CatalogueError as exc:
            errors.extend(exc.errors)
            continue
        if medicine["id"] in seen:
            errors.append(f"line {index}: duplicate medicine id '{medicine['id']}'")
            continue
        seen.add(medicine["id"])
        output.append(medicine)
    if errors:
        raise CatalogueError("medicine catalogue failed validation", errors)
    return output


def load_kendras(path: Path) -> list[dict[str, Any]]:
    raw, _ = _read(path, KENDRA_FIELDS)
    output: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(raw, start=2):
        try:
            kendra = validate_kendra(row, index)
        except CatalogueError as exc:
            errors.extend(exc.errors)
            continue
        if kendra["id"] in seen:
            errors.append(f"line {index}: duplicate kendra id '{kendra['id']}'")
            continue
        seen.add(kendra["id"])
        output.append(kendra)
    if errors:
        raise CatalogueError("kendra catalogue failed validation", errors)
    return output


def catalogue_mode(medicines: Iterable[dict[str, Any]], kendras: Iterable[dict[str, Any]]) -> str:
    medicine_rows = list(medicines)
    kendra_rows = list(kendras)
    flags = [int(row.get("is_sample") or 0) for row in (*medicine_rows, *kendra_rows)]
    if not flags:
        return "empty"
    if all(flags):
        return "sample"
    if not any(flags):
        return "verified"
    return "mixed"
