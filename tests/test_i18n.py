"""Bilingual coverage and translation fallback tests."""

from __future__ import annotations

import re
from pathlib import Path

from core.i18n import (
    DEFAULT_LANGUAGE,
    HI,
    REASON_TEXT,
    SUPPORTED_LANGUAGES,
    js_strings,
    reason_label,
    translate,
)

ROOT = Path(__file__).resolve().parent.parent


def template_keys() -> set[str]:
    keys: set[str] = set()
    for path in (ROOT / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        keys.update(match.group(2) for match in re.finditer(r"\bt\((['\"])(.*?)\1\)", text))
    return keys


def js_keys() -> set[str]:
    text = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r'tr\("([^"]*)"\)', text)}


def test_every_static_template_string_has_a_hindi_translation():
    missing = sorted(template_keys() - set(HI))
    assert not missing, f"missing Hindi translations: {missing}"


def test_every_javascript_string_is_available_to_the_client():
    missing = sorted(js_keys() - set(js_strings("en")))
    assert not missing, f"JavaScript strings missing from js_strings: {missing}"
    missing_hi = sorted(js_keys() - set(HI))
    assert not missing_hi, f"JavaScript strings missing Hindi: {missing_hi}"


def test_hindi_translation_never_returns_a_raw_key_for_covered_ui_text():
    for key in sorted(template_keys() | js_keys()):
        translated = translate(key, "hi")
        assert translated
        assert translated != key or key in {"English"}, key


def test_supported_languages_and_english_passthrough():
    assert SUPPORTED_LANGUAGES == ("en", "hi")
    assert DEFAULT_LANGUAGE == "en"
    assert translate("Search", "en") == "Search"
    assert translate("Not a real key", "hi") == "Not a real key"


def test_every_matcher_reason_has_bilingual_labels():
    expected = {
        "COMPOSITION_MISMATCH",
        "COMBINATION_MISMATCH",
        "STRENGTH_MISMATCH",
        "STRENGTH_COUNT_MISMATCH",
        "STRENGTH_NOT_SPECIFIED",
        "STRENGTH_UNVERIFIED",
        "STRENGTH_UNIT_INFERRED",
        "FORM_MISMATCH",
        "FORM_VARIANT_DIFFERS",
        "FORM_NOT_SPECIFIED",
        "ROUTE_MISMATCH",
        "ROUTE_UNVERIFIED",
        "RELEASE_MISMATCH",
        "RELEASE_DIFFERS",
        "RELEASE_UNVERIFIED",
        "GENERIC_NAME_MISMATCH",
        "GENERIC_NAME_MATCH",
        "BRAND_NAME_MATCH",
        "BRAND_NOT_IN_CATALOGUE",
        "NAME_ONLY",
        "NO_COMPOSITION_DATA",
        "CANDIDATE_DATA_INCOMPLETE",
        "EXACT_COMPOSITION_STRENGTH_FORM",
    }
    assert expected <= set(REASON_TEXT)
    for code in expected:
        assert reason_label(code, "en")
        assert reason_label(code, "hi")
        assert reason_label(code, "hi") != code
    assert reason_label("SOMETHING_NEW", "en") == "Something new"


def test_error_page_strings_are_translated():
    for key in (
        "Invalid request.",
        "Please check the form and try again.",
        "Page not found",
        "The page you asked for does not exist.",
        "File too large",
        "The uploaded file is too large.",
        "Too many requests",
        "Please wait a moment and try again.",
        "Something went wrong",
        "An unexpected error occurred. Please try again.",
        "This service is temporarily unavailable.",
    ):
        assert key in HI, key
