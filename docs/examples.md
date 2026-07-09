# Field test: twenty example prompts, actually run

Twenty prompts across two rounds were run through the real agent path (headless Claude + the groundstation MCP server + the earth-data skill) on 2026-07-09. **20/20 produced correct answers and working maps.** Each round's misses became fixes the same day — the field test is the product loop.

There's a visual walkthrough of all twenty at [`field-test.html`](field-test.html) (serve the console and open `/docs/field-test.html`).

## Round 1: the core workflows

| # | Prompt (short) | What happened | What it demonstrates |
|---|---|---|---|
| 1 | What's burning near Lake Chelan? | Found both active wildfires (Navarre Coulee, Chelan Hills), picked the 0.7%-cloud July 5 scene over a 20%-cloud newer one, mapped fires as markers on imagery | live events + scene judgment |
| 2 | Wenatchee NDVI, now vs last July | Same MGRS tile both years, both near-cloud-free: 0.626 vs 0.622 — called it "noise-level, not a real vegetation change" | honest year-over-year comparison |
| 3 | Barotse in the wet season — use radar | Chose Sentinel-1, ran statistics first and derived the rescale (30–380) from actual percentiles instead of guessing a dB stretch, flagged the swath seam | sensor reasoning + self-calibration |
| 4 | Surface water now vs March (NDWI) | NDWI −0.29 → −0.48, a 64% relative decline, correctly read as the expected seasonal Zambezi drawdown, swipe map | index math + domain interpretation |
| 5 | VEDA on the Caldor fire | Found both VEDA products, read the categorical severity stats (majority class 3, moderate), layered 2021 severity over a current 0.1%-cloud scene | cross-catalog: NASA products over fresh imagery |
| 6 | Des Plaines NAIP + WorldCover | Mosaicked 7 NAIP tiles (all 2023-07-10) plus 2 WorldCover tiles split at the 42°N seam, as toggleable layers | multi-tile mosaics, cross-catalog overlay |
| 7 | Rio Grande flood alerts | No active alerts, but flagged the real signal: Del Rio and Eagle Pass tracking toward a 40–60mm event July 14–15 — "the stretch most likely to generate a flood watch" | forward-looking synthesis across feeds |
| 8 | Yirgacheffe coffee, January vs now | Widened a stadium-point geocode to the growing zone itself, then refused to trust its own number: July's best scene is 53% cloud, so "trust the direction, not the magnitude — or switch to Sentinel-1" | epistemic honesty about data quality |
| 9 | Brief me on Efate, Vanuatu | Clean alert read (one distant Green-level quake, correctly dismissed), dry-season weather, stitched two adjacent tiles to span the island | multi-tile AOIs, alert triage |
| 10 | Ashburn data center corridor | Newest fully-covering NAIP (2021) + current Sentinel-2, NDVI mean 0.38 with std 0.30 read as "the signature of tree cover mixed with rooftops and pavement," caveated NAIP's age | resolution tradeoffs, distribution-not-just-mean reading |

## Round 2: different question types, partner-shaped

Deliberately different shapes — other spectral indices, global triage, claim verification, terrain, dataset discovery, catalog trust — each mapped to a real partner context (water security, humanitarian response, marine, urban, EUDR compliance, catalog operators).

