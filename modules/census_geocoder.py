"""U.S. Census Bureau Geocoder client (single-address and batch modes).

This module contains NO Streamlit code. It exposes:

* ``CensusGeocoder``         - HTTP client with timeouts, bounded retry/backoff
* ``parse_single_response``  - pure function: Census JSON -> ``SingleGeocodeResult``
* ``GeocodeContext``         - the benchmark/vintage actually used (audit trail)

Design notes
------------
* The Census service returns NO numeric confidence score. Quality signals are
  derived elsewhere (qa_engine, Phase 5).
* The single-address JSON does NOT include the Exact / Non_Exact match type
  (only the batch CSV does), so ``CensusMatchType`` is None in single mode.
* Coordinates are interpolated along TIGER address ranges. A match is not a
  rooftop, building or parcel location.
* Benchmark / vintage names such as ``Public_AR_Current`` are moving aliases.
  We resolve them against the live ``/benchmarks`` and ``/vintages`` endpoints
  and record the result for reproducibility.
* Privacy: never log addresses, URLs or ``requests`` exception text (which
  embeds the URL and query string).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from utils import constants as C

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class CensusGeocoderError(Exception):
    """Base class for all errors raised by this module."""


class CensusServiceError(CensusGeocoderError):
    """Network failure or HTTP error / service unavailable (possibly transient)."""


class CensusTimeoutError(CensusServiceError):
    """The request timed out after all retries."""


class CensusRequestError(CensusGeocoderError):
    """Permanent client-side problem (HTTP 4xx, 'errors' payload). Never retried."""


class CensusResponseError(CensusGeocoderError):
    """Response was empty, not JSON, or not shaped as documented."""


_USER_MESSAGES = {
    CensusTimeoutError: "The Census geocoder did not respond in time. Please try again shortly.",
    CensusServiceError: "The Census geocoder is unavailable or returned an HTTP error. Please try again later.",
    CensusRequestError: "The Census geocoder rejected the request. Please check the address fields.",
    CensusResponseError: "The Census geocoder returned an unreadable response. Please try again.",
}


def _user_message(exc: Exception) -> str:
    for cls in (CensusTimeoutError, CensusServiceError, CensusRequestError, CensusResponseError):
        if isinstance(exc, cls):
            return _USER_MESSAGES[cls]
    return "An unexpected error occurred while geocoding."


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BenchmarkInfo:
    id: str
    name: str
    description: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class VintageInfo:
    id: str
    name: str
    description: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class GeocodeContext:
    """Benchmark + vintage actually used for a run (audit / reproducibility)."""

    benchmark_id: str
    benchmark_name: str
    benchmark_description: str
    vintage_id: str
    vintage_name: str
    vintage_description: str
    resolved_at_utc: str

    def audit_fields(self, timestamp_utc: Optional[str] = None) -> Dict[str, Optional[str]]:
        return {
            "GeocodeProvider": C.GEOCODE_PROVIDER,
            "GeocodeBenchmark": self.benchmark_name,
            "GeocodeBenchmarkID": self.benchmark_id,
            "GeocodeBenchmarkDescription": self.benchmark_description,
            "GeocodeVintage": self.vintage_name,
            "GeocodeVintageID": self.vintage_id,
            "GeocodeTimestamp": timestamp_utc or self.resolved_at_utc,
            "GeocodeMethod": C.GEOCODE_METHOD,
        }


@dataclass(frozen=True)
class GeographyInfo:
    """Geography codes from the geoLookup. Any element may be None."""

    state_fips: Optional[str] = None
    county_fips: Optional[str] = None
    census_tract: Optional[str] = None
    census_block: Optional[str] = None


@dataclass(frozen=True)
class AddressMatch:
    matched_address: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    tigerline_id: Optional[str]
    tigerline_side: Optional[str]
    components: Dict[str, Any] = field(default_factory=dict)
    geography: Optional[GeographyInfo] = None


@dataclass
class SingleGeocodeResult:
    status: str                                   # C.STATUS_*
    matches: List[AddressMatch] = field(default_factory=list)
    message: str = ""                             # user-friendly
    request_echo: Dict[str, str] = field(default_factory=dict)
    benchmark_reported: Dict[str, Any] = field(default_factory=dict)  # as echoed by Census
    context: Optional[GeocodeContext] = None
    timestamp_utc: Optional[str] = None

    @property
    def best(self) -> Optional[AddressMatch]:
        """The single usable match. None for Tie / No_Match / errors."""
        return self.matches[0] if self.status == C.STATUS_MATCH and self.matches else None

    @property
    def candidates(self) -> List[AddressMatch]:
        """All returned matches (more than one when the result is a Tie)."""
        return list(self.matches)

    def to_record(self) -> Dict[str, Any]:
        """Flat dict using the output field names from the specification."""
        m = self.best
        geo = m.geography if m else None
        record: Dict[str, Any] = {
            "CensusMatchedAddress": m.matched_address if m else None,
            "Latitude": m.latitude if m else None,
            "Longitude": m.longitude if m else None,
            "CensusMatchIndicator": C.MATCH_INDICATOR.get(self.status),
            "CensusMatchType": None,   # not provided by the single-address endpoint
            "TigerLineID": m.tigerline_id if m else None,
            "TigerLineSide": m.tigerline_side if m else None,
            "StateFIPS": geo.state_fips if geo else None,
            "CountyFIPS": geo.county_fips if geo else None,
            "CensusTract": geo.census_tract if geo else None,
            "CensusBlock": geo.census_block if geo else None,
            "GeocodeSource": C.GEOCODE_SOURCE if m else None,
            "CoordinateMethod": C.COORDINATE_METHOD if m else None,
        }
        if self.context:
            record.update(self.context.audit_fields(self.timestamp_utc))
        return record


# ---------------------------------------------------------------------------
# Parsing (pure functions - fully unit-testable without network)
# ---------------------------------------------------------------------------
def _to_float(value: Any, lo: float, hi: float) -> Optional[float]:
    """Convert to a finite float within [lo, hi]; otherwise None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or not (lo <= f <= hi):
        return None
    return f


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _first_dict(items: Any) -> Optional[dict]:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _layer(geographies: dict, *, contains: str, excludes: Tuple[str, ...] = ()) -> Optional[dict]:
    """Find the first record of the layer whose name contains ``contains``.

    Layer names vary with vintage (e.g. "Census Blocks" vs "2020 Census Blocks"),
    so match case-insensitively on a fragment instead of an exact key.
    """
    for name, items in geographies.items():
        lowered = str(name).lower()
        if contains in lowered and not any(x in lowered for x in excludes):
            rec = _first_dict(items)
            if rec:
                return rec
    return None


