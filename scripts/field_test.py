#!/usr/bin/env python3
"""The field-test factory: cases JSON in, the standard field-test page out.

A field test is a repeatable ritual — run real prompts against live data,
show the outputs in one consistent format, link every result to its live
interactive artifact. This script owns the format so each edition is just a
cases file. Cases are curated by us (trusted); `answer`/`learning` allow the
inline markup the cards use (<b>, <em>).

    uv run scripts/field_test.py docs/field-test-2026-07-22.cases.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='38' fill='%23CF3F02'/></svg>">
<link href="https://fonts.googleapis.com/css2?family=Roboto+Condensed:wght@400;600;700&family=Roboto+Mono:wght@400;500&family=Roboto:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --accent: #CF3F02; --ink: #443F3F; --mid: #4a4440; --muted: #9a9490;
  --rule: #dedad4; --paper: #f7f4ef; --white: #fff;
  --calm: #2e7d32; --blue: #1d4e8f;
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.6 Roboto, sans-serif; color: var(--ink); background: var(--paper); }
header { border-bottom: 2px solid var(--ink); padding: 42px 24px 30px; }
.wrap { max-width: 1060px; margin: 0 auto; }
header h1 { font: 700 34px/1.1 'Roboto Condensed', sans-serif; margin: 0 0 6px; letter-spacing: .02em; }
header h1 .dot { display: inline-block; width: 15px; height: 15px; border-radius: 50%; background: var(--accent); margin-right: 12px; }
header .sub { font: 500 11.5px 'Roboto Mono', monospace; letter-spacing: .12em; text-transform: uppercase; color: var(--mid); }
header p.lede { max-width: 760px; font-size: 16px; margin: 16px 0 0; color: var(--mid); }
.stats { display: flex; gap: 14px; flex-wrap: wrap; margin: 26px auto 0; max-width: 1060px; padding: 0; }
.stat { flex: 1; min-width: 160px; background: var(--white); border: 1px solid var(--rule); border-top: 3px solid var(--accent); padding: 12px 16px; }
.stat b { display: block; font: 600 30px 'Roboto Condensed', sans-serif; color: var(--accent); }
.stat span { font-size: 12px; color: var(--mid); }
main { padding: 10px 24px 60px; }
h2.round { font: 500 12px 'Roboto Mono', monospace; letter-spacing: .16em; text-transform: uppercase; color: var(--mid);
  display: flex; align-items: center; gap: 12px; margin: 44px 0 18px; }
h2.round::after { content: ""; flex: 1; height: 1px; background: var(--rule); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 16px; }
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--white); border: 1px solid var(--rule); padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; }
.card .top { display: flex; align-items: center; gap: 10px; }
.card .num { font: 700 15px 'Roboto Condensed', sans-serif; background: var(--ink); color: var(--paper); width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex: none; }
.card .tag { font: 500 10px 'Roboto Mono', monospace; letter-spacing: .1em; text-transform: uppercase; color: var(--mid); border: 1px solid var(--rule); padding: 3px 8px; }
.card .who { margin-left: auto; font: 400 10.5px 'Roboto Mono', monospace; color: var(--muted); }
.label { font: 500 9.5px 'Roboto Mono', monospace; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin-bottom: 3px; }
.prompt { font: 400 12.5px/1.55 'Roboto Mono', monospace; background: var(--paper); border: 1px solid var(--rule); padding: 9px 12px; color: var(--ink); }
.answer { font-size: 13.5px; color: var(--mid); }
.answer b { color: var(--ink); }
.learning { border-left: 3px solid var(--accent); background: color-mix(in srgb, var(--accent) 5%, white); padding: 8px 12px; font-size: 12.5px; color: var(--mid); }
.learning b { color: var(--accent); font-style: normal; }
.shows { border-left: 3px solid var(--rule); padding: 6px 12px; font-size: 12.5px; color: var(--muted); font-style: italic; }
.repro { background: var(--white); border: 1px solid var(--rule); padding: 16px 20px; margin-top: 40px; }
.repro code { display: block; font: 400 12px/1.6 'Roboto Mono', monospace; background: var(--paper); padding: 10px 12px; overflow-x: auto; white-space: pre; }
.fig { position: relative; display: block; border: 1px solid var(--rule); text-decoration: none; }
.fig img { width: 100%; display: block; }
.fig .open { position: absolute; right: 8px; bottom: 8px; font: 500 10px 'Roboto Mono', monospace;
  background: var(--paper); border: 1px solid var(--ink); padding: 3px 8px; color: var(--ink); }
.fig:hover .open { background: var(--accent); color: #fff; border-color: var(--accent); }
footer { text-align: center; font: 400 11px 'Roboto Mono', monospace; color: var(--muted); padding: 26px 0 40px; }
</style>
</head>
<body>

<header><div class="wrap">
  <h1><span class="dot"></span>__HEADING__</h1>
  <div class="sub">__SUB__</div>
  <p class="lede">__LEDE__</p>
</div></header>

<div class="stats wrap" style="padding: 0 24px">
__STATS__
</div>

<main class="wrap">
__ROUNDS__
__REPRO__
</main>

<footer>__FOOTER__</footer>

</body>
</html>
"""


def _card(n: int, c: dict) -> str:
    parts = [f'<div class="card"><div class="top"><div class="num">{n}</div>'
             f'<div class="tag">{c["tag"]}</div><div class="who">{c.get("who", "")}</div></div>']
    parts.append(f'<div><div class="label">Input</div><div class="prompt">{c["prompt"]}</div></div>')
    parts.append(f'<div class="answer"><div class="label">Output</div>{c["answer"]}</div>')
    if c.get("artifact"):
        img = f'<img src="{c["image"]}" alt="artifact produced by example {n}" loading="lazy">' if c.get("image") else ""
        label = c.get("artifact_label", "live artifact ↗")
        parts.append(f'<a class="fig" href="{c["artifact"]}" target="_blank" title="open the live artifact">{img}<span class="open">{label}</span></a>')
    if c.get("learning"):
        parts.append(f'<div class="learning"><b>Learning →</b> {c["learning"]}</div>')
    if c.get("shows"):
        parts.append(f'<div class="shows">{c["shows"]}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def build(cases_path: str | Path) -> Path:
    spec = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    n = 0
    rounds_html = []
    for rnd in spec["rounds"]:
        cards = []
        for c in rnd["cases"]:
            n += 1
            cards.append(_card(n, c))
        rounds_html.append(f'<h2 class="round">{rnd["name"]}</h2>\n<div class="grid">\n' + "\n\n".join(cards) + "\n</div>")
    stats = "\n".join(f'  <div class="stat"><b>{s["value"]}</b><span>{s["label"]}</span></div>' for s in spec.get("stats", []))
    repro = ""
    if spec.get("repro"):
        repro = f'<div class="repro"><div class="label">Reproduce</div><code>{spec["repro"]}</code></div>'
    html = (
        PAGE.replace("__TITLE__", spec["title"])
        .replace("__HEADING__", spec["heading"])
        .replace("__SUB__", spec["sub"])
        .replace("__LEDE__", spec["lede"])
        .replace("__STATS__", stats)
        .replace("__ROUNDS__", "\n\n".join(rounds_html))
        .replace("__REPRO__", repro)
        .replace("__FOOTER__", spec.get("footer", "groundstation · Development Seed labs"))
    )
    out = Path(cases_path).with_suffix("").with_suffix(".html")  # x.cases.json -> x.html
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({n} cases)")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: field_test.py <cases.json>")
    build(sys.argv[1])
