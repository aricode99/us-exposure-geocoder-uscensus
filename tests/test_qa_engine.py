"""QA engine tests. Pure functions only: no network."""
import numpy as np
import pandas as pd
import pytest

from modules.geography import ReferenceData
from modules.qa_engine import (
    apply_qa,
    normalise_street,
    qa_row,
    split_matched_address,
)
from utils import constants as C

REF = ReferenceData(zcta={}, places={}, counties={}, state_bounds={"MD": (36.0, 40.0, -80.0, -74.0),
                                                                    "DC": (37.0, 40.0, -79.0, -75.0)})


def census_row(**kw):
    base = {
        "GeocodeRoute": C.ROUTE_CENSUS, "CleanFlags": "",
        "CleanStreet": "4600 Silver Hill Rd", "CleanCity": "Washington", "CleanState": "DC", "CleanZIP": "20233",
        "CensusMatchIndicator": "Match", "CensusMatchType": "Exact",
        "CensusMatchedAddress": "4600 SILVER HILL RD, WASHINGTON, DC, 20233",
        "Latitude": 38.845, "Longitude": -76.928, "TigerLineID": "657091557",
        "StateFIPS": "24", "CountyFIPS": "033", "CensusTract": "802405", "CensusBlock": "2004",
    }
    base.update(kw)
    return base


def qa(**kw):
    return qa_row(census_row(**kw), REF)


# ---- baseline ------------------------------------------------------------------------
def test_exact_match_is_high_and_interpolated():
    r = qa()
    assert r["QAStatus"] == C.QA_HIGH
    assert r["GeocodeResolution"] == C.RES_ADDRESS
    assert r["GeocodeSource"] == "US Census MAF/TIGER"
    assert r["CoordinateMethod"] == "Address Range Interpolation"
    assert r["CensusGeographyLevel"] == "BLOCK"


def test_non_exact_match_is_medium():
    assert qa(CensusMatchType="Non_Exact")["QAStatus"] == C.QA_MEDIUM


def test_tie_is_review_with_no_resolution():
    r = qa(CensusMatchIndicator="Tie", CensusMatchType=None, CensusMatchedAddress=None,
           Latitude=np.nan, Longitude=np.nan)
    assert r["QAStatus"] == C.QA_REVIEW and r["GeocodeResolution"] == C.RES_NONE
    assert "tie" in r["QAReason"].lower()


def test_no_match_is_failed():
    r = qa(CensusMatchIndicator="No_Match", CensusMatchType=None, CensusMatchedAddress=None,
           Latitude=np.nan, Longitude=np.nan)
    assert r["QAStatus"] == C.QA_FAILED and r["GeocodeResolution"] == C.RES_NONE


def test_no_result_for_record_is_failed():
    r = qa(CensusMatchIndicator=None, CensusMatchType=None, Latitude=np.nan, Longitude=np.nan)
    assert r["QAStatus"] == C.QA_FAILED
    assert "No result" in r["QAReason"]


def test_missing_coordinate_on_match_is_failed():
    r = qa(Latitude=np.nan, Longitude=np.nan)
    assert r["QAStatus"] == C.QA_FAILED and r["GeocodeResolution"] == C.RES_NONE


# ---- mismatch detection ------------------------------------------------------------------
def test_state_mismatch_forces_review_even_when_exact():
    r = qa(CleanState="MD")
    assert r["QAStatus"] == C.QA_REVIEW
    assert "State changed: MD -> DC" in r["QAReason"]


def test_zip_change_downgrades_one_level():
    r = qa(CleanZIP="20001")
    assert r["QAStatus"] == C.QA_MEDIUM and "ZIP changed: 20001 -> 20233" in r["QAReason"]


def test_zip_change_on_non_exact_becomes_review():
    assert qa(CensusMatchType="Non_Exact", CleanZIP="20001")["QAStatus"] == C.QA_REVIEW


def test_city_change_downgrades():
    r = qa(CleanCity="Baltimore")
    assert r["QAStatus"] == C.QA_MEDIUM and "City changed" in r["QAReason"]


def test_zip_and_city_both_changed_reaches_review():
    assert qa(CleanZIP="20001", CleanCity="Baltimore")["QAStatus"] == C.QA_REVIEW


