"""Match levels, confidence bands, street cleanup and analyst edits."""
import numpy as np
import pandas as pd
import pytest

from modules import edits
from modules.address_cleaner import clean_address_row, strip_embedded_locality
from modules.confidence import band_for, haversine_km
from modules.match_levels import match_level_for
from modules.pipeline import GEO_COLUMNS, PipelineOptions, run_geocoding, summarize
from tests.test_batch_and_pipeline import MAPPING, FakeCensus, client, sov
from utils import constants as C


def run(rows, fake=None, **opts):
    return run_geocoding(sov(rows), MAPPING, PipelineOptions(**opts), geocoder=client(fake or FakeCensus()))


ROWS = [
    ["1", "10 Oak St", "New Orleans", "LA", "70112"],            # Exact
    ["2", "20 NONEXACT Oak St", "New Orleans", "LA", "70112"],   # Non_Exact
    ["3", "NOMATCH St", "New Orleans", "LA", "70112"],           # No_Match
    ["4", "", "", "", "70112"],                                    # ZIP fallback
    ["5", "", "New Orleans", "LA", ""],                            # city fallback
    ["6", "1 Main St", "San Juan", "PR", "00901"],                 # out of scope
]


# ---- crosswalk -----------------------------------------------------------------------------
def test_match_level_table_matches_the_agreed_crosswalk():
    res = run(ROWS).geo.set_index("GeocodeRowID")
    got = {k: (res.loc[k, "GeoMatchLevelCode"], res.loc[k, "EnhancedGeoMatchLevelCode"], res.loc[k, "GeoMatchLevel"])
           for k in res.index}
    assert got["1"] == ("ADDR", "SEGI", "Exact Address")
    assert got["3"] == ("NONE", "NONE", "None")
    assert got["4"] == ("POST", "POST", "Postal Code centroid")
    assert got["5"] == ("CITY", "CITY", "City centroid")
    assert got["6"] == ("NONE", "NONE", "None")
    assert got["2"][:1] == ("RLXA",) and got["2"][2] == "Relaxed Address"


def test_county_centroid_level():
    r = {"GeocodeResolution": C.RES_COUNTY}
    assert match_level_for(r) == ("CNTY", "COUNTY", "County centroid")


def test_relaxed_match_splits_on_house_number():
    base = {"GeocodeResolution": C.RES_ADDRESS, "CensusMatchType": "Non_Exact"}
    same = dict(base, CleanStreet="12 Oak St", CensusMatchedAddress="12 OAK ST, X, LA, 70112")
    diff = dict(base, CleanStreet="12 Oak St", CensusMatchedAddress="40 OAK ST, X, LA, 70112")
    nonum = dict(base, CleanStreet="Oak St", CensusMatchedAddress="OAK ST, X, LA, 70112")
    assert match_level_for(same)[:2] == ("RLXA", "SEGI")
    assert match_level_for(diff)[:2] == ("RLXA", "STRI")
    assert match_level_for(nonum)[:2] == ("RLXA", "SEGI")


def test_new_columns_are_exported():
    res = run(ROWS)
    for col in ("GeoMatchLevelCode", "EnhancedGeoMatchLevelCode", "GeoMatchLevel", "GeocodeConfidence",
                "ConfidenceBasis", "AnalystEdit"):
        assert col in res.export_frame().columns
    assert all(c in GEO_COLUMNS for c in ("GeoMatchLevel", "GeocodeConfidence", "AnalystEditCount"))


# ---- confidence ------------------------------------------------------------------------------
def test_confidence_bands_follow_level_and_failure():
    g = run(ROWS).geo.set_index("GeocodeRowID")
    assert g.loc["1", "GeocodeConfidence"] in (C.CONF_HIGH, C.CONF_MODERATE)
    assert g.loc["3", "GeocodeConfidence"] == C.CONF_NONE
    assert g.loc["4", "GeocodeConfidence"] == C.CONF_LOW           # ZIP centroid
    assert g.loc["5", "GeocodeConfidence"] == C.CONF_VERY_LOW      # city centroid
    assert g.loc["6", "GeocodeConfidence"] == C.CONF_NONE
    assert g.loc["2", "GeocodeConfidence"] != C.CONF_HIGH           # relaxed never High


@pytest.mark.parametrize("score,band", [(95, "High"), (80, "High"), (79, "Moderate"), (60, "Moderate"),
                                        (59, "Low"), (35, "Low"), (34, "Very low"), (1, "Very low")])
def test_band_thresholds(score, band):
    assert band_for(score) == band


def test_far_from_zip_centroid_lowers_the_band():
    # fake Census puts every point near lat 30 / lon -90: ZIP 10001 (New York) is >1,000 km away
    res = run([["1", "10 Oak St", "New York", "NY", "10001"]])
    row = res.geo.iloc[0]
    assert "km from the ZIP centroid" in row["ConfidenceBasis"]
    assert row["GeocodeConfidence"] in (C.CONF_LOW, C.CONF_VERY_LOW)


def test_haversine_sanity():
    assert haversine_km(40.0, -75.0, 40.0, -75.0) == 0
    assert 110 < haversine_km(40.0, -75.0, 41.0, -75.0) < 112


