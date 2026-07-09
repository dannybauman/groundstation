"""Grounding and structure checks for generated Earth briefs.

    uv run evals/brief_checks.py [demo_dir]

For every brief-*.md in the demo dir with a matching .data.json snapshot,
verify the generative output is structurally complete and grounded in the
data it was given — the discipline generative products need before anyone
schedules them. Exits nonzero on any failure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = ["## TL;DR", "## What changed", "## Weather signal", "## Fresh eyes", "## Suggested next steps"]


def check_brief(md_path: Path, data_path: Path) -> list[str]:
    md = md_path.read_text(encoding="utf-8")
    data = json.loads(data_path.read_text(encoding="utf-8"))
    problems = []

    for section in REQUIRED_SECTIONS:
        if section not in md:
            problems.append(f"missing section {section!r}")
    if not re.search(r"\b(CALM|WATCH|ACT)\b", md):
        problems.append("no alert level (CALM/WATCH/ACT)")
    if not re.search(r"\b20\d\d-\d\d(-\d\d)?\b", md):
        problems.append("no dates cited")

    # grounding: proper-noun event names in the brief must exist in the input data
    input_events = " ".join(
        str(e.get("title") or "")
        for e in (data.get("events", {}).get("eonet", []) + data.get("events", {}).get("gdacs", []))
    ).lower()
    claimed = re.findall(
        r"\b(?:Wildfire|Fire|Flood|Storm|Cyclone|Earthquake)\s+([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+)?)", md
    )
    for c in claimed:
        if c.lower() not in input_events:
            problems.append(f"event {c!r} not found in input data (possible hallucination)")

    # grounding: NDVI numbers cited must match the data (if a change block exists)
    ch = data.get("ndvi_change")
    if ch and str(ch["current"]["mean"])[:4].rstrip("0.") and f"{ch['current']['mean']:.2f}"[:4] not in md and str(ch["current"]["mean"]) not in md:
        problems.append(f"ndvi_change present ({ch['current']['mean']}) but not cited")

    return problems


def main(demo_dir: Path) -> int:
    pairs = []
    for md_path in sorted(demo_dir.glob("brief-*.md")):
        data_path = md_path.with_suffix("").with_suffix("")  # strip .md
        data_path = md_path.parent / (md_path.stem + ".data.json")
        if data_path.exists():
            pairs.append((md_path, data_path))
    if not pairs:
        print("no (brief.md, data.json) pairs found — run a brief first")
        return 0
    failures = 0
    for md_path, data_path in pairs:
        problems = check_brief(md_path, data_path)
        status = "PASS" if not problems else "FAIL"
        print(f"{status}  {md_path.name}")
        for p in problems:
            print(f"      - {p}")
        failures += bool(problems)
    print(f"\n{len(pairs) - failures}/{len(pairs)} briefs pass")
    return 1 if failures else 0


if __name__ == "__main__":
    demo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "demo"
    sys.exit(main(demo))
