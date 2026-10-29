"""Phase 2 tests: Census single-address parsing and client behaviour.

All HTTP is mocked - these tests never call the Census service.
"""
import json

import pytest
import requests

from modules.census_geocoder import (
    CensusGeocoder,
    CensusRequestError,
    CensusResponseError,
    parse_geographies,
    parse_single_response,
)
from utils import constants as C

# --------------------------------------------------------------------------
# Fixtures: payloads modelled on the official documentation example
# --------------------------------------------------------------------------
MATCH_PAYLOAD = {
    "result": {
        "input": {
            "address": {"address": "4600 Silver Hill Rd, Washington, DC 20233"},
            "benchmark": {"isDefault": True, "benchmarkDescription": "Public Address Ranges - Current Benchmark",
                          "id": "4", "benchmarkName": "Public_AR_Current"},
        },
        "addressMatches": [{
            "tigerLine": {"side": "L", "tigerLineId": "76355984"},
            "coordinates": {"x": -76.92748724230096, "y": 38.84601622386617},
            "addressComponents": {"zip": "20233", "streetName": "SILVER HILL", "city": "WASHINGTON",
                                  "fromAddress": "4600", "state": "DC", "suffixType": "RD",
                                  "toAddress": "4700"},
            "matchedAddress": "4600 SILVER HILL RD, WASHINGTON, DC, 20233",
        }],
    }
}

NO_MATCH_PAYLOAD = {"result": {"input": {}, "addressMatches": []}}

GEOGRAPHIES = {
    "States": [{"STATE": "24", "NAME": "Maryland"}],
    "Counties": [{"STATE": "24", "COUNTY": "033"}],
    "Census Tracts": [{"TRACT": "802405", "COUNTY": "033"}],
    "2020 Census Blocks": [{"BLOCK": "2007", "TRACT": "802405"}],
    "Census Block Groups": [{"BLKGRP": "2"}],
}

BENCHMARKS_PAYLOAD = {"benchmarks": [
    {"isDefault": True, "benchmarkDescription": "Public Address Ranges - Current Benchmark",
     "id": "4", "benchmarkName": "Public_AR_Current"},
    {"isDefault": False, "benchmarkDescription": "Public Address Ranges - Census 2020 Benchmark",
     "id": "2020", "benchmarkName": "Public_AR_Census2020"},
]}

VINTAGES_PAYLOAD = {"vintages": [
    {"isDefault": True, "id": "4", "vintageName": "Current_Current",
     "vintageDescription": "Current Vintage - Current Benchmark"},
    {"isDefault": False, "id": "420", "vintageName": "Census2020_Current",
     "vintageDescription": "Census2020 Vintage - Current Benchmark"},
]}


def tie_payload():
    two = json.loads(json.dumps(MATCH_PAYLOAD))
    second = json.loads(json.dumps(MATCH_PAYLOAD["result"]["addressMatches"][0]))
    second["coordinates"] = {"x": -76.93, "y": 38.85}
    two["result"]["addressMatches"].append(second)
    return two


# --------------------------------------------------------------------------
# Fake HTTP layer
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Returns queued responses in order; an Exception in the queue is raised."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(queue, **kw):
    sleeps = []
    session = FakeSession(queue)
    client = CensusGeocoder(session=session, sleep=sleeps.append, **kw)
    return client, session, sleeps


def resolver_responses():
    return [FakeResponse(payload=BENCHMARKS_PAYLOAD), FakeResponse(payload=VINTAGES_PAYLOAD)]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_parse_match_maps_fields_and_swaps_xy():
    res = parse_single_response(MATCH_PAYLOAD)
    assert res.status == C.STATUS_MATCH
    m = res.best
    assert m.longitude == pytest.approx(-76.92748724230096)   # x
    assert m.latitude == pytest.approx(38.84601622386617)     # y
    assert m.matched_address == "4600 SILVER HILL RD, WASHINGTON, DC, 20233"
    assert m.tigerline_id == "76355984"
    assert m.tigerline_side == "L"
    assert m.components["fromAddress"] == "4600"
    assert res.benchmark_reported["benchmarkName"] == "Public_AR_Current"


def test_parse_no_match_is_not_an_error():
    res = parse_single_response(NO_MATCH_PAYLOAD)
    assert res.status == C.STATUS_NO_MATCH
    assert res.best is None and res.matches == []


def test_parse_tie_returns_all_candidates_and_no_best():
    res = parse_single_response(tie_payload())
    assert res.status == C.STATUS_TIE
    assert len(res.candidates) == 2
    assert res.best is None
    assert res.to_record()["Latitude"] is None


@pytest.mark.parametrize("payload", [
    None, [], "text", {}, {"result": None}, {"result": "x"},
    {"result": {}}, {"result": {"addressMatches": "nope"}},
    {"result": {"addressMatches": ["not-a-dict"]}},
])
def test_parse_malformed_raises_response_error(payload):
    with pytest.raises(CensusResponseError):
        parse_single_response(payload)