def parse_geographies(geographies: Any) -> Optional[GeographyInfo]:
    """Extract State / County / Tract / Block codes; missing pieces stay None."""
    if not isinstance(geographies, dict) or not geographies:
        return None
    state = _layer(geographies, contains="state", excludes=("legislative", "congress"))
    county = _layer(geographies, contains="count", excludes=("subdivision",))
    tract = _layer(geographies, contains="tract")
    block = _layer(geographies, contains="block", excludes=("group",))

    info = GeographyInfo(
        state_fips=_clean_str((state or {}).get("STATE") or (county or {}).get("STATE")),
        county_fips=_clean_str((county or {}).get("COUNTY")),
        census_tract=_clean_str((tract or {}).get("TRACT")),
        census_block=_clean_str((block or {}).get("BLOCK")),
    )
    if not any((info.state_fips, info.county_fips, info.census_tract, info.census_block)):
        return None
    return info


def _parse_match(raw: Any) -> AddressMatch:
    if not isinstance(raw, dict):
        raise CensusResponseError("addressMatches contains a non-object entry")

    coords = raw.get("coordinates") if isinstance(raw.get("coordinates"), dict) else {}
    tiger = raw.get("tigerLine") if isinstance(raw.get("tigerLine"), dict) else {}
    comps = raw.get("addressComponents") if isinstance(raw.get("addressComponents"), dict) else {}

    return AddressMatch(
        matched_address=_clean_str(raw.get("matchedAddress")),
        # Census: x = longitude, y = latitude
        longitude=_to_float(coords.get("x"), -180.0, 180.0),
        latitude=_to_float(coords.get("y"), -90.0, 90.0),
        tigerline_id=_clean_str(tiger.get("tigerLineId")),
        tigerline_side=_clean_str(tiger.get("side")),
        components=dict(comps),
        geography=parse_geographies(raw.get("geographies")),
    )


