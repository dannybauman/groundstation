"""Offline unit checks — deterministic, no network. This is what CI runs.

    uv run evals/unit_checks.py
"""

from __future__ import annotations

import math
import base64
import json
import os
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

# artifacts may only land under GROUNDSTATION_OUT or the cwd tree; the suite's
# tempdirs are neither, so the suite's out root is the system tempdir
os.environ["GROUNDSTATION_OUT"] = tempfile.gettempdir()


def _offline(*a, **k):
    raise RuntimeError("offline suite: no network")


# a layer without asset_href reads its item for the 429 fallback; offline that is a
# cached None, loud if anything expected a real answer
tools._get_json = _offline

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


def t_coverage_set_nameless_reads_collection_and_date():
    def names(coll, date, **kw):
        fcs = {"date": date, "items": [{"catalog": "planetary-computer", "collection": coll, "id": "T1", "bbox": [0, 0, 1, 1]}]}
        return [(l["name"], l["detail"]) for l in tools._expand_coverage_set({"type": "coverage_set", "set": fcs, **kw})]
    assert names("esa-worldcover", "2021-01-01") == [("ESA WorldCover 2021", "T1")]
    assert names("esa-worldcover", "2020-01-01") == [("ESA WorldCover 2020", "T1")]
    assert names("esa-worldcover", "2021-01-01", name="Mine") == [("Mine", "T1")]
    assert names("sentinel-2-l2a", "2024-05-01") == [("sentinel-2-l2a 2024-05-01", "T1")]


def t_next_pass_pure_helpers():
    # one dip under the swath line = one pass; a shallow approach = none
    assert tools._pass_minima([300, 100, 50, 100, 300], 145) == [2]
    assert tools._pass_minima([300, 200, 300], 145) == []
    # plateau at the minimum still counts once (<= left, < right)
    assert tools._pass_minima([300, 50, 50, 300], 145) == [2]
    # sun elevation (NOAA approximation, about 1 degree)
    U = dt.timezone.utc
    sun = tools._sun_elevation_deg
    assert sun(dt.datetime(2026, 3, 20, 12, 0, tzinfo=U), 0, 0) > 85
    assert abs(sun(dt.datetime(2026, 6, 21, 12, 0, tzinfo=U), 0, 0) - 66.5) < 1.5
    assert sun(dt.datetime(2026, 10, 26, 12, 29, tzinfo=U), 78.22, 15.65) < 0  # Longyearbyen, polar night
    assert 30 < sun(dt.datetime(2026, 10, 28, 19, 13, tzinfo=U), 38.82, -123.19) < 40


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


def t_3d_fills_short_coverage_with_same_day_siblings():
    main = {"type": "item", "catalog": "earth-search", "collection_id": "c", "item_id": "A_1",
            "assets": ["visual"], "bbox": [0, 0, 6, 10]}
    sib = {"id": "A_2", "collection": "c", "datetime": "2026-10-23T19:00:00Z", "bbox": [5, 0, 10, 10]}
    main_it = {**sib, "id": "A_1", "bbox": [0, 0, 6, 10]}
    o_img, o_item, o_tile = tools.search_imagery, tools._item, tools.tile_url_template
    tools.search_imagery = lambda *a, **kw: {"items": [main_it, sib]}
    tools._item = lambda cat, coll, iid: {"properties": {"datetime": "2026-10-23T19:00:00Z"}}
    tools.tile_url_template = lambda cat, coll, iid, *a, **k: f"https://x/{iid}/{{z}}/{{x}}/{{y}}.png"
    try:
        with tempfile.TemporaryDirectory() as d:
            r = tools.render_map_3d("t", [0, 0, 10, 10], main, out_path=str(Path(d) / "a.html"))
            s = r["state"]
            assert r["filled_with"] == ["A_2"] and s["coverage_pct"] == 100.0
            assert [l["name"] for l in s["layers"]] == ["A_1", "A_2"]
            assert not s["layers"][1].get("deferred") and "sibling" in s["layers"][1]["reason"]
            assert "coverage_note" not in r
            html = Path(r["path"]).read_text()
            assert "https://x/A_2/" in html and "sibling0" in html
            r = tools.render_map_3d("t", [0, 0, 10, 10], main, fill_coverage=False, out_path=str(Path(d) / "b.html"))
            assert r["state"]["coverage_pct"] == 60.0 and "filled_with" not in r
            assert len(r["state"]["layers"]) == 1
    finally:
        tools.search_imagery, tools._item, tools.tile_url_template = o_img, o_item, o_tile


def t_field_test_etime_parser():
    # the field-test preflight compares MCP server start time against source
    # mtime; getting ps -o etime parsing wrong makes the warning silently never
    # fire, which is the failure it exists to prevent
    import importlib.util
    fp = Path(__file__).resolve().parents[1] / "src" / "groundstation" / "field_test.py"
    spec = importlib.util.spec_from_file_location("_ft", fp)
    ft = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ft)
    assert ft._etime_seconds("05:30") == 330
    assert ft._etime_seconds("01:00:00") == 3600
    assert ft._etime_seconds("2-03:00:00") == 2 * 86400 + 3 * 3600
    assert ft._etime_seconds("") is None
    assert ft._etime_seconds("not-a-time") is None


def t_map_draws_legend_for_worldcover():
    with tempfile.TemporaryDirectory() as d:
        def lay(cm):
            return [{"type": "raster", "name": "r", "tiles": "https://x/{z}/{x}/{y}.png", "colormap_name": cm}]
        out = str(Path(d) / "a.html")
        r = tools.render_map("t", [0, 0, 1, 1], lay("esa-worldcover"), out_path=out)
        html = Path(r["map_path"]).read_text()
        assert "ESA WorldCover 2021" in html and "Cropland" in html and "Built-up" in html
        assert len(r["legend"]["classes"]) == 11 and r["legend"]["classes"][3]["name"] == "Cropland"
        r = tools.render_map("t", [0, 0, 1, 1], lay("viridis"), out_path=str(Path(d) / "b.html"))
        assert "legend" not in r and 'id="legends"' not in Path(r["map_path"]).read_text()


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


def t_geojson_polygons_are_filled():
    # ForTy forest classes drawn as outlines only were unreadable over imagery;
    # a polygon layer gets a fill, and the layer toggle has to hide it too
    gj = {"type": "FeatureCollection", "features": []}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.html"
        tools.render_map("t", [0, 0, 10, 10], [{"type": "geojson", "name": "x", "data": gj}], out_path=str(p))
        html = p.read_text()
        assert '"-area", type: "fill"' in html and 'id + "-area", id + "-line"' in html


