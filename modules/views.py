"""UI-free helpers that prepare data for the map and QA tables."""
from __future__ import annotations

import html
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from modules.pipeline import PipelineResult
from utils import constants as C

STATE_OUTLINE_PATH = Path(__file__).resolve().parents[1] / "data" / "reference" / "us_states.geojson"


@lru_cache(maxsize=1)
def load_state_outline() -> Optional[dict]:
    """Bundled U.S. state boundaries (GeoJSON) used as an offline map background.

    Returns None if the file is missing or unreadable so the map still draws the points.
    """
    try:
        data = json.loads(STATE_OUTLINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("features") else None


QA_COLORS: Dict[str, List[int]] = {
    C.QA_HIGH: [34, 139, 34, 200],
    C.QA_MEDIUM: [240, 180, 0, 215],
    C.QA_REVIEW: [230, 90, 0, 225],
    C.QA_FALLBACK: [130, 80, 200, 205],
}
LEGEND = [("HIGH", "#228b22"), ("MEDIUM", "#f0b400"), ("REVIEW", "#e65a00"),
          ("FALLBACK (area centroid)", "#8250c8")]


def match_status(geo: pd.DataFrame) -> pd.Series:
    """One label per row combining routing and the Census indicator."""
    route = geo["GeocodeRoute"]
    ind = geo["CensusMatchIndicator"]
    mtype = geo["CensusMatchType"]
    out = pd.Series("No result", index=geo.index, dtype=object)
    census = route == C.ROUTE_CENSUS
    out[census & (ind == "Match")] = mtype[census & (ind == "Match")].fillna("Match")
    out[census & (ind == "Tie")] = "Tie"
    out[census & (ind == "No_Match")] = "No_Match"
    out[route == C.ROUTE_FALLBACK] = "Fallback"
    out[route == C.ROUTE_OUT_OF_SCOPE] = "Out of scope"
    out[route == C.ROUTE_NOT_SUBMITTED] = "Not submitted"
    return out


def _original_address(res: PipelineResult) -> List[str]:
    g = res.geo
    return [", ".join(p for p in parts if p)
            for parts in zip(g["OriginalStreet"], g["OriginalCity"], g["OriginalState"], g["OriginalZIP"])]


def build_view_frame(res: PipelineResult) -> pd.DataFrame:
    """Geocoding fields plus LocationID, OriginalAddress and MatchStatus (index preserved)."""
    g = res.geo
    view = g.drop(columns=["OriginalStreet", "OriginalCity", "OriginalState", "OriginalZIP"]).copy()
    id_col = res.mapping.get("location_id")
    view.insert(0, "LocationID", res.original[id_col].astype(str).values if id_col else g["GeocodeRowID"].values)
    view.insert(1, "OriginalAddress", _original_address(res))
    view["MatchStatus"] = match_status(g)
    return view


def map_points(
    view: pd.DataFrame,
    qa_statuses: Optional[Iterable[str]] = None,
    states: Optional[Iterable[str]] = None,
    match_statuses: Optional[Iterable[str]] = None,
    resolutions: Optional[Iterable[str]] = None,
    max_points: int = 50_000,
    seed: int = 7,
) -> Tuple[pd.DataFrame, int, bool]:
    """Select plottable rows. Returns (frame for pydeck, available_count, was_sampled).

    Rows without coordinates are never plotted. Text shown in tooltips is HTML-escaped
    because deck.gl renders tooltips as HTML and SOV text is untrusted.
    """
    df = view[view["Latitude"].notna() & view["Longitude"].notna()]
    if qa_statuses is not None:
        df = df[df["QAStatus"].isin(list(qa_statuses))]
    if states is not None:
        df = df[df["CleanState"].isin(list(states))]
    if match_statuses is not None:
        df = df[df["MatchStatus"].isin(list(match_statuses))]
    if resolutions is not None:
        df = df[df["GeocodeResolution"].isin(list(resolutions))]

    available = len(df)
    sampled = available > max_points
    if sampled:
        df = df.sample(n=max_points, random_state=seed)

    esc = lambda s: [html.escape(str(v)) if v is not None and not pd.isna(v) else "" for v in s]  # noqa: E731
    plot = pd.DataFrame({
        "LocationID": esc(df["LocationID"]),
        "OriginalAddress": esc(df["OriginalAddress"]),
        "CensusMatchedAddress": esc(df["CensusMatchedAddress"]),
        "Latitude": df["Latitude"].astype(float).round(6).values,
        "Longitude": df["Longitude"].astype(float).round(6).values,
        "MatchType": esc(df["MatchStatus"]),
        "QAStatus": esc(df["QAStatus"]),
        "Resolution": esc(df["GeocodeResolution"]),
        "color": [QA_COLORS.get(s, [120, 120, 120, 200]) for s in df["QAStatus"]],
        "radius": np.where(df["QAStatus"] == C.QA_FALLBACK, 7, 4),
    })
    return plot, available, sampled


TOOLTIP_HTML = (
    "<b>{LocationID}</b><br/>"
    "Original: {OriginalAddress}<br/>"
    "Census matched: {CensusMatchedAddress}<br/>"
    "Lat / Lon: {Latitude}, {Longitude}<br/>"
    "Match type: {MatchType}<br/>"
    "QA status: {QAStatus}<br/>"
    "Resolution: {Resolution}"
)


def view_state_for(plot: pd.DataFrame) -> Dict[str, float]:
    """Centre and zoom that frame the points (continental U.S. default when empty)."""
    if plot.empty:
        return {"latitude": 39.5, "longitude": -98.35, "zoom": 3.2}
    lat, lon = plot["Latitude"].to_numpy(), plot["Longitude"].to_numpy()
    lat_lo, lat_hi = np.percentile(lat, [1, 99]) if len(lat) > 50 else (lat.min(), lat.max())
    lon_lo, lon_hi = np.percentile(lon, [1, 99]) if len(lon) > 50 else (lon.min(), lon.max())
    span = max(lat_hi - lat_lo, (lon_hi - lon_lo) * math.cos(math.radians((lat_lo + lat_hi) / 2)), 0.005)
    zoom = float(np.clip(math.log2(360.0 / span) - 0.6, 2.0, 14.0))
    return {"latitude": float((lat_lo + lat_hi) / 2), "longitude": float((lon_lo + lon_hi) / 2), "zoom": zoom}


def qa_review_frame(view: pd.DataFrame, qa_statuses=None, states=None, match_statuses=None) -> pd.DataFrame:
    """Rows needing attention (anything not a clean HIGH match), optionally filtered."""
    df = view[view["QAStatus"] != C.QA_HIGH]
    if qa_statuses is not None:
        df = df[df["QAStatus"].isin(list(qa_statuses))]
    if states is not None:
        df = df[df["CleanState"].isin(list(states))]
    if match_statuses is not None:
        df = df[df["MatchStatus"].isin(list(match_statuses))]
    return df
