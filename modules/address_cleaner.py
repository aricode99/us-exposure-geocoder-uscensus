"""Conservative address normalisation for SOV data.

Principles
----------
* Never overwrite originals: callers keep the original columns and receive
  separate ``Clean*`` fields plus a ``CleanFlags`` audit string.
* Never aggressively rewrite street text. Only whitespace, control characters,
  typographic artefacts, stray trailing punctuation and clearly broken ZIP codes
  are touched. No abbreviation expansion, no case changes, no unit removal.
* ZIP codes are always strings. Leading zeroes are restored only where the
  pattern is unambiguous (3-4 digits, or 8 digits for ZIP+4).
* State names become USPS codes only when the name is recognised exactly.
  An unrecognised state is blanked (and flagged) rather than guessed, because
  the Census service treats an invalid state as a hard No_Match.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Tuple

import pandas as pd

from utils import constants as C

# Characters that are visually whitespace but not ASCII space.
_SPACE_LIKE = {
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ", "\u200a": " ",
    "\u3000": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ", "\u2005": " ",
}
# Zero-width / BOM characters are removed outright.
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
_TYPOGRAPHIC = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
}
_NULL_TOKENS = {"nan", "none", "null", "<na>", "nat"}
_PO_BOX = re.compile(r"^\s*(?:P\.?\s*O\.?\s*BOX|POST\s+OFFICE\s+BOX|POB)\b", re.I)
_ZIP_FLOAT = re.compile(r"^(\d+)\.0+$")


def _as_text(value: Any) -> str:
    """Convert any cell value to text; NaN/None/'nan' become ''."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value)
    if text.strip().lower() in _NULL_TOKENS:
        return ""
    return text


def strip_control_chars(text: str) -> str:
    """Replace newlines/tabs by spaces; drop other control and zero-width characters."""
    out: List[str] = []
    for ch in text:
        if ch in _SPACE_LIKE:
            out.append(_SPACE_LIKE[ch])
        elif ch in _ZERO_WIDTH:
            continue
        elif ch in "\r\n\t\v\f":
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(_TYPOGRAPHIC.get(ch, ch))
    return "".join(out)


def _normalise_text(value: Any) -> str:
    text = strip_control_chars(_as_text(value))
    text = re.sub(r"\s+", " ", text).strip()
    # stray trailing separators left by concatenated exports: "123 Main St,"
    text = re.sub(r"[\s,;]+$", "", text)
    return text


_ALPHA_PART = re.compile(r"^[A-Za-z.' -]{2,40}$")
_STATE_ZIP = re.compile(r"^[A-Za-z]{2}\s+\d{5}(?:-\d{4})?$")


def strip_embedded_locality(street: str, city: str, state: str, zip_code: str, county: str = "") -> str:
    """Remove a trailing ", City, ST, County, ZIP ..." that was pasted into the street column.

    The street is trimmed only when one comma-separated part equals the City column (the anchor) and every
    part after it is a known locality value (city, state, ZIP, county) or a single county-like word group.
    Anything else, such as ", Apt 4B", leaves the street untouched.
    """
    parts = [p.strip() for p in street.split(",")]
    if len(parts) < 3 or not city:
        return street
    cities = {re.sub(r"\s+", " ", city.lower())}
    states = {state.lower()} | {n for n, a in C.STATE_NAME_TO_ABBR.items() if a == state}
    counties = {re.sub(r"\s+", " ", county.lower()), re.sub(r"\s+", " ", county.lower()) + " county"} if county else set()
    zips = {zip_code} if zip_code else set()

    def known(part: str) -> bool:
        low = re.sub(r"\s+", " ", part.lower())
        if low in cities or low in states or low in counties or part in zips or part[:5] in zips:
            return True
        if _STATE_ZIP.match(part) and part[:2].lower() in states:
            return True
        return False

    for k in range(1, len(parts)):
        if re.sub(r"\s+", " ", parts[k].lower()) not in cities:
            continue
        rest = parts[k:]
        if all(known(p) or (_ALPHA_PART.match(p) and not any(ch.isdigit() for ch in p)) for p in rest) \
                and any(known(p) for p in rest[1:]):
            return ", ".join(parts[:k])
    return street


def clean_street(value: Any) -> Tuple[str, List[str]]:
    original = _as_text(value)
    cleaned = _normalise_text(value)
    flags: List[str] = []
    if cleaned != original.strip():
        flags.append("STREET_NORMALIZED")
    if cleaned and _PO_BOX.match(cleaned):
        flags.append("PO_BOX")
    return cleaned, flags


