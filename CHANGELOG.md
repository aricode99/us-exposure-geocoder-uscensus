# Changelog

## 0.3.0 - sidebar step navigation
- The 8 horizontal tabs are replaced by a vertical **Steps** navigator in the sidebar (workflow order, ✓ for completed
  steps, 🔒 for pages that need geocoding results) with "Next →" buttons after mapping and after geocoding.
- Only the selected page is rendered, so interactions on large portfolios are faster.
- Geocoding settings, Map style and Privacy moved into collapsible sidebar sections.
- Column-mapping choices are remembered when navigating between pages. Filters on other pages (map, QA, download format)
  reset when you leave and return to the page; uploaded data and results are kept.
- Headless-browser verified: upload → mapping → geocode → dashboard → map → QA → back to upload, no exceptions.

## 0.2.1
- QA Review table: per-row **Open in OSM / Open in Google** links (coordinates only in the URL; sent only on click).

## 0.2.0 - street maps
- **Map engines:** interactive Folium/Leaflet street map (Auto up to 5,000 points) and pydeck large-portfolio map.
- **Styles:** OpenStreetMap (new default), Esri streets, USGS satellite/topographic, Offline state outlines.
- **Overlays:** Census TIGERweb state/county boundaries and tracts (online styles).
- Single Address tab now shows a street-level Folium map.
- New `modules/maps.py`; dependencies `folium`, `streamlit-folium`. FEMA overlay dropped.
- **Privacy change:** default map now requests tiles from a public provider (see README). Folium loads Leaflet assets from CDNs.
- Tests: 182. Headless-browser verified: Folium+OSM, pydeck+OSM+TIGERweb, Offline. Not verified: USGS satellite render, Single Address map, clustering.

## 0.1.1 - map background fix
- **Fixed:** map showed floating dots on a blank white background when the network blocks `*.cartocdn.com`
  (common on corporate networks).
- **Map background** is now a choice: *State outlines (offline)* (default), *Online basemap tiles*, *None*.
- State outlines are bundled (`data/reference/us_states.geojson`, Census 1:20m cartographic boundaries,
  rebuild with `dev_scripts/build_state_boundaries.py`).
- **Privacy:** Streamlit silently falls back to Carto tiles whenever a map style is omitted, so the offline
  modes use a tile-less inline style. Verified in a headless browser: offline modes contact only the app's own server.
- Tests: 165 (added outline checks).

## 0.1.0 - initial test build
