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


def _fcs_item(id_, day, bbox, collection="sentinel-2-l2a"):
    return {"id": id_, "datetime": f"{day}T18:30:00Z", "bbox": bbox, "collection": collection}


def t_full_coverage_set_two_halves():
    aoi = [-114.3, 50.8, -113.8, 51.2]
    west = _fcs_item("west", "2026-07-19", [-115.0, 50.0, -114.0, 52.0])
    east = _fcs_item("east", "2026-07-19", [-114.1, 50.0, -113.0, 52.0])  # overlaps west
    got = tools.find_full_coverage_set([west, east], aoi)
    assert got and {i["id"] for i in got["items"]} == {"west", "east"}
    assert got["date"] == "2026-07-19" and got["union_covers_aoi_pct"] >= 99.0


def t_full_coverage_set_single_covering_item():
    aoi = [-114.3, 50.8, -113.8, 51.2]
    full = _fcs_item("full", "2026-07-19", [-115.0, 50.0, -113.0, 52.0])
    part = _fcs_item("part", "2026-07-19", [-115.0, 50.0, -114.0, 52.0])
    got = tools.find_full_coverage_set([full, part], aoi)
    assert got and [i["id"] for i in got["items"]] == ["full"]  # no free riders


def t_full_coverage_set_never_mixes_days():
    aoi = [-114.3, 50.8, -113.8, 51.2]
    west = _fcs_item("west", "2026-07-19", [-115.0, 50.0, -114.0, 52.0])
    east = _fcs_item("east", "2026-07-21", [-114.1, 50.0, -113.0, 52.0])
    assert tools.find_full_coverage_set([west, east], aoi) is None


def t_full_coverage_set_prefers_newest_full_day():
    aoi = [-114.3, 50.8, -113.8, 51.2]
    old = [
        _fcs_item("ow", "2026-07-19", [-115.0, 50.0, -114.0, 52.0]),
        _fcs_item("oe", "2026-07-19", [-114.1, 50.0, -113.0, 52.0]),
    ]
    newer_partial = _fcs_item("np", "2026-07-21", [-115.0, 50.0, -114.0, 52.0])
    got = tools.find_full_coverage_set(old + [newer_partial], aoi)
    assert got and got["date"] == "2026-07-19"  # completeness beats freshness


def t_union_coverage_no_double_count():
    aoi = [0.0, 0.0, 10.0, 10.0]
    # two identical half-boxes: union is 50, not 100
    half = [0.0, 0.0, 5.0, 10.0]
    assert tools._union_coverage_pct(aoi, [half, half]) == 50.0
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


# ---- scheduled sweeps: gate header, transition guarantee, run.sh lock ----

import datetime as dt  # noqa: E402
import subprocess  # noqa: E402


def t_slack_payload_withheld_header():
    results = [{"place": "A", "alert": "CALM", "tldr": "Quiet."}]
    p = brief.slack_payload(results, dt.date(2026, 7, 22), total=2, withheld=1)
    assert "1 of 2 areas, 1 withheld by checks" in p["text"]
    assert "A" in p["text"]


def t_slack_payload_all_pass_unchanged():
    results = [{"place": "A", "alert": "CALM", "tldr": "Quiet."}]
    p = brief.slack_payload(results, dt.date(2026, 7, 22))
    assert "(1 areas)" in p["text"] and "withheld" not in p["text"]


def t_transition_note_added_when_model_forgot():
    md = "## TL;DR\nAll quiet, alert level CALM as of 2026-07-22.\n## What changed\n- nothing"
    out = brief._ensure_transition_note(md, "WATCH")
    assert "stood down from the last run's WATCH" in out
    assert out.index("stood down") < out.index("All quiet")


def t_transition_note_respects_existing_mention():
    md = "## TL;DR\nYesterday's WATCH stands down, CALM today.\n## What changed\n- x"
    assert brief._ensure_transition_note(md, "WATCH") == md


def t_transition_note_only_on_deescalation():
    md = "## TL;DR\nStill WATCH, winds rising.\n"
    assert brief._ensure_transition_note(md, "WATCH") == md
    assert brief._ensure_transition_note("## TL;DR\nCALM.\n", None) == "## TL;DR\nCALM.\n"
    assert brief._ensure_transition_note("## TL;DR\nCALM.\n", "CALM") == "## TL;DR\nCALM.\n"


def t_run_sh_skips_when_lock_held():
    root = Path(brief.__file__).resolve().parents[1]
    lock = root / "briefing" / "state" / ".run.lock"
    lock.mkdir(parents=True, exist_ok=True)  # fresh lock = a run in progress
    try:
        r = subprocess.run(
            ["bash", str(root / "briefing" / "run.sh")],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0 and "already running" in r.stdout
    finally:
        lock.rmdir()


# ---- local synthesis routing (stubbed endpoint on loopback, deterministic) ----

import contextlib  # noqa: E402
import os  # noqa: E402


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _stub_llm(content: str):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"data": []})

        def do_POST(self):
            self._send({"choices": [{"message": {"content": content}}]})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def t_local_synth_declines_without_model():
    with _env(GROUNDSTATION_LOCAL_MODEL=None):
        assert brief._synthesize_local("p") is None


def t_local_synth_declines_oversize_prompt():
    with _env(GROUNDSTATION_LOCAL_MODEL="m"):
        assert brief._synthesize_local("x" * (brief.LOCAL_PROMPT_BUDGET_CHARS + 1)) is None


def t_local_synth_declines_unreachable_endpoint():
    with _env(GROUNDSTATION_LOCAL_MODEL="m", GROUNDSTATION_LOCAL_URL="http://127.0.0.1:9/v1"):
        assert brief._synthesize_local("p") is None


def t_local_synth_uses_local_and_strips_think():
    srv = _stub_llm("<think>internal</think>## TL;DR\nCALM, quiet day.")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        with _env(GROUNDSTATION_LLM="local", GROUNDSTATION_LOCAL_MODEL="m", GROUNDSTATION_LOCAL_URL=url):
            out = brief.synthesize({"place": "t", "events": {"eonet": []}, "imagery": {"items": []}})
        assert out.startswith("## TL;DR") and "<think>" not in out
    finally:
        srv.shutdown()


def t_local_synth_everything_down_still_briefs():
    # bogus local endpoint AND no claude CLI -> deterministic data-only brief
    real_run = brief.subprocess.run

    def no_cli(*a, **k):
        raise FileNotFoundError("claude")

    brief.subprocess.run = no_cli
    try:
        with _env(GROUNDSTATION_LLM="local", GROUNDSTATION_LOCAL_MODEL="m",
                  GROUNDSTATION_LOCAL_URL="http://127.0.0.1:9/v1"):
            out = brief.synthesize({"place": "t", "events": {"eonet": []}, "imagery": {"items": []}})
        assert "## TL;DR" in out  # the floor holds
    finally:
        brief.subprocess.run = real_run


def t_local_synth_claude_default_never_touches_local():
    # default engine must not even look at local env; claude stubbed to succeed
    real_run = brief.subprocess.run

    class R:
        returncode, stdout, stderr = 0, "## TL;DR\nvia claude", ""

    brief.subprocess.run = lambda *a, **k: R()
    try:
        with _env(GROUNDSTATION_LLM=None, GROUNDSTATION_LOCAL_MODEL=None):
            out = brief.synthesize({"place": "t", "events": {"eonet": []}, "imagery": {"items": []}})
        assert out == "## TL;DR\nvia claude"
    finally:
        brief.subprocess.run = real_run


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(name, fn)
    print(f"\n{len([k for k in globals() if k.startswith('t_')]) - len(FAILED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
