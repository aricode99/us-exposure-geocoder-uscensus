"""Batch geocoding and pipeline tests. A fake Census service is used throughout."""
import csv
import io
import json

import numpy as np
import pandas as pd
import pytest
import requests

from modules.census_geocoder import (
    CensusGeocoder,
    build_batch_csv,
    parse_batch_output,
)
from modules.pipeline import GEO_COLUMNS, PipelineOptions, run_geocoding, summarize
from utils import constants as C

BENCHMARKS = {"benchmarks": [{"isDefault": True, "benchmarkDescription": "Public Address Ranges - Current Benchmark",
                              "id": "4", "benchmarkName": "Public_AR_Current"}]}
VINTAGES = {"vintages": [{"isDefault": True, "id": "4", "vintageName": "Current_Current",
                          "vintageDescription": "Current Vintage - Current Benchmark"}]}


# --------------------------------------------------------------------------------------
# Fake Census service
# --------------------------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = json.dumps(payload) if payload is not None else text

    def json(self):
        return json.loads(self.text)


class FakeCensus:
    """GET serves benchmarks/vintages; POST emulates the headerless batch CSV.

    Output rows are returned in REVERSE order to prove nothing depends on row order.
    Street text controls the outcome: NOMATCH -> No_Match, TIE -> Tie,
    NONEXACT -> Non_Exact, otherwise Exact. Coordinates encode the submitted ID so a
    mis-join is detectable: lat = 30 + id/1e6, lon = -90.
    """

    def __init__(self, fail_batches=(), geographies_missing=False, benchmarks_down=False):
        self.posts = []
        self.fail_batches = set(fail_batches)
        self.geographies_missing = geographies_missing
        self.benchmarks_down = benchmarks_down

    def get(self, url, params=None, timeout=None):
        if self.benchmarks_down:
            return Resp(503)
        return Resp(payload=VINTAGES if "vintages" in url else BENCHMARKS)

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append({"url": url, "data": data, "n": None})
        number = len(self.posts)
        if number in self.fail_batches:
            raise requests.Timeout("secret?street=1 Main")
        content = files["addressFile"][1].decode()
        rows = list(csv.reader(io.StringIO(content)))
        self.posts[-1]["n"] = len(rows)
        self.posts[-1]["first_row"] = rows[0] if rows else None
        out = io.StringIO()
        w = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
        geo = url.endswith("geographies/addressbatch")
        for rid, street, city, state, zipc in reversed(rows):
            inp = f"{street}, {city}, {state}, {zipc}"
            if "NOMATCH" in street:
                w.writerow([rid, inp, "No_Match"])
            elif "TIE" in street:
                w.writerow([rid, inp, "Tie"])
            else:
                mtype = "Non_Exact" if "NONEXACT" in street else "Exact"
                matched = f"{street.upper()}, {city.upper()}, {state}, {zipc}"
                lat = 30 + int(rid) / 1e6
                rec = [rid, inp, "Match", mtype, matched, f"-90.0,{lat}", "111", "L"]
                if geo and not self.geographies_missing:
                    rec += ["22", "071", "000100", "1001"]
                w.writerow(rec)
        return Resp(text=out.getvalue())


def client(fake, **kw):
    return CensusGeocoder(session=fake, sleep=lambda s: None, **kw)


def records(n, street="1 Main St"):
    return [(str(i), f"{i} {street}", "New Orleans", "LA", "70112") for i in range(1, n + 1)]


def sov(rows):
    return pd.DataFrame(rows, columns=["LocID", "Address", "City", "State", "ZIP"])


MAPPING = {"location_id": "LocID", "street": "Address", "city": "City", "state": "State", "zip": "ZIP"}


# --------------------------------------------------------------------------------------
# Batch input / output
# --------------------------------------------------------------------------------------
def test_build_batch_csv_has_no_header_quotes_commas_and_keeps_empty_fields():
    data = build_batch_csv([("1", "10 Main St, Apt 2", "", "TX", "02134"),
                            ("2", "5 Elm\nSt", "Austin", "TX", "")]).decode()
    lines = data.strip("\n").split("\n")
    assert lines[0] == '1,"10 Main St, Apt 2",,TX,02134'
    assert lines[1] == "2,5 Elm St,Austin,TX,"             # newline flattened, empty fields kept