| # | Prompt (short) | What happened | What it demonstrates |
|---|---|---|---|
| 11 | Snow left in the North Cascades vs early May (NDSI) | Snow-covered fraction via the standard 0.4 threshold: 27.6% → 5.9% ("a 4.7× drop"), and explained May's lower valid-pixel share as terrain shadow from low sun angle | derived metrics, not just means |
| 12 | Most severe disaster alerts worldwide right now | Found GDACS's only Red alert (Super Typhoon Bavi), mapped today's Sentinel-2 through the typhoon itself, demoted a chronic drought below fast-moving events | global triage without an AOI |
| 13 | Wadden Sea flats — what can one scene say about tide state? | Textbook honesty: flats were exposed at 11:06 UTC, but one scene has "no time derivative and no absolute datum" — named Rijkswaterstaat gauges as the missing reference | knowing the limits of the data |
| 14 | Jakarta's coastline, 2019 vs now, with Landsat | Hit AccessDenied on earth-search's requester-pays Landsat bucket, recovered by switching to Planetary Computer's copy, then spotted the Tanjung Priok port expansion in the swipe | graceful failure + real change found |
| 15 | Verify a client's "significant greening" claim near Ouarzazate | +0.008 NDVI spread uniformly across the whole distribution including bare rock, 98th percentile flat: "more consistent with interannual variability… the data don't support 'greened significantly'" | evidence-based claim verification |
| 16 | Icelandic volcanic activity + ash toward Reykjavik? | No eruption — verified the feed wasn't down (it had flagged Etna), imaged the Sundhnúkagígar fissures anyway, declined to fabricate an ash analysis, and reported that wind *direction* wasn't in the weather tool | refusing to manufacture an answer |
| 17 | Elevation context for a Cologne flood story | Copernicus DEM (4 tiles) + Rhine imagery; separated the bbox artifact (240m foothills at the corner) from the real answer (city core 37–65m) | terrain + spatial-artifact awareness |
| 18 | Nighttime lights or settlement data for Nairobi urbanization? | Surveyed VEDA's event nightlights, GRDI, HREA, io-lulc across catalogs, argued lights "saturate over a dense core" and swiped io-lulc built-up 2017 vs 2023 instead | dataset discovery with an opinion |
| 19 | Burn severity for the Navarre Coulee fire (NBR) | Pre/post dNBR with the area-wide dilution explained, smoke-vs-cloud caveat, and "the burn may still be spreading past what this scene captures" | post-fire assessment, honestly caveated |
| 20 | Same Munich scene in two catalogs — do they agree? | Exact agreement (0.606% cloud both), explained the differing ID conventions trace to the same ESA product | cross-catalog metadata trust |

## Improvements round 2 produced

1. **Landsat routing codified**: earth-search's Landsat is requester-pays and won't tile — the skill and catalog notes now route Landsat to planetary-computer (example 14's agent had to discover this; the next one won't).
2. **Wind direction added to `weather_summary`**: example 16's ash question exposed that the tool returned speed only — dominant daily direction now included, which smoke/ash/plume questions need.
3. **NBR and NDSI recipes** joined NDVI/NDWI in the skill's index list, with the snow threshold (example 11 and 19's agents derived these themselves).

## Improvements round 1 produced

Field-testing is the product loop — these went straight back into the code:

1. **Swipe vs overlay disambiguation.** Example 5 exposed that "exactly two rasters → swipe" was wrong for severity-over-imagery. Maps now swipe only when both rasters are the *same collection* (a true comparison); different collections stack as overlays. Explicit `compare=` overrides.
2. **Overlay opacity guidance** in the skill (thematic layers carry `opacity` ~0.75 so imagery shows through).
3. **Sentinel-1 recipe codified** in the skill: GRD assets are digital numbers, not dB — derive the stretch from statistics percentiles (example 3's agent figured this out itself; now it doesn't have to).
4. **Region-scale geocoding guidance**: sanity-check the bbox size against the question's scale (example 8's agent recovered from a stadium-point geocode on its own; the skill now teaches it upfront).
5. **`compare_dates` accepts a `label`** so bbox-only comparisons stop titling their maps "AOI".

## Repro

```bash
claude -p "<prompt>. Finish with the map path as MAP: <path>." \
  --mcp-config '{"mcpServers":{"groundstation":{"command":"uv","args":["--directory","<repo>","run","groundstation"]}}}' \
  --strict-mcp-config --allowedTools "mcp__groundstation" \
  --append-system-prompt "$(cat skills/earth-data/SKILL.md)"
```
