#!/bin/bash
# Central-vs-modular comparison experiment (docs/rfc-modularity.md).
# 6 field-test questions x 2 configs, each as a headless claude session with a
# tight pre-approved allowlist. Usage:
#   bash evals/modularity-exp/runner.sh            # writes to evals/modularity-exp/out/
#   EXP=/somewhere bash evals/modularity-exp/runner.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
EXP="${EXP:-$HERE/out}"
mkdir -p "$EXP"
ALLOWED='Bash(uv run:*),Bash(mkdir:*),Bash(curl:*),Bash(sleep:*),Read,Write,WebFetch,WebSearch'
MODEL="${MODEL:-opus}"
MAXJOBS="${MAXJOBS:-3}"

if ! mkdir "$EXP/.lock" 2>/dev/null; then echo "already running (lock exists at $EXP/.lock)"; exit 0; fi

run_one() {
  local config="$1" q="$2" question="$3"
  local outdir="$EXP/$config/$q"
  mkdir -p "$outdir"
  local prompt
  prompt="$(cat "$HERE/prompts/$config-common.txt")

OUTDIR: $outdir
REPO: $REPO

QUESTION (answer it as if a user asked): \"$question\""
  local cwd="$outdir"
  [ "$config" = "central" ] && cwd="$REPO"
  ( cd "$cwd" && timeout 1500 claude -p "$prompt" \
      --model "$MODEL" --max-turns 60 \
      --allowedTools "$ALLOWED" \
      > "$outdir/report.md" 2> "$outdir/stderr.log" )
  echo "$(date +%H:%M:%S) done $config/$q exit=$?" >> "$EXP/runner.log"
}

echo "$(date +%H:%M:%S) runner start" > "$EXP/runner.log"
while IFS='|' read -r q question; do
  [ -z "$q" ] && continue
  for config in central modular; do
    while [ "$(jobs -rp | wc -l)" -ge "$MAXJOBS" ]; do wait -n; done
    run_one "$config" "$q" "$question" &
  done
done < "$HERE/prompts/questions.txt"
wait
echo "$(date +%H:%M:%S) runner finished" >> "$EXP/runner.log"
echo "RUNNER-FINISHED"
