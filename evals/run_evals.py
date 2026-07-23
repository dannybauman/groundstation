"""Live integration evals for groundstation. Run: uv run evals/run_evals.py

Hits real endpoints — these evals prove the demo works right now, not that it
worked when written. Each check is the smallest thing that fails if that
integration breaks.
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from groundstation import tools  # noqa: E402

FAILURES = []


def check(name):
    def deco(fn):
        def run():
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                FAILURES.append(name)
                print(f"FAIL  {name}")
                traceback.print_exc(limit=2)

        return run

    return deco


@check("geocode resolves a place with a sane bbox")
def t_geocode():
    g = tools.geocode("Barotse Floodplain")
    assert g["source"] in ("gazet", "nominatim")
    w, s, e, n = g["bbox"]
    assert w < e and s < n and abs(g["lat"]) <= 90


@check("search_datasets finds sentinel across catalogs")
def t_datasets():
    hits = tools.search_datasets("sentinel-2")
    assert any(h.get("catalog") == "earth-search" for h in hits if "id" in h)


@check("search_imagery returns recent low-cloud Sentinel-2")
def t_search():
    r = tools.search_imagery(
        "earth-search", ["sentinel-2-l2a"], place="Barotse Floodplain",
        max_cloud_cover=20, limit=3,
    )
    assert r["count"] > 0
    assert r["items"][0]["self_url"].startswith("https://")
    t_search.item = r["items"][0]


@check("search_imagery assembles a full-coverage set for Calgary (two UTM zones)")
def t_full_coverage_calgary():
    # the Kevin regression: no single S2 tile covers Calgary, ever — the
    # answer must be a same-pass set, not the freshest cropped scene.
    # Geometry/coverage assertions only; cloud values are weather, not a test.
    r = tools.search_imagery(
        "earth-search", ["sentinel-2-l2a"],
        bbox=[-114.32, 50.84, -113.86, 51.21],
        datetime_range=tools.last_days_window(30), limit=20,
    )
    assert r["count"] > 0
    assert all((it.get("covers_aoi_pct") or 0) < 99 for it in r["items"])
    full = r.get("full_coverage_set")
    assert full, "expected a full_coverage_set for a two-zone city"
    assert len(full["items"]) >= 2
    assert len({i["collection"] for i in full["items"]}) == 1
    assert len({i["datetime"][:10] for i in full["items"]}) == 1
    assert full["union_covers_aoi_pct"] >= 99.0


@check("preview_item URL returns a PNG")
def t_preview():
    import httpx

    it = t_search.item
    p = tools.preview_item("earth-search", it["collection"], it["id"], max_size=128)
    r = httpx.get(p["preview_url"], timeout=90)
    assert r.status_code == 200 and "image" in r.headers.get("content-type", "")


@check("compute_statistics NDVI lands in [-1, 1]")
def t_stats():
    it = t_search.item
    s = tools.compute_statistics(
        "earth-search", it["collection"], it["id"], expression="(nir-red)/(nir+red)"
    )
    b = next(iter(s.values()))
    assert -1.0 <= b["mean"] <= 1.0 and b["std"] > 0


@check("render_map writes HTML with live tile URL")
def t_map():
    it = t_search.item
    m = tools.render_map(
        "eval map", it["bbox"],
        [{"type": "item", "name": "s2", "catalog": "earth-search",
          "collection_id": it["collection"], "item_id": it["id"], "assets": ["visual"]}],
        out_path=str(Path(tempfile.gettempdir()) / "groundstation-eval-map.html"),
    )
    html = Path(m["map_path"]).read_text(encoding="utf-8")
    assert "titiler.xyz/stac/tiles" in html and "maplibre" in html


@check("active_events returns EONET and GDACS data")
def t_events():
    ev = tools.active_events(days=30)
    assert "eonet_error" not in ev and "gdacs_error" not in ev
    assert len(ev["eonet"]) + len(ev["gdacs"]) > 0


@check("weather_summary returns past + forecast days")
def t_weather():
    w = tools.weather_summary(47.5, -120.5)
    assert len(w["daily"]["time"]) == 14


@check("veda catalog searchable and previewable")
def t_veda():
    r = tools.search_imagery("veda", ["caldor-fire-burn-severity"], bbox=[-121, 38, -119.5, 39.2], limit=1)
    assert r["count"] >= 1
    p = tools.preview_item("veda", r["items"][0]["collection"], r["items"][0]["id"], assets=["cog_default"])
    assert "openveda.cloud/api/raster" in p["preview_url"]


@check("planetary-computer search + rendered preview")
def t_pc():
    r = tools.search_imagery("planetary-computer", ["sentinel-2-l2a"], place="Munich", max_cloud_cover=30, limit=1)
    assert r["count"] >= 1
    p = tools.preview_item("planetary-computer", r["items"][0]["collection"], r["items"][0]["id"])
    assert "preview_url" in p


@check("local synth brief passes brief_checks (loud SKIP without an endpoint)")
def t_local_synth_live():
    import os

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "briefing"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import httpx

    import brief
    import brief_checks

    url = os.environ.get("GROUNDSTATION_LOCAL_URL", "http://localhost:11434/v1").rstrip("/")
    model = os.environ.get("GROUNDSTATION_LOCAL_MODEL")
    try:
        httpx.get(f"{url}/models", timeout=2)
    except Exception:
        print(f"      SKIP local-synth: no endpoint at {url}")
        return
    if not model:
        print("      SKIP local-synth: GROUNDSTATION_LOCAL_MODEL not set")
        return
    fixtures = sorted(Path(__file__).resolve().parents[1].glob("demo/*.data.json"))
    assert fixtures, "no demo/*.data.json fixture to synthesize from"
    data = json.loads(fixtures[0].read_text(encoding="utf-8"))
    os.environ["GROUNDSTATION_LLM"] = "local"
    try:
        md = brief._synthesize_local(brief.SYNTH_PROMPT + json.dumps(data, default=str))
    finally:
        os.environ.pop("GROUNDSTATION_LLM", None)
    assert md, "local endpoint up but synthesis declined or failed"
    with tempfile.TemporaryDirectory() as d:
        mp, dp = Path(d) / "b.md", Path(d) / "b.data.json"
        mp.write_text(md, encoding="utf-8")
        dp.write_text(json.dumps(data), encoding="utf-8")
        problems = brief_checks.check_brief(mp, dp)
    assert problems == [], f"local brief failed checks: {problems}"


if __name__ == "__main__":
    checks = [t_geocode, t_datasets, t_search, t_full_coverage_calgary, t_preview, t_stats, t_map, t_events, t_weather, t_veda, t_pc, t_local_synth_live]
    for fn in checks:
        fn()
    print(f"\n{len(checks) - len(FAILURES)}/{len(checks)} passed")
    sys.exit(1 if FAILURES else 0)