def test_parse_batch_output_full_geographies_row():
    text = ('"7","a","Match","Exact","4600 SILVER HILL RD, WASHINGTON, DC, 20233",'
            '"-76.92836638093,38.84505589808","657091557","L","24","033","802405","2004"\n')
    rows, bad = parse_batch_output(text)
    r = rows["7"]
    assert bad == 0 and r.indicator == "Match" and r.match_type == "Exact"
    assert r.latitude == pytest.approx(38.84505589808) and r.longitude == pytest.approx(-76.92836638093)
    assert (r.state_fips, r.county_fips, r.census_tract, r.census_block) == ("24", "033", "802405", "2004")
    assert (r.tiger_line_id, r.tiger_line_side) == ("657091557", "L")


def test_parse_batch_output_locations_row_has_null_geographies():
    text = '"1","a","Match","Non_Exact","X, Y, TX, 75001","-96.5,32.5","9","R"\n'
    r = parse_batch_output(text)[0]["1"]
    assert r.match_type == "Non_Exact" and r.state_fips is None and r.census_block is None


def test_parse_batch_output_no_match_and_tie_have_no_coordinates():
    text = '"3","x","No_Match"\n"4","y","Tie"\n'
    rows, bad = parse_batch_output(text)
    assert bad == 0
    assert rows["3"].indicator == "No_Match" and rows["3"].latitude is None
    assert rows["4"].indicator == "Tie" and rows["4"].matched_address is None


def test_parse_batch_output_counts_malformed_and_ignores_blank_lines():
    text = '\n"1","a","Match","Exact","m","-90,30","1","L"\nGARBAGE\n"2","b","Maybe"\n"","c","Match"\n,,\n'
    rows, bad = parse_batch_output(text)
    assert list(rows) == ["1"] and bad == 3


@pytest.mark.parametrize("coords", ["", "abc", "1,2,3", "500,30", "-90,95"])
def test_parse_batch_output_bad_coordinates_become_none(coords):
    text = f'"1","a","Match","Exact","m","{coords}","1","L"\n'
    r = parse_batch_output(text)[0]["1"]
    assert r.latitude is None or r.longitude is None


# --------------------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------------------
def test_23500_records_become_10000_10000_3500_and_recombine_by_id():
    fake = FakeCensus()
    results, log = client(fake).geocode_batch(records(23_500), client(fake).resolve_context(), chunk_size=10_000)
    assert [p["n"] for p in fake.posts] == [10_000, 10_000, 3_500]
    assert [e["submitted"] for e in log] == [10_000, 10_000, 3_500]
    assert len(results) == 23_500
    # fake returns rows reversed: every ID must still carry ITS OWN coordinate
    assert all(results[str(i)].latitude == pytest.approx(30 + i / 1e6) for i in (1, 9_999, 10_000, 10_001, 23_500))
    assert results["23500"].batch_number == 3


def test_chunk_size_is_clamped_to_census_limit_and_minimum():
    fake = FakeCensus()
    c = client(fake)
    c.geocode_batch(records(10_005), c.resolve_context(), chunk_size=50_000)
    assert [p["n"] for p in fake.posts] == [10_000, 5]
    fake2 = FakeCensus()
    c2 = client(fake2)
    c2.geocode_batch(records(3), c2.resolve_context(), chunk_size=0)
    assert [p["n"] for p in fake2.posts] == [1, 1, 1]


def test_batch_request_carries_benchmark_and_vintage_and_picks_endpoint():
    fake = FakeCensus()
    c = client(fake)
    c.geocode_batch(records(2), c.resolve_context(), include_geographies=True)
    c.geocode_batch(records(2), c.resolve_context(), include_geographies=False)
    geo, loc = fake.posts
    assert geo["url"] == C.CENSUS_BATCH_GEOGRAPHIES_URL
    assert geo["data"] == {"benchmark": "Public_AR_Current", "vintage": "Current_Current"}
    assert loc["url"] == C.CENSUS_BATCH_LOCATIONS_URL and loc["data"] == {"benchmark": "Public_AR_Current"}


def test_failed_chunk_does_not_stop_later_chunks():
    fake = FakeCensus(fail_batches={2 + 3 + 3 + 3})   # POST #1 fine; calls 2.. are retries of batch 2
    c = client(fake, max_retries=2)
    # batch 1 = POST 1; batch 2 = POSTs 2,3,4 (all time out after retries); batch 3 = POST 5
    fake.fail_batches = {2, 3, 4}
    results, log = c.geocode_batch(records(30), c.resolve_context(), chunk_size=10)
    assert [e["status"] for e in log] == ["OK", "FAILED", "OK"]
    assert "did not respond" in log[1]["message"]
    assert len(results) == 20 and "15" not in results and "5" in results and "25" in results