def t_local_raster_file_goes_on_the_map():
    # ForTy had no STAC item and signed links no tiler could follow, so its
    # classes went on as 900 KB of polygons. A file layer bakes the raster in;
    # its nodata (the sea, 255) stays clear and a south-up file stays north-up
    import base64 as b64
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import Affine

    a = np.full((20, 20), 255, "uint8")
    a[:10, 10:] = 1  # rows 0-9 are SOUTH in a south-up file
    a[10:, 10:] = 2
    with tempfile.TemporaryDirectory() as d:
        tif = Path(d) / "classes.tif"
        with rasterio.open(tif, "w", driver="GTiff", width=20, height=20, count=1, dtype="uint8",
                           crs="EPSG:4326", nodata=255, transform=Affine(0.1, 0, 10, 0, 0.1, 0)) as w:
            w.write(a, 1)
        out = tools.render_map("t", [10, 0, 12, 2], [{"type": "file", "name": "classes", "path": str(tif),
                               "colormap": {"1": "#ff0000", "2": "#0000ff"}}], out_path=str(Path(d) / "m.html"))
        st = out["state"]["layers"][0]
        assert st["kind"] == "raster" and st["local"] and st["bounds"] == [10, 0, 12, 2], st
        html = Path(out["map_path"]).read_text()
        assert 'type: "image"' in html and "rio-tiler" in html
        png = b64.b64decode(html.split("data:image/png;base64,")[1].split('"')[0])
        with MemoryFile(png) as m, m.open() as r:
            img = r.read()
        h, wd = img.shape[1:]
        assert img[3, h // 4, wd // 4] == 0, "nodata (west half) should be transparent"
        north, south = img[:3, h // 4, 3 * wd // 4], img[:3, 3 * h // 4, 3 * wd // 4]
        assert list(north) == [0, 0, 255] and list(south) == [255, 0, 0], (north, south)
        vrt = Path(d) / "x.vrt"
        vrt.write_text(f"<VRTDataset><SimpleSource><SourceFilename>{tif}</SourceFilename></SimpleSource></VRTDataset>")
        for bad in (str(vrt), "/vsicurl/http://127.0.0.1/x.tif", "http://169.254.169.254/x.tif"):
            try:
                tools.render_map("t", [10, 0, 12, 2], [{"type": "file", "path": bad}], out_path=str(Path(d) / "b.html"))
            except Exception:
                continue
            raise AssertionError(f"file layer opened {bad}")


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


def t_render_map_tiles_carry_origin_key():
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        tools.render_map("t", [0, 0, 1, 1], [{"type": "raster", "name": "A", "tiles": "https://x/{z}/{x}/{y}.png"}], out_path=out)
        html = Path(out).read_text(encoding="utf-8")
        assert "const originKey = (u) =>" in html
        assert "tiles: [originKey(l.tiles)]" in html and "setTiles([originKey(l.fallback_tiles)])" in html


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


def t_every_skill_names_its_folder():
    # the plugin ships each skills/<dir>/SKILL.md; a frontmatter name that drifts
    # from its folder, or a missing description, leaves the skill unfindable
    root = Path(__file__).resolve().parents[1]
    for p in sorted((root / "skills").glob("*/SKILL.md")):
        head = p.read_text(encoding="utf-8").split("---")[1]
        assert re.search(rf"^name: {re.escape(p.parent.name)}$", head, re.M), f"{p.parent.name}: name does not match folder"
        assert re.search(r"^description: \S", head, re.M), f"{p.parent.name}: no description"


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


def t_compare_dates_clips_means_to_bbox():
    # Field tests 4, 8, 10, 11: the delta was a whole-tile number for the place asked about
    saved = (tools.search_imagery, tools.compute_statistics, tools.render_map)
    box = [-121.5, 39.4, -121.4, 39.5]
    seen = []

    def item(i, d):
        return {"id": i, "collection": "sentinel-2-l2a", "datetime": d + "T18:00:00Z",
                "cloud_cover": 1, "bbox": [-122, 39, -121, 40]}

    tools.search_imagery = lambda *a, **kw: {"items": [item("S2A_10TFK_20260601_0_L2A", "2026-06-01"),
                                                       item("S2A_10TFK_20260501_0_L2A", "2026-05-01")]}

    def fake_stats(cat, coll, iid, **kw):
        seen.append(kw.get("bbox"))
        return {"nd": {"mean": 0.5}, "clipped_to": kw.get("bbox")}

    tools.compute_statistics = fake_stats
    maps = []
    tools.render_map = lambda **kw: maps.append(kw) or {"map_path": "x.html"}
    try:
        out = tools.compare_dates(bbox=box, window_before="a/b", window_after="c/d", stack_facts={"terrain": True})
        assert maps[0]["stack_facts"] == {"terrain": True}, maps[0]["stack_facts"]
        assert seen == [box, box], seen
        assert out["clipped_to"] == box and "extent_note" not in out
    finally:
        tools.search_imagery, tools.compute_statistics, tools.render_map = saved


def t_compare_dates_carries_band_scaling_and_names_factors():
    saved = (tools.search_imagery, tools.compute_statistics, tools.render_map)
    item = lambda i, d: {"id": i, "collection": "sentinel-2-l2a", "datetime": d + "T18:00:00Z", "cloud_cover": 1, "bbox": [-122, 39, -121, 40]}
    wins = iter([[item("S2A_10TFK_20230328_0_L2A", "2023-03-28")], [item("S2A_10TFK_20210203_0_L2A", "2021-02-03")]])
    tools.search_imagery = lambda *a, **kw: {"items": next(wins)}
    groups = {
        "S2A_10TFK_20230328_0_L2A": [{"scale": 0.0001, "declared_offset": -0.1, "offset": 0, "bands": ["a"] * 13}],
        "S2A_10TFK_20210203_0_L2A": [{"scale": 0.0001, "offset": -0.1, "bands": ["a"] * 13}],
    }
    tools.compute_statistics = lambda cat, coll, iid, **kw: {"nd": {"mean": 0.5}, "scaling_applied": True, "band_scaling": groups[iid],
                                                             **({"range_note": "r"} if "2021" in iid else {})}
    tools.render_map = lambda **kw: {"map_path": "x.html"}
    try:
        out = tools.compare_dates(bbox=[-121.5, 39.4, -121.4, 39.5], window_before="a/b", window_after="c/d")
        assert out["after"]["band_scaling"][0]["declared_offset"] == -0.1 and out["before"]["band_scaling"][0]["offset"] == -0.1
        assert out["before"]["range_note"] == "r" and "range_note" not in out["after"] and "range_note" not in out, "per date"
        n = out["scaling_note"]
        assert "offset -0.1 applied for 2021-02-03" in n and "offset 0 applied for 2023-03-28 (Earth Search already took the -0.1 offset out of its pixels)" in n, n
    finally:
        tools.search_imagery, tools.compute_statistics, tools.render_map = saved


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


def t_postcard_legend_row():
    html = _postcard(legends=tools._legends_for([], "esa-worldcover"))
    assert "ESA WorldCover 2021" in html and "Mangroves" in html and "<script" not in html
    assert 'class="legend"' not in _postcard()


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


class _ImgResp:
    def __init__(self, status):
        self.status_code, self.content = status, CANNED_PNG


def _postcard_call(status=200, **kw):
    seen = []
    saved = tools._client.get, tools._get_json
    tools._client.get = lambda url, **k: (seen.append(url), _ImgResp(status))[1]
    tools._get_json = lambda url, **k: {"bbox": [-113, 33, -111, 34], "license": "x"}
    try:
        with tempfile.TemporaryDirectory() as d:
            out = tools.render_postcard(
                "planetary-computer", "landsat-c2-l2", "I", "P", "2026-08-17",
                out_path=str(Path(d) / "c.html"), **kw,
            )
            html = Path(out["path"]).read_text(encoding="utf-8") if "path" in out else ""
    finally:
        tools._client.get, tools._get_json = saved
    return out, seen, html


def t_postcard_pc_honors_assets_rescale_colormap_bbox():
    out, seen, html = _postcard_call(
        assets=["lwir11"], rescale="48000,57000", colormap_name="inferno",
        bbox=[-112.25, 33.35, -111.95, 33.60],
    )
    u = seen[0]
    assert "/api/data/v1/item/bbox/" in u and "assets=lwir11" in u
    assert "rescale=48000%2C57000" in u and "colormap_name=inferno" in u
    assert "dropped" not in out and "not applied" not in html


def t_postcard_pc_no_params_keeps_prerendered_preview():
    saved = tools._get_json
    seen = []
    try:
        tools._get_json = lambda url, **k: {"assets": {"rendered_preview": {"href": "https://pc/pre.png"}}, "license": "x"}
        saved_get = tools._client.get
        tools._client.get = lambda url, **k: (seen.append(url), _ImgResp(200))[1]
        with tempfile.TemporaryDirectory() as d:
            tools.render_postcard("planetary-computer", "landsat-c2-l2", "I", "P", "d", out_path=str(Path(d) / "c.html"))
    finally:
        tools._get_json, tools._client.get = saved, saved_get
    assert seen == ["https://pc/pre.png"]


def t_postcard_failed_image_is_an_error_not_a_card():
    out, seen, _ = _postcard_call(status=500, assets=["red"])
    assert "path" not in out and "HTTP 500" in out["error"] and out["url"] == seen[0]


def t_postcard_names_what_it_dropped():
    out, _, html = _postcard_call(bbox=[-112.25, 33.35, -111.95, 33.60], fill_item_ids=["J"])
    assert {d["param"] for d in out["dropped"]} == {"fill_item_ids"}
    assert "not applied: fill_item_ids" in html


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


def t_render_map_3d_camera_lifts_to_terrain():
    # field test 15: the Berkeley Pit box at zoom 14.5 put the camera at 1195 m under
    # 2865 m of exaggerated ground; an unfrozen ground height follows the DEM as it lands
    js = _render_3d().split('map.on("load"')[1].split("\n});")[0]
    assert "duration: 0, freezeElevation: true })" in js


# ---- stack layer (epic G) ----

from groundstation import stack as gstack  # noqa: E402


def t_stack_parse_all_fields():
    comps = gstack.parse_stack()
    assert len(comps) >= 12
    for c in comps:
        for field in ("name", "stage", "what", "ds-role", "integration", "speaks-to", "link", "when"):
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


_OK_BLOCK = "- stage: data\n- what: x\n- ds-role: uses\n- integration: x\n- speaks-to: x\n- link: https://x\n- when: always\n"


def t_stack_parse_curation_mistakes_fail_loudly():
    err = _bad_stack("## Quantum\n" + _OK_BLOCK.replace("stage: data", "stage: quantum"))
    assert "Quantum" in err and "quantum" in err
    assert "missing" in _bad_stack("## Thin\n- stage: data\n- ds-role: uses\n")
    assert "unknown fact" in _bad_stack("## Typo\n" + _OK_BLOCK.replace("when: always", "when: catalgs"))
    assert "unknown island" in _bad_stack("## Atlantis\n" + _OK_BLOCK + "- island: atlantis\n")
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
    # them would fabricate provenance. It did draw, on a basemap
    entries = gstack.stack_instances(gstack.parse_stack(), {"catalogs": [], "maplibre": True})
    assert [e["name"] for e in entries] == ["MapLibre GL", "OpenFreeMap", "OpenStreetMap"]
    # even the renderer is a fact: a static artifact (postcard) claims no engine
    assert gstack.stack_instances(gstack.parse_stack(), {"catalogs": []}) == []


def t_stack_when_rules_activate_the_right_components():
    # activation is data in stack.md, not code: each artifact shape below must
    # light the components that really served it and none that did not
    comps = gstack.parse_stack()
    def names(facts):
        return {e["name"] for e in gstack.stack_instances(comps, facts)}
    veda = names({"catalogs": ["veda"], "collections_by_catalog": {"veda": ["caldor-fire-burn-severity"]}, "tiler_hosts": ["openveda.cloud"], "maplibre": True})
    assert {"eoAPI", "stac-fastapi", "pgstac", "titiler-pgstac", "NASA VEDA", "TiTiler"} <= veda and "stac-server" not in veda
    pc = names({"catalogs": ["planetary-computer"], "collections_by_catalog": {"planetary-computer": ["naip"]}, "tiler_hosts": ["planetarycomputer.microsoft.com"], "maplibre": True})
    assert {"stac-fastapi", "pgstac", "titiler-pgstac", "NAIP", "Planetary Computer"} <= pc and "eoAPI" not in pc
    es = names({"catalogs": ["earth-search"], "collections_by_catalog": {"earth-search": ["sentinel-1-grd"]}, "tiler_hosts": ["titiler.xyz"], "maplibre": True})
    assert {"stac-server", "Copernicus Sentinel", "Earth Search", "TiTiler", "COG + HTTP range requests"} <= es and "pgstac" not in es
    three_d = names({"catalogs": ["earth-search"], "collections_by_catalog": {"earth-search": ["sentinel-2-l2a"]}, "tiler_hosts": ["titiler.xyz"], "maplibre": True, "terrain": True})
    assert {"AWS Terrarium terrain", "OpenStreetMap"} <= three_d and "OpenFreeMap" not in three_d
    assert {"Gazet", "Overture Maps", "Natural Earth"} <= names({"geocoded": "gazet"}) and "Nominatim" not in names({"geocoded": "gazet"})
    assert {"Nominatim", "OpenStreetMap"} <= names({"geocoded": "nominatim"}) and "Gazet" not in names({"geocoded": "nominatim"})
    assert names({"events": True}) == {"NASA EONET", "GDACS"} and names({"weather": True}) == {"Open-Meteo"}
    assert names({"passes": True}) == {"eo-predictor", "Celestrak"}
    assert names({"snapshot": True}) == {"Playwright"} and names({"mosaic_scenes": 3}) == {"rio-tiler"}


def t_stack_when_grammar():
    rule = "catalog=veda,planetary-computer & !terrain | snapshot"
    assert gstack.matches(rule, {"catalogs": ["veda"], "terrain": False})
    assert not gstack.matches(rule, {"catalogs": ["veda"], "terrain": True})
    assert gstack.matches(rule, {"catalogs": ["earth-search"], "terrain": True, "snapshot": True})
    assert gstack.matches("geocoded=gazet", {"geocoded": True}), "the older bare True claims every geocoder"
    assert not gstack.matches("geocoded=gazet", {"geocoded": "nominatim"})
    try:
        gstack.matches("catalgs", {})
        raise AssertionError("an unknown fact must fail loudly")
    except ValueError as e:
        assert "unknown fact" in str(e)


def t_stack_islands_and_summary():
    comps = gstack.parse_stack()
    entries = gstack.stack_instances(comps, {"catalogs": ["veda"], "collections_by_catalog": {"veda": ["x"]}, "tiler_hosts": ["openveda.cloud"], "maplibre": True, "geocoded": "gazet"})
    assert gstack.islands_exercised(entries) == ["Cloud Native Geospatial", "Data in the Browser", "Agentic"]
    line = gstack.summary(entries)
    assert "built by Development Seed" in line and "islands: Cloud Native Geospatial, Data in the Browser, Agentic" in line
    # Development Seed's own components lead their stage
    by_stage = {}
    for e in entries:
        by_stage.setdefault(e["stage"], []).append(e)
    assert by_stage["pixels"][0]["ds-role"] in gstack.DS_OWN and by_stage["catalog"][0]["ds-role"] in gstack.DS_OWN


def t_stack_group_order_and_attribution_shape():
    entries = gstack.stack_instances(gstack.parse_stack(), _STACK_FACTS)
    # literal expected pipeline order for this fixture (geocoded=True is the
    # older form and claims both geocoders and their data), not a re-derivation
    assert [e["stage"] for e in entries] == ["place"] * 4 + ["catalog"] * 3 + ["data"] + ["pixels"] * 3 + ["draw"] * 3, [e["name"] for e in entries]
    assert entries[0]["name"] == "Gazet" and [e["name"] for e in entries if e["stage"] == "pixels"][0] == "TiTiler"
    html = tools._stack_panel_html(entries)
    assert 'class="stack-group"' in html and "TiTiler" in html
    for role in {e["ds-role"] for e in entries}:
        assert role in gstack.DS_ROLES  # attribution is role-shaped by construction
    # and the rendered panel carries no person-shaped attribution
    assert not re.search(r"\b(?:by|from)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b", html)


def t_stack_panel_escapes_untrusted_values():
    entries = [{"name": "x", "kind": "data", "stage": "data", "what": "<script>alert(1)</script>",
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


def t_stack_layer_absent_when_off():
    # on by default since 0.23.0 (field tests 10 and 11 shipped 19 of 20 without it); off is the opt-out
    html, r = _render_stack_map(stack_layer=False)
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
    assert 'id="stack-toggle"' not in _render_3d(stack_layer=False)  # off on request, bytes unchanged


def t_stack_postcard_listing_static_and_honest():
    listing = tools._stack_credit_for("earth-search", "sentinel-2-l2a", "titiler.xyz")
    html = _postcard(stack_html=listing)
    assert "the stack behind this card (Cloud Native Geospatial):" in html
    assert "serving sentinel-2-l2a via titiler.xyz" in html
    assert "MapLibre" not in html  # a still image runs no map engine
    assert "https://" not in html  # the no-live-URLs guarantee survives the listing
    assert "the stack behind this card" not in _postcard()  # off by default


def t_stack_map_honest_extra_facts():
    # callers that geocoded / fetched events say so; the panel only then claims it
    token = tools._tool_geocoded.set(True)  # only a tool that ran the geocode credits it
    try:
        html, _ = _render_stack_map(stack_layer=True, stack_facts={"events": True, "weather": True})
    finally:
        tools._tool_geocoded.reset(token)
    for name in ("Gazet", "Nominatim", "NASA EONET", "GDACS", "Open-Meteo", "Overture Maps"):
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
    # the flat map counts its load handlers down, so a snapshot cannot fire
    # between map.loaded() and the raster layers being added (field test 11)
    assert "window.gsPending = 1;" in map_html and "window.gsPending--;" in map_html
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
    # same coverage: cloud decides, whatever order the server returned
    same = [{"id": "b", "bbox": [0, 0, 1, 1], "cloud_cover": 20.0}, {"id": "a", "bbox": [0, 0, 1, 1], "cloud_cover": 2.0}]
    assert tools.pick_best_scene(same, [0, 0, 1, 1])["id"] == "a"
    # an exact tie: id decides, both orders
    tie = [{"id": "y", "bbox": [0, 0, 1, 1], "cloud_cover": 5.0}, {"id": "x", "bbox": [0, 0, 1, 1], "cloud_cover": 5.0}]
    assert tools.pick_best_scene(tie, [0, 0, 1, 1])["id"] == "x"
    assert tools.pick_best_scene(tie[::-1], [0, 0, 1, 1])["id"] == "x"


def t_ndvi_change_baseline_prefers_coverage():
    # stop 33: a clear sliver must not be the baseline the delta is read against
    saved = (tools.search_imagery, tools.compute_statistics)
    box = [0, 0, 1, 1]
    best = {"id": "S2A_10TFK_20260601_0_L2A", "collection": "sentinel-2-l2a", "datetime": "2026-06-01T18:00:00Z",
            "cloud_cover": 3.0, "bbox": [-1, -1, 2, 2], "covers_aoi_pct": 100.0}
    seen = []

    def cand(i, d, cloud, bb):
        return {"id": i, "collection": "sentinel-2-l2a", "datetime": d + "T18:00:00Z", "cloud_cover": cloud, "bbox": bb}

    items = [cand("S2A_10TFK_20260501_0_L2A", "2026-05-01", 0.0, [0, 0, 0.02, 0.02]),
             cand("S2B_10TFK_20260503_0_L2A", "2026-05-03", 20.0, box)]
    tools.search_imagery = lambda *a, **kw: seen.append(kw["bbox"]) or {"items": items}
    tools.compute_statistics = lambda cat, coll, iid, **kw: {"nd": {"mean": 0.5}}
    try:
        out = brief.ndvi_change(best, box, dt.date(2026, 6, 10), 14)
        assert seen == [box], seen  # ranked over the AOI, so both covers_aoi_pct mean the same area
        assert out["baseline"]["scene"] == "S2B_10TFK_20260503_0_L2A", out
        assert out["baseline"]["cloud"] == 20.0 and round(out["baseline"]["covers_aoi_pct"]) == 100
        assert out["current"]["cloud"] == 3.0 and out["current"]["covers_aoi_pct"] == 100.0
        assert out["caveats"] == [], out
        out = brief.ndvi_change({**best, "covers_aoi_pct": 47.6}, box, dt.date(2026, 6, 10), 14)
        assert len(out["caveats"]) == 1 and "48%" in out["caveats"][0] and "100%" in out["caveats"][0], out
    finally:
        tools.search_imagery, tools.compute_statistics = saved


def t_brief_ndvi_change_is_over_the_place():
    # stop 35: both means are clipped to the place box, the label names the place and the covered share
    saved = (tools.search_imagery, tools.compute_statistics)
    box = [0, 0, 1, 1]
    best = {"id": "S2A_10TFK_20260601_0_L2A", "collection": "sentinel-2-l2a", "datetime": "2026-06-01T18:00:00Z",
            "cloud_cover": 3.0, "bbox": [-1, -1, 2, 2], "covers_aoi_pct": 47.6}
    ref = {"id": "S2B_10TFK_20260503_0_L2A", "collection": "sentinel-2-l2a", "datetime": "2026-05-03T18:00:00Z",
           "cloud_cover": 5.0, "bbox": [-1, -1, 2, 2]}
    calls = []

    def stats(cat, coll, iid, **kw):
        calls.append((iid, kw.get("bbox")))
        if kw.get("bbox") != box:
            return {"nd": {"mean": 0.2}}  # the whole-tile number the brief must no longer carry
        return {"nd": {"mean": 0.7 if iid == best["id"] else 0.5}}

    tools.search_imagery = lambda *a, **kw: {"items": [ref]}
    tools.compute_statistics = stats
    try:
        out = brief.ndvi_change(best, box, dt.date(2026, 6, 10), 14, "Chelan County")
        assert sorted(calls) == sorted([(best["id"], box), (ref["id"], box)]), calls
        assert out["current"]["mean"] == 0.7 and out["baseline"]["mean"] == 0.5 and out["delta"] == 0.2, out
        assert out["metric"] == "NDVI (vegetation) over Chelan County, 47.6% of it covered", out["metric"]
        full = brief.ndvi_change(dict(best, covers_aoi_pct=100.0), box, dt.date(2026, 6, 10), 14, "Chelan County")
        assert full["metric"] == "NDVI (vegetation) over Chelan County", full["metric"]
        assert "MGRS tile 10TFK" in brief.ndvi_change(best, box, dt.date(2026, 6, 10), 14)["metric"]
    finally:
        tools.search_imagery, tools.compute_statistics = saved


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


def t_events_empty_is_all_clear_only_if_every_source_answered():
    saved = tools._get_json
    try:
        def gdacs_down(url, **kw):
            if "gdacs" in url:
                raise TimeoutError("timed out")
            return {"events": []}
        tools._get_json = gdacs_down
        ev = tools.active_events(bbox=[-95.9, 29.5, -94.9, 30.2])
        assert ev["all_clear"] is False and ev["sources"]["gdacs"]["ok"] is False, ev
        assert ev["sources"]["eonet"]["ok"] is True and ev["sources"]["eonet"]["listed"] == 0
        assert "GDACS did not answer" in ev["all_clear_note"], ev["all_clear_note"]
        sig = tools._conditions_signals({}, {}, ev)
        assert "no events from EONET; GDACS did not answer" in sig, sig
        tools._get_json = lambda url, **kw: {"events": [], "features": []}
        ev = tools.active_events(bbox=[-95.9, 29.5, -94.9, 30.2])
        assert ev["all_clear"] is True and ev["sources"]["gdacs"]["listed"] == 0, ev
    finally:
        tools._get_json = saved


def t_brief_calm_over_failed_source_becomes_watch():
    md = "## TL;DR\nCALM. Nothing to report.\n\n## What changed\nNone."
    down = {"events": {"sources": {"eonet": {"ok": True}, "gdacs": {"ok": False}}}}
    out = brief._ensure_source_honesty(md, down)
    assert "WATCH — GDACS did not answer; CALM is unconfirmed" in out and "Nothing to report." in out, out
    assert brief._ensure_source_honesty(md, {"events": {"sources": {"gdacs": {"ok": True}}}}) == md
    assert brief._ensure_source_honesty(md, {"weather_error": "x"}).count("WATCH") == 1


def t_brief_says_when_the_baseline_moved():
    best = {"id": "S2C_10UFU_20261024"}
    change = {"baseline": {"scene": "S2C_10UFU_20261003"}, "current": {"mean": 0.544}}
    prev = {"pick_id": "S2C_10UFU_20261024", "baseline_id": "S2B_10UFU_20261018", "ndvi_mean": 0.544}
    out = brief._scene_vs_last_run(prev, best, change)
    assert "baseline changed" in out and "S2B_10UFU_20261018" in out and "S2C_10UFU_20261003" in out, out
    assert "pick changed" not in out and "same" not in out, out
    same = brief._scene_vs_last_run(dict(prev, baseline_id="S2C_10UFU_20261003"), best, change)
    assert same == "same pick, same baseline, same mean" and "changed" not in same, same
    old = brief._scene_vs_last_run({"ndvi_mean": 0.544}, best, change)
    assert "no scene ids" in old and "same" not in old, old
    gone = brief._scene_vs_last_run(prev, best, None)
    assert "this run has no NDVI comparison" in gone and "None" not in gone and "same" not in gone, gone
    nopick = brief._scene_vs_last_run(prev, None, None)
    assert nopick.startswith("this run has no scene pick, so unchanged cannot be claimed") and "None" not in nopick, nopick
    none = brief._scene_vs_last_run(dict(prev, baseline_id=None, ndvi_mean=None), best, None)
    assert none.endswith("neither run has an NDVI comparison, so unchanged cannot be claimed"), none
    noted = brief._ensure_scene_note(GOOD_MD, out)
    assert "## What changed\nSince the last run: baseline changed: S2B_10UFU_20261018 -> S2C_10UFU_20261003." in noted, noted
    assert brief._ensure_scene_note(GOOD_MD, same) == GOOD_MD and brief._ensure_scene_note(noted, out) == noted
    assert _fixture(noted, events=[{"title": "Wildfire NAVARRE COULEE, Chelan, Washington"}]) == []


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
        assert len(out) == 1 and out[0]["searched"] == [*tools.CATALOGS, "zarr"], out
        assert "does not mean the data does not exist" in out[0]["note"], out
        one = tools.search_datasets("himawari", catalog="veda")
        assert one[0]["searched"] == ["veda"], one
        hit = [h for h in tools.search_datasets("sentinel") if h["catalog"] != "zarr"]  # OlmoEarth is built from Sentinel
        assert hit == [{"catalog": c, "id": "sentinel-2-l2a", "title": "Sentinel-2", "summary": "", "gsd": None}
                       for c in tools.CATALOGS], hit
        assert not any("note" in h for h in hit)
    finally:
        tools._collections = saved


def t_search_datasets_spatial_and_resolution_filters():
    efate = [168.0, -18.0, 168.6, -17.4]
    cols = [
        {"id": "inside", "extent": {"spatial": {"bbox": [[160, -25, 175, -10]]}},
         "summaries": {"gsd": [10, 20]}},
        {"id": "outside", "extent": {"spatial": {"bbox": [[-10, 40, 10, 60]]}},
         "summaries": {"gsd": [10]}},
        {"id": "blank"},
        {"id": "nulls", "extent": {"spatial": {"bbox": [[None, None, None, None]]}}},
        {"id": "short", "extent": {"spatial": {"bbox": [[1, 2]]}}},
        {"id": "coarse", "extent": {"spatial": {"bbox": [[160, -25, 175, -10]]}},
         "item_assets": {"b": {"gsd": 30}}},
    ]
    saved = tools._collections
    tools._collections = lambda cat: cols
    try:
        out = tools.search_datasets("", catalog="veda", bbox=efate, max_gsd=10)
        assert [h["id"] for h in out] == ["inside", "blank", "nulls", "short"], out
        assert out[0]["gsd"] == 10.0 and out[0]["covers_aoi"] is True
        assert out[1]["gsd"] is None and out[1]["covers_aoi"] == "unknown"
        assert "no gsd" in out[1]["note"] and "no spatial extent" in out[1]["note"]
        assert [h["id"] for h in tools.search_datasets("", catalog="veda", bbox=efate)] == ["inside", "blank", "nulls", "short", "coarse"]
        assert "covers_aoi" not in tools.search_datasets("", catalog="veda")[0]
        # Antimeridian: bbox crossing 180 matches collections covering that longitude
        fiji = [177.0, -19.0, -179.0, -16.0]
        fiji_col = {"id": "fiji-cross", "extent": {"spatial": {"bbox": [[175, -20, 180, -15]]}}, "summaries": {"gsd": [10]}}
        tools._collections = lambda cat: [fiji_col, {"id": "other", "extent": {"spatial": {"bbox": [[0, 0, 10, 10]]}}, "summaries": {"gsd": [10]}}]
        fiji_out = tools.search_datasets("", catalog="veda", bbox=fiji, max_gsd=10)
        assert [h["id"] for h in fiji_out] == ["fiji-cross"]
        assert fiji_out[0]["covers_aoi"] is True
    finally:
        tools._collections = saved


def t_eo_predictor_mapping_and_fallback():
    fixture = json.loads("""{"constellation": "WorldView-3", "sensor_type": "optical",
      "swath_km": 13, "altitude_km": 617, "off_nadir_deg": 30, "data_access": "commercial",
      "tasking": true, "norad_ids": [40115]}""")
    sar = {"sensor_type": "SAR", "swath_km": 410, "altitude_km": 693, "off_nadir_deg": 33,
           "data_access": "open", "tasking": False, "norad_ids": [39634, 62261]}
    cfgs, skipped = tools._eo_predictor_cfgs(
        {"worldview-3": fixture, "sentinel-1": sar, "bad": {"swath_km": 5}}
    )
    assert skipped == ["bad"] and cfgs["sentinel-1"]["optical"] is False
    assert cfgs["sentinel-1"]["reach_km"] == 205 and cfgs["worldview-3"]["access"] == "commercial"
    assert abs(cfgs["worldview-3"]["reach_km"] - (6.5 + 617 * 0.57735)) < 0.1
    saved = (tools._client, tools._CATALOG_CACHE, tools._fetch_tles, tools._TLE_CACHE)
    try:
        class Dead:
            def get(self, *a, **k):
                raise RuntimeError("offline")
        with tempfile.TemporaryDirectory() as d:
            tools._client = Dead()
            tools._CATALOG_CACHE = Path(d) / "eo-predictor.json"
            tools._TLE_CACHE = Path(d) / "tle.txt"
            (Path(d) / "tle-builtin.txt").write_text("")
            tools._fetch_tles = lambda cfgs, path: []
            tools._CATALOG_CACHE.write_text("{not json")
            c, src, sk = tools._load_constellations()
            assert "built-in list" in src and not tools._CATALOG_CACHE.exists()
            c, src, sk = tools._load_constellations()
            assert c is tools._CONSTELLATIONS and "built-in list" in src
            out = tools.next_pass(-17.7, 168.3, days=1, constellations=["sentinel-2"])
            assert "built-in list" in out["constellations_source"], out
            assert out["constellations_available"] == list(tools._CONSTELLATIONS)
            assert out["eo_predictor_url"] == "https://developmentseed.org/eo-predictor"
    finally:
        tools._client, tools._CATALOG_CACHE, tools._fetch_tles, tools._TLE_CACHE = saved


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
    status_code = 200

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


def _with_geocoders(gazet_payload, nominatim_payload, fn, ctype="application/json", reverse=lambda lat, lon: {}):
    saved_get, saved_json, saved_skip = tools._client.get, tools._get_json, tools._gazet_skip_until
    calls = {"gazet": 0, "nominatim": 0}

    def fake_get(url, **kw):
        calls["gazet"] += 1
        assert url == tools.GAZET_URL and kw["params"]["mode"] == "fuzzy" and kw["params"]["ids_only"] == "true", kw
        return _FakeResp(gazet_payload, ctype)

    def fake_json(url, **kw):
        if url.endswith("/reverse"):
            calls["reverse"] = calls.get("reverse", 0) + 1
            return {"address": reverse(kw["params"]["lat"], kw["params"]["lon"])}
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
    assert "2 places match 'Loja'" in out["geocode_note"] and "Loja Canton (EC, county) at 4.1S 79.3W" in out["geocode_note"], out
    assert "Maloja" not in out["geocode_note"], "a 0.72 near miss is not a tie"


_GAZET_HARRIS = {"ids": [
    {"source": "divisions_area", "id": "tx", "name": "Harris County", "country": "US", "subtype": "county",
     "bbox": [-95.9608455, 29.497339, -94.9084924, 30.1706958], "similarity": 1.0, "is_substring_match": True},
    {"source": "divisions_area", "id": "ga", "name": "Harris County", "country": "US", "subtype": "county",
     "bbox": [-85.0, 32.6, -84.6, 32.9], "similarity": 1.0, "is_substring_match": True},
]}


def t_geocode_tie_note_tells_same_named_places_apart():
    # Field test No.8: Harris County, Texas and Harris County, Georgia both read
    # "(US, county)". A note that names them identically has said nothing.
    out, _ = _with_geocoders(_GAZET_HARRIS, [], lambda: tools.geocode("Harris County"))
    assert out["source"] == "gazet" and out["bbox"][0] == -95.9608455, out
    note = out["geocode_note"]
    assert "at 29.8N 95.4W" in note and "at 32.8N 84.8W" in note, note


def t_geocode_non_json_marks_gazet_down_and_falls_through():
    out, calls = _with_geocoders({}, _NOMINATIM_SALINAS, lambda: tools.geocode("Salinas Valley"), ctype="text/html")
    assert out["source"] == "nominatim" and calls["nominatim"] == 1, (out, calls)


def _stac_item(id_, bbox, day="2026-08-24"):
    return {"id": id_, "collection": "sentinel-2-l2a", "bbox": bbox, "links": [], "assets": {"visual": {}},
            "properties": {"datetime": f"{day}T19:00:00Z", "eo:cloud_cover": 1.0}}


def _with_stac_search(features, fn):
    saved = tools._client.post
    tools._client.post = lambda url, **kw: _FakeResp({"type": "FeatureCollection", "features": features})
    try:
        return fn()
    finally:
        tools._client.post = saved


def t_recommended_says_when_the_set_is_starved():
    # Field test No.8, Chelan County: the best single scene covered 49% and no
    # same-day set covered the county within 10 results. The recommendation
    # must say that, or the 49% scene is rendered as the answer.
    slivers = [_stac_item("a", [0, 0, 4, 10]), _stac_item("b", [4, 0, 6, 10])]
    r = _with_stac_search(slivers, lambda: tools.search_imagery("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 10, 10]))
    assert "full_coverage_set" not in r
    assert "No single scene covers this area" in r["recommended"]["reason"] and "2 results" in r["recommended"]["reason"], r["recommended"]
    halves = [_stac_item("a", [0, 0, 5, 10]), _stac_item("b", [5, 0, 10, 10])]
    r = _with_stac_search(halves, lambda: tools.search_imagery("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 10, 10]))
    assert "full_coverage_set" in r and "No single scene" not in r["recommended"]["reason"], r["recommended"]


def _jp2_only(id_, bbox, collection="sentinel-2-l2a"):
    it = _stac_item(id_, bbox)
    it["collection"] = collection
    it["assets"] = {"red": {"type": "image/jp2"}, "nir": {"type": "image/jp2"}, "granule_metadata": {"type": "application/xml"}}
    return it


def t_scene_with_no_cog_is_flagged_and_never_recommended():
    # Earth Search issue #79: 263,066 sentinel-2-l2a items are JPEG2000 only and
    # the tiler 500s on their pixels. The search has to say so, and must not
    # hand one over as the scene to start from.
    assert "cannot be read" in tools._no_cog_note(_jp2_only("x", [0, 0, 1, 1]))
    assert tools._no_cog_note(_jp2_only("x", [0, 0, 1, 1], "sentinel-2-l1c")) is None  # JP2 by design, reads fine
    cog = _stac_item("ok", [0, 0, 5, 10])
    cog["assets"] = {"red": {"type": "image/tiff; application=geotiff; profile=cloud-optimized"}, "red-jp2": {"type": "image/jp2"}}
    assert tools._no_cog_note(cog) is None
    # the unreadable scene covers the whole area, the readable one half of it
    r = _with_stac_search([_jp2_only("dead", [0, 0, 10, 10]), cog],
                          lambda: tools.search_imagery("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 10, 10]))
    by_id = {it["id"]: it for it in r["items"]}
    assert "read_note" in by_id["dead"] and "read_note" not in by_id["ok"], r["items"]
    assert r["recommended"]["id"] == "ok", r["recommended"]
    assert "full_coverage_set" not in r


def t_statistics_on_a_no_cog_scene_says_why():
    class _Boom(_FakeResp):
        status_code = 500

        def raise_for_status(self):
            raise RuntimeError("500 Internal Server Error")

    saved_get, saved_json = tools._client.get, tools._get_json
    tools._client.get = lambda url, **kw: _Boom({})
    try:
        tools._get_json = lambda url, **kw: _jp2_only("dead", [0, 0, 1, 1])
        out = tools.compute_statistics("earth-search", "sentinel-2-l2a", "dead", expression="(nir-red)/(nir+red)")
        assert "cannot be read" in out["error"], out
        # any other 500 still raises: the note is not a blanket excuse
        tools._get_json = lambda url, **kw: _stac_item("ok", [0, 0, 1, 1])
        try:
            tools.compute_statistics("earth-search", "sentinel-2-l2a", "ok", expression="(nir-red)/(nir+red)")
            raise AssertionError("a 500 on a readable scene must still raise")
        except RuntimeError:
            pass
    finally:
        tools._client.get, tools._get_json = saved_get, saved_json


def t_recommended_ties_go_to_the_newest_clean_scene():
    # Field test No.8, Chelan County over 30 days: 49.7% at 19.3% cloud (Aug 9)
    # vs 48.9% at 0.6% (Aug 24). Coverage within 5 points is a tie, cloud within
    # 5 points is a tie, and the newest of what is left wins.
    items = [
        {"id": "S2B_10UFU_20260809_0_L2A", "datetime": "2026-08-09T19:11:00Z", "cloud_cover": 19.292465, "covers_aoi_pct": 49.7},
        {"id": "S2C_10UFU_20260824_1_L2A", "datetime": "2026-08-24T19:11:07Z", "cloud_cover": 0.596335, "covers_aoi_pct": 48.9},
        {"id": "S2C_10TGT_20260824_0_L2A", "datetime": "2026-08-24T19:11:16Z", "cloud_cover": 0.002519, "covers_aoi_pct": 20.9},
    ]
    assert tools._recommend(items)["id"] == "S2C_10UFU_20260824_1_L2A"
    # Loja Canton: two 98.2% scenes, 18.7% cloud on Aug 3 and 19.1% on Aug 28. Freshness wins the tie.
    loja = [
        {"id": "old", "datetime": "2026-08-03T15:44:00Z", "cloud_cover": 18.677768, "covers_aoi_pct": 98.2},
        {"id": "new", "datetime": "2026-08-28T15:44:21Z", "cloud_cover": 19.125262, "covers_aoi_pct": 98.2},
        {"id": "cloudy", "datetime": "2026-09-02T15:44:21Z", "cloud_cover": 59.815603, "covers_aoi_pct": 98.2},
    ]
    rec = tools._recommend(loja)
    assert rec["id"] == "new" and "newest of 2 near-equal scenes" in rec["reason"] and "2026-08-03" in rec["reason"], rec


def t_recommended_names_the_step_that_dropped_the_newest_scene():
    # Field test No.16, Casco Bay: the Oct 21 scene lost at the cloud tie and the reason never said so
    casco = [
        {"id": "LC09_L2SP_012030_20261021_02_T1", "datetime": "2026-10-21", "cloud_cover": 23.01, "covers_aoi_pct": 100.0},
        {"id": "LC09_L2SP_012029_20261021_02_T1", "datetime": "2026-10-21", "cloud_cover": 2.63, "covers_aoi_pct": 66.6},
        {"id": "LC09_L2SP_011030_20261014_02_T1", "datetime": "2026-10-14", "cloud_cover": 53.29, "covers_aoi_pct": 87.0},
        {"id": "LC08_L2SP_012030_20261013_02_T1", "datetime": "2026-10-13", "cloud_cover": 100.0, "covers_aoi_pct": 100.0},
        {"id": "LC08_L2SP_011030_20261006_02_T1", "datetime": "2026-10-06", "cloud_cover": 0.33, "covers_aoi_pct": 84.2},
        {"id": "LC09_L2SP_012030_20261005_02_T1", "datetime": "2026-10-05", "cloud_cover": 15.28, "covers_aoi_pct": 100.0},
    ]
    rec = tools._recommend(casco)
    assert rec["id"] == "LC09_L2SP_012030_20261005_02_T1", rec
    assert "the newest, 2026-10-21, lost on cloud: 23.0% against the pick's 15.3%, outside the 5-point tie" in rec["reason"], rec
    assert "the clearest scene (0.3% cloud) covers 84.2%" in rec["reason"], rec
    # two scenes share Oct 21 and STAC order is not stable: either order explains the one that got furthest
    assert tools._recommend([casco[1], casco[0]] + casco[2:])["reason"] == rec["reason"]


def t_recommended_names_the_ceiling_and_coverage_steps():
    ceiling = [
        {"id": "new", "datetime": "2026-10-21", "cloud_cover": 45.0, "covers_aoi_pct": 100.0},
        {"id": "old", "datetime": "2026-10-05", "cloud_cover": 10.0, "covers_aoi_pct": 100.0},
        {"id": "clear", "datetime": "2026-10-04", "cloud_cover": 1.0, "covers_aoi_pct": 50.0},
    ]
    rec = tools._recommend(ceiling)
    assert rec["id"] == "old" and "the newest, 2026-10-21, lost on cloud: 45.0%, over the 30% ceiling" in rec["reason"], rec
    short = [
        {"id": "new", "datetime": "2026-10-21", "cloud_cover": 2.0, "covers_aoi_pct": 66.6},
        {"id": "old", "datetime": "2026-10-05", "cloud_cover": 10.0, "covers_aoi_pct": 100.0},
        {"id": "clear", "datetime": "2026-10-04", "cloud_cover": 1.0, "covers_aoi_pct": 50.0},
    ]
    rec = tools._recommend(short)
    assert rec["id"] == "old" and "the newest, 2026-10-21, lost on coverage: 66.6% against the pick's 100.0%, outside the 5-point band" in rec["reason"], rec
    # the newest scene won: no clause
    assert "newest, " not in tools._recommend([{"id": "a", "datetime": "2026-01-02", "cloud_cover": 1.0, "covers_aoi_pct": 100.0},
                                               {"id": "b", "datetime": "2026-01-01", "cloud_cover": 9.0, "covers_aoi_pct": 100.0}])["reason"]
    # the coverage band is measured from the best cover, not the pick, when the cloud tie dropped the best cover
    band = [
        {"id": "top", "datetime": "2026-10-05", "cloud_cover": 20.0, "covers_aoi_pct": 100.0},
        {"id": "pick", "datetime": "2026-10-04", "cloud_cover": 1.0, "covers_aoi_pct": 96.0},
        {"id": "new", "datetime": "2026-10-21", "cloud_cover": 1.0, "covers_aoi_pct": 93.0},
    ]
    rec = tools._recommend(band)
    assert rec["id"] == "pick" and "lost on coverage: 93.0% against the best-covering scene's 100.0%, outside the 5-point band" in rec["reason"], rec
    # every scene over the ceiling: never blame the ceiling; no cloud figure or no datetime: no crash
    cloudy = [{"id": "new", "datetime": "2026-10-21", "cloud_cover": 90.0, "covers_aoi_pct": 100.0},
              {"id": "old", "datetime": "2026-10-05", "cloud_cover": 40.0, "covers_aoi_pct": 100.0}]
    assert "ceiling" not in tools._recommend(cloudy)["reason"].split("; the newest")[-1]
    sar = [{"id": "new", "datetime": "2026-10-21", "cloud_cover": None, "covers_aoi_pct": 50.0},
           {"id": "old", "datetime": None, "cloud_cover": None, "covers_aoi_pct": 100.0}]
    assert "lost on coverage: 50.0%" in tools._recommend(sar)["reason"]


def t_stack_panel_credits_the_geocoder_that_answered():
    # Field test No.8: every geocoded map credited Gazet, including the ones
    # Nominatim answered. geocode() returns `source`; the panel claims that one.
    pat = re.compile(r'class="stack-name">([^<]+)<')
    lay = [{"type": "raster", "name": "r", "tiles": "https://titiler.xyz/x/{z}/{x}/{y}.png", "bounds": [0, 0, 10, 10]}]
    with tempfile.TemporaryDirectory() as d:
        def names(fact):
            token = tools._tool_geocoded.set(fact)
            try:
                m = tools.render_map("t", [0, 0, 10, 10], lay, out_path=str(Path(d) / "m.html"), stack_layer=True)
            finally:
                tools._tool_geocoded.reset(token)
            return {n for n in pat.findall(Path(m["map_path"]).read_text()) if n in ("Gazet", "Nominatim")}
        assert names("gazet") == {"Gazet"}
        assert names("nominatim") == {"Nominatim"}
        assert names(True) == {"Gazet", "Nominatim"}, "the older bare-True form still claims both"
        assert names(False) == set()


def t_wrap_aware_bbox_puts_fiji_back_together():
    # Field test No.8: Gazet's bbox for Fiji is the whole world, because the
    # country crosses the dateline. The real extent is 176.9E to 178.2W.
    geom = {"type": "MultiPolygon", "coordinates": [
        [[[177.0, -18.0], [180.0, -18.0], [180.0, -17.0], [177.0, -17.0], [177.0, -18.0]]],
        [[[-180.0, -17.5], [-178.2, -17.5], [-178.2, -16.5], [-180.0, -16.5], [-180.0, -17.5]]],
    ]}
    b = tools._wrap_aware_bbox(geom)
    assert b == [177.0, -18.0, -178.2, -16.5], b
    assert b[0] > b[2], "west > east is the RFC 7946 antimeridian form the coverage math reads"
    assert tools._bbox_coverage_pct(b, [176.0, -19.0, 180.0, -16.0]) is not None
    # something that genuinely wraps the globe stays as it was
    globe = {"type": "Polygon", "coordinates": [[[-180, -85], [-90, -85], [0, -85], [90, -85], [180, -85], [180, -60], [0, -60], [-180, -60], [-180, -85]]]}
    assert tools._wrap_aware_bbox(globe) is None
    assert tools._at({"bbox": [-180.0, -21.0, 180.0, -12.5]}) == "spanning the antimeridian"
    assert tools._at({"bbox": [176.9, -21.0, -178.3, -12.5]}) == "at 16.8S 179.3E", "the centre of a west > east box is across the dateline"


def t_statistics_have_one_shape_clipped_or_not():
    # Field test No.8: clipped stats came back as a GeoJSON Feature, unclipped as
    # the band dict, and the first consumer that saw both crashed on it.
    feature = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
               "properties": {"statistics": {"b1": {"min": 1.0, "max": 900.0, "mean": 120.0, "percentile_2": 20.0, "percentile_98": 400.0, "histogram": [1, 2]}}}}
    saved_post, saved_json = tools._client.post, tools._get_json
    tools._client.post = lambda url, **kw: _FakeResp(feature)
    tools._get_json = lambda url, **kw: {"bbox": [0, 0, 1, 1], "assets": {"vv": {"raster:bands": [{"scale": 1.0, "unit": "DN"}]}}}
    try:
        out = tools.compute_statistics("earth-search", "sentinel-1-grd", "S1X", assets=["vv"], bbox=[0.2, 0.2, 0.8, 0.8])
    finally:
        tools._client.post, tools._get_json = saved_post, saved_json
    assert out["vv"]["percentile_98"] == 400.0 and "histogram" not in out["vv"], out
    assert out["clipped_to"] == [0.2, 0.2, 0.8, 0.8] and "extent_note" not in out, out
    assert out["band_scaling"] == [{"scale": 1.0, "unit": "DN", "bands": ["vv"]}], "scaling travels on the clipped path too"
    assert "scaling_note" not in out, "scale 1 offset 0 is not a caveat"


def _scaled_stats(item, catalog="earth-search", lo=1000.0, **kw):
    # one compute_statistics call against a fake tiler; returns (result, params the tiler was sent)
    # PC keys a band asset_b1 and an expression result by the expression it was sent, the tilers b1
    def _feature(p):
        key = "b1" if catalog != "planetary-computer" else p.get("expression") or f"{p['assets']}_b1"
        return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
                "properties": {"statistics": {key: {"count": 10, "min": lo, "max": 3000.0, "mean": 2000.0, "std": 500.0,
                                                    "percentile_2": 1100.0, "percentile_98": 2900.0, "histogram": [1, 2]}}}}
    sent = {}
    saved_post, saved_json = tools._client.post, tools._get_json
    tools._client.post = lambda url, **k: (sent.update(k), _FakeResp(_feature(dict(k["params"]))))[1]
    tools._get_json = lambda url, **k: item
    try:
        out = tools.compute_statistics(catalog, "sentinel-2-l2a", "S2X", bbox=[0.2, 0.2, 0.8, 0.8], **kw)
    finally:
        tools._client.post, tools._get_json = saved_post, saved_json
    return out, dict(sent["params"])


_S2_BAND = [{"scale": 0.0001, "offset": -0.1}]
_S2_ITEM = {"bbox": [0, 0, 1, 1], "assets": {k: {"raster:bands": _S2_BAND} for k in ("green", "swir16", "nir", "red")}}


def t_statistics_apply_scale_and_offset_by_default():
    # Field test No.15: ratios cancel the scale but not the offset, so raw NDVI was biased after 2022
    out, _ = _scaled_stats(_S2_ITEM, assets=["green"])
    g = out["green"]
    assert abs(g["min"] - 0.0) < 1e-9 and abs(g["max"] - 0.2) < 1e-9 and abs(g["mean"] - 0.1) < 1e-9, g
    assert abs(g["percentile_2"] - 0.01) < 1e-9 and abs(g["std"] - 0.05) < 1e-9, "std scales only"
    assert g["count"] == 10, "count is not a value"
    assert out["scaling_applied"] is True and "scaling_note" not in out, out
    assert out["band_scaling"][0]["scale"] == 0.0001, "the factors used travel with the result"


def t_statistics_raw_path_keeps_the_scaling_note():
    # Field test No.12: raw digital numbers must still say so in words when scaling is off
    out, _ = _scaled_stats(_S2_ITEM, assets=["green"], apply_scaling=False)
    assert out["green"]["mean"] == 2000.0 and out["green"]["std"] == 500.0, out
    assert "scaling_applied" not in out, out
    assert len(out["band_scaling"]) == 1 and len(out["band_scaling"][0]["bands"]) == 4, out["band_scaling"]
    assert "0.0001" in out["scaling_note"] and "-0.1" in out["scaling_note"] and "4 bands" in out["scaling_note"], out


def t_expression_bands_are_corrected_before_the_index():
    _, params = _scaled_stats(_S2_ITEM, expression="(nir-red)/(nir+red)")
    e = params["expression"]
    assert e == "((b1*0.000100000000-0.100000000000)-(b2*0.000100000000-0.100000000000))/((b1*0.000100000000-0.100000000000)+(b2*0.000100000000-0.100000000000))", e
    # factor-less bands are left as written
    plain = {"bbox": [0, 0, 1, 1], "assets": {k: {"raster:bands": [{"scale": 1.0}]} for k in ("nir", "red")}}
    out, params = _scaled_stats(plain, expression="(nir-red)/(nir+red)")
    assert params["expression"] == "(b1-b2)/(b1+b2)", params
    assert "scaling_applied" not in out, out
    _, params = _scaled_stats(_S2_ITEM, expression="(nir-red)/(nir+red)", apply_scaling=False)
    assert params["expression"] == "(b1-b2)/(b1+b2)", "the raw path sends the expression as written"


def t_earth_search_offset_already_in_the_pixels_is_not_applied_twice():
    # stop 23 review: S2A_15SXS_20230818 red median is 926 DN on earth-search, 1930 on PC; the -1000 is already gone
    item = {**_S2_ITEM, "properties": {"earthsearch:boa_offset_applied": True}}
    out, _ = _scaled_stats(item, assets=["green"])
    assert abs(out["green"]["mean"] - 0.2) < 1e-9 and abs(out["green"]["min"] - 0.1) < 1e-9, out["green"]
    assert out["band_scaling"][0]["offset"] == 0 and out["band_scaling"][0]["declared_offset"] == -0.1, out["band_scaling"]
    _, params = _scaled_stats(item, expression="(nir-red)/(nir+red)")
    assert "-0.1" not in params["expression"] and "where" not in params["expression"], params
    out, _ = _scaled_stats(item, assets=["green"], apply_scaling=False)
    assert "already in the pixels" in out["scaling_note"], out


def _pc_item(baseline):
    # Planetary Computer shape: band-id assets, no raster:bands, the offset still in the pixels
    return {"bbox": [0, 0, 1, 1], "properties": {"s2:processing_baseline": baseline},
            "assets": {k: {} for k in ("B02", "B04", "B08", "B8A", "SCL", "AOT", "WVP", "visual")}}


def t_planetary_computer_sentinel2_gets_the_collection_convention():
    # stop 31: PC items carry no raster:bands, so the means were raw DN and a post-2022 NDVI was biased silently
    for baseline, off in (("05.09", -0.1), ("03.01", 0)):
        out, params = _scaled_stats(_pc_item(baseline), catalog="planetary-computer", assets=["B04"])
        g = out["band_scaling"][0]
        assert out["scaling_applied"] is True and g["scale"] == 0.0001 and g["offset"] == off, out
        assert g["bands"] == ["B02", "B04", "B08", "B8A"], "reflectance bands only, never SCL, AOT, WVP or visual"
        assert g["source"].startswith("collection convention"), g
        assert abs(out["B04"]["mean"] - (0.2 + off)) < 1e-9 and "B04_b1" not in out, out
        assert abs(out["B04"]["percentile_98"] - (0.29 + off)) < 1e-9 and abs(out["B04"]["std"] - 0.05) < 1e-9, out
    out, params = _scaled_stats(_pc_item("05.09"), catalog="planetary-computer", expression="(B08-B04)/(B08+B04)")
    assert list(out)[0] == "(B08-B04)/(B08+B04)", "the caller's expression, first, as compare_dates reads it"
    e = params["expression"]
    assert e.count("-0.100000000000") == 4 and "B08*0.000100000000" in e and "SCL" not in e, e
    _, params = _scaled_stats(_pc_item("03.01"), catalog="planetary-computer", expression="(B08-B04)/(B08+B04)")
    assert "B04*0.000100000000+0.000000000000" in params["expression"], params
    out, _ = _scaled_stats(_pc_item("05.09"), catalog="planetary-computer", assets=["B04"], apply_scaling=False)
    assert "scaling_applied" not in out and "collection convention" in out["scaling_note"], out


def t_index_outside_its_range_after_the_offset_says_so():
    # stop 36: field test 18's 2023 Rolling Fork NDVI ran -64.0 to 4.07; a dark pixel is below 0 after the -0.1 offset
    out, params = _scaled_stats(_pc_item("05.09"), catalog="planetary-computer", lo=-64.0, expression="(B08-B04)/(B08+B04)")
    assert out["(B08-B04)/(B08+B04)"]["min"] == -64.0, "not clamped"
    assert "outside -1 to 1" in out["range_note"] and "-0.1 offset" in out["range_note"] and "not clamped" in out["range_note"], out
    assert "under digital number 1000" in out["range_note"] and "median" in out["range_note"], out
    assert params.get("nodata") == "0", "digital number 0 is masked before the offset"
    out, _ = _scaled_stats(_pc_item("05.09"), catalog="planetary-computer", lo=-64.0, expression="(B03-B08)/(B03+B08)*2")
    assert "range_note" not in out, "not a normalized index, so -1 to 1 is no claim to make"
    out, _ = _scaled_stats(_pc_item("03.01"), catalog="planetary-computer", expression="(B08-B04)/(B08+B04)")
    assert "range_note" not in out, "an index inside its range earns no note"


def t_baseline_not_in_nn_nn_form_is_raw_with_the_form_named():
    # stop 36: a baseline like 5.09 used to get the -0.1 by a string compare that only worked by accident
    out, _ = _scaled_stats(_pc_item("5.09"), catalog="planetary-computer", assets=["B04"])
    assert out["scaling_applied"] is False and "'5.09' is not in the NN.NN form" in out["scaling_note"], out
    assert out["B04"]["mean"] == 2000.0 and "band_scaling" not in out, out


def t_sentinel2_without_raster_bands_or_a_convention_says_raw():
    # stop 31: any other catalog with no raster:bands must not look scaled
    out, _ = _scaled_stats(_pc_item("05.09"), assets=["B04"])
    assert out["scaling_applied"] is False and "raw digital numbers" in out["scaling_note"], out
    assert "band_scaling" not in out and out["B04"]["mean"] == 2000.0, out


def t_nominatim_fallback_says_what_it_matched():
    # Field test No.8: "the Ashburn data center corridor" simplified to "Ashburn"
    # and the top hit was Ashburn, Georgia. The note names the match and the rest.
    ga = {"boundingbox": ["31.69", "31.74", "-83.67", "-83.63"], "lat": "31.71", "lon": "-83.65",
          "display_name": "Ashburn, Turner County, Georgia, 31714, United States"}
    va = {"boundingbox": ["39.0", "39.1", "-77.5", "-77.4"], "lat": "39.04", "lon": "-77.49",
          "display_name": "Ashburn, Loudoun County, Virginia, United States"}
    saved_get, saved_json, saved_skip = tools._client.get, tools._get_json, tools._gazet_skip_until
    tools._client.get = lambda url, **kw: _FakeResp({"ids": []})
    va2 = dict(va, display_name="Ashburn, Loudoun County, Virginia, 20147, United States")
    tools._get_json = lambda url, **kw: [ga, va, va2] if kw["params"]["q"] == "Ashburn" else []
    tools._gazet_skip_until = 0.0
    try:
        out = tools.geocode("the Ashburn data center corridor")
    finally:
        tools._client.get, tools._get_json, tools._gazet_skip_until = saved_get, saved_json, saved_skip
    # neither row carries an importance, so extent decides: Virginia's box is the larger
    assert out["source"] == "nominatim" and out["bbox"][0] == -77.5, out
    assert len(out["candidates"]) == 2, ("the same place twice is not two candidates", out["candidates"])
    note = out["geocode_note"]
    assert "matched 'Ashburn' after simplifying 'the Ashburn data center corridor'" in note, note
    assert note.count("Ashburn, Georgia, United States") == 1, note


def _nom(name, kind, box, imp=0.5):
    lat, lon = (box[1] + box[3]) / 2, (box[0] + box[2]) / 2
    return {"boundingbox": [str(box[1]), str(box[3]), str(box[0]), str(box[2])], "lat": str(lat), "lon": str(lon),
            "display_name": name, "addresstype": kind, "importance": imp}


def _scored_on_its_inputs(out):
    # Stop 18: every candidate shows what it was scored on, best score first
    for c in out["candidates"]:
        assert {"kind", "extent", "importance", "source", "score"} <= c.keys(), c
    scores = [c["score"] for c in out["candidates"]]
    assert scores == sorted(scores, reverse=True), scores


# boxes and importances are the live Nominatim ones from field test No.14
_SHASTA_TOWN = _nom("Shasta Lake, Shasta County, California, United States", "town", [-122.411, 40.651, -122.337, 40.710], 0.441)
_SHASTA_RES = _nom("Shasta Lake, Shasta County, California, United States", "reservoir", [-122.55, 40.70, -122.097, 40.933], 0.342)
_KILAUEA_VILLAGE = _nom("Kilauea, Kauai County, Hawaii, 96754, United States", "locality", [-159.42, 22.19, -159.39, 22.215], 0.35)
_KILAUEA_VOLCANO = _nom("Kilauea, Hawaii County, Hawaii, 96718, United States", "volcano", [-155.28705, 19.42105, -155.28695, 19.42115], 0.477)


def t_geocode_candidates_carry_kind_and_feature_word_picks():
    # Field test No.12: "Shasta Lake" landed on the town, and Kilauea's candidates
    # said nothing about which was the volcano
    out, _ = _with_geocoders({"ids": []}, [_SHASTA_TOWN, _SHASTA_RES], lambda: tools.geocode("Shasta Lake, California"))
    _scored_on_its_inputs(out)
    assert [c["kind"] for c in out["candidates"]] == ["reservoir", "town"] and not out.get("ambiguous"), out
    assert out["bbox"] == [-122.55, 40.70, -122.097, 40.933] and "bbox_note" not in out, out
    with_word = out["candidates"][0]["score"]
    out, _ = _with_geocoders({"ids": []}, [_SHASTA_TOWN, _SHASTA_RES], lambda: tools.geocode("Shasta, California"))
    res = next(c for c in out["candidates"] if c["kind"] == "reservoir")
    assert round(with_word - res["score"], 3) == 0.2, ("the word is worth 0.2 and nothing else", with_word, res)
    # no feature word: importance 0.48 over 0.35 plus the feature kind clears the margin
    out, _ = _with_geocoders({"ids": []}, [_KILAUEA_VILLAGE, _KILAUEA_VOLCANO], lambda: tools.geocode("Kilauea, Hawaii"))
    _scored_on_its_inputs(out)
    assert out["candidates"][0]["kind"] == "volcano" and not out.get("ambiguous"), out
    assert out["candidates"][0]["extent"] == [0.0001, 0.0001] and "bbox_note" in out, "a node is a point, widened"
    assert out["extent_basis"] == "kind" and abs((out["bbox"][3] - out["bbox"][1]) * 111.32 / 2 - 5) < 1e-6, ("a volcano node is 5 km half-width", out)
    out, _ = _with_geocoders({"ids": []}, [_KILAUEA_VILLAGE, _KILAUEA_VOLCANO], lambda: tools.geocode("Kilauea volcano, Hawaii"))
    assert out["candidates"][0]["kind"] == "volcano" and not out.get("ambiguous"), out


def t_geocode_point_extent_comes_from_its_kind():
    # Stop 27: a point is padded by what it is, not one 0.3 deg square
    def half_km(out):
        return round((out["bbox"][3] - out["bbox"][1]) * 111.32 / 2, 3)
    pt = lambda kind, lat=37.0: _nom("Foo, Utah, United States", kind, [-111.0, lat, -111.0, lat], 0.4)
    for kind, km in [("bay", 2), ("town", 5), ("city", 10)]:
        out, _ = _with_geocoders({"ids": []}, [pt(kind)], lambda: tools.geocode("Foo, Utah"))
        assert out["extent_basis"] == "kind" and out["candidates"][0]["extent_basis"] == "kind" and half_km(out) == km, (kind, out)
        assert abs((out["bbox"][2] - out["bbox"][0]) / (out["bbox"][3] - out["bbox"][1]) - 1 / math.cos(math.radians(37.0))) < 1e-6, out
    out, _ = _with_geocoders({"ids": []}, [pt("quux")], lambda: tools.geocode("Foo, Utah"))
    assert out["extent_basis"] == "default" and abs(out["bbox"][2] - out["bbox"][0] - 0.3) < 1e-9, out
    out, _ = _with_geocoders({"ids": []}, [pt("bay", 90.0)], lambda: tools.geocode("Foo, Utah"))
    w, s, e, n = out["bbox"]
    assert s < n <= 90.0 and w < e and e - w <= 360.0, ("a pole stays a box, not inverted or infinite", out)
    own = _nom("Foo, Utah, United States", "bay", [-111.2, 36.9, -111.0, 37.1], 0.4)
    out, _ = _with_geocoders({"ids": []}, [own], lambda: tools.geocode("Foo, Utah"))
    assert out["extent_basis"] == "own" and out["bbox"] == [-111.2, 36.9, -111.0, 37.1] and "bbox_note" not in out, out


def t_geocode_feature_word_yields_to_a_place_named_for_it():
    # Field test No.13: "Lake Charles, Louisiana" took the lake over the city.
    # The same rule that gives Shasta the reservoir gives Lake Charles the city:
    # 0.55 over 0.18 is more than the word and the feature kind are worth
    city = _nom("Lake Charles, Calcasieu Parish, Louisiana, United States", "city", [-93.317, 30.124, -93.129, 30.272], 0.547)
    lake = _nom("Lake Charles, Calcasieu Parish, Louisiana, United States", "lake", [-93.245, 30.215, -93.218, 30.237], 0.182)
    out, _ = _with_geocoders({"ids": []}, [city, lake], lambda: tools.geocode("Lake Charles, Louisiana"))
    _scored_on_its_inputs(out)
    assert [c["kind"] for c in out["candidates"]] == ["city", "lake"] and not out.get("ambiguous"), out
    assert out["candidates"][1]["importance"] == 0.182, "the importance the margin was judged on is on the output"
    out, _ = _with_geocoders({"ids": []}, [_SHASTA_TOWN, _SHASTA_RES], lambda: tools.geocode("Shasta Lake, California"))
    assert out["candidates"][0]["kind"] == "reservoir" and not out.get("ambiguous"), "a close town does not outrank the feature"


def t_geocode_water_and_lake_are_one_kind():
    # Field test No.13: Nominatim tags the Berkeley Pit "water", so the word lake found nothing
    butte = _nom("Butte, Silver Bow County, Montana, United States", "city", [-112.6, 45.9, -112.4, 46.1], 0.5)
    pit = _nom("Berkeley Pit, Butte, Silver Bow County, Montana, United States", "water", [-112.52, 46.01, -112.50, 46.02], 0.3)
    out, _ = _with_geocoders({"ids": []}, [butte, pit], lambda: tools.geocode("Berkeley Pit lake, Butte, Montana"))
    assert out["candidates"][0]["kind"] == "water", out
    gz = {"ids": [
        {"source": "divisions_area", "id": "au", "name": "Lake Powell", "country": "AU", "subtype": "locality",
         "bbox": [142.87, -34.78, 143.02, -34.65], "similarity": 1.0},
        {"source": "natural_earth", "id": "ne", "name": "Powell", "country": None, "subtype": "reservoir",
         "bbox": [-111.56, 36.93, -110.36, 37.91], "similarity": 1.0},
    ]}
    out, _ = _with_geocoders(gz, [], lambda: tools.geocode("Lake Powell"))
    assert out["name"] == "Powell (reservoir)" and not out.get("ambiguous"), "the word picks among Gazet's ties too"
    _scored_on_its_inputs(out)
    assert [c["importance"] for c in out["candidates"]] == [None, None], "Gazet has no importance; extent and kind decide"
    # Gazet scores "Lake Powell, Utah" under its gate, so Nominatim answers with
    # the reservoir's own polygon, the lake and not a county
    res = _nom("Lake Powell, San Juan County, Utah, United States", "reservoir", [-111.6, 36.85, -110.319, 37.915], 0.435)
    out, _ = _with_geocoders({"ids": []}, [res], lambda: tools.geocode("Lake Powell, Utah"))
    assert out["candidates"][0]["kind"] == "reservoir" and out["bbox"] == [-111.6, 36.85, -110.319, 37.915], out


def t_geocode_real_extent_stays_however_small():
    # Field test No.14: the pit's one candidate carried its 0.024 by 0.013 deg
    # box and the top level was still the 0.3 deg point box. A real extent stays
    pit = _nom("Berkeley Pit, Butte, Silver Bow County, Montana, United States", "water", [-112.524, 46.008, -112.5, 46.021], 0.353)
    out, _ = _with_geocoders({"ids": []}, [pit], lambda: tools.geocode("Berkeley Pit"))
    _scored_on_its_inputs(out)
    assert out["bbox"] == [-112.524, 46.008, -112.5, 46.021] and "bbox_note" not in out, out
    assert out["candidates"][0]["extent"] == [0.024, 0.013], out["candidates"]


def t_geocode_refusal_names_state_or_country():
    # Field test No.13: the Portland refusal named no states
    ore = _nom("Portland, Multnomah County, Oregon, United States", "city", [-122.8, 45.4, -122.5, 45.7], 0.71)
    me = _nom("Portland, Cumberland County, Maine, 04101, United States", "city", [-70.35, 43.54, -70.2, 43.7], 0.604)
    out, _ = _with_geocoders({"ids": []}, [ore, me], lambda: tools.geocode("Portland"))
    err = tools._ambiguous_error("Portland", out)["error"]
    assert "city (Multnomah County, Oregon, United States) or city (Cumberland County, Maine, United States)?" in err, err
    # Field test No.14: Gazet answers a bare "Portland" and its rows carry no
    # state, so the refusal was word for word No.13's. The live four rows
    gz = {"ids": [
        {"source": "urban_centres", "id": "ghsl_4941", "name": "Portland", "country": "United States", "subtype": "urban_centre",
         "bbox": [-123.01, 45.358, -122.36, 45.718], "similarity": 1.0},
        {"source": "divisions_area", "id": "or", "name": "Portland", "country": "US", "subtype": "locality",
         "bbox": [-122.837, 45.433, -122.472, 45.653], "similarity": 1.0},
        {"source": "divisions_area", "id": "me", "name": "Portland", "country": "US", "subtype": "locality",
         "bbox": [-70.347, 43.544, -69.976, 43.728], "similarity": 1.0},
        {"source": "urban_centres", "id": "ghsl_8297", "name": "Portland", "country": "United States", "subtype": "urban_centre",
         "bbox": [-70.328, 43.621, -70.209, 43.694], "similarity": 1.0},
    ]}
    state = lambda lat, lon: {"state": "Oregon" if lon < -100 else "Maine", "country": "United States"}
    out, calls = _with_geocoders(gz, [], lambda: tools.geocode("Portland"), reverse=state)
    _scored_on_its_inputs(out)
    assert out["ambiguous"] and calls["reverse"] == 4 and calls["nominatim"] == 0, (out, calls)
    err = tools._ambiguous_error("Portland", out)["error"]
    assert ("urban_centre (Oregon, United States) or locality (Oregon, United States)"
            " or locality (Maine, United States) or urban_centre (Maine, United States)?") in err, err
    out, calls = _with_geocoders(_GAZET_CHELAN, [], lambda: tools.geocode("Chelan County"), reverse=state)
    assert "reverse" not in calls, "a name that is not refused costs no lookup"


def t_gdacs_feed_is_windowed_and_counted():
    # Field test No.8: days=7 and days=30 both returned 100 GDACS events. The
    # feed is a current-events list; the window is applied here and reported.
    now = tools.dt.datetime.now(tools.dt.timezone.utc)
    fresh = (now - tools.dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    stale = (now - tools.dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    feed = {"features": [
        {"geometry": {"type": "Point", "coordinates": [10.0, 10.0]}, "properties": {"name": "new", "eventtype": "FL", "alertlevel": "Green", "todate": fresh}},
        {"geometry": {"type": "Point", "coordinates": [11.0, 11.0]}, "properties": {"name": "old", "eventtype": "EQ", "alertlevel": "Green", "todate": stale}},
    ]}
    saved = tools._get_json
    tools._get_json = lambda url, **kw: feed if "gdacs" in url else {"events": []}
    try:
        out = tools.active_events(days=7)
    finally:
        tools._get_json = saved
    assert [e["title"] for e in out["gdacs"]] == ["new"], out
    assert out["gdacs_note"] == "GDACS current-events feed listed 2; 1 active within 7 days", out


def t_pc_registration_timeout_falls_back_to_item_tiles():
    # Field test No.8: Planetary Computer's mosaic registration hung for 30s
    # three runs in a row and the whole render died with it. Item tiles need no
    # registration, and the map says which path it took.
    import httpx as _hx
    saved_post = tools._client.post
    def boom(url, **kw):
        raise _hx.ReadTimeout("slow")
    tools._client.post = boom
    tools._pc_mosaic_cache.pop("naip:X", None)
    try:
        url = tools.tile_url_template("planetary-computer", "naip", "X", assets=["image"])
        assert "/item/tiles/" in url and "item=X" in url, url
        with tempfile.TemporaryDirectory() as d:
            m = tools.render_map("t", [-77.6, 39.0, -77.4, 39.1],
                                 [{"type": "item", "name": "NAIP X", "catalog": "planetary-computer", "collection_id": "naip", "item_id": "X",
                                   "bbox": [-77.6, 39.0, -77.4, 39.1], "assets": ["image"]}], out_path=str(Path(d) / "m.html"))
        assert "mosaic registration timed out for NAIP X" in m["note"], m
    finally:
        tools._client.post = saved_post
        tools._PC_FALLBACKS.discard("X")


def t_burned_area_tiles_draw_unburned_clear():
    # Field test on Danny's Mac, 2026-10-25: MODIS burned area stores "did not
    # burn" as 0, and an unmasked layer laid a black wash over eastern Oregon
    t = tools.tile_url_template("earth-search", "modis-64A1-061", "X", assets=["Burn_Date"])
    assert "nodata=0" in t, t
    assert "nodata=" not in tools.tile_url_template("earth-search", "sentinel-2-l2a", "X")
    assert "nodata=-9999" in tools.tile_url_template("earth-search", "sentinel-2-l2a", "X", nodata="-9999")


def t_no_artifact_rides_the_watermarked_basemap():
    # Field test No.8: CARTO's free basemap began returning "API KEY REQUIRED"
    # tiles under every map. No artifact may reference it again.
    with tempfile.TemporaryDirectory() as d:
        lay = [{"type": "raster", "name": "r", "tiles": "https://titiler.xyz/x/{z}/{x}/{y}.png", "bounds": [0, 0, 10, 10]}]
        m = tools.render_map("t", [0, 0, 10, 10], lay, out_path=str(Path(d) / "m.html"))
        html = Path(m["map_path"]).read_text()
        assert "cartocdn" not in html and "tiles.openfreemap.org/styles/positron" in html
        m3 = tools.render_map_3d("t", [0, 0, 10, 10], {"type": "raster", "name": "r", "tiles": "https://titiler.xyz/x/{z}/{x}/{y}.png", "bounds": [0, 0, 10, 10]}, out_path=str(Path(d) / "m3.html"))
        html3 = Path(m3["path"]).read_text()
        assert "cartocdn" not in html3 and "tile.openstreetmap.org" in html3


def t_hrrr_url_builders():
    import datetime as dt
    g = tools._hrrr_grib_url(dt.date(2026, 10, 25), 6, 24)
    assert g == "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261025/conus/hrrr.t06z.wrfsfcf24.grib2"
    t = tools._hrrr_tile_template(g)
    assert t.startswith(tools.EOAPI + "/external/tiles/WebMercatorQuad/{z}/{x}/{y}.png?")
    # bands=76 rides INSIDE the vrt value (encoded), the expression and colormap outside it
    assert "url=vrt%3A%2F%2F" in t and "bands%3D76" in t and "&expression=b1%2A1000000000" in t and "&colormap=" in t


def t_smoke_forecast_outside_conus_makes_no_network_call():
    real = tools._client
    class Boom:
        def __getattr__(self, k):
            raise AssertionError("network touched")
    tools._client = Boom()
    try:
        r = tools.smoke_forecast(51.5, -0.1)
    finally:
        tools._client = real
    assert r["error"].startswith("HRRR smoke covers") and r["coverage_bbox"] == tools.HRRR_BBOX


def _fallback_map(**extra):
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        layer = {"type": "item", "name": "s2", "catalog": "earth-search", "collection_id": "sentinel-2-l2a",
                 "item_id": "X", "bbox": [0, 0, 1, 1], **extra}
        r = tools.render_map("t", [0, 0, 1, 1], [layer], out_path=out)
        return Path(out).read_text(encoding="utf-8"), r


def t_render_map_429_fallback_tiles():
    html, r = _fallback_map(asset_href="https://example.com/visual.tif")
    fb = r["state"]["layers"][0]["fallback_tiles"]
    assert fb.startswith(tools.EOAPI + "/external/tiles/") and "visual.tif" in fb
    assert r["state"]["fallback"] == "raster.eoapi.dev" and fb in html and "setTiles" in html
    # an expression layer cannot fall back (eoapi.dev has no /stac), and carries no key
    _, r2 = _fallback_map(expression="(nir-red)/(nir+red)", asset_href="https://example.com/visual.tif")
    assert "fallback" not in r2["state"] and "fallback_tiles" not in r2["state"]["layers"][0]


def t_stac_map_link_only_with_an_item_layer():
    html, r = _fallback_map(asset_href="https://example.com/visual.tif")
    assert 'id="stacmap"' in html and r["stac_map_url"].startswith("https://developmentseed.org/stac-map/?href=https%3A%2F%2F")
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        r = tools.render_map("t", [0, 0, 1, 1], [{"type": "raster", "name": "r", "tiles": "https://x/{z}/{x}/{y}"}], out_path=out)
        assert 'id="stacmap"' not in Path(out).read_text(encoding="utf-8") and "stac_map_url" not in r


def t_llms_txt_present():
    root = Path(__file__).resolve().parents[1]
    assert (root / "llms.txt").read_text(encoding="utf-8").startswith("# groundstation")


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        assert self.status_code < 400


def _fake_smoke(now, available, get_json=None):
    """Run smoke_forecast with a fake clock, fake NOAA bucket (available(url) -> bool) and point reads."""
    saved = tools._client, tools._get_json, tools._utcnow
    class C:
        def head(self, url):
            return _Resp(200 if available(url) else 404)
    tools._client, tools._utcnow = C(), lambda: now
    tools._get_json = get_json or (lambda url, **kw: {"values": [1.234]})
    try:
        return tools.smoke_forecast(47.8, -120.3)
    finally:
        tools._client, tools._get_json, tools._utcnow = saved


def t_smoke_forecast_run_selection():
    import datetime as dt
    utc = dt.timezone.utc
    # day boundary: 02z now, the newest run is 18z yesterday, 8 h old
    r = _fake_smoke(dt.datetime(2026, 10, 25, 2, 30, tzinfo=utc), lambda u: "hrrr.20261024/conus/hrrr.t18z" in u)
    assert r["run"] == "2026-10-24T18:00Z" and r["forecast_hour"] == 32 and r["valid_time"] == "2026-10-26T02:00Z"
    assert r["now_valid_time"] == "2026-10-25T02:00Z" and r["now_ugm3"] == 1.2
    assert not any("earlier than asked" in c for c in r["caveat"])
    # the wanted frame is missing: step down to one that exists, and say so
    def upto29(u):
        return "hrrr.20261024/conus/hrrr.t18z" in u and int(u.split("wrfsfcf")[1][:2]) <= 29
    r = _fake_smoke(dt.datetime(2026, 10, 25, 2, 30, tzinfo=utc), upto29)
    assert r["forecast_hour"] == 29 and any("earlier than asked" in c for c in r["caveat"])
    # a run older than 24 h clamps to 48 and must still say the frame is earlier than asked
    r = _fake_smoke(dt.datetime(2026, 10, 26, 8, 0, tzinfo=utc), lambda u: "hrrr.20261025/conus/hrrr.t00z" in u)
    assert r["forecast_hour"] == 48 and any("earlier than asked" in c for c in r["caveat"])
    # nothing within 36 h: an error dict, not a raise
    r = _fake_smoke(dt.datetime(2026, 10, 26, 8, 0, tzinfo=utc), lambda u: False)
    assert "error" in r and r["source"]["model"] == "NOAA HRRR"


def t_smoke_forecast_failures_are_data():
    import datetime as dt
    now = dt.datetime(2026, 10, 25, 12, 30, tzinfo=dt.timezone.utc)
    def boom(url, **kw):
        raise RuntimeError("429 from the point route")
    r = _fake_smoke(now, lambda u: True, boom)
    assert r["error"].startswith("HRRR smoke lookup failed") and "source" in r
    r = _fake_smoke(now, lambda u: True, lambda url, **kw: {"values": [None]})
    assert r["now_ugm3"] is None and r["forecast_ugm3"] is None and any("empty" in c for c in r["caveat"])


def t_asset_href_cache_and_scheme():
    calls = []
    saved = tools._get_json
    item = {"assets": {"visual": {"href": "https://x/v.tif"}, "raw": {"href": "s3://bucket/raw.tif"}}}
    def fake(url, **kw):
        calls.append(url)
        if "dead" in url:
            raise RuntimeError("404")
        return item
    tools._get_json = fake
    tools._ITEMS.clear()
    try:
        assert tools._asset_href("earth-search", "c", "ok", "visual") == "https://x/v.tif"
        assert tools._asset_href("earth-search", "c", "ok", "raw") is None  # s3:// never goes to the public tiler
        assert len(calls) == 1  # one GET per item
        assert tools._asset_href("earth-search", "c", "dead", "visual") is None
        assert tools._asset_href("earth-search", "c", "dead", "visual") is None
        assert len(calls) == 2  # a failure is cached too
        calls.clear()
        _fallback_map(asset_href="https://x/v.tif")  # caller holds the href and bbox: no fetch at all
        assert calls == []
    finally:
        tools._get_json = saved
        tools._ITEMS.clear()


def t_statistics_429_retries_once_on_eoapi():
    saved = tools._client, tools._get_json
    item = {"bbox": [0, 0, 1, 1], "assets": {"visual": {"href": "https://x/v.tif"}}}
    seen = []
    class C:
        def __init__(self, eoapi_status):
            self.eoapi_status = eoapi_status
        def get(self, url, **kw):
            seen.append(url)
            if "raster.eoapi.dev" in url:
                return _Resp(self.eoapi_status, {"b1": {"min": 0, "max": 1}})
            return _Resp(429)
    tools._get_json = lambda url, **kw: item
    tools._ITEMS.clear()
    try:
        tools._client = C(200)
        out = tools.compute_statistics("earth-search", "sentinel-2-l2a", "I1")
        assert "visual" in out and "b1" not in out and out["note"].startswith("titiler.xyz rate-limited")
        assert len(seen) == 2 and "raster.eoapi.dev/external/statistics" in seen[1]
        seen.clear()
        tools._client = C(429)  # both limited: an error dict, no third try
        out = tools.compute_statistics("earth-search", "sentinel-2-l2a", "I1")
        assert "rate-limited" in out["error"] and len(seen) == 2
    finally:
        tools._client, tools._get_json = saved
        tools._ITEMS.clear()


def _archive_item(i, x, cloud):
    return {"type": "Feature", "stac_version": "1.1.0", "id": i, "collection": "sentinel-2-l2a",
            "geometry": {"type": "Polygon", "coordinates": [[[x, 0], [x + 1, 0], [x + 1, 1], [x, 1], [x, 0]]]},
            "bbox": [x, 0, x + 1, 1],
            "properties": {"datetime": f"2026-10-0{x % 9 + 1}T00:00:00Z", "eo:cloud_cover": cloud},
            "assets": {"visual": {"href": "https://e/v.tif", "type": "image/tiff"}},
            "links": [{"rel": "self", "href": f"https://earth-search.aws.element84.com/v1/items/{i}"}]}


def t_geoparquet_archive_searches_like_a_catalog():
    import rustac
    assert [tools._is_archive(c) for c in ("a.parquet", "s3://x.parquet", "/tmp/a/b.GEOPARQUET", "https://h/x.parquet?v=1", "https://h/x.parquet#f", "earth-search", "veda")] == [True] * 5 + [False] * 2
    saved = tools._client
    tools._client = None  # any STAC API call in the archive path raises
    try:
        with tempfile.TemporaryDirectory() as d:
            href = str(Path(d) / "a.parquet")
            rustac.write_sync(href, [_archive_item("a", 0, 5.0), _archive_item("b", 1, 50.0), _archive_item("n", 2, None), _archive_item("w", 175, 1.0)])
            out = tools.search_imagery(href, [], bbox=[0, 0, 3, 1])
            assert out["count"] == 3 and [i["id"] for i in out["items"]] == ["n", "b", "a"]
            assert set(out["items"][0]) >= {"catalog", "collection", "id", "datetime", "cloud_cover", "bbox", "assets", "self_url", "covers_aoi_pct"}
            assert out["items"][0]["self_url"].endswith("/items/n") and out["recommended"]
            assert out["archive"]["href"] == href and out["archive"]["datetime_range"] == ["2026-10-01T00:00:00Z", "2026-10-05T00:00:00Z"]
            assert out["archive"]["written"] and "snapshot" in out["archive"]["note"]
            assert tools.search_imagery(href, [], bbox=[0, 0, 0.9, 1])["count"] == 1
            capped = tools.search_imagery(href, [], bbox=[0, 0, 3, 1], max_cloud_cover=10)
            assert [i["id"] for i in capped["items"]] == ["a"]  # no cloud value is dropped, as the API does
            wrap = tools.search_imagery(href, [], bbox=[170, 0, -170, 1])  # antimeridian: not read as -170..170
            assert [i["id"] for i in wrap["items"]] == ["w"]
            assert "'earth-search'" in out["archive"]["note"]
            noc = tools._recommend([{"id": "x", "cloud_cover": None, "covers_aoi_pct": 50.0, "datetime": "d"}, {"id": "y", "cloud_cover": 5.0, "covers_aoi_pct": 50.0, "datetime": "c"}])
            assert noc["id"] == "y" and isinstance(noc["reason"], str)  # a None cloud value must not crash the reason text, and reads as the worst
            err = tools.write_archive("earth-search", ["c"], bbox=[0, 0, 1, 1], out_path=str(Path(d) / "a.txt"))
            assert "error" in err and not (Path(d) / "a.txt").exists()
    finally:
        tools._client = saved
def t_zarr_catalog_surfaces():
    keys = {"title", "url", "variables", "sel", "coverage", "chunks", "stats", "tiles", "source"}
    for sid, z in tools.ZARR_STORES.items():
        assert keys <= z.keys(), (sid, keys - z.keys())
        assert z["tiles"] or z.get("tiles_note"), f"{sid}: tiles false needs the reason"
        assert all({"description", "units"} <= v.keys() for v in z["variables"].values()), sid
    assert any(c["catalog"] == "zarr" for c in tools.list_catalogs())
    hits = tools.search_datasets("temperature")  # STAC catalogs raise offline and are caught per catalog
    assert any(h.get("catalog") == "zarr" and h.get("id") == "noaa-gfs-forecast" for h in hits)
    d = tools.describe_collection("zarr", "noaa-hrrr-forecast")
    assert d["id"] == "noaa-hrrr-forecast" and "TMP" in d["variables"] and d["tiles"] is False
    assert "error" in tools.describe_collection("zarr", "nope")
    sel = dict(valid_time="2023-01-10T12:00")
    url = tools.tile_url_template("zarr", "weatherbench2-era5", "2m_temperature", sel=sel)
    assert "/zarr/tiles/" in url and "variable=2m_temperature" in url and "time%3Dnearest%3A%3A2023-01-10T12%3A00" in url
    assert tools.tile_url_template("zarr", "noaa-gfs-forecast", "temperature_2m") == tools.ZARR_STORES["noaa-gfs-forecast"]["tiles_note"]
    for sid, var in (("nasa-mur-sst", "analysed_sst"), ("noaa-gfs-forecast", "temperature_2m")):
        assert "error" in tools.compute_statistics("zarr", sid, var, bbox=[0, 0, 1, 1])
    assert "unknown" in tools.tile_url_template("zarr", "nope", "x")
    assert "unknown variable" in tools.tile_url_template("zarr", "weatherbench2-era5", "nope")
    assert tools.ZARR_STORES["noaa-hrrr-forecast"]["coverage"].startswith("CONUS only")


def t_zarr_clip_reduce_and_select():
    import numpy as np
    import xarray as xr

    # descending latitude and 0..360 longitude, like GFS/ERA5: a bbox in -180..180 still lands
    lat, lon = np.arange(30.0, 24.0, -1.0), np.arange(255.0, 265.0, 1.0)
    da = xr.DataArray(np.arange(60.0).reshape(6, 10), dims=("latitude", "longitude"), coords=dict(latitude=lat, longitude=lon))
    r = tools._clip_reduce(da, [-104.5, 26.5, -102.5, 28.5])
    assert r["stats"]["count"] == 4 and r["stats"]["min"] == 21.0 and r["stats"]["max"] == 32.0, r
    assert "coarse grid" in r["caveats"][0]
    tiny = tools._clip_reduce(da, [-103.9, 27.1, -103.8, 27.2])
    assert tiny["stats"]["count"] == 1 and "smaller than one grid cell" in tiny["caveats"][0]
    assert "error" in tools._clip_reduce(da.where(False), [-104.5, 26.5, -102.5, 28.5])
    t = np.array(["2026-10-25T00", "2026-10-25T06"], dtype="datetime64[ns]")
    ts = xr.DataArray(np.zeros((2, 6, 10)), dims=("time", "latitude", "longitude"), coords=dict(time=t, latitude=lat, longitude=lon))
    _, used, notes = tools._zarr_select(ts, dict(valid_time="2026-10-25T05:00"))
    assert used["time"] == "2026-10-25 06:00:00" and "snapped" in notes[0]


def t_zarr_bbox_outside_coverage_is_refused():
    import numpy as np
    import xarray as xr

    lat, lon = np.arange(30.0, 24.0, -1.0), np.arange(255.0, 265.0, 1.0)
    da = xr.DataArray(np.zeros((6, 10)), dims=("latitude", "longitude"), coords=dict(latitude=lat, longitude=lon))
    assert "outside" in tools._clip_reduce(da, [5.0, 45.0, 6.0, 46.0])["error"]
    wide = xr.DataArray(np.zeros((6, 360)), dims=("latitude", "longitude"), coords=dict(latitude=lat, longitude=np.arange(360.0)))
    assert "outside" in tools._clip_reduce(wide, [-1.0, 26.0, 1.0, 28.0])["error"]  # across 0 on a 0..360 grid
    z = tools.ZARR_STORES["weatherbench2-era5"]
    saved = tools._open_zarr
    tools._open_zarr = lambda sid: xr.Dataset({"2m_temperature": wide.expand_dims(time=[np.datetime64("2023-01-10", "ns")])})
    try:
        err = tools._zarr_statistics("weatherbench2-era5", "2m_temperature", [-1.0, 26.0, 1.0, 28.0], None)
    finally:
        tools._open_zarr = saved
    assert z["title"] in err["error"] and z["coverage"] in err["error"], err


def t_zarr_select_says_how_far_it_snapped():
    import numpy as np
    import xarray as xr

    t = np.array(["2022-12-31T18", "2023-01-10T12"], dtype="datetime64[ns]")
    da = xr.DataArray(np.zeros((2, 2, 2)), dims=("time", "latitude", "longitude"), coords=dict(time=t, latitude=[1.0, 0.0], longitude=[0.0, 1.0]))
    _, used, notes = tools._zarr_select(da, dict(valid_time="2023-01-11T00:00"))
    assert used["time"].startswith("2023-01-10") and "outside the store's range" in notes[0] and "off by" in notes[0], notes


def t_http_zarr_store_range_206():
    import asyncio
    from zarr.abc.store import RangeByteRequest

    store = tools._http_zarr_store("https://example.com/test.zarr")
    class FakeBuf:
        @classmethod
        def from_bytes(cls, b): return b
    class FakeProto:
        buffer = FakeBuf

    class FakeResp:
        def __init__(self, status, content):
            self.status_code = status
            self.content = content
        def raise_for_status(self):
            if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")

    async def run():
        # 206 partial content returns bytes
        async def get_206(url, headers): return FakeResp(206, b"abc")
        store._c.get = get_206
        res = await store.get("data", FakeProto, RangeByteRequest(0, 3))
        assert res == b"abc"

        # 200 on a Range request raises RuntimeError (server ignored Range)
        async def get_200(url, headers): return FakeResp(200, b"full content")
        store._c.get = get_200
        try:
            await store.get("data", FakeProto, RangeByteRequest(0, 3))
            raise AssertionError("expected server Range refusal error")
        except RuntimeError as e:
            assert "ignored a Range request" in str(e)

        # 404 returns None
        async def get_404(url, headers): return FakeResp(404, b"")
        store._c.get = get_404
        assert (await store.get("data", FakeProto, None)) is None

    asyncio.run(run())


def t_write_archive_paging_get_merge():
    # Test next-link GET paging and merge in write_archive
    saved_client = tools._client
    calls = []
    class FakeClient:
        def post(self, url, json=None, **kw):
            calls.append(("POST", url, json))
            return _FakeResp({
                "features": [_archive_item("item-1", 1, 10.0)],
                "links": [{"rel": "next", "href": "search?page=2", "method": "GET", "merge": True, "body": {"token": "abc"}}]
            })
        def get(self, url, **kw):
            calls.append(("GET", url, kw))
            return _FakeResp({
                "features": [_archive_item("item-2", 2, 20.0)],
                "links": []
            })
    tools._client = FakeClient()
    try:
        with tempfile.TemporaryDirectory() as d:
            out_p = str(Path(d) / "paged.parquet")
            res = tools.write_archive("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 1, 1], out_path=out_p, limit=5, max_cloud_cover=20)
            assert calls[0][2]["query"] == {"eo:cloud_cover": {"lte": 20}}
            assert res["written"] == dt.datetime.now(dt.timezone.utc).date().isoformat()
            assert res["count"] == 2
            assert Path(out_p).exists()
            assert len(calls) == 2
            assert calls[0][0] == "POST"
            assert calls[1][0] == "GET" and calls[1][1].endswith("/search?page=2")
    finally:
        tools._client = saved_client


def _stub_geocode_search(ambiguous):
    cands = [dict(name="A", bbox=[0, 0, 1, 1], source="gazet", where="x"),
             dict(name="B", bbox=[5, 5, 6, 6], source="gazet", where="y")]
    g = dict(name="A", bbox=[0, 0, 1, 1], source="gazet", candidates=cands, ambiguous=ambiguous)
    posts = []

    class Resp:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return dict(features=[], links=[])

    class Client:
        def post(self, url, **k):
            posts.append(url)
            return Resp()

    saved = tools.geocode, tools._client
    tools.geocode, tools._client = (lambda q: g), Client()
    try:
        return tools.search_imagery("earth-search", ["sentinel-2-l2a"], place="Somewhere"), posts
    finally:
        tools.geocode, tools._client = saved


def t_ambiguous_geocode_refuses_place_tools():
    out, posts = _stub_geocode_search(True)
    assert "more than one place" in out["error"] and len(out["candidates"]) == 2, out
    assert posts == [], "an ambiguous name must not reach the network"


def t_unambiguous_geocode_runs_the_search():
    out, posts = _stub_geocode_search(False)
    assert "error" not in out and len(posts) == 1, out


def _stub_frames(n):
    return [{"type": "raster", "name": f"2026-0{i + 1}", "tiles": f"https://t.example/{i}/{{z}}/{{x}}/{{y}}.png"}
            for i in range(n)]


def t_timelapse_page_controls_and_cap():
    with tempfile.TemporaryDirectory() as d:
        r = tools.render_timelapse("tl", [0, 0, 1, 1], _stub_frames(3), out_path=str(Path(d) / "t.html"))
        html = Path(r["map_path"]).read_text(encoding="utf-8")
        assert r["frames"] == 3 and r["state"]["layers"], r
        for needle in ('id="tl-prev"', 'id="tl-play"', 'id="tl-next"', 'id="tl-name"', "ArrowLeft", "window.gsPending",
                       "2026-01", "2026-02", "2026-03", ".clean #panel, .clean #credit, .clean #stack-toggle, .clean #stack, .clean #tl"):
            assert needle in html, needle
        # frames are shown one at a time: show() hides every layer but the current, and starts at 0
        assert '"visibility", i === cur ? "visible" : "none"' in html and "show(0)" in html
        assert "__" not in html.replace("__proto__", ""), "unfilled template marker"
        over = tools.render_timelapse("tl", [0, 0, 1, 1], _stub_frames(9), out_path=str(Path(d) / "u.html"))
        assert "error" in over and "cap of 8" in over["error"] and not (Path(d) / "u.html").exists(), over


XSS = "<script>alert(1)</script>"


def _hostile_pages(d):
    raster = {"type": "raster", "name": "</script>" + XSS, "tiles": "https://t.example/{z}/{x}/{y}.png"}
    main = {"type": "raster", "name": "m", "tiles": "https://t.example/{z}/{x}/{y}.png"}
    pages = [
        tools.render_map(XSS, [0, 0, 1, 1], [raster], subtitle=XSS, out_path=str(Path(d) / "a.html"))["map_path"],
        tools.render_map_3d(XSS, [0, 0, 10, 10], main, subtitle=XSS, out_path=str(Path(d) / "b.html"))["path"],
        tools.render_timelapse(XSS, [0, 0, 1, 1], [raster, raster], out_path=str(Path(d) / "c.html"))["map_path"],
    ]
    card = tools._postcard_html(b"x", XSS, XSS, XSS, XSS, XSS, XSS)
    return [Path(p).read_text(encoding="utf-8") for p in pages] + [card]


def t_renderers_escape_titles_and_names():
    with tempfile.TemporaryDirectory() as d:
        pages = _hostile_pages(d)
    for html in pages:
        assert XSS not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    for html in (pages[0], pages[2]):
        # a layer name cannot end the JSON block: the raw closer appears only
        # for the template's own script tags, the name's one is \\u003c
        assert "\\u003c/script>" in html


def t_fill_never_reexpands_values():
    assert tools._fill("__A__ __B__ __C__", {"A": "__B__", "B": "x"}) == "__B__ x __C__"


def t_artifact_path_confined():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        old_out, old_cwd = os.environ["GROUNDSTATION_OUT"], os.getcwd()
        os.environ["GROUNDSTATION_OUT"] = out
        os.chdir(d)
        try:
            main = {"type": "raster", "name": "m", "tiles": "https://t.example/{z}/{x}/{y}.png"}
            with tempfile.TemporaryDirectory() as elsewhere:
                link = Path(d) / "link.html"
                link.symlink_to(Path(elsewhere) / "target.html")
                bad = [str(Path(elsewhere) / "x.html"), "../../x.html", str(Path(d) / "x.txt"), str(link)]
                for p in bad:
                    r = tools.render_map("t", [0, 0, 1, 1], [main], out_path=p)
                    assert "error" in r and "map_path" not in r, (p, r)
                assert not list(Path(elsewhere).iterdir())
            (Path(d) / "docs").mkdir()
            # a plain cwd is not a root: only GROUNDSTATION_OUT counts until cwd is a checkout
            r = tools.render_map("t", [0, 0, 1, 1], [main], out_path=str(Path(d) / "docs" / "no.html"))
            assert "error" in r and not (Path(d) / "docs" / "no.html").exists(), r
            (Path(d) / ".git").mkdir()
            for p in (Path(d) / "docs" / "ok.html", Path(out) / "ok.html"):
                ok = tools.render_map("t", [0, 0, 1, 1], [main], out_path=str(p))
                assert Path(ok["map_path"]).exists(), ok
            assert not [f for f in (Path(d) / "docs").iterdir() if f.name.endswith(".tmp")]
        finally:
            os.chdir(old_cwd)
            os.environ["GROUNDSTATION_OUT"] = old_out


def t_zarr_tile_time_outside_range_refused():
    # field test 12 card 4: a 2026 ask was served 2023 data without a word
    out = tools.tile_url_template("zarr", "weatherbench2-era5", "2m_temperature", sel={"time": "2026-10-25T12:00"})
    assert "outside" in out and "2023-01-10" in out and "{z}" not in out, out
    ok = tools.tile_url_template("zarr", "weatherbench2-era5", "2m_temperature", sel={"time": "2023-01-10T12:00"})
    assert "{z}" in ok, ok


def t_zarr_snap_past_one_step_is_refused():
    import numpy as np
    import xarray as xr

    t = np.array(["2026-10-25T00", "2026-10-25T06", "2026-10-25T12"], dtype="datetime64[ns]")
    da = xr.DataArray(np.zeros((3, 2, 2)), dims=("time", "latitude", "longitude"), coords=dict(time=t, latitude=[1.0, 0.0], longitude=[0.0, 1.0]))
    try:
        tools._zarr_select(da, dict(valid_time="2026-10-20T21:00"))
        raise AssertionError("a 5-day snap was not refused")
    except ValueError as e:
        assert "2026-10-25 00:00:00" in str(e) and "2026-10-25 12:00:00" in str(e), e
    _, used, notes = tools._zarr_select(da, dict(valid_time="2026-10-25T09:00"))  # 3 h off on a 6 h grid
    assert "snapped" in notes[0], notes
    _, _, notes = tools._zarr_select(da, dict(valid_time="2026-10-25T05:00"))
    assert "snapped" in notes[0], notes


def t_zarr_stats_refusals_name_the_range():
    import numpy as np
    import xarray as xr

    saved = tools._open_zarr
    def boom(sid):
        raise AssertionError("opened the store for a time it does not hold")
    tools._open_zarr = boom
    try:
        mur = tools.compute_statistics("zarr", "nasa-mur-sst", "analysed_sst", bbox=[-122.05, 36.65, -121.75, 36.95], sel={"time": "2026-10-20"})
        assert "2020-01-20" in mur["error"], mur
        era = tools.compute_statistics("zarr", "weatherbench2-era5", "total_precipitation_6hr", bbox=[-122.4, 38.6, -121.4, 40.0], sel={"time": "2026-10-15T12:00"})
        assert "1959-01-01" in era["error"] and "2023-01-10" in era["error"] and "stats" not in era, era
    finally:
        tools._open_zarr = saved
    # a rolling store: the snap refusal carries the store's own range note
    t = np.array(["2026-10-26T00", "2026-10-26T01"], dtype="datetime64[ns]")
    da = xr.DataArray(np.zeros((2, 2, 2)), dims=("time", "y", "x"), coords=dict(time=t, y=[0.0, 1.0], x=[0.0, 1.0]))
    tools._open_zarr = lambda sid: xr.Dataset({"TMP": da})
    try:
        hr = tools._zarr_statistics("noaa-hrrr-forecast", "TMP", [0.0, 0.0, 1.0, 1.0], dict(valid_time="2026-10-20T21:00"))
    finally:
        tools._open_zarr = saved
    assert "2026-10-26 01:00:00" in hr["error"] and "past dates are not in this store" in hr["error"], hr


def t_zarr_uncurated_variable_in_store_is_accepted():
    import numpy as np
    import xarray as xr

    lat, lon = np.arange(30.0, 24.0, -1.0), np.arange(255.0, 265.0, 1.0)
    da = xr.DataArray(np.ones((6, 10)), dims=("latitude", "longitude"), coords=dict(latitude=lat, longitude=lon), attrs=dict(long_name="Sea ice", units="1"))
    ds = xr.Dataset({"sea_ice": da.expand_dims(time=[np.datetime64("2023-01-10", "ns")])})
    saved = tools._open_zarr
    tools._open_zarr = lambda sid: ds
    try:
        ok = tools._zarr_statistics("weatherbench2-era5", "sea_ice", [-104.5, 26.5, -102.5, 28.5], dict(time="2023-01-10T00:00"))
        assert ok["units"] == "1" and "Sea ice" in ok["dataset"], ok
        bad = tools._zarr_statistics("weatherbench2-era5", "nope", [-104.5, 26.5, -102.5, 28.5], None)
        assert "1 variables" in bad["error"] and "2m_temperature" in bad["error"], bad
    finally:
        tools._open_zarr = saved
    assert "total_precipitation_6hr" in tools.describe_collection("zarr", "weatherbench2-era5")["variables"]


def t_classifier_labels_in_map_out():
    # labels over one year's embeddings, a class map for another; the 2025 east
    # half flips to the other class's signature, so changes name that move,
    # and the caveats travel with the agreement number
    import numpy as np
    import rasterio
    import zarr
    from rasterio.warp import transform

    xs, ys = transform("EPSG:4326", "EPSG:32646", [95.95], [4.31])
    x0, y0 = xs[0] - 500, ys[0] + 500
    g = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    g.attrs.update({"proj:code": "EPSG:32646", "spatial:transform": [10, 0, x0, 0, -10, y0],
                    "geoemb:quantization": {"power": 2.0, "nodata": -128}})
    g.create_array("time", data=np.array([2020, 2025]))
    rng = np.random.default_rng(1)
    q = rng.integers(-10, 10, (2, 64, 100, 100)).astype("int8")
    q[:, :32, :, :50] += 90   # forest signature on the west half
    q[:, 32:, :, 50:] += 90   # crops signature on the east half
    q[1, :32, 30:70, 50:] += 90  # by 2025 part of the east looks like forest
    q[1, 32:, 30:70, 50:] -= 90
    for r_, c_ in ((5, 5), (90, 20), (50, 10)):  # lone 2025 flips: noise, not land change
        q[1, :32, r_, c_] -= 90
        q[1, 32:, r_, c_] += 90
    g.create_array("embeddings", data=q)

    def box(lo_x, hi_x):
        xs_ = [x0 + lo_x, x0 + hi_x, x0 + hi_x, x0 + lo_x]
        ys_ = [y0 - 50, y0 - 50, y0 - 950, y0 - 950]
        lon, lat = transform("EPSG:32646", "EPSG:4326", xs_, ys_)
        ring = [list(p) for p in zip(lon, lat)]
        return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}
    labels = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"class": "forest"}, "geometry": box(50, 450)},
        {"type": "Feature", "properties": {"class": "crops"}, "geometry": box(550, 950)},
        {"type": "Feature", "properties": {"class": "reef"}, "geometry": {"type": "Point", "coordinates": [95.95, 4.31]}}]}
    saved = tools._open_zone
    tools._open_zone = lambda z, zone: g
    try:
        lon, lat = transform("EPSG:32646", "EPSG:4326", [x0 + 5, x0 + 995], [y0 - 995, y0 - 5])
        bbox = [lon[0], lat[0], lon[1], lat[1]]
        r = tools.classify_embeddings(labels, 2020, [2020, 2025], bbox=bbox)
        assert r["held_out_agreement_pct"] > 95, r
        assert sorted(r["classes"].values()) == ["crops", "forest"], r["classes"]
        assert any("dropped reef" in c for c in r["caveats"]), r["caveats"]
        assert any(c.startswith("labels inherited") for c in r["caveats"])
        top = r["changes"]["top"][0]
        assert (top["from"], top["to"]) == ("crops", "forest") and top["ha"] > 10, r["changes"]
        assert all(t["from"] != "forest" for t in r["changes"]["top"]), r["changes"]
        assert r["changes"]["speckle_ha"] >= 0.03 and r["changes"]["min_patch_ha"] == 0.25, r["changes"]
        with rasterio.open(r["years"]["2025"]["path"]) as ds:
            assert ds.nodata == 0 and ds.crs.to_epsg() == 32646
        assert r["layer"]["type"] == "file" and r["layer"]["classified"]
        assert "error" in tools.classify_embeddings(labels, 2019, bbox=bbox)
        assert "error" in tools.classify_embeddings(labels, 2020, bbox=[95.0, 4.0, 95.5, 4.5])
        assert "error" in tools.classify_embeddings(labels, 2020, bbox=bbox, store="noaa-gfs-forecast")
        for bad in ("http://169.254.169.254/x.tif", "/vsicurl/http://x/y.tif"):
            try:
                tools.classify_embeddings(bad, 2020, bbox=bbox)
            except ValueError:
                continue
            raise AssertionError(f"labels opened {bad}")
    finally:
        tools._open_zone = saved


def t_classifier_labels_stack_sources():
    # a land cover polygon first, a water mask second: the mask fills only
    # what the polygon left blank, 2 water values merge, land named null stays out
    import numpy as np
    import rasterio
    from rasterio.warp import transform_geom

    win = {"rows": (0, 10), "cols": (0, 10), "crs": "EPSG:32646", "transform": (10, 0, 500000, 0, -10, 100100)}
    ring = [[500000, 100100], [500050, 100100], [500050, 100000], [500000, 100000], [500000, 100100]]
    poly = {"type": "Feature", "properties": {"class": "forest"},
            "geometry": transform_geom("EPSG:32646", "EPSG:4326", {"type": "Polygon", "coordinates": [ring]})}
    mask = np.ones((10, 10), "uint8")
    mask[:, 6:8], mask[:, 8:] = 3, 4  # freshwater, buffer ocean
    mask[:, :2] = 4  # under the polygon: the polygon keeps it
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "mask.tif")
        with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1, dtype="uint8",
                           crs=win["crs"], transform=rasterio.Affine(*win["transform"]), nodata=255) as ds:
            ds.write(mask, 1)
        grid, names = tools._labels_grid([{"type": "FeatureCollection", "features": [poly]}, path], "class", win,
                                         {"1": None, "3": "water", "4": "water"})
        box = tools._labels_bbox([path, path])
    assert names == ["forest", "water"], names
    assert (grid[:, :5] == 1).all() and (grid[:, 5] == 0).all() and (grid[:, 6:] == 2).all(), grid
    assert box[0] < box[2] and box[1] < box[3], box


def t_embedding_store_decodes_and_picks_the_zone():
    # OlmoEarth: one group per UTM zone, int8 with -128 nodata and a signed
    # power rule in its attrs; the number is a feature, so it says so
    import numpy as np
    import zarr

    from rasterio.warp import transform

    xs, ys = transform("EPSG:4326", "EPSG:32646", [95.95], [4.31])
    g = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    g.attrs.update({"proj:code": "EPSG:32646", "spatial:transform": [10, 0, xs[0] - 500, 0, -10, ys[0] + 500],
                    "geoemb:quantization": {"power": 2.0, "nodata": -128, "formula": "(abs(q) / 127.5) ** 2.0 * sign(q)"}})
    g.create_array("time", data=np.array([2020, 2025]))
    q = np.full((2, 2, 100, 100), -128, "int8")
    q[1, 1, 20:80, 20:80] = -64  # the 2025 / dim 1 box around the point; 2020 is all nodata
    g.create_array("embeddings", data=q)
    opened = []
    saved = tools._open_zone
    tools._open_zone = lambda z, zone: opened.append(zone) or g
    try:
        z = tools.ZARR_STORES["olmoearth-v1-3-embeddings"]
        bbox = [95.949, 4.309, 95.951, 4.311]
        r = tools.compute_statistics("zarr", "olmoearth-v1-3-embeddings", "embeddings", bbox=bbox, sel={"time": "2025", "band": "1"})
        assert opened == ["utm46"], opened
        assert abs(r["embeddings"]["mean"] + (64 / 127.5) ** 2) < 1e-6 and r["coverage_pct"] == 100, r
        assert "not a measurement" in r["caveats"][0]
        assert "backfilling" in tools._embedding_statistics("o", z, bbox, {"time": "2020"})["error"]
        assert "2020 to 2025" in tools._embedding_statistics("o", z, bbox, {"time": "2019"})["error"]
        assert "10 km" in tools._embedding_statistics("o", z, [95.5, 4.0, 96.0, 4.5], {})["error"]
    finally:
        tools._open_zone = saved
    assert "proj convention" in tools.tile_url_template("zarr", "olmoearth-v1-3-embeddings", "embeddings")


def t_date_only_window_becomes_rfc3339():
    assert tools._rfc3339_window("2024-10-01/2024-10-20") == "2024-10-01T00:00:00Z/2024-10-20T23:59:59Z"
    full = "2026-06-01T00:00:00Z/2026-07-09T00:00:00Z"
    assert tools._rfc3339_window(full) == full


def t_compare_dates_default_path_per_box_and_out_path():
    item = lambda i, d: {"id": i, "datetime": f"{d}T10:00:00Z", "collection": "sentinel-2-l2a", "bbox": [0, 0, 1, 1], "cloud_cover": 1}
    saved = tools.search_imagery, tools.compute_statistics, tools._coverage
    tools.search_imagery = lambda *a, **k: {"items": [item("S2B_31TBF_20240101_0_L2A", "2024-01-01")]}
    tools.compute_statistics = lambda *a, **k: {"x": {"mean": 0.5}}
    tools._coverage = lambda *a, **k: 1.0
    try:
        w = dict(window_before="2024-01-01/2024-01-02", window_after="2024-02-01/2024-02-02")
        p1 = tools.compare_dates(bbox=[0, 0, 1, 1], stack_layer=False, **w)["map_path"]
        p2 = tools.compare_dates(bbox=[5, 5, 6, 6], stack_layer=False, **w)["map_path"]
        assert p1 != p2, p1
        want = str(Path(tempfile.gettempdir()) / "mine.html")
        assert tools.compare_dates(bbox=[0, 0, 1, 1], stack_layer=False, out_path=want, **w)["map_path"] == want
    finally:
        tools.search_imagery, tools.compute_statistics, tools._coverage = saved


def _notes_map(**kw):
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "m.html")
        res = tools.render_map("t", [0, 0, 1, 1], [{"type": "raster", "name": "A", "tiles": "https://x/{z}/{x}/{y}.png"}], out_path=out, **kw)
        return res, Path(out).read_text(encoding="utf-8")


def t_notes_render_escaped_in_box_and_stack():
    res, html = _notes_map(notes=["claim 2024-03-01", "<script>x</script>"])
    assert res["notes"] == ["claim 2024-03-01", "<script>x</script>"]
    assert 'id="notes"' in html and "<b>Notes</b>" in html and "claim 2024-03-01" in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script>x</script>" not in html
    assert html.count("claim 2024-03-01") == 2  # box and Stack panel
    assert ".swipe #notes" in html and 'classList.add("swipe")' in html  # a swipe lifts notes off the left layer's name


def t_notes_absent_changes_nothing():
    res, html = _notes_map()
    assert 'id="notes"' not in html and "notes" not in res and "stack_facts_ignored" not in res
    res2, html2 = _notes_map(notes=[])
    assert html2 == html and "notes" not in res2


def t_stack_facts_unknown_key_named_known_key_kept():
    res, html = _notes_map(stack_facts={"claim_event": "x", "track": "y", "events": True})
    assert res["stack_facts_ignored"] == ["claim_event", "track"] and "notes" in res["note"]
    assert "claim_event" not in html


def t_compare_dates_passes_notes_through():
    item = lambda i, d: {"id": i, "datetime": f"{d}T10:00:00Z", "collection": "sentinel-2-l2a", "bbox": [0, 0, 1, 1], "cloud_cover": 1}
    saved = tools.search_imagery, tools.compute_statistics, tools._coverage
    tools.search_imagery = lambda *a, **k: {"items": [item("S2B_31TBF_20240101_0_L2A", "2024-01-01")]}
    tools.compute_statistics = lambda *a, **k: {"x": {"mean": 0.5}}
    tools._coverage = lambda *a, **k: 1.0
    try:
        out = tools.compare_dates(bbox=[0, 0, 1, 1], window_before="2024-01-01/2024-01-02", window_after="2024-02-01/2024-02-02",
                                  notes=["storm <b>"], stack_facts={"bogus": 1})
        assert out["notes"] == ["storm <b>"] and out["stack_facts_ignored"] == ["bogus"]
        assert "storm &lt;b&gt;" in Path(out["map_path"]).read_text(encoding="utf-8")
    finally:
        tools.search_imagery, tools.compute_statistics, tools._coverage = saved


def t_compare_dates_same_call_same_scene():
    # Stop 20: two tiles both cover the box at 100, so the tile came from set order and flipped between runs
    def item(tile, d, cloud, cov=100.0):
        return {"id": f"S2A_{tile}_{d}_0_L2A", "datetime": f"2024-{d[4:6]}-{d[6:]}T10:00:00Z", "collection": "sentinel-2-l2a",
                "bbox": [0, 0, 1, 1], "cloud_cover": cloud, "covers_aoi_pct": cov}

    def run(items):
        saved = tools.search_imagery, tools.compute_statistics, tools.render_map
        tools.search_imagery = lambda *a, **k: {"items": list(items)}
        tools.compute_statistics = lambda *a, **k: {"x": {"mean": 0.5}}
        tools.render_map = lambda **k: {"map_path": "x.html"}
        try:
            return tools.compare_dates(bbox=[0, 0, 1, 1], window_before="a/b", window_after="c/d")
        finally:
            tools.search_imagery, tools.compute_statistics, tools.render_map = saved

    tied = [item("31SBD", "20241115", 20.0), item("30SYJ", "20241115", 20.0)]
    one, two = run(tied), run(tied[::-1])
    assert one["tile"] == two["tile"] == "30SYJ" and one["after"]["id"] == two["after"]["id"], (one["tile"], two["tile"])
    assert one["after"]["runner_up"]["id"] == "S2A_31SBD_20241115_0_L2A" and "scene id decided" in one["after"]["pick_note"]
    better = [item("31SBD", "20241115", 20.0, cov=100.0), item("30SYJ", "20241115", 5.0, cov=80.0)]
    for order in (better, better[::-1]):
        out = run(order)
        assert out["tile"] == "31SBD" and "pick_note" not in out["after"], out["after"]
        assert out["after"]["runner_up"]["covers_aoi_pct"] == 80.0


def t_brief_page_and_conditions_brief_share_one_radius():
    import importlib.util
    spec = importlib.util.spec_from_file_location("brief", Path(__file__).resolve().parents[1] / "briefing" / "brief.py")
    src = Path(spec.origin).read_text()
    assert "pad = tools.BRIEF_PAD_DEG" in src and "pad=BRIEF_PAD_DEG" in Path(tools.__file__).read_text()
    saved = tools._get_json
    tools._get_json = lambda *a, **k: {"events": []}
    try:
        assert tools.active_events(bbox=[0, 0, 1, 1], pad=tools.BRIEF_PAD_DEG)["pad_deg"] == tools.BRIEF_PAD_DEG == 1.0
    finally:
        tools._get_json = saved


def t_same_collection_layers_read_as_collection_and_date_with_the_id_in_the_detail():
    items = [{"catalog": "planetary-computer", "collection": "esa-worldcover", "id": f"ESA_WorldCover_10m_2021_v200_{t}_Map", "bbox": [0, 0, 1, 1]}
             for t in ("N45W123", "N45W120", "N42W123", "N42W120", "N39W123", "N39W120")]
    out = tools._expand_coverage_set({"type": "coverage_set", "set": {"items": items}, "name": "WorldCover 2021"})
    assert {l["name"] for l in out} == {"WorldCover 2021"}, [l["name"] for l in out]
    assert len({l["detail"] for l in out}) == 6 and all("_Map" in l["detail"] for l in out)
    # the caller's state keeps them apart too, not only the page
    assert len({tools._layer_state(l)["detail"] for l in out}) == 6


def t_layer_panel_folds_at_phone_width_like_the_legend():
    t = tools._MAP_TEMPLATE
    assert '<details class="fold" open>' in t and 'innerWidth <= 500' in t and "#panel details" in t
    assert "max-width: calc(100vw - 150px)" in t  # clear of the STACK button
    assert "#panel details:has(#toggles:empty) { display: none; }" in t  # a timelapse has no rows
    for tpl in (t, tools._MAP3D_TEMPLATE):  # the credit folds into MapLibre's attribution on a phone
        assert "@media (max-width: 500px) { #credit { display: none; } }" in tpl
        assert 'attributionControl: { customAttribution: document.getElementById("credit").textContent }' in tpl


def t_next_pass_names_the_night_passes_it_dropped():
    tle = Path(tempfile.mkdtemp()) / "tle.txt"
    tle.write_text("ISS\n1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927\n"
                   "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537\n")
    cfg = {"x": {"norad_ids": [25544], "swath_km": 40000, "reach_km": 20000, "optical": True}}
    saved = tools._load_constellations, tools._fetch_tles, tools._TLE_CACHE
    tools._load_constellations = lambda: (cfg, "test", [])
    tools._fetch_tles = lambda c, p: []
    tools._TLE_CACHE = tle.with_name("tle.txt")
    tle.rename(tle.with_name("tle-eo-predictor.txt"))
    try:
        r = tools.next_pass(0.0, 0.0, days=3)
    finally:
        tools._load_constellations, tools._fetch_tles, tools._TLE_CACHE = saved
    night = r["optical_passes_at_night_list"]
    assert night and len(night) == r["optical_passes_at_night"], r
    assert all({"satellite", "time_utc", "sun_elevation_deg"} <= set(n) and n["sun_elevation_deg"] <= 0 for n in night), night


def _poly(*pts):
    return {"type": "Polygon", "coordinates": [[list(p) for p in pts] + [list(pts[0])]]}


# bbox [-71, 43, -69, 44.5] reads 100 on the Casco Bay box, but the swath edge cuts across its south
_SLANT = _poly((-71, 44.5), (-69, 44.5), (-69, 43.5), (-71, 43.9))
_CASCO = [-70.40, 43.55, -69.90, 43.85]


def _geo_item(id_, bbox, geometry=None, day="2026-08-24"):
    f = _stac_item(id_, bbox, day)
    if geometry:
        f["geometry"] = geometry
    return f


def t_footprint_coverage_beats_the_bbox_and_names_the_side():
    r = _with_stac_search([_geo_item("slant", [-71, 43, -69, 44.5], _SLANT)],
                          lambda: tools.search_imagery("planetary-computer", ["landsat-c2-l2"], bbox=_CASCO))
    it = r["items"][0]
    assert tools._bbox_coverage_pct(_CASCO, it["bbox"]) == 100.0
    assert 30 < it["covers_aoi_pct"] < 80 and it["coverage_basis"] == "footprint", it
    pct, where = tools._uncovered_side(_CASCO, [_SLANT])
    assert 20 < pct < 70 and "south" in where, (pct, where)


def t_footprint_that_misses_the_box_reads_zero_and_is_not_recommended():
    away = _poly((-80, 40), (-78, 40), (-78, 41), (-80, 41))
    items = [_geo_item("miss", [-71, 43, -69, 44.5], away, day="2026-08-25"),
             _geo_item("hit", [-71, 43, -69, 44.5], _poly((-71, 43), (-69, 43), (-69, 44.5), (-71, 44.5)), day="2026-08-24")]
    r = _with_stac_search(items, lambda: tools.search_imagery("planetary-computer", ["landsat-c2-l2"], bbox=_CASCO))
    by_id = {it["id"]: it for it in r["items"]}
    assert by_id["miss"]["covers_aoi_pct"] == 0.0 and by_id["hit"]["covers_aoi_pct"] == 100.0, by_id
    assert r["recommended"]["id"] == "hit", r["recommended"]


def t_full_coverage_set_is_built_on_footprints():
    box = [-70.40, 43.55, -69.90, 43.85]
    west = _fcs_item("west", "2026-07-19", [-71, 43, -69, 44.5])
    east = _fcs_item("east", "2026-07-19", [-71, 43, -69, 44.5])
    west["footprint"] = _poly((-71, 43), (-70.15, 43), (-70.15, 44.5), (-71, 44.5))
    east["footprint"] = _poly((-70.2, 43), (-69, 43), (-69, 44.5), (-70.2, 44.5))
    # the bboxes alone would call either one a full cover and need no set
    assert tools._union_coverage_pct(box, [west["bbox"]]) == 100.0
    assert 40 < tools._union_coverage_pct(box, [west["footprint"]]) < 70
    got = tools.find_full_coverage_set([west, east], box)
    assert got and {i["id"] for i in got["items"]} == {"west", "east"} and got["union_covers_aoi_pct"] >= 99.0, got
    # a hole in a footprint is not covered, a multipolygon is the union of its parts
    holed = {"type": "Polygon", "coordinates": [[[-71, 43], [-69, 43], [-69, 44.5], [-71, 44.5], [-71, 43]],
                                                [[-70.3, 43.6], [-70.0, 43.6], [-70.0, 43.8], [-70.3, 43.8], [-70.3, 43.6]]]}
    assert 0 < tools._union_coverage_pct(box, [holed]) < 90
    multi = {"type": "MultiPolygon", "coordinates": [west["footprint"]["coordinates"], east["footprint"]["coordinates"]]}
    assert tools._union_coverage_pct(box, [multi]) >= 99.0


def t_item_without_geometry_falls_back_to_bbox_and_says_so():
    r = _with_stac_search([_geo_item("plain", [-71, 43, -69, 44.5])],
                          lambda: tools.search_imagery("planetary-computer", ["landsat-c2-l2"], bbox=_CASCO))
    it = r["items"][0]
    assert it["covers_aoi_pct"] == 100.0 and it["coverage_basis"] == "bbox" and "footprint" not in it, it


def t_recommendation_comes_from_the_set_when_no_scene_covers_the_box():
    west = _geo_item("w", [-71, 43, -69, 44.5], _poly((-71, 43), (-70.15, 43), (-70.15, 44.5), (-71, 44.5)))
    east = _geo_item("e", [-71, 43, -69, 44.5], _poly((-70.2, 43), (-69, 43), (-69, 44.5), (-70.2, 44.5)))
    r = _with_stac_search([west, east], lambda: tools.search_imagery("planetary-computer", ["landsat-c2-l2"], bbox=_CASCO))
    assert r["full_coverage_set"] and set(r["recommended"]["scene_ids"]) == {"w", "e"}, r["recommended"]
    assert r["recommended"]["covers_aoi_pct"] >= 99.0


def t_postcard_names_the_uncovered_side_and_why_it_cannot_fill():
    saved = tools._client.get, tools._get_json
    tools._client.get = lambda url, **k: _ImgResp(200)
    tools._get_json = lambda url, **k: {"bbox": [-71, 43, -69, 44.5], "geometry": _SLANT, "license": "x"}
    try:
        with tempfile.TemporaryDirectory() as d:
            out = tools.render_postcard("planetary-computer", "landsat-c2-l2", "I", "P", "2026-10-21",
                                        out_path=str(Path(d) / "c.html"), bbox=_CASCO)
            out2 = tools.render_postcard("planetary-computer", "landsat-c2-l2", "I", "P", "2026-10-21",
                                         out_path=str(Path(d) / "d.html"), bbox=_CASCO, fill_item_ids=["J"])
    finally:
        tools._client.get, tools._get_json = saved
    assert out["uncovered_pct"] > 20 and "south" in out["uncovered_side"], out
    assert "south" in out["coverage_note"] and "direct COG access" in out["coverage_note"], out
    assert "direct COG access" in out2["coverage_note"] and out2["dropped"], out2


def _compare_fixture(before_cloud, render, **kw):
    item = lambda i, d, c: {"id": i, "datetime": f"{d}T10:00:00Z", "collection": "sentinel-2-l2a", "bbox": [0, 0, 1, 1], "cloud_cover": c}
    saved = tools.search_imagery, tools.compute_statistics, tools.render_map
    wins = iter([[item("S2B_31TBF_20240201_0_L2A", "2024-02-01", 1)], [item("S2B_31TBF_20240101_0_L2A", "2024-01-01", before_cloud)]])
    tools.search_imagery = lambda *a, **k: {"items": next(wins)}
    tools.compute_statistics = lambda *a, **k: {"x": {"mean": 0.5}}
    tools.render_map = render
    try:
        return tools.compare_dates(bbox=[0, 0, 1, 1], window_before="2024-01-01/2024-01-02", window_after="2024-02-01/2024-02-02", **kw)
    finally:
        tools.search_imagery, tools.compute_statistics, tools.render_map = saved


def t_compare_dates_returns_render_maps_error():
    # Field test 15: an out_path outside the repo made render_map return an error and compare_dates index map_path
    out = _compare_fixture(1, lambda **kw: {"error": "out_path must be inside the out root"}, out_path="/tmp/outside.html")
    assert out == {"error": "out_path must be inside the out root"}, out


_PORTLAND_GAZET = [
    {"source": "divisions_area", "id": "or", "name": "Portland", "country": "US", "subtype": "locality",
     "bbox": [-122.837, 45.433, -122.472, 45.653], "similarity": 1.0},
    {"source": "divisions_area", "id": "me", "name": "Portland", "country": "US", "subtype": "locality",
     "bbox": [-70.347, 43.544, -69.976, 43.728], "similarity": 1.0},
]
_STATE_OF = lambda lat, lon: {"state": "Oregon" if lon < -100 else "Maine", "country": "United States"}


def t_geocode_refusal_drops_names_without_the_ask():
    # Field test 15: Le Moule (Guadeloupe) sat in the Portland refusal; its name shares no word with the ask
    moule = {"source": "divisions_area", "id": "gp", "name": "Le Moule", "country": "GP", "subtype": "locality",
             "bbox": [-61.4, 16.3, -61.3, 16.4], "similarity": 1.0}
    out, _ = _with_geocoders({"ids": [*_PORTLAND_GAZET, moule]}, [], lambda: tools.geocode("Portland"), reverse=_STATE_OF)
    assert out["ambiguous"] and len(out["candidates"]) == 2, out["candidates"]
    assert "Le Moule" not in str(out) and out["geocode_note"].startswith("2 places match"), out


def t_geocode_note_names_the_state():
    # Field test 15: the refusal named Oregon and Maine while the note still gave 45.5N 122.7W
    out, _ = _with_geocoders({"ids": _PORTLAND_GAZET}, [], lambda: tools.geocode("Portland"), reverse=_STATE_OF)
    note = out["geocode_note"]
    assert "Oregon, United States" in note and "Maine, United States" in note and "122.7W" not in note, note
    assert "using Portland (Oregon" in note, note


def t_search_imagery_names_an_empty_window_at_the_cap():
    # Field test 15: Hobet's 2016 window found 0 scenes at the cap and nothing said so
    saved = tools._client.post
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"features": []}
    tools._client.post = lambda *a, **k: R()
    try:
        out = tools.search_imagery("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 1, 1], max_cloud_cover=15)
        bare = tools.search_imagery("earth-search", ["sentinel-2-l2a"], bbox=[0, 0, 1, 1])
    finally:
        tools._client.post = saved
    assert "0 scenes at max_cloud_cover 15" in out["note"] and "higher cap" in out["note"], out
    assert "note" not in bare, bare


def t_search_imagery_folds_reprocessed_duplicates():
    # Field test 18: Planetary Computer served a reprocessed scene twice and the reason compared it with itself
    saved = tools._client.post
    box = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    feat = lambda i, base, when="2023-08-18T16:38:41.024000Z", tile="15SXS", cc=0.0144: {
        "id": i, "collection": "sentinel-2-l2a", "bbox": [0, 0, 1, 1], "geometry": box, "assets": {},
        "properties": {"datetime": when, "eo:cloud_cover": cc, "s2:mgrs_tile": tile, "s2:processing_baseline": base}}
    half = {**feat("S2A_T15SYS_20241026", "05.10", tile="15SYS"), "bbox": [0.5, 0, 1.5, 1],
            "geometry": {"type": "Polygon", "coordinates": [[[0.5, 0], [1.5, 0], [1.5, 1], [0.5, 1], [0.5, 0]]]}}
    half["properties"]["eo:cloud_cover"] = 0.0  # clearer than the pick by less than the 0.1% the reason prints, as live
    second_pass = feat("S2A_T15SXS_PASS2", "05.10", when="2023-08-18T18:18:41.024000Z", cc=80.0)
    other = {**feat("S2A_OTHER_DAY", "05.09"), "properties": {"datetime": "2023-08-10T16:38:41Z", "eo:cloud_cover": 30.0, "s2:mgrs_tile": "15SXS"}}
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"features": [half, feat("S2A_T15SXS_20230819", "05.09"), feat("S2A_T15SXS_20241026", "05.10"), second_pass, other]}
    tools._client.post = lambda *a, **k: R()
    try:
        out = tools.search_imagery("planetary-computer", ["sentinel-2-l2a"], bbox=[0, 0, 1, 1])
    finally:
        tools._client.post = saved
    ids = [i["id"] for i in out["items"]]
    assert out["count"] == 4 and out["folded_duplicates"] == 1 and "S2A_T15SXS_20241026" in ids and "S2A_T15SXS_20230819" not in ids, out
    assert "S2A_T15SXS_PASS2" in ids, ids  # a same-day second pass is a different scene
    assert "older processing of the same scene" in out["note"] and "newest" in out["note"], out["note"]
    # the half-covering tile ties on 0.0% cloud: the pick is the clearest, not a contrast with it
    rec = out["recommended"]
    assert rec["id"] == "S2A_T15SXS_20241026" and "not a tradeoff" in rec["reason"] and " but " not in rec["reason"], rec


def t_compare_dates_cloud_caveat_names_side_and_scene():
    # Field test 15: Rolling Fork's before scene was 46% cloud and only the cloud field said so
    ok = lambda **kw: {"map_path": "x.html"}
    cav = _compare_fixture(46, ok)["caveat"][0]
    assert "before scene S2B_31TBF_20240101_0_L2A 46%" in cav, cav
    assert "caveat" not in _compare_fixture(10, ok)


def t_stack_facts_geocoded_is_the_tools_alone():
    # Field test 15: a caller's {"geocoded": "nominatim"} for a hand-picked box credited a lookup that never ran
    out = _compare_fixture(1, lambda **kw: {"map_path": "x.html"}, stack_facts={"geocoded": "nominatim"})
    assert out["stack_facts_ignored"] == ["geocoded"], out
    assert tools._map_stack_facts([], [], {"geocoded": "nominatim"}, [0, 0, 1, 1])["geocoded"] is False
    # a box geocode returned keeps the claim, with its source, matched at 4 decimals
    tools._record_geocode({"bbox": [10.00001, 20, 11, 21], "source": "gazet"})
    keep = lambda v, b: tools._map_stack_facts([], [], {"geocoded": v}, b)["geocoded"]
    assert keep("gazet", [10, 20, 11, 21]) == "gazet"
    assert keep("nominatim", [10, 20, 11, 21]) is False, "source must match"
    assert keep("gazet", [10, 20, 12, 21]) is False, "a box geocode never returned"
    token = tools._tool_geocoded.set("gazet")
    try:
        assert tools._map_stack_facts([], [], {"geocoded": "nominatim"}, [0, 0, 1, 1])["geocoded"] == "gazet"
    finally:
        tools._tool_geocoded.reset(token)


def t_geocode_accent_strip_matches_zurich():
    assert tools._names_the_ask("Zürich", "Zurich") and not tools._names_the_ask("Le Moule", "Portland")


def t_one_scene_key_in_recommended():
    # Casco Bay: a 100% scene at 23% cloud and a newer 68% scene at 5%. Coverage is outside the
    # tie band of the 100% one, so the key decides, not recency
    full = {"id": "full", "datetime": "2026-10-10T15:00:00Z", "cloud_cover": 23.0, "covers_aoi_pct": 100.0}
    part = {"id": "part", "datetime": "2026-10-21T15:00:00Z", "cloud_cover": 5.0, "covers_aoi_pct": 68.0}
    assert tools._recommend([part, full])["id"] == "full"
    # inside the tie bands and the recency band, _scene_key picks: the newer scene is no better on it
    a = {"id": "a", "datetime": "2026-10-10T15:00:00Z", "cloud_cover": 6.0, "covers_aoi_pct": 99.0}
    b = {"id": "b", "datetime": "2026-10-13T15:00:00Z", "cloud_cover": 3.0, "covers_aoi_pct": 97.0}
    rec = tools._recommend([a, b])
    assert rec["id"] == "a" and "and newest of" not in rec["reason"] and "best on coverage, then cloud, of 2 near-equal scenes within 7 days of the newest" in rec["reason"], rec
    # outside the recency band the newer look still wins the tie, as stop 8 set it
    c = {**b, "id": "c", "datetime": "2026-10-25T15:00:00Z"}
    rec = tools._recommend([a, c])
    assert rec["id"] == "c", rec
    # an archive with no cloud figures: a dead tie keeps the newest, not the id as text
    old = {"id": "a", "datetime": "2026-10-10T15:00:00Z", "cloud_cover": None, "covers_aoi_pct": 90.0}
    new = {"id": "z", "datetime": "2026-10-12T15:00:00Z", "cloud_cover": None, "covers_aoi_pct": 90.0}
    assert tools._recommend([old, new])["id"] == "z"


def t_postcard_prints_the_uncovered_wedge_on_the_card():
    saved = tools._client.get, tools._get_json
    tools._client.get = lambda url, **k: _ImgResp(200)
    tools._get_json = lambda url, **k: {"bbox": [-71, 43, -69, 44.5], "geometry": _SLANT, "license": "x"}
    try:
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "c.html")
            out = tools.render_postcard("planetary-computer", "landsat-c2-l2", "I", "P", "2026-10-21",
                                        caption="Casco Bay", out_path=p, bbox=_CASCO)
            html = Path(p).read_text()
    finally:
        tools._client.get, tools._get_json = saved
    want = f"{out['uncovered_pct']:.1f}% of the box uncovered, mostly {out['uncovered_side']}"
    assert want in html and "Casco Bay" in html, (want, out)


def t_map_subtitle_prints_the_uncovered_wedge():
    lay = [{"type": "raster", "name": "r", "tiles": "https://titiler.xyz/x/{z}/{x}/{y}.png", "bounds": [0, 0, 10, 4]}]
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "m.html")
        out = tools.render_map("t", [0, 0, 10, 10], lay, subtitle="hello", out_path=p)
        html = Path(p).read_text()
        assert f"{out['state']['uncovered_pct']:.1f}% of the box uncovered, mostly north" in html and "hello" in html, out
        full = [{**lay[0], "bounds": [0, 0, 10, 10]}]
        tools.render_map("t", [0, 0, 10, 10], full, out_path=p)
        assert "uncovered" not in Path(p).read_text()