def test_parse_errors_payload_raises_request_error():
    with pytest.raises(CensusRequestError):
        parse_single_response({"errors": ["Address Format Error"], "status": "400"})


@pytest.mark.parametrize("coords", [
    {}, {"x": None, "y": None}, {"x": "abc", "y": "def"},
    {"x": 500, "y": 38.8}, {"x": -76.9, "y": 95}, {"x": float("nan"), "y": 38.8},
])
def test_invalid_coordinates_become_none(coords):
    payload = json.loads(json.dumps(MATCH_PAYLOAD))
    payload["result"]["addressMatches"][0]["coordinates"] = coords
    m = parse_single_response(payload).matches[0]
    assert m.latitude is None or m.longitude is None


def test_parse_geographies_handles_vintage_specific_layer_names():
    g = parse_geographies(GEOGRAPHIES)
    assert (g.state_fips, g.county_fips, g.census_tract, g.census_block) == ("24", "033", "802405", "2007")


def test_parse_geographies_partial_and_empty_return_none_not_error():
    assert parse_geographies(None) is None
    assert parse_geographies({}) is None
    g = parse_geographies({"Counties": [{"STATE": "24", "COUNTY": "033"}]})
    assert g.county_fips == "033" and g.census_tract is None and g.census_block is None


# --------------------------------------------------------------------------
# Benchmark / vintage resolution (audit trail)
# --------------------------------------------------------------------------
def test_resolve_context_uses_live_ids_and_caches():
    client, session, _ = make_client(resolver_responses())
    ctx = client.resolve_context()
    assert (ctx.benchmark_id, ctx.benchmark_name) == ("4", "Public_AR_Current")
    assert (ctx.vintage_id, ctx.vintage_name) == ("4", "Current_Current")
    assert session.calls[1]["params"] == {"benchmark": "4"}
    client.resolve_context()                      # cached: no further HTTP
    assert len(session.calls) == 2


def test_resolve_context_unknown_benchmark_raises_with_options():
    client, _, _ = make_client([FakeResponse(payload=BENCHMARKS_PAYLOAD)])
    with pytest.raises(CensusRequestError, match="Public_AR_Current"):
        client.resolve_context(benchmark_name="Nope")


def test_audit_fields_are_captured_not_hardcoded():
    client, _, _ = make_client(resolver_responses())
    audit = client.resolve_context().audit_fields()
    assert audit["GeocodeBenchmark"] == "Public_AR_Current"
    assert audit["GeocodeBenchmarkID"] == "4"
    assert audit["GeocodeVintage"] == "Current_Current"
    assert audit["GeocodeProvider"] == "US Census Bureau"
    assert audit["GeocodeMethod"] == "MAF/TIGER Address Range"
    assert audit["GeocodeTimestamp"]


# --------------------------------------------------------------------------
# geocode_single: happy path and request construction
# --------------------------------------------------------------------------
def test_geocode_single_success_and_params():
    client, session, _ = make_client(resolver_responses() + [FakeResponse(payload=MATCH_PAYLOAD)])
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC", "20233-1234")
    assert res.status == C.STATUS_MATCH
    call = session.calls[-1]
    assert call["url"] == C.CENSUS_SINGLE_LOCATIONS_URL
    assert call["params"] == {"street": "4600 Silver Hill Rd", "city": "Washington", "state": "DC",
                              "zip": "20233", "benchmark": "Public_AR_Current", "format": "json"}
    rec = res.to_record()
    assert rec["CensusMatchIndicator"] == "Match"
    assert rec["CensusMatchType"] is None
    assert rec["GeocodeSource"] == "US Census MAF/TIGER"
    assert rec["CoordinateMethod"] == "Address Range Interpolation"
    assert rec["GeocodeBenchmark"] == "Public_AR_Current"


def test_geocode_single_with_geographies_uses_geographies_endpoint_and_vintage():
    payload = json.loads(json.dumps(MATCH_PAYLOAD))
    payload["result"]["addressMatches"][0]["geographies"] = GEOGRAPHIES
    client, session, _ = make_client(resolver_responses() + [FakeResponse(payload=payload)])
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC", include_geographies=True)
    call = session.calls[-1]
    assert call["url"] == C.CENSUS_SINGLE_GEOGRAPHIES_URL
    assert call["params"]["vintage"] == "Current_Current"
    rec = res.to_record()
    assert (rec["StateFIPS"], rec["CountyFIPS"], rec["CensusTract"], rec["CensusBlock"]) == (
        "24", "033", "802405", "2007")


