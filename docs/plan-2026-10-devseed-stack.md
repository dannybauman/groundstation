# Plan: catching groundstation up with the 2026 DevSeed stack

Written 2026-10-25 by Roy for Danny, from a sweep of what Development Seed people shipped since July 2026 (GitHub activity, the `now` repo, the 2026 blog posts, the islands notes). groundstation is a COG-era tool: STAC search, TiTiler tiles, event and weather feeds, map artifacts, briefs. The team moved on 4 fronts since then, and 2 of them are cheap to pick up because the public hosting already exists.

Why this plan exists: labs#496 (Jonas, July) asked whether a central geo MCP scales, and labs#501 (unassigned) wants a published specialist geo plug-in. groundstation can be the answer to both if it reads what the team now produces.

## What changed around groundstation, by person

| Person | Shipped since July | What it means here |
|---|---|---|
| Henry Rodman (hrodmn) | titiler-multidim on TiTiler 2 with an xarray coordinate endpoint; smokehrrr (HRRR smoke through raster.eoapi.dev `vrt://` GRIB); hls-stac-geoparquet-archive; icestac; lazycogs; stac-collection-discovery with Lane | Zarr tiles, a smoke layer for the fire story, geoparquet search, cross-catalog collection search |
| Pete Gadomski (gadomski) | cloud-optimized-stac-geoparquet; stac-map; rustac-py; stac-hash; covering fixes in stac-geoparquet; eo-predictor contributions | geoparquet as a catalog, stac-map as the deep-exploration hand-off, better next-pass |
| Anthony Lukach (alukach) | stac-auth-proxy; multistore; source.coop and its CLI; an llms.txt for agents; Portolan catalogs (cloud-native-pcds) | auth and storage for a hosted groundstation; llms.txt now |
| Gjore Milevski (dzole0311) | 3D Zarr cube in deck.gl (labs#507, zarr-sql-views); Zarr forecast example in manywidgets; Mosaic + DataFusion + Zarr in sqlrooms | a 3D Zarr artifact later; Zarr forecasts as data |
| Kyle Barron (kylebarron) | deck.gl-raster 0.9 (client-side COG and Zarr, MultiCOGLayer, terrain draping); zarrista; obstore 0.12; async-tiff | client-side Zarr layers in artifacts with no tiler |
| Daniel Wiesmann (yellowcap) | mcp-toolsets-runtime with Ciaran (the Agentic island's named bet, labs#500); geo-assistant-mcp-toolset (Overture, NAIP, Zarr through DuckDB); stac-zap (one prompt to a STAC query with the jev decision model); gazet | the hosting path for groundstation, and an overlapping sibling to talk to first |
| Daniel da Silva | healpix-ts, deck.gl-healpix, in use on the DestinE Assistant | a HEALPix layer when an open store exists |
| Sean Harkins | virtualizarr-data-pipelines, NAQFC air quality forecast as Icechunk | air quality for the fire story, if the store is public |
| Will Rynearson | eo-predictor (with Pete); montandon-skills (with Sanjay) | next_pass from a real constellation catalog; Montandon as an events source, token needed |
| Emmanuel Mathot, Chuck Daniels | titiler-covjson; titiler-openeo; esa-montandon | time series from Zarr later; openEO processes later |

## Live endpoints checked 2026-10-25

- titiler.xyz runs TiTiler 2.4.0 with zarr 3.4.1 and xarray 2026.9.0, and exposes `/zarr/dataset/...` endpoints, keyless. Zarr rendering needs no new hosting.
- raster.eoapi.dev (titiler 0.24.2, titiler.pgstac 1.9.0) is public and serves NOAA HRRR GRIB through `vrt://`. That is how smokehrrr works.
- staging.openveda.cloud/api/titiler-multidim answers, but staging can change.
- stac.dynamical.org returned 404 for its collection, so the first Zarr datasets are a curated list, not a catalog search.
- source.coop has no STAC endpoint I could find.

## The stops, in Danny's order

Each stop is one branch and one PR, evals output in the PR body, and a version bump in the 3 files when behavior ships (repo rule).

### 1. The Chelan brief shows tomorrow's smoke, and the console survives a 429

- `smoke_forecast` (or a smoke field in `weather_summary`): HRRR MASSDEN band 76 through raster.eoapi.dev `vrt://`, the smokehrrr recipe. CONUS only, forecast frames lag NOAA's bucket; say both in the tool text.
- raster.eoapi.dev as the fallback tiler when titiler.xyz returns 429 (today the fallback is local docker).
- An "open in stac-map" link on every artifact.
- `llms.txt` at the repo root, Anthony's pattern.
- Check `skills/titiler/` recipes against TiTiler 2 (tilesize, no `@2x`, `sel=` selectors).

Done looks like: a fresh Chelan brief carries a smoke frame and a sentence about it, and a forced 429 on titiler.xyz still renders a map.

### 2. Zarr is a first-class source

- A curated Zarr list in `list_catalogs` / `search_datasets`: NOAA HRRR 2 m temperature (small chunks, the geo-assistant pick), NASA MUR SST (metadata only, 123 MB chunks), NAQFC air quality if Sean's Icechunk store is public, dynamical.org forecasts when its catalog answers.
- Tiles and previews through titiler.xyz `/zarr/...` with `variable`, `sel`, `rescale`, `colormap_name`.
- `compute_statistics` over a Zarr variable with xarray plus zarr, clipped to the AOI. 2 new deps, and the reason for them in `docs/stack.md`.
- Option, not in scope: a client-side Zarr layer through deck.gl-raster 0.9 in the artifact HTML, Kyle's path, when the tiler is the bottleneck.

Done looks like: "What is the temperature forecast over Laredo tomorrow afternoon" returns a map and a number from a Zarr store, with the dataset named.

### 3. groundstation runs as a toolset beside geo-assistant

- First a conversation with Daniel W, who is on the November Big Tent lineup for the runtime: groundstation's imagery, events and brief tools as one toolset next to geo-assistant, not a second geo assistant. Overlap to settle: Overture and gazet geocoding, NAIP, Zarr.
- Then the port: the runtime wants LangChain tools over streamable HTTP; groundstation is `mcp<2` plus Claude-side skills. The service-adapter knowledge already lives in `skills/stac-api/` and `skills/titiler/`, which is the modular half Jonas asked for.
- Hosted deployment is the planned follow-on in `docs/architecture.md`; the runtime gives deploy, index, credential pass-through and a hosted chat. stac-auth-proxy and multistore (Anthony) enter here, not before.
- Comment on labs#501 naming groundstation as a candidate.

Done looks like: a groundstation toolset deployed from the toolsets index, callable from Claude.ai, with a hosted chat page.

### 4. Finding data across catalogs, and knowing when the next image comes

- `search_datasets` calls stac-fastapi-collection-discovery (Henry, Lane; blog 2026-07-30) for one federated collection search across Earth Search, VEDA, PC and ESA instead of 3 sequential calls.
- `next_pass` reads EO Predictor's constellation catalog (Will, Pete) instead of a hand list of TLEs, and links the EO Predictor view for the AOI.
- stac-zap's jev decision model as a reference for how `search_imagery` turns a sentence into a valid STAC query; a read, not a dependency, unless it fits.

Done looks like: "which collections cover Efate at better than 10 m" answers across catalogs in one call, and `next_pass` names the same satellites EO Predictor shows.

### 5. stac-geoparquet archives search like a catalog

- `search_imagery` accepts a stac-geoparquet URL as a catalog through rustac-py; Henry's HLS archive is the first target, Pete's cloud-optimized layout the second.
- HLS as a scene family beside Sentinel-2 and Landsat.

Done looks like: a search against a parquet archive returns the same item shape as Earth Search, and the fleet sweep can run against a cached archive with the network off.

## Parked, with the person to ask

- HEALPix layer in `render_map`: Daniel da Silva's healpix-ts and deck.gl-healpix. Waits for an open HEALPix store; the island's DGGS position is still open (Pete: do Zarr well first).
- 3D Zarr cube artifact: Gjore's labs#507. Waits for the reusable component he and Kyle are designing.
- Montandon as an events source: token from the Montandon team, so it breaks the keyless rule; worth it for the IFRC disaster record once a public tier exists.
- Time series from Zarr through titiler-covjson (Chuck, Emmanuel): early alpha.
- openEO processes through titiler-openeo for pixel math beyond band expressions.
- Earth embeddings (GeoAI island, primer blog 2026-10-02): similarity search over an AOI, when an open embeddings store exists.

## Sources

labs#496, labs#500, labs#501, labs#507; islands#9; developmentseed.org/blog 2026 posts (2026-07-30 STAC discovery, 2026-09-28 Behind the Tech Henry, 2026-10-02 embeddings primer, 2026-10-16 Agents with Constraints); the repos named above, read 2026-10-25.
