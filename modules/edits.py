"""Analyst corrections after the first geocoding run (UI-free).

Two actions, both fully audited (``AnalystEdit*`` columns on the row plus ``PipelineResult.edit_log``):

* ``regeocode_rows``  - the analyst corrects Street / City / State / ZIP. Only those rows are sent through the
  normal pipeline again (clean -> route -> Census -> fallback -> QA -> match level -> confidence).
* ``set_coordinates`` - the analyst enters a latitude / longitude with a mandatory reason. The row is labelled
  MANUAL_OVERRIDE and is never counted as a Census result.

The original SOV values (``Original*`` columns and the uploaded columns) are never changed.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from modules.census_geocoder import CensusGeocoder
from modules.geography import ReferenceData, coordinate_plausible_for_state, load_reference
from modules.pipeline import GEO_COLUMNS, PipelineOptions, PipelineResult, ProgressFn, run_geocoding
from utils import constants as C

ADDRESS_FIELDS = ["Street", "City", "State", "ZIP"]
EDIT_LOG_COLUMNS = ["Timestamp", "GeocodeRowID", "LocationID", "Action", "Before", "After", "Note", "ResultingQAStatus"]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blank(v: Any) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _location_id(result: PipelineResult, pos: int) -> str:
    col = result.mapping.get("location_id")
    return str(result.original.iloc[pos][col]) if col and col in result.original.columns else ""


def _positions(result: PipelineResult, ids: List[str]) -> Dict[str, int]:
    lookup = {rid: i for i, rid in enumerate(result.geo["GeocodeRowID"].astype(str))}
    return {rid: lookup[rid] for rid in ids if rid in lookup}


def _addr_text(street: str, city: str, state: str, zip_code: str) -> str:
    return ", ".join(p for p in (street, city, state, zip_code) if p)


def regeocode_rows(
    result: PipelineResult,
    edits: pd.DataFrame,
    options: Optional[PipelineOptions] = None,
    progress: Optional[ProgressFn] = None,
    geocoder: Optional[CensusGeocoder] = None,
    ref: Optional[ReferenceData] = None,
) -> PipelineResult:
    """Return a NEW result with the edited rows re-geocoded.

    ``edits`` columns: GeocodeRowID, Street, City, State, ZIP, Note (optional).
    """
    ref = ref or load_reference()
    options = options or result.options
    ids = [str(i) for i in edits["GeocodeRowID"]]
    pos = _positions(result, ids)
    if not pos:
        return result
    edits = edits[edits["GeocodeRowID"].astype(str).isin(pos)].reset_index(drop=True)
    ids = [str(i) for i in edits["GeocodeRowID"]]

    county_col = result.mapping.get("county")
    counties = [(_blank(result.original.iloc[pos[i]][county_col])
                 if county_col and county_col in result.original.columns else "") for i in ids]
    subset = pd.DataFrame({"Street": edits["Street"].astype(str), "City": edits["City"].astype(str),
                           "State": edits["State"].astype(str), "ZIP": edits["ZIP"].astype(str),
                           "County": counties})
    sub_mapping = {"location_id": None, "street": "Street", "city": "City", "state": "State",
                   "zip": "ZIP", "county": "County"}
    rerun = run_geocoding(subset, sub_mapping, options, progress, geocoder, ref)

    geo = result.geo.copy()
    stamp = _now()
    log = list(result.edit_log)
    new_geo = rerun.geo.reset_index(drop=True)
    for k, rid in enumerate(ids):
        i = pos[rid]
        old = result.geo.iloc[i]
        count = int(old.get("AnalystEditCount") or 0) + 1
        note = _blank(edits.iloc[k].get("Note")) if "Note" in edits.columns else ""
        for col in GEO_COLUMNS:
            if col in ("GeocodeRowID", "OriginalStreet", "OriginalCity", "OriginalState", "OriginalZIP"):
                continue
            geo.iat[i, geo.columns.get_loc(col)] = new_geo.iloc[k][col]
        for col, val in (("AnalystEdit", C.EDIT_ADDRESS), ("AnalystEditNote", note),
                         ("AnalystEditTimestamp", stamp), ("AnalystEditCount", count)):
            geo.iat[i, geo.columns.get_loc(col)] = val
        log.append({
            "Timestamp": stamp, "GeocodeRowID": rid, "LocationID": _location_id(result, i),
            "Action": C.EDIT_ADDRESS,
            "Before": _addr_text(_blank(old["CleanStreet"]), _blank(old["CleanCity"]), _blank(old["CleanState"]),
                                 _blank(old["CleanZIP"])),
            "After": _addr_text(_blank(new_geo.iloc[k]["CleanStreet"]), _blank(new_geo.iloc[k]["CleanCity"]),
                                _blank(new_geo.iloc[k]["CleanState"]), _blank(new_geo.iloc[k]["CleanZIP"])),
            "Note": note, "ResultingQAStatus": new_geo.iloc[k]["QAStatus"]})
    warnings = list(result.warnings) + [f"Re-geocode: {w}" for w in rerun.warnings]
    return dataclasses.replace(result, geo=geo, edit_log=log, warnings=warnings,
                               chunk_log=list(result.chunk_log) + [dict(c, rerun=True) for c in rerun.chunk_log])


def validate_coordinates(lat: Any, lon: Any, state: str, ref: ReferenceData) -> Tuple[Optional[Tuple[float, float]], str]:
    """(coordinates, error). Rejects non-numbers and points far outside the expected area."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, "Latitude and longitude must be numbers"
    if np.isnan(la) or np.isnan(lo):
        return None, "Latitude and longitude must be numbers"
    if not coordinate_plausible_for_state(la, lo, (state or "").upper(), ref):
        return None, "Coordinate is outside the expected area for this location"
    return (la, lo), ""


