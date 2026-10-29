"""Geography helpers: fallback centroids and coordinate plausibility.

Fallback centroids come from bundled U.S. Census Gazetteer files
(``data/reference``). Nothing is sent over the network; lookups are local.

Fallback order (first success wins):

1. ZIP  -> ZCTA centroid   (rejected if the ZIP belongs to a different state)
2. City -> place centroid  (needs a state; never guessed when ambiguous)
3. County -> county centroid (needs a state; never guessed when ambiguous)

These are AREA centroids. They are not site locations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from utils import constants as C

logger = logging.getLogger(__name__)

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "data" / "reference"

_PLACE_SUFFIXES = (
    " city and borough", " consolidated government (balance)", " metropolitan government (balance)",
    " unified government (balance)", " urban county", " municipality", " borough", " village",
    " township", " plantation", " town", " city", " cdp", " comunidad", " zona urbana", " (balance)",
)
# " city" is deliberately NOT stripped: Virginia's independent cities (e.g. "Fairfax city")
# are county-equivalents distinct from the same-named county.
_COUNTY_SUFFIXES = (
    " city and borough", " census area", " municipality", " borough", " parish", " county",
)
_ABBREV = (
    (r"\bst\b\.?", "saint"), (r"\bste\b\.?", "sainte"), (r"\bft\b\.?", "fort"), (r"\bmt\b\.?", "mount"),
)


def _norm(name: str, suffixes: Tuple[str, ...]) -> str:
    s = (name or "").lower().strip()
    s = s.replace("&", " and ")
    for suf in suffixes:                      # strip ONE trailing legal/statistical descriptor
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = re.sub(r"[^\w\s]", " ", s)
    for pattern, repl in _ABBREV:
        s = re.sub(pattern, repl, s)
    return re.sub(r"\s+", " ", s).strip()


def normalise_place_name(name: str) -> str:
    return _norm(name, _PLACE_SUFFIXES)


def normalise_county_name(name: str) -> str:
    return _norm(name, _COUNTY_SUFFIXES)


@dataclass(frozen=True)
class FallbackResult:
    resolution: str               # C.RES_ZIP / RES_CITY / RES_COUNTY
    latitude: float
    longitude: float
    geography_level: str          # ZCTA / PLACE / COUNTY
    basis: str                    # plain-text explanation
    state: Optional[str] = None


@dataclass
class ReferenceData:
    zcta: Dict[str, Tuple[float, float, str, List[str]]]            # zip -> lat, lon, primary, all states
    places: Dict[Tuple[str, str], List[Tuple[str, str, float, float]]]   # (state, norm) -> [(geoid, funcstat, lat, lon)]
    counties: Dict[Tuple[str, str], List[Tuple[str, float, float]]]      # (state, norm) -> [(geoid, lat, lon)]
    state_bounds: Dict[str, Tuple[float, float, float, float]]      # state -> latmin, latmax, lonmin, lonmax
    source_note: str = ""

    @property
    def available(self) -> bool:
        return bool(self.zcta or self.places or self.counties)


def _empty_reference() -> ReferenceData:
    return ReferenceData({}, {}, {}, {}, "Reference data not found")


@lru_cache(maxsize=1)
def load_reference(directory: Optional[str] = None) -> ReferenceData:
    """Load (and cache) the bundled Gazetteer tables. Missing files disable fallback."""
    base = Path(directory) if directory else REFERENCE_DIR
    try:
        zc = pd.read_csv(base / "zcta.csv.gz", dtype=str)
        co = pd.read_csv(base / "counties.csv.gz", dtype=str)
        pl = pd.read_csv(base / "places.csv.gz", dtype=str)
    except (FileNotFoundError, OSError, ValueError):
        logger.warning("Fallback reference data not available; centroid fallback disabled")
        return _empty_reference()

    zcta: Dict[str, Tuple[float, float, str, List[str]]] = {}
    for z, st, alls, lat, lon in zip(zc["zip"], zc["state_primary"], zc["states_all"], zc["lat"], zc["lon"]):
        states = [x for x in str(alls).split("|") if x and x != "nan"]
        zcta[str(z)] = (float(lat), float(lon), "" if pd.isna(st) else str(st), states)

    counties: Dict[Tuple[str, str], List[Tuple[str, float, float]]] = {}
    bounds_acc: Dict[str, List[float]] = {}
    for st, geoid, name, lat, lon in zip(co["state"], co["geoid"], co["name"], co["lat"], co["lon"]):
        la, lo = float(lat), float(lon)
        counties.setdefault((st, normalise_county_name(name)), []).append((geoid, la, lo))
        b = bounds_acc.setdefault(st, [90.0, -90.0, 180.0, -180.0])
        b[0], b[1] = min(b[0], la), max(b[1], la)
        b[2], b[3] = min(b[2], lo), max(b[3], lo)

    places: Dict[Tuple[str, str], List[Tuple[str, str, float, float]]] = {}
    for st, geoid, name, fs, lat, lon in zip(pl["state"], pl["geoid"], pl["name"], pl["funcstat"],
                                              pl["lat"], pl["lon"]):
        places.setdefault((st, normalise_place_name(name)), []).append(
            (geoid, str(fs), float(lat), float(lon)))

    # Coarse plausibility box: county-centroid extent plus a margin. Alaska is skipped
    # (the Aleutians cross the antimeridian). This is a sanity check, not a boundary test.
    margin = 1.5
    state_bounds = {st: (b[0] - margin, b[1] + margin, b[2] - margin, b[3] + margin)
                    for st, b in bounds_acc.items() if st != "AK"}

    note = ""
    src = base / "SOURCE.txt"
    if src.exists():
        note = src.read_text(encoding="utf-8")
    return ReferenceData(zcta, places, counties, state_bounds, note)


def gazetteer_year(ref: Optional[ReferenceData] = None) -> str:
    ref = ref or load_reference()
    m = re.search(r"Gazetteer year:\s*(\d{4})", ref.source_note or "")
    return m.group(1) if m else "unknown"


def coordinate_plausible_for_state(lat: Optional[float], lon: Optional[float],
                                   state: Optional[str], ref: Optional[ReferenceData] = None) -> bool:
    """False when a coordinate is clearly outside the expected area for ``state``.

    Returns True (no objection) when there is not enough information to judge.
    """
    if lat is None or lon is None:
        return True
    lat_ok = 17.0 <= lat <= 72.0
    lon_ok = (-180.0 <= lon <= -64.0) or lon >= 172.0     # >= 172: western Aleutians
    if not (lat_ok and lon_ok):
        return False
    ref = ref or load_reference()
    box = ref.state_bounds.get(state or "")
    if not box:
        return True
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def fallback_centroid(
    state: str = "", zip_code: str = "", city: str = "", county: str = "",
    ref: Optional[ReferenceData] = None,
) -> Tuple[Optional[FallbackResult], List[str]]:
    """Return (result or None, notes explaining skipped tiers).

    ``notes`` always describe why a higher tier could not be used, so the QA
    reason can say, for example, "city name is ambiguous in IL".
    """
    ref = ref or load_reference()
    notes: List[str] = []
    if not ref.available:
        return None, ["fallback reference data unavailable"]

    state = (state or "").upper()

    # 1) ZIP -> ZCTA centroid
    if zip_code:
        hit = ref.zcta.get(zip_code)
        if not hit:
            notes.append(f"ZIP {zip_code} not found in ZCTA table")
        else:
            lat, lon, primary, states = hit
            if state and states and state not in states:
                notes.append(f"ZIP {zip_code} belongs to {primary or '/'.join(states)}, not {state}")
            else:
                return FallbackResult(
                    C.RES_ZIP, lat, lon, "ZCTA",
                    f"No usable street; ZCTA {zip_code} centroid used", primary or None), notes

    # 2) City + state -> place centroid
    if city and state:
        key = (state, normalise_place_name(city))
        cands = ref.places.get(key, [])
        chosen = None
        if len(cands) == 1:
            chosen = cands[0]
        elif len(cands) > 1:
            incorporated = [c for c in cands if c[1] == "A"]
            if len(incorporated) == 1:
                chosen = incorporated[0]
        if chosen:
            return FallbackResult(
                C.RES_CITY, chosen[2], chosen[3], "PLACE",
                f"No usable street or ZIP; place centroid for {city.title()}, {state} used", state), notes
        notes.append(f"city '{city}' is ambiguous in {state}" if cands
                     else f"city '{city}' not found in {state}")
    elif city and not state:
        notes.append("city given without a state")

    # 3) County + state -> county centroid
    if county and state:
        cands = ref.counties.get((state, normalise_county_name(county)), [])
        if len(cands) == 1:
            _, lat, lon = cands[0]
            return FallbackResult(
                C.RES_COUNTY, lat, lon, "COUNTY",
                f"No usable street, ZIP or city; county centroid for {county.title()}, {state} used",
                state), notes
        notes.append(f"county '{county}' is ambiguous in {state}" if cands
                     else f"county '{county}' not found in {state}")
    elif county and not state:
        notes.append("county given without a state")

    return None, notes
