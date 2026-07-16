# RFC: agent-ready geo services — federation, modularity, and self-documentation

*Response to the "how do we best make eoAPI (and other services) agent-ready" thread. Status: draft, with a measured experiment behind it.*

## The question, decomposed

The feedback poses two questions: does a central federating MCP service scale, and should there instead be a per-service MCP/skill (one for STAC APIs, one for TiTiler, …) — plus, is a protocol needed to make services self-documenting for agents beyond Swagger and docs pages?

groundstation, as built, blends three different kinds of knowledge that are worth naming, because the modularity question resolves differently for each:

1. **Service-adapter knowledge** — how to drive *any instance* of a spec-compliant service. pystac-client and CQL2 filtering, conformance-class discovery, TiTiler URL syntax, the `assets→b1..bN` expression merge, rescale/colormap recipes, "statistics before you guess a SAR stretch." Generic, spec-level, stable. **This modularizes cleanly at the service level.**
2. **Instance knowledge** — which endpoints exist, what content they carry, and each deployment's quirks: earth-search's Landsat/NAIP sit in requester-pays buckets; Planetary Computer signs assets through its own data API and names Sentinel-2 bands `B04`/`B08` where earth-search says `red`/`nir`; VEDA layers usually want `cog_default` plus a rescale from the collection's `renders` metadata. **This belongs with the instance — served by it, ideally (see self-documentation below).**
3. **Cross-service judgment** — the knowledge that only exists *between* services: "earth-search Landsat won't tile on your tiler → use Planetary Computer's copy of the same product"; `compare_dates` (same-tile scene matching across two search windows + index delta + swipe map); routing each catalog to the raster backend that can actually render it. **This is the part a per-service skill cannot hold, by construction** — no TiTiler skill can know that another catalog has a tileable copy of the dataset that just 500'd.

So the answer to "does the central approach scale?" is: the *adapter* layer shouldn't be central (it's now split out as `skills/stac-api/` and `skills/titiler/`, instance-generic and endpoint-parameterized), but the *federation* layer — cross-catalog routing, recovery paths, composition tools — is the actual value of groundstation and has no per-service home. The experiment below measures what happens when an agent gets only the modular layers.

## Experiment: central federation vs. modular per-service skills

Two agent configurations answered the same field-test questions against the same live services and the same self-hosted tiler:

- **Config A (central)**: groundstation's tools + the `earth-data` skill — the current architecture.
- **Config B (modular)**: no groundstation code. Generic HTTP + pystac-client, the two per-service skills (`stac-api`, `titiler`), and instance config (endpoint URLs only) — the proposed modular architecture, made concrete.

Questions were chosen to cover the trap categories the 30-question field test surfaced: baseline search→map flow, index math, VEDA raster routing, requester-pays recovery (NAIP, Landsat), cross-catalog product identity, and SAR rescale discipline.

### Results (run 2026-07-16, model: opus, shared tiler titiler.xyz, single run per cell)

**Headline: both configs passed 6/6 on the pre-registered rubric.** With a strong model, per-service skills plus already-self-describing services were *sufficient for correctness* on every question — including the trap questions. The teammate's modular hypothesis holds up better than this repo's architecture doc assumed. The differences are in cost, durability, and where knowledge got rediscovered:

| Metric | Central (tools + earth-data skill) | Modular (HTTP/pystac-client + per-service skills) |
|---|---|---|
| Rubric outcome | 6/6 pass | 6/6 pass |
| Total wall time (self-reported) | ~45 min | ~79 min (~1.75×) |
| Dead-ends hit | 15 substantive | 30+ substantive |
| Known traps encountered | 0 (absorbed by tools/skill) | requester-pays ×2, PC signing rediscovered independently 3×, VEDA pagination near-miss, VEDA raster routing |
| Map artifacts | durable tile URLs (PC mosaic registration) | 3 of 6 maps carry ~1-hour SAS tokens — perishable, need a re-sign script |

Trap ledger, the detail that matters:

