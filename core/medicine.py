"""Parsing and normalisation helpers for medicine names, compositions and strengths.

Safety principle: this module never guesses a missing strength, form or
composition. It normalises only what is explicitly present. Callers can see
which values were inferred through explicit flags.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Strengths
# ---------------------------------------------------------------------------

_UNIT_ALIASES = {
    "mcg": "mcg",
    "μg": "mcg",
    "ug": "mcg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "ml": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "iu": "iu",
    "unit": "unit",
    "units": "unit",
    "%": "%",
}

_STRENGTH_RE = re.compile(
    r"(?<![\w.])(\d{1,5}(?:[.,]\d{1,3})?)\s*(mcg|μg|ug|mg|g|ml|iu|units?|micrograms?|milligrams?|grams?|millilit(?:re|er)s?|%)(?![\w])",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"(?<![\w.])(\d{2,4})(?![\w.%])")


@dataclass(frozen=True)
class Strength:
    value: Decimal
    unit: str
    raw: str = ""

    def canonical(self) -> tuple[str, Decimal]:
        """Return a comparable (unit, value) pair.

        Mass units are normalised to milligrams so that 0.5 g == 500 mg and
        500 mcg == 0.5 mg. Volumes, IU and percentages stay in their unit.
        """
        if self.unit == "g":
            return ("mg", self.value * Decimal(1000))
        if self.unit == "mcg":
            return ("mg", self.value / Decimal(1000))
        return (self.unit, self.value)

    def label(self) -> str:
        value = self.value.normalize()
        text = format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")
        if not text:
            text = "0"
        return f"{text} {self.unit}"


def _decimal(raw: str) -> Decimal | None:
    text = raw.strip()
    if "," in text:
        # Treat 1,000 as a thousands separator and 0,5 as a decimal comma.
        if re.fullmatch(r"\d{1,3}(,\d{3})+", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def parse_strengths(text: str | None) -> tuple[Strength, ...]:
    """Extract every explicit strength with a unit, preserving order."""
    if not text:
        return ()
    found: list[Strength] = []
    for match in _STRENGTH_RE.finditer(text):
        value = _decimal(match.group(1))
        if value is None:
            continue
        unit = _UNIT_ALIASES.get(match.group(2).lower())
        if unit is None:
            continue
        found.append(Strength(value=value, unit=unit, raw=match.group(0).strip()))
    return tuple(found)


def parse_bare_strength(text: str | None) -> Decimal | None:
    """Find a single number without a unit (for example, 'Dolo 650').

    The caller must treat the result as inferred and mark the match uncertain.
    """
    if not text:
        return None
    values = {_decimal(m.group(1)) for m in _BARE_NUMBER_RE.finditer(text)}
    values.discard(None)
    if len(values) == 1:
        return next(iter(values))
    return None


def strength_pairs(strengths: tuple[Strength, ...]) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted(s.canonical() for s in strengths))


def strengths_equal(a: tuple[Strength, ...], b: tuple[Strength, ...]) -> bool:
    return strength_pairs(a) == strength_pairs(b)


def format_strengths(strengths: tuple[Strength, ...]) -> str:
    return " + ".join(s.label() for s in strengths)


# ---------------------------------------------------------------------------
# Ingredients
# ---------------------------------------------------------------------------

# Synonyms are deliberately conservative. Only well-established alternate
# spellings/names are mapped. Salt forms are not silently discarded.
_INGREDIENT_ALIASES = {
    "acetaminophen": "paracetamol",
    "amoxycillin": "amoxicillin",
    "amoxycillin trihydrate": "amoxicillin",
    "metformin": "metformin hydrochloride",
    "metformin hcl": "metformin hydrochloride",
    "metformin hydrocloride": "metformin hydrochloride",
    "levothyroxine": "levothyroxine sodium",
    "thyroxine": "levothyroxine sodium",
    "thyroxine sodium": "levothyroxine sodium",
    "salbutamol": "salbutamol",
    "albuterol": "salbutamol",
    "frusemide": "furosemide",
    "frutemide": "furosemide",
    "methylcobalamine": "methylcobalamin",
    "cyanocobalamine": "cyanocobalamin",
    "cholecalciferol": "cholecalciferol",
    "vitamin d3": "cholecalciferol",
    "vitamin b12": "methylcobalamin",
    "cotrimoxazole": "sulfamethoxazole + trimethoprim",
}

_INGREDIENT_SPLIT_RE = re.compile(r"\s*(?:;|\+|/)\s*")
_STRENGTH_IN_NAME_RE = _STRENGTH_RE


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower().replace("&", " and ")
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9%+ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonical_ingredient(name: str) -> str:
    """Normalise one ingredient name without removing its salt form."""
    text = _STRENGTH_IN_NAME_RE.sub(" ", name or "")
    text = _clean(text)
    text = re.sub(r"^(?:tab|tablet|cap|capsule|syp|syrup|inj|injection|susp|suspension)\s+", "", text)
    text = re.sub(r"\s+(?:tablet|tablets|capsule|capsules|syrup|injection)$", "", text).strip()
    return _INGREDIENT_ALIASES.get(text, text)


def split_ingredients(composition: str | None) -> tuple[str, ...]:
    if not composition:
        return ()
    parts = [canonical_ingredient(part) for part in _INGREDIENT_SPLIT_RE.split(composition)]
    return tuple(part for part in parts if part)


def composition_key(composition: str | None) -> tuple[str, ...]:
    """Order-insensitive ingredient key, preserving duplicates."""
    return tuple(sorted(split_ingredients(composition)))


def composition_pairs(
    composition: str | None, strengths: tuple[Strength, ...]
) -> tuple[tuple[str, str, str], ...] | None:
    """Pair each ingredient with its strength when counts line up."""
    ingredients = split_ingredients(composition)
    if not ingredients or not strengths or len(ingredients) != len(strengths):
        return None
    pairs = []
    for ingredient, strength in zip(ingredients, strengths):
        unit, value = strength.canonical()
        pairs.append((ingredient, unit, format(value.normalize(), "f")))
    return tuple(sorted(pairs))


# ---------------------------------------------------------------------------
# Dosage forms, routes and release mechanisms
# ---------------------------------------------------------------------------

FORM_ALIASES = {
    "tab": "tablet",
    "tabs": "tablet",
    "tablet": "tablet",
    "tablets": "tablet",
    "cap": "capsule",
    "caps": "capsule",
    "capsule": "capsule",
    "capsules": "capsule",
    "syp": "syrup",
    "syrup": "syrup",
    "susp": "suspension",
    "suspension": "suspension",
    "inj": "injection",
    "injection": "injection",
    "inhaler": "inhaler",
    "inhalation": "inhalation",
    "mdi": "inhaler",
    "dpi": "inhalation",
    "cream": "cream",
    "ointment": "ointment",
    "gel": "gel",
    "lotion": "lotion",
    "drops": "drops",
    "sachet": "sachet",
    "powder": "powder",
    "granules": "granules",
    "suppository": "suppository",
    "patch": "patch",
    "spray": "spray",
}

FORM_FAMILY_KEYWORDS = (
    ("inhal", "inhalation"),
    ("inject", "injection"),
    ("vial", "injection"),
    ("ampoule", "injection"),
    ("cream", "topical"),
    ("ointment", "topical"),
    ("lotion", "topical"),
    ("gel", "topical"),
    ("suppositor", "rectal"),
    ("patch", "transdermal"),
    ("ophthalm", "ophthalmic"),
    ("eye drop", "ophthalmic"),
    ("ear drop", "otic"),
    ("tablet", "oral_solid"),
    ("caplet", "oral_solid"),
    ("capsule", "oral_solid"),
    ("syrup", "oral_liquid"),
    ("suspension", "oral_liquid"),
    ("solution", "oral_liquid"),
    ("elixir", "oral_liquid"),
    ("sachet", "oral_powder"),
    ("powder", "oral_powder"),
    ("granule", "oral_powder"),
    ("spray", "nasal"),
)

_FORM_VARIANTS = (
    "dispersible",
    "chewable",
    "effervescent",
    "orodispersible",
    "film coated",
    "film-coated",
    "enteric",
    "sugar free",
    "sustained release",
    "extended release",
    "modified release",
    "delayed release",
)


def normalize_form(form: str | None) -> str:
    text = _clean(form or "")
    if not text:
        return ""
    return FORM_ALIASES.get(text, text)


def form_family(form: str | None) -> str:
    text = (form or "").lower()
    for keyword, family in FORM_FAMILY_KEYWORDS:
        if keyword in text:
            return family
    return "other"


def form_variant(form: str | None) -> tuple[str, ...]:
    text = (form or "").lower()
    return tuple(v for v in _FORM_VARIANTS if v in text)


def normalize_route(route: str | None) -> str:
    text = _clean(route or "")
    aliases = {
        "po": "oral",
        "by mouth": "oral",
        "oral": "oral",
        "topical": "topical",
        "skin": "topical",
        "inhalation": "inhalation",
        "inhaled": "inhalation",
        "injection": "injection",
        "iv": "injection",
        "im": "injection",
        "ophthalmic": "ophthalmic",
        "eye": "ophthalmic",
        "nasal": "nasal",
        "rectal": "rectal",
        "sublingual": "sublingual",
        "transdermal": "transdermal",
    }
    return aliases.get(text, text)


def normalize_release(release: str | None, form: str | None = None) -> str:
    rel = _clean(release or "")
    if rel in {"extended", "extended release", "sustained release", "controlled release", "sr", "xr", "er"}:
        return "extended"
    if rel in {"modified", "modified release", "mr"}:
        return "modified"
    if rel in {"delayed", "delayed release", "enteric", "enteric coated"}:
        return "delayed"
    if rel in {"immediate", "immediate release", "plain", "ir"}:
        return "immediate"

    text = _clean(f"{release or ''} {form or ''}")
    if not text:
        return "unknown"
    if any(phrase in text for phrase in ("extended release", "sustained release", "controlled release")):
        return "extended"
    if re.search(r"\b(?:sr|xr|er)\b", text):
        return "extended"
    if "modified release" in text or text.strip() in {"mr", "modified"}:
        return "modified"
    if "delayed release" in text or "enteric" in text:
        return "delayed"
    if any(word in text for word in ("immediate", "plain")) or re.search(r"\bir\b", text):
        return "immediate"
    return "unknown"


def release_is_modified(release: str | None) -> bool:
    return normalize_release(release) in {"extended", "modified", "delayed"}


def normalize_code(code: str | None) -> str:
    return _clean(code or "").replace(" ", "")
