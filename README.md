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

## Earth briefs you

The `briefing/` layer inverts the interaction: instead of you asking Earth the right question, Earth tells you what you need to know.

```bash
uv run briefing/brief.py --place "Chelan County, Washington" --days 10
```

Gathers active events, weather, and fresh imagery for the AOI, has Claude synthesize a decision-ready brief (TL;DR + alert level, what changed, weather signal, fresh scenes, next steps), and writes a shareable HTML page with a live interactive map. Cron it for real dailies. Direct follow-through on team-week#155 ("Thinking Outside the Chat Box").

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
