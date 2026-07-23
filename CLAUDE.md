# groundstation — session rules

A Development Seed labs prototype. Full architecture in `docs/architecture.md`, test conventions in `evals/README.md`, the artifact attribution model in `docs/stack.md`.

## DevSeed tools first

Before building a capability or reaching for a generic dependency, ask: **does a DevSeed tool already do this?** Prefer it, even when a generic option looks marginally easier — this project exists to show the DS/open-source stack working, so using our own tools IS the product. The family to check first: TiTiler, rio-tiler, titiler-pgstac, cogeo-mosaic/MosaicJSON, rio-cogeo, eoAPI, stac-fastapi, pgstac, Gazet, stactools, lonboard/deck.gl-raster (viz). When unsure, search the developmentseed and cogeotiff GitHub orgs before adding anything.

**And attribute it.** Any tool that ends up in an artifact's render path gets an entry in `docs/stack.md` (the curated attribution source) in the same PR — the stack layer can only credit what the file lists. Attribution is to projects, never people.

## Working agreements

- one branch + one small PR per change, evals in `evals/unit_checks.py` before the PR, eval output pasted in the PR body
- bump `.claude-plugin/plugin.json` in every PR that ships behavior — installs cache per version
- multi `-m` commits, Claude co-author trailer
- deliberate shortcuts get a `ponytail:` comment naming the ceiling and the upgrade path
- the shared titiler.xyz is a courtesy: be frugal in tests and demos, batch live checks, stop on 429
