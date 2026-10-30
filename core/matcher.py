"""Strict, safety-first medicine matching.

This module intentionally offers **no** function that recommends a medicine.
It answers a narrower question: "does this catalogue record have the same
composition, strength, dosage form, route and release mechanism as the query?"

Fuzzy string similarity is never used here. Fuzzy matching lives in
``core.search`` and only affects result ordering; it can never make two
medicines "compatible".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .medicine import (
    FORM_ALIASES,
    Strength,
    canonical_ingredient,
    composition_pairs,
    form_family,
    normalize_form,
    normalize_release,
    normalize_route,
    parse_bare_strength,
    parse_strengths,
    release_is_modified,
    split_ingredients,
    strengths_equal,
)

EXACT = "exact"
COMPATIBLE = "compatible"
UNCERTAIN = "uncertain"
REJECTED = "rejected"

STATUS_ORDER = {REJECTED: 0, UNCERTAIN: 1, COMPATIBLE: 2, EXACT: 3}


@dataclass(frozen=True)
class Candidate:
    id: str
    generic_name: str
    brand_names: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    composition: str = ""
    strengths: tuple[Strength, ...] = ()
    form: str = ""
    route: str = ""
    release: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Candidate":
        def _split(value: Any) -> tuple[str, ...]:
            if not value:
                return ()
            return tuple(part.strip() for part in re.split(r"[;|]", str(value)) if part.strip())

        return cls(
            id=str(row["id"]),
            generic_name=str(row.get("generic_name") or ""),
            brand_names=_split(row.get("common_brands")),
            synonyms=_split(row.get("synonyms")),
            composition=str(row.get("composition") or ""),
            strengths=parse_strengths(str(row.get("strength") or "")),
            form=str(row.get("dosage_form") or ""),
            route=str(row.get("route") or ""),
            release=str(row.get("release_type") or ""),
        )

    @property
    def ingredients(self) -> tuple[str, ...]:
        return split_ingredients(self.composition)


@dataclass
class Query:
    text: str = ""
    generic_name: str | None = None
    brand_name: str | None = None
    ingredients: tuple[str, ...] | None = None
    strengths: tuple[Strength, ...] | None = None
    form: str | None = None
    route: str | None = None
    release: str | None = None
    strength_unit_inferred: bool = False
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> "Query":
        return cls(
            text=candidate.generic_name,
            generic_name=candidate.generic_name,
            ingredients=candidate.ingredients,
            strengths=candidate.strengths,
            form=candidate.form,
            route=candidate.route,
            release=candidate.release,
        )


@dataclass
class Evaluation:
    candidate_id: str
    status: str
    reasons: tuple[str, ...]
    checks: dict[str, Any]
    requires_pharmacist: bool = True
    substitution_not_authorised: bool = True

    @property
    def is_safe_match(self) -> bool:
        return self.status in {EXACT, COMPATIBLE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "medicine_id": self.candidate_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "checks": self.checks,
            "requires_pharmacist": self.requires_pharmacist,
            "substitution_not_authorised": True,
        }


def _normal_word(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def brand_matches(brand: str, branded_names: Iterable[str]) -> bool:
    """Deliberately literal brand matching (no fuzzy scoring)."""
    target_words = [w for w in _normal_word(brand).split() if len(w) >= 3]
    if not target_words:
        return False
    for name in branded_names:
        normal = _normal_word(name)
        if not normal:
            continue
        if normal == _normal_word(brand):
            return True
        # Every meaningful word of the catalogue brand must appear in the query,
        # so "Dolo 650" does not silently match "Dolo 500".
        name_words = [w for w in normal.split() if len(w) >= 3]
        if name_words and all(word in target_words for word in name_words):
            return True
    return False


def _generic_matches(query_name: str, candidate: Candidate) -> bool:
    wanted = _name_key(query_name)
    if not wanted:
        return False
    allowed = {_name_key(candidate.generic_name)}
    allowed.update(_name_key(s) for s in candidate.synonyms)
    return wanted in allowed


def _name_key(name: str) -> tuple[str, ...]:
    """Compare combination generic names without depending on ingredient order."""
    parts: list[str] = []
    for part in split_ingredients(name):
        for sub_part in part.split("+"):
            canonical = canonical_ingredient(sub_part)
            if canonical:
                parts.append(canonical)
    return tuple(sorted(parts))


def evaluate_match(query: Query, candidate: Candidate) -> Evaluation:
    rejected: list[str] = []
    uncertain: list[str] = []
    confirmed: list[str] = []
    checks: dict[str, Any] = {}

    # 1. Generic name is checked before any brand claim.
    if query.generic_name:
        if _generic_matches(query.generic_name, candidate):
            confirmed.append("GENERIC_NAME_MATCH")
            checks["generic_name"] = "match"
        else:
            rejected.append("GENERIC_NAME_MISMATCH")
            checks["generic_name"] = "mismatch"

    # 2. Exact composition (order-insensitive, combination-aware).
    if query.ingredients:
        if not candidate.ingredients:
            uncertain.append("CANDIDATE_DATA_INCOMPLETE")
            checks["composition"] = "candidate_missing"
        elif tuple(sorted(query.ingredients)) != tuple(sorted(candidate.ingredients)):
            rejected.append("COMPOSITION_MISMATCH")
            if set(query.ingredients) & set(candidate.ingredients):
                rejected.append("COMBINATION_MISMATCH")
            checks["composition"] = "mismatch"
        else:
            checks["composition"] = "match"

    # 3. Strength. Numeric mismatches are hard rejections.
    if query.strengths:
        if not candidate.strengths:
            uncertain.append("STRENGTH_UNVERIFIED")
            checks["strength"] = "candidate_missing"
        elif query.ingredients and len(query.ingredients) == len(query.strengths):
            q_pairs = composition_pairs(";".join(query.ingredients), query.strengths)
            c_pairs = composition_pairs(candidate.composition, candidate.strengths)
            if q_pairs and c_pairs:
                if q_pairs != c_pairs:
                    rejected.append("STRENGTH_MISMATCH")
                    checks["strength"] = "mismatch"
                else:
                    checks["strength"] = "match"
            elif not strengths_equal(query.strengths, candidate.strengths):
                rejected.append("STRENGTH_MISMATCH")
                checks["strength"] = "mismatch"
            else:
                checks["strength"] = "match"
        elif len(query.strengths) != len(candidate.strengths):
            uncertain.append("STRENGTH_COUNT_MISMATCH")
            checks["strength"] = "count_mismatch"
        elif strengths_equal(query.strengths, candidate.strengths):
            checks["strength"] = "match"
        else:
            rejected.append("STRENGTH_MISMATCH")
            checks["strength"] = "mismatch"
        if query.strength_unit_inferred:
            uncertain.append("STRENGTH_UNIT_INFERRED")
    else:
        uncertain.append("STRENGTH_NOT_SPECIFIED")
        checks["strength"] = "not_specified"

    # 4. Dosage form: same family but different variant is uncertain, not equal.
    qf = normalize_form(query.form)
    cf = normalize_form(candidate.form)
    if not qf:
        uncertain.append("FORM_NOT_SPECIFIED")
        checks["form"] = "not_specified"
    elif not cf:
        uncertain.append("CANDIDATE_DATA_INCOMPLETE")
        checks["form"] = "candidate_missing"
    elif qf == cf:
        checks["form"] = "match"
    elif form_family(qf) == form_family(cf):
        uncertain.append("FORM_VARIANT_DIFFERS")
        checks["form"] = "variant_differs"
    else:
        rejected.append("FORM_MISMATCH")
        checks["form"] = "mismatch"

    # 5. Route.
    qr = normalize_route(query.route)
    cr = normalize_route(candidate.route)
    if not qr or not cr:
        uncertain.append("ROUTE_UNVERIFIED")
        checks["route"] = "unverified"
    elif qr != cr:
        rejected.append("ROUTE_MISMATCH")
        checks["route"] = "mismatch"
    else:
        checks["route"] = "match"

    # 6. Release mechanism (immediate vs modified release is never automatic).
    if not query.release or not candidate.release:
        uncertain.append("RELEASE_UNVERIFIED")
        checks["release"] = "unverified"
    else:
        qrel = normalize_release(query.release, query.form)
        crel = normalize_release(candidate.release, candidate.form)
        if qrel == "unknown" or crel == "unknown":
            uncertain.append("RELEASE_UNVERIFIED")
            checks["release"] = "unverified"
        elif qrel != crel:
            if release_is_modified(qrel) != release_is_modified(crel):
                rejected.append("RELEASE_MISMATCH")
            else:
                uncertain.append("RELEASE_DIFFERS")
            checks["release"] = "mismatch"
        else:
            checks["release"] = "match"

    # 7. Brand is informational only; it cannot override composition checks.
    if query.brand_name:
        if brand_matches(query.brand_name, candidate.brand_names):
            confirmed.append("BRAND_NAME_MATCH")
            checks["brand"] = "match"
        else:
            checks["brand"] = "not_listed"
            if not query.ingredients and not query.generic_name:
                uncertain.append("BRAND_NOT_IN_CATALOGUE")
                uncertain.append("NAME_ONLY")

    # A bare name can never be an exact match.
    if not query.ingredients and not query.generic_name:
        uncertain.append("NAME_ONLY")
    if not query.ingredients:
        uncertain.append("NO_COMPOSITION_DATA")

    if rejected:
        status = REJECTED
    elif uncertain:
        status = UNCERTAIN
    elif query.ingredients and query.strengths:
        status = EXACT
    else:
        status = COMPATIBLE

    reasons = []
    for code in (*rejected, *uncertain, *confirmed):
        if code not in reasons:
            reasons.append(code)

    return Evaluation(
        candidate_id=candidate.id,
        status=status,
        reasons=tuple(reasons),
        checks=checks,
        requires_pharmacist=status != REJECTED,
    )


def find_alternatives(
    target: Candidate, catalogue: Iterable[Candidate], limit: int = 10
) -> list[tuple[Candidate, Evaluation]]:
    """Return only records that pass every strict check.

    If the target lacks composition, strength, form, route or release data, the
    result is empty rather than a guess.
    """
    query = Query.from_candidate(target)
    matches: list[tuple[Candidate, Evaluation]] = []
    for candidate in catalogue:
        if candidate.id == target.id:
            continue
        result = evaluate_match(query, candidate)
        if result.is_safe_match:
            matches.append((candidate, result))
    matches.sort(key=lambda item: (item[1].status != EXACT, item[0].generic_name.lower()))
    return matches[:limit]


def _contains_phrase(text_words: list[str], phrase: str) -> bool:
    words = [w for w in _normal_word(phrase).split() if len(w) >= 3]
    if not words:
        return False
    return all(word in text_words for word in words)


def identify_text(text: str, catalogue: Iterable[Candidate], limit: int = 5) -> list[dict[str, Any]]:
    """Find possible catalogue entries mentioned in free text.

    The output is always marked ``uncertain``. It is identification support for
    human review, never a prescribing or substitution recommendation.
    """
    raw = (text or "").strip()
    if not raw:
        return []

    lowered = raw.lower()
    text_words = _normal_word(raw).split()
    strengths = parse_strengths(raw)
    inferred = False
    if not strengths:
        bare = parse_bare_strength(raw)
        if bare is not None:
            strengths = (Strength(value=bare, unit="mg", raw=str(bare)),)
            inferred = True

    # Recognise dosage form words without guessing.
    form_hint = ""
    for alias in sorted(FORM_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            form_hint = FORM_ALIASES[alias]
            break

    results: list[dict[str, Any]] = []
    for candidate in catalogue:
        evidence: list[str] = []
        score = 0.0
        brand_hit = any(
            _contains_phrase(text_words, name) or _contains_phrase(text_words, name.split()[0])
            and len(name.split()[0]) >= 5
            for name in candidate.brand_names
        )
        # Require the full brand phrase when it contains a number, to avoid
        # confusing 500 mg and 650 mg products.
        if brand_hit:
            for name in candidate.brand_names:
                words = _normal_word(name).split()
                if words and all(w in text_words for w in words if len(w) >= 3):
                    score = max(score, 0.55)
                    evidence.append("BRAND_NAME_MATCH")
                    break

        generic_hit = _contains_phrase(text_words, candidate.generic_name)
        if not generic_hit:
            for synonym in candidate.synonyms:
                if _contains_phrase(text_words, synonym):
                    generic_hit = True
                    break
        if generic_hit:
            score = max(score, 0.5)
            evidence.append("GENERIC_NAME_MATCH")

        ingredient_hit = any(
            _contains_phrase(text_words, ingredient) for ingredient in candidate.ingredients
        )
        if ingredient_hit and not generic_hit:
            score = max(score, 0.35)
            evidence.append("GENERIC_NAME_MATCH")

        if not evidence:
            continue

        query = Query(
            text=raw,
            generic_name=candidate.generic_name if generic_hit or ingredient_hit else None,
            brand_name=None,
            ingredients=None,
            strengths=strengths or None,
            form=form_hint or None,
            route=None,
            release=None,
            strength_unit_inferred=inferred,
        )
        evaluation = evaluate_match(query, candidate)

        if evaluation.status == REJECTED:
            continue

        if strengths and candidate.strengths and strengths_equal(strengths, candidate.strengths):
            score += 0.2
            evidence.append("STRENGTH_MATCH")
        if form_hint and normalize_form(form_hint) == normalize_form(candidate.form):
            score += 0.1
            evidence.append("FORM_MATCH")
        if evaluation.status == UNCERTAIN:
            score = min(score, 0.85)
        if score < 0.45:
            continue

        results.append(
            {
                "candidate": candidate,
                "evaluation": evaluation,
                "confidence": round(min(score, 0.85), 2),
                "evidence": tuple(dict.fromkeys(evidence)),
            }
        )

    results.sort(key=lambda item: (-item["confidence"], item["candidate"].generic_name.lower()))
    return results[:limit]