def test_unexpected_ids_in_response_are_ignored_and_missing_ids_reported():
    class Odd(FakeCensus):
        def post(self, url, data=None, files=None, timeout=None):
            r = super().post(url, data, files, timeout)
            lines = r.text.strip().split("\n")[1:]                       # drop one real row
            lines.append('"999","ghost","Match","Exact","m","-90,30","1","L"')
            return Resp(text="\n".join(lines) + "\n")
    c = client(Odd())
    results, log = c.geocode_batch(records(5), c.resolve_context(), chunk_size=5)
    assert "999" not in results and log[0]["returned"] == 4 and log[0]["missing"] == 1


def test_progress_callback_reports_stages():
    events = []
    c = client(FakeCensus())
    c.geocode_batch(records(4), c.resolve_context(), chunk_size=2,
                    progress=lambda s, f, d: events.append((s, round(f, 2))))
    assert events[0] == ("Submitting batch", 0.0)
    assert events[-1] == ("Parsing results", 1.0)


# --------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------
def run(df, fake=None, **opts):
    return run_geocoding(df, MAPPING, PipelineOptions(**opts), geocoder=client(fake or FakeCensus()))


def test_pipeline_joins_on_row_key_not_order_even_with_duplicate_location_ids():
    df = sov([["A", "10 Oak St", "New Orleans", "LA", "70112"],
              ["A", "20 Oak St", "New Orleans", "LA", "70112"],      # duplicate Location ID
              ["B", "30 Oak St", "New Orleans", "LA", "70112"]])
    res = run(df)
    assert list(res.geo["Latitude"]) == pytest.approx([30 + 1e-6, 30 + 2e-6, 30 + 3e-6])
    assert list(res.geo["GeocodeRowID"]) == ["1", "2", "3"]


def test_location_id_is_never_sent_to_census():
    fake = FakeCensus()
    run(sov([["SECRET-LOC-77", "10 Oak St", "New Orleans", "LA", "70112"]]), fake)
    assert fake.posts[0]["first_row"][0] == "1"


def test_originals_are_untouched_and_exports_keep_every_column_first():
    df = sov([["A", " 10  Oak St ", "New Orleans", "Louisiana", "70112"]])
    before = df.copy(deep=True)
    res = run(df)
    pd.testing.assert_frame_equal(df, before)
    ex = res.export_frame()
    assert list(ex.columns[:5]) == list(df.columns)
    assert ex.loc[0, "Address"] == " 10  Oak St "                    # original text preserved
    assert ex.loc[0, "CleanStreet"] == "10 Oak St" and ex.loc[0, "CleanState"] == "LA"
    assert list(ex.columns[5:]) == GEO_COLUMNS


def test_existing_columns_with_appended_names_are_not_overwritten():
    df = sov([["A", "10 Oak St", "New Orleans", "LA", "70112"]])
    df["Latitude"] = "ORIGINAL-LAT"
    res = run(df)
    ex = res.export_frame()
    assert ex.loc[0, "Latitude"] == "ORIGINAL-LAT"
    assert ex.loc[0, "Latitude_Geocode"] == pytest.approx(30 + 1e-6)
    assert res.column_collisions() == {"Latitude": "Latitude_Geocode"}


def test_dedupe_submits_unique_addresses_once_and_fans_results_back():
    rows = [[f"L{i}", "10 Oak St", "New Orleans", "LA", "70112"] for i in range(5)]
    rows.append(["L9", "99 Other St", "New Orleans", "LA", "70112"])
    fake = FakeCensus()
    res = run(sov(rows), fake, dedupe_addresses=True)
    assert fake.posts[0]["n"] == 2 and res.unique_addresses_submitted == 2
    assert res.geo["QAStatus"].tolist().count(C.QA_HIGH) == 6
    assert len(set(res.geo["Latitude"][:5])) == 1               # shared result
    assert res.geo["Latitude"].notna().all()

    fake2 = FakeCensus()
    run(sov(rows), fake2, dedupe_addresses=False)
    assert fake2.posts[0]["n"] == 6


def test_outcomes_tie_nomatch_nonexact():
    df = sov([["1", "1 NOMATCH Rd", "New Orleans", "LA", "70112"],
              ["2", "2 TIE Rd", "New Orleans", "LA", "70112"],
              ["3", "3 NONEXACT Rd", "New Orleans", "LA", "70112"],
              ["4", "4 Good Rd", "New Orleans", "LA", "70112"]])
    g = run(df).geo
    assert g["QAStatus"].tolist() == [C.QA_FAILED, C.QA_REVIEW, C.QA_MEDIUM, C.QA_HIGH]
    assert g["GeocodeResolution"].tolist() == [C.RES_NONE, C.RES_NONE, C.RES_ADDRESS, C.RES_ADDRESS]
    assert g["Latitude"].isna().tolist() == [True, True, False, False]


