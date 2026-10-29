"""Application-level geocoding QA.

IMPORTANT: the Census Geocoder does not publish a numeric confidence score.
``QAStatus`` / ``QAReason`` are produced HERE, by comparing the cleaned input
address with the address Census matched. They are not a Census measure.

Baseline (Census Match Indicator / Match Type)
    Match + Exact      -> HIGH
    Match + Non_Exact  -> MEDIUM
    Tie                -> REVIEW   (no coordinate is chosen)
    No_Match           -> FAILED

Adjustments for matches (each recorded in QAReason)
    State changed                         -> REVIEW (forced)
    ZIP changed                           -> down one level
    City changed (similarity < 0.80)      -> down one level
    House number changed                  -> down one level
    Street name similarity < 0.70         -> down one level
    Coordinate implausible for the state  -> REVIEW (forced)
    Match without usable coordinates      -> FAILED

Levels never fall below REVIEW through address comparison alone; FAILED is
reserved for no match / no coordinate / no result.

Non-Census outcomes
    FALLBACK      area centroid used (ZIP / city / county); never HIGH or MEDIUM
    OUT_OF_SCOPE  Puerto Rico / territories (v1 scope decision); not submitted
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from modules.geography import ReferenceData, coordinate_plausible_for_state, load_reference
from utils import constants as C

STREET_SIM_REVIEW = 0.70
STREET_SIM_NOTE = 0.85
CITY_SIM_MIN = 0.80

_LEVELS = [C.QA_HIGH, C.QA_MEDIUM, C.QA_REVIEW]

_TOKEN_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "LANE": "LN", "COURT": "CT", "PLACE": "PL", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "CIRCLE": "CIR", "TERRACE": "TER", "TRAIL": "TRL", "SQUARE": "SQ", "EXPRESSWAY": "EXPY",
    "FREEWAY": "FWY", "ALLEY": "ALY", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
    "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH",
    "ELEVENTH": "11TH", "TWELFTH": "12TH", "SAINT": "ST", "MOUNT": "MT", "FORT": "FT",
}
_UNIT = re.compile(
    r"\b(?:APT|APARTMENT|UNIT|STE|SUITE|FL|FLOOR|BLDG|BUILDING|RM|ROOM|LOT|TRLR|SPC)\b.*$|#.*$")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text or "")).strip()


def normalise_street(text: str) -> str:
    """Upper-case, drop unit designators, standardise common suffix/direction words."""
    s = _UNIT.sub("", (text or "").upper())
    return " ".join(_TOKEN_MAP.get(t, t) for t in _squash(s).split())


def normalise_city(text: str) -> str:
    return " ".join(_TOKEN_MAP.get(t, t) for t in _squash((text or "").upper()).split())


def split_matched_address(matched: Optional[str]) -> Dict[str, str]:
    """'4600 SILVER HILL RD, WASHINGTON, DC, 20233' -> street/city/state/zip."""
    parts = [p.strip() for p in (matched or "").split(",")]
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if len(parts) >= 4:
        out["zip"], out["state"], out["city"] = parts[-1], parts[-2], parts[-3]
        out["street"] = ", ".join(parts[:-3])
    elif parts:
        out["street"] = parts[0]
    return out


def _house_number(street: str) -> Optional[str]:
    m = re.match(r"\s*(\d+)", street or "")
    return m.group(1) if m else None


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 1.0 if a == b else 0.0
    return SequenceMatcher(None, a, b).ratio()


def _downgrade(level: str, steps: int) -> str:
    return _LEVELS[min(_LEVELS.index(level) + steps, len(_LEVELS) - 1)]


def _geography_level(r: Dict[str, Any]) -> Optional[str]:
    for col, label in (("CensusBlock", "BLOCK"), ("CensusTract", "TRACT"),
                       ("CountyFIPS", "COUNTY"), ("StateFIPS", "STATE")):
        v = r.get(col)
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
            return label
    return None


def _blank(v: Any) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _result(status, reason, resolution, basis, level, source, method) -> Dict[str, Any]:
    return {"QAStatus": status, "QAReason": reason, "GeocodeResolution": resolution,
            "GeoResolutionBasis": basis, "CensusGeographyLevel": level,
            "GeocodeSource": source, "CoordinateMethod": method}


def evaluate_match(r: Dict[str, Any], ref: ReferenceData) -> Tuple[str, List[str]]:
    """QA for a Census ``Match`` row. Returns (status, reasons)."""
    lat, lon = r.get("Latitude"), r.get("Longitude")
    if pd.isna(lat) or pd.isna(lon):
        return C.QA_FAILED, ["Census reported a match but returned no usable coordinates"]

    mtype = _blank(r.get("CensusMatchType"))
    if mtype == "Exact":
        level, reasons = C.QA_HIGH, ["Census exact match"]
    elif mtype == "Non_Exact":
        level, reasons = C.QA_MEDIUM, ["Census non-exact match"]
    else:
        level, reasons = C.QA_MEDIUM, ["Census match type not reported"]

    matched = split_matched_address(_blank(r.get("CensusMatchedAddress")))
    in_state, in_zip = _blank(r.get("CleanState")).upper(), _blank(r.get("CleanZIP"))
    in_city, in_street = _blank(r.get("CleanCity")), _blank(r.get("CleanStreet"))
    steps = 0
    force_review = False

    if in_state and matched["state"] and in_state != matched["state"].upper():
        force_review = True
        reasons.append(f"State changed: {in_state} -> {matched['state'].upper()}")

    if in_zip and matched["zip"] and in_zip[:5] != matched["zip"][:5]:
        steps += 1
        reasons.append(f"ZIP changed: {in_zip[:5]} -> {matched['zip'][:5]}")

    if in_city and matched["city"]:
        sim = _similarity(normalise_city(in_city), normalise_city(matched["city"]))
        if sim < CITY_SIM_MIN:
            steps += 1
            reasons.append(f"City changed: {in_city.upper()} -> {matched['city'].upper()}")

    in_num, m_num = _house_number(in_street), _house_number(matched["street"])
    if in_num and m_num and int(in_num) != int(m_num):
        steps += 1
        reasons.append(f"House number changed: {in_num} -> {m_num}")

    if in_street and matched["street"]:
        a = re.sub(r"^\s*\d+[A-Za-z]?\s*", "", normalise_street(in_street))
        b = re.sub(r"^\s*\d+[A-Za-z]?\s*", "", normalise_street(matched["street"]))
        if a and b:
            sim = _similarity(a, b)
            if sim < STREET_SIM_REVIEW:
                steps += 1
                reasons.append(f"Street name substantially changed (similarity {sim:.2f})")
            elif sim < STREET_SIM_NOTE:
                reasons.append(f"Street name differs slightly (similarity {sim:.2f})")

    state_for_box = (matched["state"] or in_state).upper()
    if not coordinate_plausible_for_state(float(lat), float(lon), state_for_box, ref):
        force_review = True
        reasons.append(f"Coordinate is outside the expected area for {state_for_box or 'the U.S.'}")

    if steps:
        level = _downgrade(level, steps)
    if force_review:
        level = C.QA_REVIEW
    if len(reasons) == 1:
        reasons.append("address components agree")
    return level, reasons


def qa_row(r: Dict[str, Any], ref: ReferenceData) -> Dict[str, Any]:
    """Final QA / resolution fields for one record."""
    route = r.get("GeocodeRoute")
    note = _blank(r.get("RouteNote"))

    if route == C.ROUTE_OUT_OF_SCOPE:
        return _result(C.QA_OUT_OF_SCOPE, note or "Outside v1 scope (Puerto Rico / U.S. territories); not submitted",
                       C.RES_NONE, "Not geocoded: outside scope", None, None, None)

    if route == C.ROUTE_FALLBACK:
        basis = _blank(r.get("FallbackBasis"))
        census = _blank(r.get("CensusMatchIndicator"))
        reason = basis + (f"; Census outcome was {census}" if census and census != "Match" else "")
        if note:
            reason += f"; {note}"
        return _result(C.QA_FALLBACK, reason + ". Area centroid, not a site location",
                       _blank(r.get("FallbackResolution")) or C.RES_NONE, basis,
                       _blank(r.get("FallbackGeographyLevel")) or None,
                       C.FALLBACK_SOURCE, C.FALLBACK_COORDINATE_METHOD)

    if route == C.ROUTE_NOT_SUBMITTED:
        return _result(C.QA_FAILED, note or "Insufficient address information to geocode",
                       C.RES_NONE, "Not geocoded", None, None, None)

    # --- Census-submitted records ---
    indicator = _blank(r.get("CensusMatchIndicator"))
    flags = _blank(r.get("CleanFlags"))
    hints = []
    if "INVALID_STATE" in flags:
        hints.append("input state was not recognised and was ignored")
    if "INVALID_ZIP" in flags:
        hints.append("input ZIP was invalid and was ignored")

    if not indicator:
        return _result(C.QA_FAILED, "No result was returned for this record (batch failed or record missing)",
                       C.RES_NONE, "No Census result", None, None, None)
    if indicator == "No_Match":
        reason = "Census could not match this address (No_Match)"
        if hints:
            reason += "; " + "; ".join(hints)
        return _result(C.QA_FAILED, reason, C.RES_NONE, "Census No_Match", None, None, None)
    if indicator == "Tie":
        return _result(C.QA_REVIEW,
                       "Census returned a tie (multiple equally likely candidates); no coordinate provided",
                       C.RES_NONE, "Census Tie: no single location selected", None, None, None)

    status, reasons = evaluate_match(r, ref)
    reasons = reasons + [h[0].upper() + h[1:] for h in hints]
    if status == C.QA_FAILED:
        return _result(status, "; ".join(reasons), C.RES_NONE, "Match without coordinates",
                       None, None, None)
    tiger = _blank(r.get("TigerLineID"))
    basis = "Census " + (_blank(r.get("CensusMatchType")) or "match") + \
        ", interpolated along TIGER address range" + (f" (TLID {tiger})" if tiger else "")
    return _result(status, "; ".join(reasons), C.RES_ADDRESS, basis, _geography_level(r),
                   C.GEOCODE_SOURCE, C.COORDINATE_METHOD)


QA_COLUMNS = ["QAStatus", "QAReason", "GeocodeResolution", "GeoResolutionBasis",
              "CensusGeographyLevel", "GeocodeSource", "CoordinateMethod"]


def apply_qa(df: pd.DataFrame, ref: Optional[ReferenceData] = None) -> pd.DataFrame:
    """Return a frame (same index) with the QA/resolution columns for every row."""
    ref = ref or load_reference()
    records = df.to_dict("records")
    out = [qa_row(rec, ref) for rec in records]
    return pd.DataFrame(out, index=df.index, columns=QA_COLUMNS)
