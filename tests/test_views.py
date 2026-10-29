"""View-helper tests: map selection, escaping, match-status labels, QA review frame."""
import numpy as np
import pandas as pd

from modules.pipeline import PipelineOptions, run_geocoding
from modules.views import build_view_frame, map_points, match_status, qa_review_frame, view_state_for
from utils import constants as C

# reuse the fake Census service from the pipeline tests
from tests.test_batch_and_pipeline import MAPPING, FakeCensus, client, sov


def _result():
    df = sov([["<b>1</b>", "1 NOMATCH Rd", "New Orleans", "LA", "70112"],
              ["2", "2 TIE Rd", "New Orleans", "LA", "70112"],
              ["3", "3 NONEXACT <script>x</script> Rd", "New Orleans", "LA", "70112"],
              ["4", "4 Good Rd", "New Orleans", "LA", "70112"],
              ["5", "", "Austin", "TX", "78701"],
              ["6", "6 Rd", "San Juan", "PR", "00901"]])
    return run_geocoding(df, MAPPING, PipelineOptions(), geocoder=client(FakeCensus()))


def test_match_status_labels():
    labels = match_status(_result().geo).tolist()
    assert labels == ["No_Match", "Tie", "Non_Exact", "Exact", "Fallback", "Out of scope"]


def test_view_frame_adds_location_id_and_original_address():
    res = _result()
    v = build_view_frame(res)
    assert v.loc[3, "LocationID"] == "4" and v.loc[3, "OriginalAddress"] == "4 Good Rd, New Orleans, LA, 70112"
    assert "OriginalStreet" not in v.columns and len(v) == len(res.geo)


def test_map_never_plots_records_without_coordinates():
    plot, available, sampled = map_points(build_view_frame(_result()))
    assert available == 3 and len(plot) == 3 and not sampled        # 2 street matches + 1 fallback
    assert plot[["Latitude", "Longitude"]].notna().all().all()


def test_map_filters_and_fallback_styling():
    v = build_view_frame(_result())
    plot, _, _ = map_points(v, qa_statuses=[C.QA_FALLBACK])
    assert len(plot) == 1 and plot.loc[plot.index[0], "radius"] == 7
    plot, _, _ = map_points(v, match_statuses=["Exact"])
    assert len(plot) == 1 and plot.iloc[0]["QAStatus"] == "HIGH"
    plot, _, _ = map_points(v, states=["TX"])
    assert len(plot) == 1


def test_map_tooltip_fields_are_html_escaped():
    plot, _, _ = map_points(build_view_frame(_result()))
    joined = " ".join(plot["OriginalAddress"])
    assert "<script>" not in joined and "&lt;script&gt;" in joined


def test_map_samples_large_frames_reproducibly():
    v = build_view_frame(_result())
    big = pd.concat([v[v["Latitude"].notna()]] * 100, ignore_index=True)
    p1, avail, sampled = map_points(big, max_points=50, seed=1)
    p2, _, _ = map_points(big, max_points=50, seed=1)
    assert avail == 300 and sampled and len(p1) == 50 and p1.equals(p2)


def test_view_state_defaults_and_framing():
    assert view_state_for(pd.DataFrame({"Latitude": [], "Longitude": []}))["zoom"] < 4
    one = view_state_for(pd.DataFrame({"Latitude": [30.0], "Longitude": [-90.0]}))
    assert one["zoom"] > 10 and one["latitude"] == 30.0


def test_qa_review_excludes_high_and_applies_filters():
    v = build_view_frame(_result())
    base = qa_review_frame(v)
    assert C.QA_HIGH not in set(base["QAStatus"]) and len(base) == 5
    assert set(qa_review_frame(v, qa_statuses=[C.QA_FAILED])["MatchStatus"]) == {"No_Match"}
    assert len(qa_review_frame(v, match_statuses=["Tie"])) == 1


def test_state_outline_is_bundled_and_valid():
    from modules.views import load_state_outline
    g = load_state_outline()
    assert g is not None and g["type"] == "FeatureCollection"
    states = {f["properties"]["state"] for f in g["features"]}
    assert {"TX", "FL", "SC", "DC", "AK", "HI"} <= states and len(states) >= 51
    assert all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in g["features"])


def test_state_outline_missing_file_returns_none(tmp_path, monkeypatch):
    import modules.views as v
    v.load_state_outline.cache_clear()
    monkeypatch.setattr(v, "STATE_OUTLINE_PATH", tmp_path / "nope.geojson")
    assert v.load_state_outline() is None
    v.load_state_outline.cache_clear()