def parse_single_response(payload: Any) -> SingleGeocodeResult:
    """Convert a decoded single-address JSON payload into a result object.

    Raises ``CensusRequestError`` for Census-reported input errors and
    ``CensusResponseError`` when the payload is not shaped as documented.
    A well-formed response with zero matches is NOT an error: it returns
    ``STATUS_NO_MATCH``.
    """
    if not isinstance(payload, dict):
        raise CensusResponseError("Response JSON is not an object")

    result = payload.get("result")
    if result is None and payload.get("errors"):
        errors = payload["errors"]
        text = "; ".join(map(str, errors)) if isinstance(errors, list) else str(errors)
        raise CensusRequestError(f"Census reported an input error: {text}")
    if not isinstance(result, dict):
        raise CensusResponseError("Response is missing the 'result' object")

    raw_matches = result.get("addressMatches")
    if not isinstance(raw_matches, list):
        raise CensusResponseError("Response is missing the 'addressMatches' list")

    matches = [_parse_match(m) for m in raw_matches]

    bench = {}
    inp = result.get("input")
    if isinstance(inp, dict) and isinstance(inp.get("benchmark"), dict):
        bench = dict(inp["benchmark"])

    if len(matches) == 0:
        status, message = C.STATUS_NO_MATCH, "Census found no match for this address."
    elif len(matches) == 1:
        status, message = C.STATUS_MATCH, "Census returned one match."
    else:
        status = C.STATUS_TIE
        message = (f"Census returned {len(matches)} candidate matches (tie). "
                   "No single location was selected; review the candidates.")

    return SingleGeocodeResult(status=status, matches=matches, message=message,
                               benchmark_reported=bench)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_ZIP5 = re.compile(r"^\s*(\d{5})(?:[-\s]?\d{4})?\s*$")


def _tidy_zip(zip_code: Any) -> str:
    """Single-mode convenience: reduce ZIP+4 to ZIP5; otherwise pass through.

    Full ZIP cleansing (zero restoration etc.) lives in address_cleaner (Phase 4).
    """
    s = "" if zip_code is None else str(zip_code).strip()
    m = _ZIP5.match(s)
    return m.group(1) if m else s


