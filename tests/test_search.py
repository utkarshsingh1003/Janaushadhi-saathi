"""Catalogue discovery tests, including the separation from safety matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import db
from core.matcher import Candidate, Query, evaluate_match
from core.search import MIN_SCORE, score_row, search_rows

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def catalogue(tmp_path_factory):
    path = tmp_path_factory.mktemp("search") / "catalogue.db"
    db.init_database(
        path,
        ROOT / "data" / "medicines.csv",
        ROOT / "data" / "kendras.csv",
        force_reload=True,
    )
    connection = db.connect(path)
    try:
        return db.all_medicines(connection)
    finally:
        connection.close()


def test_typo_search_finds_the_medicine_without_claiming_compatibility(catalogue):
    result = search_rows(catalogue, query="paracetmol", per_page=10)
    assert result["total"] >= 1
    assert any(item["row"]["generic_name"] == "Paracetamol" for item in result["entries"])
    assert result["entries"][0]["score"] >= MIN_SCORE

    match = Candidate.from_row(result["entries"][0]["row"])
    evaluation = evaluate_match(Query(generic_name=match.generic_name), match)
    assert evaluation.is_safe_match is False


def test_brand_and_product_code_search(catalogue):
    brand = search_rows(catalogue, query="Dolo 650")
    assert brand["entries"]
    assert brand["entries"][0]["row"]["id"] == "paracetamol-650-tablet"

    code = search_rows(catalogue, query="SAMPLE-001")
    assert code["entries"]
    assert code["entries"][0]["row"]["id"] == "paracetamol-500-tablet"


def test_form_and_category_filters_and_sorting(catalogue):
    syrups = search_rows(catalogue, form="syrup")
    assert syrups["total"] >= 1
    assert all(item["row"]["dosage_form"] == "syrup" for item in syrups["entries"])

    pain = search_rows(catalogue, category="Pain and fever")
    assert pain["total"] >= 1
    assert all(item["row"]["category"] == "Pain and fever" for item in pain["entries"])

    by_name = search_rows(catalogue, sort="name", per_page=50)
    names = [item["row"]["generic_name"].lower() for item in by_name["entries"]]
    assert names == sorted(names)

    by_price = search_rows(catalogue, sort="price", per_page=60)
    prices = [item["row"]["jan_price_paise"] for item in by_price["entries"]]
    non_null = [price for price in prices if price is not None]
    assert non_null == sorted(non_null)


def test_pagination_is_bounded_and_stable(catalogue):
    first = search_rows(catalogue, page=1, per_page=10)
    second = search_rows(catalogue, page=2, per_page=10)
    last = search_rows(catalogue, page=99999, per_page=10)
    assert first["pages"] == 6
    assert last["page"] == first["pages"]
    assert first["entries"] != second["entries"]
    assert first["entries"][0]["row"]["id"] != second["entries"][0]["row"]["id"]


def test_empty_query_returns_catalogue_sorted_by_name(catalogue):
    result = search_rows(catalogue, query="")
    assert result["total"] == 54
    assert result["entries"][0]["score"] == 100.0


def test_score_row_handles_empty_fields():
    score, field = score_row("paracetamol", {"generic_name": "", "common_brands": ""})
    assert score >= 0
    assert field == ""