class _StatsResp:
    status_code = 200

    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


def _stretch_env(item_assets, calls):
    """Stub the item fetch and the tiler statistics POST; returns the restore function."""
    saved = tools._get_json, tools._client.post
    item = {"bbox": [0, 0, 2, 2], "assets": item_assets}
    tools._get_json = lambda url, **k: item

    def post(url, **k):
        if "statistics" not in url:
            return _StatsResp({"id": "mosaic1"})  # the PC mosaic registration shares this POST
        calls.append(url)
        return _StatsResp({"b1": {"min": 0, "max": 99999, "percentile_2": 14000.0, "percentile_98": 15500.0}})

    tools._client.post = post
    tools._STRETCH.clear()
    tools._ITEMS.clear()

    def restore():
        tools._get_json, tools._client.post = saved
        tools._STRETCH.clear()
        tools._ITEMS.clear()

    return restore


def _stretch_layer(**kw):
    return {"type": "item", "name": "lst", "catalog": "planetary-computer", "collection_id": "modis-11A1-061",
            "item_id": "I1", "assets": ["LST_Day_1km"], **kw}


def t_stretch_from_data_when_no_rescale():
    # field test 15: MODIS LST drew black on a guessed rescale and took 2 hand-tuned tries
    calls = []
    restore = _stretch_env({"LST_Day_1km": {"raster:bands": [{"scale": 0.02}]}}, calls)
    try:
        with tempfile.TemporaryDirectory() as d:
            r = tools.render_map("t", [0, 0, 1, 1], [_stretch_layer()], out_path=str(Path(d) / "a.html"))
        st = r["state"]["layers"][0]
        # raw numbers, not x0.02 physical: tiles are raw, so a scaled stretch would draw black
        assert st["rescale_used"] == "14000,15500", st
        assert "2nd to 98th percentile" in st["rescale_basis"] and "box" in st["rescale_basis"], st
        assert "rescale=14000%2C15500" in st["tiles"], st["tiles"]
        assert len(calls) == 1
    finally:
        restore()


