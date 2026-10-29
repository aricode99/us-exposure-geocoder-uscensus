# Changelog

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
