"""Regression tests for the safety-critical matching engine."""

from __future__ import annotations

import pytest

from core.matcher import (
    COMPATIBLE,
    EXACT,
    REJECTED,
    UNCERTAIN,
    Candidate,
    Query,
    brand_matches,
    evaluate_match,
    find_alternatives,
    identify_text,
)
from core.medicine import parse_strengths


def make_candidate(**overrides) -> Candidate:
    values = {
        "id": "paracetamol-500-tablet",
        "generic_name": "Paracetamol",
        "brand_names": ("Dolo 500", "Calpol 500"),
        "composition": "Paracetamol",
        "strengths": parse_strengths("500 mg"),
        "form": "tablet",
        "route": "oral",
        "release": "immediate",
    }
    values.update(overrides)
    return Candidate(**values)


def query_for(candidate: Candidate, **overrides) -> Query:
    query = Query.from_candidate(candidate)
    for key, value in overrides.items():
        setattr(query, key, value)
    return query


def test_exact_composition_strength_form_is_exact_and_not_a_recommendation():
    target = make_candidate()
    result = evaluate_match(query_for(target), target)
    assert result.status == EXACT
    assert result.is_safe_match
    assert result.requires_pharmacist is True
    assert result.substitution_not_authorised is True
    assert "EXACT_COMPOSITION_STRENGTH_FORM" not in result.reasons


def test_numeric_strength_mismatch_is_hard_rejected():
    target = make_candidate()
    other = make_candidate(id="paracetamol-650", strengths=parse_strengths("650 mg"))
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "STRENGTH_MISMATCH" in result.reasons
    assert result.requires_pharmacist is False


def test_equivalent_units_are_equal_but_units_are_not_guessed():
    target = make_candidate(strengths=parse_strengths("0.5 g"))
    other = make_candidate(strengths=parse_strengths("500 mg"))
    assert evaluate_match(query_for(target), other).status == EXACT


def test_composition_mismatch_and_partial_combination_are_rejected():
    target = make_candidate()
    other = make_candidate(
        composition="Paracetamol + Ibuprofen",
        strengths=parse_strengths("500 mg; 400 mg"),
    )
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "COMPOSITION_MISMATCH" in result.reasons
    assert "COMBINATION_MISMATCH" in result.reasons


def test_combination_order_does_not_change_composition_or_pairing():
    target = make_candidate(
        id="combination-a",
        generic_name="Ibuprofen + Paracetamol",
        composition="Ibuprofen; Paracetamol",
        strengths=parse_strengths("400 mg; 325 mg"),
    )
    other = make_candidate(
        id="combination-b",
        generic_name="Paracetamol + Ibuprofen",
        composition="Paracetamol + Ibuprofen",
        strengths=parse_strengths("325 mg; 400 mg"),
    )
    assert evaluate_match(query_for(target), other).status == EXACT


def test_missing_strength_never_becomes_an_exact_match():
    target = make_candidate()
    other = make_candidate(id="unknown-strength", strengths=())
    result = evaluate_match(query_for(target), other)
    assert result.status == UNCERTAIN
    assert "STRENGTH_UNVERIFIED" in result.reasons


def test_inferred_bare_strength_stays_uncertain():
    target = make_candidate()
    query = query_for(target, strengths=parse_strengths("500 mg"), strength_unit_inferred=True)
    result = evaluate_match(query, target)
    assert result.status == UNCERTAIN
    assert "STRENGTH_UNIT_INFERRED" in result.reasons


def test_different_dosage_form_is_rejected():
    target = make_candidate()
    other = make_candidate(id="syrup", form="syrup")
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "FORM_MISMATCH" in result.reasons


def test_form_variant_is_uncertain_not_equal():
    target = make_candidate()
    other = make_candidate(id="dispersible", form="dispersible tablet")
    result = evaluate_match(query_for(target), other)
    assert result.status == UNCERTAIN
    assert "FORM_VARIANT_DIFFERS" in result.reasons


def test_route_mismatch_is_rejected():
    target = make_candidate()
    other = make_candidate(id="injection", form="injection", route="injection")
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "ROUTE_MISMATCH" in result.reasons


def test_immediate_and_extended_release_are_never_interchangeable():
    target = make_candidate()
    other = make_candidate(id="er", release="extended", form="tablet")
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "RELEASE_MISMATCH" in result.reasons


def test_two_modified_release_mechanisms_require_pharmacist_review():
    target = make_candidate(id="er", release="extended")
    other = make_candidate(id="mr", release="modified")
    result = evaluate_match(query_for(target), other)
    assert result.status == UNCERTAIN
    assert "RELEASE_DIFFERS" in result.reasons
    assert result.requires_pharmacist is True


