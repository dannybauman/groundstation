"""Offline unit checks — deterministic, no network. This is what CI runs.

    uv run evals/unit_checks.py
"""

from __future__ import annotations

import base64
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


def t_coverage_antimeridian():
    # 1. Pinned real B22A case (crossing scene, normal AOI)
    aoi_b22a = [-178.5, -68.6, -175.4, -65.6]
    scene_b22a = [177.5841928816539, -69.74115112594123, -171.51300204756348, -66.10281509848461]
    cov = _bbox_coverage_pct(aoi_b22a, scene_b22a)
    assert cov is not None
    assert 80.0 < cov < 90.0, f"Expected coverage between 80 and 90, got {cov}"

    # 2. Crossing AOI vs non-crossing scene
    crossing_aoi = [179.0, -10.0, -179.0, 10.0]  # wraps across 180 (width 2 degrees)
    normal_scene_inside = [-179.8, -5.0, -179.2, 5.0]  # inside the eastern part of AOI
    cov2 = _bbox_coverage_pct(crossing_aoi, normal_scene_inside)
    assert cov2 == 15.0, f"Expected coverage 15.0, got {cov2}"

    # 3. Non-crossing AOI vs crossing scene
    normal_aoi = [-179.5, -10.0, -178.5, 10.0]
    crossing_scene = [179.0, -5.0, -179.0, 5.0]
    cov3 = _bbox_coverage_pct(normal_aoi, crossing_scene)
    assert cov3 == 25.0, f"Expected coverage 25.0, got {cov3}"

    # 4. Union coverage crossing
    cov_union = tools._union_coverage_pct(crossing_aoi, [[179.5, -5.0, 180.0, 5.0], [-180.0, -5.0, -179.5, 5.0]])
    assert cov_union == 25.0, f"Expected union coverage 25.0, got {cov_union}"

    # 5. Footprints overlap crossing
    layer_a = {"type": "raster", "bounds": [179.0, -10.0, -179.0, 10.0]}
    layer_b = {"type": "raster", "bounds": [179.5, -10.0, -179.5, 10.0]}
    assert tools._footprints_overlap([layer_a, layer_b]) is True

    layer_c = {"type": "raster", "bounds": [179.0, -10.0, 179.5, 10.0]}
    layer_d = {"type": "raster", "bounds": [-179.5, -10.0, -179.0, 10.0]}
    assert tools._footprints_overlap([layer_c, layer_d]) is False


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


def t_mosaic_is_not_a_swipe():
    # the real Chelan regression: two adjacent Sentinel-2 tiles of ONE
    # collection are a mosaic, not a before/after. Swiping them leaves half
    # the AOI blank at every divider position.
    west = {"type": "raster", "bounds": [-121.665655, 47.731054, -120.147575, 48.744975]}
    east = {"type": "raster", "bounds": [-120.332977, 47.690861, -118.79173, 48.720918]}
    assert tools._footprints_overlap([west, east]) is False
    # same scene at two dates IS a comparison
    a = {"type": "raster", "bounds": [90.896, 23.577, 93.650, 25.501]}
    b = {"type": "raster", "bounds": [90.899, 23.562, 93.653, 25.485]}
    assert tools._footprints_overlap([a, b]) is True
    # two layers off one item (true colour + index) always compare
    assert tools._footprints_overlap([a, a]) is True
    # unknown bounds cannot be judged, so keep the asked-for comparison
    assert tools._footprints_overlap([{"type": "raster"}, {"type": "raster"}]) is True


def t_tile_url_expression():
    t = tools.tile_url_template("earth-search", "sentinel-2-l2a", "ITEM", expression="(nir-red)/(nir+red)", rescale="-1,1")
    assert "b1" in t and "assets=nir" in t and "assets=visual" not in t


def t_tile_url_default_visual():
    t = tools.tile_url_template("earth-search", "sentinel-2-l2a", "ITEM")
    assert "assets=visual" in t and "expression" not in t


def t_coverage_set_expands_to_item_layers():
    # pass search_imagery's full_coverage_set straight through — the manual
    # unroll is how field test №4 shipped a 58%-coverage Novo Progresso map
    fcs = {"date": "2026-08-03", "items": [
        {"catalog": "earth-search", "collection": "sentinel-2-l2a", "id": "S2B_21MXN_X", "bbox": [0, 0, 1, 1]},
        {"catalog": "earth-search", "collection": "sentinel-2-l2a", "id": "S2B_21MXM_X", "bbox": [0, -1, 1, 0]},
    ]}
    out = tools._expand_coverage_set({"type": "coverage_set", "set": fcs, "name": "NDVI",
                                      "expression": "(nir-red)/(nir+red)"})
    assert len(out) == 2 and all(l["type"] == "item" for l in out)
    assert all(l["expression"] == "(nir-red)/(nir+red)" for l in out)
    assert out[0]["bbox"] == [0, 0, 1, 1] and "NDVI" in out[0]["name"]
    # non-coverage layers pass through untouched
    plain = {"type": "geojson", "name": "x", "data": {}}
    assert tools._expand_coverage_set(plain) == [plain]
    try:
        tools._expand_coverage_set({"type": "coverage_set", "set": {}})
        raise AssertionError("empty set should raise")
    except ValueError:
        pass


def t_next_pass_pure_helpers():
    # one dip under the swath line = one pass; a shallow approach = none
    assert tools._pass_minima([300, 100, 50, 100, 300], 145) == [2]
    assert tools._pass_minima([300, 200, 300], 145) == []
    # plateau at the minimum still counts once (<= left, < right)
    assert tools._pass_minima([300, 50, 50, 300], 145) == [2]
    # local solar time: UTC 19:00 at 120W is 11:00 — a daytime optical pass
    assert abs(tools._local_solar_hour(19.0, -120) - 11.0) < 0.01
    assert abs(tools._local_solar_hour(2.0, 140) - 11.33) < 0.34


