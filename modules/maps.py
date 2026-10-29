"""Map helpers (no Streamlit code): style catalogue, Folium builder, pydeck raster styles.

Two renderers share one style catalogue:

* **Folium / Leaflet**: interactive street-level review of small and medium sets (hover tooltips,
  in-map layer switcher, optional clustering). Practical up to roughly 5,000-10,000 points.
* **pydeck / deck.gl** (WebGL): large portfolios. Street tiles are supplied as a MapLibre raster
  style passed as a ``data:`` URI, so no extra JavaScript library is needed.

Privacy: tiles are fetched by the USER'S BROWSER from the provider. No exposure data is sent, but the
provider can see which area is being viewed. ``Offline`` draws only the bundled state outlines and
makes no third-party requests.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

import folium
import pandas as pd
from folium.plugins import MarkerCluster

from modules.views import load_state_outline
from utils import constants as C

OFFLINE = "Offline: state outlines only"

# name -> tile template and terms. {z}/{x}/{y} ordering differs by provider (Esri/USGS use z/y/x).
STYLES: Dict[str, Dict[str, Any]] = {
    "Streets (OpenStreetMap)": {
        "tiles": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attr": "&copy; OpenStreetMap contributors", "max_zoom": 19, "native_zoom": 19,
        "host": "tile.openstreetmap.org"},
    "Streets (Esri)": {
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles &copy; Esri", "max_zoom": 19, "native_zoom": 19,
        "host": "server.arcgisonline.com"},
    "Satellite (USGS imagery)": {
        "tiles": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
        "attr": "Imagery: U.S. Geological Survey", "max_zoom": 19, "native_zoom": 16,
        "host": "basemap.nationalmap.gov"},
    "Topographic (USGS)": {
        "tiles": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        "attr": "Map: U.S. Geological Survey", "max_zoom": 19, "native_zoom": 16,
        "host": "basemap.nationalmap.gov"},
}
STYLE_NAMES: List[str] = list(STYLES) + [OFFLINE]
DEFAULT_MAP_STYLE = C.DEFAULT_MAP_STYLE

_TIGER_WMS = "https://tigerweb.geo.census.gov/arcgis/services/TIGERweb/tigerWMS_Current/MapServer/WMSServer"
# Census TIGERweb WMS layer ids (verified against GetCapabilities, Oct 2026)
OVERLAYS: Dict[str, Dict[str, str]] = {
    "State & county boundaries (Census TIGERweb)": {"layers": "11,13", "attr": "U.S. Census Bureau TIGERweb"},
    "Census tracts (Census TIGERweb)": {"layers": "71", "attr": "U.S. Census Bureau TIGERweb"},
}

_BLANK_BG = "#dde5ee"


def hosts_for(style: str, overlays: Iterable[str] = ()) -> List[str]:
    """Third-party hosts the browser will contact (for the privacy caption)."""
    hosts: List[str] = []
    if style in STYLES:
        hosts.append(STYLES[style]["host"])
        if list(overlays):
            hosts.append("tigerweb.geo.census.gov")
    return hosts


# ---------------------------------------------------------------------------
# pydeck / MapLibre raster style (data URI)
# ---------------------------------------------------------------------------
def _data_uri(style: dict) -> str:
    return "data:application/json;base64," + base64.b64encode(
        json.dumps(style, separators=(",", ":")).encode()).decode()


def blank_style_uri() -> str:
    """A style with no tile sources. Streamlit falls back to Carto tiles if no style is given."""
    return _data_uri({"version": 8, "sources": {},
                      "layers": [{"id": "bg", "type": "background", "paint": {"background-color": _BLANK_BG}}]})


def raster_style(style: str, overlays: Iterable[str] = ()) -> Optional[dict]:
    """MapLibre style JSON for an online style plus optional TIGERweb overlays; None if offline."""
    spec = STYLES.get(style)
    if spec is None:
        return None
    sources: Dict[str, Any] = {"base": {"type": "raster", "tiles": [spec["tiles"]], "tileSize": 256,
                                        "maxzoom": spec["native_zoom"], "attribution": spec["attr"]}}
    layers: List[dict] = [{"id": "base", "type": "raster", "source": "base"}]
    for i, name in enumerate(o for o in overlays if o in OVERLAYS):
        url = (f"{_TIGER_WMS}?service=WMS&version=1.1.1&request=GetMap&layers={OVERLAYS[name]['layers']}"
               "&styles=&format=image/png&transparent=true&srs=EPSG:3857&width=256&height=256"
               "&bbox={bbox-epsg-3857}")
        sources[f"ov{i}"] = {"type": "raster", "tiles": [url], "tileSize": 256,
                             "attribution": OVERLAYS[name]["attr"]}
        layers.append({"id": f"ov{i}", "type": "raster", "source": f"ov{i}"})
    return {"version": 8, "sources": sources, "layers": layers}


def pydeck_style_uri(style: str, overlays: Iterable[str] = ()) -> str:
    """map_style value for pydeck: online raster style, or the blank style for offline."""
    ms = raster_style(style, overlays)
    return _data_uri(ms) if ms else blank_style_uri()


# ---------------------------------------------------------------------------
# Engine choice
# ---------------------------------------------------------------------------
ENGINE_AUTO = "Auto"
ENGINE_FOLIUM = "Interactive street map (Folium)"
ENGINE_PYDECK = "Large-portfolio map (pydeck)"
ENGINES = [ENGINE_AUTO, ENGINE_FOLIUM, ENGINE_PYDECK]


def choose_engine(requested: str, n_points: int) -> str:
    """Resolve the engine. Auto uses Folium for small sets and pydeck above the threshold."""
    if requested == ENGINE_FOLIUM:
        return ENGINE_FOLIUM
    if requested == ENGINE_PYDECK:
        return ENGINE_PYDECK
    return ENGINE_FOLIUM if n_points <= C.FOLIUM_AUTO_MAX_POINTS else ENGINE_PYDECK


# ---------------------------------------------------------------------------
# Folium
# ---------------------------------------------------------------------------
def _rgba_to_hex(rgba: List[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgba[:3])


def _add_base_layers(m: folium.Map, selected: str) -> None:
    for name, spec in STYLES.items():
        folium.TileLayer(tiles=spec["tiles"], attr=spec["attr"], name=name, max_zoom=spec["max_zoom"],
                         max_native_zoom=spec["native_zoom"], show=(name == selected),
                         overlay=False, control=True).add_to(m)


def _add_outline(m: folium.Map, show: bool = True) -> None:
    """Bundled state outlines (offline background)."""
    outline = load_state_outline()
    if not outline:
        return
    folium.GeoJson(
        outline, name="State outlines (bundled)", show=show,
        style_function=lambda _f: {"color": "#8c96a5", "weight": 1, "fillColor": "#f4f6f9",
                                   "fillOpacity": 1.0},
        control=True).add_to(m)


def build_folium_map(
    plot: pd.DataFrame,
    style: str = DEFAULT_MAP_STYLE,
    overlays: Iterable[str] = (),
    cluster: bool = False,
) -> folium.Map:
    """Interactive map from a ``views.map_points`` frame (text columns already HTML-escaped).

    All online base layers are added with an in-map switcher; ``style`` picks the initial one.
    ``Offline`` draws bundled state outlines only and requests no tiles.
    """
    offline = style == OFFLINE or style not in STYLES
    m = folium.Map(location=[39.5, -98.35], zoom_start=4, tiles=None, control_scale=True,
                   prefer_canvas=True, zoom_control=True)
    # Leaflet tooltips default to one unwrapped line, which gets clipped at the map edge: wrap and cap width.
    m.get_root().header.add_child(folium.Element(
        "<style>.leaflet-tooltip{white-space:normal !important;width:max-content;max-width:300px;"
        "font-size:12px;line-height:1.35;overflow-wrap:anywhere;}</style>"))
    if offline:
        folium.Element(f"<style>.leaflet-container{{background:{_BLANK_BG};}}</style>").add_to(m.get_root().header)
        _add_outline(m, show=True)
    else:
        _add_base_layers(m, style)
        for name in overlays:
            if name in OVERLAYS:
                folium.WmsTileLayer(
                    url=_TIGER_WMS, layers=OVERLAYS[name]["layers"], fmt="image/png", transparent=True,
                    name=name, attr=OVERLAYS[name]["attr"], overlay=True, control=True, show=True,
                    version="1.1.1").add_to(m)

    target: Any = MarkerCluster(name="Locations (clustered)", disableClusteringAtZoom=15).add_to(m) \
        if cluster else folium.FeatureGroup(name="Locations").add_to(m)
    for row in plot.itertuples(index=False):
        tooltip = (f"<b>{row.LocationID}</b><br/>Original: {row.OriginalAddress}<br/>"
                   f"Census matched: {row.CensusMatchedAddress}<br/>Lat / Lon: {row.Latitude}, {row.Longitude}<br/>"
                   f"Match type: {row.MatchType}<br/>QA status: {row.QAStatus}<br/>Resolution: {row.Resolution}")
        colour = _rgba_to_hex(list(row.color))
        folium.CircleMarker(
            location=(row.Latitude, row.Longitude), radius=float(row.radius) + 2, color="#ffffff", weight=1,
            fill=True, fill_color=colour, fill_opacity=0.9,
            tooltip=folium.Tooltip(tooltip, sticky=True, direction="auto")).add_to(target)

    if not plot.empty:
        south, north = float(plot["Latitude"].min()), float(plot["Latitude"].max())
        west, east = float(plot["Longitude"].min()), float(plot["Longitude"].max())
        if abs(north - south) < 1e-4 and abs(east - west) < 1e-4:
            m.location, m.options["zoom"] = [south, west], 15
        else:
            m.fit_bounds([[south, west], [north, east]], padding=(30, 30), max_zoom=15)
    folium.LayerControl(collapsed=True, position="topright").add_to(m)
    return m


def single_point_map(lat: float, lon: float, label: str, style: str = DEFAULT_MAP_STYLE) -> folium.Map:
    """Map for the Single Address tab: one marker, street-level zoom."""
    import html as _html
    plot = pd.DataFrame([{
        "LocationID": "Census match", "OriginalAddress": "", "CensusMatchedAddress": _html.escape(label),
        "Latitude": round(lat, 6), "Longitude": round(lon, 6), "MatchType": "Match", "QAStatus": "n/a",
        "Resolution": C.RES_ADDRESS, "color": [31, 111, 235, 220], "radius": 5}])
    return build_folium_map(plot, style)


def folium_cap(requested_max: int) -> int:
    return max(1, min(int(requested_max), C.FOLIUM_HARD_MAX_POINTS))
