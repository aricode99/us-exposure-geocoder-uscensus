"""Application-wide constants for the US Exposure Geocoding & QA Tool.

Only values that are shared across modules live here. Nothing in this file
may contain exposure data.
"""

# ---------------------------------------------------------------------------
# Census Geocoder endpoints
# Official docs: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html
# ---------------------------------------------------------------------------
CENSUS_BASE_URL = "https://geocoding.geo.census.gov/geocoder"
CENSUS_BENCHMARKS_URL = f"{CENSUS_BASE_URL}/benchmarks"
CENSUS_VINTAGES_URL = f"{CENSUS_BASE_URL}/vintages"
CENSUS_SINGLE_LOCATIONS_URL = f"{CENSUS_BASE_URL}/locations/address"
CENSUS_SINGLE_GEOGRAPHIES_URL = f"{CENSUS_BASE_URL}/geographies/address"

# Names are resolved to IDs against the live /benchmarks and /vintages
# endpoints at run time. IDs are never hardcoded because "Current" is a
# moving alias.
DEFAULT_BENCHMARK_NAME = "Public_AR_Current"
DEFAULT_VINTAGE_NAME = "Current_Current"

# ---------------------------------------------------------------------------
# Batch settings (used from Phase 3)
# ---------------------------------------------------------------------------
BATCH_MAX_RECORDS = 10_000          # Census documented upper limit ("currently")
DEFAULT_BATCH_SIZE = 500            # Default chunk size; configurable up to BATCH_MAX_RECORDS

# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
CONNECT_TIMEOUT_S = 10
SINGLE_READ_TIMEOUT_S = 30
MAX_RETRIES = 3                     # retries AFTER the first attempt
BACKOFF_BASE_S = 1.0                # sleep = base * 2 ** retry_number
# Transient conditions only. Permanent outcomes (No_Match, 4xx) are never retried.
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# ---------------------------------------------------------------------------
# Result status labels (single-address mode)
# ---------------------------------------------------------------------------
STATUS_MATCH = "MATCH"
STATUS_NO_MATCH = "NO_MATCH"
STATUS_TIE = "TIE"
STATUS_INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"
STATUS_ERROR = "ERROR"

# Census wording for CensusMatchIndicator (matches batch output wording)
MATCH_INDICATOR = {
    STATUS_MATCH: "Match",
    STATUS_NO_MATCH: "No_Match",
    STATUS_TIE: "Tie",
}

# ---------------------------------------------------------------------------
# Audit / provenance labels
# ---------------------------------------------------------------------------
GEOCODE_PROVIDER = "US Census Bureau"
GEOCODE_SOURCE = "US Census MAF/TIGER"
GEOCODE_METHOD = "MAF/TIGER Address Range"
COORDINATE_METHOD = "Address Range Interpolation"

# ---------------------------------------------------------------------------
# Scope decisions
# ---------------------------------------------------------------------------
# Puerto Rico and U.S. territories are out of scope for v1.
OUT_OF_SCOPE_STATES = frozenset({"PR", "VI", "GU", "AS", "MP"})

# ---------------------------------------------------------------------------
# Batch endpoints / timeouts (verified against the live service, Oct 2026)
# Batch output is a HEADERLESS, fully quoted CSV:
#   Match:    id, input, "Match", "Exact|Non_Exact", matched, "lon,lat", tigerId, side
#             [+ stateFIPS, countyFIPS, tract, block when returntype=geographies]
#   No match: id, input, "No_Match"        (3 columns only)
#   Tie:      id, input, "Tie"             (3 columns only)
# ---------------------------------------------------------------------------
CENSUS_BATCH_LOCATIONS_URL = f"{CENSUS_BASE_URL}/locations/addressbatch"
CENSUS_BATCH_GEOGRAPHIES_URL = f"{CENSUS_BASE_URL}/geographies/addressbatch"
BATCH_READ_TIMEOUT_S = 300          # a transient hang was observed in live testing

# ---------------------------------------------------------------------------
# File handling limits
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = 200
MAX_ROWS = 250_000

# ---------------------------------------------------------------------------
# QA statuses and resolutions
# ---------------------------------------------------------------------------
QA_HIGH, QA_MEDIUM, QA_REVIEW, QA_FAILED = "HIGH", "MEDIUM", "REVIEW", "FAILED"
QA_FALLBACK, QA_OUT_OF_SCOPE = "FALLBACK", "OUT_OF_SCOPE"
QA_ORDER = [QA_HIGH, QA_MEDIUM, QA_REVIEW, QA_FAILED, QA_FALLBACK, QA_OUT_OF_SCOPE]

RES_ADDRESS = "ADDRESS_RANGE_INTERPOLATED"
RES_ZIP = "ZIP_CENTROID"
RES_CITY = "CITY_CENTROID"
RES_COUNTY = "COUNTY_CENTROID"
RES_NONE = "NONE"
RESOLUTION_ORDER = [RES_ADDRESS, RES_ZIP, RES_CITY, RES_COUNTY, RES_NONE]

ROUTE_CENSUS = "CENSUS"
ROUTE_FALLBACK = "FALLBACK"
ROUTE_OUT_OF_SCOPE = "OUT_OF_SCOPE"
ROUTE_NOT_SUBMITTED = "NOT_SUBMITTED"

FALLBACK_SOURCE = "US Census Gazetteer"
FALLBACK_COORDINATE_METHOD = "Area Centroid (not a site location)"
FALLBACK_METHOD = "Gazetteer Internal Point"

# ---------------------------------------------------------------------------
# Disclaimers (shown in the UI and README)
# ---------------------------------------------------------------------------
QA_DISCLAIMER = (
    "QAStatus and QAReason are generated by this application by comparing the cleaned input "
    "address with the address Census matched. They are NOT an official Census confidence "
    "score; the Census Geocoder does not publish a numeric confidence value."
)
CAT_DISCLAIMER_TITLE = "Census coordinates are interpolated, not rooftop locations"
CAT_DISCLAIMER = (
    "Geocode Source = US Census MAF/TIGER. Coordinate Method = Address Range Interpolation. "
    "A successful Census geocode places a point along a TIGER street address range. It must NOT "
    "be interpreted as a rooftop, building centroid or parcel centroid. Consider higher-precision "
    "geocoding where small coordinate differences can materially change results, for example: "
    "flood, storm surge, wildfire, coastal proximity and other localized hazards."
)
FALLBACK_DISCLAIMER = (
    "FALLBACK locations use the centroid of a ZIP Code Tabulation Area, place or county. "
    "These are area centroids, can be kilometres from the true site, and are not suitable for "
    "site-level hazard modelling."
)

# ---------------------------------------------------------------------------
# USPS state codes and names
# ---------------------------------------------------------------------------
_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # Out of scope for v1, but recognised so they can be flagged correctly
    "puerto rico": "PR", "virgin islands": "VI", "u.s. virgin islands": "VI",
    "us virgin islands": "VI", "guam": "GU", "american samoa": "AS",
    "northern mariana islands": "MP",
}
STATE_NAME_TO_ABBR = dict(_STATES)
# Common non-standard but unambiguous spellings
STATE_NAME_TO_ABBR.update({"washington dc": "DC", "washington d.c.": "DC", "d.c.": "DC",
                           "dc": "DC", "district of columbia (dc)": "DC"})
VALID_STATE_CODES = frozenset(_STATES.values())
