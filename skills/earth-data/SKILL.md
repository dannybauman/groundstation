---
name: earth-data
description: Discover, analyze, and map Earth observation data through the groundstation MCP tools (STAC + TiTiler + geocoding). Use when the user asks about satellite imagery, environmental conditions, land change, disasters, or wants a map of any place on Earth.
---

# Earth data, agent-ready

You have groundstation MCP tools. They put the cloud-native geospatial stack in your hands: three STAC catalogs, dynamic tiling, band math, and shareable map artifacts. Your job is to turn a question about a place into a decision-ready answer, not a data dump.

## The standard flow

1. `geocode` the place (concise names work best: "Barotse Floodplain", not "the floodplain area along the Zambezi in western Zambia").
2. Pick data: `search_datasets` / `describe_collection` if you're not sure what exists, otherwise go straight to `search_imagery`.
3. Analyze: `preview_item` for eyes-on, `compute_statistics` for numbers.
4. **Always end spatial answers with `render_map`** and give the user the file path. A map they can open and share beats prose.

## Which catalog for what

- **earth-search** — fresh raw imagery. Sentinel-2 (`sentinel-2-l2a`), Sentinel-1 (`sentinel-1-grd`), NAIP, Copernicus DEM. First stop for "recent imagery of X". **Landsat exception**: earth-search's `landsat-c2-l2` assets live in a requester-pays bucket the tiler cannot read (AccessDenied on every band) — for Landsat, search and preview on planetary-computer instead.
- **veda** — NASA-curated analysis products: fire severity, air quality, climate indicators, disaster layers. First stop for "what does NASA have on X event".
- **planetary-computer** — deep archive breadth: MODIS, land cover (`io-lulc-annual-v02`, `esa-worldcover`), biomass, DEMs. First stop for historical or thematic layers. Previews use the item's `rendered_preview`; statistics aren't wired yet.

## Conventions that save round trips

- Cloud filtering: pass `max_cloud_cover=20` for optical searches by default; relax only if nothing comes back.
- Sentinel-2 true color: `assets=["visual"]` — one COG, no rescale needed. Landsat: `["red","green","blue"]` with `rescale="0,0.3"` (surface reflectance floats) — check `describe_collection` if colors look wrong.
- NDVI on Sentinel-2 (earth-search): `expression="(nir-red)/(nir+red)"`. NDWI: `"(green-nir)/(green+nir)"`. NBR (burn severity): `"(nir-swir22)/(nir+swir22)"` — compare pre/post fire. NDSI (snow): `"(green-swir16)/(green+swir16)"`, snow-covered where > 0.4. Asset names, not band numbers — translation to TiTiler band indices happens for you.
- **Index layers on maps**: pass the same `expression` on the `render_map` item layer with `rescale="-1,1"` and `colormap_name="rdylgn"` (never bare `assets=[nir, red]` — that renders raw reflectance, which shows as blank).
- **Overlay vs compare**: two rasters of the same collection auto-render as a swipe comparison; different collections stack as toggleable overlays (pass `compare` to override). A thematic layer over imagery (burn severity, land cover) should carry `opacity` ~0.75 so the imagery shows through.
- **Sentinel-1 GRD** (earth-search): `vv`/`vh` assets are unscaled digital numbers, not dB — don't guess a rescale, run `compute_statistics` first and stretch to roughly the 2nd–98th percentile. Radar is the answer when optical is clouded out.
- **Region-scale place names** (a coffee zone, a floodplain, a corridor): geocoding may return a tiny point-feature bbox. Sanity-check the bbox size against the question's scale and widen it yourself before searching, or geocode a better-known containing name.
- Comparing two dates: search with two `datetime_range` windows, then `render_map` with both items as layers (newest on top) so the user can toggle. Name layers with their dates.
- VEDA layers usually want `assets=["cog_default"]` plus a `rescale`/`colormap_name`; check the collection's `renders` metadata via `describe_collection` when unsure.

## Monitoring and briefings

`active_events` (EONET + GDACS) and `weather_summary` exist so you can answer "what's happening around X" and write proactive briefs. When events have coordinates, put them on the map as a geojson layer alongside imagery.

## Judgment rules

- Don't paste raw JSON at the user. Summarize: scene date, cloud cover, what the numbers mean, what changed.
- Interpret statistics against the question: NDVI 0.38 mean is "moderately vegetated"; a drop from 0.5 to 0.2 between dates is the story, not the digits.
- If a search returns nothing, widen one constraint at a time (cloud cover, then time window, then bbox) and say what you relaxed.
- Tiling and statistics ride a shared community endpoint (titiler.xyz) unless GROUNDSTATION_TITILER is set. Be frugal: don't loop preview or statistics calls, keep max_size small. If you get HTTP 429, stop retrying, tell the user the shared tiler is rate-limited, and point them to the README's "Be a good neighbor" section (one-container self-hosting).
- State the scene date next to every claim — Earth changes, and a July answer built on March imagery misleads.
