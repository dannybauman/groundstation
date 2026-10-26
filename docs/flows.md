# Five flows the new tools make possible

Each flow was run live on version 0.20.0 and is shown, with its verbatim output and what went wrong, in [field-test-2026-10-25.html](field-test-2026-10-25.html).

## 1. Tomorrow's temperature in Laredo, Texas

Question: what will the temperature be there tomorrow?

1. `geocode("Laredo, Texas")`
2. `search_datasets("temperature forecast", place="Laredo, Texas")`
3. `compute_statistics(catalog="zarr", collection_id="noaa-hrrr-forecast", item_id="TMP", bbox=<Laredo>, sel={"valid_time": "..."})`

Comes back: the HRRR store, with min, max and mean over the city's box. Honest about: the answer is in kelvin, GFS is listed with statistics off because that copy is deprecated, and HRRR covers the contiguous US only.

## 2. What covers Efate at 10 m or finer

Question: which collections cover the island under 10 m?

1. `geocode("Efate")`
2. `search_datasets("sentinel", place="Efate", max_gsd=10)`

Comes back: Sentinel-2 collections with gsd 10.0, plus collections that declare no gsd. Honest about: the search matches words, so "optical imagery" returned an empty list with a note, and unknown resolution is labelled, not dropped.

## 3. The next open-data pass over Efate

Question: when could a satellite next image the island?

1. `next_pass(lat, lon, days=3)` with Efate's centroid

Comes back: five passes with constellation, UTC time, optical or radar, and an EO Predictor link. Honest about: a pass is a possible capture, not an acquisition plan, and imagery reaches the catalogs hours to a day later.

## 4. A month of Chelan County, searched offline

Question: can I keep a month of Sentinel-2 and search it with no network?

1. `write_archive("earth-search", ["sentinel-2-l2a"], place="Chelan County", datetime_range=..., out_path=...)`
2. `search_imagery(<that parquet path>, ["sentinel-2-l2a"], bbox=..., max_cloud_cover=20)` with the network cut

Comes back: 64 archived scenes, then five cloud-filtered ones from the file. Honest about: it is a snapshot, analysis still goes through the live catalog, and passing `place=` still geocodes over the network, so pass a bbox.

## 5. A Chelan County conditions read and a separate smoke frame

Question: what are conditions in the county, and is there smoke?

1. `conditions_brief(place="Chelan County")`
2. `smoke_forecast(lat, lon, hours_ahead=24)`
3. `render_map(...)` with the smoke tile layer

Comes back: dryness, wind, events, latest look and next passes, then a smoke reading now and for tomorrow. Honest about: smoke is NOAA HRRR model guidance, and `conditions_brief` does not itself include the smoke frame or write a page.
