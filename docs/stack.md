# The stack

What is actually behind a groundstation artifact, told as components in the order an artifact is made: a place, a catalog, the data, the pixels, the drawing, the next look. This file is curated, not generated: it says what matters, not what happens to be installed. Artifacts join these entries with their real render parameters, so a panel describes the specific collection, tiler, and buckets on screen.

Fields per component:

- `stage`: `place` / `catalog` / `data` / `pixels` / `draw` / `orbit`. The pipeline position the panel groups by
- `what`: one plain line
- `ds-role`: `created` / `maintains` / `contributes` / `uses`. Projects, never people. `created` and `maintains` are Development Seed's own; panels show those with a filled badge and list them first within a stage
- `island`: optional, `cng` / `dib` / `geoai` / `agentic`. The Development Seed island the component sits in; the panel says which islands an artifact exercised
- `when`: the rule that puts the component on screen, over the artifact's render facts. Bare fact names test truthiness (`terrain`), `fact=a,b` tests membership (`catalog=veda,planetary-computer`, `collection=naip`), `!` negates, `&` is and, `|` is or, `always` is always. Facts: `catalogs`, `collections`, `tiler_hosts`, `maplibre`, `terrain`, `geocoded` (`gazet` or `nominatim`), `events`, `weather`, `passes`, `mosaic_scenes`, `snapshot`. An unknown fact name fails the parser, so a typo cannot silently drop a component from every panel
- `integration`: what it does in this system (the panel's line when it has nothing more specific to say)
- `speaks-to`: edges of the web
- `link`

Adding a tool is one entry here with a `when` rule. `src/groundstation/stack.py` reads this file and needs no change; it only adds a more specific instance line for a few names it knows (TiTiler, STAC, the storage entry, the pgstac trio). An eval parses every entry and checks the rules against the facts of real artifacts.

## Gazet
- stage: place
- what: Development Seed's small-model geocoder — a fuzzy index over Overture divisions and Natural Earth, gated on similarity so a near miss falls through
- ds-role: created
- island: agentic
- integration: first stop for turning "Chelan County" into a bbox, with the real boundary extent
- speaks-to: Overture Maps, Natural Earth, Nominatim
- link: https://github.com/developmentseed/gazet
- when: geocoded=gazet

## Overture Maps
- stage: place
- what: the open map data foundation — the division boundaries Gazet indexes
- ds-role: uses
- integration: the countries, regions and counties Gazet answers with
- speaks-to: Gazet
- link: https://overturemaps.org
- when: geocoded=gazet

## Natural Earth
- stage: place
- what: public-domain physical geography at 1:10m — the lakes, floodplains, ranges and island groups Gazet indexes
- ds-role: uses
- integration: the physical features Gazet answers with
- speaks-to: Gazet
- link: https://www.naturalearthdata.com
- when: geocoded=gazet

## Nominatim
- stage: place
- what: OpenStreetMap's geocoder
- ds-role: uses
- integration: the fallback for towns, landmarks and phrases Gazet's index does not carry, plus reverse geocoding for map labels
- speaks-to: OpenStreetMap
- link: https://nominatim.openstreetmap.org
- when: geocoded=nominatim

## STAC
- stage: catalog
- what: the open catalog spec that makes satellite archives searchable the same way everywhere
- ds-role: contributes
- island: cng
- integration: every imagery search is a STAC POST; every layer starts as a STAC item
- speaks-to: Earth Search, NASA VEDA, Planetary Computer, stac-fastapi, stac-server
- link: https://stacspec.org
- when: catalogs

## eoAPI
- stage: catalog
- what: the deployable Earth-observation API stack — STAC search, dynamic tiling, and vector features as one install
- ds-role: created
- island: cng
- integration: NASA VEDA's catalog and raster APIs run on an eoAPI deployment, so every VEDA layer here is served by it
- speaks-to: stac-fastapi, titiler-pgstac, pgstac, NASA VEDA
- link: https://eoapi.dev
- when: catalog=veda

## eoAPI raster
- stage: pixels
- what: Development Seed's public eoAPI instance, raster.eoapi.dev — it reads HRRR GRIB through GDAL's vrt:// and is the fallback tiler when titiler.xyz rate-limits
- ds-role: created
- island: cng
- integration: the HRRR smoke frames on a brief's map, and the tiles a map swaps to on a titiler.xyz 429
- speaks-to: eoAPI, TiTiler, NOAA HRRR
- link: https://github.com/developmentseed/eoAPI
- when: tiler_hosts=raster.eoapi.dev

## NOAA HRRR
- stage: data
- what: NOAA's 3 km hourly CONUS weather model — the near-surface smoke mass density band
- ds-role: uses
- integration: tomorrow's smoke over the area, as a map frame and two numbers
- speaks-to: eoAPI raster
- link: https://registry.opendata.aws/noaa-hrrr-pds/
- when: tiler_hosts=raster.eoapi.dev

## smokehrrr
- stage: pixels
- what: the recipe for tiling NOAA HRRR smoke GRIBs through eoAPI vrt://
- ds-role: uses
- integration: the vrt:// URL construction and colormap that turns HRRR MASSDEN GRIBs into transparent smoke tiles
- speaks-to: NOAA HRRR, eoAPI raster
- link: https://github.com/hrodmn/smokehrrr
- when: tiler_hosts=raster.eoapi.dev

## stac-fastapi
- stage: catalog
- what: the STAC API server — the one NASA VEDA and Planetary Computer both answer searches with
- ds-role: created
- island: cng
- integration: the search endpoint behind every VEDA and Planetary Computer query here
- speaks-to: pgstac, STAC, eoAPI
- link: https://github.com/stac-utils/stac-fastapi
- when: catalog=veda,planetary-computer

## stac-fastapi-collection-discovery
- stage: catalog
- what: Development Seed's STAC API extension for federated collection discovery across catalogs
- ds-role: created
- island: cng
- integration: search_datasets queries multiple catalogs concurrently and filters collections in-process by place and resolution following this pattern
- speaks-to: STAC, stac-fastapi
- link: https://github.com/developmentseed/stac-fastapi-collection-discovery
- when: catalog=veda,planetary-computer

## pgstac
- stage: catalog
- what: the PostgreSQL STAC database under eoAPI and under Planetary Computer's catalog
- ds-role: created
- island: cng
- integration: where the items a search returns actually live
- speaks-to: stac-fastapi, titiler-pgstac
- link: https://github.com/stac-utils/pgstac
- when: catalog=veda,planetary-computer

## rustac
- stage: catalog
- what: the Rust STAC library with Python bindings; reads, searches and writes stac-geoparquet
- ds-role: contributes
- island: cng
- integration: searches a cached stac-geoparquet archive like a catalog, and writes the archive from a live search, so the search step runs with the network off
- speaks-to: STAC, Earth Search
- link: https://github.com/stac-utils/rustac-py
- when: catalog=geoparquet
<!-- archive layers carry no catalog fact yet: this never fires until stack_facts names one -->

## stac-geoparquet
- stage: catalog
- what: the open specification for storing STAC collections and item records in GeoParquet
- ds-role: contributes
- island: cng
- integration: the file format write_archive writes and search_imagery searches like a catalog
- speaks-to: STAC, rustac
- link: https://github.com/stac-utils/stac-geoparquet
- when: catalog=geoparquet
<!-- archive layers carry no catalog fact yet: this never fires until stack_facts names one -->

## stac-server
- stage: catalog
- what: Element 84's serverless STAC API, the one Earth Search runs on
- ds-role: uses
- integration: the search endpoint behind every Earth Search query here
- speaks-to: STAC, Earth Search
- link: https://github.com/stac-utils/stac-server
- when: catalog=earth-search

## Earth Search
- stage: catalog
- what: Element 84's open STAC catalog of Sentinel-2, Sentinel-1, NAIP, and Copernicus DEM on AWS
- ds-role: uses
- integration: the default catalog for fresh imagery searches
- speaks-to: STAC, stac-server, Cloud object storage
- link: https://earth-search.aws.element84.com/v1
- when: catalog=earth-search

## NASA VEDA
- stage: catalog
- what: NASA's curated Earth science data platform — fire severity, air quality, disaster layers
- ds-role: contributes
- island: cng
- integration: the catalog for analysis-ready NASA products, with its own raster API
- speaks-to: eoAPI, STAC, Cloud object storage
- link: https://www.earthdata.nasa.gov/dashboard
- when: catalog=veda

## Planetary Computer
- stage: catalog
- what: Microsoft's deep Earth archive — MODIS, Landsat, NAIP, land cover, biomass, DEMs
- ds-role: uses
- integration: the catalog for historical and thematic layers, with signed-URL access
- speaks-to: stac-fastapi, titiler-pgstac, Cloud object storage
- link: https://planetarycomputer.microsoft.com
- when: catalog=planetary-computer

## Copernicus Sentinel
- stage: data
- what: ESA's Sentinel-1 radar and Sentinel-2 optical missions, free and open, revisiting every few days
- ds-role: uses
- integration: the scenes behind every sentinel-2-l2a and sentinel-1-grd layer
- speaks-to: Earth Search
- link: https://dataspace.copernicus.eu
- when: collection=sentinel-2-l2a,sentinel-2-l1c,sentinel-1-grd

## Landsat
- stage: data
- what: the USGS and NASA Landsat program, fifty years of 30 m imagery with a thermal band
- ds-role: uses
- integration: the scenes behind every landsat-c2-l2 layer, surface temperature included
- speaks-to: Planetary Computer, Earth Search
- link: https://landsat.gsfc.nasa.gov
- when: collection=landsat-c2-l2,landsat-c2-l1

## NAIP
- stage: data
- what: the USDA's aerial imagery of the United States at about a metre
- ds-role: uses
- integration: the scenes behind every naip layer
- speaks-to: Planetary Computer, Earth Search
- link: https://naip-usdaonline.hub.arcgis.com
- when: collection=naip

## ESA WorldCover
- stage: data
- what: ESA's 10 m global land cover map
- ds-role: uses
- integration: the land cover toggle
- speaks-to: Planetary Computer
- link: https://esa-worldcover.org
- when: collection=esa-worldcover

## AWS Terrarium terrain
- stage: data
- what: free, keyless global elevation tiles (Mapzen legacy, hosted on AWS Open Data)
- ds-role: uses
- integration: the raster-dem source behind 3D fly-throughs
- speaks-to: MapLibre GL, Cloud object storage
- link: https://registry.opendata.aws/terrain-tiles
- when: terrain

## NASA EONET
- stage: data
- what: NASA's open feed of natural events — wildfires, storms, volcanoes, floods
- ds-role: uses
- integration: the events half of "what's happening around X"
- speaks-to: GDACS
- link: https://eonet.gsfc.nasa.gov
- when: events

## GDACS
- stage: data
- what: the UN/EC global disaster alert system
- ds-role: uses
- integration: disaster alert levels alongside EONET events, from its current-events feed
- speaks-to: NASA EONET
- link: https://www.gdacs.org
- when: events

## Open-Meteo
- stage: data
- what: open weather API, no key required
- ds-role: uses
- integration: past and forecast weather for briefs and conditions
- speaks-to: NASA EONET
- link: https://open-meteo.com
- when: weather

## COG + HTTP range requests
- stage: pixels
- what: Cloud-Optimized GeoTIFF — imagery you can read in pieces, so nobody downloads a whole scene
- ds-role: contributes
- island: cng
- integration: how every pixel travels — the tiler range-reads only the bytes each tile needs
- speaks-to: Cloud object storage, TiTiler, rio-tiler
- link: https://cogeo.org
- when: catalogs

## TiTiler
- stage: pixels
- what: dynamic tile server — turns COGs into web map tiles on the fly, band math included
- ds-role: created
- island: cng
- integration: every raster layer's tile URL; expressions like NDVI run server-side
- speaks-to: STAC, COG + HTTP range requests, rio-tiler
- link: https://github.com/developmentseed/titiler
- when: tiler_hosts

## titiler-pgstac
- stage: pixels
- what: the mosaic tiler built on TiTiler and pgstac — eoAPI's raster service, and what Planetary Computer's data API tiles with
- ds-role: created
- island: cng
- integration: the tiler behind every VEDA and Planetary Computer layer here
- speaks-to: TiTiler, pgstac, eoAPI
- link: https://github.com/stac-utils/titiler-pgstac
- when: catalog=veda,planetary-computer

## rio-tiler
- stage: pixels
- what: the Python raster engine that reads COGs and renders them — the core TiTiler is built on
- ds-role: created
- island: cng
- integration: bakes mosaic postcards straight from the COGs, no tiler in the path
- speaks-to: COG + HTTP range requests, TiTiler
- link: https://github.com/cogeotiff/rio-tiler
- when: mosaic_scenes

## Cloud object storage
- stage: pixels
- what: the cloud buckets the pixels actually live in
- ds-role: uses
- integration: every range request bottoms out here
- speaks-to: COG + HTTP range requests
- link: https://registry.opendata.aws
- when: catalogs | terrain

## MapLibre GL
- stage: draw
- what: the open-source WebGL map renderer
- ds-role: uses
- island: dib
- integration: draws every interactive artifact — layers, swipe compares, 3D terrain
- speaks-to: TiTiler, OpenFreeMap, AWS Terrarium terrain
- link: https://maplibre.org
- when: maplibre

## OpenFreeMap
- stage: draw
- what: free vector basemap tiles with no API key, built from OpenMapTiles
- ds-role: uses
- integration: the light basemap under every 2D map and snapshot card
- speaks-to: OpenStreetMap, MapLibre GL
- link: https://openfreemap.org
- when: maplibre & !terrain

## OpenStreetMap
- stage: draw
- what: the volunteer map of the world
- ds-role: uses
- integration: the data under every basemap here, the raster tiles under 3D scenes, and the gazetteer behind Nominatim
- speaks-to: OpenFreeMap, Nominatim
- link: https://www.openstreetmap.org
- when: maplibre | geocoded=nominatim

## Playwright
- stage: draw
- what: headless Chromium
- ds-role: uses
- integration: a snapshot card is the real map photographed, pixels embedded so nothing in it expires
- speaks-to: MapLibre GL
- link: https://playwright.dev
- when: snapshot

## eo-predictor
- stage: orbit
- what: satellite pass prediction — when each Earth-observation constellation next flies over a place
- ds-role: created
- integration: next_pass follows its prediction approach and uses its constellation definitions (swath, orbit ids), so "when's the next look" is answered the way it answers it
- speaks-to: Celestrak, Earth Search
- link: https://github.com/developmentseed/eo-predictor
- when: passes

## Celestrak
- stage: orbit
- what: the public source of live orbital elements (TLEs) for every satellite
- ds-role: uses
- integration: every pass prediction starts from a TLE fetched here
- speaks-to: eo-predictor
- link: https://celestrak.org
- when: passes

## Zarr
- stage: data
- what: chunked, cloud-native N-dimensional arrays — the format of forecast and reanalysis grids that STAC and COGs do not carry
- ds-role: contributes
- island: cng
- integration: the curated stores (GFS, HRRR, ERA5, MUR) read by `compute_statistics` and drawn by titiler.xyz `/zarr/tiles`. New dep `zarr`: it opens the stores; the format has no lighter reader
- speaks-to: xarray, obstore, TiTiler
- link: https://zarr.dev
- when: collection=noaa-gfs-forecast,noaa-hrrr-forecast,nasa-mur-sst,weatherbench2-era5

## xarray
- stage: data
- what: labelled N-dimensional arrays — selects a variable, a time and a bbox out of a Zarr store
- ds-role: uses
- integration: the nearest-time `sel` and the clip-and-reduce behind a Zarr statistic. New dep `xarray`: titiler.xarray reads stores through it, so the statistics and the tiles agree on what a selection means
- speaks-to: Zarr, obstore
- link: https://xarray.dev
- when: collection=noaa-gfs-forecast,noaa-hrrr-forecast,nasa-mur-sst,weatherbench2-era5

## obstore
- stage: data
- what: Development Seed's fast object-store client for Python (Rust-backed), with a Zarr store adapter
- ds-role: created
- island: cng
- integration: reads the S3 Zarr stores (HRRR, MUR) anonymously. New dep `obstore`: it is our own tool and the one zarr's `ObjectStore` wraps. The https stores go through httpx instead, because Cloudflare-fronted hosts send compressed metadata with no Content-Length, which obstore's HTTP store rejects
- speaks-to: Zarr, Cloud object storage
- link: https://github.com/developmentseed/obstore
- when: collection=noaa-hrrr-forecast,nasa-mur-sst

## titiler.xarray
- stage: pixels
- what: TiTiler's xarray extension — web map tiles, point and statistics routes over Zarr variables, selected by `variable` and `sel`
- ds-role: created
- island: cng
- integration: titiler.xyz `/zarr/tiles` draws the stores that open there (ERA5 today); GFS, HRRR and MUR do not and say why in `tiles_note`
- speaks-to: TiTiler, Zarr, xarray
- link: https://github.com/developmentseed/titiler
- when: collection=weatherbench2-era5

## NOAA HRRR Zarr (University of Utah)
- stage: data
- what: University of Utah's hourly HRRR Zarr archive on AWS Open Data
- ds-role: uses
- integration: the hourly CONUS temperature forecast grid behind compute_statistics on noaa-hrrr-forecast
- speaks-to: Zarr, xarray, obstore
- link: https://registry.opendata.aws/noaa-hrrr-zarr/
- when: collection=noaa-hrrr-forecast

## GFS Zarr (Dynamical.org)
- stage: data
- what: NOAA Global Forecast System weather model archived in Zarr format by Dynamical.org
- ds-role: uses
- integration: the global GFS forecast grid (deprecated plain Zarr copy; Icechunk is the upgrade path)
- speaks-to: Zarr, xarray
- link: https://data.dynamical.org
- when: collection=noaa-gfs-forecast

## NASA MUR SST
- stage: data
- what: NASA Multi-scale Ultra-high Resolution (MUR) Sea Surface Temperature on AWS Open Data
- ds-role: uses
- integration: 1 km daily global sea surface temperature analysis in Zarr format
- speaks-to: Zarr, obstore
- link: https://registry.opendata.aws/mur/
- when: collection=nasa-mur-sst

## WeatherBench 2 ERA5
- stage: data
- what: Google Research benchmark dataset — ECMWF ERA5 reanalysis regridded to Zarr on Google Cloud
- ds-role: uses
- integration: global 1.5 degree atmospheric reanalysis drawn by titiler.xyz and computed by compute_statistics
- speaks-to: Zarr, xarray, TiTiler
- link: https://weatherbench2.readthedocs.io
- when: collection=weatherbench2-era5
