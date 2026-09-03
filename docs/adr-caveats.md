# ADR: caveats — a derived number carries the conditions that invert it

Status: accepted, September 2026

## Context

Field test №7 went looking for the failure mode that matters most to an agent consumer and found it: `compare_dates` reported NDVI down 87.8% over Chilika Lake during the monsoon, the season Odisha greens. The number was arithmetically right. The after scene was 51.7% cloud and the baseline mean was 0.06, so a small absolute change read as a large percentage. Both facts were already computed and neither travelled with the number.

The fix already existed in this codebase several times over. `next_pass` returns a `note`, `compute_statistics` an `extent_note`, `geocode` a `bbox_note`, `conditions_brief` a `note`, and the renderers a `coverage_note`. `compare_dates` returned a bare number. The convention was a habit, applied unevenly. This page makes it a rule a fresh session can follow without reading `tools.py`.

## Decision

**Trigger.** A tool returns a caveat when a caller could act on a returned value without knowing a condition that inverts or empties its meaning. Derived numbers (a mean, a delta, a percentage, a coverage figure) are the usual case. Raw passthrough is exempt: `weather_summary` reports what Open-Meteo said and adds nothing to it.

**Shape.** Plain sentences on the same return object as the value, under the key the tool already uses (`note`, `extent_note`, `bbox_note`, `coverage_note`, `caveat`). One sentence per condition. The key is **omitted entirely when nothing applies**, never an empty list or an empty string, so its presence is the signal. A caveat is a list of plain strings or one plain string, never a nested structure and never a new dependency.

**Register.** Written for a small local model with no extra reasoning: no jargon, and it names the **direction** of the error, not only its existence. The worked example is the cloud sentence in `_delta_caveats`: *"cloud pushes a normalized index toward zero."* A caveat that says only "may be unreliable" has not done its job.

**Thresholds** are named module constants with a comment naming the tradeoff (`CLOUD_CAVEAT_PCT`, `FLAT_BASELINE`, `PARTIAL_COVERAGE_PCT`, `RECOMMEND_MAX_CLOUD`), never inline literals. They are calibration knobs and will be tuned against real scenes.

**Absence is a fact, and a ranking is a decision.** An empty result is the return most likely to be narrated into a confident falsehood, so it says what was searched: `search_datasets` returns one record naming the catalogs it checked. A ranked list has already chosen for the model, so it also returns the axis it did not sort on: `search_imagery` returns `recommended` with the reason, because the newest scene may clip the area and the clearest may see a fifth of it.

## Audit, September 2026

| Tool | Derived value | Caveat |
|---|---|---|
| `geocode` | bbox | `bbox_note` when the geocoder returned a point and the box was widened |
| `search_datasets` | hits | one record with `searched` and `note` when nothing matched |
| `search_imagery` | ranking | `recommended` with a reason, `covers_aoi_pct` on every item |
| `compute_statistics` | mean and friends | `extent_note` when unclipped |
| `compare_dates` | delta, delta_pct | `caveat` list: cloud on either scene, near-zero baseline |
| `render_map`, `render_map_3d` | coverage_pct | `coverage_note` below `PARTIAL_COVERAGE_PCT`, or when no imagery layer exists |
| `next_pass` | pass times | `note`: swath geometry from TLEs, a pass is a possible capture |
| `conditions_brief` | signals | `note`: conditions from live sources, not a prediction |
| `weather_summary` | none, passthrough | exempt; `elevation_note` only when the grid cell sits high |
| `preview_item`, `tile_url_template`, `render_postcard`, `active_events`, `list_catalogs`, `describe_collection` | none | exempt, no derived number |

## Consequences

A new tool decides the question from this page. Every caveat lands with the smallest offline check that fails if it breaks, in `evals/unit_checks.py`, and a story that changes a returned number re-pins every downstream number in the same PR, field-test cards included.

The risk is caveat fatigue: a caveat on every number trains callers to skip all of them. That is why thresholds are tuned constants and a clean return carries no key at all.

## Not the caveat's job

A caveat saying "this geocode came from OpenStreetMap" is honest and still the wrong geocoder. Honesty about a number does not substitute for getting the right number.
