# groundstation

**Earth data, agent-ready.** An MCP server + Claude skill that gives AI agents working hands on the cloud native geospatial stack: geocoding, federated STAC search, dynamic tile previews, band math statistics, shareable interactive maps, and the monitoring feeds to write proactive Earth briefings.

A Development Seed labs prototype (July 2026). Everything runs against public, keyless endpoints, so it demos anywhere.

## What an agent can do with it

| Tool | What it does | Backed by |
|---|---|---|
| `geocode` | place name → coordinates + bbox | Gazet (when its JSON API is exposed), Nominatim fallback |
| `list_catalogs` / `search_datasets` / `describe_collection` | find the right data across catalogs | Earth Search, NASA VEDA, Planetary Computer |
| `search_imagery` | recent items with cloud filtering, place names accepted | STAC APIs |
| `preview_item` | browser-openable PNG of any item | titiler.xyz, VEDA raster API, PC data API |
| `compute_statistics` | band math over an item, e.g. NDVI `(nir-red)/(nir+red)` | TiTiler statistics |
| `render_map` | self-contained interactive HTML map artifact | MapLibre + live tiles |
| `active_events` | open wildfires, floods, storms, alerts near a bbox | NASA EONET + GDACS |
| `weather_summary` | past week + week ahead for a point | Open-Meteo |

The per-catalog raster routing is the differentiator: Earth Search items tile through titiler.xyz, VEDA through `openveda.cloud/api/raster`, Planetary Computer through its signing data API. Federated *and* preview-capable, which neither `nasa/earthdata-mcp` nor community `stac-mcp` servers offer.

## Quickstart

```bash
uv sync

# register with Claude Code
claude mcp add groundstation -- uv --directory /path/to/groundstation run groundstation

# then ask Claude things like:
#   "Find a cloud-free Sentinel-2 scene of the Barotse Floodplain from this week,
#    compute NDVI, and give me a map I can share."
```

The paired skill in `skills/earth-data/` carries the judgment layer (which catalog for what, asset conventions, rescale defaults, comparison workflows). Drop it into `~/.claude/skills/` or a project `.claude/skills/`.

## Things to ask it

- "Find the clearest Sentinel-2 scene of Lake Chelan from the past two weeks, tell me what's burning nearby, and give me a map I can share."
- "Compare NDVI east of the Cascades near Wenatchee between now and the same window last July. Same collection, low cloud. Map both dates so I can toggle."
- "Optical is useless over the Barotse Floodplain in the wet season. Find recent Sentinel-1 radar scenes there instead and map one — check the collection's assets for the right rescale."
- "How much surface water is on the Barotse Floodplain right now vs early March? Use NDWI on Sentinel-2, give me the numbers and a two-layer map."
- "What does NASA VEDA have on the Caldor fire? Put the burn severity layer on a map over a current Sentinel-2 scene of the Eldorado National Forest."
- "Show the newest NAIP aerial imagery of Des Plaines, Illinois with ESA WorldCover land cover from Planetary Computer as a toggleable layer on top."
- "Any active flood alerts along the Rio Grande between El Paso and Laredo? Show me alerts, week-ahead rain, and the latest usable imagery on one map."
- "For the Yirgacheffe coffee region in Ethiopia, compare vegetation between January and this month with NDVI stats, and tell me which scenes you'd trust given cloud cover."
- "Brief me on Efate, Vanuatu: any open storm or disaster alerts, the week ahead in weather, and the most recent cloud-free Sentinel-2, all on a map."
- "Show the Ashburn, Virginia data center corridor: newest NAIP at full resolution plus a Sentinel-2 from this month as toggle layers, and give me the NDVI stats for the corridor."

First `search_datasets` call in a session takes ~20-30s while collection lists cache; everything after is instant.

## Earth briefs you

The `briefing/` layer inverts the interaction: instead of you asking Earth the right question, Earth tells you what you need to know.

```bash
uv run briefing/brief.py --place "Chelan County, Washington" --days 10   # one AOI
uv run briefing/brief.py --fleet briefing/fleet.json                     # morning sweep
```

Gathers active events, weather, and fresh imagery for the AOI, computes an NDVI change signal against a same-tile baseline from ~a month back, has Claude synthesize a decision-ready brief (TL;DR + CALM/WATCH/ACT alert level, what changed, weather signal, fresh scenes, next steps), and writes a shareable HTML page with a live interactive map. Follow-through on an internal "thinking outside the chat box" session: instead of being forced to ask the right question, Earth tells you what you need to know.

Fleet mode runs a list of AOIs and writes a morning-sweep index page, sorted ACT → WATCH → CALM, one card per place. That's the ambient story: every area you care about, triaged before you sit down.

## Web console

```bash
uv run --group web groundstation-web   # open http://127.0.0.1:8765
```

The no-CLI, no-chat-client door: type a place, and imagery, active events, and weather populate in parallel while the clearest scene streams onto the map through TiTiler. NDVI stats, Earth brief generation, and a free-text "ask the agent" (headless Claude + this MCP server) are one click each. Deep exploration hands off to [stac-map](https://github.com/developmentseed/stac-map) — Pete Gadomski's map-first STAC visualizer, which renders COGs client-side via Kyle Barron's [deck.gl-raster](https://github.com/developmentseed/deck.gl-raster) — via embedded panel and per-scene links, so the console stays thin and the heavy viz rides on tools the team already builds.

### Console vs MCP server vs brief script — which one when

Three doors into the same tool functions:

- **Web console**: for humans without a terminal — demos, partners, quick looks. Browser UI, zero setup beyond `uv run`.

- **MCP server** (`claude mcp add ...`): the conversational door. You ask, an agent answers with tools — including brief-style questions ("brief me on Efate"). Interactive, exploratory, great for watching the agent think. You never run anything by hand.
- **`brief.py`**: the unprompted door. Nobody asks — the script runs on a schedule (cron, CI, a Slack webhook) and Earth reports in with a polished standalone HTML page. This is the point of #155: the value is that it arrives without a conversation.

Rule of thumb: demos and day-to-day use go through the MCP server. `brief.py` exists so briefings can happen while no one is watching.

## Evals

```bash
uv run evals/run_evals.py
```

Ten live checks against the real endpoints — they prove the demo works *now*, not that it worked once.

## Design notes

- Tool functions in `src/groundstation/tools.py` are plain typed functions with docstrings, following the `developmentseed/mcp-toolsets` shape so they can be dropped into that scaffold as a hosted toolset later.
- Geocoding intentionally routes to Gazet first (set `GAZET_URL` when its JSON endpoint is exposed) — wrapping Gazet as an agentic plug-in is a named Agentic Sea bet.
- Statistics on titiler.xyz merge the asset list into bands `b1..bN`; `compute_statistics` accepts friendly asset-name expressions and translates them.
- Not wired yet: Planetary Computer statistics, eoAPI/CMR backends, next-satellite-pass (see `developmentseed/eo-predictor`), Montandon as an events source (see `developmentseed/montandon-skills`).