def test_summary_has_level_and_confidence_distributions_that_sum_to_total():
    s = summarize(run(ROWS).geo)
    assert sum(v["count"] for v in s["match_levels"].values()) == s["total"]
    assert sum(v["count"] for v in s["confidence"].values()) == s["total"]
    assert sum(v["count"] for k, v in s["outcomes"].items() if k != "Total Locations"
               and k not in ("Exact Matches", "Non-Exact Matches")) == s["total"]


# ---- street cleanup ------------------------------------------------------------------------------
def test_embedded_locality_is_removed_from_street():
    s = "239 Deerfield Ct,St Helena Island,SC,Beaufort,29920, St Helena Island, SC, 29920"
    out = clean_address_row(s, "St Helena Island", "SC", "29920")
    assert out["CleanStreet"] == "239 Deerfield Ct"
    assert "STREET_LOCALITY_REMOVED" in out["CleanFlags"]


@pytest.mark.parametrize("street,city", [
    ("123 Main St, Apt 4B", "Springfield"),
    ("1 Main St, Suite 5", "Springfield"),
    ("123 Main St", "Springfield"),
    ("123 Main St, Springfield", "Springfield"),        # only the city: nothing to confirm, leave alone
])
def test_street_without_embedded_locality_is_unchanged(street, city):
    assert strip_embedded_locality(street, city, "IL", "62701") == street


def test_street_with_city_state_zip_suffix_is_trimmed():
    assert strip_embedded_locality("123 Main St, Springfield, IL 62701", "Springfield", "IL", "62701") == "123 Main St"


# ---- analyst edits -----------------------------------------------------------------------------------
def test_regeocode_updates_only_the_edited_row_and_logs_it():
    fake = FakeCensus()
    res = run(ROWS, fake)
    before = res.geo.copy()
    edit = pd.DataFrame([{"GeocodeRowID": "3", "Street": "30 Elm St", "City": "New Orleans", "State": "LA",
                          "ZIP": "70112", "Note": "typo fixed"}])
    new = edits.regeocode_rows(res, edit, geocoder=client(fake))
    g = new.geo.set_index("GeocodeRowID")
    assert g.loc["3", "QAStatus"] in (C.QA_HIGH, C.QA_MEDIUM, C.QA_REVIEW)
    assert g.loc["3", "AnalystEdit"] == C.EDIT_ADDRESS and g.loc["3", "AnalystEditCount"] == 1
    assert g.loc["3", "OriginalStreet"] == "NOMATCH St"                      # original preserved
    assert g.loc["3", "CleanStreet"] == "30 Elm St"
    other = [i for i in g.index if i != "3"]
    pd.testing.assert_frame_equal(g.loc[other].drop(columns=[]), before.set_index("GeocodeRowID").loc[other],
                                  check_dtype=False)
    log = edits.edit_log_frame(new)
    assert list(log["GeocodeRowID"]) == ["3"] and log.iloc[0]["Note"] == "typo fixed"
    assert "NOMATCH" in log.iloc[0]["Before"] and "30 Elm St" in log.iloc[0]["After"]
    assert res.edit_log == []                                                    # input result untouched


def test_second_edit_increments_count():
    fake = FakeCensus()
    res = run(ROWS, fake)
    e = pd.DataFrame([{"GeocodeRowID": "3", "Street": "30 Elm St", "City": "New Orleans", "State": "LA", "ZIP": "70112"}])
    r2 = edits.regeocode_rows(edits.regeocode_rows(res, e, geocoder=client(fake)), e, geocoder=client(fake))
    assert r2.geo.set_index("GeocodeRowID").loc["3", "AnalystEditCount"] == 2
    assert len(r2.edit_log) == 2


def test_manual_coordinates_are_labelled_and_never_counted_as_census():
    res = run(ROWS)
    ov = pd.DataFrame([{"GeocodeRowID": "3", "Latitude": 29.95, "Longitude": -90.07, "Note": "checked on street map"}])
    new, errors = edits.set_coordinates(res, ov)
    assert errors == {}
    row = new.geo.set_index("GeocodeRowID").loc["3"]
    assert (row["Latitude"], row["Longitude"]) == (29.95, -90.07)
    assert row["GeocodeResolution"] == C.RES_MANUAL and row["QAStatus"] == C.QA_MANUAL
    assert row["GeoMatchLevel"] == "Manual (analyst-set)" and row["GeocodeConfidence"] == C.CONF_MANUAL
    assert row["CensusTract"] is None and row["TigerLineID"] is None
    s = summarize(new.geo)
    assert s["outcomes"]["Analyst-set Coordinates"]["count"] == 1
    assert s["outcomes"]["Successfully Geocoded"]["count"] == summarize(res.geo)["outcomes"]["Successfully Geocoded"]["count"]
    assert not new.mask_geocoded().loc[row.name if False else new.geo.index[2]]
    assert new.mask_edited().sum() == 1


@pytest.mark.parametrize("lat,lon,note,msg", [
    ("abc", -90, "x", "numbers"),
    (29.9, -90.0, "", "reason"),
    (48.0, 2.0, "x", "outside"),            # Paris
])
def test_manual_coordinates_are_validated(lat, lon, note, msg):
    res = run(ROWS)
    new, errors = edits.set_coordinates(res, pd.DataFrame([{"GeocodeRowID": "3", "Latitude": lat,
                                                           "Longitude": lon, "Note": note}]))
    assert msg in errors["3"].lower()
    pd.testing.assert_frame_equal(new.geo, res.geo)
