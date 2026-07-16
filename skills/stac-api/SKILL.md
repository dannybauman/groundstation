---
name: stac-api
description: Connect to and search any STAC API instance — discovery via the landing page and conformance classes, item search with bbox/datetime/cloud filters, CQL2 filtering, and asset-layout inspection. Instance-generic; pair with instance config (endpoint URL) and a tiler skill for pixels.
---

# STAC API — any instance

A STAC API is self-describing. Given only a root URL, discover everything else; never hardcode collection names or asset layouts from one instance into requests against another.

## Bootstrap from the root URL

1. `GET {root}/` — the landing page. `links` carry `rel=search`, `rel=data` (collections), `rel=conformance`, and often `rel=queryables`.
2. `GET {root}/conformance` — what the instance actually supports. Look for `item-search`, `filter` (CQL2), `query`, `sort`, `fields`. Don't send CQL2 to an instance that doesn't declare the filter class — you'll get 400s or silently unfiltered results.
3. `GET {root}/collections` — ids, temporal/spatial extents, and (best case) `item_assets` describing each collection's asset layout. This is where you learn whether bands are named `red`/`nir` or `B04`/`B08` — **asset naming is an instance/collection convention, not part of the spec**.
4. `GET {root}/collections/{id}/queryables` — which properties you may filter on (e.g. `eo:cloud_cover`, `sar:polarizations`).

## Searching

`POST {root}/search` with a JSON body (preferred over GET — bbox/intersects get unwieldy in query strings):

```json
{"collections": ["sentinel-2-l2a"], "bbox": [22.5, -15.7, 23.5, -14.8],
 "datetime": "2026-06-16T00:00:00Z/2026-07-16T23:59:59Z", "limit": 5,
 "query": {"eo:cloud_cover": {"lt": 20}}, "sortby": [{"field": "properties.datetime", "direction": "desc"}]}
```

- `pystac-client` wraps all of this (`Client.open(root).search(...)`) and handles pagination; use it when Python is available, raw httpx otherwise.
- CQL2 (`"filter": {"op": "<", "args": [{"property": "eo:cloud_cover"}, 20]}`, `"filter-lang": "cql2-json"`) is the standards path; the older `query` extension is more widely deployed. Check conformance for which one the instance speaks.
- Cloud filtering: `eo:cloud_cover < 20` is a sane default for optical; relax one constraint at a time (cloud, then window, then bbox) if nothing returns.
- Sort newest-first and keep `limit` small; item JSON is heavy. Request only what you'll read.

## Reading items

- `assets` is a dict of name → href + metadata. The item's `self` link is what raster services (TiTiler) take as input.
- Asset hrefs may not be directly fetchable: private buckets, **requester-pays buckets** (anonymous reads fail with AccessDenied), or signed-URL schemes (some instances require a signing step or token for the data plane even though the metadata API is open — check the provider's docs when a valid-looking href 403s).
- `proj:*` fields carry CRS/shape; `eo:bands` or the collection's `item_assets` map band semantics (which asset is NIR) — read them instead of assuming.

## Judgment

- Same physical product can appear in multiple instances under different item-id and asset-name conventions; ids are not comparable across instances.
- A tiny point-feature bbox over a region-scale question means the geocode was too literal — widen it before blaming the catalog.
- Empty result ≠ no data: check the collection's declared temporal extent before concluding a gap.