def test_house_number_change_downgrades():
    r = qa(CleanStreet="4800 Silver Hill Rd")
    assert r["QAStatus"] == C.QA_MEDIUM and "House number changed: 4800 -> 4600" in r["QAReason"]


def test_street_name_substantially_changed_downgrades():
    r = qa(CleanStreet="4600 Pennsylvania Ave")
    assert r["QAStatus"] == C.QA_MEDIUM and "Street name substantially changed" in r["QAReason"]


def test_abbreviation_and_unit_differences_are_not_mismatches():
    r = qa(CleanStreet="4600 Silver Hill Road, Suite 200",
           CensusMatchedAddress="4600 SILVER HILL RD, WASHINGTON, DC, 20233")
    assert r["QAStatus"] == C.QA_HIGH


def test_census_rewrites_ordinal_names_without_penalty():
    r = qa(CleanStreet="350 Fifth Avenue", CleanCity="New York", CleanState="NY", CleanZIP="10118",
           CensusMatchedAddress="350 5TH AVE, NEW YORK, NY, 10118")
    assert r["QAStatus"] == C.QA_HIGH


def test_coordinate_outside_state_forces_review():
    r = qa(Latitude=29.7, Longitude=-95.3)          # Houston coordinates, DC address
    assert r["QAStatus"] == C.QA_REVIEW and "outside the expected area" in r["QAReason"]


def test_ignored_invalid_input_is_reported():
    r = qa(CleanState="", CleanFlags="INVALID_STATE")
    assert "state was not recognised" in r["QAReason"]


def test_never_below_review_from_address_comparison():
    r = qa(CensusMatchType="Non_Exact", CleanZIP="20001", CleanCity="Baltimore",
           CleanStreet="1 Other Blvd")
    assert r["QAStatus"] == C.QA_REVIEW


# ---- non-Census routes ----------------------------------------------------------------------
def test_fallback_is_never_high_or_medium_and_labelled_as_area_centroid():
    r = qa_row({"GeocodeRoute": C.ROUTE_FALLBACK, "FallbackResolution": C.RES_ZIP,
                "FallbackBasis": "No usable street; ZCTA 78701 centroid used",
                "FallbackGeographyLevel": "ZCTA", "RouteNote": "No street address"}, REF)
    assert r["QAStatus"] == C.QA_FALLBACK and r["GeocodeResolution"] == C.RES_ZIP
    assert r["CoordinateMethod"] == C.FALLBACK_COORDINATE_METHOD
    assert r["GeocodeSource"] == C.FALLBACK_SOURCE
    assert "not a site location" in r["QAReason"]


def test_out_of_scope_row():
    r = qa_row({"GeocodeRoute": C.ROUTE_OUT_OF_SCOPE, "RouteNote": ""}, REF)
    assert r["QAStatus"] == C.QA_OUT_OF_SCOPE and r["GeocodeResolution"] == C.RES_NONE


def test_not_submitted_row_is_failed_with_reason():
    r = qa_row({"GeocodeRoute": C.ROUTE_NOT_SUBMITTED, "RouteNote": "No street address; no usable fallback"}, REF)
    assert r["QAStatus"] == C.QA_FAILED and "no usable fallback" in r["QAReason"]


# ---- helpers ----------------------------------------------------------------------------------
def test_split_matched_address():
    assert split_matched_address("4600 SILVER HILL RD, WASHINGTON, DC, 20233") == {
        "street": "4600 SILVER HILL RD", "city": "WASHINGTON", "state": "DC", "zip": "20233"}
    assert split_matched_address(None)["street"] == ""


def test_normalise_street_drops_units_and_standardises_words():
    assert normalise_street("123 North Main Street Apt 4B") == "123 N MAIN ST"
    assert normalise_street("350 Fifth Avenue #12") == "350 5TH AVE"


def test_apply_qa_keeps_index_and_columns():
    df = pd.DataFrame([census_row(), census_row(CleanState="MD")], index=[10, 20])
    out = apply_qa(df, REF)
    assert list(out.index) == [10, 20]
    assert list(out["QAStatus"]) == [C.QA_HIGH, C.QA_REVIEW]
