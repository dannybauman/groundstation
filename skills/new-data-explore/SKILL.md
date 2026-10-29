---
name: new-data-explore
description: A new Earth dataset, model or tool was just released; find out what it really is, reach it, look at it on a map, and write down what was learned. Use when the user drops a release link or name ("Google put out a forest map", "try the new OlmoEarth embeddings", "explore this dataset", "a new model dropped, what can we do with it"), or asks whether groundstation can use a dataset it does not know yet.
---

# Explore a new release

A release post says what a dataset is. This skill finds out what it takes to actually use it, and whether groundstation can. The finished run leaves 3 things: a map, a short findings block, and any trap it hit added to "Traps seen" below so the next run starts smarter.

Pair with `earth-data` for the tools themselves; this skill is the order of moves around them.

## 1. Resolve the release

Read the announcement and its links until you can answer, in one line each:

- what it is (a map, embeddings, a model, a tool), who made it, its licence
- resolution, coverage, years, classes or bands, and the nodata value
- **where the data lives and in what format**: a STAC collection, a Zarr store, COGs on a bucket, zipped tiles on a download site, Earth Engine only, Hugging Face. This decides everything after it
- the date of the release, so the write-up says how fresh it is

Prefer the dataset's own README, AGENTS.md or data card over the blog post; stores built for agents often say how to read them. A plain `curl` with the HTML stripped is enough for a blog post when a scraping service is unavailable.

## 2. Is groundstation already able to reach it?

Try the tools before writing any code: `search_datasets` with the release's name and a `bbox`, then `describe_collection`. A hit means the standard `earth-data` flow works and the rest of this skill is a quick check. No hit is the interesting case: name which route is missing (not in a catalog, not tileable, needs an account) and keep going by hand, using the stack's own libraries (rio-tiler, rasterio, zarr, obstore, pystac-client are already in groundstation's environment).

## 3. Probe small before big

Never start with a bulk download. Climb, and stop at the first rung that answers the question:

1. **Headers and metadata**: a HEAD request for size, the Zarr group's attrs, a COG's header (`rio info` over `/vsicurl/`), a STAC item's assets
2. **One pixel or one chunk**, timed, so you know what a window will cost
3. **One small window** over the place you care about
4. **The smallest whole file** when nothing is range-readable

Ask before any download in the hundreds of megabytes. Delete downloads when the run ends. Recipes for each rung: [references/probes.md](references/probes.md).

## 4. Check before trusting a number

- **Nodata**: read it from the file and confirm a known-empty area (sea, outside coverage) comes back as nodata, not as a class. An argmax over a nodata value invents a class
- **Orientation and CRS**: check the transform's sign and the CRS before merging tiles; reproject each tile onto one grid rather than mosaicking by bounds
- **Units**: quantized or scaled values need their decoding rule from the data card; embeddings have no physical units at all
- **Agreement with something known**: compare a class share or value against imagery of the same place before reporting it

## 5. Look at it

Put it on a `render_map` over imagery of the same place (`search_imagery` for a clear Sentinel-2 or NAIP scene). A GeoTIFF you downloaded goes on as a `file` layer (`path`, plus `colormap` for classes) and its nodata stays clear; a public COG goes on through a TiTiler `/cog` tile URL as a `raster` layer. Vectors go in as `geojson` layers, which are filled by default. Say plainly what you had to do by hand to get it on the map; that is a groundstation gap.

## 6. Look for a pairing

Check what else came out around the same time, or what groundstation already reaches, that answers a question neither can alone. A map with labels plus an embeddings release is the classic pair: labels in, a classifier on the embeddings, a map for years the labels do not cover. The pairing is often the most useful result of a run.

## 7. Write it down

End with a findings block the user can keep or hand on:

```
Release: <name>, <who>, <date>, <licence>
Access: <where and how, one line>; groundstation reach: <yes / partly / no, and what is missing>
Look: <the map path>; what it shows in one line
Traps: <what would have given a wrong answer>
Pairing: <what it combines with, and the result>
Gaps: <groundstation changes this suggests, each naming the existing open-source tool that would close it>
```

Then add any new trap or access route to "Traps seen" below in a small PR, so the skill improves with every run. Keep the entries general (what kind of data, what went wrong, how to check), never a private project or partner.

## Traps seen

- **Ocean as nodata 255 read as a class** (ForTy, 2026-10): a per-class probability raster used 255 for nodata, so an argmax over it called the sea primary forest. Mask nodata first
- **South-up tiles** (ForTy, 2026-10): GeoTIFFs stored with a positive y pixel size; merging by bounds silently returned nothing. Reproject each tile onto a north-up grid
- **Signed links that expire in seconds** (figshare, 2026-10): the download URLs cannot be handed to a tiler. Download one tile, probe it, delete it
- **Per-UTM-zone groups** (OlmoEarth Zarr, 2026-10): one group per zone, each in its own CRS. Pick the zone from the longitude, `utm{int((lon + 180) // 6) + 1:02d}`, then transform the point into `proj:code`
- **Quantized embeddings** (OlmoEarth, 2026-10): int8 with -128 as nodata and a signed square-root encoding; decode as `(abs(q) / 127.5) ** 2 * sign(q)` before any distance or mean
- **Time series only on Earth Engine** (ForTy, 2026-10): the public download held one year. Say what the open download covers, and treat Earth Engine reads as a separate access question
- **A single huge COG on Zenodo** (global land mask, 2026-10): range reads work (206), but GDAL cannot open the `/api/records/.../content` URL. Use the `/records/<id>/files/<name>` URL with `CPL_VSIL_CURL_USE_HEAD=NO`; a 5 km window is one block, about 3 s
- **Labels with no water class** (ForTy over OlmoEarth, 2026-10): a classifier gives every pixel one of the labelled classes, so coastal water lands in a land class. Check the box against a land or water mask and add a water class to the labels