def test_routing_out_of_scope_fallback_and_insufficient():
    df = sov([["1", "1 Calle Sol", "San Juan", "PR", "00901"],      # out of scope (state)
              ["2", "2 Calle Luna", "", "", "00918"],               # out of scope (PR ZIP, no state)
              ["3", "", "Austin", "TX", "78701"],                   # no street -> ZIP centroid
              ["4", "PO Box 5", "Denver", "CO", ""],                # PO Box -> city centroid
              ["5", "5 Main St", "", "IL", ""],                     # street but insufficient
              ["6", "", "", "", ""]])                               # nothing
    fake = FakeCensus()
    g = run(df, fake).geo
    assert g["GeocodeRoute"].tolist() == [C.ROUTE_OUT_OF_SCOPE, C.ROUTE_OUT_OF_SCOPE, C.ROUTE_FALLBACK,
                                          C.ROUTE_FALLBACK, C.ROUTE_NOT_SUBMITTED, C.ROUTE_NOT_SUBMITTED]
    assert g["GeocodeResolution"].tolist() == [C.RES_NONE, C.RES_NONE, C.RES_ZIP, C.RES_CITY, C.RES_NONE, C.RES_NONE]
    assert g["QAStatus"].tolist() == [C.QA_OUT_OF_SCOPE, C.QA_OUT_OF_SCOPE, C.QA_FALLBACK, C.QA_FALLBACK,
                                      C.QA_FAILED, C.QA_FAILED]
    assert fake.posts == []                                          # nothing here is sent to Census
    assert g.loc[2, "Latitude"] == pytest.approx(30.27, abs=0.2)     # Austin ZCTA centroid
    assert g.loc[2, "CoordinateMethod"] == C.FALLBACK_COORDINATE_METHOD
    assert g.loc[2, "GeocodeMethod"] == C.FALLBACK_METHOD


def test_submit_insufficient_override_sends_street_only_rows():
    df = sov([["1", "5 Main St", "", "IL", ""]])
    fake = FakeCensus()
    g = run(df, fake, submit_insufficient=True).geo
    assert fake.posts[0]["n"] == 1 and g.loc[0, "GeocodeRoute"] == C.ROUTE_CENSUS


def test_county_fallback_uses_county_when_nothing_else_is_available():
    df = sov([["1", "", "", "IL", ""]])
    df["County"] = "Cook County"
    mapping = dict(MAPPING, county="County")
    g = run_geocoding(df, mapping, PipelineOptions(), geocoder=client(FakeCensus())).geo
    assert g.loc[0, "GeocodeResolution"] == C.RES_COUNTY and g.loc[0, "CensusGeographyLevel"] == "COUNTY"


def test_zip_that_conflicts_with_state_is_rejected_for_fallback():
    df = sov([["1", "", "", "TX", "02134"]])                         # ZIP 02134 is Massachusetts
    g = run(df).geo
    assert g.loc[0, "QAStatus"] == C.QA_FAILED
    assert "belongs to MA" in g.loc[0, "QAReason"]


def test_fallback_for_failed_toggle_only_changes_nomatch_and_tie_rows():
    df = sov([["1", "1 NOMATCH Rd", "New Orleans", "LA", "70112"],
              ["2", "2 Good Rd", "New Orleans", "LA", "70112"]])
    off = run(df).geo
    on = run(df, fallback_for_failed=True).geo
    assert off.loc[0, "QAStatus"] == C.QA_FAILED
    assert on.loc[0, "QAStatus"] == C.QA_FALLBACK and "No_Match" in on.loc[0, "QAReason"]
    assert on.loc[0, "CensusMatchIndicator"] == "No_Match"          # Census outcome still recorded
    assert on.loc[1, "QAStatus"] == C.QA_HIGH


def test_failed_batch_marks_only_its_records_and_run_completes():
    df = sov([[str(i), f"{i} Oak St", "New Orleans", "LA", "70112"] for i in range(1, 7)])
    fake = FakeCensus(fail_batches={2, 3, 4})
    res = run_geocoding(df, MAPPING, PipelineOptions(chunk_size=3),
                        geocoder=client(fake, max_retries=2))
    q = res.geo["QAStatus"].tolist()
    assert q[:3] == [C.QA_HIGH] * 3 and q[3:] == [C.QA_FAILED] * 3
    assert "No result was returned" in res.geo.loc[3, "QAReason"]
    assert any("failed" in w for w in res.warnings)
    assert [e["status"] for e in res.chunk_log] == ["OK", "FAILED"]


