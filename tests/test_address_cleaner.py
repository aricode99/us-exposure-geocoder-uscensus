"""Address cleaning tests: conservative normalisation only."""
import numpy as np
import pandas as pd
import pytest

from modules.address_cleaner import (
    clean_address_row,
    clean_dataframe,
    clean_state,
    clean_street,
    clean_zip,
)


# ---- whitespace / control characters ---------------------------------------
def test_trim_and_collapse_whitespace():
    assert clean_street("  123   Main   St  ")[0] == "123 Main St"


def test_control_characters_removed_and_newlines_become_spaces():
    out, flags = clean_street("123 Main\nSt\x00\x07\t Apt 4")
    assert out == "123 Main St Apt 4"
    assert "STREET_NORMALIZED" in flags


def test_non_breaking_and_zero_width_characters():
    assert clean_street("123\u00a0Main\u200b St")[0] == "123 Main St"


def test_typographic_artifacts_normalised():
    assert clean_street("O\u2019Brien \u2013 Plaza 5")[0] == "O'Brien - Plaza 5"


def test_trailing_separators_removed():
    assert clean_street("123 Main St,")[0] == "123 Main St"
    assert clean_street("123 Main St ; ")[0] == "123 Main St"


def test_valid_street_is_not_modified():
    street = "1600 Pennsylvania Ave NW Suite 200"
    out, flags = clean_street(street)
    assert out == street and flags == []


def test_no_abbreviation_expansion_or_case_change():
    assert clean_street("123 main street apt 4b")[0] == "123 main street apt 4b"


@pytest.mark.parametrize("value", [None, np.nan, pd.NA, "nan", "None", "NULL", ""])
def test_null_like_values_become_blank(value):
    assert clean_street(value)[0] == ""


def test_po_box_is_flagged_not_changed():
    for text in ("PO Box 123", "P.O. Box 9", "Post Office Box 55"):
        out, flags = clean_street(text)
        assert out == text and "PO_BOX" in flags
    assert "PO_BOX" not in clean_street("100 Boxwood Dr")[1]


# ---- ZIP preservation / restoration -----------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("02134", "02134"), ("20233", "20233"),
    ("2134", "02134"),            # Excel dropped the leading zero
    ("501", "00501"),
    (2134, "02134"), ("2134.0", "02134"),
    ("20233-1234", "20233"), ("202331234", "20233"),
    ("2134-5678", "02134"), ("21345678", "02134"),
    (" 20233 ", "20233"),
])
def test_zip_restoration_and_zip4(raw, expected):
    assert clean_zip(raw)[0] == expected


def test_zip_is_always_a_string():
    assert isinstance(clean_zip(2134)[0], str)


@pytest.mark.parametrize("raw", ["ABCDE", "123456", "12", "1-2", "9999999999"])
def test_invalid_zip_is_blanked_and_flagged(raw):
    out, flags = clean_zip(raw)
    assert out == "" and flags == ["INVALID_ZIP"]


def test_zip_flags_describe_changes():
    assert clean_zip("2134")[1] == ["ZIP_PADDED"]
    assert clean_zip("20233-1234")[1] == ["ZIP4_REMOVED"]
    assert clean_zip("20233")[1] == []


# ---- state normalisation ---------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("TX", "TX"), ("tx", "TX"), (" tx ", "TX"),
    ("Texas", "TX"), ("NEW YORK", "NY"), ("new  hampshire", "NH"),
    ("District of Columbia", "DC"), ("D.C.", "DC"), ("Washington DC", "DC"),
    ("Washington", "WA"), ("Puerto Rico", "PR"),
])
def test_state_normalisation(raw, expected):
    assert clean_state(raw)[0] == expected


@pytest.mark.parametrize("raw", ["ZZ", "Texass", "XX", "42", "Narnia"])
def test_unrecognised_state_blanked_and_flagged(raw):
    out, flags = clean_state(raw)
    assert out == "" and flags == ["INVALID_STATE"]


def test_blank_state_is_not_flagged():
    assert clean_state("") == ("", [])


# ---- whole-frame behaviour ---------------------------------------------------------------
def test_clean_dataframe_preserves_originals_and_input_frame():
    df = pd.DataFrame({
        "Addr": ["  12  Oak Ave ", "PO Box 5"],
        "Town": ["Boston", "Austin"],
        "St": ["Massachusetts", "tx"],
        "Zip": ["2134", "78701"],
    })
    before = df.copy(deep=True)
    out = clean_dataframe(df, "Addr", "Town", "St", "Zip")

    pd.testing.assert_frame_equal(df, before)                      # input untouched
    assert list(out["OriginalStreet"]) == ["  12  Oak Ave ", "PO Box 5"]
    assert list(out["OriginalState"]) == ["Massachusetts", "tx"]
    assert list(out["OriginalZIP"]) == ["2134", "78701"]
    assert list(out["CleanStreet"]) == ["12 Oak Ave", "PO Box 5"]
    assert list(out["CleanState"]) == ["MA", "TX"]
    assert list(out["CleanZIP"]) == ["02134", "78701"]
    assert "ZIP_PADDED" in out.loc[0, "CleanFlags"]


def test_unmapped_optional_columns_yield_blanks():
    df = pd.DataFrame({"Addr": ["1 Main St"]})
    out = clean_dataframe(df, "Addr")
    assert out.loc[0, "CleanCity"] == "" and out.loc[0, "CleanZIP"] == ""


def test_clean_address_row_county_passthrough():
    row = clean_address_row("", "", "IL", "", "  Cook   County ")
    assert row["CleanCounty"] == "Cook County"