def t_stretch_given_rescale_passes_through():
    calls = []
    restore = _stretch_env({"LST_Day_1km": {}}, calls)
    try:
        with tempfile.TemporaryDirectory() as d:
            r = tools.render_map("t", [0, 0, 1, 1], [_stretch_layer(rescale="13500,15500")], out_path=str(Path(d) / "a.html"))
        st = r["state"]["layers"][0]
        assert st["rescale_used"] == "13500,15500" and st["rescale_basis"] == "given", st
        assert "rescale=13500%2C15500" in st["tiles"] and not calls
    finally:
        restore()


def t_stretch_leaves_expression_visual_and_categorical_alone():
    calls = []
    restore = _stretch_env({"map": {"raster:bands": [{"classification:classes": [{"value": 10}]}]}, "visual": {}}, calls)
    try:
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "a.html")
            e = tools.render_map("t", [0, 0, 1, 1], [{**_stretch_layer(), "assets": None, "expression": "(nir-red)/(nir+red)"}], out_path=out)
            assert e["state"]["layers"][0]["rescale_used"] == "-1,1", e
            v = tools.render_map("t", [0, 0, 1, 1], [_stretch_layer(assets=["visual"])], out_path=out)
            c = tools.render_map("t", [0, 0, 1, 1], [_stretch_layer(assets=["map"])], out_path=out)
            w = tools.render_map("t", [0, 0, 1, 1], [_stretch_layer(colormap_name="esa-worldcover")], out_path=out)
        for r in (v, c, w):
            assert "rescale_used" not in r["state"]["layers"][0] and "rescale=" not in r["state"]["layers"][0]["tiles"], r
        assert not calls
    finally:
        restore()


