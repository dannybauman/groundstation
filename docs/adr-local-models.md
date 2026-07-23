# ADR: local models — completion-tier synthesis only

Status: accepted, July 2026

## Context

We want groundstation to use local models when possible and reliable for certain tasks — cheaper scheduled runs, and a path for supply-chain-constrained deployments where frontier APIs aren't an option. The discipline comes from the integrating-local-models skill's rung ladder: rung 0 needs no model at all (a lookup or a rule), rung 2 is single-shot completion (safe on small local models), rung 3 is structured tool-calling, where small models often emit the call as prose instead of executing it — a silent, confident failure. DS strategy adds a boundary: right-size models for our own tasks, don't build general AI-tooling infrastructure.

Most groundstation tools are rung 0 — deterministic code over STAC and TiTiler that needs no model of any size. The agent loop is rung 3. The one rung-2 task in the repo is brief synthesis: a bounded prompt in, markdown out.

## Decision

Local models get exactly one job: `synthesize()` in `briefing/brief.py`. The engine chain is local (when configured and preflighted) → `claude -p` → the deterministic data-only brief, controlled by three env vars (`GROUNDSTATION_LLM`, `GROUNDSTATION_LOCAL_URL`, `GROUNDSTATION_LOCAL_MODEL`), defaulting to exactly the pre-existing behavior. Preflight is mandatory — endpoint reachable within 2s, prompt within the context budget — and any failure means decline and fall through, never a hang and never a guess. Locally-synthesized briefs pass the same `evals/brief_checks.py` gate as cloud briefs. The golden dataset for judging a local model already exists: the real captures in `demo/*.data.json` plus those checks.

No local tool-calling, no model router, no per-tool model selection.

## Consequences

Scheduled sweeps can run without frontier API spend when a local endpoint is up, and "reliable" is measured by the eval gate rather than asserted. The ceiling is explicit: any future local task gets its own rung classification and its own eval gate, one change at a time.

## Revisit when

A laptop-class model passes real agentic tool-calling probes reliably, or a restricted deployment needs more than synthesis to run disconnected.