def set_coordinates(
    result: PipelineResult,
    overrides: pd.DataFrame,
    ref: Optional[ReferenceData] = None,
) -> Tuple[PipelineResult, Dict[str, str]]:
    """Return (NEW result, {GeocodeRowID: error}) with analyst-entered coordinates applied.

    ``overrides`` columns: GeocodeRowID, Latitude, Longitude, Note (required, non-empty).
    Rows that fail validation are left unchanged and reported in the error dict.
    """
    ref = ref or load_reference()
    geo = result.geo.copy()
    errors: Dict[str, str] = {}
    log = list(result.edit_log)
    stamp = _now()
    pos = _positions(result, [str(i) for i in overrides["GeocodeRowID"]])
    for _, row in overrides.iterrows():
        rid = str(row["GeocodeRowID"])
        if rid not in pos:
            continue
        i = pos[rid]
        note = _blank(row.get("Note"))
        if not note:
            errors[rid] = "A reason is required for manual coordinates"
            continue
        old = result.geo.iloc[i]
        coords, err = validate_coordinates(row["Latitude"], row["Longitude"],
                                           _blank(old["CleanState"]) or _blank(old["OriginalState"]), ref)
        if coords is None:
            errors[rid] = err
            continue
        la, lo = coords
        updates = {
            "Latitude": la, "Longitude": lo, "GeocodeRoute": C.ROUTE_MANUAL, "GeocodeResolution": C.RES_MANUAL,
            "GeoResolutionBasis": "Coordinates entered by an analyst", "CensusGeographyLevel": None,
            "GeocodeSource": "Analyst entry", "CoordinateMethod": "Manual entry",
            "QAStatus": C.QA_MANUAL, "QAReason": f"Coordinates set by analyst: {note}",
            "GeoMatchLevelCode": C.ML_MANUAL[0], "EnhancedGeoMatchLevelCode": C.ML_MANUAL[1],
            "GeoMatchLevel": C.ML_MANUAL[2], "GeocodeConfidence": C.CONF_MANUAL,
            "ConfidenceBasis": "Coordinates entered by an analyst; not verified by this tool",
            "TigerLineID": None, "TigerLineSide": None, "StateFIPS": None, "CountyFIPS": None,
            "CensusTract": None, "CensusBlock": None,
            "GeocodeProvider": "Analyst", "GeocodeBenchmark": None, "GeocodeBenchmarkID": None,
            "GeocodeVintage": None, "GeocodeVintageID": None, "GeocodeTimestamp": stamp,
            "GeocodeMethod": "Manual entry",
            "AnalystEdit": C.EDIT_COORDS, "AnalystEditNote": note, "AnalystEditTimestamp": stamp,
            "AnalystEditCount": int(old.get("AnalystEditCount") or 0) + 1,
        }
        for col, val in updates.items():
            geo.iat[i, geo.columns.get_loc(col)] = val
        before = "none" if pd.isna(old["Latitude"]) else f"{old['Latitude']:.6f}, {old['Longitude']:.6f} ({old['GeoMatchLevel']})"
        log.append({"Timestamp": stamp, "GeocodeRowID": rid, "LocationID": _location_id(result, i),
                    "Action": C.EDIT_COORDS, "Before": before, "After": f"{la:.6f}, {lo:.6f}",
                    "Note": note, "ResultingQAStatus": C.QA_MANUAL})
    return dataclasses.replace(result, geo=geo, edit_log=log), errors


def edit_log_frame(result: PipelineResult) -> pd.DataFrame:
    return pd.DataFrame(result.edit_log, columns=EDIT_LOG_COLUMNS)
