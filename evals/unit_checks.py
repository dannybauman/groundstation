"""Offline unit checks — deterministic, no network. This is what CI runs.

    uv run evals/unit_checks.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "briefing"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from groundstation import tools  # noqa: E402
from groundstation.tools import _bbox_coverage_pct, _expression_to_bands  # noqa: E402
import brief  # noqa: E402
import brief_checks  # noqa: E402

FAILED = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except Exception as e:
        FAILED.append(name)
        print(f"FAIL  {name}: {e}")


def t_expression_ndvi():
    expr, assets = _expression_to_bands("(nir-red)/(nir+red)", None)
    assert expr == "(b1-b2)/(b1+b2)" and assets == ["nir", "red"]


def t_expression_funcs_and_explicit_assets():
    expr, assets = _expression_to_bands("where(nir>0, nir/red, 0)", ["nir", "red"])
    assert expr == "where(b1>0, b1/b2, 0)" and assets == ["nir", "red"]


def t_expression_bindex_passthrough():
    expr, assets = _expression_to_bands("(b1-b2)/(b1+b2)", ["nir", "red"])
    assert expr == "(b1-b2)/(b1+b2)"


def t_coverage_full_partial_none():
    aoi = [-114.3, 50.8, -113.8, 51.2]  # roughly Calgary
    assert _bbox_coverage_pct(aoi, [-115.0, 50.0, -113.0, 52.0]) == 100.0
    assert _bbox_coverage_pct(aoi, [-115.0, 50.0, -114.05, 52.0]) == 50.0  # the half-covered-city case
    assert _bbox_coverage_pct(aoi, [-110.0, 50.8, -109.0, 51.2]) == 0.0


def t_coverage_degenerate_inputs():
    aoi = [-114.3, 50.8, -113.8, 51.2]
    assert _bbox_coverage_pct(aoi, None) is None
    assert _bbox_coverage_pct(aoi, [-115.0]) is None
    assert _bbox_coverage_pct([-114.0, 51.0, -114.0, 51.0], [-115.0, 50.0, -113.0, 52.0]) is None


def t_tile_url_expression():
    t = tools.tile_url_template("earth-search", "sentinel-2-l2a", "ITEM", expression="(nir-red)/(nir+red)", rescale="-1,1")
    assert "b1" in t and "assets=nir" in t and "assets=visual" not in t


def t_tile_url_default_visual():
    t = tools.tile_url_template("earth-search", "sentinel-2-l2a", "ITEM")
    assert "assets=visual" in t and "expression" not in t


def t_render_map_compare_mode():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        layers = [
            {"type": "item", "name": "A", "catalog": "earth-search", "collection_id": "sentinel-2-l2a",
             "item_id": "X", "expression": "(nir-red)/(nir+red)", "bbox": [0, 0, 1, 1]},
            {"type": "item", "name": "B", "catalog": "earth-search", "collection_id": "sentinel-2-l2a",
             "item_id": "Y", "expression": "(nir-red)/(nir+red)", "bbox": [0, 0, 1, 1]},
        ]
        tools.render_map("t", [0, 0, 1, 1], layers, out_path=out)
        html = Path(out).read_text(encoding="utf-8")
        assert "const COMPARE = true" in html and "divider" in html


def t_render_map_overlay_mode():
    # different collections = overlay (severity over imagery), never a swipe
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        layers = [
            {"type": "item", "name": "S2", "catalog": "earth-search", "collection_id": "sentinel-2-l2a", "item_id": "X", "bbox": [0, 0, 1, 1]},
            {"type": "item", "name": "severity", "catalog": "veda", "collection_id": "caldor-fire-burn-severity",
             "item_id": "bs_to_save", "assets": ["cog_default"], "opacity": 0.75, "bbox": [0, 0, 1, 1]},
        ]
        tools.render_map("t", [0, 0, 1, 1], layers, out_path=out)
        html = Path(out).read_text(encoding="utf-8")
        assert "const COMPARE = false" in html
        assert '"opacity": 0.75' in html


def t_render_map_compare_override():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        layers = [
            {"type": "raster", "name": "A", "tiles": "https://x/{z}/{x}/{y}"},
            {"type": "raster", "name": "B", "tiles": "https://y/{z}/{x}/{y}"},
        ]
        tools.render_map("t", [0, 0, 1, 1], layers, out_path=out, compare=True)
        html = Path(out).read_text(encoding="utf-8")
        assert "const COMPARE = true" in html


def _render_3d(**kw) -> str:
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m3d.html")
        layer = {"type": "item", "name": "s2", "catalog": "earth-search",
                 "collection_id": "sentinel-2-l2a", "item_id": "X", "bbox": [0, 0, 1, 1]}
        tools.render_map_3d("Torres del Paine", [0, 0, 1, 1], layer, out_path=out, **kw)
        return Path(out).read_text(encoding="utf-8")


def t_render_map_3d_terrain_source():
    html = _render_3d()
    assert '"type": "raster-dem"' in html and '"encoding": "terrarium"' in html
    assert "s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png" in html


def t_render_map_3d_imagery_and_attribution():
    html = _render_3d()
    assert "titiler.xyz/stac/tiles" in html and "assets=visual" in html
    assert "Development Seed" in html and "AWS Terrarium" in html


def t_render_map_3d_no_local_paths():
    html = _render_3d()
    assert "/Users/" not in html and "file://" not in html


def t_render_map_3d_controls():
    html = _render_3d(exaggeration=2.5)
    assert 'id="exaggeration"' in html and 'id="flythrough"' in html and 'id="reset"' in html
    assert 'value="2.5"' in html and "let exaggeration = 2.5" in html


def t_pick_best_scene_prefers_coverage():
    items = [
        {"id": "full", "bbox": [0, 0, 1, 1], "cloud_cover": 5.0},
        {"id": "sliver", "bbox": [0, 0, 0.02, 0.02], "cloud_cover": 0.0},
    ]
    assert tools.pick_best_scene(items, [0, 0, 1, 1])["id"] == "full"
    assert tools.pick_best_scene([], [0, 0, 1, 1]) is None


def t_expression_defaults_rescale():
    t = tools.tile_url_template("earth-search", "sentinel-2-l2a", "X", expression="(nir-red)/(nir+red)")
    assert "rescale=-1%2C1" in t


def t_slugify():
    assert tools.slugify("Chelan County, Washington") == "chelan-county--washington"
    assert tools.slugify("A" * 100, max_len=10) == "aaaaaaaaaa"


def t_last_days_window():
    w = tools.last_days_window(14)
    assert w.count("/") == 1 and w.endswith("T23:59:59Z")


def t_md_to_html():
    html = brief.md_to_html("## TL;DR\nAll calm.\n- item one\n- item two\nDone.")
    assert "<h2>TL;DR</h2>" in html and html.count("<li>") == 2 and "<ul>" in html and "</ul>" in html


def t_alert_extraction():
    md = "## TL;DR\nAlert level: **WATCH** because reasons.\n## What changed\n- x"
    m = re.search(r"\b(CALM|WATCH|ACT)\b", md)
    assert m and m.group(1) == "WATCH"


def _fixture(md: str, events=None) -> list[str]:
    data = {"events": {"eonet": events or [], "gdacs": []}}
    with tempfile.TemporaryDirectory() as d:
        mp, dp = Path(d) / "b.md", Path(d) / "b.data.json"
        mp.write_text(md, encoding="utf-8")
        dp.write_text(json.dumps(data), encoding="utf-8")
        return brief_checks.check_brief(mp, dp)


GOOD_MD = """## TL;DR
Quiet day, alert level CALM as of 2026-07-09.
## What changed
- Wildfire Navarre Coulee still open.
## Weather signal
- dry
## Fresh eyes on the ground
- one scene 2026-07-08
## Suggested next steps
1. nothing
"""


def t_brief_checks_pass():
    problems = _fixture(GOOD_MD, events=[{"title": "Wildfire NAVARRE COULEE, Chelan, Washington"}])
    assert problems == [], problems


def t_brief_checks_catch_hallucination():
    problems = _fixture(GOOD_MD, events=[{"title": "Flood in Texas"}])
    assert any("hallucination" in p for p in problems)


def t_brief_checks_catch_missing_section():
    problems = _fixture("## TL;DR\nCALM 2026-07-09\n")
    assert any("missing section" in p for p in problems)


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(name, fn)
    print(f"\n{len([k for k in globals() if k.startswith('t_')]) - len(FAILED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