class CensusGeocoder:
    """Thin, testable client for the Census Geocoder REST service."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        connect_timeout: float = C.CONNECT_TIMEOUT_S,
        read_timeout: float = C.SINGLE_READ_TIMEOUT_S,
        max_retries: int = C.MAX_RETRIES,
        backoff_base: float = C.BACKOFF_BASE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = (connect_timeout, read_timeout)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._context_cache: Dict[Tuple[str, str], GeocodeContext] = {}

    # -- HTTP -----------------------------------------------------------------
    def _send(self, do_request: Callable[[], Any], validate: Callable[[str], Any]) -> Any:
        """Run a request with bounded retry on transient failures only.

        ``do_request`` performs the HTTP call; ``validate`` turns the response text
        into the parsed value (raising ValueError/CensusResponseError if unusable).

        Retried: timeouts, connection errors, HTTP 429/5xx, empty body, unusable body.
        Not retried: other HTTP 4xx (permanent) and valid answers such as No_Match.
        Never logs URLs, parameters or exception text (they contain address data).
        """
        last_exc: CensusGeocoderError = CensusServiceError("Request failed")

        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.backoff_base * (2 ** (attempt - 1))
                logger.info("Retrying Census request (retry %d/%d) in %.1fs",
                            attempt, self.max_retries, delay)
                self._sleep(delay)

            try:
                resp = do_request()
            except requests.Timeout:
                logger.warning("Census request timed out")
                last_exc = CensusTimeoutError("Census request timed out")
                continue
            except requests.ConnectionError:
                logger.warning("Census connection error")
                last_exc = CensusServiceError("Could not connect to the Census geocoder")
                continue
            except requests.RequestException as exc:
                # Do not chain: the original message contains the full URL.
                logger.error("Unexpected requests error: %s", type(exc).__name__)
                raise CensusServiceError("Unexpected network error") from None

            status = resp.status_code
            if status in C.RETRY_STATUS_CODES:
                logger.warning("Census returned HTTP %s", status)
                last_exc = CensusServiceError(f"Census returned HTTP {status}")
                continue
            if status >= 500:
                logger.error("Census returned HTTP %s (not retried)", status)
                raise CensusServiceError(f"Census returned HTTP {status}")
            if status >= 400:
                logger.error("Census rejected the request with HTTP %s", status)
                detail = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict) and body.get("errors"):
                        detail = ": " + "; ".join(map(str, body["errors"]))
                except ValueError:
                    pass
                raise CensusRequestError(f"Census rejected the request (HTTP {status}){detail}")

            text = getattr(resp, "text", "") or ""
            if not text.strip():
                logger.warning("Census returned an empty body")
                last_exc = CensusResponseError("Census returned an empty response")
                continue
            try:
                return validate(text)
            except (ValueError, CensusResponseError):
                logger.warning("Census returned an unusable body")
                last_exc = CensusResponseError("Census returned an unreadable response")
                continue

        raise last_exc

    def _get_json(self, url: str, params: Optional[dict] = None) -> Any:
        """GET and decode JSON (see ``_send`` for retry rules)."""
        return self._send(
            lambda: self.session.get(url, params=params, timeout=self.timeout),
            json.loads,
        )

    # -- Benchmarks / vintages ------------------------------------------------
    def list_benchmarks(self) -> List[BenchmarkInfo]:
        payload = self._get_json(C.CENSUS_BENCHMARKS_URL)
        items = payload.get("benchmarks") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise CensusResponseError("Benchmark list has an unexpected shape")
        out = []
        for b in items:
            if isinstance(b, dict) and b.get("id") is not None and b.get("benchmarkName"):
                out.append(BenchmarkInfo(
                    id=str(b["id"]), name=str(b["benchmarkName"]),
                    description=str(b.get("benchmarkDescription", "")),
                    is_default=bool(b.get("isDefault", False))))
        return out

    def list_vintages(self, benchmark_id: str) -> List[VintageInfo]:
        payload = self._get_json(C.CENSUS_VINTAGES_URL, params={"benchmark": benchmark_id})
        items = payload.get("vintages") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise CensusResponseError("Vintage list has an unexpected shape")
        out = []
        for v in items:
            if isinstance(v, dict) and v.get("id") is not None and v.get("vintageName"):
                out.append(VintageInfo(
                    id=str(v["id"]), name=str(v["vintageName"]),
                    description=str(v.get("vintageDescription", "")),
                    is_default=bool(v.get("isDefault", False))))
        return out

    def resolve_context(
        self,
        benchmark_name: str = C.DEFAULT_BENCHMARK_NAME,
        vintage_name: str = C.DEFAULT_VINTAGE_NAME,
        refresh: bool = False,
    ) -> GeocodeContext:
        """Resolve names to the IDs/descriptions the service currently publishes."""
        key = (benchmark_name.lower(), vintage_name.lower())
        if not refresh and key in self._context_cache:
            return self._context_cache[key]

        benchmarks = self.list_benchmarks()
        bench = next((b for b in benchmarks if b.name.lower() == benchmark_name.lower()), None)
        if bench is None:
            available = ", ".join(b.name for b in benchmarks) or "none"
            raise CensusRequestError(
                f"Benchmark '{benchmark_name}' is not offered by Census. Available: {available}")

        vintages = self.list_vintages(bench.id)
        vint = next((v for v in vintages if v.name.lower() == vintage_name.lower()), None)
        if vint is None:
            available = ", ".join(v.name for v in vintages) or "none"
            raise CensusRequestError(
                f"Vintage '{vintage_name}' is not offered for benchmark '{bench.name}'. "
                f"Available: {available}")

        ctx = GeocodeContext(
            benchmark_id=bench.id, benchmark_name=bench.name,
            benchmark_description=bench.description,
            vintage_id=vint.id, vintage_name=vint.name,
            vintage_description=vint.description,
            resolved_at_utc=_utc_now_iso(),
        )
        self._context_cache[key] = ctx
        return ctx

    # -- Single address ---------------------------------------------------------
    @staticmethod
    def check_single_input(street: str, city: str, state: str, zip_code: str) -> Optional[str]:
        """Return a user-facing problem description, or None if input is sufficient.

        Census minimum: street + ZIP, OR street + city + state.
        """
        if not street:
            return "A street address is required for Census geocoding."
        if zip_code or (city and state):
            return None
        return ("Census needs a street plus ZIP, or a street plus city and state. "
                "Add the missing fields for a reliable match.")

    def geocode_single(
        self,
        street: str,
        city: str = "",
        state: str = "",
        zip_code: str = "",
        include_geographies: bool = False,
        allow_insufficient: bool = False,
        benchmark_name: str = C.DEFAULT_BENCHMARK_NAME,
        vintage_name: str = C.DEFAULT_VINTAGE_NAME,
    ) -> SingleGeocodeResult:
        """Geocode one address. Never raises for service or data problems.

        Failures are returned as ``STATUS_ERROR`` with a user-friendly message;
        technical details go to the log (without address data).
        """
        street = (street or "").strip()
        city = (city or "").strip()
        state = (state or "").strip()
        zip_code = _tidy_zip(zip_code)
        echo = {"street": street, "city": city, "state": state, "zip": zip_code}

        problem = self.check_single_input(street, city, state, zip_code)
        if problem and (not street or not allow_insufficient):
            return SingleGeocodeResult(status=C.STATUS_INSUFFICIENT_INPUT,
                                       message=problem, request_echo=echo)

        try:
            ctx = self.resolve_context(benchmark_name, vintage_name)

            params = {"street": street, "benchmark": ctx.benchmark_name, "format": "json"}
            if city:
                params["city"] = city
            if state:
                params["state"] = state
            if zip_code:
                params["zip"] = zip_code
            if include_geographies:
                params["vintage"] = ctx.vintage_name
                url = C.CENSUS_SINGLE_GEOGRAPHIES_URL
            else:
                url = C.CENSUS_SINGLE_LOCATIONS_URL

            payload = self._get_json(url, params=params)
            result = parse_single_response(payload)
            return replace(result, request_echo=echo, context=ctx, timestamp_utc=_utc_now_iso())

        except CensusGeocoderError as exc:
            logger.error("Single geocode failed: %s", type(exc).__name__)
            return SingleGeocodeResult(status=C.STATUS_ERROR, message=_user_message(exc),
                                       request_echo=echo)
        except Exception as exc:  # last-resort guard: the UI must never crash
            logger.error("Unexpected error in geocode_single: %s", type(exc).__name__)
            return SingleGeocodeResult(status=C.STATUS_ERROR, message=_user_message(exc),
                                       request_echo=echo)

    # -- Batch ------------------------------------------------------------------
    def geocode_batch_chunk(
        self,
        records: Sequence[Tuple[str, str, str, str, str]],
        ctx: GeocodeContext,
        include_geographies: bool = True,
    ) -> Tuple[Dict[str, "BatchRow"], int]:
        """Submit ONE chunk (<= BATCH_MAX_RECORDS). Returns (rows_by_id, malformed_line_count).

        ``records`` are (id, street, city, state, zip) tuples. Results are keyed by the
        submitted ID; the order of the Census output is never relied upon.
        """
        if not records:
            return {}, 0
        if len(records) > C.BATCH_MAX_RECORDS:
            raise CensusRequestError(
                f"A single Census batch may contain at most {C.BATCH_MAX_RECORDS} records.")

        payload = build_batch_csv(records)
        url = C.CENSUS_BATCH_GEOGRAPHIES_URL if include_geographies else C.CENSUS_BATCH_LOCATIONS_URL
        data = {"benchmark": ctx.benchmark_name}
        if include_geographies:
            data["vintage"] = ctx.vintage_name
        timeout = (self.timeout[0], C.BATCH_READ_TIMEOUT_S)

        def do_request():
            return self.session.post(
                url, data=data, timeout=timeout,
                files={"addressFile": ("addresses.csv", payload, "text/csv")})

        def validate(text: str):
            rows, bad = parse_batch_output(text)
            if not rows:
                raise CensusResponseError("Batch response contained no parseable rows")
            return rows, bad

        return self._send(do_request, validate)

    def geocode_batch(
        self,
        records: Sequence[Tuple[str, str, str, str, str]],
        ctx: GeocodeContext,
        chunk_size: int = C.DEFAULT_BATCH_SIZE,
        include_geographies: bool = True,
        progress: Optional[Callable[[str, float, str], None]] = None,
    ) -> Tuple[Dict[str, "BatchRow"], List[Dict[str, Any]]]:
        """Geocode all records, splitting into sequential chunks.

        A failed chunk never aborts the run: its records simply have no result and
        the failure is described in the returned chunk log.
        """
        chunk_size = max(1, min(int(chunk_size), C.BATCH_MAX_RECORDS))
        chunks = [records[i:i + chunk_size] for i in range(0, len(records), chunk_size)]
        total = len(chunks)
        results: Dict[str, BatchRow] = {}
        log: List[Dict[str, Any]] = []

        for n, chunk in enumerate(chunks, start=1):
            if progress:
                progress("Submitting batch", (n - 1) / total, f"Batch {n} of {total} ({len(chunk)} records)")
            started = time.monotonic()
            entry: Dict[str, Any] = {"batch": n, "submitted": len(chunk), "returned": 0,
                                     "missing": len(chunk), "malformed_lines": 0,
                                     "status": "OK", "message": ""}
            try:
                rows, bad = self.geocode_batch_chunk(chunk, ctx, include_geographies)
                submitted_ids = {str(r[0]) for r in chunk}
                accepted = {k: v for k, v in rows.items() if k in submitted_ids}
                stamp = _utc_now_iso()
                for row in accepted.values():
                    row.batch_number = n
                    row.timestamp_utc = stamp
                results.update(accepted)
                entry.update(returned=len(accepted), missing=len(submitted_ids) - len(accepted),
                             malformed_lines=bad, timestamp_utc=stamp)
                if entry["missing"] or bad:
                    entry["message"] = (f"{entry['missing']} record(s) missing from the response; "
                                        f"{bad} unreadable line(s).")
            except CensusGeocoderError as exc:
                logger.error("Batch %d/%d failed: %s", n, total, type(exc).__name__)
                entry.update(status="FAILED", message=_user_message(exc))
            except Exception as exc:  # the run must continue with remaining chunks
                logger.error("Unexpected error in batch %d/%d: %s", n, total, type(exc).__name__)
                entry.update(status="FAILED", message=_user_message(exc))
            entry["seconds"] = round(time.monotonic() - started, 2)
            log.append(entry)
            if progress:
                progress("Parsing results", n / total, f"Batch {n} of {total} complete")

        return results, log


# ---------------------------------------------------------------------------
# Batch input / output (pure functions)
# ---------------------------------------------------------------------------
@dataclass
class BatchRow:
    """One parsed line of Census batch output."""

    row_id: str
    indicator: str                       # Match | No_Match | Tie
    match_type: Optional[str] = None     # Exact | Non_Exact (Match only)
    matched_address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    tiger_line_id: Optional[str] = None
    tiger_line_side: Optional[str] = None
    state_fips: Optional[str] = None
    county_fips: Optional[str] = None
    census_tract: Optional[str] = None
    census_block: Optional[str] = None
    batch_number: Optional[int] = None
    timestamp_utc: Optional[str] = None


_VALID_INDICATORS = {"Match", "No_Match", "Tie"}


def _flat(value: Any) -> str:
    """Single-line text: embedded newlines/tabs would corrupt the headerless CSV."""
    return re.sub(r"[\r\n\t]+", " ", "" if value is None else str(value)).strip()


def build_batch_csv(records: Iterable[Sequence[Any]]) -> bytes:
    """Build the Census batch input: ``ID, Street, City, State, ZIP`` with no header.

    Missing components keep their delimiter (empty field). ``csv`` quotes any field
    that contains a comma.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    for rid, street, city, state, zip_code in records:
        writer.writerow([_flat(rid), _flat(street), _flat(city), _flat(state), _flat(zip_code)])
    return buf.getvalue().encode("utf-8")


