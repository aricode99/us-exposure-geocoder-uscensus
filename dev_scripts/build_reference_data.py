"""Build the bundled fallback-centroid reference tables from official Census files.

Sources (public, U.S. Census Bureau):
  * Gazetteer: ZCTA, Counties, Places   (internal-point coordinates)
  * 2020 ZCTA-to-County relationship file (used only to learn which state(s) a
    ZIP Code Tabulation Area belongs to, so a ZIP/state conflict can be detected)

Output (data/reference/):
  zcta.csv.gz, counties.csv.gz, places.csv.gz, SOURCE.txt

Run from the project root:

    python dev_scripts/build_reference_data.py            # default Gazetteer year 2025
    python dev_scripts/build_reference_data.py 2024

The generated files are committed to the repository so that the deployed app
needs no network access for fallback centroids and sends nothing anywhere.
"""
import io
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

YEAR = sys.argv[1] if len(sys.argv) > 1 else "2025"
BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{YEAR}_Gazetteer"
REL_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
OUT = Path(__file__).resolve().parents[1] / "data" / "reference"


def fetch(url: str) -> bytes:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def gazetteer(kind: str) -> pd.DataFrame:
    raw = fetch(f"{BASE}/{YEAR}_Gaz_{kind}_national.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        df = pd.read_csv(zf.open(name), sep="|", dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    return df


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    counties = gazetteer("counties")
    counties = counties.rename(columns={"USPS": "state", "GEOID": "geoid", "NAME": "name",
                                        "INTPTLAT": "lat", "INTPTLONG": "lon"})
    counties["lat"] = pd.to_numeric(counties["lat"].str.strip())
    counties["lon"] = pd.to_numeric(counties["lon"].str.strip())
    counties[["state", "geoid", "name", "lat", "lon"]].to_csv(OUT / "counties.csv.gz", index=False)

    fips_to_usps = {g[:2]: s for g, s in zip(counties["geoid"], counties["state"])}

    places = gazetteer("place")
    places = places.rename(columns={"USPS": "state", "GEOID": "geoid", "NAME": "name",
                                    "FUNCSTAT": "funcstat", "INTPTLAT": "lat", "INTPTLONG": "lon"})
    places["lat"] = pd.to_numeric(places["lat"].str.strip())
    places["lon"] = pd.to_numeric(places["lon"].str.strip())
    places[["state", "geoid", "name", "funcstat", "lat", "lon"]].to_csv(OUT / "places.csv.gz", index=False)

    zcta = gazetteer("zcta").rename(columns={"GEOID": "zip", "INTPTLAT": "lat", "INTPTLONG": "lon"})
    zcta["lat"] = pd.to_numeric(zcta["lat"].str.strip())
    zcta["lon"] = pd.to_numeric(zcta["lon"].str.strip())

    rel = pd.read_csv(io.BytesIO(fetch(REL_URL)), sep="|", dtype=str, encoding="utf-8-sig")
    rel = rel[rel["GEOID_ZCTA5_20"].notna()].copy()
    rel["state"] = rel["GEOID_COUNTY_20"].str[:2].map(fips_to_usps)
    rel["area"] = pd.to_numeric(rel["AREALAND_PART"], errors="coerce").fillna(0)
    by_state = rel.groupby(["GEOID_ZCTA5_20", "state"], dropna=True)["area"].sum().reset_index()
    by_state = by_state.sort_values(["GEOID_ZCTA5_20", "area"], ascending=[True, False])
    primary = by_state.drop_duplicates("GEOID_ZCTA5_20").set_index("GEOID_ZCTA5_20")["state"]
    allst = by_state.groupby("GEOID_ZCTA5_20")["state"].agg(lambda s: "|".join(s))

    zcta["state_primary"] = zcta["zip"].map(primary)
    zcta["states_all"] = zcta["zip"].map(allst)
    zcta[["zip", "state_primary", "states_all", "lat", "lon"]].to_csv(OUT / "zcta.csv.gz", index=False)

    (OUT / "SOURCE.txt").write_text(
        "Fallback-centroid reference data (U.S. Census Bureau, public domain)\n"
        f"Gazetteer year: {YEAR}\n"
        "Relationship file: 2020 ZCTA5-to-County (state membership only)\n"
        f"Built (UTC): {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}\n"
        "Coordinates are Census internal points, NOT site locations.\n"
        "ZCTAs approximate USPS ZIP Codes and are not identical to them.\n"
    )
    print("zcta", len(zcta), "counties", len(counties), "places", len(places))
    print("zcta with state:", int(zcta["state_primary"].notna().sum()))


if __name__ == "__main__":
    main()