def t_coverage_note_names_the_hole():
    # a map whose layers cover a sliver of the view "succeeds" and renders
    # mostly blank — the field test №4 Dolphin card shipped exactly this way
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        sliver = [{"type": "item", "name": "A", "catalog": "earth-search",
                   "collection_id": "sentinel-2-l2a", "item_id": "X",
                   "bbox": [0.0, 0.0, 1.0, 1.0]}]
        r = tools.render_map("t", [0, 0, 10, 10], sliver, out_path=out)
        assert "coverage_note" in r and "1%" in r["coverage_note"]
        full = [{"type": "item", "name": "A", "catalog": "earth-search",
                 "collection_id": "sentinel-2-l2a", "item_id": "X",
                 "bbox": [0, 0, 10, 10]}]
        r = tools.render_map("t", [0, 0, 10, 10], full, out_path=out)
        assert "coverage_note" not in r


def t_state_would_have_caught_the_two_real_bugs():
    """Both bugs this repo shipped were caught by a human looking at a picture.

    render_map's `state` is what makes them checkable. Each half below fails
    on the pre-fix behaviour and passes now — that is the whole point of
    returning state rather than only a file path.
    """
    tile = "https://x/{z}/{x}/{y}.png"
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")

        # 1. Field test No.4's Chelan card: two mosaic tiles rendered as a swipe,
        # so half the AOI was blank at every divider position.
        west = {"type": "raster", "name": "W", "tiles": tile, "bounds": [-121.66, 47.73, -120.14, 48.74]}
        east = {"type": "raster", "name": "E", "tiles": tile, "bounds": [-120.33, 47.69, -118.79, 48.72]}
        r = tools.render_map("t", [-120.95, 47.83, -119.45, 48.75], [west, east], out_path=out)
        assert r["state"]["compare"] is False, "a mosaic must not render as a swipe"
        assert len(r["state"]["layers"]) == 2

        # 2. Field test No.4's Novo Progresso card: one scene over an AOI it
        # only half covered, shipped without anyone noticing the hole.
        sliver = [{"type": "raster", "name": "A", "tiles": tile, "bounds": [0.0, 0.0, 1.0, 1.0]}]
        r = tools.render_map("t", [0, 0, 10, 10], sliver, out_path=out)
        assert r["state"]["coverage_pct"] < 95
        assert "coverage_note" in r

        # and a genuine before/after still compares — item layers, since
        # auto-decide keys on collection and a raw raster layer has none
        a = {"type": "item", "name": "A", "catalog": "earth-search",
             "collection_id": "sentinel-2-l2a", "item_id": "X", "bbox": [0, 0, 10, 10]}
        b = dict(a, name="B", item_id="Y")
        r = tools.render_map("t", [0, 0, 10, 10], [a, b], out_path=out)
        assert r["state"]["compare"] is True and r["state"]["coverage_pct"] == 100.0


def t_3d_coverage_note_accounts_for_the_load_button():
    # 3D differs from 2D twice: only the main layer drapes on load, and an
    # uncovered area shows terrain with no imagery rather than blank. The note
    # has to say what the viewer gets NOW and what the button would add.
    tile = "https://x/{z}/{x}/{y}.png"
    main = {"type": "raster", "name": "M", "tiles": tile, "bounds": [0, 0, 5, 10]}
    extra = {"type": "raster", "name": "E", "tiles": tile, "bounds": [5, 0, 10, 10]}
    with tempfile.TemporaryDirectory() as d:
        r = tools.render_map_3d("t", [0, 0, 10, 10], main, out_path=str(Path(d) / "a.html"))
        assert "50%" in r["coverage_note"] and "extra_layers" in r["coverage_note"]
        r = tools.render_map_3d("t", [0, 0, 10, 10], main, extra_layers=[extra],
                                out_path=str(Path(d) / "b.html"))
        assert "50%" in r["coverage_note"] and "100%" in r["coverage_note"]
        full = {"type": "raster", "name": "F", "tiles": tile, "bounds": [0, 0, 10, 10]}
        r = tools.render_map_3d("t", [0, 0, 10, 10], full, out_path=str(Path(d) / "c.html"))
        assert "coverage_note" not in r
        assert sorted(r) == ["path", "state", "title"]  # + state as of Aug 5


def t_3d_state_marks_what_does_not_draw_on_load():
    # 3D state is not 2D's: extras are embedded but deferred behind the Load
    # full coverage button, and the vertical stretch is a real render fact.
    tile = "https://x/{z}/{x}/{y}.png"
    main = {"type": "raster", "name": "M", "tiles": tile, "bounds": [0, 0, 5, 10]}
    extra = {"type": "raster", "name": "E", "tiles": tile, "bounds": [5, 0, 10, 10]}
    with tempfile.TemporaryDirectory() as d:
        r = tools.render_map_3d("t", [0, 0, 10, 10], main, extra_layers=[extra],
                                exaggeration=1.8, out_path=str(Path(d) / "a.html"))
        s = r["state"]
        assert s["exaggeration"] == 1.8 and s["terrain"] is True
        assert [l.get("deferred") for l in s["layers"]] == [None, True]
        # the two coverage numbers are different things and both are reported
        assert s["coverage_pct"] == 50.0 and s["coverage_pct_loaded"] == 100.0
        # no extras means no loaded number to report
        r = tools.render_map_3d("t", [0, 0, 10, 10], main, out_path=str(Path(d) / "b.html"))
        assert r["state"]["coverage_pct"] == 50.0
        assert "coverage_pct_loaded" not in r["state"]


def t_field_test_etime_parser():
    # the field-test preflight compares MCP server start time against source
    # mtime; getting ps -o etime parsing wrong makes the warning silently never
    # fire, which is the failure it exists to prevent
    import importlib.util
    fp = Path(__file__).resolve().parents[1] / "scripts" / "field_test.py"
    spec = importlib.util.spec_from_file_location("_ft", fp)
    ft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ft)
    assert ft._etime_seconds("05:30") == 330
    assert ft._etime_seconds("01:00:00") == 3600
    assert ft._etime_seconds("2-03:00:00") == 2 * 86400 + 3 * 3600
    assert ft._etime_seconds("") is None
    assert ft._etime_seconds("not-a-time") is None