def test_missing_route_or_release_is_uncertain():
    target = make_candidate()
    other = make_candidate(id="unknown-route", route="")
    result = evaluate_match(query_for(target), other)
    assert result.status == UNCERTAIN
    assert "ROUTE_UNVERIFIED" in result.reasons
    other = make_candidate(id="unknown-release", release="")
    result = evaluate_match(query_for(target), other)
    assert result.status == UNCERTAIN
    assert "RELEASE_UNVERIFIED" in result.reasons


def test_generic_name_is_checked_before_brand_and_mismatch_rejects():
    target = make_candidate()
    other = make_candidate(
        id="metformin",
        generic_name="Metformin hydrochloride",
        composition="Metformin hydrochloride",
        strengths=parse_strengths("500 mg"),
        brand_names=("Dolo 500",),
    )
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "GENERIC_NAME_MISMATCH" in result.reasons


def test_brand_match_is_literal_and_cannot_override_composition():
    assert brand_matches("Dolo 650", ("Dolo 650", "Calpol 650"))
    assert not brand_matches("Dolo 650", ("Dolo 500",))
    target = make_candidate(brand_names=("Dolo 500",))
    other = make_candidate(
        id="another",
        generic_name="Metformin hydrochloride",
        composition="Metformin hydrochloride",
        strengths=parse_strengths("500 mg"),
        brand_names=("Dolo 500",),
    )
    result = evaluate_match(query_for(target), other)
    assert result.status == REJECTED
    assert "GENERIC_NAME_MISMATCH" in result.reasons


def test_name_only_match_is_uncertain():
    query = Query(generic_name="Paracetamol", text="Paracetamol")
    result = evaluate_match(query, make_candidate())
    assert result.status == UNCERTAIN
    assert "NO_COMPOSITION_DATA" in result.reasons
    assert not result.is_safe_match


def test_find_alternatives_returns_only_strict_matches_and_never_self():
    target = make_candidate()
    same = make_candidate(id="same", brand_names=())
    different = make_candidate(id="different", strengths=parse_strengths("650 mg"))
    another_form = make_candidate(id="another-form", form="syrup")
    results = find_alternatives(target, [target, same, different, another_form])
    assert [candidate.id for candidate, _ in results] == ["same"]


def test_find_alternatives_is_empty_when_target_data_is_incomplete():
    target = make_candidate(composition="", strengths=())
    results = find_alternatives(target, [make_candidate()])
    assert results == []


def test_identify_text_is_uncertain_identification_support():
    catalogue = [
        make_candidate(brand_names=("Dolo 650",), strengths=parse_strengths("650 mg")),
        make_candidate(id="dolo-500", brand_names=("Dolo 500",)),
    ]
    results = identify_text("Tab Dolo 650", catalogue)
    assert results
    top = results[0]
    assert top["candidate"].id == "paracetamol-500-tablet"
    assert top["evaluation"].status == UNCERTAIN
    assert top["confidence"] <= 0.85
    assert top["confidence"] > 0


def test_identify_text_does_not_confuse_500_and_650_brands():
    catalogue = [
        make_candidate(id="dolo-500", brand_names=("Dolo 500",), strengths=parse_strengths("500 mg")),
        make_candidate(id="dolo-650", brand_names=("Dolo 650",), strengths=parse_strengths("650 mg")),
    ]
    results = identify_text("Dolo 650", catalogue)
    assert [item["candidate"].id for item in results] == ["dolo-650"]


def test_lookalike_name_is_not_a_match():
    target = make_candidate()
    lookalike = make_candidate(
        id="paracetamol-lookalike",
        generic_name="Paracetamol",
        composition="Paracetamol",
        strengths=parse_strengths("650 mg"),
    )
    result = evaluate_match(query_for(target), lookalike)
    assert result.status == REJECTED
    assert not result.is_safe_match


@pytest.mark.parametrize(
    "release,form,expected",
    [
        ("immediate", "tablet", "immediate"),
        ("extended", "tablet", "extended"),
        ("modified", "tablet", "modified"),
        ("delayed", "tablet", "delayed"),
        ("extended", "extended release tablet", "extended"),
        ("unknown", "tablet", "unknown"),
    ],
)
def test_release_labels_are_normalised_without_losing_meaning(release, form, expected):
    from core.medicine import normalize_release

    assert normalize_release(release, form) == expected
