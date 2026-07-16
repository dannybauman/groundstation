# Pre-registered grading rubric (written before any run completed)

Same model, same endpoints, same tiler for both configs.
PASS requires ALL bullets; PARTIAL = answer substantively correct but an artifact/number missing; FAIL otherwise.

- Q1 Chelan: recent (≤14d) low-cloud S2 scene with date+cloud% stated; real currently-active fire(s) near Chelan identified from a live feed; shareable map with imagery + fire locations.
- Q3 Barotse SAR: S1 GRD scene found; rescale DERIVED from statistics/percentiles, not a guessed dB stretch; map renders the scene.
- Q4 Barotse NDWI: NDWI mean for a current scene AND an early-March scene, both in [-1,1], delta interpreted; two-layer (swipe/toggle) map.
- Q5 VEDA Caldor: VEDA Caldor product(s) found; burn severity layer actually renders (VEDA raster API) over a current S2 scene; severity read stated.
- Q14 Jakarta Landsat: requester-pays obstacle on earth-search Landsat either avoided (routed to PC) or recovered from; 2019-vs-now comparison produced with a real change observation.
- Q20 Munich: same physical scene matched across both catalogs despite differing id conventions; both cloud values reported; previews from BOTH catalogs (PC needs rendered_preview or signing); agreement verdict.

Secondary metrics per run: raster-call count, dead-end count (self-reported + artifacts), wall time.
Trap ledger (which config rediscovered known traps): asset->b1..bN expression merge; blank index layer without rescale; PC signing; VEDA raster routing; requester-pays; SAR DN stretch; region-scale geocode bbox.