def test_census_unreachable_does_not_crash():
    df = sov([["1", "10 Oak St", "New Orleans", "LA", "70112"],
              ["2", "", "Austin", "TX", "78701"]])
    res = run(df, FakeCensus(benchmarks_down=True))
    assert res.geo.loc[0, "QAStatus"] == C.QA_FAILED
    assert res.geo.loc[1, "QAStatus"] == C.QA_FALLBACK              # local fallback still works
    assert res.context is None and res.warnings


def test_audit_fields_capture_actual_benchmark_vintage_and_timestamp():
    g = run(sov([["1", "10 Oak St", "New Orleans", "LA", "70112"]])).geo
    r = g.iloc[0]
    assert (r.GeocodeProvider, r.GeocodeBenchmark, r.GeocodeVintage) == (
        "US Census Bureau", "Public_AR_Current", "Current_Current")
    assert r.GeocodeBenchmarkID == "4" and r.GeocodeMethod == "MAF/TIGER Address Range"
    assert r.GeocodeTimestamp and r.GeocodeBatchNumber == 1


def test_geographies_are_null_not_errors_when_missing():
    g = run(sov([["1", "10 Oak St", "New Orleans", "LA", "70112"]]), FakeCensus(geographies_missing=True),
            include_geographies=True).geo
    assert g.loc[0, "QAStatus"] == C.QA_HIGH
    assert g.loc[0, "CensusBlock"] is None and g.loc[0, "CensusGeographyLevel"] is None


def test_geography_level_and_fips_present_when_returned():
    g = run(sov([["1", "10 Oak St", "New Orleans", "LA", "70112"]])).geo
    r = g.iloc[0]
    assert (r.StateFIPS, r.CountyFIPS, r.CensusTract, r.CensusBlock) == ("22", "071", "000100", "1001")
    assert r.CensusGeographyLevel == "BLOCK"


def test_subset_masks_follow_the_agreed_definitions():
    df = sov([["1", "1 NOMATCH Rd", "New Orleans", "LA", "70112"],  # failed
              ["2", "2 Good Rd", "New Orleans", "LA", "70112"],     # high
              ["3", "", "Austin", "TX", "78701"],                   # fallback
              ["4", "4 NONEXACT Rd", "New Orleans", "LA", "70112"], # medium
              ["5", "5 Rd", "San Juan", "PR", "00901"]])            # out of scope
    res = run(df)
    assert res.mask_geocoded().tolist() == [False, True, False, True, False]
    assert res.mask_fallback().tolist() == [False, False, True, False, False]
    assert res.mask_exceptions().tolist() == [True, False, True, True, True]
    assert res.mask_failed().tolist() == [True, False, False, False, True]
    assert len(res.export_frame(res.mask_geocoded())) == 2


# --------------------------------------------------------------------------------------
# Summary / dashboard numbers
# --------------------------------------------------------------------------------------
def test_summary_outcomes_are_mutually_exclusive_and_sum_to_total():
    df = sov([["1", "1 NOMATCH Rd", "New Orleans", "LA", "70112"],
              ["2", "2 TIE Rd", "New Orleans", "LA", "70112"],
              ["3", "3 Good Rd", "New Orleans", "LA", "70112"],
              ["4", "4 NONEXACT Rd", "New Orleans", "LA", "70112"],
              ["5", "", "Austin", "TX", "78701"],
              ["6", "6 Rd", "San Juan", "PR", "00901"],
              ["7", "", "", "", ""]])
    s = summarize(run(df).geo)
    o = s["outcomes"]
    assert o["Total Locations"]["count"] == 7
    parts = ["Successfully Geocoded", "Tie", "No Match", "Fallback (area centroid)", "Out of Scope",
             "Other Failed / Not Submitted"]
    assert sum(o[p]["count"] for p in parts) == 7
    assert o["Exact Matches"]["count"] + o["Non-Exact Matches"]["count"] == o["Successfully Geocoded"]["count"]
    assert o["Successfully Geocoded"]["pct"] == pytest.approx(100 * 2 / 7, abs=0.1)
    assert sum(v["count"] for v in s["resolution"].values()) == 7
    assert sum(v["count"] for v in s["qa"].values()) == 7


def test_summary_handles_empty_frame():
    s = summarize(pd.DataFrame({c: [] for c in GEO_COLUMNS}))
    assert s["total"] == 0 and s["outcomes"]["Successfully Geocoded"]["pct"] == 0.0