def t_map_with_no_imagery_says_so():
    # field test No.6: a geojson-only storm track rendered a 10 KB still that
    # was two dots on blank. The render "succeeded" and nothing said otherwise.
    gj = {"type": "FeatureCollection", "features": []}
    with tempfile.TemporaryDirectory() as d:
        r = tools.render_map("t", [0, 0, 10, 10],
                             [{"type": "geojson", "name": "x", "data": gj}],
                             out_path=str(Path(d) / "a.html"))
        assert "no imagery layer" in r["coverage_note"]
        # with a raster present the note reverts to the coverage question
        r = tools.render_map("t", [0, 0, 10, 10],
                             [{"type": "raster", "name": "r", "tiles": "https://x/{z}/{x}/{y}.png",
                               "bounds": [0, 0, 10, 10]},
                              {"type": "geojson", "name": "x", "data": gj}],
                             out_path=str(Path(d) / "b.html"))
        assert "coverage_note" not in r


def t_state_never_changes_the_file():
    # state is additive: the artifact a partner opens must be byte-identical
    # whether or not anything reads the structured half
    tile = "https://x/{z}/{x}/{y}.png"
    layers = [{"type": "raster", "name": "A", "tiles": tile, "bounds": [0, 0, 1, 1]}]
    with tempfile.TemporaryDirectory() as d:
        one = str(Path(d) / "a.html")
        two = str(Path(d) / "b.html")
        r1 = tools.render_map("t", [0, 0, 1, 1], layers, out_path=one)
        r2 = tools.render_map("t", [0, 0, 1, 1], layers, out_path=two)
        assert Path(one).read_bytes() == Path(two).read_bytes()
        assert r1["state"] == r2["state"]
        assert r1["map_path"] != r2["map_path"]  # only the path differs


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


def t_skill_tool_count_matches_server():
    # the skill tells the agent how many tools to wait for on a cold start; a
    # stale number makes it give up early or wait for tools that aren't coming
    from groundstation.server import TOOLS

    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills" / "earth-data" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"all (\d+) tools", skill)
    assert m, "SKILL.md no longer states a tool count — update this check or the wording"
    assert int(m.group(1)) == len(TOOLS), f"SKILL.md says {m.group(1)} tools, server registers {len(TOOLS)}"


def t_plugin_version_is_semver():
    # plugin installs are cached per version, so a bump is what actually
    # delivers new tools to anyone who installed via /plugin
    root = Path(__file__).resolve().parents[1]
    v = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"plugin.json version {v!r} is not major.minor.patch"


def t_compass():
    assert tools._compass(0) == "from the N"
    assert tools._compass(360) == "from the N"
    assert tools._compass(45) == "from the NE"
    assert tools._compass(90) == "from the E"
    assert tools._compass(135) == "from the SE"
    assert tools._compass(180) == "from the S"
    assert tools._compass(225) == "from the SW"
    assert tools._compass(270) == "from the W"
    assert tools._compass(315) == "from the NW"
    assert tools._compass(None) == "unknown"


def t_conditions_signals_cases():
    # 1. Dry and windy case
    dryness = {"total_precipitation": 0.0, "days_since_last_rain": None, "days_back": 14}
    wind = {"today": {"wind_speed_max": 45.2, "wind_direction_compass": "from the NE"}}
    events = {"eonet": [], "gdacs": []}
    signals = tools._conditions_signals(dryness, wind, events)
    assert "no rain ≥1mm in the last 14 days" in signals
    assert "peak wind today 45 km/h from the NE" in signals
    assert "no active events within ~150 km" in signals

    # 2. Rainy case with 1 active wildfire
    dryness = {"total_precipitation": 12.4, "days_since_last_rain": 3, "days_back": 14}
    wind = {"today": {"wind_speed_max": 15.0, "wind_direction_compass": "from the S"}}
    events = {"eonet": [{"category": "Wildfires", "title": "Fire A"}], "gdacs": []}
    signals2 = tools._conditions_signals(dryness, wind, events)
    assert "last rain ≥1mm was 3 days ago" in signals2
    assert "peak wind today 15 km/h from the S" in signals2
    assert "1 active wildfire within ~150 km" in signals2

    # 3. Multiple events case (and 1 day since rain)
    dryness = {"total_precipitation": 0.0, "days_since_last_rain": 1, "days_back": 14}
    wind = {}
    events = {
        "eonet": [{"category": "Severe Storms"}],
        "gdacs": [{"type": "WF"}, {"type": "WF"}]
    }
    signals3 = tools._conditions_signals(dryness, wind, events)
    assert "last rain ≥1mm was 1 day ago" in signals3
    assert "2 active wildfires within ~150 km" in signals3
    assert "1 active storm within ~150 km" in signals3


