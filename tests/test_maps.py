"""Map module tests: style catalogue, raster style JSON, engine choice, Folium output."""
import base64
import json

import pandas as pd
import pytest

from modules import maps
from modules.pipeline import PipelineOptions, run_geocoding
from modules.views import build_view_frame, map_points
from tests.test_batch_and_pipeline import MAPPING, FakeCensus, client, sov
from utils import constants as C


def _plot():
    df = sov([["<i>1</i>", "1 Good Rd", "New Orleans", "LA", "70112"],
              ["2", "2 NONEXACT Rd", "New Orleans", "LA", "70112"],
              ["3", "", "Austin", "TX", "78701"]])
    res = run_geocoding(df, MAPPING, PipelineOptions(), geocoder=client(FakeCensus()))
    return map_points(build_view_frame(res))[0]


def _html(m):
    return m.get_root().render()


def _decode(uri):
    assert uri.startswith("data:application/json;base64,")
    return json.loads(base64.b64decode(uri.split(",", 1)[1]))


def test_catalogue_defaults_and_offline_option():
    assert maps.DEFAULT_MAP_STYLE in maps.STYLE_NAMES
    assert maps.OFFLINE in maps.STYLE_NAMES
    for name, spec in maps.STYLES.items():
        assert spec["tiles"].startswith("https://") and "{z}" in spec["tiles"] and spec["attr"]


def test_raster_style_is_valid_maplibre_json_with_overlays():
    ms = maps.raster_style("Streets (OpenStreetMap)", ["Census tracts (Census TIGERweb)"])
    assert ms["version"] == 8 and [l["id"] for l in ms["layers"]] == ["base", "ov0"]
    assert ms["sources"]["base"]["tiles"] == ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"]
    assert "{bbox-epsg-3857}" in ms["sources"]["ov0"]["tiles"][0] and "layers=71" in ms["sources"]["ov0"]["tiles"][0]
    assert maps.raster_style(maps.OFFLINE) is None
    assert maps.raster_style("Unknown style") is None


def test_unknown_overlay_names_are_ignored():
    ms = maps.raster_style("Streets (Esri)", ["not an overlay"])
    assert list(ms["sources"]) == ["base"]


def test_offline_pydeck_style_has_no_tile_sources():
    ms = _decode(maps.pydeck_style_uri(maps.OFFLINE, ["State & county boundaries (Census TIGERweb)"]))
    assert ms["sources"] == {}
    assert _decode(maps.blank_style_uri())["sources"] == {}


def test_online_pydeck_style_uri_roundtrips():
    ms = _decode(maps.pydeck_style_uri("Satellite (USGS imagery)"))
    assert ms["sources"]["base"]["maxzoom"] == 16


def test_hosts_reported_for_privacy_caption():
    assert maps.hosts_for(maps.OFFLINE, ["State & county boundaries (Census TIGERweb)"]) == []
    assert maps.hosts_for("Streets (OpenStreetMap)") == ["tile.openstreetmap.org"]
    assert "tigerweb.geo.census.gov" in maps.hosts_for("Streets (Esri)", ["Census tracts (Census TIGERweb)"])


@pytest.mark.parametrize("requested,n,expected", [
    (maps.ENGINE_AUTO, 10, maps.ENGINE_FOLIUM),
    (maps.ENGINE_AUTO, C.FOLIUM_AUTO_MAX_POINTS, maps.ENGINE_FOLIUM),
    (maps.ENGINE_AUTO, C.FOLIUM_AUTO_MAX_POINTS + 1, maps.ENGINE_PYDECK),
    (maps.ENGINE_FOLIUM, 10**6, maps.ENGINE_FOLIUM),
    (maps.ENGINE_PYDECK, 1, maps.ENGINE_PYDECK),
])
def test_engine_choice(requested, n, expected):
    assert maps.choose_engine(requested, n) == expected


def test_folium_cap_respects_hard_limit():
    assert maps.folium_cap(50_000) == C.FOLIUM_HARD_MAX_POINTS and maps.folium_cap(500) == 500


def test_folium_map_has_markers_layers_and_escaped_tooltips():
    html = _html(maps.build_folium_map(_plot(), "Streets (OpenStreetMap)"))
    assert html.count("L.circleMarker") == 3
    assert "tile.openstreetmap.org" in html and "basemap.nationalmap.gov" in html     # in-map switcher
    assert "<i>1</i>" not in html and "&lt;i&gt;1&lt;/i&gt;" in html                  # SOV text is escaped
    assert "L.control.layers" in html


def test_folium_offline_requests_no_tiles():
    html = _html(maps.build_folium_map(_plot(), maps.OFFLINE))
    for host in ("openstreetmap", "arcgisonline", "nationalmap", "tigerweb", "cartocdn"):
        assert host not in html


def test_folium_overlay_added_only_when_requested():
    def render(ov):
        return _html(maps.build_folium_map(_plot(), "Streets (OpenStreetMap)", ov))
    assert "tigerweb" not in render([])
    assert "tigerweb" in render(["Census tracts (Census TIGERweb)"])


def test_folium_cluster_option_and_empty_frame():
    h = _html(maps.build_folium_map(_plot(), "Streets (Esri)", cluster=True))
    assert "markerClusterGroup" in h
    empty = _plot().iloc[0:0]
    assert maps.build_folium_map(empty, "Streets (Esri)") is not None


def test_single_point_map_zooms_to_street_level():
    h = _html(maps.single_point_map(32.3279, -80.4675, "239 DEERFIELD CT, SAINT HELENA ISLAND, SC, 29920"))
    assert "zoom: 15" in h or '"zoom": 15' in h
    assert "L.circleMarker" in h


def test_map_links_only_contain_coordinates():
    import numpy as np
    import pandas as pd
    from modules.views import with_map_links
    df = pd.DataFrame({"LocationID": ["SECRET-1", "X"], "Latitude": [32.3, np.nan], "Longitude": [-80.6, np.nan]})
    out = with_map_links(df)
    assert "32.300000" in out.loc[0, "OpenStreetMap"] and "-80.600000" in out.loc[0, "GoogleMaps"]
    assert "SECRET" not in out.loc[0, "OpenStreetMap"] + out.loc[0, "GoogleMaps"]
    assert pd.isna(out.loc[1, "OpenStreetMap"]) and pd.isna(out.loc[1, "GoogleMaps"])
