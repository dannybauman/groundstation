#!/usr/bin/env bash
# groundstation doctor — first-run preflight.
# Walks the setup chain in the order it actually breaks and prints the exact
# fix for the first broken link. Read-only apart from warming the venv
# (which is itself the fix for the slowest first-run stumble).
set -u
cd "$(dirname "$0")/.."

pass=0; warn=0; fail=0
ok()   { echo "  [ok] $1"; pass=$((pass+1)); }
bad()  { echo "  [XX] $1"; echo "       fix: $2"; fail=$((fail+1)); }
note() { echo "  [!!] $1"; warn=$((warn+1)); }

echo "groundstation doctor"
echo

echo "1. uv (the server launches via 'uv run')"
if command -v uv >/dev/null 2>&1; then
  ok "uv found: $(uv --version 2>/dev/null)"
else
  bad "uv is not on your PATH" "brew install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh), then rerun this script"
  echo; echo "Fix the item above and rerun — later checks depend on it."; exit 1
fi

echo "2. server env (first launch builds the venv, which looks like a hang)"
start=$(date +%s)
if uv run python -c "import groundstation.server" >/dev/null 2>&1; then
  took=$(( $(date +%s) - start ))
  if [ "$took" -ge 5 ]; then
    ok "server imports (took ${took}s — that was the one-time venv build, now warm)"
  else
    ok "server imports (venv already warm)"
  fi
else
  bad "server failed to build or import" "run 'uv sync' here and read its output, then rerun this script"
fi

echo "3. Claude Code CLI (plugins install through the CLI only — Cowork and the desktop app can't)"
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found"
else
  bad "claude CLI not found" "install Claude Code first: https://claude.com/claude-code — then: claude"
fi

echo "4. wired into Claude Code (plugin install, or an mcpServers entry)"
if ls -d "$HOME"/.claude/plugins/*/*groundstation* "$HOME"/.claude/plugins/*/*/*groundstation* >/dev/null 2>&1; then
  ok "groundstation plugin found in ~/.claude/plugins"
elif grep -q '"groundstation"' "$HOME/.claude.json" 2>/dev/null; then
  ok "groundstation found in ~/.claude.json mcpServers"
else
  note "not detected — inside Claude Code run: /plugin marketplace add $(pwd) then /plugin install groundstation@groundstation"
fi

echo "5. endpoints (conference wifi check)"
# GET, not HEAD — titiler.xyz answers HEAD with 405, any HTTP response = reachable
tiler="${GROUNDSTATION_TITILER:-https://titiler.xyz}"
if curl -s -o /dev/null --max-time 6 "$tiler/healthz"; then
  ok "tiler reachable: $tiler"
else
  note "tiler not reachable ($tiler) — maps and previews will fail until you're online (or set GROUNDSTATION_TITILER to a self-hosted one, see compose.yml)"
fi
if curl -s -o /dev/null --max-time 6 https://earth-search.aws.element84.com/v1; then
  ok "earth-search STAC reachable"
else
  note "earth-search not reachable — imagery search will fail until you're online"
fi

echo "6. up to date (new tools only reach you after an update)"
# plugin installs are cached per version under ~/.claude/plugins/cache/<mkt>/<plugin>/<version>,
# so a copy stays frozen until the plugin version bumps — that's what makes new tools appear
repo_version=$(python3 -c 'import json,sys;print(json.load(open(".claude-plugin/plugin.json"))["version"])' 2>/dev/null)
plugin_cache="$HOME/.claude/plugins/cache/groundstation/groundstation"
if [ -n "$repo_version" ] && [ -d "$plugin_cache" ]; then
  installed=$(ls -1 "$plugin_cache" 2>/dev/null | tail -1)
  if [ "$installed" = "$repo_version" ]; then
    ok "installed plugin is v$installed, same as this checkout"
  else
    note "Claude Code is running groundstation v$installed, this checkout is v$repo_version — update: claude plugin marketplace update groundstation && claude plugin update groundstation@groundstation, then restart Claude Code"
  fi
elif [ -n "$repo_version" ]; then
  ok "checkout is v$repo_version (no plugin install found — you're running the server directly)"
fi
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  # ls-remote, not fetch: this script writes nothing, not even to .git
  head_sha=$(git rev-parse HEAD 2>/dev/null)
  main_sha=$(git ls-remote origin main 2>/dev/null | awk 'NR==1 {print $1}')
  if [ -z "$main_sha" ]; then
    note "couldn't compare with origin/main — you're offline, or this shell has no credentials for the remote. Neither stops the server from running"
  elif [ "$head_sha" = "$main_sha" ]; then
    ok "checkout matches origin/main"
  else
    note "this checkout differs from origin/main — if you're not developing here: git pull, then restart Claude Code"
  fi
fi

echo
echo "result: $pass ok, $warn to check, $fail broken"
if [ "$fail" -gt 0 ]; then
  echo "fix the [XX] items above (top one first), then rerun."
  exit 1
fi
echo
echo "next, inside Claude Code:"
echo "  - /mcp should list groundstation as connected (if not: /reload-plugins, or restart the session)"
echo "  - the tools can take ~10s to surface on a cold start — that's normal"
echo "  - then ask for a map of your hometown."