def t_stretch_fetched_once_per_item_asset_box():
    calls = []
    restore = _stretch_env({"LST_Day_1km": {}}, calls)
    try:
        with tempfile.TemporaryDirectory() as d:
            for i in range(2):
                tools.render_map("t", [0, 0, 1, 1], [_stretch_layer()], out_path=str(Path(d) / f"{i}.html"))
            assert len(calls) == 1, calls
            tools.render_map("t", [0, 0, 1, 2], [_stretch_layer()], out_path=str(Path(d) / "c.html"))
            assert len(calls) == 2, calls
        # tile_url_template shares the same stretch and the same cache
        u = tools.tile_url_template("planetary-computer", "modis-11A1-061", "I1", assets=["LST_Day_1km"], bbox=[0, 0, 1, 2])
        assert "rescale=14000%2C15500" in u and len(calls) == 2, (u, calls)
    finally:
        restore()


def t_stretch_one_range_across_a_coverage_set():
    # 2 tiles of one product side by side on 2 ranges draw a seam: one temperature, 2 colours
    calls = []
    restore = _stretch_env({"LST_Day_1km": {}}, calls)
    post = tools._client.post

    def per_item(url, **k):
        r = post(url, **k)
        if "statistics" in url and "I2" in url + json.dumps(k, default=str):
            r._b = {"b1": {"percentile_2": 13900.0, "percentile_98": 15800.0}}
        return r

    tools._client.post = per_item
    try:
        items = [{"id": i, "catalog": "planetary-computer", "collection": "modis-11A1-061", "bbox": [0, 0, 2, 2]} for i in ("I1", "I2")]
        layer = {"type": "coverage_set", "set": {"date": "d", "items": items}, "assets": ["LST_Day_1km"]}
        with tempfile.TemporaryDirectory() as d:
            r = tools.render_map("t", [0, 0, 1, 1], [layer], out_path=str(Path(d) / "a.html"))
        a, b = r["state"]["layers"][:2]
        assert len(calls) == 2, calls
        assert a["rescale_used"] == b["rescale_used"] == "13900,15800", (a, b)
        assert "widest of 2 scenes" in a["rescale_basis"], a
        assert "rescale=13900%2C15800" in a["tiles"] and "rescale=13900%2C15800" in b["tiles"]
    finally:
        restore()