def _split_coordinates(value: str) -> Tuple[Optional[float], Optional[float]]:
    """Census batch returns one field ``"lon,lat"``. Returns (lat, lon)."""
    parts = [p.strip() for p in (value or "").split(",")]
    if len(parts) != 2:
        return None, None
    return _to_float(parts[1], -90.0, 90.0), _to_float(parts[0], -180.0, 180.0)


def parse_batch_output(text: str) -> Tuple[Dict[str, BatchRow], int]:
    """Parse the headerless batch CSV. Returns (rows keyed by submitted ID, malformed_count).

    Rows are keyed by ID, never by position. Short rows (No_Match / Tie have only
    three columns) and missing geography columns yield None, never an exception.
    """
    rows: Dict[str, BatchRow] = {}
    malformed = 0
    for fields in csv.reader(io.StringIO(text)):
        if not fields or all(not f.strip() for f in fields):
            continue
        if len(fields) < 3:
            malformed += 1
            continue
        rid = fields[0].strip()
        indicator = fields[2].strip()
        if not rid or indicator not in _VALID_INDICATORS:
            malformed += 1
            continue

        f = [x.strip() for x in fields] + [""] * (12 - len(fields))
        row = BatchRow(row_id=rid, indicator=indicator)
        if indicator == "Match":
            row.match_type = f[3] or None
            row.matched_address = f[4] or None
            row.latitude, row.longitude = _split_coordinates(f[5])
            row.tiger_line_id = f[6] or None
            row.tiger_line_side = f[7] or None
            row.state_fips = f[8] or None
            row.county_fips = f[9] or None
            row.census_tract = f[10] or None
            row.census_block = f[11] or None
        rows[rid] = row
    return rows, malformed
