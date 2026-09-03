---
name: groundstation
description: Run, demo and operate groundstation without remembering a command. Start or check the web console, open the field tests, run an Earth brief or the fleet sweep, run the doctor, hit the console API, prep a demo room. Use when the user says "run groundstation", "demo groundstation", "is groundstation running", "open the field tests", "brief me on <place>", "morning sweep", or wants to try groundstation from the command line. Answering an Earth question with the MCP tools is the earth-data skill, not this one.
---

# groundstation, operated

Everything runs from the checkout that `~/.claude.json` `mcpServers.groundstation` points at (normally `~/Source/groundstation`). That is also the copy Claude launches as the MCP server. Do the thing, then say where the output is. Never hand the user these commands to type.

## Which door

| They want | Do |
|---|---|
| "is it running" / "get it running" | doctor, console up, say what is connected |
| a demo | the demo checklist |
| "brief me on X" / "what is Earth saying about X" | a brief |
| "morning sweep" / "run the fleet" | the fleet sweep |
| "open the field tests" | every `docs/field-test*.html` through the console, newest opened last so it is the active tab |
| "try it from the command line" | one brief, two curl calls, the `claude -p` one-liner |

## Commands

- **Doctor** (first, about 20 s): `bash scripts/doctor.sh`. One line per link in the chain (uv, server env, CLI, MCP wiring, endpoints, browser, version) with the fix for the first broken one. "checkout differs from origin/main" is expected on a branch.
- **Console:** `lsof -nP -iTCP:8765 -sTCP:LISTEN` first. If empty: `nohup uv run --group web groundstation-web > /tmp/groundstation-web.log 2>&1 < /dev/null &`, then poll `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/` until 200. Poll, never flat-sleep. It serves `/` (the console), `/docs/…` and `/demo/…`, so field tests and artifacts open with their maps.
- **Open a page:** `open -a "Google Chrome" "http://127.0.0.1:8765/docs/<page>.html"`. `file://` also works when the console is down, the pages are self contained.
- **A brief:** `uv run briefing/brief.py --place "<place>" --days 10`. Two to four minutes, Claude writes the synthesis through `claude -p`. Output `demo/brief-<slug>-<date>.html` plus `.md`, `.data.json` and `brief-map-<slug>-<date>.html`. Open the HTML. Per-AOI memory lives in `briefing/state/`, so a second run says what changed since the first.
- **Fleet sweep:** `uv run briefing/brief.py --fleet briefing/fleet.json` writes `demo/morning-sweep-<date>.html`. `--slack-dry-run` prints the Slack payload, `--slack-webhook <url>` delivers it.
- **Console API**, the same functions the MCP server exposes: `curl -s "http://127.0.0.1:8765/api/geocode?q=Lisbon"`, also `/api/scenes?q=&days=`, `/api/events?q=`, `/api/weather?q=`, `/api/ndvi`, `/api/compare`, `/api/artifacts`. POST `/api/brief`, `/api/ask`, `/api/insight` return a job, poll `/api/jobs/{id}`.
- **Headless Claude:** `claude -p "make me a swipe map of NDVI over <place>, this month versus last"` from the repo.
- **A field test page from cases:** `uv run scripts/field_test.py docs/<name>.cases.json`. Running the cases honestly is the earth-data skill's job.

## Demo checklist

1. `git branch --show-current` and `gh pr list`: say which branch the room will see and which PRs are still open.
2. Doctor green. titiler.xyz or earth-search unreachable is the conference wifi failure, say it before the room fills.
3. Console up, field tests open with the newest active, one fresh brief for the host city so a today-dated page exists.
4. The MCP server is whatever was checked out when the Claude session started. After a branch switch or a pull: restart Claude Code, then `/mcp` should list groundstation connected. Tools surface about 10 s after start.
5. The shared tiler rate-limits (429) under a room full of scans. Fallback: `docker compose up` (compose.yml) and `GROUNDSTATION_TITILER=http://localhost:8000`.

## Say

Paths, not commands. "Brief for Lisbon at demo/brief-lisbon--portugal-2026-09-03.html, opened." One line on anything not running and why.
