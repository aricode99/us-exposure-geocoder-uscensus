"""Geocode confidence bands.

IMPORTANT: the Census Geocoder publishes no confidence value. ``GeocodeConfidence`` is produced HERE
from (1) the match level and (2) independent consistency checks. It is a band, not a probability, and it
has not been calibrated against surveyed locations.

Starting point by match level (enhanced code):
    SEGI + Exact 95 · SEGI + relaxed 75 · STRI 55 · POST 40 · CITY 25 · COUNTY 12

Deductions (Census address matches only):
    state changed -60 · coordinate outside the state's plausible area -60
    ZIP changed -20 · city changed -20 · house number changed -20 · street name changed -20
    point more than 75 km from the input ZIP's centroid -20
    Census county differs from the county supplied in the SOV -20
    same coordinate also assigned to a different street -10

Bands:  High >= 80 · Moderate 60-79 · Low 35-59 · Very low < 35
Special: "Analyst-set" (coordinates entered by a person) and "Not geocoded" (no coordinate).
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from modules.geography import ReferenceData, coordinate_plausible_for_state, load_reference
from modules.qa_engine import (CITY_SIM_MIN, STREET_SIM_REVIEW, _house_number, _similarity, normalise_city,
                               normalise_street, split_matched_address)
from modules.geography import normalise_county_name
from utils import constants as C

CONFIDENCE_COLUMNS = ["GeocodeConfidence", "ConfidenceBasis"]
ZIP_DISTANCE_KM = 75.0

_BASE = {"SEGI-Exact": 95, "SEGI": 75, "STRI": 55, "POST": 40, "CITY": 25, "COUNTY": 12}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def band_for(score: int) -> str:
    if score >= 80:
        return C.CONF_HIGH
    if score >= 60:
        return C.CONF_MODERATE
    if score >= 35:
        return C.CONF_LOW
    return C.CONF_VERY_LOW


def _blank(v: Any) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _street_key(street: str) -> str:
    return re.sub(r"^\s*\d+[A-Za-z]?\s*", "", normalise_street(street))


def shared_coordinate_streets(df: pd.DataFrame) -> Dict[Tuple[float, float], int]:
    """Count of distinct street names per rounded coordinate (Census address matches only)."""
    sub = df[df["GeocodeResolution"] == C.RES_ADDRESS]
    seen: Dict[Tuple[float, float], set] = {}
    for lat, lon, street in zip(sub["Latitude"], sub["Longitude"], sub["CleanStreet"]):
        seen.setdefault((round(float(lat), 5), round(float(lon), 5)), set()).add(_street_key(street))
    return {k: len(v) for k, v in seen.items()}


def confidence_row(r: Dict[str, Any], enhanced: str, ref: ReferenceData,
                   shared: Optional[Dict[Tuple[float, float], int]] = None,
                   county_input: str = "") -> Tuple[str, str]:
    """(band, basis text) for one record."""
    res = r.get("GeocodeResolution")
    if res == C.RES_MANUAL:
        return C.CONF_MANUAL, "Coordinates entered by an analyst; not verified by this tool"
    if res in (None, C.RES_NONE) or pd.isna(r.get("Latitude")):
        return C.CONF_NONE, "No coordinate"

    if res != C.RES_ADDRESS:
        score = _BASE[enhanced]
        area = {"POST": "ZIP code area", "CITY": "city", "COUNTY": "county"}.get(enhanced, "area")
        return band_for(score), f"Area centroid ({area}); not a site location"

    is_exact = r.get("CensusMatchType") == "Exact"
    score = _BASE["SEGI-Exact"] if enhanced == "SEGI" and is_exact else _BASE.get(enhanced, 55)
    good: List[str] = ["Exact address match" if is_exact else "Relaxed address match"]
    bad: List[str] = []

    matched = split_matched_address(_blank(r.get("CensusMatchedAddress")))
    in_state, in_zip = _blank(r.get("CleanState")).upper(), _blank(r.get("CleanZIP"))
    in_city, in_street = _blank(r.get("CleanCity")), _blank(r.get("CleanStreet"))
    lat, lon = float(r["Latitude"]), float(r["Longitude"])

    if in_state and matched["state"]:
        if in_state != matched["state"].upper():
            score -= 60
            bad.append("state differs from input")
        else:
            good.append("state agrees")
    if in_zip and matched["zip"]:
        if in_zip[:5] != matched["zip"][:5]:
            score -= 20
            bad.append("ZIP differs from input")
        else:
            good.append("ZIP agrees")
    if in_city and matched["city"]:
        if _similarity(normalise_city(in_city), normalise_city(matched["city"])) < CITY_SIM_MIN:
            score -= 20
            bad.append("city differs from input")
        else:
            good.append("city agrees")
    a, b = _house_number(in_street), _house_number(matched["street"])
    if a and b and int(a) != int(b):
        score -= 20
        bad.append("house number differs from input")
    if in_street and matched["street"]:
        sa, sb = _street_key(in_street), _street_key(matched["street"])
        if sa and sb and _similarity(sa, sb) < STREET_SIM_REVIEW:
            score -= 20
            bad.append("street name differs from input")

    state_for_box = (matched["state"] or in_state).upper()
    if not coordinate_plausible_for_state(lat, lon, state_for_box, ref):
        score -= 60
        bad.append(f"point is outside the expected area for {state_for_box or 'the U.S.'}")

    hit = ref.zcta.get((matched["zip"] or in_zip)[:5]) if (matched["zip"] or in_zip) else None
    if hit:
        d = haversine_km(lat, lon, hit[0], hit[1])
        if d > ZIP_DISTANCE_KM:
            score -= 20
            bad.append(f"point is {d:.0f} km from the ZIP centroid")
        else:
            good.append(f"{d:.0f} km from ZIP centroid")

    sfips, cfips = _blank(r.get("StateFIPS")), _blank(r.get("CountyFIPS"))
    if county_input and sfips and cfips:
        census_county = ref.county_names.get(sfips + cfips)
        if census_county:
            if census_county != normalise_county_name(county_input):
                score -= 20
                bad.append("county differs from the SOV county")
            else:
                good.append("county agrees")

    if shared and shared.get((round(lat, 5), round(lon, 5)), 1) > 1:
        score -= 10
        bad.append("same point also assigned to a different street")

    score = max(score, 1)
    basis = "; ".join((bad + good) if bad else good)
    return band_for(score), basis[0].upper() + basis[1:]


def apply_confidence(df: pd.DataFrame, ref: Optional[ReferenceData] = None) -> pd.DataFrame:
    """``df`` needs the QA, match-level and Clean* columns (and CleanCounty when available)."""
    ref = ref or load_reference()
    shared = shared_coordinate_streets(df) if (df["GeocodeResolution"] == C.RES_ADDRESS).any() else {}
    counties = df["CleanCounty"] if "CleanCounty" in df.columns else pd.Series([""] * len(df), index=df.index)
    out = [confidence_row(rec, enh, ref, shared, _blank(cty))
           for rec, enh, cty in zip(df.to_dict("records"), df["EnhancedGeoMatchLevelCode"], counties)]
    return pd.DataFrame(out, index=df.index, columns=CONFIDENCE_COLUMNS)
