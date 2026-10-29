"""US Exposure Geocoding & QA Tool - Streamlit interface.

UI code only. All processing lives in ``modules/`` so it can be tested without Streamlit.

Privacy: uploads are processed in memory only and are never written to disk.
The only external destination for exposure data is the U.S. Census Geocoder.
"""
from __future__ import annotations

import base64
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

from modules import file_handler as fh
from modules.address_cleaner import clean_dataframe
from modules.census_geocoder import CensusGeocoder
from modules.pipeline import PipelineOptions, run_geocoding, summarize
from modules.views import (
    LEGEND,
    TOOLTIP_HTML,
    build_view_frame,
    load_state_outline,
    map_points,
    qa_review_frame,
    view_state_for,
)
from utils import constants as C
from utils.logging_config import setup_logging

APP_NAME = "US Exposure Geocoding & QA Tool"
APP_VERSION = "0.1.1 (test build)"

st.set_page_config(page_title=APP_NAME, page_icon="📍", layout="wide")
setup_logging()

NOT_MAPPED = "— not mapped —"
# A style with no tile sources, as a data: URI, so offline mode makes no third-party requests.
# (Streamlit falls back to Carto tiles when map_style / map_provider are simply omitted.)
_BLANK = '{"version":8,"sources":{},"layers":[{"id":"bg","type":"background","paint":{"background-color":"#dde5ee"}}]}'
BLANK_STYLE = "data:application/json;base64," + base64.b64encode(_BLANK.encode()).decode()
MAX_TABLE_ROWS = 5_000


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def _init_state() -> None:
    defaults = {
        "upload_sig": None, "file_name": None, "file_bytes": None, "sheets": None,
        "read_key": None, "raw_df": None, "read_error": None,
        "mapping": None, "mapping_ok": False,
        "result": None, "result_id": None, "view": None, "summary": None,
        "single_result": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _reset_for_new_file() -> None:
    for key in ("sheets", "read_key", "raw_df", "read_error", "mapping", "result",
                "result_id", "view", "summary"):
        st.session_state[key] = None
    st.session_state["mapping_ok"] = False


_init_state()


# ---------------------------------------------------------------------------
# Cached helpers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def census_options() -> dict:
    """{benchmark name: [vintage names]} from the live Census service (default if unreachable)."""
    options: dict = {}
    try:
        client = CensusGeocoder(max_retries=1)
        for bench in client.list_benchmarks():
            try:
                options[bench.name] = [v.name for v in client.list_vintages(bench.id)]
            except Exception:
                continue
    except Exception:
        pass
    return options or {C.DEFAULT_BENCHMARK_NAME: [C.DEFAULT_VINTAGE_NAME]}


@st.cache_data(show_spinner=False, max_entries=12)
def make_file(result_id: str, subset: str, fmt: str, filter_key: str, _res, _mask) -> bytes:
    """Build a download. ``_res``/``_mask`` are unhashed; the other args form the cache key."""
    frame = _res.export_frame(_mask)
    return fh.to_xlsx_bytes(frame, "Geocoded") if fmt == "XLSX" else fh.to_csv_bytes(frame)


def _kpi(col, label: str, item: dict, show_pct: bool = True) -> None:
    col.metric(label, f"{item['count']:,}", f"{item['pct']:.1f}%" if show_pct else None,
               delta_color="off")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


# ---------------------------------------------------------------------------
# Header + mandatory disclaimer
# ---------------------------------------------------------------------------
st.title(f"📍 {APP_NAME}")
st.caption(f"Version {APP_VERSION} · Upload an SOV → map columns → geocode with the U.S. Census Geocoder "
           "→ review quality → visualise → export")
st.warning(f"**{C.CAT_DISCLAIMER_TITLE}.** {C.CAT_DISCLAIMER}", icon="⚠️")

# ---------------------------------------------------------------------------
# Sidebar: settings + privacy
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Geocoding settings")
    chunk_size = st.number_input(
        "Batch size (records per Census request)", min_value=1, max_value=C.BATCH_MAX_RECORDS,
        value=C.DEFAULT_BATCH_SIZE, step=100,
        help=f"Census allows up to {C.BATCH_MAX_RECORDS:,} per batch. Smaller batches are easier to "
             "test and recover from. Batches run one after another, never in parallel.")
    include_geo = st.checkbox("Request State / County / Tract / Block codes", value=True)
    dedupe = st.checkbox("Geocode identical addresses once", value=False,
                         help="Identical cleaned addresses are submitted once and the result is shared.")
    fallback_failed = st.checkbox("Use area centroid when Census returns No_Match / Tie", value=False,
                                  help="Rows with no street always use the fallback. This option also "
                                       "applies it to rows that had a street but Census could not match.")
    submit_insufficient = st.checkbox("Submit street-only rows to Census anyway", value=False,
                                      help="Census needs street + ZIP, or street + city + state.")

    opts_map = census_options()
    bench_names = list(opts_map)
    bench_default = bench_names.index(C.DEFAULT_BENCHMARK_NAME) if C.DEFAULT_BENCHMARK_NAME in bench_names else 0
    benchmark = st.selectbox("Census benchmark", bench_names, index=bench_default,
                             help="'Current' is a moving alias; the exact benchmark and vintage used "
                                  "are recorded in every output row.")
    vint_names = opts_map.get(benchmark) or [C.DEFAULT_VINTAGE_NAME]
    vint_default = vint_names.index(C.DEFAULT_VINTAGE_NAME) if C.DEFAULT_VINTAGE_NAME in vint_names else 0
    vintage = st.selectbox("Geography vintage", vint_names, index=vint_default,
                           help="Used for the State/County/Tract/Block lookup.")

    st.divider()
    st.subheader("Privacy")
    st.caption("Files are processed in memory and are not saved to disk. The only external service that "
               "receives address data is the U.S. Census Geocoder. No analytics or tracking is used. "
               "The default map background (state outlines) is bundled and makes no outside requests. The optional "
               "online basemap loads tiles from a public provider in your browser; they contain no exposure data.")

options = PipelineOptions(
    chunk_size=int(chunk_size), include_geographies=include_geo, dedupe_addresses=dedupe,
    fallback_for_failed=fallback_failed, submit_insufficient=submit_insufficient,
    benchmark_name=benchmark, vintage_name=vintage)

tab_upload, tab_geocode, tab_dash, tab_map, tab_qa, tab_dl, tab_single, tab_about = st.tabs([
    "1 · Upload & Map", "2 · Geocode", "3 · Dashboard", "4 · Map", "5 · QA Review",
    "6 · Downloads", "Single Address", "About & Method"])

# ===========================================================================
# 1. Upload & column mapping
# ===========================================================================
with tab_upload:
    st.subheader("Upload your Statement of Values")
    uploaded = st.file_uploader("CSV or XLSX", type=["csv", "xlsx"],
                                help=f"Up to {C.MAX_UPLOAD_MB} MB / {C.MAX_ROWS:,} rows. "
                                     "Your file is never modified.")
    if uploaded is not None:
        sig = getattr(uploaded, "file_id", None) or f"{uploaded.name}:{uploaded.size}"
        if st.session_state.upload_sig != sig:
            _reset_for_new_file()
            st.session_state.upload_sig = sig
            st.session_state.file_name = uploaded.name
            st.session_state.file_bytes = uploaded.getvalue()

        data, name = st.session_state.file_bytes, st.session_state.file_name
        sheet = None
        if name.lower().endswith(".xlsx") and data is not None:
            if st.session_state.sheets is None:
                try:
                    st.session_state.sheets = fh.excel_sheet_names(data)
                except fh.FileReadError as exc:
                    st.session_state.read_error = str(exc)
            if st.session_state.sheets:
                sheet = st.selectbox("Worksheet", st.session_state.sheets, key=f"sheet_{sig}")

        read_key = (sig, sheet)
        if st.session_state.read_key != read_key and data is not None and not st.session_state.read_error:
            try:
                st.session_state.raw_df = fh.read_upload(name, data, sheet)
                st.session_state.read_error = None
            except fh.FileReadError as exc:
                st.session_state.raw_df, st.session_state.read_error = None, str(exc)
            st.session_state.read_key = read_key
            st.session_state.result = st.session_state.view = st.session_state.summary = None
            if not name.lower().endswith(".xlsx"):
                st.session_state.file_bytes = None        # CSV is fully parsed; free the raw bytes

    if st.session_state.read_error:
        st.error(st.session_state.read_error)

    raw_df = st.session_state.raw_df
    if raw_df is None:
        st.info("Upload a CSV or XLSX file to begin. Try `sample_data/sample_sov.csv` from the repository "
                "for a quick test (public landmark addresses and deliberately broken rows).")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Rows", f"{len(raw_df):,}")
        c2.metric("Columns", f"{raw_df.shape[1]:,}")
        c3.metric("File", st.session_state.file_name)
        with st.expander("Preview (first 10 rows, exactly as uploaded)", expanded=False):
            st.dataframe(raw_df.head(10), hide_index=True)

        st.subheader("Map your columns")
        st.caption("Only Location ID and Street Address are required. City, State and ZIP greatly "
                   "improve match rates. County is optional and used only for the area-centroid fallback.")
        suggestions = fh.suggest_mapping(list(raw_df.columns))
        columns = [NOT_MAPPED] + list(raw_df.columns)
        mapping = {}
        cols = st.columns(3)
        for i, field_name in enumerate(["location_id", "street", "city", "state", "zip", "county"]):
            suggested = suggestions.get(field_name)
            idx = columns.index(suggested) if suggested in columns else 0
            choice = cols[i % 3].selectbox(fh.MAPPING_LABELS[field_name], columns, index=idx,
                                           key=f"map_{field_name}_{st.session_state.upload_sig}")
            mapping[field_name] = None if choice == NOT_MAPPED else choice

        report = fh.validate_mapping(raw_df, mapping)
        st.session_state.mapping, st.session_state.mapping_ok = mapping, report.ok
        for msg in report.errors:
            st.error(msg)
        for msg in report.warnings:
            st.warning(msg)

        if report.ok:
            with st.expander("Cleaning preview (first 10 rows): originals are never overwritten"):
                preview = clean_dataframe(raw_df.head(10), mapping["street"], mapping.get("city"),
                                          mapping.get("state"), mapping.get("zip"), mapping.get("county"))
                st.dataframe(preview, hide_index=True)
            st.success("Mapping looks good. Continue to **2 · Geocode**.")

# ===========================================================================
# 2. Geocode
# ===========================================================================
with tab_geocode:
    st.subheader("Geocode with the U.S. Census Geocoder")
    raw_df, mapping_ok = st.session_state.raw_df, st.session_state.mapping_ok
    if raw_df is None or not mapping_ok:
        st.info("Upload a file and complete the column mapping first.")
    else:
        n = len(raw_df)
        batches = max(1, math.ceil(n / options.chunk_size))
        st.write(f"**{n:,}** locations · up to **{batches:,}** Census batch request(s) of "
                 f"{options.chunk_size:,} records, run sequentially.")
        st.caption("Rows with a street plus ZIP (or city and state) go to the Census Geocoder. Rows with no "
                   "usable street receive an area-centroid fallback (ZIP → city → county) and are clearly "
                   "labelled FALLBACK. Puerto Rico and U.S. territories are out of scope in this version.")

        if st.button("▶ Run geocoding", type="primary"):
            bar = st.progress(0.0, text="Preparing addresses")

            def on_progress(stage: str, fraction: float, detail: str) -> None:
                bar.progress(min(max(fraction, 0.0), 1.0), text=f"{stage}: {detail}")

            try:
                result = run_geocoding(raw_df, st.session_state.mapping, options, progress=on_progress)
                on_progress("Creating map", 0.97, "Preparing map and dashboard data")
                st.session_state.result = result
                st.session_state.result_id = uuid.uuid4().hex
                st.session_state.view = build_view_frame(result)
                st.session_state.summary = summarize(result.geo)
                on_progress("Complete", 1.0, f"{n:,} locations processed")
            except Exception:                                   # never show a stack trace to the analyst
                st.session_state.result = None
                st.error("Something went wrong while processing this file. Please check the column mapping "
                         "and try again. Technical details were written to the application log.")
                import logging
                logging.getLogger(__name__).exception("Pipeline failure")

        result = st.session_state.result
        if result is not None:
            s = st.session_state.summary["outcomes"]
            st.success(f"Done. {s['Successfully Geocoded']['count']:,} of {s['Total Locations']['count']:,} "
                       f"locations geocoded at street level ({s['Successfully Geocoded']['pct']:.1f}%). "
                       "Open the Dashboard, Map and QA Review tabs.")
            for w in result.warnings:
                st.warning(w)
            if result.column_collisions():
                st.info("Your file already contains columns with the same names as some appended fields. "
                        "Originals were kept; the appended versions carry a `_Geocode` suffix: "
                        + ", ".join(f"`{a}` → `{b}`" for a, b in result.column_collisions().items()))
            if result.chunk_log:
                with st.expander("Batch log"):
                    st.dataframe(pd.DataFrame(result.chunk_log), hide_index=True)

# ===========================================================================
# 3. Dashboard
# ===========================================================================
with tab_dash:
    st.subheader("Geocoding summary")
    result = st.session_state.result
    if result is None:
        st.info("Run geocoding to see the dashboard.")
    else:
        summ = st.session_state.summary
        o = summ["outcomes"]
        st.markdown("**Census outcomes** (each location counted once)")
        r1 = st.columns(5)
        _kpi(r1[0], "Total Locations", o["Total Locations"], show_pct=False)
        _kpi(r1[1], "Successfully Geocoded", o["Successfully Geocoded"])
        _kpi(r1[2], "Exact Matches", o["Exact Matches"])
        _kpi(r1[3], "Non-Exact Matches", o["Non-Exact Matches"])
        _kpi(r1[4], "Tie", o["Tie"])
        r2 = st.columns(4)
        _kpi(r2[0], "No Match", o["No Match"])
        _kpi(r2[1], "Fallback (area centroid)", o["Fallback (area centroid)"])
        _kpi(r2[2], "Out of Scope", o["Out of Scope"])
        _kpi(r2[3], "Other Failed / Not Submitted", o["Other Failed / Not Submitted"])
        st.caption("“Successfully Geocoded” means a street-level Census match with coordinates. "
                   "Fallback centroids are never counted as geocoded.")

        st.markdown("**QA status** (application-generated; a matched location can be downgraded to REVIEW)")
        qa_cols = st.columns(len(C.QA_ORDER))
        for col, status in zip(qa_cols, C.QA_ORDER):
            _kpi(col, status.replace("_", " ").title() if status == C.QA_OUT_OF_SCOPE else status,
                 summ["qa"][status])
        st.caption(C.QA_DISCLAIMER)

        st.markdown("**Geocode resolution**")
        res_df = pd.DataFrame([{"Resolution": k, "Locations": v["count"], "Share (%)": v["pct"]}
                               for k, v in summ["resolution"].items()])
        left, right = st.columns([1, 1])
        left.dataframe(res_df, hide_index=True)
        right.bar_chart(res_df.set_index("Resolution")["Locations"])
        if summ["resolution"][C.RES_ZIP]["count"] + summ["resolution"][C.RES_CITY]["count"] \
                + summ["resolution"][C.RES_COUNTY]["count"]:
            st.warning(C.FALLBACK_DISCLAIMER)

        with st.expander("Run details (audit)"):
            ctx = result.context
            st.write({
                "Provider": C.GEOCODE_PROVIDER,
                "Benchmark (resolved)": f"{ctx.benchmark_name} (id {ctx.benchmark_id})" if ctx else "n/a",
                "Benchmark description": ctx.benchmark_description if ctx else "n/a",
                "Vintage (resolved)": f"{ctx.vintage_name} (id {ctx.vintage_id})" if ctx else "n/a",
                "Started (UTC)": result.started_utc, "Finished (UTC)": result.finished_utc,
                "Batch size": result.options.chunk_size,
                "Records submitted to Census": result.unique_addresses_submitted,
                "De-duplication": result.options.dedupe_addresses,
            })

# ===========================================================================
# 4. Map
# ===========================================================================
with tab_map:
    st.subheader("Portfolio map")
    result, view = st.session_state.result, st.session_state.view
    if result is None:
        st.info("Run geocoding to see the map.")
    else:
        rid = st.session_state.result_id
        located = view[view["Latitude"].notna()]
        state_opts = sorted(s for s in located["CleanState"].dropna().unique() if s)
        qa_opts = [s for s in C.QA_ORDER if s in set(located["QAStatus"])]
        ms_opts = sorted(located["MatchStatus"].unique())

        f1, f2, f3 = st.columns(3)
        sel_states = f1.multiselect("State", state_opts, default=state_opts, key=f"map_state_{rid}")
        sel_qa = f2.multiselect("QA status", qa_opts, default=qa_opts, key=f"map_qa_{rid}")
        sel_ms = f3.multiselect("Match type", ms_opts, default=ms_opts, key=f"map_ms_{rid}")
        g1, g2 = st.columns([2, 1])
        max_points = g1.slider("Maximum points drawn", 1_000, 200_000, 50_000, step=1_000,
                               help="Large portfolios are randomly sampled for the map only. "
                                    "Tables and downloads always contain every record.")
        bg_options = ["State outlines (offline)", "Online basemap tiles", "None"]
        background = g2.radio(
            "Map background", bg_options, index=0, key=f"map_bg_{rid}",
            help="State outlines are bundled with the app: no third-party requests. Online tiles come "
                 "from a public map provider (cartocdn.com), are fetched by your browser, and may be "
                 "blocked on corporate networks.")

        # a state filter on rows without a state (e.g. invalid state) must not hide them
        states_arg = None if set(sel_states) == set(state_opts) else sel_states
        plot, available, sampled = map_points(
            view, qa_statuses=sel_qa, states=states_arg, match_statuses=sel_ms, max_points=max_points)

        if plot.empty:
            st.info("No located records match these filters. Records without coordinates are never plotted.")
        else:
            st.caption(f"Showing {len(plot):,} of {available:,} located records"
                       + (" (random sample for performance)" if sampled else "") + ".")
            layer = pdk.Layer(
                "ScatterplotLayer", data=plot, get_position=["Longitude", "Latitude"],
                get_fill_color="color", get_radius="radius", radius_units="pixels",
                radius_min_pixels=3, radius_max_pixels=14, pickable=True, stroked=False)
            layers = [layer]
            outline = load_state_outline() if background == bg_options[0] else None
            if background == bg_options[0] and outline is None:
                st.warning("The bundled state outline file (data/reference/us_states.geojson) was not found; "
                           "showing points only.")
            if outline is not None:
                outline_layer = pdk.Layer(
                    "GeoJsonLayer", data=outline, stroked=True, filled=True, pickable=False,
                    get_fill_color=[244, 246, 249, 255], get_line_color=[140, 150, 165, 255],
                    line_width_min_pixels=1)
                layers = [outline_layer, layer]
            vs = view_state_for(plot)
            if background == bg_options[1]:
                deck_kwargs = dict(map_provider="carto", map_style="light")
            else:
                deck_kwargs = dict(map_provider="carto", map_style=BLANK_STYLE)
            st.pydeck_chart(pdk.Deck(
                layers=layers,
                initial_view_state=pdk.ViewState(latitude=vs["latitude"], longitude=vs["longitude"],
                                                 zoom=vs["zoom"]),
                tooltip={"html": TOOLTIP_HTML, "style": {"backgroundColor": "white", "color": "#222"}},
                **deck_kwargs))
            if background == bg_options[1]:
                st.caption("If the background stays blank, your network is probably blocking "
                           "basemaps.cartocdn.com. Switch to 'State outlines (offline)'.")
            st.markdown(" &nbsp; ".join(
                f"<span style='color:{c}'>●</span> {n}" for n, c in LEGEND), unsafe_allow_html=True)
            st.caption("Census points are interpolated along street address ranges and are not rooftop "
                       "locations. Purple FALLBACK points are area centroids. "
                       "Failed / no-coordinate records are not plotted.")

# ===========================================================================
# 5. QA Review
# ===========================================================================
with tab_qa:
    st.subheader("QA review")
    result, view = st.session_state.result, st.session_state.view
    if result is None:
        st.info("Run geocoding to review exceptions.")
    else:
        rid = st.session_state.result_id
        base = qa_review_frame(view)
        st.caption("Shows everything that is not a clean HIGH match: non-exact matches, ties, no matches, "
                   "address mismatches, missing coordinates, fallbacks and out-of-scope rows. "
                   + C.QA_DISCLAIMER)
        if base.empty:
            st.success("No exceptions: every location is a HIGH-confidence match.")
        else:
            st_opts = sorted(s for s in base["CleanState"].dropna().unique() if s) or []
            ms_opts = sorted(base["MatchStatus"].unique())
            qa_opts = [s for s in C.QA_ORDER if s in set(base["QAStatus"])]
            q1, q2, q3 = st.columns(3)
            sel_states = q1.multiselect("State", st_opts, default=st_opts, key=f"qa_state_{rid}")
            sel_ms = q2.multiselect("Match status", ms_opts, default=ms_opts, key=f"qa_ms_{rid}")
            sel_qa = q3.multiselect("QA status", qa_opts, default=qa_opts, key=f"qa_qa_{rid}")
            states_arg = None if set(sel_states) == set(st_opts) else sel_states
            filtered = qa_review_frame(view, sel_qa, states_arg, sel_ms)

            st.write(f"**{len(filtered):,}** of {len(base):,} exception records")
            show_cols = ["LocationID", "OriginalAddress", "CensusMatchedAddress", "MatchStatus", "QAStatus",
                         "QAReason", "GeocodeResolution", "Latitude", "Longitude", "CleanFlags"]
            st.dataframe(filtered[show_cols].head(MAX_TABLE_ROWS), hide_index=True)
            if len(filtered) > MAX_TABLE_ROWS:
                st.caption(f"Table limited to the first {MAX_TABLE_ROWS:,} rows; the download contains all "
                           f"{len(filtered):,}.")

            mask = result.geo.index.isin(filtered.index)
            key = f"{sorted(sel_states)}|{sorted(sel_ms)}|{sorted(sel_qa)}"
            fmt = st.radio("Format", ["CSV", "XLSX"], horizontal=True, key=f"qa_fmt_{rid}")
            payload = make_file(rid, "qa_exceptions", fmt, key, result, pd.Series(mask, index=result.geo.index))
            st.download_button(
                f"⬇ Download these exceptions ({fmt})", payload,
                file_name=f"qa_exceptions_{_stamp()}.{fmt.lower()}",
                mime="text/csv" if fmt == "CSV" else
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                on_click="ignore")

# ===========================================================================
# 6. Downloads
# ===========================================================================
with tab_dl:
    st.subheader("Downloads")
    result = st.session_state.result
    if result is None:
        st.info("Run geocoding to enable downloads.")
    else:
        rid = st.session_state.result_id
        fmt = st.radio("Format", ["CSV", "XLSX"], horizontal=True, key=f"dl_fmt_{rid}",
                       help="XLSX is slower to build for very large files. Every original column is preserved.")
        stem = Path(st.session_state.file_name or "sov").stem
        subsets = [
            ("Complete enriched SOV", "complete", None,
             "Every original column plus all geocoding, QA and audit fields."),
            ("Successfully geocoded (street level)", "geocoded", result.mask_geocoded(),
             "Street-level Census matches with coordinates (includes REVIEW-status matches)."),
            ("QA review / exceptions", "exceptions", result.mask_exceptions(),
             "Everything that is not a clean HIGH match."),
            ("Failed / no-coordinate locations", "failed", result.mask_failed(),
             "No coordinates: No_Match, Tie, insufficient address, out of scope."),
            ("Fallback locations (area centroids)", "fallback", result.mask_fallback(),
             "ZIP / city / county centroids. Not suitable for site-level hazard modelling."),
        ]
        for label, code, mask, helptext in subsets:
            count = len(result.geo) if mask is None else int(mask.sum())
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{label}** · {count:,} rows  \n{helptext}")
            if count == 0:
                c2.caption("No rows")
                continue
            try:
                data = make_file(rid, code, fmt, "", result, mask)
                c2.download_button(
                    f"⬇ {fmt}", data, file_name=f"{stem}_{code}_{_stamp()}.{fmt.lower()}",
                    mime="text/csv" if fmt == "CSV" else
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_{code}_{fmt}_{rid}", on_click="ignore")
            except fh.FileReadError as exc:
                c2.error(str(exc))

# ===========================================================================
# Single address
# ===========================================================================
with tab_single:
    st.subheader("Single-address test")
    st.caption("Geocodes one address with the Census single-address service. The address you type is sent "
               "to the Census Bureau. Use public or test addresses when experimenting.")
    with st.form("single_form"):
        a, b = st.columns(2)
        s_street = a.text_input("Street", value="4600 Silver Hill Rd")
        s_city = b.text_input("City", value="Washington")
        c_, d_, e_ = st.columns([1, 1, 2])
        s_state = c_.text_input("State", value="DC")
        s_zip = d_.text_input("ZIP", value="20233")
        s_geo = e_.checkbox("Include State / County / Tract / Block", value=True)
        go = st.form_submit_button("Geocode")
    if go:
        with st.spinner("Contacting the Census Geocoder…"):
            st.session_state.single_result = CensusGeocoder().geocode_single(
                s_street, s_city, s_state, s_zip, include_geographies=s_geo,
                benchmark_name=benchmark, vintage_name=vintage)

    sr = st.session_state.single_result
    if sr is not None:
        {"MATCH": st.success, "NO_MATCH": st.warning, "TIE": st.warning,
         "INSUFFICIENT_INPUT": st.warning, "ERROR": st.error}.get(sr.status, st.info)(sr.message)
        if sr.status == C.STATUS_MATCH and sr.best:
            rec = sr.to_record()
            m1, m2, m3 = st.columns(3)
            m1.metric("Latitude", f"{rec['Latitude']:.6f}")
            m2.metric("Longitude", f"{rec['Longitude']:.6f}")
            m3.metric("TIGER Line ID / side", f"{rec['TigerLineID']} / {rec['TigerLineSide']}")
            st.write(f"**Matched address:** {rec['CensusMatchedAddress']}")
            st.caption(f"Geocode Source = {C.GEOCODE_SOURCE} · Coordinate Method = {C.COORDINATE_METHOD}. "
                       "Not a rooftop location. The single-address service does not report Exact / Non_Exact.")
            st.map(pd.DataFrame({"lat": [rec["Latitude"]], "lon": [rec["Longitude"]]}), zoom=15)
            with st.expander("Address components and audit fields"):
                st.json({"addressComponents": sr.best.components,
                         "record": {k: v for k, v in rec.items() if v is not None}})
        elif sr.status == C.STATUS_TIE:
            st.dataframe(pd.DataFrame([{"Matched address": c.matched_address, "Latitude": c.latitude,
                                        "Longitude": c.longitude, "TIGER Line ID": c.tigerline_id}
                                       for c in sr.candidates]), hide_index=True)

# ===========================================================================
# About
# ===========================================================================
with tab_about:
    st.subheader("About & method")
    st.markdown(f"""
**Workflow:** Upload SOV → inspect → map columns → clean addresses → geocode (Census) → evaluate match quality →
review exceptions → visualise → export.

#### Geocoding source
* **Geocode Source = {C.GEOCODE_SOURCE}**, **Coordinate Method = {C.COORDINATE_METHOD}**.
* The Census Geocoder returns no numeric confidence score. {C.QA_DISCLAIMER}

#### QA rules (applied to Census matches)
| Check | Effect |
|---|---|
| Match + Exact | HIGH |
| Match + Non_Exact | MEDIUM |
| Tie | REVIEW (no coordinate selected) |
| No_Match, or no result | FAILED |
| State differs from input | REVIEW (forced) |
| ZIP differs from input | one level lower |
| City differs (similarity < 0.80) | one level lower |
| House number differs | one level lower |
| Street name similarity < 0.70 | one level lower |
| Coordinate outside the expected area for the state | REVIEW (forced) |
| Match without usable coordinates | FAILED |

Address comparison never lowers a match below REVIEW. Every applied rule is written to `QAReason`.

#### Other statuses
* **FALLBACK** – no usable street (or PO Box): the centroid of the ZIP code tabulation area, else the place
  (city + state), else the county is used. {C.FALLBACK_DISCLAIMER}
* **OUT_OF_SCOPE** – Puerto Rico and U.S. territories (not supported in this version).

#### Resolution fields
`GeocodeResolution` = how the coordinate was produced ({', '.join(C.RESOLUTION_ORDER)}).
`CensusGeographyLevel` = finest Census geography returned (BLOCK, TRACT, COUNTY, STATE) or the fallback area
type (ZCTA, PLACE, COUNTY). A block code does **not** imply block-level accuracy.

#### Known limitations
* Census ranges can place points on the wrong side of a street or far along a long segment.
* New construction and renamed streets may not match. Unique-use ZIP codes are not ZIP code tabulation areas,
  so ZIP fallback is unavailable for them.
* PO Boxes cannot be geocoded to a site. Puerto Rico and territories are excluded.
* Census does not publish rate limits; very large runs can take a long time and may hit transient errors
  (retried automatically up to {C.MAX_RETRIES} times).
""")
