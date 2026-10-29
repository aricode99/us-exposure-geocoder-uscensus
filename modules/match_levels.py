"""Geocode match level for every record.

Three fields describe how closely a coordinate follows the supplied address:

``GeoMatchLevelCode``          ADDR, RLXA, POST, CITY, CNTY, MANL, NONE
``EnhancedGeoMatchLevelCode``  SEGI, STRI, POST, CITY, COUNTY, MANL, NONE
``GeoMatchLevel``              the label shown in the tool

======================================  ==========  ==========  ====================
Result                                   Code        Enhanced    Label
======================================  ==========  ==========  ====================
Census Exact                             ADDR        SEGI        Exact Address
Census Non_Exact, house number inside    RLXA        SEGI        Relaxed Address
the matched range
Census Non_Exact, house number differs   RLXA        STRI        Relaxed Address
ZIP centroid fallback                    POST        POST        Postal Code centroid
City centroid fallback                   CITY        CITY        City centroid
County centroid fallback                 CNTY        COUNTY      County centroid
Tie / No_Match / not submitted           NONE        NONE        None
Out of scope                             NONE        NONE        None
Analyst-set coordinates                  MANL        MANL        Manual (analyst-set)
======================================  ==========  ==========  ====================

Census reports only Exact / Non_Exact. The "house number differs" split is inferred by
comparing the input house number with the matched one, so it is a heuristic.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

import pandas as pd

from utils import constants as C

MATCH_LEVEL_COLUMNS = ["GeoMatchLevelCode", "EnhancedGeoMatchLevelCode", "GeoMatchLevel"]


def _house_number(text: Any) -> str:
    m = re.match(r"\s*(\d+)", "" if text is None or (isinstance(text, float) and pd.isna(text)) else str(text))
    return m.group(1) if m else ""


def match_level_for(r: Dict[str, Any]) -> Tuple[str, str, str]:
    """(code, enhanced code, label) for one record that already has its QA fields."""
    res = r.get("GeocodeResolution")
    if res == C.RES_MANUAL:
        return C.ML_MANUAL
    if res == C.RES_ZIP:
        return C.ML_POSTAL
    if res == C.RES_CITY:
        return C.ML_CITY
    if res == C.RES_COUNTY:
        return C.ML_COUNTY
    if res == C.RES_ADDRESS:
        if r.get("CensusMatchType") == "Exact":
            return C.ML_EXACT
        wanted = _house_number(r.get("CleanStreet"))
        found = _house_number((r.get("CensusMatchedAddress") or "").split(",")[0])
        if wanted and found and int(wanted) != int(found):
            return C.ML_RELAXED_STREET
        return C.ML_RELAXED_SEG
    return C.ML_NONE


def apply_match_levels(df: pd.DataFrame) -> pd.DataFrame:
    rows = [match_level_for(rec) for rec in df.to_dict("records")]
    return pd.DataFrame(rows, index=df.index, columns=MATCH_LEVEL_COLUMNS)
