"""UI-free orchestration of the whole workflow.

    original SOV -> clean -> route -> Census batch (chunked) -> fallback -> QA -> summary

``run_geocoding`` never raises for data or service problems: failures are
recorded per row (QAStatus FAILED with a reason) and per batch (chunk log).
The uploaded DataFrame is never modified.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from modules import address_cleaner
from modules.census_geocoder import BatchRow, CensusGeocoder, CensusGeocoderError, GeocodeContext
from modules.geography import ReferenceData, fallback_centroid, gazetteer_year, load_reference
from modules.confidence import apply_confidence
from modules.match_levels import apply_match_levels
from modules.qa_engine import apply_qa
from utils import constants as C

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, float, str], None]

GEO_COLUMNS = [
    "GeocodeRowID",
    "OriginalStreet", "OriginalCity", "OriginalState", "OriginalZIP",
    "CleanStreet", "CleanCity", "CleanState", "CleanZIP", "CleanFlags",
    "CensusMatchedAddress", "Latitude", "Longitude", "CensusMatchIndicator", "CensusMatchType",
    "TigerLineID", "TigerLineSide", "StateFIPS", "CountyFIPS", "CensusTract", "CensusBlock",
    "GeocodeRoute", "GeocodeResolution", "GeoResolutionBasis", "CensusGeographyLevel",
    "GeocodeSource", "CoordinateMethod", "QAStatus", "QAReason",
    "GeoMatchLevelCode", "EnhancedGeoMatchLevelCode", "GeoMatchLevel",
    "GeocodeConfidence", "ConfidenceBasis",
    "GeocodeProvider", "GeocodeBenchmark", "GeocodeBenchmarkID", "GeocodeVintage",
    "GeocodeVintageID", "GeocodeTimestamp", "GeocodeMethod", "GeocodeBatchNumber",
    "AnalystEdit", "AnalystEditNote", "AnalystEditTimestamp", "AnalystEditCount",
]


@dataclass
class PipelineOptions:
    chunk_size: int = C.DEFAULT_BATCH_SIZE
    include_geographies: bool = True
    dedupe_addresses: bool = False          # geocode identical cleaned addresses once
    fallback_for_failed: bool = False       # also use centroids for Census No_Match / Tie rows
    submit_insufficient: bool = False       # send street-only rows to Census anyway
    benchmark_name: str = C.DEFAULT_BENCHMARK_NAME
    vintage_name: str = C.DEFAULT_VINTAGE_NAME


@dataclass
class PipelineResult:
    original: pd.DataFrame
    geo: pd.DataFrame
    mapping: Dict[str, Optional[str]]
    options: PipelineOptions
    context: Optional[GeocodeContext]
    chunk_log: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    started_utc: str = ""
    finished_utc: str = ""
    unique_addresses_submitted: int = 0
    edit_log: List[Dict[str, Any]] = field(default_factory=list)

    # -- export -----------------------------------------------------------------
    def column_collisions(self) -> Dict[str, str]:
        """Appended columns whose names already exist in the SOV -> name used in exports."""
        existing = set(self.original.columns)
        return {c: f"{c}_Geocode" for c in GEO_COLUMNS if c in existing}

    def export_frame(self, mask: Optional[pd.Series] = None) -> pd.DataFrame:
        """Original SOV columns (untouched) followed by the appended geocoding columns."""
        geo = self.geo.rename(columns=self.column_collisions())
        out = pd.concat([self.original, geo], axis=1)
        return out if mask is None else out.loc[mask].reset_index(drop=True)

    # -- subsets (spec section 12) ------------------------------------------------
    def mask_geocoded(self) -> pd.Series:
        return self.geo["GeocodeResolution"] == C.RES_ADDRESS

    def mask_exceptions(self) -> pd.Series:
        """Everything that is not a clean HIGH match."""
        return self.geo["QAStatus"] != C.QA_HIGH

    def mask_failed(self) -> pd.Series:
        """No coordinate at all: failed, tie, or out of scope."""
        return self.geo["Latitude"].isna()

    def mask_fallback(self) -> pd.Series:
        return self.geo["GeocodeResolution"].isin([C.RES_ZIP, C.RES_CITY, C.RES_COUNTY])

    def mask_edited(self) -> pd.Series:
        """Rows an analyst changed (address re-geocoded or coordinates set)."""
        return self.geo["AnalystEdit"].notna()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
_PR_VI_ZIP3 = {"006", "007", "008", "009"}


def _route_row(c: Dict[str, Any], options: PipelineOptions, ref: ReferenceData) -> Dict[str, Any]:
    street, city, state = c["CleanStreet"], c["CleanCity"], c["CleanState"]
    zip_code, county, flags = c["CleanZIP"], c.get("CleanCounty", ""), c["CleanFlags"]
    base = {"GeocodeRoute": C.ROUTE_NOT_SUBMITTED, "RouteNote": "", "FallbackResolution": None,
            "FallbackBasis": None, "FallbackGeographyLevel": None,
            "FallbackLatitude": np.nan, "FallbackLongitude": np.nan}

    if state in C.OUT_OF_SCOPE_STATES or (not state and zip_code[:3] in _PR_VI_ZIP3 and zip_code):
        base.update(GeocodeRoute=C.ROUTE_OUT_OF_SCOPE,
                    RouteNote="Puerto Rico / U.S. territory addresses are outside v1 scope; not submitted")
        return base

    po_box = "PO_BOX" in flags
    has_street = bool(street) and not po_box
    sufficient = bool(zip_code) or (bool(city) and bool(state))
    if has_street and (sufficient or options.submit_insufficient):
        base["GeocodeRoute"] = C.ROUTE_CENSUS
        return base

    if po_box:
        why = "PO Box cannot be geocoded to a site"
    elif has_street:
        why = "Street present but Census needs a ZIP, or a city and state"
    else:
        why = "No street address"

    fb, notes = fallback_centroid(state, zip_code, city, county, ref)
    if fb:
        base.update(GeocodeRoute=C.ROUTE_FALLBACK, FallbackResolution=fb.resolution,
                    FallbackBasis=fb.basis, FallbackGeographyLevel=fb.geography_level,
                    FallbackLatitude=fb.latitude, FallbackLongitude=fb.longitude,
                    RouteNote=why + (f" ({'; '.join(notes)})" if notes else ""))
        return base

    detail = "; ".join(notes) if notes else "no ZIP, city+state or county available"
    base["RouteNote"] = f"{why}; no usable fallback ({detail})"
    return base


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _noop(stage: str, fraction: float, detail: str = "") -> None:
    return None


def run_geocoding(
    df: pd.DataFrame,
    mapping: Dict[str, Optional[str]],
    options: Optional[PipelineOptions] = None,
    progress: Optional[ProgressFn] = None,
    geocoder: Optional[CensusGeocoder] = None,
    ref: Optional[ReferenceData] = None,
) -> PipelineResult:
    options = options or PipelineOptions()
    progress = progress or _noop
    geocoder = geocoder or CensusGeocoder()
    ref = ref or load_reference()
    started = _utc_now()
    warnings: List[str] = []

    # ---- 1. Prepare -------------------------------------------------------------
    progress("Preparing addresses", 0.0, f"Cleaning {len(df):,} records")
    original = df.reset_index(drop=True)
    n = len(original)
    clean = address_cleaner.clean_dataframe(
        original, mapping["street"], mapping.get("city"), mapping.get("state"),
        mapping.get("zip"), mapping.get("county"))
    clean.insert(0, "GeocodeRowID", [str(i) for i in range(1, n + 1)])

    routed = pd.DataFrame([_route_row(rec, options, ref) for rec in clean.to_dict("records")],
                          index=clean.index)
    work = pd.concat([clean, routed], axis=1)
    if not ref.available:
        warnings.append("Fallback reference data is missing; area-centroid fallback is disabled.")

    # ---- 2. Resolve benchmark / vintage ----------------------------------------------
    ctx: Optional[GeocodeContext] = None
    census_idx = work.index[work["GeocodeRoute"] == C.ROUTE_CENSUS]
    if len(census_idx):
        try:
            ctx = geocoder.resolve_context(options.benchmark_name, options.vintage_name)
        except CensusGeocoderError as exc:
            logger.error("Could not resolve Census benchmark/vintage: %s", type(exc).__name__)
            warnings.append("Could not reach the Census geocoder to resolve the benchmark/vintage. "
                            "No addresses were submitted.")

    # ---- 3. Build submission (optionally de-duplicated) ------------------------------
    results: Dict[str, BatchRow] = {}
    chunk_log: List[Dict[str, Any]] = []
    rep_of: Dict[int, str] = {}                      # row index -> submitted ID
    records: List[Tuple[str, str, str, str, str]] = []
    if ctx is not None and len(census_idx):
        seen: Dict[Tuple[str, str, str, str], str] = {}
        for idx in census_idx:
            row = work.loc[idx]
            if options.dedupe_addresses:
                key = (row["CleanStreet"].upper(), row["CleanCity"].upper(),
                       row["CleanState"], row["CleanZIP"])
                rid = seen.get(key)
                if rid is None:
                    rid = row["GeocodeRowID"]
                    seen[key] = rid
                    records.append((rid, row["CleanStreet"], row["CleanCity"], row["CleanState"], row["CleanZIP"]))
            else:
                rid = row["GeocodeRowID"]
                records.append((rid, row["CleanStreet"], row["CleanCity"], row["CleanState"], row["CleanZIP"]))
            rep_of[idx] = rid

        # ---- 4. Census batches (sequential, chunked) -----------------------------------
        def batch_progress(stage: str, fraction: float, detail: str) -> None:
            progress(stage, 0.05 + 0.80 * fraction, detail)

        results, chunk_log = geocoder.geocode_batch(
            records, ctx, options.chunk_size, options.include_geographies, batch_progress)
        failed_chunks = [c for c in chunk_log if c["status"] != "OK"]
        if failed_chunks:
            warnings.append(f"{len(failed_chunks)} of {len(chunk_log)} Census batch(es) failed; "
                            "affected records are marked FAILED and can be re-run.")

    # ---- 5. Assemble Census fields (keyed by ID, never by order) --------------------------
    progress("Parsing results", 0.86, "Merging results on the internal row key")
    cols = ["CensusMatchedAddress", "Latitude", "Longitude", "CensusMatchIndicator", "CensusMatchType",
            "TigerLineID", "TigerLineSide", "StateFIPS", "CountyFIPS", "CensusTract", "CensusBlock",
            "GeocodeBatchNumber", "_CensusTimestamp"]
    for c in cols:
        work[c] = np.nan if c in ("Latitude", "Longitude") else None
    work["Latitude"] = work["Latitude"].astype(float)
    work["Longitude"] = work["Longitude"].astype(float)

    for idx, rid in rep_of.items():
        r = results.get(rid)
        if r is None:
            continue
        work.at[idx, "CensusMatchedAddress"] = r.matched_address
        work.at[idx, "Latitude"] = np.nan if r.latitude is None else r.latitude
        work.at[idx, "Longitude"] = np.nan if r.longitude is None else r.longitude
        work.at[idx, "CensusMatchIndicator"] = r.indicator
        work.at[idx, "CensusMatchType"] = r.match_type
        work.at[idx, "TigerLineID"] = r.tiger_line_id
        work.at[idx, "TigerLineSide"] = r.tiger_line_side
        work.at[idx, "StateFIPS"] = r.state_fips
        work.at[idx, "CountyFIPS"] = r.county_fips
        work.at[idx, "CensusTract"] = r.census_tract
        work.at[idx, "CensusBlock"] = r.census_block
        work.at[idx, "GeocodeBatchNumber"] = r.batch_number
        work.at[idx, "_CensusTimestamp"] = r.timestamp_utc

    # ---- 6. Optional fallback for Census No_Match / Tie --------------------------------------
    if options.fallback_for_failed and ref.available:
        retry = work.index[(work["GeocodeRoute"] == C.ROUTE_CENSUS)
                           & work["CensusMatchIndicator"].isin(["No_Match", "Tie"])]
        for idx in retry:
            row = work.loc[idx]
            fb, notes = fallback_centroid(row["CleanState"], row["CleanZIP"], row["CleanCity"],
                                          row.get("CleanCounty", ""), ref)
            if fb:
                work.at[idx, "GeocodeRoute"] = C.ROUTE_FALLBACK
                work.at[idx, "FallbackResolution"] = fb.resolution
                work.at[idx, "FallbackBasis"] = fb.basis
                work.at[idx, "FallbackGeographyLevel"] = fb.geography_level
                work.at[idx, "FallbackLatitude"] = fb.latitude
                work.at[idx, "FallbackLongitude"] = fb.longitude
                work.at[idx, "Latitude"] = np.nan
                work.at[idx, "Longitude"] = np.nan

    # Fallback coordinates go into the shared Latitude / Longitude columns
    fb_mask = work["GeocodeRoute"] == C.ROUTE_FALLBACK
    work.loc[fb_mask, "Latitude"] = work.loc[fb_mask, "FallbackLatitude"].astype(float)
    work.loc[fb_mask, "Longitude"] = work.loc[fb_mask, "FallbackLongitude"].astype(float)

    # ---- 7. QA ------------------------------------------------------------------------------
    progress("Running QA", 0.90, "Comparing input with Census matched addresses")
    qa = apply_qa(work, ref)
    work = pd.concat([work, qa], axis=1)
    work = pd.concat([work, apply_match_levels(work)], axis=1)
    work = pd.concat([work, apply_confidence(work, ref)], axis=1)
    for col in ("AnalystEdit", "AnalystEditNote", "AnalystEditTimestamp"):
        work[col] = None
    work["AnalystEditCount"] = 0

    # ---- 8. Audit fields ---------------------------------------------------------------------
    now = _utc_now()
    year = gazetteer_year(ref)
    audit = {k: [None] * n for k in ("GeocodeProvider", "GeocodeBenchmark", "GeocodeBenchmarkID",
                                     "GeocodeVintage", "GeocodeVintageID", "GeocodeTimestamp", "GeocodeMethod")}
    for pos, (idx, row) in enumerate(work.iterrows()):
        if row["GeocodeRoute"] == C.ROUTE_FALLBACK:
            vals = (C.GEOCODE_PROVIDER, f"Gazetteer {year}", None, None, None, now, C.FALLBACK_METHOD)
        elif row["GeocodeRoute"] == C.ROUTE_CENSUS and ctx is not None and row["CensusMatchIndicator"]:
            a = ctx.audit_fields(row["_CensusTimestamp"])
            vals = (a["GeocodeProvider"], a["GeocodeBenchmark"], a["GeocodeBenchmarkID"], a["GeocodeVintage"],
                    a["GeocodeVintageID"], a["GeocodeTimestamp"], a["GeocodeMethod"])
        else:
            continue
        for key, v in zip(audit, vals):
            audit[key][pos] = v
    for key, values in audit.items():
        work[key] = values

    geo = work[GEO_COLUMNS].copy()
    progress("Complete", 1.0, f"{n:,} locations processed")
    return PipelineResult(
        original=original, geo=geo, mapping=dict(mapping), options=options, context=ctx,
        chunk_log=chunk_log, warnings=warnings, started_utc=started, finished_utc=_utc_now(),
        unique_addresses_submitted=len(records))


# ---------------------------------------------------------------------------
# Summary (dashboard)
# ---------------------------------------------------------------------------
def _pct(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def summarize(geo: pd.DataFrame) -> Dict[str, Any]:
    """KPI counts and percentages. All values are computed from the data.

    The first group (outcomes) is mutually exclusive and sums to ``total``.
    The QA group overlaps it, because a Census match can be downgraded to REVIEW.
    """
    total = len(geo)
    route = geo["GeocodeRoute"]
    census = route == C.ROUTE_CENSUS
    geocoded = geo["GeocodeResolution"] == C.RES_ADDRESS
    exact = geocoded & (geo["CensusMatchType"] == "Exact")
    non_exact = geocoded & ~exact
    tie = census & (geo["CensusMatchIndicator"] == "Tie")
    no_match = census & (geo["CensusMatchIndicator"] == "No_Match")
    fallback = route == C.ROUTE_FALLBACK
    manual = route == C.ROUTE_MANUAL
    out_scope = route == C.ROUTE_OUT_OF_SCOPE
    other = total - int(geocoded.sum() + tie.sum() + no_match.sum() + fallback.sum() + out_scope.sum()
                        + manual.sum())

    def item(mask_or_count) -> Dict[str, Any]:
        count = int(mask_or_count.sum()) if hasattr(mask_or_count, "sum") else int(mask_or_count)
        return {"count": count, "pct": _pct(count, total)}

    outcomes = {
        "Total Locations": {"count": total, "pct": 100.0 if total else 0.0},
        "Successfully Geocoded": item(geocoded),
        "Exact Matches": item(exact),
        "Non-Exact Matches": item(non_exact),
        "Tie": item(tie),
        "No Match": item(no_match),
        "Fallback (area centroid)": item(fallback),
        "Analyst-set Coordinates": item(manual),
        "Out of Scope": item(out_scope),
        "Other Failed / Not Submitted": item(other),
    }
    qa_counts = geo["QAStatus"].value_counts()
    qa = {s: item(int(qa_counts.get(s, 0))) for s in C.QA_ORDER}
    res_counts = geo["GeocodeResolution"].value_counts()
    resolution = {s: item(int(res_counts.get(s, 0))) for s in C.RESOLUTION_ORDER}
    lvl_counts = geo["GeoMatchLevel"].value_counts()
    levels = {s: item(int(lvl_counts.get(s, 0))) for s in C.MATCH_LEVEL_ORDER}
    conf_counts = geo["GeocodeConfidence"].value_counts()
    confidence = {s: item(int(conf_counts.get(s, 0))) for s in C.CONF_ORDER}
    return {"outcomes": outcomes, "qa": qa, "resolution": resolution, "match_levels": levels,
            "confidence": confidence, "total": total}
