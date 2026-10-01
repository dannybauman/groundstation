# Briefings: scheduling, delivery, local models

The briefing engine is `briefing/brief.py`. One place:

```bash
uv run briefing/brief.py --place "Chelan County, Washington" --days 10
```

A watch list, which writes a triaged morning-sweep index:

```bash
uv run briefing/brief.py --fleet briefing/fleet.json                  # + --slack-webhook <url> to deliver
```

Each place keeps a small state file in `briefing/state/`, so the next run says what changed since the last one.

## Scheduled sweeps

`briefing/run.sh` is the unattended entrypoint (unix only): it takes a lock so overlapping runs skip cleanly, appends everything to `briefing/state/run.log`, and runs the fleet. Every brief is checked by `evals/brief_checks.py` before posting. Failing briefs are withheld from the Slack message and the post says "N of M areas, K withheld by checks" rather than pretending. A CALM day after a WATCH day says so in the brief, guaranteed, not just prompted.

Dry-run first, then schedule:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/... bash briefing/run.sh --slack-dry-run
```

cron (every morning at 7):

```
0 7 * * * SLACK_WEBHOOK_URL=https://hooks.slack.com/... /path/to/groundstation/briefing/run.sh
```

launchd (macOS): save as `~/Library/LaunchAgents/org.groundstation.sweep.plist`, then `launchctl load` it.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist SYSTEM "file://localhost/System/Library/DTDs/PropertyList.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.groundstation.sweep</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>/path/to/groundstation/briefing/run.sh</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>SLACK_WEBHOOK_URL</key><string>https://hooks.slack.com/...</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
```

## Local models

Brief synthesis can run on a local OpenAI-compatible endpoint (Ollama, LM Studio) instead of `claude -p`:

```bash
GROUNDSTATION_LLM=auto \
GROUNDSTATION_LOCAL_URL=http://localhost:11434/v1 \
GROUNDSTATION_LOCAL_MODEL=qwen3.5-9b \
uv run briefing/brief.py --place "Chelan County, Washington"
```

`GROUNDSTATION_LLM` is `claude` (default), `local`, or `auto`. The local path preflights the endpoint and the prompt size, and on any failure falls through to `claude -p`, then to the deterministic data-only brief. The chain never produces nothing. Synthesis is the only task that goes local: it's bounded completion work, which small models do well. The agent loop's tool-calling never will: small models tend to describe tool calls instead of making them. Reasoning and boundaries: `docs/adr-local-models.md`.
