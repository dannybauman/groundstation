# Field test: the ten example prompts, actually run

All ten README-style prompts were run through the real agent path (headless Claude + the groundstation MCP server + the earth-data skill) on 2026-07-09. Every one produced a correct answer and a working map. Below: what each did, and what it shows.

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

## Improvements this run produced

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
