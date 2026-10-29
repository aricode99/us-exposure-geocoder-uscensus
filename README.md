# US Exposure Geocoding & QA Tool

A lightweight internal web app for **U.S. insurance exposure data and catastrophe-modelling workflows**.
An analyst uploads a CSV or Excel Statement of Values (SOV), maps the address columns, geocodes the
locations with the free **U.S. Census Bureau Geocoder**, reviews match quality, maps the portfolio and
downloads an enriched SOV.

> **Status: test build (v0.1.1).** All Census calls in the automated tests are mocked. The batch and
> single-address formats were verified against the live service in October 2026 (see
> [Census API usage](#census-api-usage)). Test with synthetic or public addresses before using real exposure data.

---

## Contents

1. [Purpose](#purpose) · 2. [Architecture](#architecture) · 3. [Installation](#installation) ·
4. [Local execution](#local-execution) · 5. [Census API usage](#census-api-usage) ·
6. [Input requirements](#input-requirements) · 7. [Output fields](#output-fields) ·
8. [QA methodology](#qa-methodology) · 9. [Known limitations](#known-limitations) ·
10. [CAT modelling considerations](#cat-modelling-considerations) · 11. [Testing](#testing) ·
12. [Deployment](#deployment) · 13. [Privacy](#privacy-and-security)

## Purpose

Workflow: **Upload SOV → inspect → map columns → clean → geocode (Census) → evaluate match quality →
review exceptions → visualise → export.**

It is built for exposure management, not as a generic geocoder: it keeps every original column, records the
exact Census benchmark/vintage used, separates street-level matches from area-centroid fallbacks, and states
plainly what Census coordinates are (and are not).

## Architecture

```
us-exposure-geocoder/
├── app.py                      Streamlit UI only
├── modules/
│   ├── file_handler.py         in-memory CSV/XLSX read, column suggestions, validation, CSV/XLSX export
│   ├── address_cleaner.py      conservative normalisation -> Clean* fields (originals preserved)
│   ├── census_geocoder.py      Census client: single + batch, retry/backoff, parsers, benchmark resolver
│   ├── geography.py            bundled Gazetteer lookups: ZIP/city/county fallback centroids, state bounds
│   ├── qa_engine.py            rule-based QAStatus / QAReason / GeocodeResolution
│   ├── pipeline.py             orchestration + dashboard summary (no UI code)
│   ├── views.py                map / QA-table helpers (no UI code)
│   ├── maps.py                 map styles, Folium builder, pydeck raster styles (no UI code)
│   ├── match_levels.py         match-level crosswalk (code / enhanced code / label)
│   ├── confidence.py           confidence bands from match level + consistency checks
│   ├── edits.py                analyst edits: re-geocode corrected addresses, set coordinates
│   └── __init__.py
├── utils/constants.py          URLs, limits, labels, disclaimers, state tables
├── utils/logging_config.py     logging that never records addresses or URLs
├── data/reference/             Census Gazetteer tables (fallback centroids) + us_states.geojson (map outline)
├── sample_data/sample_sov.csv  synthetic test file (public landmark addresses + deliberate problems)
├── dev_scripts/                smoke_single.py, build_reference_data.py, build_state_boundaries.py
├── tests/                      163 unit tests, Census fully mocked
├── .streamlit/config.toml      telemetry off, upload limit
├── Dockerfile, requirements*.txt, .github/workflows/tests.yml
```

**Data flow.** Uploaded bytes → DataFrame (all text) → `Clean*` fields → routing per row →
sequential Census batches → merge on an internal row key → fallback → QA → audit fields → dashboard / map / downloads.

Key design decisions:

* **Internal row key.** An internal `GeocodeRowID` (1..n) is what Census receives, not your Location ID. Results are
  merged by that key, never by row order, and duplicate or blank Location IDs cannot corrupt the join. Your Location ID
  never leaves the app.
* **Routing.** Street + (ZIP or city+state) → Census. No usable street → area-centroid fallback.
  Puerto Rico / territories → out of scope. Nothing is silently dropped.
* **API logic is separate from UI logic.** Everything in `modules/` runs without Streamlit.

## Installation

Python 3.11 or 3.12 (developed and tested on 3.12 with pandas 2.3 and 3.0).

```bash
git clone <your-repo-url> us-exposure-geocoder
cd us-exposure-geocoder
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Local execution

```bash
streamlit run app.py
```

Then open the URL shown (default http://localhost:8501) and upload `sample_data/sample_sov.csv` for a first test.
The **Single Address** page lets you test one address without a file.

## Census API usage

Official documentation: <https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html>
(reviewed 2026-10; page marked *Last updated 02/2026*).

| Use | Endpoint |
|---|---|
| Single address | `GET /geocoder/locations/address` and `/geocoder/geographies/address` |
| Batch | `POST /geocoder/locations/addressbatch` and `/geocoder/geographies/addressbatch` (multipart `addressFile`) |
| Benchmarks / vintages | `GET /geocoder/benchmarks`, `GET /geocoder/vintages?benchmark=<id>` |

* **Batch limit:** 10,000 records per file ("currently"). The app defaults to **500** per request, configurable up to 10,000,
  and runs batches **sequentially**. Example: 23,500 records at 10,000 → batches of 10,000 / 10,000 / 3,500.
* **Batch input:** headerless CSV `ID, Street, City, State, ZIP`, empty fields keep their commas.
* **Batch output (verified):** headerless, fully quoted CSV. Match: `id, input, Match, Exact|Non_Exact, matched address,
  "lon,lat", TIGER line id, side [, state, county, tract, block]`. No_Match and Tie rows contain only three columns.
  Coordinates arrive as one `"lon,lat"` field (the app swaps them to Latitude / Longitude).
* **Minimum address:** street + ZIP, or street + city + state.
* **Benchmark / vintage:** names such as `Public_AR_Current` / `Current_Current` are *moving aliases*. The app resolves them
  against the live endpoints at run time and stamps the actual ID, name and description on every output row. IDs are never hardcoded.
* **Single-address mode** does not report Exact / Non_Exact, so `CensusMatchType` is empty there.
* **Retries:** timeouts, connection errors, HTTP 429/5xx, empty or unreadable bodies are retried up to 3 times with
  1 s / 2 s / 4 s backoff. No_Match and other 4xx responses are never retried. Batch read timeout is 300 s.
* Census does not document rate limits. A transient hang was observed once in testing, which is why batches are sequential and retried.

## Input requirements

* `.csv` (UTF-8, Windows-1252 or Latin-1; comma, semicolon or tab) or `.xlsx` (sheet selector). Up to 200 MB / 250,000 rows.
* Column names are free; the app suggests mappings from common names (`LocationID`, `LocID`, `Address`, `Street`, `StreetAddress`,
  `Address1`, `City`, `State`, `StateCode`, `ZIP`, `ZipCode`, `PostalCode`, `County`…).
* **Required:** Location ID, Street Address. **Recommended:** City, State, ZIP. **Optional:** County (fallback only).
* Everything is read as text, so ZIP codes and IDs are never converted to numbers. Your file is never modified.

## Output fields

Every original column is preserved, followed by:

| Group | Fields |
|---|---|
| Key | `GeocodeRowID` |
| Preserved / cleaned input | `OriginalStreet/City/State/ZIP`, `CleanStreet/City/State/ZIP`, `CleanFlags` |
| Census result | `CensusMatchedAddress`, `Latitude`, `Longitude`, `CensusMatchIndicator` (Match/No_Match/Tie), `CensusMatchType` (Exact/Non_Exact), `TigerLineID`, `TigerLineSide`, `StateFIPS`, `CountyFIPS`, `CensusTract`, `CensusBlock` |
| Routing / resolution | `GeocodeRoute`, `GeocodeResolution`, `GeoResolutionBasis`, `CensusGeographyLevel`, `GeocodeSource`, `CoordinateMethod` |
| QA | `QAStatus`, `QAReason` |
| Audit | `GeocodeProvider`, `GeocodeBenchmark`, `GeocodeBenchmarkID`, `GeocodeVintage`, `GeocodeVintageID`, `GeocodeTimestamp`, `GeocodeMethod`, `GeocodeBatchNumber` |

If your file already has a column with an appended name (e.g. `Latitude`), your column is left untouched and the appended
one is exported as `Latitude_Geocode`.

`GeocodeResolution` (how the coordinate was produced): `ADDRESS_RANGE_INTERPOLATED` (Census street match),
`ZIP_CENTROID`, `CITY_CENTROID`, `COUNTY_CENTROID` (fallbacks), `NONE`.
`CensusGeographyLevel` (which Census area the point falls in): `BLOCK`, `TRACT`, `COUNTY`, `STATE`, or the fallback
area type `ZCTA` / `PLACE` / `COUNTY`. **A block code does not mean block-level accuracy.**

**Downloads:** complete enriched SOV · successfully geocoded (street level) · QA review / exceptions ·
failed / no-coordinate · fallback locations. CSV or XLSX. Note: opening a CSV directly in Excel drops leading zeros from
ZIP columns; use the XLSX download or import the column as text.

## Match level, confidence and analyst edits

**Match level.** Every record gets `GeoMatchLevelCode`, `EnhancedGeoMatchLevelCode` and a `GeoMatchLevel` label:

| Result | Code / enhanced | Label |
|---|---|---|
| Census Exact | ADDR / SEGI | Exact Address |
| Census Non_Exact, house number inside the matched range | RLXA / SEGI | Relaxed Address |
| Census Non_Exact, house number differs from the matched one | RLXA / STRI | Relaxed Address |
| ZIP centroid fallback | POST / POST | Postal Code centroid |
| City centroid fallback | CITY / CITY | City centroid |
| County centroid fallback | CNTY / COUNTY | County centroid |
| Tie, No_Match, not submitted, out of scope | NONE / NONE | None |
| Analyst-set coordinates | MANL / MANL | Manual (analyst-set) |

Census reports only Exact / Non_Exact; the house-number split is inferred, so treat it as a heuristic.

**Confidence band.** `GeocodeConfidence` is **High / Moderate / Low / Very low**, plus **Analyst-set** and **Not geocoded**.
It starts from the match level (Exact 95, relaxed 75 or 55, postal 40, city 25, county 12) and subtracts for evidence against the
match: state / ZIP / city / house number / street name differ from the input (−60 state, −20 others), point outside the state's
plausible area (−60), point more than 75 km from the input ZIP's centroid (−20), Census county differs from the SOV county (−20),
same point assigned to a different street (−10). High ≥ 80, Moderate 60–79, Low 35–59, Very low < 35. A relaxed match is never
High. `ConfidenceBasis` lists what applied. **It is generated by this application, is not a Census measure and is not calibrated
against surveyed locations.** Constants live at the top of `modules/confidence.py`.

**Street-field cleanup.** When the Street column also contains the city, state, county or ZIP (for example
`239 Deerfield Ct,St Helena Island,SC,Beaufort,29920, St Helena Island, SC, 29920`) the trailing locality is removed from `CleanStreet`
and flagged `STREET_LOCALITY_REMOVED`. The original text is kept in `OriginalStreet`.

**Analyst edits (QA Review).** Correct Street / City / State / ZIP and re-geocode only those rows, or enter verified coordinates with a
required reason (labelled MANUAL_OVERRIDE, never counted as a Census result). Original SOV values are never changed. Each row carries
`AnalystEdit*` fields and the downloadable edit log records before / after, time and reason. Results live in the browser session only,
so download the edit log with your files.

## QA methodology

> `QAStatus` and `QAReason` are generated **by this application**. The Census Geocoder publishes **no numeric confidence
> score**, and these values are **not** an official Census measure.

| Check | Effect |
|---|---|
| Match + Exact | HIGH |
| Match + Non_Exact | MEDIUM |
| Tie | REVIEW (no coordinate selected) |
| No_Match, or no result returned | FAILED |
| State differs between cleaned input and matched address | REVIEW (forced) |
| ZIP differs | one level lower |
| City differs (similarity < 0.80) | one level lower |
| House number differs | one level lower |
| Street-name similarity < 0.70 | one level lower |
| Coordinate outside the expected area for the state (coarse box) | REVIEW (forced) |
| Match without usable coordinates | FAILED |

Address comparison never lowers a *match* below REVIEW. Street comparison ignores unit designators and standardises common
suffix, direction and ordinal words (so "Fifth Avenue" vs "5TH AVE" is not a mismatch). Every rule that fires is written to `QAReason`.

Additional statuses: **FALLBACK** (area centroid, never HIGH/MEDIUM) and **OUT_OF_SCOPE** (Puerto Rico / territories).
Thresholds are constants at the top of `modules/qa_engine.py`.

**Dashboard:** the "Census outcomes" row is mutually exclusive and sums to the total. The QA row overlaps it
(a Census match can be downgraded to REVIEW). "Successfully Geocoded" counts street-level matches only; fallbacks are never counted as geocoded.

## Known limitations

* Census coordinates are **interpolated along address ranges**: points can fall on the wrong side of the street or far along a long segment.
* New construction, renamed streets, rural routes and unit-only addresses may not match (e.g. a well-known former corporate campus address returned No_Match in testing).
* **Fallback centroids** use ZIP Code Tabulation Areas, which approximate but are not USPS ZIP codes; unique-use ZIPs (e.g. a single large organisation) have no ZCTA. City names that are ambiguous within a state are never guessed. Fallback data is the 2025 Gazetteer (rebuild with `dev_scripts/build_reference_data.py`).
* Puerto Rico and U.S. territories are out of scope. PO Boxes cannot be geocoded to a site and use fallback.
* The "coordinate outside expected area" check is a coarse sanity box, not a boundary test; it is skipped for Alaska.
* Large portfolios: the pipeline itself takes ~30 s for 100,000 rows excluding Census time; total time is dominated by Census (hundreds of sequential requests at the default batch size). The browser session must stay open for the run.
* No persistence: results live in the browser session only. Download your files before closing the tab.

## CAT modelling considerations

Census coordinates are **not** rooftop, building-centroid or parcel-centroid locations
(`Geocode Source = US Census MAF/TIGER`, `Coordinate Method = Address Range Interpolation`).
Consider higher-precision geocoding where small coordinate differences can materially change results:
**flood, storm surge, wildfire, coastal proximity and other localised hazards.** Review all FALLBACK, REVIEW and MEDIUM
locations before modelling, and keep the benchmark/vintage columns with the exposure file so results are reproducible.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # 211 tests, no network
python dev_scripts/smoke_single.py        # optional live single-address check
```

## Deployment

**Streamlit Community Cloud (testing only).** Push to GitHub, create an app pointing at `app.py`, Python 3.11 or 3.12.
`requirements.txt` and `.streamlit/config.toml` are picked up automatically.
> ⚠️ Community Cloud is third-party hosting. **Use it with synthetic or public data only.**
> Uploaded SOVs would be processed on infrastructure outside your organisation.

**Internal hosting (for real exposure data).** Run `streamlit run app.py` on an internal server, or use the included `Dockerfile`:

```bash
docker build -t us-exposure-geocoder .
docker run -p 8501:8501 us-exposure-geocoder
```

(The Dockerfile follows standard Streamlit practice but has not been build-tested in this repository.) Put it behind your normal
authentication / reverse proxy; the app has no login of its own. Outbound access to `geocoding.geo.census.gov` is required.

## Privacy and security

* Uploads are processed **in memory** and never written to disk by the app.
* Address data is sent **only** to the U.S. Census Geocoder, using an internal row key rather than your Location ID.
* No analytics or tracking libraries; Streamlit telemetry is disabled in `.streamlit/config.toml`.
* Logs contain counts, status codes and error class names, never addresses, IDs or request URLs.
* **Maps (v0.2.0).** Two renderers: an interactive **Folium/Leaflet street map** (default for up to 5,000 points: layer switcher, hover tooltips, optional clustering) and a **pydeck** WebGL map for large portfolios. Base styles: OpenStreetMap (default), Esri streets, USGS satellite / topographic, plus optional Census **TIGERweb** state/county and tract boundary overlays. The default style loads tiles **in the user's browser** from public providers; no exposure data is sent, but the provider sees the viewed area and your network may block it. Folium also loads the Leaflet/Bootstrap/jQuery assets from public CDNs (jsdelivr, cdnjs, jquery.com, bootstrapcdn); the pydeck engine does not. Choose **Offline: state outlines only** (or set `DEFAULT_MAP_STYLE` in `utils/constants.py`) to avoid map tiles; note Folium CDN assets are still fetched, so use the pydeck engine for a fully third-party-free map. Check each tile provider's usage terms before heavy or commercial use (OSM's public tile server is for light use).
* Map tooltips escape HTML so a malicious cell value cannot inject markup.
* `.gitignore` blocks `*.csv` / `*.xlsx` so exposure files are not committed; only the synthetic sample is allowed.