- **Requester-pays**: modular hit it live both times it applied (Landsat q14: burned a raster call on the 500; Sentinel-1 q3: avoided the wasted call because the *titiler skill's* requester-pays note plus the `s3://` scheme was decisive). Recovery was impressive — both runs rerouted to Planetary Computer copies and self-signed SAS tokens. But note what that means: the recovery *was* cross-service judgment, reinvented from scratch, per question.
- **PC signing** was independently rediscovered three times (q3, q14, q20) — each run paid the discovery cost the federation layer pays once. And the resulting maps embed ~1h tokens, where the central `render_map` goes through PC's mosaic registration and produces durable URLs. Artifact durability turns out to be a federation-layer property.
- **VEDA pagination near-miss (q5)**: modular's `GET /collections` returned the default 10 of 247 collections and the run *nearly concluded VEDA had nothing on Caldor* — the closest any run came to a wrong answer. The central `search_datasets` handles pagination invisibly.
- **Both configs share the analytic failure modes** — clearest-scene-doesn't-cover-the-AOI (both q1 runs caught the 0.0007%-cloud tile that misses the lake), same-orbit scene matching for honest comparisons (both q4 runs), region-scale geocode bboxes. This judgment sits *above* both layers; it's what `compare_dates` encodes procedurally and what no OpenAPI/conformance document will ever carry.
- **Modular's freedom paid off once**: unconstrained by our `active_events` tool, the q1 run pulled NIFC/WFIGS ArcGIS feeds and returned *richer* fire data than EONET (containment %, same-day new starts). Curation cuts both ways.
- **The experiment audited our own skill**: central q14 found the earth-data skill's Landsat `rescale="0,0.3"` guidance is wrong for PC's `landsat-c2-l2` (unscaled uint16 DN, and the C2 offset doesn't cancel in normalized differences). Fix queued.

Caveats: n=6 questions, one model, one run per cell, self-reported call counts.

And the one that matters most: **the modular config's 6/6 depends on the model already knowing these services.** When it hit Planetary Computer's auth wall, it recalled the SAS signing endpoint from training data — neither the skills nor PC itself supplied it. Earth Search, Planetary Computer and VEDA are famous enough to have been memorized; a private eoAPI deployment is not, and there a modular agent has nothing to fall back on. The experiment therefore measures modularity under best-case conditions, and the untested case — an instance the model has never seen — is precisely the one the self-documentation layer below exists to serve.

**Reading of the result**: modularity is viable for correctness *today, on famous services, with a frontier model*; the federation layer's value is efficiency (~1.75× wall time), durability of artifacts, absorbed failure modes, and composition — not gatekeeping correctness. That reframes groundstation: less "the only way in," more "the amortized fast path plus the cross-service knowledge that has no other home."

## Self-documentation: what exists, what's missing

Survey of the July 2026 landscape (full sourcing in the thread; headline findings):

- **Capability discovery is already solved — natively — for our stack.** STAC/OGC APIs self-describe their *shape*: landing page with `conformsTo` and typed link relations, `/conformance`, `/collections/{id}/queryables` for CQL2-filterable fields, OpenAPI via `service-desc`. eoAPI bundles three services that all do this. No new protocol needed at this layer.
- **OpenAPI→MCP auto-generation is a documented trap, not a shortcut.** FastMCP's own author argues auto-converting REST APIs "poisons your agent": tool sprawl, parameter overload, multi-round-trip atomicity, and — the recurring phrase — *structure without strategy*. Speakeasy and TrueFoundry publish the same conclusion: curated beats generated. The curation step IS the judgment layer.
- **Existing STAC MCP servers are thin wrappers.** The most active community one exposes ~8 endpoint-shaped tools and deliberately encodes no routing logic or parameter recipes; the public discussion sits at "MCP is coming" altitude.
- **But STAC's `render` extension is already a machine-readable usage-judgment layer — and it works.** Verified live: `openveda.cloud`'s `caldor-fire-burn-severity` declares `stac_extensions: ["render", "item-assets"]` and serves `{"dashboard": {"assets": ["cog_default"], "rescale": [[0,5]], "colormap_name": "inferno_r"}}`. This is exactly the kind of judgment no OpenAPI document carries — *which asset, what stretch, which colormap* — and it is already standardized, already deployed, and already consumed: the modular q5 run read those render parameters and used them verbatim. Any claim that "nothing serves usage judgment today" is too strong; the correct claim is that `render` covers the per-collection *render-recipe* slice and nothing covers the rest.
- **llms.txt** has ~10% adoption but near-zero crawler/agent uptake and no API semantics — as a spec it's a link list. Its value here is only as a *well-known entry point*, not as the content format.
- **Agent Skills (SKILL.md)** — now an open spec under the Agentic AI Foundation, supported by ~40 products — is the one format built to carry usage judgment (recipes, edge cases, failure workarounds, progressive disclosure). **Critical gap: the spec defines no hosting or discovery convention.** Skills are local directories; "a service advertises its own skill at a well-known URL" is unpaved road.
- **The well-known-manifest lineage:** OpenAI's `/.well-known/ai-plugin.json` tried exactly this in 2023 and is dead. `agents.json` (wild-card-ai) is the closest survivor — OpenAPI plus "flows" (multi-call outcome contracts) and "links" (how to chain actions) at a well-known URL — the right *idea* for machine-readable judgment, but v0.1 with a tiny registry.

So the three layers an agent needs are: (1) capability discovery — **solved** by STAC/OGC conformance; (2) a callable tool surface — **partial**, MCP works but auto-generation is an anti-pattern; (3) usage judgment — **partly solved, and that's the useful surprise**.

The `render` extension already carries per-collection render recipes, and agents already read them. So the gap is narrower and better-shaped than "nothing exists":

| Kind of judgment | Example | Served today? |
|---|---|---|
| Per-collection render recipe | `cog_default` + `rescale=0,5` + `inferno_r` | **Yes — `render` extension** |
| Per-collection data quirks | "these bands are uint16 DN; the C2 offset doesn't cancel in a normalized difference" | No |
| Deployment quirks | "this instance has no AWS credentials → requester-pays sources 500 here" | No |
| Pipeline knowledge | search → mosaic → tiles/stats | No |
| Failure recipes | "on a 500 from an `s3://` href, try another catalog's copy" | No |
| Collection selection | "which collection answers this question" | No |

The unserved rows have a shape in common: they are prose, not parameters. That is why `render` could be standardized as JSON and the rest resists it — and why the vehicle question below is a real design choice rather than a formality.

### The prototype: the instance serves its own judgment layer

`compose.yml` now ships a `selfdoc` sidecar: `http://localhost:8001/llms.txt` is the agent entry point for the local tiler instance — what the service is, the deployment quirks OpenAPI can't express (no AWS credentials → requester-pays sources 500 here), and links to the service-level skills, served by the deployment itself.

The point of the prototype: **the missing protocol is mostly a delivery convention, not a new spec.** The judgment that `render` doesn't cover already has a portable container (a skill file); what it lacks is a well-known place for an instance to serve it. An `llms.txt` per deployment, linking to skill documents maintained alongside each service, lets an agent pointed at a bare eoAPI instance bootstrap most of what groundstation pre-installs — except the cross-service layer, which by definition no single instance can serve.

The prototype is one point in a design space, not the answer. Alternatives worth weighing:

1. **Extend the `render` precedent.** STAC has an extension mechanism, a community process, and — as verified above — a working proof that per-collection recipes belong in collection metadata. Judgment mostly binds at the *collection* level ("this Landsat is uint16 DN"), which is exactly where `render` lives. Best fit for the "per-collection data quirks" row. Fits the other rows badly: JSON schemas hold parameters well and prose poorly.
2. **Ship SKILL.md inside the package.** `pip install titiler` brings its skill; eoAPI vendors one. No serving, no URL, no new spec — distribution is the package manager you already have. Covers service-generic knowledge only: a packaged skill cannot know *your* deployment lacks AWS credentials.
3. **A curated MCP shipped with eoAPI** — hand-written, never generated. Most adopted path; costs someone a server to run and version.
4. **`agents.json` at `/.well-known/`.** Right content model (flows, links), right location, near-zero adoption — a bet on a v0.1 spec.
5. **Generate the guidance from the instance's own data.** An endpoint that derives rescales from actual holdings instead of a human writing them down. Self-maintaining and genuinely novel; also the most work, and only reaches the rows that are computable.
6. **Do nothing.** Frontier models already memorize famous services — this experiment is weak evidence for that. Fails precisely on the private instances that need it most.

**Recommendation: a split, not a single vehicle.** Package-ship the service-generic skill (2); push collection-specific recipes into the `render` lineage (1), which is where the precedent and the community process already are; reserve a well-known URL (the prototype) for the genuine residue — deployment quirks and failure recipes that are neither generic enough to package nor schema-shaped enough to standardize.

### Proposed division of labor

| Layer | Where it lives | Who maintains it |
|---|---|---|
| Service adapter (STAC, TiTiler, …) | per-service skill, shipped *in the package* | the service project (eoAPI, TiTiler upstream) |
| Per-collection recipes | collection metadata, `render` lineage | whoever publishes the collection |
| Instance quirks + failure recipes | served by the deployment (`llms.txt` → skills) | whoever operates the instance |
| Cross-service judgment + composition | a federation agent/toolset like groundstation | whoever owns the use case |

## What this means for groundstation

- Keep: the federation tools (`compare_dates`, `render_map`, per-catalog raster routing), monitoring feeds, briefing engine. The experiment reframes their value honestly: not correctness gatekeeping, but ~1.75× efficiency, durable artifacts (mosaic registration vs expiring SAS tokens), absorbed failure modes, and cross-service composition.
- Newly split out: `skills/stac-api/`, `skills/titiler/` — candidates to upstream to the service projects rather than live here.
- New: the `selfdoc` sidecar as a reference implementation of instance self-documentation — and the load-bearing case for it is *non-famous instances*, where the modular config's crutch (the model's prior knowledge of PC/earth-search) disappears.
- Queued fixes the experiment surfaced: earth-data skill's Landsat rescale guidance is wrong for PC's `landsat-c2-l2`; consider exposing an optional histogram in `compute_statistics` (its stripping forced a workaround in central q4 that modular didn't need); consider an `active_events` note that US wildfire questions are better served by NIFC/WFIGS.
- Reproduce: `bash evals/modularity-exp/runner.sh`, grade with `collect.sh` against `rubric.md` (pre-registered before any run completed).
