"""Typo-tolerant catalogue discovery.

Fuzzy scores here only rank *search results*. They are never passed to the
matching engine and cannot establish that two medicines are interchangeable.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from rapidfuzz import fuzz

from .medicine import normalize_code, normalize_form

MIN_SCORE = 55.0


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def searchable_fields(row: Mapping[str, Any]) -> list[tuple[str, str, float]]:
    """Return (field name, text, weight) tuples used for ranking."""
    fields: list[tuple[str, str, float]] = [
        ("generic_name", str(row.get("generic_name") or ""), 1.0),
        ("composition", str(row.get("composition") or ""), 0.95),
        ("product_code", str(row.get("product_code") or ""), 1.0),
        ("strength", str(row.get("strength") or ""), 0.5),
        ("dosage_form", str(row.get("dosage_form") or ""), 0.4),
    ]
    for brand in str(row.get("common_brands") or "").split(";"):
        if brand.strip():
            fields.append(("brand", brand.strip(), 0.9))
    for synonym in str(row.get("synonyms") or "").split(";"):
        if synonym.strip():
            fields.append(("synonym", synonym.strip(), 0.85))
    return fields


def score_row(query: str, row: Mapping[str, Any]) -> tuple[float, str]:
    q = _norm(query)
    if not q:
        return (0.0, "")
    best = 0.0
    best_field = ""
    for name, text, weight in searchable_fields(row):
        candidate = _norm(text)
        if not candidate:
            continue
        if normalize_code(q) and normalize_code(q) == normalize_code(candidate):
            score = 100.0
        elif q in candidate or candidate in q:
            # Prefer whole-phrase substring matches strongly.
            score = 96.0 if len(q) >= 3 else 80.0
        else:
            score = max(
                fuzz.WRatio(q, candidate),
                fuzz.token_set_ratio(q, candidate),
                fuzz.partial_ratio(q, candidate) * 0.85,
            )
        score *= weight
        if score > best:
            best = score
            best_field = name
    return (round(best, 2), best_field)


def search_rows(
    rows: list[Mapping[str, Any]],
    query: str = "",
    form: str = "",
    category: str = "",
    sort: str = "relevance",
    page: int = 1,
    per_page: int = 10,
) -> dict[str, Any]:
    """Filter, rank and paginate in-memory catalogue rows."""
    filtered: list[tuple[Mapping[str, Any], float, str]] = []
    q = (query or "").strip()
    form_key = normalize_form(form)
    category_key = (category or "").strip().lower()

    for row in rows:
        if form_key and normalize_form(str(row.get("dosage_form") or "")) != form_key:
            continue
        if category_key and str(row.get("category") or "").lower() != category_key:
            continue
        score, field = score_row(q, row) if q else (100.0, "")
        if q and score < MIN_SCORE:
            continue
        filtered.append((row, score, field))

    if sort == "name":
        filtered.sort(key=lambda item: str(item[0].get("generic_name") or "").lower())
    elif sort == "price":
        filtered.sort(
            key=lambda item: (
                item[0].get("jan_price_paise") is None,
                item[0].get("jan_price_paise") or 0,
            )
        )
    else:
        if q:
            filtered.sort(
                key=lambda item: (-item[1], str(item[0].get("generic_name") or "").lower())
            )
        else:
            filtered.sort(key=lambda item: str(item[0].get("generic_name") or "").lower())

    total = len(filtered)
    per_page = max(1, min(int(per_page), 50))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    start = (page - 1) * per_page
    window = filtered[start : start + per_page]

    return {
        "entries": [
            {"row": row, "score": score, "matched_field": field}
            for row, score, field in window
        ],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "query": q,
    }
