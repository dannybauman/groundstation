---
name: earth-data
description: Discover, analyze, and map Earth observation data through the groundstation MCP tools (STAC + TiTiler + geocoding). Use when the user asks about satellite imagery, environmental conditions, land change, disasters, or wants a map of any place on Earth.
---

# Earth data, agent-ready

You have groundstation MCP tools. They put the cloud-native geospatial stack in your hands: three STAC catalogs, dynamic tiling, band math, and shareable map artifacts. Your job is to turn a question about a place into a decision-ready answer, not a data dump.

## First: use the tools, never route around them

The value here is the groundstation tools. If they aren't visible yet, the server is still starting — on the first use after install, `uv` builds the server's virtualenv (a few seconds), so its tools surface a moment after the other servers. Once warm, it hands over all 14 tools in about a second.

- **Wait and retry discovery** a few times over ~10-15 seconds before doing anything else. The tools almost always appear.
- **Never fall back to raw STAC / TiTiler / FIRMS / `httpx` calls to work around missing tools.** Hand-rolling the pipeline gives a worse answer and hides a fixable setup problem. Missing tools are a thing to fix, not to route around.
- **If they're still missing after retrying, stop and tell the user, in one or two lines:**
  - Check `/mcp` — `groundstation` should be listed and connected. If not, run `/reload-plugins` (or restart the session) to relaunch it.
  - Make sure `uv` is installed (`which uv`) — the server launches via `uv run`.
  - First launch builds the venv; if it seems stuck, warm it once with `uv sync` in the groundstation repo, then reload.
  - Or hand them one command: `scripts/doctor.sh` in the groundstation repo checks the whole chain (uv, server env, CLI, plugin wiring, endpoints, and whether the copy Claude runs is current) and prints the exact fix.

- **Some tools present but one you expected is missing? The install is stale, not broken.** Plugin installs are cached per version, so a copy keeps serving the tool set it was installed with. Tell them: `claude plugin marketplace update groundstation && claude plugin update groundstation@groundstation`, then restart Claude Code. Work with the tools you have in the meantime, and say which one you're missing.

  That's the fix. Don't proceed with a hand-built substitute.

## The standard flow

1. `geocode` the place (concise names work best: "Barotse Floodplain", not "the floodplain area along the Zambezi in western Zambia").
2. Pick data: `search_datasets` / `describe_collection` if you're not sure what exists, otherwise go straight to `search_imagery`.
3. Analyze: `preview_item` for eyes-on, `compute_statistics` for numbers.
4. **Always end spatial answers with `render_map`** and give the user the file path. A map they can open and share beats prose.
5. Pass `stack_layer=True` to `render_map` when the user asks how it works, what's behind it, or the map is for showing DS/open-source capability — it adds a toggleable panel naming the actual tools, formats, and catalogs on screen. Same flag on `render_map_3d` (the panel then also names the terrain), `render_postcard` (a static credit-block listing), and `compare_dates`. If you geocoded the place or put an `active_events` layer on the map, tell the panel via `stack_facts={"geocoded": True, "events": True}` — only claim what you actually did.

## Say once, early: the demo tiler