def t_pc_mosaic_register_reads_searchid_or_id():
    # field test 16: Planetary Computer's register reply switched "id" to
    # "searchid" and every WorldCover map died on KeyError; both keys serve
    class R:
        def __init__(self, j): self._j = j
        def raise_for_status(self): pass
        def json(self): return self._j
    saved_post = tools._client.post
    try:
        tools._client.post = lambda url, **kw: R({"searchid": "pc1-new"})
        tools._pc_mosaic_cache.pop("esa-worldcover:A", None)
        assert tools._pc_mosaic_id("esa-worldcover", "A") == "pc1-new"
        tools._client.post = lambda url, **kw: R({"id": "old-style"})
        tools._pc_mosaic_cache.pop("esa-worldcover:B", None)
        assert tools._pc_mosaic_id("esa-worldcover", "B") == "old-style"
        tools._client.post = lambda url, **kw: R({"links": []})
        tools._pc_mosaic_cache.pop("esa-worldcover:C", None)
        import httpx as _hx
        try:
            tools._pc_mosaic_id("esa-worldcover", "C")
            assert False, "no id should raise the HTTPError the fallback catches"
        except _hx.HTTPError:
            pass
    finally:
        tools._client.post = saved_post


def t_field_test_copies_artifacts_and_refuses_bad_out_dirs():
    demo = Path(tempfile.mkdtemp()) / "demo"
    demo.mkdir()
    (demo / "map.html").write_text("<html></html>", encoding="utf-8")
    saved = os.environ["GROUNDSTATION_OUT"]
    os.environ["GROUNDSTATION_OUT"] = str(demo)
    try:
        case = {"tag": "t", "prompt": "Show me a place", "answer": "ok", "returned": "x", "artifact": str(demo / "map.html"), "image": "x.png"}
        cases = {"title": "t", "heading": "H", "sub": "s", "lede": "l", "rounds": [{"name": "r", "cases": [case]}]}
        out = Path(tempfile.mkdtemp()) / "gs"
        r = tools.field_test(cases, str(out))
        assert "error" not in r, r
        assert (out / "img" / "map.html").is_file()
        assert json.loads(Path(r["cases"]).read_text())["rounds"][0]["cases"][0]["artifact"] == "img/map.html"
        assert 'href="img/map.html"' in Path(r["page"]).read_text(encoding="utf-8")
        assert "error" in tools.field_test(cases, "relative/dir")
        assert "error" in tools.field_test(cases, str(demo / "inside"))
        assert "error" in tools.field_test(cases, str(Path(tools.__file__).resolve().parents[2] / "x"))
    finally:
        os.environ["GROUNDSTATION_OUT"] = saved


def t_versions_agree():
    # the plugin version is what installs cache by and what the Store compares;
    # pyproject's is what the MCP handshake reports. One number, three files
    import tomllib
    root = Path(__file__).resolve().parents[1]
    py = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    plug = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"]
    mkt = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())["plugins"][0]["version"]
    assert py == plug == mkt, (py, plug, mkt)


if __name__ == "__main__":
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        check(name, fn)
    print(f"\n{len([k for k in globals() if k.startswith('t_')]) - len(FAILED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)