def test_geocode_single_no_match_is_not_retried():
    client, session, sleeps = make_client(resolver_responses() + [FakeResponse(payload=NO_MATCH_PAYLOAD)])
    res = client.geocode_single("1 Nowhere Ln", "Atlantis", "ZZ")
    assert res.status == C.STATUS_NO_MATCH
    assert len(session.calls) == 3 and sleeps == []


def test_geocode_single_tie_status():
    client, _, _ = make_client(resolver_responses() + [FakeResponse(payload=tie_payload())])
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC")
    assert res.status == C.STATUS_TIE and len(res.candidates) == 2


# --------------------------------------------------------------------------
# Input checks
# --------------------------------------------------------------------------
def test_missing_street_never_calls_api():
    client, session, _ = make_client([])
    res = client.geocode_single("", "Washington", "DC", "20233")
    assert res.status == C.STATUS_INSUFFICIENT_INPUT and session.calls == []


def test_street_only_is_insufficient_unless_overridden():
    client, session, _ = make_client([])
    assert client.geocode_single("4600 Silver Hill Rd").status == C.STATUS_INSUFFICIENT_INPUT
    assert session.calls == []

    client, session, _ = make_client(resolver_responses() + [FakeResponse(payload=NO_MATCH_PAYLOAD)])
    res = client.geocode_single("4600 Silver Hill Rd", allow_insufficient=True)
    assert res.status == C.STATUS_NO_MATCH


def test_street_plus_zip_is_sufficient():
    assert CensusGeocoder.check_single_input("1 Main St", "", "", "20233") is None
    assert CensusGeocoder.check_single_input("1 Main St", "Town", "", "") is not None


# --------------------------------------------------------------------------
# Resilience: retries, timeouts, HTTP errors, empty / malformed bodies
# --------------------------------------------------------------------------
def test_retries_503_then_succeeds_with_exponential_backoff():
    queue = resolver_responses() + [FakeResponse(503), FakeResponse(502), FakeResponse(payload=MATCH_PAYLOAD)]
    client, session, sleeps = make_client(queue, backoff_base=1.0)
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC")
    assert res.status == C.STATUS_MATCH
    assert sleeps == [1.0, 2.0]


def test_timeout_exhausts_retries_and_returns_error_result():
    queue = resolver_responses() + [requests.Timeout("secret-url?street=123 Main")] * 4
    client, session, sleeps = make_client(queue, max_retries=3)
    res = client.geocode_single("123 Main St", "Town", "TX")
    assert res.status == C.STATUS_ERROR
    assert "did not respond" in res.message
    assert "123 Main" not in res.message                  # no address / URL leakage
    assert len(sleeps) == 3


def test_connection_error_returns_error_result():
    queue = resolver_responses() + [requests.ConnectionError("boom")] * 4
    client, _, _ = make_client(queue)
    res = client.geocode_single("123 Main St", "Town", "TX")
    assert res.status == C.STATUS_ERROR and "unavailable" in res.message


def test_http_400_is_permanent_and_not_retried():
    queue = resolver_responses() + [FakeResponse(400, payload={"errors": ["bad"]})]
    client, session, sleeps = make_client(queue)
    res = client.geocode_single("123 Main St", "Town", "TX")
    assert res.status == C.STATUS_ERROR and "rejected" in res.message
    assert sleeps == [] and len(session.calls) == 3


def test_http_404_not_retried():
    client, session, sleeps = make_client(resolver_responses() + [FakeResponse(404)])
    assert client.geocode_single("123 Main St", "Town", "TX").status == C.STATUS_ERROR
    assert sleeps == []


@pytest.mark.parametrize("bad_body", ["", "   ", "<html>Bad gateway</html>", "{not json"])
def test_empty_or_malformed_body_retried_then_error(bad_body):
    queue = resolver_responses() + [FakeResponse(200, text=bad_body)] * 4
    client, _, sleeps = make_client(queue, max_retries=3)
    res = client.geocode_single("123 Main St", "Town", "TX")
    assert res.status == C.STATUS_ERROR and "unreadable" in res.message
    assert len(sleeps) == 3


def test_transient_bad_body_recovers_on_retry():
    queue = resolver_responses() + [FakeResponse(200, text=""), FakeResponse(payload=MATCH_PAYLOAD)]
    client, _, sleeps = make_client(queue)
    assert client.geocode_single("4600 Silver Hill Rd", "Washington", "DC").status == C.STATUS_MATCH
    assert len(sleeps) == 1


def test_valid_json_with_wrong_shape_is_error_not_crash():
    queue = resolver_responses() + [FakeResponse(payload={"unexpected": True})]
    client, _, _ = make_client(queue)
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC")
    assert res.status == C.STATUS_ERROR


def test_benchmark_endpoint_down_returns_error_result():
    client, _, _ = make_client([FakeResponse(503)] * 4)
    res = client.geocode_single("4600 Silver Hill Rd", "Washington", "DC")
    assert res.status == C.STATUS_ERROR