def t_conditions_weather_partitioning():
    # Save original functions
    orig_weather = tools.weather_summary
    orig_events = tools.active_events
    orig_imagery = tools.search_imagery
    orig_next = tools.next_pass

    try:
        # Mock weather_summary
        # Phoenix example with N=3 on Aug 4:
        # daily array has 10 elements. today is index 3.
        # past & today has zero rain, forecast has rain.
        def mock_weather_summary(lat, lon, past_days):
            return {
                "units": {"precipitation_sum": "mm", "wind_speed_10m_max": "km/h"},
                "daily": {
                    "time": ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"],
                    "precipitation_sum": [0.0, 0.0, 0.0, 0.0, 1.4, 0.0, 0.0, 0.5, 4.5, 1.2],
                    "wind_speed_10m_max": [10.0, 10.0, 10.0, 45.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
                    "wind_direction_10m_dominant": [45, 45, 45, 45, 45, 45, 45, 45, 45, 45]
                }
            }

        def mock_active_events(bbox, pad):
            return {"eonet": [], "gdacs": []}

        def mock_search_imagery(catalog, collections, bbox, datetime_range, limit):
            return {"items": []}

        def mock_next_pass(lat, lon, days):
            return {"passes": []}

        tools.weather_summary = mock_weather_summary
        tools.active_events = mock_active_events
        tools.search_imagery = mock_search_imagery
        tools.next_pass = mock_next_pass

        # Run conditions_brief for N=3
        res = tools.conditions_brief(lat=33.4484, lon=-112.0740, days_back=3)

        # Assertions
        assert "weather_error" not in res
        dryness = res["dryness"]
        assert dryness["total_precipitation"] == 0.0
        assert dryness["days_since_last_rain"] is None
        assert "no rain ≥1mm in the last 3 days" in res["signals"]

        # Now test when there was rain on yesterday (index 2)
        def mock_weather_summary_rain(lat, lon, past_days):
            return {
                "units": {"precipitation_sum": "mm", "wind_speed_10m_max": "km/h"},
                "daily": {
                    "time": ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"],
                    # yesterday index 2 has 2.0mm
                    "precipitation_sum": [0.0, 0.0, 2.0, 0.0, 5.0],
                    "wind_speed_10m_max": [10.0, 10.0, 10.0, 45.0, 10.0],
                    "wind_direction_10m_dominant": [45, 45, 45, 45, 45]
                }
            }
        tools.weather_summary = mock_weather_summary_rain
        res_rain = tools.conditions_brief(lat=33.4484, lon=-112.0740, days_back=3)
        assert res_rain["dryness"]["total_precipitation"] == 2.0
        assert res_rain["dryness"]["days_since_last_rain"] == 1 # yesterday
        assert "last rain ≥1mm was 1 day ago" in res_rain["signals"]

    finally:
        tools.weather_summary = orig_weather
        tools.active_events = orig_events
        tools.search_imagery = orig_imagery
        tools.next_pass = orig_next


# smallest valid PNG (1x1) — the card only has to embed bytes, not decode them
CANNED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _postcard(**kw) -> str:
    return tools._postcard_html(
        CANNED_PNG, "Torres del Paine", "2026-07-10", "sentinel-2-l2a",
        tools._catalog_source("earth-search", "sentinel-2-l2a"), **kw
    )


def t_postcard_embeds_pixels_not_urls():
    html = _postcard()
    assert "data:image/png;base64,iVBOR" in html
    # no live imagery URLs at all: nothing to expire, nothing to 404
    assert "token=" not in html and "sas=" not in html and "https://" not in html


def t_postcard_attribution_block():
    html = _postcard(license_="proprietary")
    assert "Development Seed" in html and "STAC" in html and "TiTiler" in html
    assert "sentinel-2-l2a via Element 84 Earth Search" in html
    assert "license: proprietary" in html
    assert "license:" not in _postcard()  # omitted, not left blank


def t_postcard_license_placeholder_omitted():
    # STAC's "proprietary" is a missing-SPDX-id marker, not a terms claim
    assert tools._shareable_license("proprietary") is None
    assert tools._shareable_license("various") is None
    assert tools._shareable_license(None) is None
    assert tools._shareable_license("CC-BY-4.0") == "CC-BY-4.0"


def t_postcard_spread_fits_viewport():
    # a tall card must never overflow the browser: image capped to the
    # viewport, info beside it when there's room (flex-wrap, no media query)
    html = _postcard()
    assert "max-height: 86vh" in html and "flex-wrap: wrap" in html
    assert '<div class="fig">' in html and "object-fit: contain" in html


def t_postcard_no_local_paths_and_small():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "card.html"
        out.write_text(_postcard(caption="First light after the storm."), encoding="utf-8")
        html = out.read_text(encoding="utf-8")
        assert "/Users/" not in html and "file://" not in html
        assert "First light after the storm." in html
        assert out.stat().st_size < 5 * 1024 * 1024


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


def t_render_map_3d_lazy_extra_coverage():
    # extras embed but must not load upfront: sources live in EXTRAS, not STYLE
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m3d.html")
        layer = {"type": "item", "name": "s2", "catalog": "earth-search",
                 "collection_id": "sentinel-2-l2a", "item_id": "MAIN", "bbox": [0, 0, 1, 1]}
        extra = {"type": "item", "catalog": "earth-search", "collection_id": "sentinel-2-l2a",
                 "item_id": "GAPFILL", "bbox": [1, 0, 2, 1]}
        tools.render_map_3d("t", [0, 0, 2, 1], layer, out_path=out, extra_layers=[extra])
        html = Path(out).read_text(encoding="utf-8")
        assert 'id="loadmore"' in html and "Load full coverage (1 more)" in html
        style_json = html.split("const STYLE = ")[1].split("\nconst BBOX")[0]
        assert "GAPFILL" not in style_json and "GAPFILL" in html
        assert '"imagery"' in html.split("const EXTRAS = ")[1]  # fillers insert beneath the main drape
        # slow tiles need visible progress: button shows loading, footprint
        # outlines mark where pixels will land, both clear on map idle
        assert "Loading " in html and "-pending" in html and 'once("idle"' in html
    assert 'id="loadmore"' not in _render_3d()  # no extras, no button, zero extra bytes


def t_render_map_3d_controls():
    html = _render_3d(exaggeration=2.5)
    assert 'id="exaggeration"' in html and 'id="flythrough"' in html and 'id="reset"' in html
    assert 'value="2.5"' in html and "let exaggeration = 2.5" in html


# ---- stack layer (epic G) ----

from groundstation import stack as gstack  # noqa: E402


def t_stack_parse_all_fields():
    comps = gstack.parse_stack()
    assert len(comps) >= 12
    for c in comps:
        for field in ("name", "kind", "what", "ds-role", "integration", "speaks-to", "link"):
            assert c.get(field), f"{c.get('name')} missing {field}"
        assert c["ds-role"] in gstack.DS_ROLES


def _bad_stack(body: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "stack.md"
        p.write_text(body, encoding="utf-8")
        try:
            gstack.parse_stack(p)
            return ""
        except ValueError as e:
            return str(e)


_OK_BLOCK = "- kind: data\n- what: x\n- ds-role: uses\n- integration: x\n- speaks-to: x\n- link: https://x\n"


def t_stack_parse_curation_mistakes_fail_loudly():
    err = _bad_stack("## Quantum\n" + _OK_BLOCK.replace("kind: data", "kind: quantum"))
    assert "Quantum" in err and "quantum" in err
    assert "missing" in _bad_stack("## Thin\n- kind: data\n- ds-role: uses\n")
    assert "duplicate" in _bad_stack(f"## Twin\n{_OK_BLOCK}\n## Twin\n{_OK_BLOCK}")
    assert "empty" in _bad_stack("## \n" + _OK_BLOCK)


def t_stack_parse_leading_heading_not_dropped():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "stack.md"
        p.write_text("## First\n" + _OK_BLOCK, encoding="utf-8")  # no preamble at all
        assert [c["name"] for c in gstack.parse_stack(p)] == ["First"]


_STACK_FACTS = {"catalogs": ["earth-search"], "collections_by_catalog": {"earth-search": ["sentinel-2-l2a"]},
                "tiler_hosts": ["titiler.xyz"], "maplibre": True, "terrain": False, "geocoded": True, "events": False}


def t_stack_join_names_the_real_render():
    entries = gstack.stack_instances(gstack.parse_stack(), _STACK_FACTS)
    tiler = next(e for e in entries if e["name"] == "TiTiler")
    assert tiler["instance"] == "serving sentinel-2-l2a via titiler.xyz"
    es = next(e for e in entries if e["name"] == "Earth Search")
    assert es["instance"] == "source of sentinel-2-l2a"
    assert not any(e["name"] == "AWS Terrarium terrain" for e in entries)  # no terrain on a 2D map
    assert not any(e["name"] == "NASA EONET" for e in entries)  # no events layer


def t_stack_join_understates_without_rasters():
    # a geojson-only map exercised no catalog, tiler, or bucket — claiming
    # them would fabricate provenance
    entries = gstack.stack_instances(gstack.parse_stack(), {"catalogs": [], "maplibre": True})
    assert [e["name"] for e in entries] == ["MapLibre GL"]
    # even the renderer is a fact: a static artifact (postcard) claims no engine
    assert gstack.stack_instances(gstack.parse_stack(), {"catalogs": []}) == []


def t_stack_active_names_exist_in_stack_md():
    # the join string-matches component names; a stack.md heading rename must
    # fail here instead of silently vanishing from every panel
    names = {c["name"] for c in gstack.parse_stack()}
    wired = {"MapLibre GL", "STAC", "COG + HTTP range requests", "TiTiler", "rio-tiler",
             "Cloud object storage", "AWS Terrarium terrain", "Gazet", "Nominatim",
             "NASA EONET", "GDACS", "Open-Meteo", *gstack._CATALOG_COMPONENT.values()}
    assert wired <= names, f"wired names missing from stack.md: {sorted(wired - names)}"


def t_stack_group_order_and_attribution_shape():
    entries = gstack.stack_instances(gstack.parse_stack(), _STACK_FACTS)
    # literal expected pipeline order for this fixture (geocoded=True adds the
    # two access-kind geocoders), not a re-derivation
    assert [e["kind"] for e in entries] == ["data", "access", "access", "access", "tiling", "viz", "standard", "infra"]
    html = tools._stack_panel_html(entries)
    assert 'class="stack-group"' in html and "TiTiler" in html
    for role in {e["ds-role"] for e in entries}:
        assert role in gstack.DS_ROLES  # attribution is role-shaped by construction
    # and the rendered panel carries no person-shaped attribution
    assert not re.search(r"\b(?:by|from)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b", html)


def t_stack_panel_escapes_untrusted_values():
    entries = [{"name": "x", "kind": "data", "what": "<script>alert(1)</script>",
                "ds-role": "uses", "instance": 'onerror="x" <img>', "link": 'https://x/"><script>'}]
    html = tools._stack_panel_html(entries)
    assert "<script>" not in html and "<img>" not in html
    assert 'href="https://x/&quot;&gt;&lt;script&gt;"' in html


def t_stack_no_ansi_when_piped_static():
    # unit checks stay offline, so this is the static form of the piped-output
    # rule: no literal escape bytes, and every color assignment sits behind
    # the TTY guard
    for script in ("scripts/doctor.sh", "briefing/run.sh"):
        text = (Path(__file__).resolve().parents[1] / script).read_text(encoding="utf-8")
        assert "\x1b" not in text, f"{script} has a literal ESC byte"
        assert "[ -t 1 ]" in text and "NO_COLOR" in text, f"{script} missing the TTY/NO_COLOR guard"


def _render_stack_map(**kw) -> tuple[str, dict]:
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        layers = [{"type": "item", "name": "s2", "catalog": "earth-search",
                   "collection_id": "sentinel-2-l2a", "item_id": "X", "bbox": [0, 0, 1, 1]}]
        r = tools.render_map("t", [0, 0, 1, 1], layers, out_path=out, **kw)
        return Path(out).read_text(encoding="utf-8"), r


def t_stack_layer_toggle_present_when_on():
    html, _ = _render_stack_map(stack_layer=True)
    assert 'id="stack-toggle"' in html and 'id="stack"' in html
    assert "sentinel-2-l2a" in html and "prefers-reduced-motion" in html


def t_stack_layer_absent_by_default():
    html, r = _render_stack_map()
    assert 'id="stack-toggle"' not in html and 'class="stack-entry"' not in html
    assert "note" not in r


def t_stack_layer_missing_stack_md_skips_gracefully():
    real = gstack.parse_stack

    def gone(path=None):
        raise FileNotFoundError("stack.md")

    gstack.parse_stack = gone
    try:
        html, r = _render_stack_map(stack_layer=True)
        assert 'id="stack-toggle"' not in html and "stack.md" in r["note"]
    finally:
        gstack.parse_stack = real


# ---- stack layer on the remaining surfaces (G.3) ----


def t_stack_passes_claims_eo_predictor():
    # next_pass returns data, not an artifact, so the panel can only learn it
    # from a caller-declared fact — and must not claim it otherwise
    comps = gstack.parse_stack()
    on = gstack.stack_instances(comps, {"catalogs": ["earth-search"], "maplibre": True, "passes": True})
    assert any(c["name"] == "eo-predictor" and c["ds-role"] == "created" for c in on)
    off = gstack.stack_instances(comps, {"catalogs": ["earth-search"], "maplibre": True})
    assert not any(c["name"] == "eo-predictor" for c in off)


def t_stack_3d_claims_terrain():
    html = _render_3d(stack_layer=True)
    assert 'id="stack-toggle"' in html and 'id="stack"' in html
    assert "AWS Terrarium terrain" in html and "sentinel-2-l2a" in html
    assert 'id="stack-toggle"' not in _render_3d()  # off by default, bytes unchanged


def t_stack_postcard_listing_static_and_honest():
    listing = tools._stack_credit_for("earth-search", "sentinel-2-l2a", "titiler.xyz")
    html = _postcard(stack_html=listing)
    assert "the stack behind this card:" in html
    assert "serving sentinel-2-l2a via titiler.xyz" in html
    assert "MapLibre" not in html  # a still image runs no map engine
    assert "https://" not in html  # the no-live-URLs guarantee survives the listing
    assert "the stack behind this card:" not in _postcard()  # off by default


def t_stack_map_honest_extra_facts():
    # callers that geocoded / fetched events say so; the panel only then claims it
    html, _ = _render_stack_map(stack_layer=True, stack_facts={"geocoded": True, "events": True})
    for name in ("Gazet", "Nominatim", "NASA EONET", "GDACS", "Open-Meteo"):
        assert name in html, f"{name} missing despite honest facts"
    base, _ = _render_stack_map(stack_layer=True)
    assert "Gazet" not in base and "NASA EONET" not in base


def t_stack_infra_names_only_buckets_on_screen():
    # the Rainier confusion: a pure earth-search map must never mention Azure —
    # the infra entry is instance-specific like every other claim
    comps = gstack.parse_stack()
    e = next(x for x in gstack.stack_instances(comps, {**_STACK_FACTS, "terrain": True})
             if x["name"] == "Cloud object storage")
    assert e["instance"] == "streaming from AWS S3 (sentinel-cogs, terrain tiles)"
    v = next(x for x in gstack.stack_instances(
        comps, {"catalogs": ["veda"], "collections_by_catalog": {"veda": ["fire-severity"]}})
        if x["name"] == "Cloud object storage")
    assert v["instance"] == "streaming from Azure Blob (VEDA)"
    assert "Azure" not in e["instance"] and "sentinel-cogs" not in v["instance"]


def t_stack_panel_depth_on_demand():
    # collapsed = name + role + instance; what/speaks-to/link revealed per entry
    entries = gstack.stack_instances(gstack.parse_stack(), _STACK_FACTS)
    html = tools._stack_panel_html(entries)
    assert html.count("<details") == len(entries) and "<summary>" in html
    assert "speaks to " in html and 'class="spk"' in html


def t_stack_mosaic_card_honesty_and_ds_marks():
    # a mosaic card credits rio-tiler and NOT TiTiler — no tiler served it
    comps = gstack.parse_stack()
    entries = gstack.stack_instances(comps, {
        "catalogs": ["earth-search"],
        "collections_by_catalog": {"earth-search": ["sentinel-2-l2a"]},
        "mosaic_scenes": 2,
    })
    names = [e["name"] for e in entries]
    assert "rio-tiler" in names and "TiTiler" not in names
    rt = next(e for e in entries if e["name"] == "rio-tiler")
    assert rt["instance"] == "mosaicked 2 scenes into one frame, first valid pixel wins"
    listing = tools._stack_credit_for("earth-search", "sentinel-2-l2a", None, mosaic_scenes=2)
    assert "rio-tiler" in listing and "TiTiler" not in listing
    # DS-built marks: filled badge in the panel, tinted name on the card —
    # created/maintains only, everything else stays muted
    html = tools._stack_panel_html(entries)
    assert 'class="role ds">created</span>' in html
    assert 'class="role">uses</span>' in html
    assert '<b class="ds">rio-tiler</b>' in listing


def t_snapshot_card_templates_and_facts():
    # both map templates carry the snapshot hooks: gsMaps for load-detection,
    # #clean to strip chrome (story elements — divider, side labels — stay)
    map_html, _ = _render_stack_map()
    for html in (map_html, _render_3d()):
        assert "window.gsMaps" in html and 'location.hash === "#clean"' in html
        assert ".clean #panel" in html
    # a snapshot card inherits the map's facts: MapLibre + events claimed
    # only because the view exercised them; imagery cards still claim neither
    facts = tools._map_stack_facts([], [{"type": "geojson", "name": "ev"}], {"events": True})
    listing = tools._stack_credit_html(facts)
    assert "MapLibre GL" in listing and "NASA EONET" in listing
    single = tools._stack_credit_for("earth-search", "sentinel-2-l2a", "titiler.xyz")
    assert "MapLibre" not in single and "EONET" not in single
    # cards take deliberate standard shapes, trimmed centrally from the bbox
    r, bb = tools._snap_aspect([0, 45, 2, 45.7], "map")  # wide box at 45N
    assert r in tools._CARD_RATIOS.values()
    assert bb[0] > 0 and bb[2] < 2 and (bb[0] + bb[2]) / 2 == 1.0  # trimmed lon, center kept
    assert tools._snap_aspect([0, 0, 1, 1], "compare")[0] == 2 / 3  # divider wants landscape
    assert tools._snap_aspect([0, 0, 1, 1], "3d")[0] == 2 / 3
    assert tools._snap_aspect([0, 0, 1, 1], "map", "2:3")[0] == 3 / 2  # explicit override
    try:
        tools._snap_aspect([0, 0, 1, 1], "map", "9:16")
        raise AssertionError("bad aspect must be loud")
    except ValueError:
        pass


def t_brand_tokens_in_all_templates():
    # one shared token set: DS orange accent present in map, 3D, and postcard output
    html, _ = _render_stack_map()
    assert "--accent: #CF3F02" in html
    assert "--accent: #CF3F02" in _render_3d()
    assert "--accent: #CF3F02" in _postcard()


def t_preview_bbox_crops_to_aoi():
    # the weird-postcard fix: bbox routes to the tiler's part endpoint so the
    # card frames the subject, not the whole scene with its nodata edge
    p = tools.preview_item("earth-search", "sentinel-2-l2a", "X", bbox=[-121.88, 46.73, -121.64, 46.97])
    assert "/stac/bbox/-121.88,46.73,-121.64,46.97.png" in p["preview_url"]
    assert "assets=visual" in p["preview_url"]
    v = tools.preview_item("veda", "c", "i", assets=["cog_default"], bbox=[1, 2, 3, 4])
    assert "/items/i/bbox/1,2,3,4.png" in v["preview_url"]
    assert "/stac/preview.png" in tools.preview_item("earth-search", "sentinel-2-l2a", "X")["preview_url"]
    assert tools._intersect_bbox([0, 0, 2, 2], [1, 1, 3, 3]) == [1, 1, 2, 2]
    assert tools._intersect_bbox([0, 0, 1, 1], [2, 2, 3, 3]) is None
    # clamping must not move the frame off the subject: trims shrink both
    # sides, so the AOI center (the mountain) stays the picture center
    assert tools._centered_clamp([0, 0, 4, 4], [1, 1, 10, 10]) == [1, 1, 3, 3]
    assert tools._centered_clamp([0, 0, 4, 4], [5, 5, 10, 10]) is None
    assert tools._centered_clamp([0, 0, 4, 4], [3.5, 0, 10, 4]) == [3.5, 0, 4, 4]  # center off-scene: plain clamp


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


def t_slack_line_justifies_its_own_tag():
    # the live Barotse case: WATCH tag, but the TL;DR opens with an all-clear
    # and the reason lives in sentence two — the line must carry the reason
    tldr = ("No active fire, flood, or storm alerts in or near the Barotse Floodplain. "
            "The signal worth flagging is vegetation. "
            "Alert level: **WATCH** — NDVI fell 10.8% against two rainless weeks.")
    p = brief.slack_payload([{"place": "Barotse", "alert": "WATCH", "tldr": tldr}], dt.date(2026, 7, 22))
    assert "NDVI fell 10.8%" in p["text"] and "No active fire" not in p["text"]
    # first sentence still wins when it already names the level (or for CALM)
    p2 = brief.slack_payload([{"place": "A", "alert": "CALM", "tldr": "Quiet everywhere. Nothing to flag."}],
                             dt.date(2026, 7, 22))
    assert "Quiet everywhere." in p2["text"]


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


def t_delta_caveat_chilika_regression():
    # Field test №7: NDVI over Chilika read -87.8% during the monsoon, the season
    # Odisha greens. Both reasons it was misleading are pinned here by their real
    # numbers — 51.7% cloud on the after scene, and a 0.0605 baseline.
    out = tools._delta_caveats(0.0605, 51.685119, 16.049817)
    assert len(out) == 2, out
    assert "cloud" in out[0] and "after scene 52%" in out[0], out[0]
    assert "delta_pct" in out[1] and "0.0605" in out[1], out[1]


def t_delta_caveat_clean_delta_says_nothing():
    # A clear pair over a vegetated baseline earns no caveat, or the field is noise.
    assert tools._delta_caveats(0.62, 3.0, 4.0) == []


def t_delta_caveat_before_scene_alone_trips_it():
    out = tools._delta_caveats(0.55, 2.0, 40.0)
    assert len(out) == 1 and "before scene 40%" in out[0], out
    assert "after" not in out[0], out


def t_delta_caveat_handles_missing_cloud():
    # Sentinel-1 and friends report cloud_cover as None; that is not "0% cloud".
    assert tools._delta_caveats(0.55, None, None) == []


def t_bbox_feature_closes_its_ring():
    f = tools._bbox_feature([85.09, 19.46, 85.65, 19.90])
    ring = f["geometry"]["coordinates"][0]
    assert f["type"] == "Feature" and f["geometry"]["type"] == "Polygon"
    assert len(ring) == 5 and ring[0] == ring[-1] == [85.09, 19.46], ring
    assert [85.65, 19.90] in ring, ring


def t_partial_coverage_threshold_is_a_named_knob():
    # tool-honesty 1.2: below the constant a rendered map says it has a hole,
    # at and above it stays quiet. Pinned at the boundary so an edit to the
    # threshold cannot move it silently.
    def run(east):
        with tempfile.TemporaryDirectory() as d:
            layer = [{"type": "raster", "name": "r", "tiles": "https://x/{z}/{x}/{y}.png",
                      "bounds": [0, 0, east, 10]}]
            return tools.render_map("t", [0, 0, 10, 10], layer, out_path=str(Path(d) / "m.html"))
    assert tools.PARTIAL_COVERAGE_PCT == 95.0
    below = run(9.4)
    assert "coverage_note" in below and "94%" in below["coverage_note"], below
    assert "coverage_note" not in run(9.5), "exactly at the threshold must stay quiet"
    assert "coverage_note" not in run(9.6)


def t_empty_dataset_search_says_what_it_searched():
    # tool-honesty 2.1: himawari is real and not in these catalogs. The bare []
    # was the return most likely to become "no such data exists" in prose.
    saved = tools._collections
    tools._collections = lambda cat: [
        {"id": "sentinel-2-l2a", "title": "Sentinel-2", "description": "", "keywords": []}
    ]
    try:
        out = tools.search_datasets("himawari")
        assert len(out) == 1 and out[0]["searched"] == list(tools.CATALOGS), out
        assert "does not mean the data does not exist" in out[0]["note"], out
        one = tools.search_datasets("himawari", catalog="veda")
        assert one[0]["searched"] == ["veda"], one
        hit = tools.search_datasets("sentinel")
        assert hit == [{"catalog": c, "id": "sentinel-2-l2a", "title": "Sentinel-2", "summary": ""}
                       for c in tools.CATALOGS], hit
        assert not any("note" in h for h in hit)
    finally:
        tools._collections = saved


def t_recommended_is_the_covering_scene_not_the_clearest():
    # Field test No.7, Chilika: the two cleanest scenes saw a fifth of the lagoon.
    items = [
        {"id": "S2B_45QUC_20260605_1_L2A", "datetime": "2026-06-05T04:50:00Z", "cloud_cover": 2.2, "covers_aoi_pct": 24.2},
        {"id": "S2B_45QTB_20260605_0_L2A", "datetime": "2026-06-05T04:50:00Z", "cloud_cover": 5.0, "covers_aoi_pct": 17.2},
        {"id": "S2C_45QUB_20260610_0_L2A", "datetime": "2026-06-10T04:50:00Z", "cloud_cover": 21.9, "covers_aoi_pct": 97.9},
    ]
    before = [it["id"] for it in items]
    rec = tools._recommend(items)
    assert rec["id"] == "S2C_45QUB_20260610_0_L2A", rec
    assert "21.9% cloud but covers 97.9%" in rec["reason"] and "2.2% cloud" in rec["reason"], rec
    assert [it["id"] for it in items] == before, "recommended is additive: the list order is untouched"


def t_recommended_says_when_it_is_not_a_tradeoff():
    items = [{"id": "a", "datetime": "2026-01-02", "cloud_cover": 1.0, "covers_aoi_pct": 100.0},
             {"id": "b", "datetime": "2026-01-01", "cloud_cover": 9.0, "covers_aoi_pct": 100.0}]
    rec = tools._recommend(items)
    assert rec["id"] == "a" and "not a tradeoff" in rec["reason"], rec


def t_recommended_handles_sar_and_all_cloudy():
    # Sentinel-1 reports cloud_cover as None; that is not a reason to skip it
    sar = [{"id": "s1", "datetime": "2026-01-01", "cloud_cover": None, "covers_aoi_pct": 100.0}]
    assert "no cloud cover" in tools._recommend(sar)["reason"]
    cloudy = [{"id": "c1", "datetime": "2026-01-01", "cloud_cover": 80.0, "covers_aoi_pct": 100.0},
              {"id": "c2", "datetime": "2026-01-02", "cloud_cover": 60.0, "covers_aoi_pct": 50.0}]
    rec = tools._recommend(cloudy)
    assert rec["id"] == "c1" and "least bad" in rec["reason"], rec
    assert tools._recommend([]) is None


class _FakeResp:
    def __init__(self, payload, ctype="application/json"):
        self._p, self.headers = payload, {"content-type": ctype}

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


_GAZET_CHELAN = {"ids": [
    {"source": "divisions_area", "id": "4017061d", "name": "Chelan County", "matched_name": "Chelan County",
     "country": "US", "subtype": "county", "admin_level": 2,
     "bbox": [-121.18091, 47.2609433, -119.859708, 48.5509464], "similarity": 1.0, "is_substring_match": True},
    {"source": "divisions_area", "id": "597dabcd", "name": "McLean County", "country": "US", "subtype": "county",
     "bbox": [-102.386505, 47.157902, -100.58461, 47.849483], "similarity": 0.9209, "is_substring_match": False},
]}
_GAZET_SALINAS = {"ids": [
    {"source": "divisions_area", "id": "5a372c80", "name": "Salinas", "country": "MX", "subtype": "county",
     "bbox": [-101.9460654, 22.4650338, -101.3759004, 23.1814428], "similarity": 0.9, "is_substring_match": False},
]}
_GAZET_LOJA = {"ids": [
    {"source": "divisions_area", "id": "4ad73a51", "name": "Loja Province", "country": "EC", "subtype": "region",
     "bbox": [-80.4846769, -4.7488756, -79.1006936, -3.3295641], "similarity": 1.0, "is_substring_match": True},
    {"source": "divisions_area", "id": "d6082edf", "name": "Loja Canton", "country": "EC", "subtype": "county",
     "bbox": [-79.5457957, -4.5114788, -79.1006936, -3.6728594], "similarity": 1.0, "is_substring_match": True},
    {"source": "divisions_area", "id": "fdd5690f", "name": "Maloja", "country": "CH", "subtype": "county",
     "bbox": [9.4558, 46.2939, 10.1003, 46.6987], "similarity": 0.7222, "is_substring_match": True},
]}
_NOMINATIM_SALINAS = [{"boundingbox": ["36.2", "36.8", "-121.9", "-121.3"], "lat": "36.5", "lon": "-121.6",
                       "display_name": "Salinas Valley, Monterey County, California"}]


def _with_geocoders(gazet_payload, nominatim_payload, fn, ctype="application/json"):
    saved_get, saved_json, saved_skip = tools._client.get, tools._get_json, tools._gazet_skip_until
    calls = {"gazet": 0, "nominatim": 0}

    def fake_get(url, **kw):
        calls["gazet"] += 1
        assert url == tools.GAZET_URL and kw["params"]["mode"] == "fuzzy" and kw["params"]["ids_only"] == "true", kw
        return _FakeResp(gazet_payload, ctype)

    def fake_json(url, **kw):
        calls["nominatim"] += 1
        return nominatim_payload

    tools._client.get, tools._get_json, tools._gazet_skip_until = fake_get, fake_json, 0.0
    try:
        return fn(), calls
    finally:
        tools._client.get, tools._get_json, tools._gazet_skip_until = saved_get, saved_json, saved_skip


def t_geocode_gazet_answers_first_with_the_real_extent():
    # Field tests No.4 to No.7 all filed "our own geocoder never fires". The JSON API
    # was live on the Space the whole time; the ds.io domain fronts a static page.
    out, calls = _with_geocoders(_GAZET_CHELAN, [], lambda: tools.geocode("Chelan County"))
    assert out["source"] == "gazet" and out["name"] == "Chelan County (US, county)", out
    assert out["bbox"] == [-121.18091, 47.2609433, -119.859708, 48.5509464], out
    assert "geocode_note" not in out, out
    assert calls == {"gazet": 1, "nominatim": 0}, calls


def t_geocode_similarity_gate_sends_near_misses_to_nominatim():
    # Jaro-Winkler always returns a best row. "Salinas Valley" scored 0.9 as
    # Salinas, Mexico — the gate is what stops a confident wrong continent.
    out, calls = _with_geocoders(_GAZET_SALINAS, _NOMINATIM_SALINAS, lambda: tools.geocode("Salinas Valley"))
    assert out["source"] == "nominatim" and out["bbox"] == [-121.9, 36.2, -121.3, 36.8], out
    assert calls == {"gazet": 1, "nominatim": 1}, calls
    assert tools.GAZET_MIN_SIMILARITY > 0.9


def t_geocode_names_the_tie_it_broke():
    # Two real Lojas score 1.0. Picking one silently is a decision the caller cannot see.
    out, _ = _with_geocoders(_GAZET_LOJA, [], lambda: tools.geocode("Loja"))
    assert out["source"] == "gazet" and out["name"] == "Loja Province (EC, region)", out
    assert "2 places match 'Loja'" in out["geocode_note"] and "Loja Canton (EC, county)" in out["geocode_note"], out
    assert "Maloja" not in out["geocode_note"], "a 0.72 near miss is not a tie"


def t_geocode_non_json_marks_gazet_down_and_falls_through():
    out, calls = _with_geocoders({}, _NOMINATIM_SALINAS, lambda: tools.geocode("Salinas Valley"), ctype="text/html")
    assert out["source"] == "nominatim" and calls["nominatim"] == 1, (out, calls)


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(name, fn)
    print(f"\n{len([k for k in globals() if k.startswith('t_')]) - len(FAILED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
