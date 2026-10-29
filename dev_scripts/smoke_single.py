"""Manual live check of single-address geocoding (calls the real Census service).

Run from the project root:

    python dev_scripts/smoke_single.py
    python dev_scripts/smoke_single.py "4600 Silver Hill Rd" "Washington" "DC" "20233"

Not part of the automated test suite. Use public / test addresses only.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.census_geocoder import CensusGeocoder  # noqa: E402
from utils.logging_config import setup_logging      # noqa: E402


def main(argv):
    setup_logging()
    args = argv[1:] + [""] * (4 - len(argv[1:]))
    street, city, state, zip_code = args[:4]
    if not street:   # default: the example address from the Census documentation
        street, city, state, zip_code = "4600 Silver Hill Rd", "Washington", "DC", "20233"

    client = CensusGeocoder()
    for label, geo in (("locations", False), ("geographies", True)):
        res = client.geocode_single(street, city, state, zip_code, include_geographies=geo)
        print(f"\n=== {label} | status={res.status} | {res.message}")
        print(json.dumps(res.to_record(), indent=2, default=str))
        if res.candidates and res.status == "TIE":
            for c in res.candidates:
                print("  candidate:", c.matched_address, c.latitude, c.longitude)


if __name__ == "__main__":
    main(sys.argv)
