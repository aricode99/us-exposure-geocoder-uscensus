"""Build the bundled U.S. state outline used as the offline map background.

Source: U.S. Census Bureau cartographic boundary file (1:20,000,000), public domain.
Output: data/reference/us_states.geojson (coordinates rounded to 3 decimals, ~0.1 km).

    pip install pyshp
    python dev_scripts/build_state_boundaries.py            # default year 2024
"""
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import requests
import shapefile  # pyshp

YEAR = sys.argv[1] if len(sys.argv) > 1 else "2024"
URL = f"https://www2.census.gov/geo/tiger/GENZ{YEAR}/shp/cb_{YEAR}_us_state_20m.zip"
OUT = Path(__file__).resolve().parents[1] / "data" / "reference" / "us_states.geojson"


def _round(coords):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], 3), round(coords[1], 3)]
    return [_round(c) for c in coords]


def main() -> None:
    r = requests.get(URL, timeout=120)
    r.raise_for_status()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        zf.extractall(tmp)
        shp = next(Path(tmp).glob("*.shp"))
        reader = shapefile.Reader(str(shp))
        fields = [f[0] for f in reader.fields[1:]]
        feats = []
        for sr in reader.shapeRecords():
            rec = dict(zip(fields, sr.record))
            geom = sr.shape.__geo_interface__
            geom = {"type": geom["type"], "coordinates": _round(geom["coordinates"])}
            feats.append({"type": "Feature",
                          "properties": {"state": rec.get("STUSPS"), "name": rec.get("NAME")},
                          "geometry": geom})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                              separators=(",", ":")), encoding="utf-8")
    print(f"{len(feats)} features, {OUT.stat().st_size/1024:.0f} KB -> {OUT}")


if __name__ == "__main__":
    main()
