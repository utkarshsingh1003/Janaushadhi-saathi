"""Catalogue schema, provenance and sample-data honesty tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from core.catalogue import (
    CatalogueError,
    KENDRA_FIELDS,
    MEDICINE_FIELDS,
    catalogue_mode,
    load_kendras,
    load_medicines,
    validate_kendra,
    validate_medicine,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def medicine_row(**overrides):
    row = {
        "id": "paracetamol-500-tablet",
        "generic_name": "Paracetamol",
        "synonyms": "Acetaminophen",
        "common_brands": "Dolo 500",
        "composition": "Paracetamol",
        "strength": "500 mg",
        "dosage_form": "tablet",
        "route": "oral",
        "release_type": "immediate",
        "pack_size": "15",
        "pack_unit": "tablet",
        "category": "Pain and fever",
        "product_code": "SAMPLE-001",
        "jan_price": "6.00",
        "jan_price_source": "Illustrative sample",
        "jan_price_verified": "0",
        "is_sample": "1",
        "source_name": "Bundled sample",
        "source_url": "",
        "last_verified": "",
    }
    row.update(overrides)
    return row


def kendra_row(**overrides):
    row = {
        "id": "sample-kendra-delhi",
        "kendra_code": "SAMPLE-DL-001",
        "name": "Sample Kendra record - Delhi (not a real location)",
        "owner_name": "",
        "address": "Illustrative sample record only; no real address is stated.",
        "locality": "Sample locality",
        "city": "New Delhi",
        "district": "Central Delhi",
        "state": "Delhi",
        "pincode": "110001",
        "phone": "",
        "latitude": "28.6139",
        "longitude": "77.2090",
        "coordinate_precision": "city",
        "is_sample": "1",
        "source_name": "Bundled sample",
        "source_url": "",
        "last_verified": "",
    }
    row.update(overrides)
    return row


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_bundled_catalogue_is_explicitly_sample_data():
    medicines = load_medicines(DATA / "medicines.csv")
    kendras = load_kendras(DATA / "kendras.csv")
    assert len(medicines) == 54
    assert len(kendras) == 15
    assert catalogue_mode(medicines, kendras) == "sample"
    assert all(row["is_sample"] == 1 for row in medicines)
    assert all(row["jan_price_verified"] == 0 for row in medicines)
    assert all(row["is_sample"] == 1 for row in kendras)
    assert all("not a real location" in row["name"].lower() for row in kendras)
    assert all(row["phone"] == "" for row in kendras)
    assert all(row["product_code"].startswith("SAMPLE-") for row in medicines)


def test_sources_file_links_only_to_official_https_pages():
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    assert sources["medicines"]["mode"] == "sample"
    assert sources["kendras"]["mode"] == "sample"
    assert "NOT an official" in sources["medicines"]["description"]
    assert sources["official_links"]
    for link in sources["official_links"]:
        assert link["url"].startswith("https://janaushadhi.gov.in/")
        assert link["label"] and link["note"]


def test_verified_sample_price_is_rejected():
    with pytest.raises(CatalogueError) as error:
        validate_medicine(
            medicine_row(
                jan_price_verified="1",
                is_sample="1",
                jan_price_source="Sample source",
                last_verified="2026-10-30",
                source_url="https://example.invalid/source",
            )
        )
    assert any("sample rows cannot mark a price as verified" in item for item in error.value.errors)


def test_verified_price_requires_source_date_and_price():
    with pytest.raises(CatalogueError) as error:
        validate_medicine(
            medicine_row(
                jan_price_verified="1",
                is_sample="0",
                jan_price="",
                jan_price_source="",
                last_verified="",
                source_name="Official list",
                source_url="https://janaushadhi.gov.in/download-link",
            )
        )
    messages = " ".join(error.value.errors)
    assert "verified price requires jan_price" in messages
    assert "jan_price_source" in messages
    assert "last_verified" in messages


def test_non_sample_rows_require_provenance():
    with pytest.raises(CatalogueError) as error:
        validate_medicine(
            medicine_row(
                is_sample="0",
                source_name="",
                source_url="",
                last_verified="",
            )
        )
    messages = " ".join(error.value.errors)
    assert "non-sample row requires source_name" in messages
    assert "non-sample row requires source_url" in messages
    assert "non-sample row requires last_verified" in messages

    with pytest.raises(CatalogueError) as error:
        validate_kendra(
            kendra_row(
                is_sample="0",
                source_name="",
                source_url="",
                last_verified="",
            )
        )
    messages = " ".join(error.value.errors)
    assert "non-sample row requires source_name" in messages
    assert "non-sample row requires source_url" in messages
    assert "non-sample row requires last_verified" in messages


def test_combination_needs_one_strength_per_ingredient():
    with pytest.raises(CatalogueError) as error:
        validate_medicine(
            medicine_row(
                composition="Ibuprofen; Paracetamol",
                strength="400 mg",
                generic_name="Ibuprofen + Paracetamol",
            )
        )
    assert any("combination composition needs one strength" in item for item in error.value.errors)


def test_liquid_concentration_is_allowed():
    row = validate_medicine(
        medicine_row(
            id="paracetamol-syrup",
            generic_name="Paracetamol",
            composition="Paracetamol",
            strength="125 mg/5 ml",
            dosage_form="syrup",
            pack_size="60",
            pack_unit="ml",
        )
    )
    assert row["id"] == "paracetamol-syrup"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"id": "Bad ID"}, "id must be a lowercase slug"),
        ({"strength": "500"}, "strength must contain a number and unit"),
        ({"pack_size": "0"}, "pack_size must be greater than zero"),
        ({"last_verified": "30-10-2026"}, "last_verified must be YYYY-MM-DD"),
        ({"source_url": "javascript:alert(1)"}, "source_url must be an http(s) URL"),
        ({"release_type": "slow"}, "unsupported release_type"),
        ({"jan_price_verified": "yes"}, "jan_price_verified must be 0 or 1"),
    ],
)
def test_invalid_medicine_fields_are_reported(overrides, expected):
    with pytest.raises(CatalogueError) as error:
        validate_medicine(medicine_row(**overrides))
    assert any(expected in item for item in error.value.errors)


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"pincode": "123"}, "pincode must be six digits"),
        ({"latitude": "28.6", "longitude": ""}, "latitude and longitude must be supplied together"),
        ({"latitude": "100"}, "latitude out of range"),
        ({"phone": "call me"}, "phone contains unsupported characters"),
        ({"coordinate_precision": "exact"}, "unsupported coordinate_precision"),
        ({"is_sample": "no"}, "is_sample must be 0 or 1"),
    ],
)
def test_invalid_kendra_fields_are_reported(overrides, expected):
    with pytest.raises(CatalogueError) as error:
        validate_kendra(kendra_row(**overrides))
    assert any(expected in item for item in error.value.errors)


def test_duplicate_ids_and_missing_columns_are_rejected(tmp_path):
    row = medicine_row()
    path = write_csv(tmp_path / "duplicates.csv", MEDICINE_FIELDS, [row, row])
    with pytest.raises(CatalogueError) as error:
        load_medicines(path)
    assert any("duplicate medicine id" in item for item in error.value.errors)

    bad_path = tmp_path / "missing.csv"
    bad_path.write_text("id,generic_name\nx,Foo\n", encoding="utf-8")
    with pytest.raises(CatalogueError) as error:
        load_medicines(bad_path)
    assert any("missing columns" in str(item) or "missing column" in str(item) for item in error.value.errors)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(CatalogueError):
        load_medicines(tmp_path / "absent.csv")


def test_catalogue_mode_distinguishes_sample_mixed_and_verified():
    sample = [{"is_sample": 1}]
    real = [{"is_sample": 0}]
    assert catalogue_mode(sample, sample) == "sample"
    assert catalogue_mode(real, real) == "verified"
    assert catalogue_mode(sample, real) == "mixed"
    assert catalogue_mode([], []) == "empty"