Tiling, previews, and statistics ride titiler.xyz — a free, shared, community demo deployment. The first time a session renders something real for a user, say so in one line: this runs on the shared demo tiler, fine for trying things out, not for production or heavy use. If they want more — sustained use, their own data, private catalogs — point them two ways: talk to Development Seed about setting up a deployment for them, or you can walk them through self-hosting right now (`compose.yml` in this repo runs one container; set `GROUNDSTATION_TITILER` to point at it — the README's "Be a good neighbor" section has the steps). Say it once, not every render.

## Which catalog for what

- **earth-search** — fresh raw imagery. Sentinel-2 (`sentinel-2-l2a`), Sentinel-1 (`sentinel-1-grd`), NAIP, Copernicus DEM. First stop for "recent imagery of X". **Requester-pays exception**: earth-search's Landsat (`landsat-c2-l2`) and NAIP (`naip`) assets live in requester-pays buckets the tiler cannot read — for Landsat and NAIP, search and render on planetary-computer instead.
- **veda** — NASA-curated analysis products: fire severity, air quality, climate indicators, disaster layers. First stop for "what does NASA have on X event".
- **planetary-computer** — deep archive breadth: MODIS, land cover (`io-lulc-annual-v02`, `esa-worldcover`), biomass, DEMs. First stop for historical or thematic layers. Previews use the item's `rendered_preview`. Statistics work — note PC Sentinel-2 asset names are band ids (`B04`, `B08`), not color names.

## Conventions that save round trips

- Cloud filtering: pass `max_cloud_cover=20` for optical searches by default; relax only if nothing comes back.
- **State coverage next to every scene claim.** Search results carry `covers_aoi_pct` — 100 means the scene covers the whole asked-for area, 48 means it clips half of it. STAC returns anything that *intersects*, so a "here's Calgary" answer built on a 50%-coverage scene silently shows half the city. Below ~90: say which part is covered ("the western half"), prefer a fuller scene if one is in the results, or render the two best-covering scenes together (same collection auto-swipes). This is also the data behind the whole-area-vs-any-scene clarify case in the judgment rules — you often don't need to ask, the number answers it.
- **When no single scene covers the AOI, use `full_coverage_set`.** Some places straddle tile-grid boundaries (Calgary spans two Sentinel-2 UTM zones), so every single scene crops an edge forever. When the best scene is partial, the search response includes `full_coverage_set` — the newest same-day set whose union covers the whole AOI. Render its items as layers in ONE `render_map` call (`compare=False`, one toggleable layer per scene) and present that as the answer. If a newer partial scene exists, say so plainly: "newest full-coverage pass is Jul 19; a newer Jul 21 scene exists but misses the west edge." Completeness beats freshness for whole-area questions.
- Sentinel-2 true color: `assets=["visual"]` — one COG, no rescale needed. Landsat (planetary-computer, per the requester-pays exception): `["red","green","blue"]` are unscaled uint16 DN, not reflectance — a fixed `rescale="0,0.3"` renders blank; run `compute_statistics` first and stretch to ~p2–p98 (typically ≈`"7300,12400"`), and put the C2 scale/offset (`*0.0000275 - 0.2`) inside any index expression, since the offset doesn't cancel in normalized differences.
- NDVI on Sentinel-2 (earth-search): `expression="(nir-red)/(nir+red)"`. NDWI: `"(green-nir)/(green+nir)"`. NBR (burn severity): `"(nir-swir22)/(nir+swir22)"` — compare pre/post fire. NDSI (snow): `"(green-swir16)/(green+swir16)"`, snow-covered where > 0.4. Asset names, not band numbers — translation to TiTiler band indices happens for you.
- **Index layers on maps**: pass the same `expression` on the `render_map` item layer with `rescale="-1,1"` and `colormap_name="rdylgn"` (never bare `assets=[nir, red]` — that renders raw reflectance, which shows as blank).
- **Overlay vs compare**: two rasters of the same collection auto-render as a swipe comparison; different collections stack as toggleable overlays (pass `compare` to override). A thematic layer over imagery (burn severity, land cover) should carry `opacity` ~0.75 so the imagery shows through.
- **Sentinel-1 GRD** (earth-search): `vv`/`vh` assets are unscaled digital numbers, not dB — don't guess a rescale, run `compute_statistics` first and stretch to roughly the 2nd–98th percentile. Radar is the answer when optical is clouded out.
- **Region-scale place names** (a coffee zone, a floodplain, a corridor): geocoding may return a tiny point-feature bbox. Sanity-check the bbox size against the question's scale and widen it yourself before searching, or geocode a better-known containing name.
- Comparing two dates: search with two `datetime_range` windows, then `render_map` with both items as layers (newest on top) so the user can toggle. Name layers with their dates.
- VEDA layers usually want `assets=["cog_default"]` plus a `rescale`/`colormap_name`; check the collection's `renders` metadata via `describe_collection` when unsure.

## 3D fly-throughs

Triggers: "3D", "fly-through", "terrain", "what does this valley actually look like", anywhere relief is the story (mountains, canyons, coastlines, volcanoes, glaciers).

- Run the normal flow first — `geocode`, then `search_imagery` — and pick the lowest-cloud recent scene that covers the area. Terrain is only as good as the imagery draped on it.
- When the best scene clips the AOI (`covers_aoi_pct` below ~95), pass the other scenes of the same-day `full_coverage_set` as `extra_layers` — they embed as a "Load full coverage" button, so the gaps are one click away without loading tiles upfront. Say so in your answer ("the west edge loads on click").
- Then `render_map_3d(title, bbox, layer, exaggeration=1.5)` with that one scene as the layer (same shape as a `render_map` layer). The artifact carries an exaggeration slider, a fly-through orbit, and a reset button.
- Elevation is the keyless AWS Terrarium tileset, so the page shares as-is. It's global at ~10m-ish over land, sea floor included, and flat terrain looks flat — pick relief-rich AOIs or the 3D adds nothing.
- Exaggeration 1.5 reads well for mountains; push to 2-3 for gentle terrain, drop to 1 when the shape should stay honest.

## Postcards

Triggers: "postcard", "share card", "something I can post", "can I put this on LinkedIn", any result the user is visibly proud of.

- `render_postcard(catalog, collection_id, item_id, place, date, ...)` writes one card: the scene as embedded pixels, the place and date, an optional caption, and the attribution block (Development Seed labs, STAC, TiTiler, the data source, the collection license).
- **Always pass `bbox` with the AOI** so the card is framed on the subject and filled with data — a whole-scene card shows the subject small in a big tile, sometimes with a bare nodata edge. Pick a scene with `covers_aoi_pct` near 100 for cards. (Planetary Computer cards can't crop — pre-rendered previews — so prefer a covering scene there or accept the full frame.)
- **If nothing covers the bbox** (the AOI straddles a swath edge), pass the rest of a **same-day** `full_coverage_set` as `fill_item_ids` — the card bakes as a rio-tiler mosaic read straight from the COGs, main scene winning where they overlap. Same-day fills keep the seam invisible; a different-day fill can show one, so say so in the caption if you must use one. earth-search-style catalogs only (PC/VEDA fall back to the single scene).
- **When the view itself is the story** — 3D terrain, a swipe compare, events on imagery — don't hand over a flat scene card. Pass `postcard={"place": ..., "caption": ...}` to `render_map` or `render_map_3d` instead: it snapshots the actual view (pitched terrain, divider mid-frame, event dots) into the card, and the card's stack listing inherits the map's true facts. Needs a one-time `uv run playwright install chromium`; without it the tools return the artifact plus the install hint in postcard_note.
- **Card shape is a choice, not an accident.** Cards snap to standard ratios (3:2, 4:3, 1:1, 4:5, 2:3) by trimming the bbox centrally; swipe compares and 3D default to landscape 3:2. Override when the scene's shape is the point — `aspect="2:3"` on `render_postcard`, or `postcard={"aspect": "2:3", ...}` on the render tools — for tall subjects like a coastline, a river valley, a long glacier.
- Pixels are embedded rather than linked on purpose. A map artifact points at live tiles, and a Planetary Computer URL carries a signed token that dies within the hour, so a shared page goes blank. A postcard keeps working.
- Index cards work the same way as index map layers: pass `expression` and `colormap_name`.
- The license line only appears when the collection declares a real one. Earth Search says `proprietary` for Sentinel-2, which is STAC's placeholder rather than the actual terms, so the card leaves it off instead of printing something misleading.
- Where and whether to post is the user's decision, never yours. Hand them the file and say what's in it.

## Field tests (only when asked)

A field test is a showcase page: a set of prompts run live, each with its output, its artifact linked, and any learnings — the repeatable "watch it actually work" format. Users normally don't want this; they want their specific question answered directly. Build one only when someone explicitly asks for a field test, a test report, or a page showing off a series of use cases.

- run every case for real first — live searches, real artifacts written to `demo/`. A field test with made-up outputs is worthless.
- write a cases JSON next to the existing ones in `docs/` (copy the shape of `docs/field-test-2026-07-22.cases.json`: rounds of cases, each with tag / who / prompt / answer / artifact / optional image + learning + shows).
- build the page: `uv run scripts/field_test.py docs/<name>.cases.json` — the script owns the design, you own the cases.
- include what went wrong as `learning` lines. Honest stumbles are the most credible part of the format.
- screenshots are optional; artifact links are not.

## Monitoring and briefings

`active_events` (EONET + GDACS) and `weather_summary` exist so you can answer "what's happening around X" and write proactive briefs. When events have coordinates, put them on the map as a geojson layer alongside imagery.

## Judgment rules

- **Ask one clarifying question only when the prompt is genuinely ambiguous about intent — don't gate every query on it.** The classic case: "clearest" scene of a place that's currently smoky or cloudy — a clear/smoke-free image and the most recent image (which shows the smoke) are opposite answers. Same for an unspecified date to compare against, or whether the user wants a scene covering a whole city vs any scene that intersects it. When intent is clear, just run — follow-up corrections work well, so don't add friction. When you proceed on an assumption, state it in one line ("showing the most recent scene, which includes the current smoke") so the user can redirect.
- Don't paste raw JSON at the user. Summarize: scene date, cloud cover, what the numbers mean, what changed.
- Interpret statistics against the question: NDVI 0.38 mean is "moderately vegetated"; a drop from 0.5 to 0.2 between dates is the story, not the digits.
- If a search returns nothing, widen one constraint at a time (cloud cover, then time window, then bbox) and say what you relaxed.
- Tiling and statistics ride a shared community endpoint (titiler.xyz) unless GROUNDSTATION_TITILER is set. Be frugal: don't loop preview or statistics calls, keep max_size small. If you get HTTP 429, stop retrying, tell the user the shared tiler is rate-limited, and point them to the README's "Be a good neighbor" section (one-container self-hosting via compose.yml).
- On a self-hosted tiler, NAIP and Landsat from earth-search return 500s (requester-pays buckets need AWS credentials — titiler.xyz has them, local doesn't by default). Prefer Planetary Computer's copies of those datasets when the user runs their own tiler.
- State the scene date next to every claim — Earth changes, and a July answer built on March imagery misleads.
