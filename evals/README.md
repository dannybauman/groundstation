# Evals — what goes where, and how small to keep it

Two suites, deliberately minimal. The bar for every check: the smallest thing that fails if that behavior breaks. No frameworks, no fixtures, no mocks beyond a stubbed function or a temp dir.

## unit_checks.py — offline, deterministic, what CI runs

    uv run evals/unit_checks.py

- zero network, runs in seconds. If a check needs an endpoint, it doesn't belong here.
- one `t_*` function per behavior. Extend an existing check before adding a new one — a new function is for a new behavior, not a new assertion.
- plain `assert` with a message only where the failure would be cryptic.
- new tool or template feature → one check here proving the artifact/URL/HTML carries it, and one proving the default is unchanged.

## run_evals.py — live, proves the demo works right now

    uv run evals/run_evals.py

- hits real endpoints (STAC catalogs, titiler.xyz, EONET, Open-Meteo). One check per integration, not per feature — features are unit_checks' job.
- expect occasional flakes from shared services; a check that flakes weekly is testing the service, not us — loosen it or move the logic offline.

## Keeping it cheap

- offline suite stays under ~5 seconds and zero tokens. If it creeps past that, something is over-testing.
- when a bug is found: reproduce it as one offline check first, then fix. The check stays.
- field tests (`scripts/field_test.py` + `docs/*.cases.json`) are showcases, not tests — they never run in CI and don't belong here.
