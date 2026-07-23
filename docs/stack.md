# The stack

What is actually behind a groundstation artifact, told as components. This file is curated, not generated: it says what matters, not what happens to be installed. Artifacts join these entries with their real render parameters, so a panel describes the specific collection, tiler, and buckets on screen.

Fields per component: `kind` (data / access / tiling / viz / standard / infra), `what` (one plain line), `ds-role` (created / maintains / contributes / uses — projects, never people), `integration` (what it does in this system), `speaks-to` (edges of the web), `link`.

Heads-up for editors: component *names* are joined by code (`src/groundstation/stack.py` — the catalog map and active-name sets). Renaming a `## ` heading needs a matching edit there, and an eval checks the two stay in sync.

## STAC
- kind: standard
- what: the open catalog spec that makes satellite archives searchable the same way everywhere
- ds-role: contributes
- integration: every imagery search is a STAC POST; every layer starts as a STAC item
- speaks-to: Earth Search, VEDA, Planetary Computer, TiTiler
- link: https://stacspec.org

## Earth Search
- kind: data
- what: Element 84's open STAC catalog of Sentinel-2, Sentinel-1, NAIP, and Copernicus DEM on AWS
- ds-role: uses
- integration: the default catalog for fresh imagery searches
- speaks-to: STAC, AWS S3
- link: https://earth-search.aws.element84.com/v1

## NASA VEDA
- kind: data
- what: NASA's curated Earth science data platform — fire severity, air quality, disaster layers
- ds-role: contributes
- integration: the catalog for analysis-ready NASA products, with its own raster API
- speaks-to: STAC, Azure Blob
- link: https://www.earthdata.nasa.gov/dashboard

## Planetary Computer
- kind: data
- what: Microsoft's deep Earth archive — MODIS, Landsat, land cover, biomass, DEMs
- ds-role: uses
- integration: the catalog for historical and thematic layers, with signed-URL access
- speaks-to: STAC, Azure Blob
- link: https://planetarycomputer.microsoft.com

## TiTiler
- kind: tiling
- what: dynamic tile server — turns COGs into web map tiles on the fly, band math included
- ds-role: created
- integration: every raster layer's tile URL; expressions like NDVI run server-side
- speaks-to: STAC, COG + HTTP range requests
- link: https://github.com/developmentseed/titiler

## COG + HTTP range requests
- kind: access
- what: Cloud-Optimized GeoTIFF — imagery you can read in pieces, so nobody downloads a whole scene
- ds-role: contributes
- integration: how every pixel travels — the tiler range-reads only the bytes each tile needs
- speaks-to: AWS S3, Azure Blob, TiTiler
- link: https://cogeo.org

## MapLibre GL
- kind: viz
- what: the open-source WebGL map renderer
- ds-role: uses
- integration: draws every interactive artifact — layers, swipe compares, 3D terrain
- speaks-to: TiTiler, AWS Terrarium
- link: https://maplibre.org

## AWS Terrarium terrain
- kind: data
- what: free, keyless global elevation tiles (Mapzen legacy, hosted on AWS Open Data)
- ds-role: uses
- integration: the raster-dem source behind 3D fly-throughs
- speaks-to: MapLibre GL, AWS S3
- link: https://registry.opendata.aws/terrain-tiles

## Gazet
- kind: access
- what: a small-model geocoder that resolves place names without a heavyweight service
- ds-role: created
- integration: first stop for turning "Torres del Paine" into a bbox
- speaks-to: Nominatim
- link: https://github.com/developmentseed/gazet

## Nominatim
- kind: access
- what: OpenStreetMap's geocoder
- ds-role: uses
- integration: the geocoding fallback, plus reverse geocoding for map labels
- speaks-to: Gazet
- link: https://nominatim.openstreetmap.org

## NASA EONET
- kind: data
- what: NASA's open feed of natural events — wildfires, storms, volcanoes, floods
- ds-role: uses
- integration: the events half of "what's happening around X"
- speaks-to: GDACS
- link: https://eonet.gsfc.nasa.gov

## GDACS
- kind: data
- what: the UN/EC global disaster alert system
- ds-role: uses
- integration: disaster alert levels alongside EONET events
- speaks-to: NASA EONET
- link: https://www.gdacs.org

## Open-Meteo
- kind: data
- what: open weather API, no key required
- ds-role: uses
- integration: past and forecast weather for briefs and event context
- speaks-to: NASA EONET
- link: https://open-meteo.com

## Cloud object storage
- kind: infra
- what: the cloud buckets the pixels actually live in
- ds-role: uses
- integration: every range request bottoms out here
- speaks-to: COG + HTTP range requests
- link: https://registry.opendata.aws
