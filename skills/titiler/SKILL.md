---
name: titiler
description: Drive any TiTiler instance — previews, XYZ tiles, pixel statistics, and band-math expressions over COGs and STAC items, with rescale/colormap recipes and the band-merge gotcha. Instance-generic; pair with a STAC skill for finding items.
---

# TiTiler — any instance

TiTiler serves dynamic tiles, previews, and statistics from COGs and STAC items. Endpoints below are relative to the instance base URL (`https://titiler.xyz` is the shared community demo; production work belongs on your own instance).

## The three endpoints that matter

- Preview: `GET {base}/stac/preview.png?url={item_self_url}&assets=visual&max_size=512`
- Tiles: `GET {base}/stac/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url={item_self_url}&assets=visual` — an XYZ template for any web map.
- Statistics: `GET {base}/stac/statistics?url={item_self_url}&assets=nir&max_size=512` — min/max/mean/std/percentiles/histogram per band.

`url` is the STAC item's `self` link. `/cog/...` variants take a direct COG href instead. Multiple `assets` params select bands.

## Expressions (band math) — the gotcha

The `/stac` endpoints merge the `assets` list into positional bands `b1..bN`. An expression must use those, with `assets` in matching order:

```
NDVI: expression=(b1-b2)/(b1+b2)&assets=nir&assets=red
```

Writing `(nir-red)/(nir+red)` directly fails or silently misreads. (Some deployments support `asset_as_band=true` to accept named assets — test before relying on it.) The same expression syntax works on `/stac/statistics`, `/stac/preview.png`, and tiles.

## Rescale and colormaps — why layers render blank

Tile/preview output is 8-bit; the data usually isn't. No `rescale` = everything clips to black or white:

- Normalized-difference indices (NDVI, NDWI, NBR, NDSI): `rescale=-1,1`, `colormap_name=rdylgn` (or `colormap_name=viridis` etc. — any rio-tiler colormap).
- Surface reflectance floats: try `rescale=0,0.3`; scaled integer reflectance: `rescale=0,3000`.
- Unknown data (SAR digital numbers, temperature, DEMs): **never guess** — run `/stac/statistics` first and stretch to roughly the 2nd–98th percentile.
- Pre-rendered RGB assets (e.g. a `visual` asset) need no rescale.

## Being a good client

- `max_size=512` (or smaller) on preview/statistics bounds the read; omitting it can pull full-resolution data.
- Shared instances rate-limit: on HTTP 429, stop retrying and back off — don't loop preview/statistics calls.
- Constrain map viewers to the item's footprint bounds so browsers don't request out-of-footprint tiles (404 storms count against rate limits too).
- The instance reads source buckets with *its* credentials: requester-pays or private buckets return 500/AccessDenied unless the deployment has AWS credentials (`AWS_REQUEST_PAYER=requester`, or `AWS_NO_SIGN_REQUEST=YES` for public buckets that need unsigned access). A working item on one instance can 500 on another — that's deployment config, not your request.
- Statistics responses include histograms; drop them before pasting anywhere token-priced.