def clean_city(value: Any) -> Tuple[str, List[str]]:
    original = _as_text(value)
    cleaned = _normalise_text(value)
    return cleaned, (["CITY_NORMALIZED"] if cleaned != original.strip() else [])


def clean_county(value: Any) -> Tuple[str, List[str]]:
    original = _as_text(value)
    cleaned = _normalise_text(value)
    return cleaned, (["COUNTY_NORMALIZED"] if cleaned != original.strip() else [])


def clean_state(value: Any) -> Tuple[str, List[str]]:
    """Return (USPS code or '', flags). Unrecognised states are blanked and flagged."""
    text = _normalise_text(value)
    if not text:
        return "", []
    key = re.sub(r"\s+", " ", text.replace(".", "")).strip().lower()
    code = key.upper()
    if len(code) == 2 and code in C.VALID_STATE_CODES:
        return code, (["STATE_NORMALIZED"] if text != code else [])
    # names, with and without punctuation ("D.C." / "Washington DC")
    name = C.STATE_NAME_TO_ABBR.get(key) or C.STATE_NAME_TO_ABBR.get(text.lower())
    if name:
        return name, ["STATE_ABBREVIATED"]
    return "", ["INVALID_STATE"]


def clean_zip(value: Any) -> Tuple[str, List[str]]:
    """Return (ZIP5 string or '', flags).

    Handles Excel artefacts ("2134", "2134.0", 2134), ZIP+4 and unhyphenated ZIP+4.
    Anything else that is not a plausible ZIP is blanked and flagged.
    """
    text = _normalise_text(value).replace(" ", "")
    if not text:
        return "", []
    m = _ZIP_FLOAT.match(text)          # "2134.0" from a float column
    if m:
        text = m.group(1)

    flags: List[str] = []
    if re.fullmatch(r"\d{5}", text):
        return text, flags
    if re.fullmatch(r"\d{5}-\d{4}", text):
        return text[:5], ["ZIP4_REMOVED"]
    if re.fullmatch(r"\d{3,4}", text):                       # lost leading zero(es)
        return text.zfill(5), ["ZIP_PADDED"]
    if re.fullmatch(r"\d{9}", text):                          # ZIP+4 without hyphen
        return text[:5], ["ZIP4_REMOVED"]
    if re.fullmatch(r"\d{8}", text):                          # ZIP+4, lost leading zero
        return text.zfill(9)[:5], ["ZIP_PADDED", "ZIP4_REMOVED"]
    if re.fullmatch(r"\d{4}-\d{4}", text):
        return text[:4].zfill(5), ["ZIP_PADDED", "ZIP4_REMOVED"]
    return "", ["INVALID_ZIP"]


def clean_address_row(street: Any, city: Any, state: Any, zip_code: Any,
                      county: Any = "") -> dict:
    """Clean one record. Pure function used by ``clean_dataframe`` and unit tests."""
    s, fs = clean_street(street)
    c, fc = clean_city(city)
    st, fst = clean_state(state)
    z, fz = clean_zip(zip_code)
    co, fco = clean_county(county)
    trimmed = strip_embedded_locality(s, c, st, z, co)
    if trimmed != s:
        s = trimmed
        fs = fs + ["STREET_LOCALITY_REMOVED"]
    return {
        "CleanStreet": s, "CleanCity": c, "CleanState": st, "CleanZIP": z, "CleanCounty": co,
        "CleanFlags": ";".join(fs + fc + fst + fz + fco),
    }


def clean_dataframe(df: pd.DataFrame, street_col: str, city_col: str = None,
                    state_col: str = None, zip_col: str = None,
                    county_col: str = None) -> pd.DataFrame:
    """Return a NEW frame (same index) with Original* and Clean* fields.

    ``df`` itself is never modified. Unmapped optional columns yield blank values.
    """
    def col(name):
        return df[name] if name and name in df.columns else pd.Series([""] * len(df), index=df.index)

    street, city, state = col(street_col), col(city_col), col(state_col)
    zips, county = col(zip_col), col(county_col)

    originals = pd.DataFrame({
        "OriginalStreet": [_as_text(v) for v in street],
        "OriginalCity": [_as_text(v) for v in city],
        "OriginalState": [_as_text(v) for v in state],
        "OriginalZIP": [_as_text(v) for v in zips],
    }, index=df.index)

    cleaned = pd.DataFrame(
        [clean_address_row(a, b, c, d, e)
         for a, b, c, d, e in zip(street, city, state, zips, county)],
        index=df.index,
    )
    return pd.concat([originals, cleaned], axis=1)
