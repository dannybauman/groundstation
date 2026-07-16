#!/bin/bash
# Concatenate all experiment reports for grading against rubric.md.
HERE="$(cd "$(dirname "$0")" && pwd)"
EXP="${EXP:-$HERE/out}"
for config in central modular; do
  for d in "$EXP/$config"/q*; do
    [ -d "$d" ] || continue
    echo "===== $config/$(basename "$d") ====="
    if [ -s "$d/report.md" ]; then cat "$d/report.md"; else echo "(no report)"; tail -3 "$d/stderr.log" 2>/dev/null; fi
    echo; echo "artifacts: $(ls "$d" 2>/dev/null | tr '\n' ' ')"
    echo
  done
done
