"""Plain tool functions over the cloud-native geospatial stack.

Every public function here is MCP-ready: typed args, a docstring that doubles
as the tool description, JSON-serializable returns. The same functions back
the MCP server (server.py) and the briefing generator (briefing/brief.py),
and follow the developmentseed/mcp-toolsets shape so they can be dropped into
that scaffold as a toolset later.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from urllib.parse import quote, urljoin
from typing import Any

import httpx

UA = {"User-Agent": "groundstation/0.1 (Development Seed labs prototype)"}
# titiler.xyz is a shared community endpoint with rate limits (429s under heavy
# use) — point GROUNDSTATION_TITILER at your own TiTiler deployment for real work
TITILER = os.environ.get("GROUNDSTATION_TITILER", "https://titiler.xyz")
# Development Seed's public eoAPI raster instance: the HRRR smoke tiler, and the
# fallback when titiler.xyz rate-limits. Its /external routes read any COG or
# GDAL-readable URL; it has no /stac routes
EOAPI = os.environ.get("GROUNDSTATION_EOAPI", "https://raster.eoapi.dev")

CATALOGS: dict[str, dict[str, str]] = {
    "earth-search": {
        "stac": "https://earth-search.aws.element84.com/v1",
        "raster": "titiler-xyz",
        "notes": "Element 84 Earth Search: Sentinel-2 L2A/L1C, Sentinel-1, Copernicus DEM. Landsat and NAIP here are requester-pays and won't tile — use planetary-computer for those.",
    },
    "veda": {
        "stac": "https://openveda.cloud/api/stac",
        "raster": "veda",
        "notes": "NASA VEDA: curated Earth science datasets (fires, air quality, climate indicators, disasters).",
    },
    "planetary-computer": {
        "stac": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "raster": "pc",
        "notes": "Microsoft Planetary Computer: broad archive incl. MODIS, Sentinel, Landsat, land cover, DEMs.",
    },
}

_client = httpx.Client(timeout=30, headers=UA, follow_redirects=True)
_collections_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_gazet_skip_until = 0.0
# Gazet's JSON API lives on the Hugging Face Space. The gazet.ds.io domain fronts
# a static page, which is why every field test through No.7 saw only Nominatim.
# mode=fuzzy is a Jaro-Winkler match over Overture divisions and Natural Earth,
# no LLM in the path, and ids_only keeps the payload to bboxes. It always returns
# a best row, so the similarity gate is what separates a hit from the closest
# miss: "Salinas Valley" came back as Salinas, Mexico at 0.9
GAZET_URL = os.environ.get("GAZET_URL", "https://developmentseed-gazet.hf.space/api/search")
GAZET_MIN_SIMILARITY = 0.95
# two candidates in different places whose scores sit this close are both
# real contenders, and the place-taking tools refuse the name
AMBIGUITY_MARGIN = 0.15
# a box under this in both directions is a point (a Nominatim node), not an extent
POINT_DEG = 0.001
GAZET_TIMEOUT_S = 10.0  # geocode is a hot path: a cold Space takes ~8s, a dead one must not hang the tool


def slugify(text: str, max_len: int = 60) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower())[:max_len].strip("-")


# a feature word in the ask -> the candidate kinds that count as that feature.
# The sources use several words for one kind: Nominatim tags the Berkeley Pit
# "water", Gazet has "alkaline lake" (field test No.13)
_FEATURE_KINDS = {
    **dict.fromkeys(("lake", "reservoir"), {"lake", "reservoir", "water", "alkaline lake"}),
    **dict.fromkeys(("mountain", "mount", "peak"), {"mountain", "peak", "volcano"}),
    **{w: {w} for w in ("volcano", "river", "glacier", "bay", "island", "crater", "dam", "mine")},
    "falls": {"waterfall"},
}


# a point candidate's half-width in km by kind. Nominatim pads its own place
# nodes (a town node came back 0.08 deg), so city and town here are for a source that does not
_KIND_HALF_KM = {
    **dict.fromkeys(("bay", "marina", "harbour", "beach", "village", "hamlet", "mine", "pit"), 2.0),
    **dict.fromkeys(("town", "volcano", "mountain", "peak", "lake", "reservoir", "water"), 5.0),
    "city": 10.0,
}


_PHYSICAL_KINDS = set().union(*_FEATURE_KINDS.values())


def _feature_word(query: str) -> tuple[str | None, set[str]]:
    return next(((t, _FEATURE_KINDS[t]) for t in re.findall(r"[a-z]+", query.lower()) if t in _FEATURE_KINDS), (None, set()))


def _nominatim_kind(r: dict[str, Any]) -> str:
    return str(r.get("addresstype") or r.get("type") or "place").lower()


def _candidate(name: str, bbox: list[float], source: str, where: str, kind: str,
               importance: float | None, want: set[str]) -> dict[str, Any]:
    """One geocode candidate from any source, scored by the one rule, with the inputs shown.

    score = importance (0 where the source has none, as Gazet does)
          + 0.03 x log10 of the extent in km2 (a point scores 0)
          + 0.1 when the kind is a physical feature (lake, volcano, river...)
          + 0.2 more when the ask names that feature ("lake", "volcano")
    The weights were set against the live sources (field test No.14): the
    Lake Charles city at importance 0.55 still beats the lake at 0.18 with
    both bonuses, the Shasta Lake reservoir at 0.34 beats the town at 0.44
    with them, and the Kilauea volcano at 0.48 clears the village at 0.35
    by more than AMBIGUITY_MARGIN without a feature word

    extent_basis says where the box comes from: own, or for a point (extent
    under POINT_DEG) kind when _KIND_HALF_KM sizes it, else default (the 0.3 deg square)
    """
    w, s, e, n = bbox
    width, height = e - w + (360 if e < w else 0), n - s  # west > east crosses the dateline
    km2 = width * height * 111.32**2 * max(math.cos(math.radians((s + n) / 2)), 0.01)
    score = (importance or 0.0) + 0.03 * math.log10(max(km2, 1.0))
    if kind in _PHYSICAL_KINDS:
        score += 0.1 + (0.2 if kind in want else 0.0)
    basis = "own" if max(width, height) >= POINT_DEG else "kind" if kind in _KIND_HALF_KM else "default"
    return dict(name=name, bbox=bbox, source=source, where=where, kind=kind,
                extent=[round(width, 4), round(height, 4)], importance=importance, score=round(score, 3),
                extent_basis=basis)


# the boxes geocode returned, with the source that answered, so a caller's stack_facts can claim a geocode
# only for a box that really came from one
# ponytail: last 64 boxes in memory, rounded to 4 decimals; a persistent or per-session record if callers outlive it
_geocoded_boxes: dict[tuple[float, ...], str] = {}


def _bbox_key(bbox: list[float]) -> tuple[float, ...]:
    return tuple(round(float(x), 4) for x in bbox)


def _record_geocode(out: dict[str, Any]) -> dict[str, Any]:
    key = _bbox_key(out["bbox"])
    _geocoded_boxes.pop(key, None)
    _geocoded_boxes[key] = out["source"]
    while len(_geocoded_boxes) > 64:
        del _geocoded_boxes[next(iter(_geocoded_boxes))]
    return out


def _geocoded_fact(facts: dict[str, Any] | None, bbox: list[float] | None) -> bool | str:
    """What the panel may credit: the tool's own geocode, else a caller's claim that matches a box geocode returned"""
    claimed = (facts or {}).get("geocoded")
    if claimed and bbox is not None and _geocoded_boxes.get(_bbox_key(bbox)) == claimed:
        return claimed
    return _tool_geocoded.get()


def _names_the_ask(name: str, ask: str) -> bool:
    """A candidate whose name shares no word with the ask is a fuzzy-match stray (Le Moule for Portland)"""
    words = lambda t: set(re.findall(r"\w+", unicodedata.normalize("NFKD", t.lower()).encode("ascii", "ignore").decode()))
    return bool(words(name) & words(ask))


def _state_country(c: dict[str, Any]) -> str:
    """State and country at a candidate's centre, by one Nominatim reverse lookup at state zoom.

    Gazet rows carry a country code and no state; ids_only=false adds an ISO
    region code ("US-OR") but also every geometry, 4 MB for Canada, so a
    reverse lookup on the refusal path is cheaper than either
    """
    w, s, e, n = c["bbox"]
    try:
        a = _get_json("https://nominatim.openstreetmap.org/reverse",
                      params={"lat": (s + n) / 2, "lon": (w + e) / 2, "zoom": 5, "format": "jsonv2"}).get("address") or {}
        return ", ".join(x for x in (a.get("state"), a.get("country")) if x) or c["where"]
    except Exception:
        return c["where"]


def _kind_where(c: dict[str, Any]) -> str:
    """County, state and country for a Nominatim candidate; a Gazet record carries what geocode found for it"""
    if c.get("source") != "nominatim":
        return c.get("where") or ""
    parts = [p for p in c["name"].split(", ") if not p.strip().isdigit()]
    county = [p for p in parts[1:-2] if p.endswith(("County", "Parish"))][:1]
    return ", ".join(county + parts[1:][-2:]) or c.get("where") or ""


def _ambiguous_error(place: str, g: dict[str, Any]) -> dict[str, Any]:
    which = " or ".join(f"{c.get('kind', 'place')} ({_kind_where(c)})" for c in g["candidates"])
    return dict(
        error=f"{place!r} matches more than one place: {which}? Pass a fuller name or a bbox",
        candidates=g["candidates"],
        geocode_note=g.get("geocode_note"),
    )


def _bboxes_overlap(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _resolve_bbox(place: str | None, bbox: list[float] | None) -> list[float] | dict[str, Any]:
    if bbox is None and place:
        g = geocode(place)
        if "error" in g:
            return g
        if g.get("ambiguous"):
            return _ambiguous_error(place, g)
        bbox = g["bbox"]
    if bbox is None:
        return {"error": "Provide bbox or place"}
    return bbox


def _rfc3339_window(window: str) -> str:
    """Date-only ends ("2024-10-01/2024-10-20") become the full-day RFC3339 range STAC servers accept."""
    lo, sep, hi = window.partition("/")
    if len(lo) == 10:
        lo += "T00:00:00Z"
    if len(hi) == 10:
        hi += "T23:59:59Z"
    return lo + sep + hi


def last_days_window(days: int, end_days_ago: int = 0) -> str:
    """RFC3339 range covering the last `days` days, ending `end_days_ago` days ago."""
    import datetime as dt

    end = dt.date.today() - dt.timedelta(days=end_days_ago)
    start = end - dt.timedelta(days=days)
    return f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"


def pick_best_scene(items: list[dict[str, Any]], aoi_bbox: list[float]) -> dict[str, Any] | None:
    """Best scene = covers the AOI, then low cloud — a clear sliver must not win."""
    if not items:
        return None
    return max(items, key=lambda i: _coverage(i, aoi_bbox) - (i.get("cloud_cover") or 100) / 200)


def _coverage(item: dict[str, Any], aoi_bbox: list[float]) -> float:
    w, s, e, n = aoi_bbox
    iw, is_, ie, in_ = item["bbox"]
    ov = max(0, min(e, ie) - max(w, iw)) * max(0, min(n, in_) - max(s, is_))
    return ov / max((e - w) * (n - s), 1e-9)


def _mgrs_tile(item_id: str) -> str:
    parts = item_id.split("_")
    return parts[1] if len(parts) > 1 else item_id


# ponytail: "covers the AOI" threshold — bbox math is approximate, 99 avoids
# float-edge false negatives on genuinely-covering scenes
FULL_COVERAGE_PCT = 99.0
# below this a rendered map has a hole big enough to say so: the renderers return
# coverage_note (docs/adr-caveats.md). At or above it they stay quiet
PARTIAL_COVERAGE_PCT = 95.0


# ponytail: footprints are sampled on a 64x64 grid of AOI cell centres, so a
# footprint edge is only resolved to 1/64 of the box (about 1.6 points of
# coverage). A footprint thinner than a cell can read 0; raise the grid if it matters
COVER_GRID = 64


def _shape_polys(shape: Any) -> list[list[list[list[float]]]]:
    """A bbox [w,s,e,n] or a GeoJSON Polygon/MultiPolygon as a list of polygons
    (each a list of rings). Anything else is no shape."""
    if isinstance(shape, dict):
        c = shape.get("coordinates")
        if shape.get("type") == "Polygon" and c:
            return [c]
        if shape.get("type") == "MultiPolygon" and c:
            return list(c)
        return []
    if isinstance(shape, (list, tuple)) and len(shape) >= 4:
        return [[[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]] for b in _split_antimeridian(list(shape)[:4])]
    return []


def _in_polys(x: float, y: float, polys: list) -> bool:
    for poly in polys:
        inside = False  # even-odd over every ring, so a hole flips the point back out
        for ring in poly:
            j = len(ring) - 1
            for i in range(len(ring)):
                xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                    inside = not inside
                j = i
        if inside:
            return True
    return False


def _cover_mask(aoi: list[float], shape: Any) -> int:
    """Bit k set when AOI grid point k lies inside the shape."""
    polys = _shape_polys(shape)
    if not polys:
        return 0
    w, s, e, n = aoi
    span = (e - w) % 360 if w > e else e - w
    mask, k = 0, 0
    for r in range(COVER_GRID):
        y = s + (n - s) * (r + 0.5) / COVER_GRID
        for c in range(COVER_GRID):
            x = w + span * (c + 0.5) / COVER_GRID
            # footprints that cross the antimeridian come as lon beyond 180 or below -180
            if any(_in_polys(x + d, y, polys) for d in (0, 360, -360)):
                mask |= 1 << k
            k += 1
    return mask


def _is_bbox(shape: Any) -> bool:
    return isinstance(shape, (list, tuple)) and len(shape) >= 4


def _union_coverage_pct(aoi: list[float], shapes: list[Any]) -> float:
    """Union coverage of the AOI by several scene shapes, bboxes or footprints.

    All bboxes: exact coordinate sweep. Any footprint polygon: the sampled grid
    above, so holes, multipolygons and overlap all come out of one rule."""
    if any(not _is_bbox(sh) for sh in shapes):
        if not aoi or len(aoi) < 4 or aoi[3] <= aoi[1]:
            return 0.0
        m = 0
        for sh in shapes:
            m |= _cover_mask(aoi, sh)
        return round(100.0 * bin(m).count("1") / COVER_GRID**2, 1)
    return _union_bbox_coverage_pct(aoi, shapes)


def _uncovered_side(aoi: list[float], shapes: list[Any]) -> tuple[float, str]:
    """(percent of the AOI no shape covers, where it sits: 'south', 'the northwest corner'...)."""
    m = 0
    for sh in shapes:
        m |= _cover_mask(aoi, sh)
    xs = ys = cnt = 0
    for k in range(COVER_GRID**2):
        if not m >> k & 1:
            r, c = divmod(k, COVER_GRID)
            xs += (c + 0.5) / COVER_GRID * 2 - 1
            ys += (r + 0.5) / COVER_GRID * 2 - 1
            cnt += 1
    if not cnt:
        return 0.0, ""
    dx, dy = xs / cnt, ys / cnt
    ns = "north" if dy > 0.25 else "south" if dy < -0.25 else ""
    ew = "east" if dx > 0.25 else "west" if dx < -0.25 else ""
    where = ns + ew or "the middle"
    if ns and ew and abs(dx) > 0.5 and abs(dy) > 0.5:
        where = f"the {where} corner"
    return round(100.0 * cnt / COVER_GRID**2, 1), where


def _with_wedge(text: str, unc: tuple[float, str] | None) -> str:
    """Append the uncovered-wedge sentence to a card caption or map subtitle, so the
    reader of the artifact sees what the result's uncovered_pct says"""
    if not unc or not unc[0]:
        return text
    return " · ".join(x for x in (text, f"{unc[0]:.1f}% of the box uncovered, mostly {unc[1]}") if x)


def _item_shape(it: dict[str, Any]) -> Any:
    """The scene's footprint polygon when the catalog gave one, else its bbox."""
    return it.get("footprint") or it.get("bbox")


def _union_bbox_coverage_pct(aoi: list[float], boxes: list[list[float]]) -> float:
    """Exact union coverage by axis-aligned rectangles (coordinate sweep), so
    overlapping tiles never double-count."""
    aoi_parts = _split_antimeridian(aoi)
    aoi_area = sum((part[2] - part[0]) * (part[3] - part[1]) for part in aoi_parts)
    if aoi_area <= 0:
        return 0.0
    split_boxes = []
    for b in boxes:
        split_boxes.extend(_split_antimeridian(b))
    total_covered = 0.0
    for part in aoi_parts:
        paw, pas_, pae, pan = part
        clipped = []
        for b in split_boxes:
            if not b or len(b) < 4:
                continue
            w = max(paw, b[0])
            s = max(pas_, b[1])
            e = min(pae, b[2])
            n = min(pan, b[3])
            if e > w and n > s:
                clipped.append((w, s, e, n))
        if not clipped:
            continue
        xs = sorted({v for r in clipped for v in (r[0], r[2])})
        ys = sorted({v for r in clipped for v in (r[1], r[3])})
        covered = 0.0
        for i in range(len(xs) - 1):
            for j in range(len(ys) - 1):
                cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
                if any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in clipped):
                    covered += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
        total_covered += covered
    return round(100.0 * total_covered / aoi_area, 1)


def find_full_coverage_set(
    items: list[dict[str, Any]],
    aoi_bbox: list[float],
    threshold: float = FULL_COVERAGE_PCT,
) -> dict[str, Any] | None:
    """Newest same-collection, same-day scene set whose union covers the AOI.

    The Calgary case: a city straddling two UTM zones has NO single scene that
    covers it, so the freshest scene silently crops an edge. Same acquisition
    date is the practical proxy for "same pass" — adjacent granules share it.
    Greedy by marginal coverage gain; duplicate same-tile items add zero gain
    and drop out naturally. Returns None when no day in the results reaches
    the threshold.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in items:
        day = (it.get("datetime") or "")[:10]
        if day and _item_shape(it) and it.get("collection"):
            groups.setdefault((it["collection"], day), []).append(it)
    for coll, day in sorted(groups, key=lambda k: k[1], reverse=True):
        remaining = list(groups[(coll, day)])
        chosen: list[dict[str, Any]] = []
        boxes: list[Any] = []
        covered = 0.0
        while remaining:
            best, best_gain = None, 0.05  # require a real gain, not float noise
            for it in remaining:
                gain = _union_coverage_pct(aoi_bbox, boxes + [_item_shape(it)]) - covered
                if gain > best_gain:
                    best, best_gain = it, gain
            if best is None:
                break
            chosen.append(best)
            boxes.append(_item_shape(best))
            covered = _union_coverage_pct(aoi_bbox, boxes)
            remaining.remove(best)
            if covered >= threshold:
                return {
                    "date": day,
                    "union_covers_aoi_pct": covered,
                    "items": chosen,
                }
    return None


_pc_mosaic_cache: dict[str, str] = {}


PC_REGISTER_TIMEOUT_S = 15.0  # field test No.8: registration hung 30s three times and took the whole render down
_PC_FALLBACKS: set[str] = set()  # items whose mosaic registration failed and got item tiles instead


def _pc_mosaic_id(collection_id: str, item_id: str) -> str:
    # PC's registered-mosaic tiler is its canonical rendering path; registered
    # searches persist server-side, so map artifacts built on them stay shareable
    key = f"{collection_id}:{item_id}"
    if key not in _pc_mosaic_cache:
        r = _client.post(
            "https://planetarycomputer.microsoft.com/api/data/v1/mosaic/register",
            json={"collections": [collection_id], "ids": [item_id]},
            timeout=PC_REGISTER_TIMEOUT_S,
        )
        r.raise_for_status()
        # the register reply carried "id" until 2026-10; it is "searchid" now
        # (field test 16), and a missing key is the fallback's job, not a crash
        j = r.json()
        mid = j.get("searchid") or j.get("id")
        if not mid:
            raise httpx.HTTPError(f"mosaic register reply carries no id: {sorted(j)}")
        _pc_mosaic_cache[key] = mid
    return _pc_mosaic_cache[key]


def _get_json(url: str, **kwargs: Any) -> Any:
    r = _client.get(url, **kwargs)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- geocoding


def _where(o: dict[str, Any]) -> str:
    return ", ".join(x for x in (o.get("country"), o.get("subtype")) if x)


def _gazet_where(o: dict[str, Any]) -> str:
    return ", ".join(x for x in (o.get("country"), _at(o)) if x)


def _at(o: dict[str, Any]) -> str:
    """A Gazet record's centre as a speakable position, so two same-named places read apart."""
    b = o.get("bbox") or []
    if len(b) != 4:
        return "unknown position"
    if b[2] - b[0] >= 180:
        return "spanning the antimeridian"
    lat = (b[1] + b[3]) / 2
    lon = (b[0] + b[2] + (360 if b[0] > b[2] else 0)) / 2  # west > east crosses the dateline
    lon = lon - 360 if lon > 180 else lon
    return f"at {abs(lat):.1f}{'N' if lat >= 0 else 'S'} {abs(lon):.1f}{'E' if lon >= 0 else 'W'}"


def _wrap_aware_bbox(geometry: dict[str, Any]) -> list[float] | None:
    """The real extent of a geometry that crosses the antimeridian, west > east per RFC 7946.

    A minimum bounding rectangle over Fiji is [-180, -21, 180, -12], the whole
    world. Shifting negative longitudes by 360 puts both halves on one side of
    the dateline, where the extent is honest. If it is still 180 degrees wide
    the thing genuinely wraps the globe and there is nothing better to say.
    """
    t = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polys = coords if t == "MultiPolygon" else [coords] if t == "Polygon" else []
    pts = [pt for poly in polys for ring in poly[:1] for pt in ring]
    if not pts:
        return None
    lats = [pt[1] for pt in pts]
    plain = [pt[0] for pt in pts]
    shifted = [x + 360 if x < 0 else x for x in plain]
    # take whichever frame holds the geometry in the narrower span
    w, e = min(plain), max(plain)
    ws, es = min(shifted), max(shifted)
    if es - ws < e - w:
        w, e = ws, es
    if e - w >= 180:
        return None
    unwrap = lambda x: x - 360 if x > 180 else x
    return [unwrap(w), min(lats), unwrap(e), max(lats)]


def geocode(query: str) -> dict[str, Any]:
    """Resolve a place name to coordinates and a bounding box.

    Tries Development Seed's Gazet geocoder first: its no-LLM fuzzy index over
    Overture divisions and Natural Earth answers countries, regions, counties
    and physical features with their real extent. Everything else (towns,
    landmarks, descriptive phrases) falls back to OSM Nominatim. Use concise
    names ("Barotse Floodplain", "Chelan County") rather than long phrases.
    Returns {name, lat, lon, bbox: [w, s, e, n], source, candidates}, plus
    geocode_note when several places match the name and one was picked.
    candidates lists up to 4 {name, bbox, source, where, kind, extent,
    importance, score}, best score first, kind being the source's own word
    lowercased (Gazet subtype, Nominatim addresstype or type: volcano,
    reservoir, town, county...) or "place", extent [width, height] in degrees,
    importance Nominatim's own (None from Gazet). Every candidate from either
    source is scored by one rule (see _candidate): importance, extent, a
    physical-feature kind, and a feature word in the query (lake, volcano,
    river, peak...) that names the candidate's kind. Lake, reservoir and water
    count as one kind. The top score is the result; its bbox is the
    candidate's own however small, widened only when it is a point: by
    its kind's half-width (a bay 2 km, a town or volcano 5, a city 10),
    or the 0.3 deg square when the kind is unknown. extent_basis on the result
    and each candidate says which: own, kind or default. ambiguous
    is true when a candidate in another place scores within AMBIGUITY_MARGIN
    of the top; each candidate's where then names its state and country, and
    the place-taking tools refuse the name until a fuller name or bbox is passed.
    """
    global _gazet_skip_until
    try:
        if time.time() < _gazet_skip_until:
            raise ValueError("gazet marked down")
        r = _client.get(
            GAZET_URL,
            params={"q": query, "mode": "fuzzy", "limit": 5, "ids_only": "true"},
            timeout=GAZET_TIMEOUT_S,
        )
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            _gazet_skip_until = time.time() + 900
            raise ValueError("gazet returned non-JSON")
        hits = [
            h for h in (r.json().get("ids") or [])
            if h.get("bbox") and len(h["bbox"]) == 4
            and (h.get("similarity") or 0.0) >= GAZET_MIN_SIMILARITY
        ]
        want = _feature_word(query)[1]
        # Jaro-Winkler ties are real places sharing a name (Loja Province and
        # Loja Canton both score 1.0), so every tie is a candidate and the score
        # picks among them, "Lake Powell" the Utah reservoir over an Australian
        # locality (field test No.13)
        pool = []
        for h in [o for o in hits if o.get("similarity") == hits[0].get("similarity")] if hits else []:
            bb = [float(x) for x in h["bbox"]]
            if bb[2] - bb[0] >= 180:
                # Field test No.8: Fiji came back as [-180, -21, 180, -12], the
                # minimum bounding rectangle of a country that crosses the
                # dateline. One geometry fetch gives the real extent
                gr = _client.get(
                    GAZET_URL.rsplit("/", 1)[0] + f"/geometry/{h['id']}",
                    params={"simplify": "true", "source": h.get("source")},
                    timeout=GAZET_TIMEOUT_S * 3,
                )
                gr.raise_for_status()
                bb = _wrap_aware_bbox(gr.json().get("geometry") or {}) or bb
            h = {**h, "bbox": bb}
            name = h.get("name") or query
            pool.append((_candidate(f"{name} ({_where(h)})" if _where(h) else name, bb, "gazet", _gazet_where(h),
                                    str(h.get("subtype") or "place").lower(), None, want), h))
        if pool:
            pool = [p for p in pool if _names_the_ask(p[1].get("name") or "", query)] or pool
            pool = sorted(pool, key=lambda p: -p[0]["score"])[:4]
            top, h = pool[0]
            w, s, e, n = top["bbox"]
            lon_c = (w + (e + 360 if e < w else e)) / 2
            out: dict[str, Any] = {
                "name": top["name"],
                "lat": (s + n) / 2,
                "lon": lon_c - 360 if lon_c > 180 else lon_c,
                "bbox": [w, s, e, n],
                "source": "gazet",
            }
            ambiguous = _settle(out, [c for c, _ in pool])
            # Picking one silently is the geocoder deciding for the caller, so
            # say which and name the rest. Field test No.8: two US counties are
            # both "Harris County (US, county)", so each tie carries where it is.
            # A refusal has looked up each state (_settle), so name that over coordinates
            if len(pool) > 1:
                def label(c: dict[str, Any], o: dict[str, Any]) -> str:
                    if ambiguous:
                        return f"({c['where']}, {c['kind']})" if c.get("kind") else f"({c['where']})"
                    return f"({_where(o)}) {_at(o)}"
                out["geocode_note"] = (
                    f"{len(pool)} places match {query!r} equally well; using {h.get('name')} "
                    + (f"{label(top, h)}" if ambiguous else _at(h))
                    + " (best score), not "
                    + ", ".join(f"{o.get('name')} {label(c, o)}" for c, o in pool[1:])
                    + ". Pass the fuller name if another was meant"
                )
                if ambiguous:
                    out["geocode_note"] += ". ambiguous: pass the fuller name or a bbox"
            return _record_geocode(out)
    except httpx.HTTPError:
        _gazet_skip_until = time.time() + 300  # a slow or dead Space must not tax every geocode
    except Exception:
        pass

    def _nominatim(q: str) -> list:
        return _get_json(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 5},
        )

    def _short(dn: str) -> str:
        parts = [p for p in dn.split(", ") if not p.strip().isdigit()]
        return ", ".join(parts[:1] + parts[-2:]) if len(parts) > 3 else dn

    # descriptive phrases ("the Ashburn data center corridor") fail as-is;
    # retry with capitalized tokens, then with descriptor words stripped
    stop = {"the", "a", "an", "data", "center", "centre", "corridor", "area",
            "region", "zone", "near", "around", "downtown", "greater"}
    attempts = [query]
    caps = re.findall(r"[A-Z][\w'-]*", query)
    if caps and " ".join(caps) != query:
        attempts.append(" ".join(caps))
    kept = [w for w in query.split() if w.lower() not in stop]
    if kept and " ".join(kept) != query:
        attempts.append(" ".join(kept))
    results, matched = [], query
    for q in dict.fromkeys(attempts):
        results = _nominatim(q)
        if results:
            matched = q
            break
    if not results:
        return {"error": f"No geocoding result for {query!r}"}
    want = _feature_word(query)[1]
    pool, seen = [], set()
    for r in results:
        key = (_short(r.get("display_name", "")), _nominatim_kind(r))
        if key in seen:
            continue
        seen.add(key)
        s, n, w, e = (float(x) for x in r["boundingbox"])
        imp = r.get("importance")
        pool.append((_candidate(r.get("display_name", key[0]), [w, s, e, n], "nominatim", key[0], key[1],
                                None if imp is None else round(float(imp), 3), want), r))
    pool = [p for p in pool if _names_the_ask(p[1].get("display_name", ""), matched)] or pool
    pool = sorted(pool, key=lambda p: -p[0]["score"])[:4]
    top, r0 = pool[0]
    out = {
        "name": top["name"],
        "lat": float(r0["lat"]),
        "lon": float(r0["lon"]),
        "bbox": list(top["bbox"]),
        "source": "nominatim",
    }
    # Field test No.8: "the Ashburn data center corridor" simplified to "Ashburn"
    # and Nominatim's first hit was Ashburn, Georgia. The fallback took the top
    # of a list it never showed. Say what was matched and who else shares it
    others = list(dict.fromkeys(_short(r.get("display_name", "")) for r in results))
    others = [o for o in others if o != top["where"]][:3]
    ambiguous = _settle(out, [c for c, _ in pool])
    if matched != query or others or ambiguous:
        out["geocode_note"] = (
            f"matched {matched!r}"
            + (f" after simplifying {query!r}" if matched != query else "")
            + (f"; other places share it: {' / '.join(others)}" if others else "")
            + ". Pass a state or country if another was meant"
            + (". ambiguous: pass the fuller name or a bbox" if ambiguous else "")
        )
    return _record_geocode(out)


def _settle(out: dict[str, Any], cands: list[dict[str, Any]]) -> bool:
    """The tail both sources share: candidates on out, ambiguity, a point widened. Returns ambiguous"""
    out["candidates"] = cands
    top = cands[0]
    ambiguous = any(top["score"] - c["score"] <= AMBIGUITY_MARGIN and not _bboxes_overlap(top["bbox"], c["bbox"])
                    for c in cands[1:])
    if ambiguous:
        out["ambiguous"] = True
        # Field test No.14: the Portland refusal named no state because Gazet
        # answered and its rows carry none
        for c in cands:
            if c["source"] != "nominatim":
                c["where"] = _state_country(c)
    # Nominatim returns a NODE for peaks and landmarks, so the bbox collapses to
    # a point (Mount Rainier came back 0.0001 deg wide). A degenerate bbox makes
    # a useless imagery search and an empty 3D scene, so widen it and say so. A
    # real extent stays however small: the Berkeley Pit is 0.024 by 0.013 deg
    out["extent_basis"] = top["extent_basis"]
    if top["extent_basis"] != "own":
        half_km = _KIND_HALF_KM.get(top["kind"]) if top["extent_basis"] == "kind" else None
        dlat = half_km / 111.32 if half_km else 0.15
        dlon = min(dlat / max(math.cos(math.radians(out["lat"])), 0.01), 180.0) if half_km else 0.15
        out["bbox"] = [out["lon"] - dlon, max(out["lat"] - dlat, -90.0), out["lon"] + dlon, min(out["lat"] + dlat, 90.0)]
        out["bbox_note"] = (
            "geocoder returned a point, not an extent — widened to "
            + (f"{2 * half_km:g} km across for a {top['kind']}" if half_km else "a 0.3 deg box")
            + " around it. Pass your own bbox if the subject is bigger"
        )
    return ambiguous


def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Name the area around a point (for labeling a map-viewport scan)."""
    try:
        r = _get_json(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "zoom": 10, "format": "jsonv2"},
        )
        name = ", ".join((r.get("display_name") or "").split(", ")[:3]) or "Current view"
        return {"name": name}
    except Exception:
        return {"name": "Current view"}


# ---------------------------------------------------------------- catalogs


def list_catalogs() -> list[dict[str, str]]:
    """List the STAC catalogs this server can search, with what each is good for."""
    return [{"catalog": k, **v} for k, v in CATALOGS.items()] + [{
        "catalog": "zarr",
        "notes": "Curated Zarr stores, not STAC: forecast and reanalysis grids (GFS, HRRR, ERA5, MUR SST). "
        "search_datasets finds them, describe_collection('zarr', id) lists variables, compute_statistics "
        "reads a clipped number, tile_url_template draws it where the store renders.",
    }]


def _collections(catalog: str) -> list[dict[str, Any]]:
    cached = _collections_cache.get(catalog)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    base = CATALOGS[catalog]["stac"]
    out: list[dict[str, Any]] = []
    # limit=500 collapses VEDA's default 10-per-page pagination (25 GETs) to one
    url: str | None = f"{base}/collections?limit=500"
    while url and len(out) < 1000:
        data = _get_json(url)
        out.extend(data.get("collections", []))
        url = next(
            (l["href"] for l in data.get("links", []) if l.get("rel") == "next"), None
        )
    _collections_cache[catalog] = (time.time(), out)
    return out


def _collection_gsd(c: dict[str, Any]) -> float | None:
    """Finest ground sample distance (m) a collection declares, or None."""
    vals: list[Any] = []
    summ = c.get("summaries") or {}
    for v in (summ.get("gsd"), summ.get("eo:gsd")):
        vals += v if isinstance(v, list) else [v]
    vals += [a.get("gsd") for a in (c.get("item_assets") or {}).values()]
    nums = []
    for v in vals:
        if isinstance(v, dict):  # a STAC range summary {"minimum", "maximum"}
            v = v.get("minimum")
        if isinstance(v, (int, float)) and v > 0:
            nums.append(float(v))
    return min(nums) if nums else None


def _split_antimeridian(box: list[float]) -> list[list[float]]:
    # Per RFC 7946 §5.2, a bbox with west > east is the antimeridian-crossing form, not an invalid box
    if not box or len(box) < 4:
        return []
    w, s, e, n = box
    if w > e:
        return [
            [w, s, 180.0, n],
            [-180.0, s, e, n]
        ]
    return [box]


def _covers_aoi(c: dict[str, Any], aoi: list[float]) -> bool | str:
    boxes = ((c.get("extent") or {}).get("spatial") or {}).get("bbox") or []
    if not boxes:
        return "unknown"
    try:
        aoi_parts = _split_antimeridian(aoi)
        for b in boxes:
            b_2d = [b[0], b[1], b[2], b[3]] if len(b) == 4 else [b[0], b[1], b[3], b[4]]
            for bw, bs, be, bn in _split_antimeridian(b_2d):
                for pw, ps, pe, pn in aoi_parts:
                    if bw <= pe and be >= pw and bs <= pn and bn >= ps:
                        return True
    except (TypeError, IndexError, ValueError):
        return "unknown"
    return False


def search_datasets(
    keywords: str,
    catalog: str | None = None,
    bbox: list[float] | None = None,
    place: str | None = None,
    max_gsd: float | None = None,
) -> list[dict[str, Any]]:
    """Find dataset collections matching keywords, across all catalogs or one.

    Case-insensitive match against collection id, title, description, and
    keywords. Returns up to 20 hits with {catalog, id, title, summary, gsd}.
    The catalogs are queried concurrently and filtered locally; the pattern
    follows developmentseed/stac-fastapi-collection-discovery, which does the
    same federation as a service.

    Pass `bbox` or `place` to keep collections whose declared spatial extent
    intersects the area (each hit gets `covers_aoi`), and `max_gsd` (metres) to
    keep collections at that resolution or finer (finest first). Read both
    honestly: a collection's extent is its overall envelope, not proof of
    imagery over your area, and `gsd` is what the collection declares. A
    collection with no declared extent (`covers_aoi: "unknown"`) or no declared
    gsd (`gsd: null`) is kept and flagged, because missing metadata is not
    missing coverage or resolution.

    An empty result comes back as one record naming the catalogs `searched`,
    with a `note`: "not in these catalogs" is NOT "no such data exists".
    Himawari is the live example: real, and not here.
    """
    aoi = None
    if bbox or place:
        aoi = _resolve_bbox(place, bbox)
        if isinstance(aoi, dict):
            return [aoi]
    terms = [t.lower() for t in keywords.split() if t]
    cats = [c for c in ([catalog] if catalog else list(CATALOGS)) if c != "zarr"]
    zarr_hits = _zarr_hits(terms) if catalog in (None, "zarr") else []
    if aoi or max_gsd is not None:
        for h in zarr_hits:
            h.setdefault("note", "curated zarr store: place and gsd filters not applied, see its coverage")

    def fetch(cat: str):
        try:
            return cat, _collections(cat)
        except Exception as e:
            return cat, e

    with ThreadPoolExecutor(max_workers=3) as pool:
        fetched = list(pool.map(fetch, cats))
    hits = zarr_hits
    for cat, cols in fetched:
        if isinstance(cols, Exception):
            hits.append({"catalog": cat, "error": str(cols)})
            continue
        for c in cols:
            hay = " ".join(
                [
                    c.get("id", ""),
                    c.get("title") or "",
                    (c.get("description") or "")[:500],
                    " ".join(c.get("keywords") or []),
                ]
            ).lower()
            if not all(t in hay for t in terms):
                continue
            gsd = _collection_gsd(c)
            hit = {
                "catalog": cat,
                "id": c.get("id"),
                "title": c.get("title"),
                "summary": (c.get("description") or "")[:240],
                "gsd": gsd,
            }
            notes = []
            if aoi:
                hit["covers_aoi"] = _covers_aoi(c, aoi)
                if hit["covers_aoi"] is False:
                    continue
                if hit["covers_aoi"] == "unknown":
                    notes.append("no spatial extent declared, may or may not cover the area")
            if max_gsd is not None:
                if gsd is not None and gsd > max_gsd:
                    continue
                if gsd is None:
                    notes.append("no gsd declared, resolution unknown (not necessarily coarse)")
            if notes:
                hit["note"] = "; ".join(notes)
            hits.append(hit)
    if max_gsd is not None:
        hits.sort(key=lambda h: ("error" not in h, "gsd" in h and h["gsd"] is None, h.get("gsd") or 0))
    if not hits:
        # An empty list is the return most likely to be narrated into "no such
        # data exists". Say what was searched in the return the model cannot
        # skip, not only in the docstring it might (docs/adr-caveats.md)
        searched = [catalog] if catalog else [*CATALOGS, "zarr"]
        return [{
            "searched": searched,
            "note": (
                f"nothing matched {keywords!r} in {len(searched)} catalog(s): "
                + ", ".join(searched)
                + ". Absence here does not mean the data does not exist, only that "
                "these catalogs do not carry it under those words"
            ),
        }]
    return hits[:20]


def describe_collection(catalog: str, collection_id: str) -> dict[str, Any]:
    """Get a collection's description, extent, and asset/band layout.

    catalog="zarr" with a store id from search_datasets returns the store:
    its variables and units, the dims to select on, coverage, chunking, and
    whether titiler.xyz can draw it (tiles, and tiles_note when it cannot).
    """
    if catalog == "zarr":
        z = ZARR_STORES.get(collection_id)
        return {"catalog": "zarr", "id": collection_id, **z} if z else {"error": f"unknown zarr store {collection_id!r}; options: {', '.join(ZARR_STORES)}"}
    cached = _collections_cache.get(catalog)
    c = None
    if cached and time.time() - cached[0] < 3600:
        c = next((col for col in cached[1] if col.get("id") == collection_id), None)
    if c is None:
        c = _get_json(f"{CATALOGS[catalog]['stac']}/collections/{collection_id}")
    item_assets = c.get("item_assets") or {}
    return {
        "catalog": catalog,
        "id": c.get("id"),
        "title": c.get("title"),
        "description": (c.get("description") or "")[:1200],
        "extent": c.get("extent"),
        "license": c.get("license"),
        "assets": {
            k: (v.get("title") or v.get("type", "")) for k, v in item_assets.items()
        },
    }


# ---------------------------------------------------------------- zarr stores

# Curated, not discovered: a Zarr store has no search API, so each entry here was
# opened by hand (2026-10-25). tiles=False says why titiler.xyz cannot render it
ZARR_STORES: dict[str, dict[str, Any]] = {
    "noaa-gfs-forecast": {
        "title": "NOAA GFS forecast (dynamical.org)",
        "url": "https://data.dynamical.org/noaa/gfs/forecast/latest.zarr",
        "variables": {
            "temperature_2m": {"description": "air temperature 2 m above ground", "units": "degree_Celsius"},
            "relative_humidity_2m": {"description": "relative humidity 2 m above ground", "units": "percent"},
            "wind_u_10m": {"description": "eastward wind 10 m above ground", "units": "m s-1"},
            "wind_v_10m": {"description": "northward wind 10 m above ground", "units": "m s-1"},
            "total_cloud_cover_atmosphere": {"description": "total cloud cover", "units": "percent"},
        },
        "sel": "valid_time (when the forecast is for, UTC; picks the latest run and its lead time), or init_time and lead_time",
        "time_range_note": "the range is rolling: the store holds the newest run only, up to 384 h ahead, and past dates are not in this store",
        "coverage": "global, 0.25 degrees (~20 km); runs every 6 h, lead 0-384 h (hourly to 120 h, then 3-hourly)",
        "chunks": "1 run x 210 lead steps x 726 x 726 per chunk: one point costs a quarter-globe chunk",
        "stats": False,
        "stats_note": "the data.dynamical.org plain Zarr copy is deprecated (sunset header 2026-10-01) and its last run is 2026-10-25T06Z; the supported copy is Icechunk, the upgrade path",
        "tiles": False,
        "tiles_note": "titiler.xyz cannot open this store: the host sends its metadata compressed without a Content-Length and the tiler's reader (obstore) refuses it",
        "source": "NOAA NWS NCEP GFS, processed by dynamical.org (CC-BY-4.0). The data.dynamical.org Zarr copy is deprecated (sunset 2026-10-01, still served 2026-10-25); the supported copy is Icechunk",
    },
    "noaa-hrrr-forecast": {
        "title": "NOAA HRRR forecast, 2 m temperature (University of Utah)",
        "url": "s3://hrrrzarr/sfc/{date}/{date}_{hh}z_fcst.zarr",
        "group": "2m_above_ground/TMP/2m_above_ground",
        "region": "us-west-1",
        "variables": {"TMP": {"description": "air temperature 2 m above ground", "units": "K"}},
        "sel": "valid_time (UTC); the newest 00/06/12/18z run is used, up to 48 h ahead",
        "time_range_note": "the range is rolling: the store holds the newest run only, up to 48 h ahead, and past dates are not in this store",
        "coverage": "CONUS only, 3 km Lambert conformal grid",
        "chunks": "48 steps x 150 x 150 per chunk",
        "stats": True,
        "tiles": False,
        "tiles_note": "titiler.xyz opens the group but fails on its projected x/y coordinates, which are not stored in the group",
        "source": "NOAA HRRR via the University of Utah Open Source Weather archive (AWS Open Data hrrrzarr)",
    },
    "nasa-mur-sst": {
        "title": "NASA MUR sea surface temperature, 1 km daily",
        "url": "s3://mur-sst/zarr-v1",
        "region": "us-west-2",
        "variables": {"analysed_sst": {"description": "analysed sea surface temperature", "units": "kelvin"}},
        "sel": "time (daily, 2002-06-01 to 2020-01-20)",
        "coverage": "global ocean, 0.01 degrees",
        "chunks": "5 days x 1799 x 3600 per chunk, about 123 MB: one cell costs a whole chunk",
        "stats": False,
        "stats_note": "the record ends 2020-01-20, and one cell costs a 123 MB chunk read, so statistics are not offered",
        "time_dim": "time",
        "time_range": ["2002-06-01", "2020-01-20"],
        "tiles": False,
        "tiles_note": "titiler.xyz does not reach this bucket, and a 123 MB chunk makes a tile read slow",
        "source": "NASA JPL MUR SST on AWS Open Data (metadata only here)",
    },
    "weatherbench2-era5": {
        "title": "ERA5 reanalysis, 1959 to 2023-01-10, 6-hourly, 1.5 degrees (WeatherBench 2)",
        "url": "https://storage.googleapis.com/weatherbench2/datasets/era5/1959-2023_01_10-6h-240x121_equiangular_with_poles_conservative.zarr",
        "variables": {
            "2m_temperature": {"description": "air temperature 2 m above ground", "units": "K", "rescale": "220,320", "colormap_name": "inferno"},
            "2m_dewpoint_temperature": {"description": "dewpoint temperature 2 m above ground", "units": "K", "rescale": "220,310", "colormap_name": "inferno"},
            "10m_wind_speed": {"description": "wind speed 10 m above ground", "units": "m s-1", "rescale": "0,25", "colormap_name": "viridis"},
            "total_precipitation_6hr": {"description": "total precipitation over the 6 h ending at the step", "units": "m", "rescale": "0,0.02", "colormap_name": "blues"},
            "mean_sea_level_pressure": {"description": "air pressure reduced to mean sea level", "units": "Pa", "rescale": "95000,105000", "colormap_name": "viridis"},
            "sea_surface_temperature": {"description": "sea surface temperature (NaN over land)", "units": "K", "rescale": "270,305", "colormap_name": "inferno"},
        },
        "variables_note": "the store holds 62 variables; compute_statistics accepts any of them by its store name, and the ones listed here are the ones groundstation has checked and can draw",
        "sel": "time (UTC, 6-hourly, ends 2023-01-10): history, not a forecast",
        "coverage": "global, 1.5 degrees (~165 km), longitudes 0 to 360",
        "chunks": "small time-sliced chunks",
        "stats": True,
        "tiles": True,
        "time_dim": "time",
        "time_range": ["1959-01-01T00:00", "2023-01-10T18:00"],
        "source": "ECMWF ERA5 regridded by Google Research WeatherBench 2 (CC-BY-4.0)",
    },
}
_ZARR_X = ("longitude", "lon", "x")
_ZARR_Y = ("latitude", "lat", "y")
# ponytail: HRRR's Lambert CRS is written out because the Utah store stores x/y
# in metres but no CRS; a store with a spatial_ref would drop this
_HRRR_CRS = "+proj=lcc +lat_0=38.5 +lon_0=-97.5 +lat_1=38.5 +lat_2=38.5 +R=6371229 +units=m +no_defs"


def _zarr_hits(terms: list[str]) -> list[dict[str, Any]]:
    out = []
    for sid, z in ZARR_STORES.items():
        vars_ = " ".join(f"{k} {v['description']}" for k, v in z["variables"].items())
        hay = f"{sid} {z['title']} {vars_} {z['coverage']} {z['source']} zarr".lower()
        if all(t in hay for t in terms):
            out.append({"catalog": "zarr", "id": sid, "title": z["title"], "summary": f"{z['coverage']}. Variables: {', '.join(z['variables'])}"})
    return out


def _http_zarr_store(base: str):
    # ponytail: no listing, and one client that is never closed; a Range answered
    # with 206 is required, so a server ignoring Range errors instead of returning wrong bytes.
    # Upgrade: obstore's HTTP store once it accepts a missing Content-Length, or Icechunk
    # obstore's HTTPStore needs a Content-Length, and Cloudflare-fronted hosts
    # send compressed JSON without one (data.dynamical.org), so over https read with httpx
    import httpx
    from zarr.abc.store import ByteRequest, OffsetByteRequest, RangeByteRequest, Store, SuffixByteRequest

    class HttpStore(Store):
        supports_writes = supports_deletes = supports_listing = False

        def __init__(self) -> None:
            super().__init__(read_only=True)
            self._c = httpx.AsyncClient(timeout=60, follow_redirects=True, headers=UA)

        def __eq__(self, o: object) -> bool:
            return isinstance(o, HttpStore) and o is self

        async def get(self, key: str, prototype: Any, byte_range: ByteRequest | None = None) -> Any:
            h = {}
            if isinstance(byte_range, RangeByteRequest):
                h["Range"] = f"bytes={byte_range.start}-{byte_range.end - 1}"
            elif isinstance(byte_range, OffsetByteRequest):
                h["Range"] = f"bytes={byte_range.offset}-"
            elif isinstance(byte_range, SuffixByteRequest):
                h["Range"] = f"bytes=-{byte_range.suffix}"
            r = await self._c.get(f"{base.rstrip('/')}/{key}", headers=h)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            if h and r.status_code != 206:
                raise RuntimeError(f"{base} ignored a Range request (status {r.status_code})")
            return prototype.buffer.from_bytes(r.content)

        async def get_partial_values(self, prototype: Any, key_ranges: Any) -> list:
            return [await self.get(k, prototype, r) for k, r in key_ranges]

        async def exists(self, key: str) -> bool:
            return (await self._c.head(f"{base.rstrip('/')}/{key}")).status_code == 200

        async def set(self, *a: Any, **k: Any) -> None:
            raise NotImplementedError

        async def delete(self, *a: Any, **k: Any) -> None:
            raise NotImplementedError

        async def set_if_not_exists(self, *a: Any, **k: Any) -> None:
            raise NotImplementedError

        async def list(self):
            return
            yield

        list_prefix = list_dir = lambda self, prefix: self.list()

    return HttpStore()


def _s3_zarr_store(url: str, region: str):
    from obstore.store import S3Store
    from zarr.storage import ObjectStore

    bucket, _, prefix = url.removeprefix("s3://").partition("/")
    return ObjectStore(S3Store(bucket, region=region, skip_signature=True, prefix=prefix), read_only=True)


def _open_zarr(store_id: str):
    """Open a curated store as an xarray Dataset (lazy). HRRR gets its time and x/y from the parent group."""
    import xarray as xr

    z = ZARR_STORES[store_id]
    url = z["url"]
    if "{date}" in url:
        from obstore.store import S3Store
        import obstore

        now = dt.datetime.now(dt.timezone.utc)
        for back in range(0, 48, 6):
            run = (now - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
            run = run.replace(hour=run.hour // 6 * 6)
            u = url.format(date=run.strftime("%Y%m%d"), hh=f"{run.hour:02d}")
            bucket, _, prefix = u.removeprefix("s3://").partition("/")
            try:
                obstore.head(S3Store(bucket, region=z["region"], skip_signature=True, prefix=prefix), f"{z['group']}/TMP/.zarray")
                break
            except Exception:
                continue
        else:
            raise RuntimeError("no HRRR run found in the last 48 h")
        st = _s3_zarr_store(u, z["region"])
        ds = xr.open_zarr(st, group=z["group"], consolidated=False, zarr_format=2, chunks=None)
        # the group one level up carries the real valid times and x/y; the variable's own group has none
        up = xr.open_zarr(st, group=z["group"].rsplit("/", 1)[0], consolidated=False, zarr_format=2, chunks=None)
        return ds.rename(projection_y_coordinate="y", projection_x_coordinate="x").assign_coords(
            x=up["projection_x_coordinate"].values, y=up["projection_y_coordinate"].values, time=up["time"].values
        )
    store = _s3_zarr_store(url, z["region"]) if url.startswith("s3://") else _http_zarr_store(url)
    return xr.open_zarr(store, consolidated=True, chunks=None)


def _zarr_select(da: Any, sel: dict[str, str] | None) -> tuple[Any, dict[str, str], list[str]]:
    """Pick one 2-D slice: every non-spatial dim resolved by nearest match, and say what snapped."""
    import numpy as np
    import pandas as pd

    sel = dict(sel or {})
    used: dict[str, str] = {}
    notes: list[str] = []
    kinds = {"M": pd.Timestamp, "m": pd.Timedelta}
    label = lambda v: str(kinds.get(v.dtype.kind, lambda x: x)(v))  # noqa: E731
    valid = sel.pop("valid_time", None)
    if valid and "time" in da.dims:
        sel["time"] = valid
    for dim in [d for d in da.dims if d not in _ZARR_X + _ZARR_Y]:
        want = sel.get(dim)
        if dim == "lead_time" and valid:
            want = pd.Timestamp(valid) - pd.Timestamp(used["init_time"])
        coord = da[dim].values
        if want is None or want == "latest":
            idx = len(coord) - 1 if dim in ("init_time", "time") else 0
        else:
            if coord.dtype.kind == "M":
                t = pd.Timestamp(want)
                target = (t.tz_convert(None) if t.tzinfo else t).to_datetime64()
            elif coord.dtype.kind == "m":
                target = (want if isinstance(want, pd.Timedelta) else pd.Timedelta(hours=float(want))).to_timedelta64()
            else:
                target = float(want)
            idx = int(np.abs(coord - target).argmin())
            if coord[idx] != target:
                gap = pd.Timedelta(abs(coord[idx] - target)) if coord.dtype.kind in "Mm" else abs(coord[idx] - target)
                if coord.dtype.kind in "Mm" and len(coord) > 1 and gap > pd.Timedelta(coord[1] - coord[0]):
                    raise ValueError(
                        f"{dim} {want} is {gap} from the nearest {dim} in this store ({label(coord[idx])}), more than one step ({pd.Timedelta(coord[1] - coord[0])}); "
                        f"the store holds {dim} {label(coord[0])} to {label(coord[-1])}"
                    )
                edge = " (outside the store's range)" if idx in (0, len(coord) - 1) and (target < coord[0] or target > coord[-1]) else ""
                notes.append(f"{dim} {want} snapped to {label(coord[idx])}, off by {gap}{edge}")
        da = da.isel({dim: idx})
        used[dim] = label(coord[idx])
    if valid and "init_time" in used:
        used["valid_time"] = str((pd.Timestamp(used["init_time"]) + pd.Timedelta(used["lead_time"])))
    return da, used, notes


def _clip_reduce(da: Any, bbox: list[float], crs: str | None = None) -> dict[str, Any]:
    """Clip a 2-D DataArray to bbox [w, s, e, n] (degrees) and reduce it to the COG-path stats."""
    import numpy as np

    xn = next(d for d in da.dims if d in _ZARR_X)
    yn = next(d for d in da.dims if d in _ZARR_Y)
    w, s, e, n = bbox
    if crs:
        from rasterio.warp import transform_bounds

        w, s, e, n = transform_bounds("EPSG:4326", crs, w, s, e, n)
    elif float(da[xn].max()) > 180:
        w, e = w % 360, e % 360
    xs, ys = da[xn].values, da[yn].values
    cell = abs(float(xs[1] - xs[0]))
    outside = {"error": "bbox outside the store's coverage"}
    # ponytail: a bbox straddling the prime meridian on a 0..360 grid is refused, not split into 2 reads
    if w > e or (w + e) / 2 < xs.min() - cell or (w + e) / 2 > xs.max() + cell or (s + n) / 2 < ys.min() - cell or (s + n) / 2 > ys.max() + cell:
        return outside
    sub = da.sel({xn: slice(w, e), yn: slice(s, n) if ys[0] < ys[-1] else slice(n, s)})
    caveats = []
    if sub.size == 0:
        sub = da.sel({xn: (w + e) / 2, yn: (s + n) / 2}, method="nearest")
        caveats.append("bbox is smaller than one grid cell: the value is the nearest cell, not an area statistic")
    v = np.atleast_1d(sub.values).astype(float).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"error": "no valid values in the bbox for this selection (outside the grid, or the model run is not written yet)"}
    if v.size <= 4:
        caveats.append(f"coarse grid: only {v.size} cell(s) (cell size {cell:g}{'' if crs else ' deg'}) fall in the bbox, so min/max/mean are close to one model value, not a local average")
    p2, p98 = np.percentile(v, [2, 98])
    return {
        "stats": {
            "min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "median": float(np.median(v)),
            "std": float(v.std()), "percentile_2": float(p2), "percentile_98": float(p98), "count": int(v.size),
        },
        "caveats": caveats,
    }


def _zarr_time_refusal(store_id: str, z: dict[str, Any], sel: dict[str, str] | None) -> str | None:
    """Words for an asked time outside the store's fixed time_range, else None."""
    lo, hi = z.get("time_range") or (None, None)
    asked = (sel or {}).get(z.get("time_dim", "time")) or (sel or {}).get("valid_time")
    if not (asked and lo):
        return None
    try:
        t = dt.datetime.fromisoformat(asked.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None
    if dt.datetime.fromisoformat(lo) <= t <= dt.datetime.fromisoformat(hi):
        return None
    return f"time {asked} is outside {store_id}'s range ({lo} to {hi})"


def _zarr_statistics(store_id: str, variable: str, bbox: list[float] | None, sel: dict[str, str] | None) -> dict[str, Any]:
    z = ZARR_STORES.get(store_id)
    if not z:
        return {"error": f"unknown zarr store {store_id!r}; options: {', '.join(ZARR_STORES)}"}
    if refusal := _zarr_time_refusal(store_id, z, sel):
        return {"error": f"{refusal}; no statistics returned"}
    if not z["stats"]:
        return {"error": f"statistics are not offered for {z['title']}: {z.get('stats_note') or z['chunks']}"}
    if not bbox:
        return {"error": "bbox [w, s, e, n] is required: a Zarr store is a whole grid, so unclipped statistics would describe the globe or the continent"}
    ds = _open_zarr(store_id)
    if variable in z["variables"]:
        meta = z["variables"][variable]
    elif variable in ds.data_vars:
        a = ds[variable].attrs
        meta = {"description": a.get("long_name") or variable, "units": a.get("units") or "unknown"}
    else:
        return {"error": f"unknown variable {variable!r}; curated options: {', '.join(z['variables'])}; the store holds {len(ds.data_vars)} variables"}
    try:
        da, used, notes = _zarr_select(ds[variable], sel)
    except ValueError as e:
        return {"error": "; ".join(x for x in (f"{z['title']}: {e}", z.get("time_range_note"), "no statistics returned") if x)}
    r = _clip_reduce(da, bbox, _HRRR_CRS if "{date}" in z["url"] else None)
    if r.get("error", "").startswith("bbox outside"):
        return {"error": f"bbox outside {z['title']}'s coverage ({z['coverage']})"}
    if "error" in r:
        return r | {"sel": used}
    return {
        variable: r["stats"],
        "clipped_to": bbox,
        "dataset": f"{z['title']}: {variable} ({meta['description']}), {meta['units']}",
        "units": meta["units"],
        "sel": used,
        "caveats": notes + r["caveats"],
    }


# ---------------------------------------------------------------- item search


def _no_cog_note(item: dict[str, Any]) -> str | None:
    # Earth Search issue #79: a backfill in August 2026 added 263,066
    # sentinel-2-l2a items whose assets are all JPEG2000, with no COG behind
    # them. The tiler reads their headers and then fails on the pixels (500).
    # Not a JP2 rule: sentinel-2-l1c is JP2 by design and reads fine.
    # ponytail: keyed to this one collection, delete when Element 84 repairs them
    if item.get("collection") != "sentinel-2-l2a":
        return None
    types = [a.get("type") or "" for a in item.get("assets", {}).values()]
    if any(t.startswith("image/tiff") for t in types) or not any(t.startswith("image/jp2") for t in types):
        return None
    return (
        "this scene has no cloud-optimized copy in the catalog, so its pixels cannot be read: "
        "previews, statistics and map layers fail for it. Use another scene"
    )


def _compact_item(catalog: str, item: dict[str, Any]) -> dict[str, Any]:
    props = item.get("properties", {})
    read_note = _no_cog_note(item)
    self_url = next(
        (l["href"] for l in item.get("links", []) if l.get("rel") == "self"), None
    )
    return {
        "catalog": catalog,
        "collection": item.get("collection"),
        "id": item.get("id"),
        "datetime": props.get("datetime") or props.get("start_datetime"),
        "cloud_cover": props.get("eo:cloud_cover"),
        "bbox": item.get("bbox"),
        **({"footprint": item["geometry"]} if (item.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon") else {}),
        "assets": sorted(item.get("assets", {}).keys()),
        "self_url": self_url,
        # SAR backscatter is only comparable between passes of the same
        # geometry. The catalog knows the orbit; don't drop it and leave a
        # change-detection answer resting on two mismatched passes
        **({"orbit_state": props["sat:orbit_state"]} if props.get("sat:orbit_state") else {}),
        **({"read_note": read_note} if read_note else {}),
    }




def _bbox_coverage_pct(aoi: list[float], item_bbox: list[float] | None) -> float | None:
    # the fallback for an item with no geometry: a tilted footprint covers less
    # than its bbox suggests, which is why search_imagery prefers the footprint
    if not item_bbox or len(item_bbox) < 4:
        return None
    aoi_parts = _split_antimeridian(aoi)
    aoi_area = sum((part[2] - part[0]) * (part[3] - part[1]) for part in aoi_parts)
    if aoi_area <= 0:
        return None
    item_parts = _split_antimeridian(item_bbox)
    total_overlap = 0.0
    for ap in aoi_parts:
        for ip in item_parts:
            w = max(ap[0], ip[0])
            s = max(ap[1], ip[1])
            e = min(ap[2], ip[2])
            n = min(ap[3], ip[3])
            if e > w and n > s:
                total_overlap += (e - w) * (n - s)
    return round(100.0 * total_overlap / aoi_area, 1)


# A ranked list has already made a decision for the model: newest first, the top
# item may clip the AOI, and sorted on cloud the cleanest scene may see a fifth
# of it (field test No.7, Chilika). `recommended` names the scene that balances
# both axes and says why, so a plausible-but-wrong pick takes deliberate effort
RECOMMEND_MAX_CLOUD = 30.0  # above this a scene is only recommended when nothing cleaner exists
# Field test No.8: a strict max on coverage took a 49.7% scene at 19% cloud over a
# 48.9% scene at 0.6%, and a strict min on cloud took a 25-day-older scene for 0.4
# points. Within these bands the axes are a tie; a recency band then keeps the
# scenes that are one look, and _scene_key (newest on a dead tie) picks among them
RECOMMEND_COVERAGE_TIE = 5.0
RECOMMEND_CLOUD_TIE = 5.0
RECOMMEND_RECENCY_DAYS = 7  # scenes this close to the newest near-equal one are the same look; _scene_key picks among them


def _recommend(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The scene to start from, with the reason in one speakable sentence."""
    if not items:
        return None

    def cloud(it: dict[str, Any]) -> float | None:
        return it.get("cloud_cover")

    def cov(it: dict[str, Any]) -> float:
        return it.get("covers_aoi_pct") or 0.0

    usable = [it for it in items if cloud(it) is None or cloud(it) <= RECOMMEND_MAX_CLOUD]
    all_cloudy = not usable
    if all_cloudy:
        usable = items
    top = max(cov(it) for it in usable)
    near = [it for it in usable if cov(it) >= top - RECOMMEND_COVERAGE_TIE]
    least = min((cloud(it) for it in near if cloud(it) is not None), default=None)
    if least is not None:
        near = [it for it in near if cloud(it) is None or cloud(it) <= least + RECOMMEND_CLOUD_TIE]
    def day(it: dict[str, Any]) -> dt.date | None:
        try:
            return dt.date.fromisoformat((it.get("datetime") or "")[:10])
        except ValueError:
            return None

    days = [d for d in map(day, near) if d]
    band = [it for it in near if not days or (day(it) or dt.date.min) >= max(days) - dt.timedelta(days=RECOMMEND_RECENCY_DAYS)]
    band.sort(key=lambda it: it.get("datetime") or "", reverse=True)
    # newest first, and min keeps the first of equal keys, so a dead tie (an archive with no cloud
    # figures) goes to the newest instead of falling to the id as text
    best = min(band, key=lambda it: _scene_key(it)[:2])
    is_newest = not days or day(best) == max(days)
    pick = (
        f"newest of {len(near)} near-equal scenes" if is_newest
        else f"best on coverage, then cloud, of {len(band)} near-equal scenes within {RECOMMEND_RECENCY_DAYS} days of the newest"
    )
    with_cloud = [it for it in items if cloud(it) is not None]
    clearest = min(with_cloud, key=lambda it: cloud(it) or 0.0) if with_cloud else None
    bc = cloud(best)
    if clearest is None:
        reason = f"best-covering scene at {cov(best):.1f}% of the area; this collection reports no cloud cover"
    elif clearest["id"] == best["id"]:
        reason = f"clearest ({bc:.1f}% cloud) and best-covering ({cov(best):.1f}% of the area), not a tradeoff"
    elif cov(clearest) >= cov(best) - RECOMMEND_COVERAGE_TIE and len(near) > 1:
        # the clearest scene is inside both tie bands, so freshness decided
        reason = (
            f"{'no cloud figure' if bc is None else f'{bc:.1f}% cloud'}, covers {cov(best):.1f}% of the area, and {pick}; "
            f"the clearest ({cloud(clearest):.1f}% cloud) is from {(clearest.get('datetime') or '')[:10]}"
        )
    else:
        own = f"{bc:.1f}% cloud" if bc is not None else "no cloud figure"
        reason = (
            f"{own} but covers {cov(best):.1f}% of the area; "
            f"the clearest scene ({cloud(clearest):.1f}% cloud) covers {cov(clearest):.1f}%"
        )
    if all_cloudy:
        reason = f"every scene is over {RECOMMEND_MAX_CLOUD:.0f}% cloud, so this is the least bad: " + reason
    return {"id": best["id"], "covers_aoi_pct": cov(best), "cloud_cover": bc, "reason": reason}


def _is_archive(catalog: str) -> bool:
    return catalog not in CATALOGS and re.split(r"[?#]", catalog)[0].lower().endswith((".parquet", ".geoparquet"))


def _search_archive(href: str, body: dict[str, Any], max_cloud_cover: float | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # rustac's search is async-first; search_sync works with or without a running
    # event loop (checked inside asyncio.run), and its `query` is not implemented,
    # so cloud cover is filtered here
    import rustac

    kw = {k: body[k] for k in ("bbox", "datetime") if body.get(k)}
    if body.get("collections"):
        kw["collections"] = body["collections"]
    # ponytail: loads every match then sorts and slices in Python, and scans the
    # whole archive for its time range; fine to ~1e5 items, then push sortby and
    # a max_items into rustac's duckdb search
    # rustac reads west > east as a plain box, so an antimeridian AOI is two searches
    feats, seen = [], set()
    for box in _split_antimeridian(kw["bbox"]) if "bbox" in kw else [None]:
        for f in rustac.search_sync(href, **{**kw, **({"bbox": box} if box else {})}):
            if f["id"] not in seen:
                seen.add(f["id"])
                feats.append(f)
    for f in feats:  # rustac writes +00:00 where the API says Z; keep one shape
        if f["properties"].get("datetime"):
            f["properties"]["datetime"] = f["properties"]["datetime"].replace("+00:00", "Z")
    if max_cloud_cover is not None:
        # the API's query lte drops items with no cloud value; match it
        feats = [f for f in feats if f["properties"].get("eo:cloud_cover") is not None and f["properties"]["eo:cloud_cover"] <= max_cloud_cover]
    feats.sort(key=lambda f: f["properties"].get("datetime") or "", reverse=True)
    everything = rustac.search_sync(href)
    stamps = sorted(f["properties"]["datetime"].replace("+00:00", "Z") for f in everything if f["properties"].get("datetime"))
    self_url = next((l["href"] for l in (everything[0].get("links") or []) if l.get("rel") == "self"), "") if everything else ""
    source = next((k for k, v in CATALOGS.items() if self_url.startswith(v["stac"])), None)
    path = Path(href)
    return feats[: body["limit"]], {
        "href": href,
        "written": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).date().isoformat() if path.exists() else None,
        "datetime_range": [stamps[0], stamps[-1]] if stamps else None,
        "note": "a snapshot of the catalog as of the written date, not the live catalog: newer scenes may exist. "
        "Analysis of these items (preview_item, compute_statistics, render_map) goes through the live catalog: "
        f"call them with catalog '{source or 'the catalog it was written from'}' and the item id.",
    }


def write_archive(
    catalog: str,
    collections: list[str],
    bbox: list[float] | None = None,
    place: str | None = None,
    datetime_range: str | None = None,
    out_path: str | None = None,
    limit: int = 500,
    max_cloud_cover: float | None = None,
) -> dict[str, Any]:
    """Cache a live STAC search as a stac-geoparquet archive.

    Pages the catalog's search (up to `limit` items) and writes the items with
    rustac. Pass the returned path as `catalog` to search_imagery to search the
    snapshot with the network off. max_cloud_cover filters the search the way
    search_imagery's does. Returns path, count, datetime_range, written (UTC date).
    """
    import rustac

    if out_path and not out_path.lower().endswith((".parquet", ".geoparquet")):
        return {"error": "out_path must end in .parquet or .geoparquet"}
    where = place
    bbox = _resolve_bbox(place, bbox)
    if isinstance(bbox, dict):
        return bbox
    if not out_path:
        out_dir = Path(os.environ.get("GROUNDSTATION_OUT", Path.cwd() / "demo"))
        slug = slugify("-".join([*collections, where or "-".join(f"{x:.2f}" for x in bbox)]))
        out_path = str(out_dir / "archives" / f"{slug}.parquet")
    body: dict[str, Any] = {"collections": collections, "bbox": bbox, "limit": min(limit, 100)}
    if datetime_range:
        body["datetime"] = _rfc3339_window(datetime_range)
    if max_cloud_cover is not None:
        body["query"] = {"eo:cloud_cover": {"lte": max_cloud_cover}}
    url = f"{CATALOGS[catalog]['stac']}/search"
    feats: list[dict[str, Any]] = []
    method = "POST"
    while url and len(feats) < limit:
        r = _client.get(url) if method == "GET" else _client.post(url, json=body)
        r.raise_for_status()
        page = r.json()
        feats += page.get("features", [])
        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if nxt and page.get("features"):
            url, method = urljoin(url, nxt["href"]), (nxt.get("method") or "GET").upper()
            body = {**body, **(nxt.get("body") or {})} if nxt.get("merge") else nxt.get("body", body)
        else:
            url = None
    feats = feats[:limit]
    if not feats:
        return {"error": "the search returned no items, so no archive was written"}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    rustac.write_sync(out_path, feats)
    stamps = sorted(f["properties"]["datetime"] for f in feats if f["properties"].get("datetime"))
    return {
        "path": out_path,
        "count": len(feats),
        "datetime_range": [stamps[0], stamps[-1]] if stamps else None,
        "written": dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }


def search_imagery(
    catalog: str,
    collections: list[str],
    bbox: list[float] | None = None,
    place: str | None = None,
    datetime_range: str | None = None,
    max_cloud_cover: float | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search a STAC catalog for imagery/data items.

    Provide either bbox [w, s, e, n] or a place name (geocoded for you).
    datetime_range is RFC3339, e.g. "2026-06-01T00:00:00Z/2026-07-09T00:00:00Z".
    Returns compact items sorted newest first; feed self_url/ids into
    preview_item, compute_statistics, or render_map. Each item carries
    covers_aoi_pct — how much of the searched area its footprint covers
    (coverage_basis says footprint, or bbox when the item has no geometry; 100 =
    full coverage; a low value means the scene only clips the area, so say
    so or pick a fuller scene). When no single scene covers the AOI, the
    response also carries full_coverage_set: the newest same-day scene set
    whose union does — render its items as toggleable layers in one
    render_map call instead of answering with a cropped scene. `recommended`
    names the scene to start from and why: coverage and cloud pull in
    opposite directions, and sorting on one of them alone picks wrong.
    An item carrying read_note cannot be read by the tiler; it is listed so
    the gap is visible, and never recommended or put in a coverage set.
    catalog may also be a stac-geoparquet archive (a path or URL ending in
    .parquet, e.g. one made by write_archive): it is searched with the same
    filters, offline for a local file, and the result carries an `archive`
    block (href, written date, time range) because it is a snapshot.
    """
    bbox = _resolve_bbox(place, bbox)
    if isinstance(bbox, dict):
        return bbox
    body: dict[str, Any] = {
        "collections": collections,
        "bbox": bbox,
        "limit": min(limit, 50),
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    if datetime_range:
        body["datetime"] = _rfc3339_window(datetime_range)
    if max_cloud_cover is not None:
        body["query"] = {"eo:cloud_cover": {"lte": max_cloud_cover}}
    archive = None
    if _is_archive(catalog):
        feats, archive = _search_archive(catalog, body, max_cloud_cover)
    else:
        base = CATALOGS[catalog]["stac"]
        r = _client.post(f"{base}/search", json=body)
        if r.status_code == 400 and "sortby" in body:
            body.pop("sortby")  # some APIs reject sortby; retry without
            r = _client.post(f"{base}/search", json=body)
        r.raise_for_status()
        feats = r.json().get("features", [])
    items = [_compact_item(catalog, f) for f in feats]
    for it in items:
        if it.get("footprint"):
            it["covers_aoi_pct"] = _union_coverage_pct(bbox, [it["footprint"]])
            it["coverage_basis"] = "footprint"
        else:
            it["covers_aoi_pct"] = _bbox_coverage_pct(bbox, it.get("bbox"))
            it["coverage_basis"] = "bbox"
    result = {"bbox": bbox, "count": len(feats), "items": items}
    if not feats and max_cloud_cover is not None:
        result["note"] = (f"this window returned 0 scenes at max_cloud_cover {max_cloud_cover:g}; "
                          "a higher cap or a wider window may find some")
    if archive:
        result["archive"] = archive
    readable = [it for it in items if "read_note" not in it]
    rec = _recommend(readable)
    if rec:
        result["recommended"] = rec
    best_single = max((it.get("covers_aoi_pct") or 0.0) for it in readable) if readable else 0.0
    if readable and best_single < FULL_COVERAGE_PCT:
        full = find_full_coverage_set(readable, bbox)
        if full and len(full["items"]) > 1:
            result["full_coverage_set"] = full
            # no single scene covers the box, so the set is the answer: name its
            # scenes, not a lone one that only clips it
            ids = [it["id"] for it in full["items"]]
            result["recommended"] = {
                "id": ids[0], "scene_ids": ids, "covers_aoi_pct": full["union_covers_aoi_pct"],
                "cloud_cover": full["items"][0].get("cloud_cover"),
                "reason": f"no single scene covers this area; these {len(ids)} from {full['date']} together cover "
                          f"{full['union_covers_aoi_pct']:.1f}% of it (full_coverage_set). "
                          f"The best single scene covers {best_single:.1f}%",
            }
        elif rec:
            # Field test No.8: over a county or a province the best single scene
            # is a fraction of the area, and the same-day set that would cover it
            # is starved by `limit` and by the cloud filter. Say so in the
            # recommendation, or a 49% scene gets rendered as the answer
            rec["reason"] += _starved_note(len(items))
    return result


def _starved_note(n: int) -> str:
    return (f". No single scene covers this area and no same-day set does either within these {n} results; "
            "raise limit or relax max_cloud_cover to find one")


# ---------------------------------------------------------------- rasters


def _expression_idents(expression: str) -> list[str]:
    funcs = {"where", "abs", "sqrt", "min", "max", "log", "exp", "sin", "cos", "b"}
    idents = [
        t
        for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expression)
        if t not in funcs and not re.fullmatch(r"b\d+", t)
    ]
    return list(dict.fromkeys(idents))  # order of first appearance


def _expression_to_bands(expression: str, assets: list[str] | None) -> tuple[str, list[str]]:
    # titiler.xyz /stac merges the assets list into bands b1..bN, so a friendly
    # expression like (nir-red)/(nir+red) must become (b1-b2)/(b1+b2) with
    # assets=[nir, red] in matching order.
    if not assets:
        assets = _expression_idents(expression)
    for i, a in enumerate(assets, start=1):
        expression = re.sub(rf"\b{re.escape(a)}\b", f"b{i}", expression)
    return expression, assets


def _default_assets(collection: str) -> list[str]:
    if "sentinel-2" in collection:
        return ["visual"]
    if collection.startswith("landsat"):
        return ["red", "green", "blue"]
    return []


def preview_item(
    catalog: str,
    collection_id: str,
    item_id: str,
    assets: list[str] | None = None,
    rescale: str | None = None,
    max_size: int = 512,
    colormap_name: str | None = None,
    expression: str | None = None,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """Get a browser-openable PNG preview URL for a STAC item.

    Routes to the right raster backend per catalog: titiler.xyz for
    Earth Search, NASA VEDA's raster API, or Planetary Computer's signing
    data API. Optional rescale like "0,3000" for non-visual assets, and
    expression + colormap_name for index previews (NDVI etc., same shapes
    tile_url_template takes).

    bbox [w, s, e, n] crops the preview to that area (the tiler's part
    endpoint) instead of rendering the whole scene — pass the AOI so the
    subject fills the frame and scene-edge nodata stays out. On Planetary
    Computer, passing assets or expression (with any of rescale,
    colormap_name, bbox) renders through the data API's item endpoints; with
    none of them you get the collection's pre-rendered whole-scene preview.
    When that fallback cannot honor something you passed, the result lists
    it under `dropped`.
    """
    asked = [k for k, v in (("assets", assets), ("expression", expression), ("rescale", rescale),
                            ("colormap_name", colormap_name), ("bbox", bbox)) if v]
    part = ",".join(str(round(v, 6)) for v in bbox) if bbox else ""
    assets = assets or ([] if expression else _default_assets(collection_id))
    if expression and not rescale:
        rescale = "-1,1"  # normalized-difference indices are unreadable unscaled
    backend = CATALOGS[catalog]["raster"]
    if backend == "pc":
        # the stored rendered_preview is whole-scene and takes no parameters;
        # asked-for ones go to the data API's item endpoints instead
        if asked and (assets or expression):
            if expression and not assets:
                assets = _expression_idents(expression)
            params = [("collection", collection_id), ("item", item_id)] + [("assets", a) for a in assets]
            if expression:
                params += [("expression", expression), ("asset_as_band", "true")]
            if rescale:
                params.append(("rescale", rescale))
            if colormap_name:
                params.append(("colormap_name", colormap_name))
            params.append(("max_size", str(max_size)))
            if part:
                params.append(("dst_crs", "epsg:3857"))  # same mercator crop as the other backends
            path = f"bbox/{part}.png" if part else "preview.png"
            return {
                "preview_url": "https://planetarycomputer.microsoft.com/api/data/v1/item/"
                + f"{path}?{httpx.QueryParams(params)}",
                "backend": "pc-data-api",
            }
        item = _get_json(
            f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
        )
        rp = item.get("assets", {}).get("rendered_preview", {}).get("href")
        if rp:
            out = {"preview_url": rp, "backend": "pc-data-api"}
            if asked:
                out["dropped"] = [
                    {"param": k, "why": "no assets known for this collection, so the pre-rendered "
                     "whole-scene preview was used (pass assets to apply it)"}
                    for k in asked
                ]
            return out
        return {"error": "No rendered_preview asset on this Planetary Computer item"}
    if backend == "veda":
        if expression and not assets:
            assets = _expression_idents(expression)
        params = [("assets", a) for a in assets or ["cog_default"]]
        if expression:
            params += [("expression", expression), ("asset_as_band", "true")]
        if rescale:
            params.append(("rescale", rescale))
        if colormap_name:
            params.append(("colormap_name", colormap_name))
        params.append(("max_size", str(max_size)))
        q = str(httpx.QueryParams(params))
        path = f"bbox/{part}.png" if part else "preview.png"
        return {
            "preview_url": f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}/{path}?{q}",
            "backend": "veda-raster-api",
        }
    # earth-search and any catalog with public item URLs -> titiler.xyz
    self_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    params = [("url", self_url)]
    if expression:
        expression, assets = _expression_to_bands(expression, assets or None)
        params.append(("expression", expression))
    params += [("assets", a) for a in assets]
    if rescale:
        params.append(("rescale", rescale))
    if colormap_name:
        params.append(("colormap_name", colormap_name))
    params.append(("max_size", str(max_size)))
    if part:
        # web mercator like every map view — a 4326 crop looks E-W stretched
        # at mid latitudes, and cards should match what the maps show
        params.append(("dst_crs", "epsg:3857"))
    q = str(httpx.QueryParams(params))
    path = f"bbox/{part}.png" if part else "preview.png"
    return {"preview_url": f"{TITILER}/stac/{path}?{q}", "backend": "titiler-xyz"}


def _bbox_feature(bbox: list[float]) -> dict[str, Any]:
    """[w, s, e, n] as the GeoJSON Feature the tiler statistics POST wants."""
    w, s, e, n = bbox
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
    }


def compute_statistics(
    catalog: str,
    collection_id: str,
    item_id: str,
    expression: str | None = None,
    assets: list[str] | None = None,
    aoi_geojson: dict[str, Any] | None = None,
    bbox: list[float] | None = None,
    sel: dict[str, str] | None = None,
    apply_scaling: bool = True,
) -> dict[str, Any]:
    """Compute pixel statistics (min/max/mean/std/percentiles) for a STAC item.

    Same shape clipped or not: {band_or_expression: {min, max, mean, std, ...}},
    plus clipped_to when an AOI was applied and extent_note when it was not.

    The numbers are physical by default: the item's own raster:bands scale and offset
    are applied (Sentinel-2 comes back as reflectance, 0 to 1, and an expression is
    computed on the corrected bands, so an index is not biased by the post-2022 offset).
    Earth Search's post-2022 pixels already have the offset taken out (earthsearch:boa_offset_applied),
    so only the scale is applied there; Planetary Computer items carry no raster:bands, so nothing is.
    scaling_applied: true and band_scaling (the factors used) say so. apply_scaling=False
    is the raw escape hatch: raw digital numbers, with scaling_note naming the factors
    that were not applied.

    expression is band math over asset names, e.g. "(nir-red)/(nir+red)" for
    NDVI on Sentinel-2 (Earth Search asset names; Planetary Computer uses band
    ids like B04/B08 — check describe_collection). Optionally clip to an AOI
    GeoJSON Feature. Backed by the catalog's tiler statistics endpoint.

    bbox [w, s, e, n] clips to that area — the same thing aoi_geojson does, in
    the shape callers actually hold. geocode and search_imagery hand back a
    bbox, never a Feature, so unclipped whole-scene stats are usually just the
    cost of the conversion. Pass one of the two and the extent_note goes away.

    catalog="zarr": collection_id is a store id (search_datasets), item_id the
    variable, bbox required, sel picks the slice by nearest match, e.g.
    {"valid_time": "2026-10-26T21:00"} (UTC) on a forecast store. Returns
    {variable: stats, clipped_to, dataset, units, sel (what was actually
    used), caveats}: read the caveats before quoting the number.
    """
    if catalog == "zarr":
        return _zarr_statistics(collection_id, item_id, bbox, sel)
    if bbox and not aoi_geojson:
        aoi_geojson = _bbox_feature(bbox)
    backend = CATALOGS[catalog]["raster"]
    item_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    try:
        item = _get_json(item_url)
    except Exception:
        item = None
    # earth-search already took the -1000 out of post-2022 Sentinel-2 pixels yet still declares offset -0.1;
    # applying it again put half of an August cropland scene below zero (red median 926 DN here, 1930 on PC)
    offset_in_pixels = bool(item and (item.get("properties") or {}).get("earthsearch:boa_offset_applied"))
    factors: dict[str, tuple[float, float]] = {}
    if apply_scaling and item:
        for key, a in item.get("assets", {}).items():
            b0 = (a.get("raster:bands") or [{}])[0]
            sc, off = b0.get("scale") or 1, 0 if offset_in_pixels else b0.get("offset") or 0
            if (sc, off) != (1, 0):
                factors[key] = (sc, off)
    params: list[tuple[str, str]] = [("max_size", "512")]
    corrected = False
    if expression:
        if factors:
            raw_expression = expression
            # the offset does not cancel in a ratio, so each band is corrected before the index is computed
            # fixed-point numbers only: 1e-05 would read as the identifier "e" to the band parser
            def _corrected(m: re.Match) -> str:
                if m[0] not in factors:
                    return m[0]
                sc, off = factors[m[0]]
                return f"({m[0]}*{sc:.12f}{off:+.12f})"
            expression = re.sub(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", _corrected, expression)
            corrected = expression != raw_expression
        if backend == "pc":  # PC accepts named-asset expressions directly
            params += [("expression", expression), ("asset_as_band", "true")]
            assets = assets or _expression_idents(expression)
        else:
            expression, assets = _expression_to_bands(expression, assets)
            params.append(("expression", expression))
    used = assets or _default_assets(collection_id) or ["visual"]
    for a in used:
        params.append(("assets", a))
    if backend == "pc":
        base = "https://planetarycomputer.microsoft.com/api/data/v1/item/statistics"
        params += [("collection", collection_id), ("item", item_id)]
    elif backend == "veda":
        base = f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}/statistics"
    else:
        base = f"{TITILER}/stac/statistics"
        params.append(("url", f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"))
    if aoi_geojson:
        r = _client.post(base, params=params, json=aoi_geojson, timeout=60)
    else:
        r = _client.get(base, params=params, timeout=60)
    retried = False
    if r.status_code == 429 and backend == "titiler-xyz":
        # one retry on the other tiler, never a loop: the shared tiler is a courtesy
        href = None if expression or len(used) != 1 else _asset_href(catalog, collection_id, item_id, used[0])
        if href:
            retried = True
            base, params = f"{EOAPI}/external/statistics", [("max_size", "512"), ("url", href)]
            if aoi_geojson:
                r = _client.post(base, params=params, json=aoi_geojson, timeout=60)
            else:
                r = _client.get(base, params=params, timeout=60)
        if r.status_code == 429:
            return {"error": "titiler.xyz rate-limited (429); try again later or set GROUNDSTATION_TITILER"}
    if r.status_code >= 500 and backend not in ("pc", "veda"):
        # a traceback tells an agent nothing
        try:
            note = _no_cog_note(item) if item else None
        except Exception:
            note = None
        if note:
            return {"error": note}
    r.raise_for_status()
    stats = r.json()
    # strip histograms down so responses stay small
    def _slim(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: _slim(v)
                for k, v in node.items()
                if k not in ("histogram", "valid_pixels", "masked_pixels")
            }
        return node

    out = _slim(stats)
    # Field test No.8: a clipped call came back as a GeoJSON Feature with the
    # numbers under properties.statistics, an unclipped one as the bare band
    # dict, and the first thing that consumed both broke on the difference.
    # One shape: band -> stats, plus what it was clipped to
    if isinstance(out, dict) and out.get("type") == "Feature":
        out = dict((out.get("properties") or {}).get("statistics") or {})
        out["clipped_to"] = bbox if bbox else "aoi_geojson"
    # TiTiler names the bands of a plain asset request b1..bN. Field test No.8:
    # a caller asking for "vv" got "b1" back and had to guess. Give the asset
    # its name back when the mapping is one to one and no expression is involved
    # (the eoapi.dev retry always answers b1..bN, so it renames by the asset it was sent)
    names = assets or (used if retried else None)
    if names and not expression and isinstance(out, dict):
        for i, a in enumerate(names, 1):
            if f"b{i}" in out and a not in out:
                out[a] = out.pop(f"b{i}")
    if retried and isinstance(out, dict):
        out["note"] = "titiler.xyz rate-limited; statistics from raster.eoapi.dev"
    if factors and isinstance(out, dict):
        applied = False
        for k, v in out.items():
            if isinstance(v, dict) and "mean" in v and (expression is None and k in factors):
                sc, off = factors[k]
                n = v.get("count")
                for f in [f for f in v if f in ("min", "max", "mean", "median", "majority", "minority") or f.startswith("percentile_")]:
                    v[f] = v[f] * sc + off
                if "sum" in v and n is not None:
                    v["sum"] = v["sum"] * sc + n * off
                if "std" in v:
                    v["std"] *= sc
                applied = True
        if corrected or applied:
            out["scaling_applied"] = True
    try:
        item = item or {}
        if not aoi_geojson:
            # Unclipped stats cover the WHOLE item. For a MODIS tile that is most of
            # a continent, so the mean is real and meaningless — this is how a demo
            # ships a confident wrong number. Say what was actually measured.
            bb = item.get("bbox")
            if bb:
                w, h = bb[2] - bb[0], bb[3] - bb[1]
                out["extent_note"] = (
                    f"stats cover the full item extent ({w:.1f} x {h:.1f} deg)"
                    + (
                        ". That is a very large area — pass aoi_geojson to clip, "
                        "or these numbers describe a region, not your subject"
                        if max(w, h) > 5
                        else ". Pass aoi_geojson to clip to your subject"
                    )
                )
        # scale/offset live in STAC, so pass them through rather than
        # teaching this tool any one collection's units (MODIS LST is
        # 0.02 K per DN — the caller cannot guess that from raw values).
        # Both paths: a clipped number in the wrong unit is still wrong
        groups: dict[tuple, dict[str, Any]] = {}
        for key, a in item.get("assets", {}).items():
            for band in a.get("raster:bands", []) or []:
                if band.get("scale") is not None or band.get("offset") is not None:
                    g = {k: band[k] for k in ("scale", "offset", "unit") if k in band}
                    if offset_in_pixels and g.get("offset"):
                        g["declared_offset"], g["offset"] = g["offset"], 0
                    groups.setdefault(tuple(sorted(g.items())), {**g, "bands": []})["bands"].append(key)
        if groups:
            out["band_scaling"] = list(groups.values())
            raw = [g for g in groups.values() if g.get("scale") not in (None, 1) or g.get("offset") not in (None, 0)]
            if raw and not out.get("scaling_applied"):
                g = max(raw, key=lambda g: len(g["bands"]))
                out["scaling_note"] = (
                    f"values are raw digital numbers: the item's raster:bands give scale {g.get('scale')} "
                    f"and offset {g.get('offset')} on {len(g['bands'])} bands (physical = raw * scale + offset), "
                    "and nothing here applies them; with an expression a non-zero offset shifts the index too"
                    + (f" (the declared offset {g['declared_offset']} is already in the pixels)" if "declared_offset" in g else "")
                )
    except Exception:
        pass
    return out


_STRETCH: dict[tuple, tuple[float, float] | None] = {}
_RGB_ASSETS = ("visual", "image")


def _auto_stretch(catalog: str, collection_id: str, item_id: str, assets: list[str],
                  colormap_name: str | None, box: list[float] | None) -> tuple[float, float] | None:
    """(lo, hi) from the data for a continuous single-asset layer, else None.

    One statistics call per item, asset and box, kept for the process (a failure
    is kept as None, like _item). Raw values on purpose: tiles are raw, so
    physical-unit statistics would stretch every scaled collection to black.
    """
    if catalog == "zarr" or len(assets) != 1 or assets[0] in _RGB_ASSETS or colormap_name in LEGENDS:
        return None
    key = (f"{catalog}:{collection_id}:{item_id}", assets[0], tuple(box) if box else None)
    if key not in _STRETCH:
        _STRETCH[key] = None
        a = ((_item(catalog, collection_id, item_id) or {}).get("assets") or {}).get(assets[0]) or {}
        if "classification:classes" in a or any("classification:classes" in b for b in a.get("raster:bands") or []):
            return None
        try:
            stats = compute_statistics(catalog, collection_id, item_id, assets=assets, bbox=box, apply_scaling=False)
            b = next(v for v in stats.values() if isinstance(v, dict) and "percentile_2" in v)
            lo, hi = float(b["percentile_2"]), float(b["percentile_98"])
            if math.isfinite(lo) and math.isfinite(hi) and lo < hi:
                _STRETCH[key] = (lo, hi)
        except Exception:
            pass
    return _STRETCH[key]


def _shared_stretch(layers: list[dict[str, Any]], view: list[float] | None) -> list[dict[str, Any]]:
    """Fill rescale and rescale_basis on item layers that have neither rescale nor expression.

    One range per catalog, collection, asset and colormap across all the layers:
    the lowest 2nd and highest 98th percentile among the scenes, so a coverage
    set's tiles, a swipe's two dates and a timelapse's frames draw one value
    in one colour. Layers whose statistics fail take the others' range.
    """
    def key(l):
        if l.get("type") != "item" or l.get("rescale") or l.get("expression"):
            return None
        return (l["catalog"], l["collection_id"], tuple(l.get("assets") or _default_assets(l["collection_id"])), l.get("colormap_name"))

    ranges: dict[tuple, list[tuple[float, float]]] = {}
    for l in layers:
        if k := key(l):
            st = _auto_stretch(k[0], k[1], l["item_id"], list(k[2]), k[3], view or l.get("bbox"))
            ranges.setdefault(k, []).extend([st] if st else [])
    out = []
    for l in layers:
        rs = ranges.get(key(l)) or []
        if rs:
            basis = f"2nd to 98th percentile of the raw values over the {'box' if view else 'scene'}"
            l = {**l, "rescale": "%.6g,%.6g" % (min(r[0] for r in rs), max(r[1] for r in rs)),
                 "rescale_basis": basis + (f", widest of {len(rs)} scenes" if len(rs) > 1 else "")}
        out.append(l)
    return out


def tile_url_template(
    catalog: str,
    collection_id: str,
    item_id: str,
    assets: list[str] | None = None,
    rescale: str | None = None,
    colormap_name: str | None = None,
    expression: str | None = None,
    sel: dict[str, str] | None = None,
    bbox: list[float] | None = None,
) -> str:
    """XYZ tile URL template ({z}/{x}/{y}) for a STAC item, for web maps.

    catalog="zarr": item_id is the variable and sel picks the slice, e.g.
    {"time": "2023-01-10T12:00"}; stores that titiler.xyz cannot draw return
    their tiles_note instead of a template. Pass the template to render_map
    as a {"type": "raster", "tiles": ...} layer.

    For index layers (NDVI etc.) pass expression over asset names, e.g.
    "(nir-red)/(nir+red)" with colormap_name="rdylgn" — supported on
    earth-search; ignored for veda/planetary-computer. Index expressions
    default to rescale="-1,1" (without a rescale they render blank).

    With no rescale, a continuous single-asset layer (not visual/image, not
    categorical, no expression) is stretched 2nd to 98th percentile of the raw
    values over bbox (the whole item when bbox is omitted), so an unfamiliar
    collection's units do not draw black. When the statistics fail or come
    back flat the URL carries no rescale. Pass rescale="lo,hi" to override;
    render_map reports what it used as rescale_used / rescale_basis.
    """
    if catalog == "zarr":
        z = ZARR_STORES.get(collection_id)
        if not z:
            return f"unknown zarr store {collection_id!r}; options: {', '.join(ZARR_STORES)}"
        if refusal := _zarr_time_refusal(collection_id, z, sel):
            return f"{refusal}; pick a time inside it, no tile URL returned"
        if not z["tiles"]:
            return z["tiles_note"]
        v = z["variables"].get(item_id)
        if v is None:
            return f"unknown variable {item_id!r}; options: {', '.join(z['variables'])}"
        params = [("url", z["url"]), ("variable", item_id)]
        params += [("sel", f"{z['time_dim'] if k == 'valid_time' else k}=nearest::{x}") for k, x in (sel or {}).items()]
        if rescale or v.get("rescale"):
            params.append(("rescale", rescale or v["rescale"]))
        if colormap_name or v.get("colormap_name"):
            params.append(("colormap_name", colormap_name or v["colormap_name"]))
        return f"{TITILER}/zarr/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?" + str(httpx.QueryParams(params))
    # with an expression, assets must be the expression's bands, never defaults
    assets = assets or ([] if expression else _default_assets(collection_id))
    if expression and not rescale:
        rescale = "-1,1"  # normalized-difference indices are unreadable unscaled
    elif not rescale and not expression and (st := _auto_stretch(catalog, collection_id, item_id, assets, colormap_name, bbox)):
        rescale = "%.6g,%.6g" % st
    backend = CATALOGS[catalog]["raster"]
    if backend == "pc":
        # item-tiles render empty for reprojection-heavy sources (MODIS
        # sinusoidal, GOES geostationary); the registered mosaic handles all
        # collections. Costs one registration POST per unique item (cached).
        params = [("collection", collection_id)] + [("assets", a) for a in assets]
        if rescale:
            params.append(("rescale", rescale))
        if colormap_name:
            params.append(("colormap_name", colormap_name))
        try:
            mid = _pc_mosaic_id(collection_id, item_id)
        except httpx.HTTPError:
            # A slow registration endpoint must not cost the whole map. Item
            # tiles need no registration; the render says which path it took
            _PC_FALLBACKS.add(item_id)
            q = str(httpx.QueryParams(params + [("item", item_id)]))
            return "https://planetarycomputer.microsoft.com/api/data/v1/item/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?" + q
        q = str(httpx.QueryParams(params))
        return (
            f"https://planetarycomputer.microsoft.com/api/data/v1/mosaic/{mid}"
            "/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.png?" + q
        )
    if backend == "veda":
        params = [("assets", a) for a in assets or ["cog_default"]]
        if rescale:
            params.append(("rescale", rescale))
        if colormap_name:
            params.append(("colormap_name", colormap_name))
        q = str(httpx.QueryParams(params))
        return (
            f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}"
            "/tiles/WebMercatorQuad/{z}/{x}/{y}.png?" + q
        )
    self_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    params = [("url", self_url)]
    if expression:
        expression, assets = _expression_to_bands(expression, assets or None)
        params.append(("expression", expression))
    params += [("assets", a) for a in assets]
    if rescale:
        params.append(("rescale", rescale))
    if colormap_name:
        params.append(("colormap_name", colormap_name))
    q = str(httpx.QueryParams(params))
    return f"{TITILER}/stac/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?" + q


# ---------------------------------------------------------------- map artifact

# DS brand tokens (devseed-poster system), shared by every artifact template.
# Kind colors carry meaning: data blue, access ochre, tiling DS orange,
# viz forest green, standard violet, infra gray — same semantics as the
# terminal colors in scripts/doctor.sh.
_BRAND_CSS = """  :root { --accent: #CF3F02; --ink: #443F3F; --mid: #4a4440; --rule: #dedad4; --paper: #f7f4ef;
    --k-place: #7a5c2e; --k-catalog: #1d4e8f; --k-data: #6b4c8f; --k-pixels: #CF3F02; --k-draw: #2a5c45;
    --k-orbit: #4a4440; }"""

# Categorical colormaps the tools can explain: colormap_name -> legend. The
# WorldCover colours are the tiler's own classmap
# (planetarycomputer.microsoft.com/api/data/v1/legend/classmap/esa-worldcover)
LEGENDS: dict[str, dict] = {
    "esa-worldcover": {"title": "ESA WorldCover 2021", "classes": [
        (10, "Tree cover", "#006400"), (20, "Shrubland", "#ffbb22"), (30, "Grassland", "#ffff4c"),
        (40, "Cropland", "#f096ff"), (50, "Built-up", "#fa0000"), (60, "Bare / sparse vegetation", "#b4b4b4"),
        (70, "Snow and ice", "#f0f0f0"), (80, "Permanent water bodies", "#0064c8"),
        (90, "Herbaceous wetland", "#0096a0"), (95, "Mangroves", "#00cf75"), (100, "Moss and lichen", "#fae6a0"),
    ]},
}


def _legends_for(layers: list[dict[str, Any]], colormap_name: str | None = None) -> list[dict[str, Any]]:
    """One legend per distinct known categorical colormap, in layer order."""
    names = [colormap_name] + [l.get("colormap_name") for l in layers]
    seen = dict.fromkeys(n for n in names if n in LEGENDS)
    return [
        {"colormap_name": n, "title": LEGENDS[n]["title"],
         "classes": [{"value": v, "name": nm, "color": c} for v, nm, c in LEGENDS[n]["classes"]]}
        for n in seen
    ]


def _legend_map_html(legends: list[dict[str, Any]]) -> str:
    if not legends:
        return ""
    boxes = "".join(
        f'<details class="legend" open><summary>{escape(g["title"])}</summary>'
        + "".join(f'<div><i style="background:{c["color"]}"></i>{escape(c["name"])}</div>' for c in g["classes"])
        + "</details>"
        for g in legends
    )
    # phone rule: collapsed at 390 px and capped to 40vh with its own scroll, so it never covers the map
    return f"""<div id="legends" aria-label="Legend">{boxes}</div>
<style>
  #legends {{ position: absolute; bottom: 60px; right: 12px; z-index: 2; display: flex; flex-direction: column;
    gap: 6px; max-width: min(230px, 60vw); max-height: 40vh; overflow-y: auto;
    font: 12px/1.35 "Roboto", system-ui, sans-serif; }}
  .legend {{ background: rgba(247,244,239,.96); color: var(--ink); border-top: 3px solid var(--accent);
    border-radius: 0 0 8px 8px; padding: 6px 10px; box-shadow: 0 2px 10px rgba(0,0,0,.18); }}
  .legend summary {{ cursor: pointer; font-weight: 700; }}
  .legend div {{ display: flex; align-items: center; gap: 7px; margin: 3px 0; }}
  .legend i {{ flex: none; width: 14px; height: 14px; border: 1px solid rgba(0,0,0,.35); border-radius: 3px; }}
  .clean #legends {{ display: none !important; }}
</style>
<script>
  if (innerWidth <= 500) document.querySelectorAll(".legend").forEach(d => d.removeAttribute("open"));
</script>
"""


def _legend_card_html(legends: list[dict[str, Any]]) -> str:
    return "".join(
        f'<div class="legend"><b>{escape(g["title"])}</b>'
        + "".join(f'<span><i style="background:{c["color"]}"></i>{escape(c["name"])}</span>' for c in g["classes"])
        + "</div>"
        for g in legends
    )


# the stack layer chunk — injected into _MAP_TEMPLATE only when stack_layer=True,
# so default artifacts carry zero extra bytes
_STACK_CHUNK = """<button id="stack-toggle" aria-expanded="false">Stack</button>
<aside id="stack" aria-label="Technology stack">
  <h2>The stack behind this map</h2>
  <p class="stack-sub">__SUMMARY__</p>
  <p class="stack-sub">the pipeline in order · tap any entry for what it is and what it talks to</p>
  __ENTRIES__
</aside>
<style>
  #stack-toggle { position: absolute; top: 12px; right: 52px; z-index: 4; font: 12px/1 "Roboto", system-ui, sans-serif;
    letter-spacing: .08em; text-transform: uppercase; background: var(--paper); color: var(--accent);
    border: 1px solid var(--accent); border-radius: 6px; padding: 7px 12px; cursor: pointer; }
  #stack-toggle:hover, #stack-toggle[aria-expanded="true"] { background: var(--accent); color: #fff; }
  #stack { position: absolute; top: 0; right: 0; bottom: 40px; width: 330px; max-width: 88vw; z-index: 3;
    background: var(--paper); color: var(--ink); font: 13px/1.5 "Roboto", system-ui, sans-serif;
    border-left: 3px solid var(--accent); padding: 46px 16px 16px; overflow-y: auto;
    transform: translateX(105%); transition: transform .22s ease; box-shadow: -2px 0 12px rgba(0,0,0,.12); }
  #stack.open { transform: none; }
  @media (prefers-reduced-motion: reduce) { #stack, .stack-entry summary::after { transition: none; } }
  #stack h2 { font: 700 15px/1.2 "Roboto Condensed", "Roboto", system-ui, sans-serif; margin: 0 0 2px; }
  #stack .stack-sub { margin: 0 0 12px; color: var(--mid); font-size: 11.5px; font-style: italic; }
  .stack-group { font: 700 10px/1 "Roboto Mono", monospace; letter-spacing: .14em; text-transform: uppercase;
    color: var(--mid); border-bottom: 1px solid var(--rule); padding-bottom: 4px; margin: 14px 0 8px; }
  .stack-entry { margin: 0 0 9px; }
  .stack-entry summary { display: flex; gap: 8px; cursor: pointer; list-style: none; }
  .stack-entry summary::-webkit-details-marker { display: none; }
  .stack-entry summary::after { content: "▸"; flex: none; margin-left: auto; color: var(--mid);
    font-size: 10px; align-self: center; transition: transform .15s ease; }
  .stack-entry[open] summary::after { transform: rotate(90deg); }
  .stack-entry .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; }
  .dot.k-place { background: var(--k-place); } .dot.k-catalog { background: var(--k-catalog); }
  .dot.k-data { background: var(--k-data); } .dot.k-pixels { background: var(--k-pixels); }
  .dot.k-draw { background: var(--k-draw); } .dot.k-orbit { background: var(--k-orbit); }
  .stack-name .island { font: 500 9px/1 "Roboto Mono", monospace; letter-spacing: .1em; text-transform: uppercase;
    color: var(--accent); border: 1px dashed var(--accent); border-radius: 3px; padding: 2px 4px; margin-left: 4px;
    vertical-align: 1px; }
  .s-head { display: flex; flex-direction: column; min-width: 0; }
  .stack-name { font-weight: 600; }
  .stack-name .role { font: 500 9px/1 "Roboto Mono", monospace; letter-spacing: .1em; text-transform: uppercase;
    color: var(--mid); border: 1px solid var(--rule); border-radius: 3px; padding: 2px 4px; margin-left: 6px;
    vertical-align: 1px; }
  .stack-name .role.ds { background: var(--accent); border-color: var(--accent); color: #fff; }
  .stack-entry .inst { font: 11px/1.5 "Roboto Mono", monospace; color: var(--mid); }
  .stack-entry .more { margin: 3px 0 2px 16px; }
  .stack-entry .more p { margin: 2px 0; color: var(--ink); }
  .stack-entry .spk { font: 10.5px/1.5 "Roboto Mono", monospace; color: var(--mid); margin: 2px 0; }
  .stack-entry a { color: var(--mid); font-size: 10.5px; text-decoration: none; border-bottom: 1px solid var(--rule); }
  .stack-entry a:hover { color: var(--accent); border-bottom-color: var(--accent); }
</style>
<script>
  const _sb = document.getElementById("stack-toggle"), _sp = document.getElementById("stack");
  _sb.addEventListener("click", () => {
    const open = _sp.classList.toggle("open");
    _sb.setAttribute("aria-expanded", String(open));
  });
</script>
"""

_MAP_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js" integrity="sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznwA0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj" crossorigin="anonymous"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" integrity="sha384-MinO0mNliZ3vwppuPOUnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V" crossorigin="anonymous">
<style>
__BRAND__
  html, body, #map { margin: 0; height: 100%; }
  #panel { position: absolute; top: 12px; left: 12px; z-index: 2; background: rgba(247,244,239,.96);
    color: var(--ink); border-top: 3px solid var(--accent);
    padding: 12px 16px; border-radius: 0 0 10px 10px; font: 14px/1.45 "Roboto", system-ui, sans-serif; max-width: 340px;
    box-shadow: 0 2px 10px rgba(0,0,0,.18); }
  #panel h1 { font: 700 15px/1.25 "Roboto Condensed", "Roboto", system-ui, sans-serif; margin: 0 0 4px; }
  #panel p { margin: 4px 0 8px; color: var(--mid); }
  #panel label { display: block; margin: 2px 0; cursor: pointer; }
  #panel summary { cursor: pointer; font-weight: 700; }
  /* a timelapse never fills the toggles, and an empty fold is a heading over nothing */
  #panel details:has(#toggles:empty) { display: none; }
  #panel small { display: block; margin-left: 22px; color: var(--mid); font-size: 11px; overflow-wrap: anywhere; }
  /* the STACK button sits top-right, so a phone panel stops short of it */
  @media (max-width: 500px) { #panel { box-sizing: border-box; max-width: calc(100vw - 150px); } }
  #credit { position: absolute; bottom: 24px; left: 12px; z-index: 2; font: 11px "Roboto Mono", monospace;
    color: var(--mid); background: rgba(247,244,239,.85); padding: 2px 8px; border-radius: 6px; }
  #mapL { position: absolute; inset: 0; z-index: 1; }
  #divider { position: absolute; top: 0; bottom: 0; width: 3px; margin-left: -1.5px; background: var(--ink);
    z-index: 3; cursor: ew-resize; touch-action: none; }
  #divider .knob { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 34px; height: 34px; border-radius: 50%; background: var(--ink); color: #fff;
    display: flex; align-items: center; justify-content: center; font: 13px system-ui; }
  .side-label { position: absolute; bottom: 52px; z-index: 2; background: rgba(247,244,239,.96); color: var(--ink);
    padding: 4px 11px; border-radius: 6px; font: 12px "Roboto", system-ui, sans-serif; box-shadow: 0 1px 5px rgba(0,0,0,.15); }
  #tl { position: absolute; bottom: 64px; left: 50%; transform: translateX(-50%); z-index: 3;
    display: flex; align-items: center; gap: 8px; background: rgba(247,244,239,.96); color: var(--ink);
    padding: 6px 12px; border-radius: 8px; font: 14px "Roboto", system-ui, sans-serif; box-shadow: 0 2px 10px rgba(0,0,0,.18); }
  #tl button { font: inherit; color: inherit; background: none; border: 1px solid var(--mid); border-radius: 6px;
    min-width: 32px; padding: 2px 8px; cursor: pointer; }
  #tl span { min-width: 7em; text-align: center; }
  /* #clean = snapshot mode for postcard bakes: map chrome off, story on
     (divider + side labels stay; MapLibre's attribution control stays) */
  .clean #panel, .clean #credit, .clean #stack-toggle, .clean #stack, .clean #tl,
  .clean .maplibregl-ctrl-top-right { display: none !important; }
</style>
<script>if (location.hash === "#clean") document.documentElement.classList.add("clean");</script>
</head><body>
<div id="map"></div>
__TLBAR__
<div id="panel"><h1>__TITLE__</h1><p>__SUBTITLE__</p><details class="fold" open><summary>Layers</summary><div id="toggles"></div></details>__STACMAP__</div>
<div id="credit">groundstation · Development Seed labs · STAC + TiTiler</div>
__STACK__
__LEGEND__
<script>
if (innerWidth <= 500) document.querySelectorAll("#panel details").forEach(d => d.removeAttribute("open"));
const esc = s => String(s).replace(/[&<>"']/g, c => "&#" + c.charCodeAt(0) + ";");
const LAYERS = __LAYERS__;
const BBOX = __BBOX__;
// Field test No.8: CARTO's free basemap started serving "API KEY REQUIRED"
// watermark tiles under every artifact. OpenFreeMap needs no key and its
// positron style is the same light look; raster layers stack on it unchanged
const BASE_STYLE = () => "https://tiles.openfreemap.org/styles/positron";
const MAP_OPTS = { style: BASE_STYLE(), bounds: [[BBOX[0], BBOX[1]], [BBOX[2], BBOX[3]]],
  fitBoundsOptions: { padding: 40 } };

// CloudFront on titiler.xyz caches the CORS header with the tile and does not
// vary by origin, so the URL must differ per origin or a cached tile is refused
const originKey = (u) => u + (u.includes("?") ? "&" : "?") + "o=" + (location.origin === "null" ? "sandbox" : location.origin.replace(/[^a-z0-9]/gi, "").slice(-16));

function addLayerTo(m, l, i) {
  const id = "layer" + i;
  if (l.type === "raster") {
    m.addSource(id, { type: "raster", tiles: [originKey(l.tiles)], tileSize: 256,
      ...(l.bounds ? { bounds: l.bounds } : {}) });
    m.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": l.opacity ?? 1 } });
    if (l.fallback_tiles) {
      // titiler.xyz rate-limits (429): swap this source to raster.eoapi.dev, once.
      // If titiler.xyz 429 lacks CORS headers, the browser reports status 0 / network error.
      let swapped = false;
      m.on("error", (e) => {
        if (swapped || e.sourceId !== id || !e.error) return;
        const s = e.error.status;
        if (s !== 429 && s !== 0 && s !== undefined) return console.error(e.error);
        swapped = true;
        m.getSource(id).setTiles([originKey(l.fallback_tiles)]);
        const lab = document.getElementById("lbl" + id);
        if (lab) lab.textContent += " (via raster.eoapi.dev)";
      });
    }
  } else if (l.type === "geojson") {
    m.addSource(id, { type: "geojson", data: l.data });
    m.addLayer({ id: id + "-fill", type: "circle", source: id,
      paint: { "circle-radius": 7, "circle-color": l.color || "#d63b3b",
               "circle-opacity": .85, "circle-stroke-width": 1.5, "circle-stroke-color": "#fff" },
      filter: ["==", ["geometry-type"], "Point"] });
    m.addLayer({ id: id + "-line", type: "line", source: id,
      paint: { "line-color": l.color || "#d63b3b", "line-width": 2 },
      filter: ["!=", ["geometry-type"], "Point"] });
    m.on("click", id + "-fill", (e) => {
      const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat)
        .setHTML("<b>" + esc(p.title || p.name || "") + "</b><br>" + esc(p.description || p.date || ""))
        .addTo(m);
    });
  }
}

const COMPARE = __COMPARE__;
const rasters = LAYERS.map((l, i) => ({ ...l, i })).filter(l => l.type === "raster");
const geojsons = LAYERS.map((l, i) => ({ ...l, i })).filter(l => l.type === "geojson");

if (COMPARE && rasters.length === 2) {
  // comparison: two synced maps, right one on top clipped left of a draggable divider
  document.getElementById("map").insertAdjacentHTML("afterend",
    `<div id="mapL"></div><div id="divider"><div class="knob">◂▸</div></div>
     <div class="side-label" style="left:12px">◂ ${esc(rasters[1].name)}</div>
     <div class="side-label" style="right:12px">${esc(rasters[0].name)} ▸</div>`);
  const mapR = new maplibregl.Map({ container: "map", ...MAP_OPTS });
  const mapL = new maplibregl.Map({ container: "mapL", style: BASE_STYLE(),
    bounds: MAP_OPTS.bounds, fitBoundsOptions: MAP_OPTS.fitBoundsOptions });
  window.gsMaps = [mapL, mapR];  // exposed for scripted screenshots and checks
  window.gsPending = 2;  // load handlers still to add their layers; a snapshot waits for 0
  mapR.addControl(new maplibregl.NavigationControl());
  mapR.on("load", () => { addLayerTo(mapR, rasters[0], rasters[0].i); geojsons.forEach(g => addLayerTo(mapR, g, g.i)); window.gsPending--; });
  mapL.on("load", () => { addLayerTo(mapL, rasters[1], rasters[1].i); geojsons.forEach(g => addLayerTo(mapL, g, g.i)); window.gsPending--; });
  let syncing = false;
  const follow = (src, dst) => src.on("move", () => {
    if (syncing) return; syncing = true;
    dst.jumpTo({ center: src.getCenter(), zoom: src.getZoom(), bearing: src.getBearing(), pitch: src.getPitch() });
    syncing = false;
  });
  follow(mapL, mapR); follow(mapR, mapL);
  const divider = document.getElementById("divider"), left = document.getElementById("mapL");
  const setSplit = x => {
    const w = document.body.clientWidth;
    x = Math.max(30, Math.min(w - 30, x));
    divider.style.left = x + "px";
    // clip-path also clips pointer events, so each half stays independently draggable
    left.style.clipPath = `inset(0 ${w - x}px 0 0)`;
  };
  setSplit(document.body.clientWidth / 2);
  divider.addEventListener("pointerdown", e => {
    e.preventDefault(); divider.setPointerCapture(e.pointerId);
    const move = ev => setSplit(ev.clientX);
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", () => divider.removeEventListener("pointermove", move), { once: true });
  });
  window.addEventListener("resize", () => setSplit(parseFloat(divider.style.left)));
  document.getElementById("toggles").innerHTML = '<span style="color:#555">drag the divider to compare</span>';
} else if (__TIMELAPSE__) {
  const map = new maplibregl.Map({ container: "map", ...MAP_OPTS });
  window.gsMaps = [map];
  window.gsPending = 1;
  map.addControl(new maplibregl.NavigationControl());
  let cur = 0, timer = null;
  const name = document.getElementById("tl-name"), play = document.getElementById("tl-play");
  // MapLibre fetches no tiles for a layer whose visibility is none, so hidden frames cost no bandwidth
  const show = k => {
    cur = (k + LAYERS.length) % LAYERS.length;
    LAYERS.forEach((_, i) => map.setLayoutProperty("layer" + i, "visibility", i === cur ? "visible" : "none"));
    name.textContent = LAYERS[cur].name + "  (" + (cur + 1) + "/" + LAYERS.length + ")";
  };
  const stop = () => { clearInterval(timer); timer = null; play.textContent = "\u25b6"; };
  const step = k => { stop(); show(cur + k); };
  document.getElementById("tl-prev").onclick = () => step(-1);
  document.getElementById("tl-next").onclick = () => step(1);
  play.onclick = () => {
    if (timer) return stop();
    play.textContent = "\u275a\u275a";
    timer = setInterval(() => show(cur + 1), __INTERVAL__);
  };
  document.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
  });
  map.on("load", () => {
    LAYERS.forEach((l, i) => addLayerTo(map, l, i));
    show(0);
    window.gsPending--;
  });
} else {
  const map = new maplibregl.Map({ container: "map", ...MAP_OPTS });
  window.gsMaps = [map];  // exposed for scripted screenshots and checks
  window.gsPending = 1;  // the load handler still has to add the layers; a snapshot waits for 0
  map.addControl(new maplibregl.NavigationControl());
  map.on("load", () => {
    const toggles = document.getElementById("toggles");
    LAYERS.forEach((l, i) => {
      addLayerTo(map, l, i);
      const id = "layer" + i;
      const row = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = true;
      cb.onchange = () => {
        const vis = cb.checked ? "visible" : "none";
        [id, id + "-fill", id + "-line"].forEach(x => map.getLayer(x) && map.setLayoutProperty(x, "visibility", vis));
      };
      const lab = document.createElement("span");
      lab.id = "lbl" + id; lab.textContent = " " + l.name;
      row.appendChild(cb); row.appendChild(lab);
      if (l.detail) { const d = document.createElement("small"); d.textContent = l.detail; row.title = l.detail; row.appendChild(d); }
      toggles.appendChild(row);
    });
    window.gsPending--;
  });
}
</script></body></html>
"""


def stac_map_link(catalog: str, collection_id: str, item_id: str) -> str:
    """stac-map opens any STAC URL from ?href= (src/utils/href.ts)."""
    if catalog not in CATALOGS or "stac" not in CATALOGS[catalog]:
        return ""
    self_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    return "https://developmentseed.org/stac-map/?href=" + quote(self_url, safe="")


_ITEMS: dict[str, dict[str, Any] | None] = {}


def _item(catalog: str, collection_id: str, item_id: str) -> dict[str, Any] | None:
    """The STAC item, fetched at most once per process; a failure is cached as None.

    ponytail: a transient failure sticks until the server restarts; add a TTL
    if a flaky catalog makes fallbacks disappear in a long-lived session
    """
    key = f"{catalog}:{collection_id}:{item_id}"
    if key not in _ITEMS:
        try:
            _ITEMS[key] = _get_json(f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}")
        except Exception:
            _ITEMS[key] = None
    return _ITEMS[key]


def _asset_href(catalog: str, collection_id: str, item_id: str, asset: str) -> str | None:
    """http(s) href of one item asset; None when absent or not publicly readable."""
    href = ((_item(catalog, collection_id, item_id) or {}).get("assets", {}).get(asset) or {}).get("href")
    # s3:// and friends: the public tiler cannot read them
    return href if isinstance(href, str) and href.startswith(("http://", "https://")) else None


def _fallback_tiles(l: dict[str, Any]) -> str | None:
    """raster.eoapi.dev tile template for a titiler.xyz item layer, or None.

    Only a single-asset layer with no expression: eoapi.dev has no /stac route,
    so it cannot merge bands or run band math.
    ponytail: single-asset only; a self-hosted titiler with /stac would let
    band-merge and expression layers fall back too
    """
    if CATALOGS[l["catalog"]]["raster"] != "titiler-xyz" or l.get("expression"):
        return None
    assets = l.get("assets") or _default_assets(l["collection_id"])
    if len(assets) != 1:
        return None
    # no asset_href from the caller: read it from the cached item (one GET per item, ever)
    href = l.get("asset_href") or _asset_href(l["catalog"], l["collection_id"], l["item_id"], assets[0])
    if not href:
        return None
    params = [("url", href)]
    if l.get("rescale"):
        params.append(("rescale", l["rescale"]))
    if l.get("colormap_name"):
        params.append(("colormap_name", l["colormap_name"]))
    return f"{EOAPI}/external/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?" + str(httpx.QueryParams(params))


# resolved layers are serialised straight into the page's JS, so the footprint
# lives beside them keyed by tile URL instead of riding along as another field
_LAYER_FOOTPRINTS: dict[str, Any] = {}


def _layer_shape(l: dict[str, Any]) -> Any:
    """The footprint of a resolved raster layer, else its bounds."""
    return _LAYER_FOOTPRINTS.get(l.get("tiles")) or l.get("bounds")


def _hole_text(aoi: list[float], shapes: list[Any]) -> str:
    pct, where = _uncovered_side(aoi, shapes)
    return f"{pct:.0f}% of the view is uncovered, mostly {where}"


def _resolve_layer(l: dict[str, Any], view: list[float] | None = None) -> dict[str, Any]:
    """An {"type": "item"} layer becomes a raster layer with tiles + scene bounds.

    view is the map's box; with no rescale given it is the box the stretch is
    measured over. A map of several layers runs _shared_stretch over all of
    them first, so this one-layer call only fills a layer nothing else shares
    """
    if l.get("type") != "item":
        return l
    l = _shared_stretch([l], view)[0]
    rescale, basis = l.get("rescale"), l.get("rescale_basis", "given")
    if not rescale and l.get("expression"):
        rescale, basis = "-1,1", "default for an index expression"
    item_bounds = l.get("bbox")
    if item_bounds is None:
        # scene footprint bounds stop the map requesting (and the tiler
        # serving 404s for) every out-of-footprint tile in the viewport
        item_bounds = (_item(l["catalog"], l["collection_id"], l["item_id"]) or {}).get("bbox")
    fallback = _fallback_tiles(l)
    tiles = tile_url_template(
        l["catalog"],
        l["collection_id"],
        l["item_id"],
        l.get("assets"),
        l.get("rescale"),
        l.get("colormap_name"),
        l.get("expression"),
        bbox=view or l.get("bbox"),
    )
    # a caller who passed bbox wants no fetch at all, so only the bbox-less path reads the item
    geom = l.get("footprint") or (None if l.get("bbox") else (_item(l["catalog"], l["collection_id"], l["item_id"]) or {}).get("geometry"))
    if isinstance(geom, dict) and geom.get("type") in ("Polygon", "MultiPolygon"):
        _LAYER_FOOTPRINTS[tiles] = geom
    return {
        "type": "raster",
        "name": l.get("name") or l["item_id"],
        **({"detail": l["detail"]} if l.get("detail") else {}),
        "tiles": tiles,
        "opacity": l.get("opacity", 1),
        **({"bounds": item_bounds} if item_bounds else {}),
        **({"pc_fallback": True} if l.get("item_id") in _PC_FALLBACKS else {}),
        **({"fallback_tiles": fallback} if fallback else {}),
        **({"rescale_used": rescale, "rescale_basis": basis} if rescale else {}),
    }


def _expand_coverage_set(layer: dict[str, Any]) -> list[dict[str, Any]]:
    """A {"type": "coverage_set"} layer becomes one item layer per scene.

    Takes search_imagery's full_coverage_set verbatim, so the caller never
    hand-unrolls items — the authoring path that dropped scenes and shipped
    a 58%-coverage map in field test №4. Render params (assets, expression,
    colormap_name, rescale, opacity) apply to every scene in the set.
    """
    if layer.get("type") != "coverage_set":
        return [layer]
    items = (layer.get("set") or {}).get("items") or []
    if not items:
        raise ValueError("coverage_set layer has no items — pass search_imagery's full_coverage_set")
    prefix = layer.get("name") or (layer.get("set") or {}).get("date") or "coverage"
    params = {k: layer[k] for k in ("assets", "expression", "colormap_name", "rescale", "opacity") if k in layer}
    out = []
    for it in items:
        out.append({
            "type": "item",
            "name": prefix,
            "detail": it["id"],
            "catalog": it["catalog"],
            "collection_id": it["collection"],
            "item_id": it["id"],
            "bbox": it.get("bbox"),
            **({"footprint": it["footprint"]} if it.get("footprint") else {}),
            **params,
        })
    return out


def _layer_state(l: dict[str, Any], deferred: bool = False) -> dict[str, Any]:
    """One layer as data an eval or a view can read, not HTML someone must open.

    Shared by render_map and render_map_3d deliberately — two copies of a shape
    that has to stay identical is how they quietly stop being identical.
    `deferred` marks a 3D extra_layer: embedded, but not requested until the
    viewer clicks Load full coverage.
    """
    return {
        "name": l.get("name"),
        **({"detail": l["detail"]} if l.get("detail") else {}),
        "kind": l.get("type"),
        **({"bounds": l["bounds"]} if l.get("bounds") else {}),
        **({"tiles": l["tiles"]} if l.get("tiles") else {}),
        **({"fallback_tiles": l["fallback_tiles"]} if l.get("fallback_tiles") else {}),
        **({"rescale_used": l["rescale_used"], "rescale_basis": l["rescale_basis"]} if l.get("rescale_used") else {}),
        **({"opacity": l["opacity"]} if l.get("opacity") not in (None, 1) else {}),
        **({"deferred": True} if deferred else {}),
    }


def _footprints_overlap(resolved: list[dict[str, Any]], min_frac: float = 0.6) -> bool:
    """True when two raster layers cover the same ground, i.e. a real comparison.

    A before/after pair shares a footprint. A mosaic's tiles sit side by side,
    and swiping one hides half the AOI behind the divider. Unknown bounds means
    we cannot tell, so fall back to comparison — that is what a caller who
    passed two dates is asking for.
    """
    boxes = [l["bounds"] for l in resolved if l.get("type") == "raster" and l.get("bounds")]
    if len(boxes) != 2:
        return True
    a, b = boxes
    a_parts = _split_antimeridian(a)
    b_parts = _split_antimeridian(b)
    area_a = sum((p[2] - p[0]) * (p[3] - p[1]) for p in a_parts)
    area_b = sum((p[2] - p[0]) * (p[3] - p[1]) for p in b_parts)
    inter = 0.0
    for pa in a_parts:
        for pb in b_parts:
            w = max(pa[0], pb[0])
            s = max(pa[1], pb[1])
            e = min(pa[2], pb[2])
            n = min(pa[3], pb[3])
            if e > w and n > s:
                inter += (e - w) * (n - s)
    smaller = min(area_a, area_b)
    return smaller > 0 and inter / smaller >= min_frac


def _js(value: Any) -> str:
    # json inside <script>: "<" as < so a "</script>" in a name cannot end the block
    return json.dumps(value).replace("<", "\\u003c")


def _fill(template: str, mapping: dict[str, str]) -> str:
    # one pass over the __KEY__ tokens: a substituted value is never re-expanded,
    # so caller text containing "__LAYERS__" stays literal
    return re.sub(r"__([A-Z]+)__", lambda m: mapping.get(m.group(1), m.group(0)), template)


def _artifact_path(out_path: str | None, prefix: str, title: str) -> str:
    """Resolve where an HTML artifact goes; raises ValueError for a path it will not write."""
    out_dir = Path(os.environ.get("GROUNDSTATION_OUT", Path.cwd() / "demo"))
    if not out_path:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"{prefix}-{slugify(title)}.html")
    dest = Path(out_path)
    if dest.suffix.lower() != ".html":
        raise ValueError("out_path must end in .html")
    if dest.is_symlink():
        raise ValueError("out_path is a symlink; refusing to write through it")
    # GROUNDSTATION_OUT is where the console writes; the cwd tree is where the
    # field-test scripts write (docs/img), and it counts only when cwd is a
    # repository checkout: a server started from a home folder or / must not
    # make the whole disk a legal destination
    cwd = Path.cwd().resolve()
    roots = [out_dir.resolve()] + ([cwd] if (cwd / ".git").exists() else [])
    if not any(dest.resolve().is_relative_to(r) for r in roots):
        raise ValueError("out_path must sit under GROUNDSTATION_OUT or a repository checkout you are working in")
    return str(dest)


def _write_artifact(path: str, html: str) -> None:
    """Write through a temp file and rename, so a symlink swapped in after the
    check cannot redirect the write (rename replaces the link, never follows it)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, dest)


# standard card shapes (name -> height/width). A card is a deliberate
# object; its shape should be a design choice, not whatever the bbox
# happened to be after geocoding.
_CARD_RATIOS = {"3:2": 2 / 3, "4:3": 3 / 4, "1:1": 1.0, "4:5": 5 / 4, "2:3": 3 / 2}


def _snap_aspect(
    bbox: list[float], kind: str = "map", override: str | None = None
) -> tuple[float, list[float]]:
    """Pick a standard card ratio for the scenario, trim the bbox to hit it.

    Swipe compares and 3D vistas force landscape 3:2 (the divider and the
    horizon both need width); everything else snaps to the nearest standard.
    The trim is central, so the frame stays data and the subject stays put.
    Returns (height/width, trimmed_bbox).
    """
    import math

    if override is not None and override not in _CARD_RATIOS:
        raise ValueError(f"aspect must be one of {sorted(_CARD_RATIOS)}, got {override!r}")
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    cos = max(math.cos(math.radians(cy)), 1e-9)
    dlon = max(bbox[2] - bbox[0], 1e-9)
    dlat = max(bbox[3] - bbox[1], 1e-9)
    aspect = dlat / (dlon * cos)
    if override:
        ratio = _CARD_RATIOS[override]
    elif kind in ("compare", "3d"):
        ratio = _CARD_RATIOS["3:2"]
    else:
        ratio = min(_CARD_RATIOS.values(), key=lambda r: abs(r - aspect))
    if aspect > ratio:  # too tall for the shape: trim latitude
        dlat = ratio * dlon * cos
    else:  # too wide: trim longitude
        dlon = dlat / (ratio * cos)
    return ratio, [cx - dlon / 2, cy - dlat / 2, cx + dlon / 2, cy + dlat / 2]


def _snapshot_artifact(
    html_path: str, width: int = 1200, height: int = 900, fit_bbox: list[float] | None = None
) -> bytes:
    """Rasterize an artifact for a postcard: headless Chromium, chrome hidden.

    Waits for every MapLibre map on the page (window.gsMaps) to finish
    loading tiles, auto-clicks "Load full coverage" when present, settles,
    shoots. Raises RuntimeError with the exact install command when the
    browser runtime is missing — callers turn that into a note, never a
    broken artifact.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "snapshot cards need playwright: uv add playwright && uv run playwright install chromium"
        )
    with sync_playwright() as pw:
        # playwright pins a chromium BUILD per package version, so a browser
        # installed by another venv on a different playwright won't satisfy this
        # one. Rather than make everyone download ~100MB to take a screenshot,
        # fall back to a Chromium-family browser they already have. Bundled
        # first, because it is the reproducible one.
        browser = None
        for kwargs in ({}, {"channel": "chromium"}, {"channel": "chrome"}, {"channel": "msedge"}):
            try:
                browser = pw.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError(
                "no Chromium-family browser found. Install Google Chrome, or run "
                "`uv run playwright install chromium --only-shell` once "
                "(--only-shell is enough for stills and is a much smaller download)"
            )
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(Path(html_path).resolve().as_uri() + "#clean")
            # a postcard has no MapLibre map — its pixels are already embedded —
            # so requiring gsMaps to exist hangs until timeout on a page that
            # was ready immediately
            # map.loaded() turns true before the "load" handler has added the raster
            # layers, so a shot on it alone is the base map (field test 11, cards 18 and 19)
            loaded = ("!window.gsMaps || (!window.gsPending && "
                      "window.gsMaps.every(m => m.loaded() && m.areTilesLoaded()))")
            page.wait_for_function(loaded, timeout=90000)
            # re-fit to the card's bbox, keeping any pitch. Flat maps get zero
            # padding (the load-time fit pads 40px and leaks area beyond the
            # data); pitched maps reserve top headroom, because fitBounds fits
            # the GROUND footprint and relief rises above it — without the
            # reserve a peak crops out the top of the frame.
            # ponytail: fixed 28% headroom — terrain-aware framing (sample
            # queryTerrainElevation, project the summit) if a case defeats it
            bb = json.dumps(fit_bbox) if fit_bbox else "BBOX"
            page.evaluate(
                "(window.gsMaps || []).forEach(m => { try { const B = " + bb + "; "
                "const head = m.getPitch() > 0 ? Math.round(m.getContainer().clientHeight * 0.28) : 0; "
                "m.fitBounds([[B[0], B[1]], [B[2], B[3]]],"
                "{ padding: { top: head, left: 0, right: 0, bottom: 0 },"
                "  duration: 0, pitch: m.getPitch(), bearing: m.getBearing() }"
                ") } catch (e) {} })"
            )
            page.wait_for_function(loaded, timeout=90000)
            if page.evaluate("!!document.getElementById('loadmore')"):
                page.evaluate("document.getElementById('loadmore').click()")
                page.wait_for_function(loaded, timeout=90000)
            page.wait_for_timeout(700)  # let the last tiles paint
            return page.screenshot()
        finally:
            browser.close()


# only a tool that ran a geocode sets this (compare_dates); a caller's stack_facts never can
_tool_geocoded: contextvars.ContextVar[bool | str] = contextvars.ContextVar("tool_geocoded", default=False)


def _map_stack_facts(
    resolved: list[dict[str, Any]],
    layers: list[dict[str, Any]],
    extra_facts: dict[str, Any] | None = None,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """The honest facts of a map render — shared by the panel and its postcard."""
    items = [l for l in layers if l.get("type") == "item" and l.get("catalog") and l.get("collection_id")]
    by_catalog: dict[str, list[str]] = {}
    for l in items:
        cols = by_catalog.setdefault(l["catalog"], [])
        if l["collection_id"] not in cols:
            cols.append(l["collection_id"])
    hosts = set()
    for r in resolved:
        parts = r.get("tiles", "").split("/") if r.get("type") == "raster" else []
        if len(parts) > 2 and parts[0] in ("http:", "https:") and parts[2]:
            hosts.add(parts[2])
    return {
        "catalogs": sorted(by_catalog),
        "collections_by_catalog": by_catalog,
        "tiler_hosts": sorted(hosts),
        "maplibre": True,  # both map artifacts render through MapLibre
        # facts this function can't see default to false — callers that
        # actually geocoded, fetched events, or draped terrain say so via
        # extra_facts; the panel understates rather than fabricates
        "terrain": False,
        "geocoded": False,
        "events": False,
        "weather": False,
        "passes": False,
        "snapshot": False,
        **{k: v for k, v in (extra_facts or {}).items() if k != "geocoded"},
        "geocoded": _geocoded_fact(extra_facts, bbox),
    }


_NOTES_CSS = (
    "<style>#notes { position: absolute; bottom: 60px; left: 12px; z-index: 2; max-width: min(320px, 80vw); "
    "background: rgba(247,244,239,.96); color: var(--ink); border-top: 3px solid var(--accent); border-radius: 4px; "
    "padding: 8px 12px; font: 12.5px/1.45 \"Roboto\", system-ui, sans-serif; box-shadow: 0 1px 6px rgba(0,0,0,.18); } "
    "#notes b { display: block; font: 700 10px/1 \"Roboto Mono\", monospace; letter-spacing: .14em; "
    "text-transform: uppercase; color: var(--mid); margin-bottom: 6px; } #notes div { margin: 0 0 4px; }</style>"
)


def _note_lines(notes: list[str] | None) -> list[str]:
    return [str(n) for n in (notes or []) if str(n).strip()]


def _notes_html(notes: list[str] | None) -> str:
    """The caller's own free-text notes as one box, escaped like every other string on the page."""
    lines = _note_lines(notes)
    if not lines:
        return ""
    return (
        f'{_NOTES_CSS}<aside id="notes" aria-label="Notes"><b>Notes</b>'
        + "".join(f"<div>{escape(n)}</div>" for n in lines)
        + "</aside>"
    )


_KNOWN_STACK_FACTS = {
    "catalogs", "collections_by_catalog", "collections", "tiler_hosts", "maplibre",
    "terrain", "geocoded", "events", "weather", "passes", "snapshot", "mosaic_scenes",
}


def _notes_result(out: dict[str, Any], notes: list[str] | None, stack_facts: dict[str, Any] | None,
                  bbox: list[float] | None = None) -> None:
    """Hand the notes back, and name stack_facts keys the panel can't use instead of dropping them."""
    if notes:
        out["notes"] = notes
    bad = [k for k in (stack_facts or {}) if k not in _KNOWN_STACK_FACTS or (k == "geocoded" and not _geocoded_fact(stack_facts, bbox))]
    if bad:
        out["stack_facts_ignored"] = bad
        extra = "stack_facts takes provenance flags only (events, weather, passes, terrain, snapshot; geocoded is kept only for a bbox geocode returned, with the source it returned); put free text in notes"
        out["note"] = f'{out["note"]}. {extra}' if out.get("note") else extra


def _stack_panel_html(entries: list[dict[str, Any]]) -> str:
    from groundstation.stack import STAGE_LABELS

    parts, last = [], None
    for e in entries:
        if e["stage"] != last:
            parts.append(f'<div class="stack-group">{STAGE_LABELS[e["stage"]]}</div>')
            last = e["stage"]
        # escape everything — instance lines carry caller-supplied collection
        # ids, and even curated stack.md text shouldn't be able to break markup
        # ponytail: click-to-expand via native <details>, not hover — hover has
        # no touch equivalent and details is keyboard-accessible for free
        spk = e.get("speaks-to", "")
        from groundstation.stack import DS_OWN

        role_cls = "role ds" if e["ds-role"] in DS_OWN else "role"
        island = f'<span class="island" title="Development Seed island">{escape(e["island"])}</span>' if e.get("island") else ""
        parts.append(
            f'<details class="stack-entry"><summary><span class="dot k-{e["stage"]}"></span><span class="s-head">'
            f'<span class="stack-name">{escape(e["name"])}<span class="{role_cls}">{escape(e["ds-role"])}</span>{island}</span>'
            f'<span class="inst">{escape(e["instance"])}</span></span></summary>'
            f'<div class="more"><p>{escape(e["what"])}</p>'
            + (f'<div class="spk">speaks to {escape(spk)}</div>' if spk else "")
            + f'<a href="{escape(e["link"], quote=True)}" target="_blank" rel="noopener">{escape(e["link"].split("//")[-1])}</a>'
            f"</div></details>"
        )
    return "".join(parts)


def _stack_chunk_for(
    resolved: list[dict[str, Any]],
    layers: list[dict[str, Any]],
    extra_facts: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    bbox: list[float] | None = None,
) -> str | None:
    from groundstation import stack as _stack

    try:
        components = _stack.parse_stack()
    except (FileNotFoundError, ValueError):
        # a malformed curated file must not take the map down with it — the
        # parser stays loud (evals hit it directly), the artifact stays alive
        return None
    entries = _stack.stack_instances(components, _map_stack_facts(resolved, layers, extra_facts, bbox))
    if not entries:
        return None
    note_p = "".join(f"<p>{escape(n)}</p>" for n in _note_lines(notes))
    notes_group = f'<div class="stack-group">Notes</div>{note_p}' if note_p else ""
    return _fill(_STACK_CHUNK, {"ENTRIES": notes_group + _stack_panel_html(entries), "SUMMARY": escape(_stack.summary(entries))})


def _artifact_postcard(
    artifact_path: str,
    postcard: dict[str, Any],
    facts: dict[str, Any],
    layers: list[dict[str, Any]],
    stack_layer: bool,
    bbox: list[float] | None = None,
    kind: str = "map",
) -> tuple[str | None, str | None]:
    """Snapshot a rendered artifact into a share card. Returns (card_path, note).

    The card inherits the artifact's own stack facts, so it claims exactly
    what the view exercised — MapLibre, terrain, events, the lot — instead
    of anyone re-typing the story.
    """
    import datetime as dt

    if bbox:
        # loud on a bad aspect string — that's caller input, not runtime luck
        ratio, fit_bbox = _snap_aspect(bbox, kind, postcard.get("aspect"))
    else:
        ratio, fit_bbox = 3 / 4, None
    try:
        png = _snapshot_artifact(artifact_path, 1200, int(1200 * ratio), fit_bbox)
    except RuntimeError as e:
        return None, str(e)
    except Exception as e:
        return None, f"postcard snapshot failed ({e.__class__.__name__}) — artifact is fine"
    items = [l for l in layers if l.get("type") == "item"]
    cols = sorted({l["collection_id"] for l in items if l.get("collection_id")})
    cats = sorted({l["catalog"] for l in items if l.get("catalog") in CATALOGS})
    source = ", ".join(CATALOGS[c]["notes"].split(":")[0] for c in cats) or "OpenStreetMap · CARTO basemap"
    html = _postcard_html(
        png,
        postcard["place"],
        postcard.get("date") or dt.date.today().isoformat(),
        ", ".join(cols) or "interactive view",
        source,
        postcard.get("caption", ""),
        None,
        _stack_credit_html({**facts, "snapshot": True}) if stack_layer else None,
        engine="STAC · MapLibre GL" if items else "MapLibre GL",
    )
    try:
        card_path = _artifact_path(postcard.get("out_path"), "postcard", postcard["place"])
    except ValueError as e:
        return None, f"postcard skipped: {e}"
    _write_artifact(card_path, html)
    return card_path, None


def render_map(
    title: str,
    bbox: list[float],
    layers: list[dict[str, Any]],
    subtitle: str = "",
    out_path: str | None = None,
    compare: bool | None = None,
    stack_layer: bool = True,
    stack_facts: dict[str, Any] | None = None,
    postcard: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Write a self-contained interactive HTML map and return its file path.

    layers is a list of:
      {"type": "item", "name": ..., "catalog": ..., "collection_id": ...,
       "item_id": ..., "assets": [...], "rescale": "0,3000", "colormap_name": ...,
       # no rescale on a continuous one-asset layer: stretched 2nd to 98th
       # percentile of the raw values over bbox, one range shared by every
       # scene of that collection and asset on the map; rescale_used /
       # rescale_basis per layer in state.layers say what was used
       "expression": "(nir-red)/(nir+red)",  # expression for index layers
       "bbox": [w, s, e, n]}  # pass the item's bbox when you have it (from
                              # search results) — it skips a STAC re-fetch
      {"type": "raster", "name": ..., "tiles": "https://..{z}/{x}/{y}.."}
      {"type": "geojson", "name": ..., "data": <FeatureCollection>, "color": "#hex"}
      {"type": "coverage_set", "set": <full_coverage_set from search_imagery>,
       "name": "True colour",  # optional prefix, tiles get N/S/E/W-ish suffixes
       "assets"/"expression"/"colormap_name"/"rescale"/"opacity": ...}
        — expands to one item layer per scene in the set. Pass search_imagery's
        full_coverage_set object straight through instead of hand-unrolling its
        items; hand-unrolling is how scenes get dropped and maps ship holes.
    Item layers resolve to the right tiling backend automatically. The HTML
    is shareable: MapLibre + live tile URLs, no server of ours required.

    compare: True renders two raster layers as a draggable swipe (before/after),
    False stacks them as toggleable overlays. Default None auto-decides: swipe
    only when both rasters come from the SAME collection (a comparison), overlay
    when they differ (e.g. burn severity over imagery — pass opacity ~0.75).

    stack_layer: on by default, a "Stack" toggle revealing the technologies behind
    the map — the actual collections, tiler, formats, and buckets on screen,
    joined from docs/stack.md. Attribution to projects, never people.
    stack_facts: honest extras the panel can't see from the layers alone —
    {"geocoded": <geocode's source>} is kept only when bbox is one geocode returned with that
    source, else it is named in stack_facts_ignored. Pass {"events": True} if an events layer came from active_events, and
    {"passes": True} if next_pass informed the answer. Only claim
    what actually happened.

    postcard: {"place": ..., "date": ..., "caption": ..., "out_path": ...}
    also bakes a share card that shows THIS view — swipe divider, event
    dots, overlays — via a headless-browser snapshot of the artifact.
    Needs playwright + chromium once; without them you get the artifact
    plus a postcard_note with the install command. Use when the view is
    the story; a plain one-scene imagery card is render_postcard's job.
    """
    layers = _shared_stretch([x for l in layers for x in _expand_coverage_set(l)], bbox)
    resolved = []
    raster_collections: list[str | None] = []
    for l in layers:
        if l.get("type") == "item":
            raster_collections.append(l.get("collection_id"))
        elif l.get("type") == "raster":
            raster_collections.append(None)
        resolved.append(_resolve_layer(l, bbox))
    if compare is None:
        # swipe only for a true comparison: two rasters of the same collection
        # whose footprints actually overlap. Same-collection alone is NOT enough
        # — an auto-mosaic full_coverage_set is also two rasters of one
        # collection, and swiping it puts each tile on its own side of the
        # divider, so half the AOI is blank at every slider position
        compare = (
            len(raster_collections) == 2
            and raster_collections[0] is not None
            and raster_collections[0] == raster_collections[1]
            and _footprints_overlap(resolved)
        )
    stack_html, stack_note = "", None
    if stack_layer:
        stack_html = _stack_chunk_for(resolved, layers, stack_facts, notes, bbox)
        if stack_html is None:
            stack_html, stack_note = "", "stack layer skipped: docs/stack.md missing, malformed, or nothing to attribute"
    legends = _legends_for(layers)
    first = next((l for l in layers if l.get("type") == "item"), None)
    stac_map_url = first and stac_map_link(first["catalog"], first["collection_id"], first["item_id"])
    _boxes = [_layer_shape(l) for l in resolved if l.get("type") == "raster" and l.get("bounds")]
    _cov = _union_coverage_pct(bbox, _boxes) if _boxes else None
    _unc = _uncovered_side(bbox, _boxes) if _boxes and _cov < PARTIAL_COVERAGE_PCT else None
    subtitle = _with_wedge(subtitle, _unc)
    html = (
        _fill(_MAP_TEMPLATE, {
            "BRAND": _BRAND_CSS, "TITLE": escape(title), "SUBTITLE": escape(subtitle),
            "LAYERS": _js(resolved), "BBOX": _js(bbox), "COMPARE": _js(compare),
            "TLBAR": "", "TIMELAPSE": "false", "INTERVAL": "0",
            "STACMAP": f'<a id="stacmap" href="{escape(stac_map_url)}" target="_blank">Open in stac-map</a>' if stac_map_url else "",
            "STACK": stack_html + _notes_html(notes), "LEGEND": _legend_map_html(legends),
        })
    )
    try:
        out_path = _artifact_path(out_path, "map", title)
    except ValueError as e:
        return {"error": str(e)}
    _write_artifact(out_path, html)
    # the scenes of one set share a name, so the caller's list carries the id too
    label = lambda l: f'{l["name"]} · {l["detail"]}' if l.get("detail") else l["name"]
    out = {"map_path": out_path, "layers": [label(l) for l in resolved]}
    # The map's contents were previously legible only to a human opening the
    # file, so every rendering bug this repo has shipped was caught by eye and
    # not by a check. `state` is the same thing the HTML was built from, in a
    # form an eval (or an MCP Apps view) can read. The file is unchanged.
    if legends:
        out["legend"] = legends[0]
        if len(legends) > 1:
            out["legends"] = legends
    out["state"] = {
        "bbox": bbox,
        "compare": bool(compare),
        "layers": [_layer_state(l) for l in resolved],
    }
    if any(l.get("fallback_tiles") for l in resolved):
        out["state"]["fallback"] = "raster.eoapi.dev"
    if stac_map_url:
        out["stac_map_url"] = stac_map_url
    if stack_note:
        out["note"] = stack_note
    _notes_result(out, notes, stack_facts, bbox)
    fb = [label(l) for l in resolved if l.get("pc_fallback")]
    if fb:
        out["note"] = (out["note"] + ". " if out.get("note") else "") + (
            f"Planetary Computer mosaic registration timed out for {', '.join(fb)}; item tiles are used "
            "instead, which may render empty for reprojection-heavy sources (MODIS, GOES)"
        )
    # A map whose layers do not fill the view has a hole in it, and the hole is
    # invisible from the caller's side — the render "succeeded". Say it here, or
    # it ships in a demo. search_imagery already hands back full_coverage_set
    # for exactly this; the usual cause is rendering only its first item.
    # A map with no raster at all draws vectors on nothing. Field test No.6's
    # storm-track card produced a 10 KB still that was two dots on blank, and
    # only a human noticing the file size caught it.
    if not any(l.get("type") == "raster" for l in resolved):
        out["coverage_note"] = (
            "no imagery layer on this map, so it renders as vectors on a blank background. "
            "Add a scene, or say plainly that none is available for this area"
        )
    if _boxes:
        out["state"]["coverage_pct"] = _cov
        if _unc:
            out["state"]["uncovered_pct"], out["state"]["uncovered_side"] = _unc
            out["coverage_note"] = (
                f"layers cover {_cov:.0f}% of the requested view ({_hole_text(bbox, _boxes)}), so the rest renders blank. "
                "Pass the whole full_coverage_set from search_imagery, or tighten bbox to what you have"
            )
    if postcard:
        card_path, card_note = _artifact_postcard(
            out_path, postcard, _map_stack_facts(resolved, layers, stack_facts, bbox), layers, stack_layer,
            bbox, "compare" if compare else "map",
        )
        if card_path:
            out["postcard_path"] = card_path
        if card_note:
            out["postcard_note"] = card_note
    return out


_TIMELAPSE_BAR = (
    '<div id="tl"><button id="tl-prev" aria-label="previous frame">\u25c0</button>'
    '<button id="tl-play" aria-label="play or pause">\u25b6</button>'
    '<button id="tl-next" aria-label="next frame">\u25b6\u25b6</button><span id="tl-name"></span></div>'
)
TIMELAPSE_MAX_FRAMES = 12  # hard ceiling: every frame is a tile layer, and bandwidth is the cost


def render_timelapse(
    title: str,
    bbox: list[float],
    frames: list[dict[str, Any]],
    subtitle: str = "",
    out_path: str | None = None,
    interval_ms: int = 1200,
    max_frames: int = 8,
    stack_layer: bool = True,
    stack_facts: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """A few frames of one place over time, with prev / play / next controls.

    frames: layers in render_map's shape (item or raster), one per date, in time
    order; each frame's "name" is its date or label. Only the first frame loads
    tiles up front, the rest load when shown, so keep it to a few frames: more
    than max_frames (hard ceiling 12) is an error. Use for 3 or more dates, a
    season or a progression; for two dates use render_map (the swipe).
    Returns {map_path, frames, state}; the frames are the caller's, so a
    window that found 0 scenes at the cloud cap is the caller's to name.
    """
    cap = min(max_frames, TIMELAPSE_MAX_FRAMES)
    if len(frames) < 2:
        return {"error": "a timelapse needs at least 2 frames; use render_map for one scene"}
    if len(frames) > cap:
        return {"error": f"{len(frames)} frames is over the cap of {cap}: every frame is a tile layer, "
                         "so pick fewer, evenly spaced windows (hard ceiling 12)"}
    if any(f.get("type") not in ("item", "raster") for f in frames):
        return {"error": "each frame must be one item or raster layer with a name"}
    resolved = [_resolve_layer(f, bbox) for f in _shared_stretch(frames, bbox)]
    stack_html = ""
    if stack_layer:
        stack_html = _stack_chunk_for(resolved, frames, stack_facts, notes, bbox) or ""
    html = (
        _fill(_MAP_TEMPLATE, {
            "BRAND": _BRAND_CSS, "TITLE": escape(title), "SUBTITLE": escape(subtitle),
            "LAYERS": _js(resolved), "BBOX": _js(bbox), "COMPARE": "false",
            "TLBAR": _TIMELAPSE_BAR, "TIMELAPSE": "true", "INTERVAL": str(int(interval_ms)),
            "STACMAP": "", "STACK": stack_html + _notes_html(notes), "LEGEND": "",
        })
    )
    try:
        out_path = _artifact_path(out_path, "timelapse", title)
    except ValueError as e:
        return {"error": str(e)}
    _write_artifact(out_path, html)
    out = {
        "map_path": out_path,
        "frames": len(resolved),
        "state": {"bbox": bbox, "interval_ms": int(interval_ms), "layers": [_layer_state(l) for l in resolved]},
    }
    _notes_result(out, notes, stack_facts, bbox)
    return out


# ---------------------------------------------------------------- 3D artifact

TERRAIN_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

# click-to-load gap fillers — injected only when extra_layers exist, so the
# default 3D artifact carries zero extra bytes and requests zero extra tiles
_EXTRA_CHUNK = """<button id="loadmore">Load full coverage (__N__ more)</button>
<script>
  const EXTRAS = __EXTRAS__;
  document.getElementById("loadmore").addEventListener("click", (ev) => {
    // the button is the progress indicator: tiles can arrive slowly from a
    // busy tiler, and a silently vanishing button reads as "nothing happened"
    const btn = ev.target;
    btn.disabled = true;
    btn.textContent = "Loading " + EXTRAS.length + " scene" + (EXTRAS.length > 1 ? "s" : "") + "…";
    EXTRAS.forEach((e, i) => {
      const id = "extra" + i;
      map.addSource(id, { type: "raster", tiles: [originKey(e.tiles)], tileSize: 256,
        ...(e.bounds ? { bounds: e.bounds } : {}) });
      // beneath the main drape: gap fillers must never cover the best scene
      map.addLayer({ id, type: "raster", source: id,
        paint: { "raster-opacity": e.opacity } }, "imagery");
      if (e.bounds) {
        // dashed outline = where pixels are about to land; cleared on idle
        const [w, s, x, n] = e.bounds;
        map.addLayer({ id: id + "-pending", type: "line",
          source: { type: "geojson", data: { type: "Feature", properties: {}, geometry: {
            type: "Polygon", coordinates: [[[w, s], [x, s], [x, n], [w, n], [w, s]]] } } },
          paint: { "line-color": "#CF3F02", "line-width": 1.5, "line-dasharray": [2, 2] } });
      }
    });
    map.once("idle", () => {
      EXTRAS.forEach((e, i) => {
        if (map.getLayer("extra" + i + "-pending")) map.removeLayer("extra" + i + "-pending");
      });
      btn.remove();
    });
  });
</script>
"""

_MAP3D_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js" integrity="sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznwA0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj" crossorigin="anonymous"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" integrity="sha384-MinO0mNliZ3vwppuPOUnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V" crossorigin="anonymous">
<style>
__BRAND__
  html, body, #map { margin: 0; height: 100%; }
  #panel { position: absolute; top: 12px; left: 12px; z-index: 2; background: rgba(247,244,239,.96);
    color: var(--ink); border-top: 3px solid var(--accent);
    padding: 12px 16px; border-radius: 0 0 10px 10px; font: 14px/1.45 "Roboto", system-ui, sans-serif; max-width: 340px;
    box-shadow: 0 2px 10px rgba(0,0,0,.18); }
  #panel h1 { font: 700 15px/1.25 "Roboto Condensed", "Roboto", system-ui, sans-serif; margin: 0 0 4px; }
  #panel p { margin: 4px 0 8px; color: var(--mid); }
  #panel label { display: block; margin: 6px 0 2px; color: var(--mid); }
  #exaggeration { width: 100%; accent-color: var(--accent); }
  #panel button { font: 13px "Roboto", system-ui, sans-serif; background: var(--accent); color: #fff; border: 0;
    border-radius: 6px; padding: 6px 12px; margin: 6px 6px 0 0; cursor: pointer; }
  #panel button:hover { background: var(--ink); }
  #panel button:disabled { background: var(--mid); cursor: default; }
  #credit { position: absolute; bottom: 24px; left: 12px; z-index: 2; font: 11px "Roboto Mono", monospace;
    color: var(--mid); background: rgba(247,244,239,.85); padding: 2px 8px; border-radius: 6px; }
  .clean #panel, .clean #credit, .clean #stack-toggle, .clean #stack,
  .clean .maplibregl-ctrl-top-right { display: none !important; }
</style>
<script>if (location.hash === "#clean") document.documentElement.classList.add("clean");</script>
</head><body>
<div id="map"></div>
<div id="panel"><h1>__TITLE__</h1><p>__SUBTITLE__</p>
  <label>Terrain exaggeration: <span id="exagValue">__EXAGGERATION__</span>&times;</label>
  <input id="exaggeration" type="range" min="1" max="3" step="0.1" value="__EXAGGERATION__">
  <button id="flythrough">Fly through</button><button id="reset">Reset view</button>
__EXTRA__
</div>
<div id="credit">groundstation · Development Seed labs · STAC + TiTiler · terrain: AWS Terrarium (open)</div>
__STACK__
<script>
const STYLE = __STYLE__;
// CloudFront on titiler.xyz caches the CORS header with the tile and does not
// vary by origin, so the URL must differ per origin or a cached tile is refused
const originKey = (u) => u + (u.includes("?") ? "&" : "?") + "o=" + (location.origin === "null" ? "sandbox" : location.origin.replace(/[^a-z0-9]/gi, "").slice(-16));
STYLE.sources.imagery.tiles = STYLE.sources.imagery.tiles.map(originKey);
const BBOX = __BBOX__;
const CENTER = [(BBOX[0] + BBOX[2]) / 2, (BBOX[1] + BBOX[3]) / 2];
const BOUNDS = [[BBOX[0], BBOX[1]], [BBOX[2], BBOX[3]]];
let exaggeration = __EXAGGERATION__;

const map = new maplibregl.Map({ container: "map", style: STYLE, bounds: BOUNDS,
  fitBoundsOptions: { padding: 40 }, maxPitch: 85 });
window.gsMaps = [map];  // exposed for scripted screenshots and checks
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
map.on("load", () => {
  map.setTerrain({ source: "dem", exaggeration });
  // MapLibre 4.7 easeTo freezes the camera's ground height and unfreezes it only with freezeElevation,
  // so without it the height stays at the 0 m read before any DEM tile and a tight box sits under the relief
  map.easeTo({ center: CENTER, pitch: 60, duration: 0, freezeElevation: true });
});

const slider = document.getElementById("exaggeration");
slider.addEventListener("input", () => {
  exaggeration = parseFloat(slider.value);
  document.getElementById("exagValue").textContent = exaggeration.toFixed(1);
  map.setTerrain({ source: "dem", exaggeration });
});

// orbit by nudging bearing each frame — easeTo/flyTo per step fights the
// user's own camera input and stutters on tile loads
const btn = document.getElementById("flythrough");
let frame = null;
const step = () => { map.setBearing(map.getBearing() + 0.08); frame = requestAnimationFrame(step); };
const stop = () => { if (frame) cancelAnimationFrame(frame); frame = null; btn.textContent = "Fly through"; };
btn.addEventListener("click", () => {
  if (frame) return stop();
  map.easeTo({ center: CENTER, pitch: 60, duration: 1500 });
  btn.textContent = "Stop";
  frame = requestAnimationFrame(step);
});
document.getElementById("reset").addEventListener("click", () => {
  stop();
  map.fitBounds(BOUNDS, { padding: 40, pitch: 60, bearing: 0 });
});
</script></body></html>
"""


def render_map_3d(
    title: str,
    bbox: list[float],
    layer: dict[str, Any],
    subtitle: str = "",
    out_path: str | None = None,
    exaggeration: float = 1.5,
    stack_layer: bool = True,
    extra_layers: list[dict[str, Any]] | None = None,
    postcard: dict[str, Any] | None = None,
    stack_facts: dict[str, Any] | None = None,
    fill_coverage: bool = True,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Write a self-contained 3D terrain fly-through with imagery draped over it.

    layer is ONE layer in render_map's shape — either
      {"type": "item", "catalog": ..., "collection_id": ..., "item_id": ...,
       "assets": [...], "bbox": [w, s, e, n]}
    or {"type": "raster", "tiles": "https://..{z}/{x}/{y}.."}.
    Terrain comes from the keyless AWS Terrarium elevation tiles, so the HTML
    is shareable as-is. exaggeration (1.0-3.0) sets the vertical stretch; the
    artifact carries a live slider, a fly-through orbit, and a reset button.
    Best over relief-rich areas — pick the lowest-cloud recent scene first.

    stack_layer: the same "Stack" toggle as render_map (on by default), and here the panel also
    names the Terrarium terrain actually under the imagery.

    extra_layers: more layers in the same shape, embedded but not requested
    until the viewer clicks "Load full coverage". Pass the REST of a same-day
    full_coverage_set when the best scene clips the AOI (covers_aoi_pct
    below ~95) — the gaps become one click away without loading tiles nobody
    asked for. They drape beneath the main scene, so it always wins.

    fill_coverage: when the main item layer clips the view and no extra_layers
    were passed, the same-day items of its collection that fill the gap drape
    on load beneath it, named in state["layers"] and "filled_with". False turns
    the search off and renders the main layer alone.

    postcard: {"place": ..., "date": ..., "caption": ...} bakes a share card
    of the actual 3D view — pitched terrain, drape, extras auto-loaded —
    via a headless-browser snapshot. Same playwright/chromium requirement
    as render_map's postcard option.
    """
    # ponytail: one draped layer; add a second when a real ask needs it
    resolved = _resolve_layer(layer)
    siblings: list[dict[str, Any]] = []
    sibling_specs: list[dict[str, Any]] = []
    if (fill_coverage and not extra_layers and layer.get("type") == "item"
            and resolved.get("bounds")
            and _union_coverage_pct(bbox, [_layer_shape(resolved)]) < PARTIAL_COVERAGE_PCT):
        try:
            cat, coll = layer["catalog"], layer["collection_id"]
            day = ((_item(cat, coll, layer["item_id"]) or {}).get("properties", {}).get("datetime") or "")[:10]
            found = search_imagery(cat, [coll], bbox=bbox, datetime_range=f"{day}T00:00:00Z/{day}T23:59:59Z",
                                   limit=10) if day else {}
            fcs = find_full_coverage_set(found.get("items") or [], bbox, threshold=PARTIAL_COVERAGE_PCT)
            # siblings draw on the main scene's stretch, so the drape has no seam
            share = ({"rescale": resolved["rescale_used"], "rescale_basis": resolved["rescale_basis"]}
                     if resolved.get("rescale_used") else {})
            for it in (fcs or {}).get("items", []):
                if it["id"] != layer["item_id"]:
                    spec = {**layer, **share, "name": it["id"], "item_id": it["id"], "bbox": it.get("bbox"),
                            **({"footprint": it["footprint"]} if it.get("footprint") else {})}
                    sibling_specs.append(spec)
                    siblings.append({**_resolve_layer(spec), "reason": "same-day sibling filling the view"})
        except Exception:
            siblings, sibling_specs = [], []  # search failed: render the main layer alone, as before
    style = {
        "version": 8,
        "sources": {
            # the 3D style is built here rather than fetched, so it keeps a raster
            # basemap; CARTO's now watermarks, OSM's standard tiles do not (light use)
            "basemap": {
                "type": "raster",
                "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                "tileSize": 256,
                "attribution": "&copy; OpenStreetMap contributors",
            },
            "imagery": {
                "type": "raster",
                "tiles": [resolved["tiles"]],
                "tileSize": 256,
                **({"bounds": resolved["bounds"]} if resolved.get("bounds") else {}),
            },
            "dem": {
                "type": "raster-dem",
                "tiles": [TERRAIN_TILES],
                "tileSize": 256,
                "encoding": "terrarium",
                "maxzoom": 15,
            },
        },
        "layers": [
            {"id": "basemap", "type": "raster", "source": "basemap"},
            {
                "id": "imagery",
                "type": "raster",
                "source": "imagery",
                "paint": {"raster-opacity": resolved.get("opacity", 1)},
            },
        ],
    }
    for i, sb in enumerate(siblings):
        # beneath "imagery" so the main scene always wins
        style["sources"][f"sibling{i}"] = {
            "type": "raster", "tiles": [sb["tiles"]], "tileSize": 256,
            **({"bounds": sb["bounds"]} if sb.get("bounds") else {}),
        }
        style["layers"].insert(-1, {"id": f"sibling{i}", "type": "raster", "source": f"sibling{i}"})
    extras = [_resolve_layer(l) for l in (extra_layers or [])]
    extra_html = ""
    if extras:
        extra_html = _fill(_EXTRA_CHUNK, {"N": str(len(extras)), "EXTRAS": _js([
            {"tiles": e["tiles"], "opacity": e.get("opacity", 1),
             **({"bounds": e["bounds"]} if e.get("bounds") else {})}
            for e in extras
        ])})
    stack_html, stack_note = "", None
    if stack_layer:
        stack_html = _stack_chunk_for([resolved, *siblings, *extras], [layer, *sibling_specs, *(extra_layers or [])], {**(stack_facts or {}), "terrain": True}, notes, bbox)
        if stack_html is None:
            stack_html, stack_note = "", "stack layer skipped: docs/stack.md missing, malformed, or nothing to attribute"
    _drape = [_layer_shape(resolved), *(_layer_shape(s) for s in siblings if s.get("bounds"))] if resolved.get("bounds") else []
    _cov = _union_coverage_pct(bbox, _drape) if _drape else None
    _unc = _uncovered_side(bbox, _drape) if _drape and _cov < PARTIAL_COVERAGE_PCT else None
    subtitle = _with_wedge(subtitle, _unc)
    html = (
        _fill(_MAP3D_TEMPLATE, {
            "BRAND": _BRAND_CSS, "TITLE": escape(title), "SUBTITLE": escape(subtitle),
            "STYLE": _js(style), "BBOX": _js(bbox),
            "EXAGGERATION": json.dumps(round(float(exaggeration), 2)),
            "EXTRA": extra_html, "STACK": stack_html + _notes_html(notes),
        })
    )
    try:
        out_path = _artifact_path(out_path, "map3d", title)
    except ValueError as e:
        return {"error": str(e)}
    _write_artifact(out_path, html)
    out = {"path": out_path, "title": title}
    # Same reasoning as render_map's state, with 3D's own facts: the vertical
    # stretch actually rendered (the template rounds it, so report the rounded
    # value), terrain always present, and extras marked deferred because they
    # do not draw until someone clicks.
    out["state"] = {
        "bbox": bbox,
        "exaggeration": round(float(exaggeration), 2),
        "terrain": True,
        "layers": [_layer_state(resolved), *(_layer_state(s) for s in siblings),
                   *(_layer_state(e, deferred=True) for e in extras)],
    }
    if siblings:
        out["filled_with"] = [s["name"] for s in siblings]
        for ls, sb in zip(out["state"]["layers"][1:], siblings):
            ls["reason"] = sb["reason"]
    # Same blind spot render_map had, but 3D differs twice: only the ONE main
    # layer drapes on load (extras wait behind the button), and an uncovered
    # area shows terrain with no imagery rather than blank. So report what the
    # viewer actually gets first, and what the button would add.
    if resolved.get("bounds"):
        out["state"]["coverage_pct"] = _cov
        if _unc:
            out["state"]["uncovered_pct"], out["state"]["uncovered_side"] = _unc
            _extra_boxes = [_layer_shape(e) for e in extras if e.get("bounds")]
            if _extra_boxes:
                _full = _union_coverage_pct(bbox, [*_drape, *_extra_boxes])
                out["state"]["coverage_pct_loaded"] = _full
                out["coverage_note"] = (
                    f"the drape covers {_cov:.0f}% of the view on load ({_hole_text(bbox, _drape)}), the rest is terrain with no "
                    f"imagery until someone clicks Load full coverage, which takes it to {_full:.0f}%"
                )
            else:
                out["coverage_note"] = (
                    f"the drape covers {_cov:.0f}% of the view ({_hole_text(bbox, _drape)}), the rest renders as terrain with no "
                    "imagery. Pass the rest of a same-day full_coverage_set as extra_layers"
                )
    if stack_note:
        out["note"] = stack_note
    _notes_result(out, notes, stack_facts, bbox)
    if postcard:
        all_layers = [layer, *(extra_layers or [])]
        card_path, card_note = _artifact_postcard(
            out_path, postcard,
            _map_stack_facts([resolved, *extras], all_layers, {**(stack_facts or {}), "terrain": True}, bbox),
            all_layers, stack_layer, bbox, "3d",
        )
        if card_path:
            out["postcard_path"] = card_path
        if card_note:
            out["postcard_note"] = card_note
    return out


# ---------------------------------------------------------------- postcards

_POSTCARD_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__PLACE__ · __DATE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
__BRAND__
  body { margin: 0; padding: 28px 16px; background: var(--paper); box-sizing: border-box;
    font: 15px/1.5 "Roboto", system-ui, sans-serif; color: var(--ink);
    min-height: 100vh; display: grid; place-items: center; }
  /* the card is a postcard spread: picture beside the writing when the
     viewport allows, wrapped above it when narrow — and the picture always
     fits the screen (86vh cap; the ink mat letterboxes odd aspects) */
  .card { display: flex; flex-wrap: wrap; max-width: min(1180px, 96vw); background: #fff;
    border-radius: 12px; border-top: 3px solid var(--accent);
    overflow: hidden; box-shadow: 0 2px 14px rgba(0,0,0,.15); }
  .fig { flex: 1 1 420px; min-width: min(420px, 100%); background: var(--ink); display: flex; }
  .card img { display: block; width: 100%; height: auto; max-height: 86vh;
    object-fit: contain; margin: auto; }
  .body { flex: 1 1 300px; padding: 20px 24px 22px; display: flex; flex-direction: column; }
  .credit { margin-top: auto; }
  h1 { font: 700 22px/1.2 "Roboto Condensed", "Roboto", system-ui, sans-serif; margin: 0 0 4px; }
  .meta { color: var(--mid); font-size: 14px; margin: 0 0 12px; }
  .caption { margin: 0 0 16px; }
  .credit { border-top: 1px solid var(--rule); padding-top: 12px; font: 12px/1.6 "Roboto Mono", monospace; color: var(--mid); }
  .credit div { margin: 2px 0; }
  .legend { margin: 0 0 14px; font-size: 13px; }
  .legend b { display: block; margin-bottom: 4px; }
  .legend span { display: inline-flex; align-items: center; gap: 5px; margin: 2px 12px 2px 0; }
  .legend i { flex: none; width: 12px; height: 12px; border: 1px solid rgba(0,0,0,.35); border-radius: 2px; }
  .stack-list { margin-top: 8px; font-size: 11px; }
  .stack-list b { color: var(--ink); font-weight: 600; }
  .stack-list b.ds { color: var(--accent); }
</style></head><body>
<div class="card">
  <div class="fig"><img src="data:image/png;base64,__IMAGE__" alt="__PLACE__, __DATE__"></div>
  <div class="body">
    <h1>__PLACE__</h1>
    <p class="meta">__DATE__ · __COLLECTION__</p>
    __CAPTION__
    __LEGEND__
    <div class="credit">
      <div>Development Seed labs · __ENGINE__ · __SOURCE__</div>
      __LICENSE__
      __STACK__
    </div>
  </div>
</div>
</body></html>
"""


def _catalog_source(catalog: str, collection_id: str) -> str:
    # the notes string already leads with the catalog's real name
    name = CATALOGS[catalog]["notes"].split(":")[0]
    return f"{collection_id} via {name}"


def _stack_credit_html(facts: dict[str, Any]) -> str | None:
    """The back-of-postcard stack listing: static credit lines, no links, no toggle.

    Facts-driven so any card tells its own truth — an imagery card claims no
    map engine, a snapshot-of-a-map card claims MapLibre and whatever the
    map exercised. Links stay out on purpose: the card's no-live-URLs
    guarantee covers the credit block too.
    """
    from groundstation import stack as _stack

    try:
        components = _stack.parse_stack()
    except (FileNotFoundError, ValueError):
        return None
    entries = _stack.stack_instances(components, facts)
    if not entries:
        return None
    ds_attr = ' class="ds"'
    lines = "".join(
        f'<div><b{ds_attr if e["ds-role"] in _stack.DS_OWN else ""}>{escape(e["name"])}</b>'
        f" — {escape(e['instance'])}</div>"
        for e in entries
    )
    islands = _stack.islands_exercised(entries)
    intro = "the stack behind this card" + (f" ({escape(', '.join(islands))})" if islands else "") + ":"
    return f'<div class="stack-list"><div>{intro}</div>{lines}</div>'


def _stack_credit_for(
    catalog: str, collection_id: str, tiler_host: str | None, mosaic_scenes: int = 0
) -> str | None:
    return _stack_credit_html({
        "catalogs": [catalog],
        "collections_by_catalog": {catalog: [collection_id]},
        "tiler_hosts": [tiler_host] if tiler_host else [],
        "mosaic_scenes": mosaic_scenes,
    })


def _intersect_bbox(a: list[float], b: list[float]) -> list[float] | None:
    w, s, e, n = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return [w, s, e, n] if w < e and s < n else None


def _mosaic_png(
    catalog: str,
    collection_id: str,
    item_ids: list[str],
    bbox: list[float],
    assets: list[str] | None = None,
    rescale: str | None = None,
    colormap_name: str | None = None,
    expression: str | None = None,
    max_size: int = 1024,
) -> tuple[bytes, int]:
    """Bake one PNG from several scenes with rio-tiler (Development Seed).

    Reads the COGs directly over HTTP range requests — no tiler involved —
    and merges scene by scene, first valid pixel wins. Item order is
    priority order: put the best scene first, fills after.
    """
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    from rio_tiler.io import STACReader
    from rio_tiler.mosaic import mosaic_reader

    base = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/"

    def _read(url: str, bbox: tuple, **kw: Any):
        with STACReader(url) as r:
            return r.part(bbox, **kw)

    # mercator output to match the map views (4326 stretches E-W at mid lat)
    kw: dict[str, Any] = {"bounds_crs": "epsg:4326", "dst_crs": "epsg:3857", "max_size": max_size}
    if expression:
        kw.update(expression=expression, asset_as_band=True)
        rescale = rescale or "-1,1"
    else:
        kw["assets"] = assets or _default_assets(collection_id)
    img, used = mosaic_reader([base + i for i in item_ids], _read, tuple(bbox), **kw)
    if rescale:
        lo, hi = (float(v) for v in rescale.split(","))
        img = img.rescale(((lo, hi),))
    if colormap_name:
        from rio_tiler.colormap import cmap as _cmap

        img = img.apply_colormap(_cmap.get(colormap_name))
    return img.render(img_format="PNG"), len(used)


def _centered_clamp(aoi: list[float], item_bbox: list[float]) -> list[float] | None:
    """Clamp aoi inside item_bbox WITHOUT moving its center.

    A plain intersection trims only the off-scene edges, which drags the
    frame's center away from the subject. Shrink symmetrically instead: the
    subject stays centered, the crop just gets tighter.
    """
    inner = _intersect_bbox(aoi, item_bbox)
    if inner is None:
        return None
    cx, cy = (aoi[0] + aoi[2]) / 2, (aoi[1] + aoi[3]) / 2
    half_x = min(cx - inner[0], inner[2] - cx)
    half_y = min(cy - inner[1], inner[3] - cy)
    if half_x <= 0 or half_y <= 0:  # center is off-scene; nothing to center on
        return inner
    return [cx - half_x, cy - half_y, cx + half_x, cy + half_y]


def _shareable_license(license_: str | None) -> str | None:
    # STAC uses "proprietary"/"various" as its not-an-SPDX-id placeholder, not as
    # a claim about terms — printing it on a share card misinforms, so omit it
    if not license_ or license_.lower() in ("proprietary", "various", "other"):
        return None
    return license_


def _postcard_html(
    png: bytes,
    place: str,
    date: str,
    collection_id: str,
    source: str,
    caption: str = "",
    license_: str | None = None,
    stack_html: str | None = None,
    engine: str = "STAC · TiTiler",
    dropped: list[dict[str, str]] | None = None,
    legends: list[dict[str, Any]] | None = None,
) -> str:
    import base64

    return (
        _fill(_POSTCARD_TEMPLATE, {
            "BRAND": _BRAND_CSS, "ENGINE": escape(engine),
            "IMAGE": base64.b64encode(png).decode(),
            "PLACE": escape(place), "DATE": escape(date),
            "COLLECTION": escape(collection_id), "SOURCE": escape(source),
            "CAPTION": f'<p class="caption">{escape(caption)}</p>' if caption else "",
            "LICENSE": (f"<div>license: {escape(license_)}</div>" if license_ else "")
            + (f"<div>not applied: {escape('; '.join(d['param'] + ' (' + d['why'] + ')' for d in dropped))}</div>" if dropped else ""),
            "STACK": stack_html or "", "LEGEND": _legend_card_html(legends or []),
        })
    )


def render_postcard(
    catalog: str,
    collection_id: str,
    item_id: str,
    place: str,
    date: str,
    assets: list[str] | None = None,
    rescale: str | None = None,
    colormap_name: str | None = None,
    expression: str | None = None,
    caption: str = "",
    out_path: str | None = None,
    stack_layer: bool = True,
    bbox: list[float] | None = None,
    fill_item_ids: list[str] | None = None,
    aspect: str | None = None,
) -> dict[str, Any]:
    """Write a share-ready card for one scene: pixels embedded, attribution baked in.

    Unlike a map artifact, a postcard has no live dependencies — the image is
    embedded as a data URI, so it never goes blank when a signed tile URL
    expires, and it carries no local paths. Use it when someone wants
    something they could post. Where and whether to post stays their call.

    date is what you want printed (usually the scene date, "2026-07-10").
    assets/rescale/colormap_name/expression take the same shapes as
    tile_url_template, so an NDVI card is expression + colormap_name.

    stack_layer: on by default, prints a compact stack listing in the credit block —
    the static back-of-postcard form of the map artifacts' Stack panel, with
    no links, so the card stays free of live URLs.

    bbox [w, s, e, n]: frame the card to this area instead of the whole
    scene. Pass the AOI — the subject fills the frame and scene-edge nodata
    stays off the card. Pick a scene that covers the bbox first. The frame
    snaps to the nearest standard card shape (3:2, 4:3, 1:1, 4:5, 2:3) by
    trimming centrally; pass aspect="2:3" etc. to choose the shape when the
    scene calls for it (a tall coastline wants portrait).

    fill_item_ids: when no single scene covers the bbox (a swath-edge AOI),
    pass the REST of a same-day full_coverage_set — the card bakes as a
    rio-tiler mosaic read straight from the COGs, main scene winning where
    scenes overlap. Same-day fills keep the seam invisible. Needs bbox;
    earth-search-style catalogs only (PC/VEDA fall back to single scene, named in `dropped`).
    """
    shapes: dict[str, Any] = {}
    if bbox is not None:
        # clamp to the scenes' union bbox so an AOI hanging off the data
        # can't print a nodata band — center-preserving, so the subject
        # stays in the middle of the frame. ponytail: bbox math only — a
        # diagonal swath edge (footprint != bbox) can still clip a corner
        # on single-scene cards; the result's coverage_note says so, fills fix it
        try:
            boxes = []
            for i in (item_id, *(fill_item_ids or [])):
                item = _get_json(f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{i}")
                if item.get("bbox"):
                    boxes.append(item["bbox"])
                    shapes[i] = item.get("geometry") if (item.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon") else item["bbox"]
            if boxes:
                union = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                         max(b[2] for b in boxes), max(b[3] for b in boxes)]
                bbox = _centered_clamp(bbox, union) or bbox
        except Exception:
            pass
        # then shape it: nearest standard card ratio (or the caller's pick),
        # trimmed centrally — inside the clamp, so still all data
        _, bbox = _snap_aspect(bbox, "imagery", aspect)
    png, mosaic_scenes, mosaic_note, dropped = None, 0, None, []
    if fill_item_ids:
        if bbox is None:
            mosaic_note = "fill_item_ids ignored: a mosaic card needs bbox"
            dropped.append({"param": "fill_item_ids", "why": "a mosaic card needs bbox"})
        elif CATALOGS[catalog]["raster"] != "titiler-xyz":
            mosaic_note = f"mosaic cards need direct COG access; {catalog} fell back to the single scene"
            dropped.append({"param": "fill_item_ids", "why": f"mosaic cards need direct COG access, which {catalog} does not offer"})
        else:
            try:
                png, mosaic_scenes = _mosaic_png(
                    catalog, collection_id, [item_id, *fill_item_ids], bbox,
                    assets, rescale, colormap_name, expression,
                )
            except Exception as e:
                # a card must always render — fall back to the single scene
                mosaic_note = f"mosaic failed ({e.__class__.__name__}); single-scene card"
    tiler_host = None
    if png is None:
        p = preview_item(
            catalog, collection_id, item_id, assets, rescale,
            max_size=1024, colormap_name=colormap_name, expression=expression, bbox=bbox,
        )
        if "preview_url" not in p:
            return p
        dropped += p.get("dropped", [])
        r = _client.get(p["preview_url"], timeout=90)
        if r.status_code >= 400:
            # never write a card around a broken image
            return {"error": f"image request failed: HTTP {r.status_code}", "url": p["preview_url"]}
        png = r.content
        from urllib.parse import urlsplit

        tiler_host = urlsplit(p["preview_url"]).netloc
    # ponytail: license from collection metadata only; per-item attribution
    # when a catalog needs it
    try:
        license_ = _shareable_license(describe_collection(catalog, collection_id).get("license"))
    except Exception:
        license_ = None
    stack_html, stack_note = None, None
    if stack_layer:
        stack_html = _stack_credit_for(catalog, collection_id, tiler_host, mosaic_scenes)
        if stack_html is None:
            stack_note = "stack listing skipped: docs/stack.md missing, malformed, or nothing to attribute"
    legends = _legends_for([], colormap_name)
    used = [shapes[i] for i in ([item_id, *fill_item_ids] if mosaic_scenes else [item_id]) if i in shapes]
    cov_pct = pct = where = None
    if bbox is not None and used:
        pct, where = _uncovered_side(bbox, used)
        cov_pct = round(100.0 - pct, 1)
        if cov_pct < PARTIAL_COVERAGE_PCT:
            caption = _with_wedge(caption, (pct, where))
    html = _postcard_html(
        png, place, date, collection_id,
        _catalog_source(catalog, collection_id), caption, license_, stack_html,
        engine="STAC · rio-tiler" if mosaic_scenes else "STAC · TiTiler",
        dropped=dropped,
        legends=legends,
    )
    try:
        out_path = _artifact_path(out_path, "postcard", f"{place}-{date}")
    except ValueError as e:
        return {"error": str(e)}
    _write_artifact(out_path, html)
    out = {"path": out_path}
    if dropped:
        out["dropped"] = dropped
    if legends:
        out["legend"] = legends[0]
    if mosaic_scenes:
        out["mosaic_scenes"] = mosaic_scenes
    notes = "; ".join(n for n in (mosaic_note, stack_note) if n)
    if notes:
        out["note"] = notes
    # the scene's footprint, not its bbox, decides what the frame holds: a slanted
    # swath edge leaves a nodata wedge inside a box the bbox says is covered
    if cov_pct is not None:
        out["coverage_pct"] = cov_pct
        if out["coverage_pct"] < PARTIAL_COVERAGE_PCT:
            out["uncovered_pct"], out["uncovered_side"] = pct, where
            why = ""
            if not mosaic_scenes:
                if dropped:
                    cause = dropped[0]["why"]
                elif CATALOGS[catalog]["raster"] != "titiler-xyz":
                    cause = f"mosaic cards need direct COG access, which {catalog} does not offer"
                else:
                    cause = "pass the rest of a same-day full_coverage_set as fill_item_ids"
                why = f"; no same-day sibling fills it: {cause}"
            out["coverage_note"] = f"the scene footprint leaves {pct:.0f}% of the card uncovered, mostly {where}, printed as black no-data{why}"
    return out


# A delta can be arithmetically right and ecologically backwards. Cloud pushes a
# normalized index toward zero, and a percentage on a near-zero baseline swings on
# noise. Both inputs are already in hand when the delta is built, so the number
# should not travel alone. Tune these rather than deleting the caveat.
CLOUD_CAVEAT_PCT = 20.0  # either scene cloudier than this and the delta may be cloud
FLAT_BASELINE = 0.1  # |mean_before| under this and delta_pct amplifies noise


def _delta_caveats(
    mean_before: float | None,
    cloud_after: float | None,
    cloud_before: float | None,
    ids: tuple[str, str] = ("", ""),
) -> list[str]:
    """Reasons this delta shouldn't be read at face value. Empty when it's clean."""
    out: list[str] = []
    cloudy = [
        (w, c)
        for w, c in (("after", cloud_after), ("before", cloud_before))
        if c is not None and c >= CLOUD_CAVEAT_PCT
    ]
    if cloudy:
        out.append(
            "cloud may be driving this delta ("
            + ", ".join(f"{w} scene {ids[w == 'before']} {c:.0f}%".replace("  ", " ") for w, c in cloudy)
            + f", threshold {CLOUD_CAVEAT_PCT:.0f}%). Cloud pushes a normalized index toward zero."
        )
    if mean_before is not None and abs(mean_before) < FLAT_BASELINE:
        out.append(
            f"delta_pct is unstable: the baseline mean ({mean_before}) is near zero, so a small "
            "absolute change reads as a large percentage. Trust delta over delta_pct."
        )
    return out


def _scene_key(it: dict[str, Any]) -> tuple[float, float, str]:
    """Sort key for the best scene: coverage descending, cloud ascending, id as the last
    tiebreak, so a tie never falls to the order the server returned"""
    cloud = it.get("cloud_cover")
    return (-(it.get("covers_aoi_pct") or 0.0), 100.0 if cloud is None else cloud, it["id"])


def _scene_line(it: dict[str, Any]) -> dict[str, Any]:
    return {"id": it["id"], "date": it["datetime"][:10], "cloud": it.get("cloud_cover"), "covers_aoi_pct": it.get("covers_aoi_pct")}


def _picked_and_runner_up(picked: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """The picked scene's coverage, the best scene it beat, and a note when only the id decided"""
    others = sorted((i for i in items if i["id"] != picked["id"]), key=_scene_key)
    out: dict[str, Any] = {"covers_aoi_pct": picked.get("covers_aoi_pct")}
    if others:
        r = others[0]
        out["runner_up"] = _scene_line(r)
        if _scene_key(r)[:2] == _scene_key(picked)[:2]:
            out["pick_note"] = f"{r['id']} was equally good (same coverage and cloud); the scene id decided"
    return out


def compare_dates(
    place: str | None = None,
    bbox: list[float] | None = None,
    window_before: str = "",
    window_after: str = "",
    expression: str = "(nir-red)/(nir+red)",
    max_cloud_cover: float = 40,
    label: str | None = None,
    stack_layer: bool = True,
    stack_facts: dict[str, Any] | None = None,
    out_path: str | None = None,
    notes: list[str] | None = None,
    apply_scaling: bool = True,
) -> dict[str, Any]:
    """Compare two time windows over a place: matched scenes, index delta, swipe map.

    The most common Earth question is "what changed?" — this answers it in one
    call. Windows are RFC3339 ranges ("2026-06-01T00:00:00Z/2026-06-30T23:59:59Z");
    window_after defaults to the last 14 days, window_before to roughly one month
    earlier. Picks Sentinel-2 scenes from the SAME MGRS tile (best AOI coverage,
    lowest cloud), computes the expression's stats for both dates, and writes a
    side-by-side swipe map. Returns scenes, means, delta, and map_path.
    out_path sets where the map is written (default: a name carrying a short hash of the box,
    so two boxes over the same dates do not overwrite each other).
    stack_layer and stack_facts pass through to the swipe map, and since this tool geocodes
    the place itself, the panel truthfully claims Gazet/Nominatim.

    Means are physical by default (scale and offset applied on both dates, so a scene from
    before 2022 and one from after compare fairly); scaling_applied says so once.
    band_scaling rides with each date and scaling_note names the factors in words.
    apply_scaling=False gives raw digital numbers, with scaling_note naming what was not applied.

    A caveat list comes back when the delta shouldn't be read at face value —
    a cloudy scene on either end, or a near-zero baseline that makes delta_pct
    swing on noise. When caveat is present, report it alongside the number.
    """
    geocoded: bool | str = False
    if bbox is None and place:
        g = geocode(place)
        if "error" in g:
            return g
        if g.get("ambiguous"):
            return _ambiguous_error(place, g)
        bbox, geocoded = g["bbox"], g["source"]  # the panel credits the geocoder that answered
    bbox = _resolve_bbox(place, bbox)
    if isinstance(bbox, dict):
        return bbox
    window_after = window_after or last_days_window(14)
    window_before = window_before or last_days_window(30, end_days_ago=25)

    def _search(window: str) -> list[dict[str, Any]]:
        r = search_imagery(
            "earth-search", ["sentinel-2-l2a"], bbox=bbox,
            datetime_range=window, max_cloud_cover=max_cloud_cover, limit=25,
        )
        return [i for i in r.get("items", []) if "read_note" not in i]

    after_items, before_items = _search(window_after), _search(window_before)
    if not after_items or not before_items:
        return {"error": f"Not enough scenes: {len(after_items)} after, {len(before_items)} before. Widen windows or cloud limit."}

    shared = {_mgrs_tile(i["id"]) for i in after_items} & {_mgrs_tile(i["id"]) for i in before_items}
    if not shared:
        return {"error": "No shared Sentinel-2 tile between the two windows — try wider windows."}
    # a set has no order, and two tiles can both cover the box at 100 (Horta Sud, 31SBD and 30SYJ),
    # so the tile is the one holding the best scene by the full key, never max() over the set
    best_tile = _mgrs_tile(min((i for i in after_items if _mgrs_tile(i["id"]) in shared), key=_scene_key)["id"])

    def pick(items: list[dict[str, Any]]) -> dict[str, Any]:
        return min((i for i in items if _mgrs_tile(i["id"]) == best_tile), key=_scene_key)

    a, b = pick(after_items), pick(before_items)

    scope: dict[str, Any] = {}
    scaling: dict[str, list[dict[str, Any]]] = {}

    def _mean(it: dict[str, Any]) -> float | None:
        try:
            # without the bbox the mean is the whole Sentinel-2 tile, not the place asked about
            s = compute_statistics("earth-search", it["collection"], it["id"], expression=expression, bbox=bbox, apply_scaling=apply_scaling)
            scope.update({k: s[k] for k in ("clipped_to", "extent_note", "scaling_applied", "scaling_note") if k in s})
            if s.get("band_scaling"):
                scaling[it["id"]] = s["band_scaling"]
            return round(next(iter(s.values()))["mean"], 4)
        except Exception:
            return None

    mean_after, mean_before = _mean(a), _mean(b)

    def layer(it: dict[str, Any], name: str) -> dict[str, Any]:
        return {
            "type": "item", "name": name, "catalog": "earth-search",
            "collection_id": it["collection"], "item_id": it["id"], "bbox": it["bbox"],
            "expression": expression, "rescale": "-1,1", "colormap_name": "rdylgn",
        }
    title = f"{label or place or 'AOI'} — {a['datetime'][:10]} vs {b['datetime'][:10]}"
    if not out_path:
        out_path = _artifact_path(None, "map", f"{title} {hashlib.sha1(repr(bbox).encode()).hexdigest()[:6]}")
    token = _tool_geocoded.set(geocoded)
    try:
        m = render_map(
            title=title,
            out_path=out_path,
            subtitle=f"{expression} · means over the area · drag the divider · tile {best_tile}",
            bbox=bbox,
            layers=[layer(a, f"{a['datetime'][:10]} (after)"), layer(b, f"{b['datetime'][:10]} (before)")],
            stack_layer=stack_layer,
            stack_facts=stack_facts,
            notes=notes,
        )
    finally:
        _tool_geocoded.reset(token)
    if "error" in m:
        return m
    delta = round(mean_after - mean_before, 4) if mean_after is not None and mean_before is not None else None
    out = {
        "tile": best_tile,
        "after": {"id": a["id"], "date": a["datetime"][:10], "cloud": a.get("cloud_cover"), "mean": mean_after, **_picked_and_runner_up(a, after_items)},
        "before": {"id": b["id"], "date": b["datetime"][:10], "cloud": b.get("cloud_cover"), "mean": mean_before, **_picked_and_runner_up(b, before_items)},
        "delta": delta,
        "delta_pct": round(delta / abs(mean_before) * 100, 1) if delta is not None and mean_before else None,
        "expression": expression,
        "map_path": m["map_path"],
        **scope,
    }
    for side, it in (("after", a), ("before", b)):
        if it["id"] in scaling:
            out[side]["band_scaling"] = scaling[it["id"]]
    if scope.get("scaling_applied") and "scaling_note" not in scope and len(scaling) == 2:
        out["scaling_note"] = _applied_scaling_words(
            [(x["datetime"][:10], scaling[x["id"]]) for x in (a, b)]
        )
    caveats = _delta_caveats(mean_before, a.get("cloud_cover"), b.get("cloud_cover"), (a["id"], b["id"]))
    if caveats:
        out["caveat"] = caveats
    _notes_result(out, notes, stack_facts, bbox)
    return out


def _applied_scaling_words(dated: list[tuple[str, list[dict[str, Any]]]]) -> str:
    big = [(d, max(gs, key=lambda g: len(g["bands"]))) for d, gs in dated]
    key = lambda g: (g.get("scale"), g.get("declared_offset", g.get("offset")), len(g["bands"]))
    same = len({key(g) for _, g in big}) == 1
    parts = []
    for dates, g in [(" and ".join(d for d, _ in big), big[0][1])] if same else [(d, g) for d, g in big]:
        off = g.get("declared_offset", g.get("offset"))
        parts.append(f"scale {g.get('scale')} and offset {off} applied on {len(g['bands'])} bands for {dates}")
    done = "; ".join(parts)
    for d, g in big:
        if "declared_offset" in g:
            done += f" (Earth Search already took the {g['declared_offset']} offset out of the {d} pixels, so 0 was added there)"
    return done


# ---------------------------------------------------------------- monitoring


BRIEF_PAD_DEG = 1.0  # one radius for the brief page and conditions_brief


def active_events(bbox: list[float] | None = None, days: int = 30, pad: float = 0.0) -> dict[str, Any]:
    """Current natural events and disaster alerts, optionally filtered to a bbox.

    Combines NASA EONET (wildfires, storms, volcanoes, floods...) and GDACS
    global disaster alerts. bbox is [w, s, e, n]; pad widens it by that many
    degrees on every side (nearby events matter). Returns compact event lists
    with coordinates suitable for mapping.
    """
    padded = bool(bbox and pad)
    if padded:
        bbox = [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]
    out: dict[str, Any] = {"eonet": [], "gdacs": [], **({"pad_deg": pad} if padded else {})}
    try:
        params: dict[str, Any] = {"days": days, "status": "open"}
        if bbox:
            w, s, e, n = bbox
            params["bbox"] = f"{w},{n},{e},{s}"  # EONET wants lonmin,latmax,lonmax,latmin
        data = _get_json("https://eonet.gsfc.nasa.gov/api/v3/events", params=params)
        events_all = data.get("events", [])
        # Field test No.8: the cap was 50 and a 30-day window over a busy fortnight
        # pushed the icebergs off the list with nothing said. Higher cap, and count
        out["eonet_note"] = f"EONET listed {len(events_all)} open events in {days} days" + ("; first 500 kept" if len(events_all) > 500 else "")
        for ev in events_all[:500]:
            geom = (ev.get("geometry") or [{}])[-1]
            out["eonet"].append(
                {
                    "title": ev.get("title"),
                    "category": (ev.get("categories") or [{}])[0].get("title"),
                    "date": geom.get("date"),
                    "coordinates": geom.get("coordinates"),
                    "link": ev.get("link"),
                }
            )
    except Exception as e:
        out["eonet_error"] = str(e)
    try:
        # GDACS made eventtype required on geteventlist/MAP (400 without it), and MAP
        # returns per-episode track points. EVENTS4APP is the current-events feed:
        # one Point per event, every type except drought
        data = _get_json("https://www.gdacs.org/gdacsapi/api/events/geteventlist/EVENTS4APP")
        # Field test No.8: the feed returned 100 events for days=7 and days=30
        # alike. It is a current-events list, not a window, so the window is
        # applied here and the return says how many the feed listed
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        listed = len(data.get("features") or [])
        for f in data.get("features", [])[:200]:
            p = f.get("properties", {})
            geom = f.get("geometry") or {}
            if geom.get("type") == "Point":
                coords = geom.get("coordinates") or [None, None]
            else:
                coords = [p.get("longitude"), p.get("latitude")]
            if coords[0] is None:
                continue
            if bbox:
                w, s, e, n = bbox
                if not (w <= coords[0] <= e and s <= coords[1] <= n):
                    continue
            if (p.get("todate") or p.get("fromdate") or "9999") < cutoff:
                continue
            out["gdacs"].append(
                {
                    "title": p.get("name") or p.get("eventname"),
                    "type": p.get("eventtype"),
                    "alert_level": p.get("alertlevel"),
                    "from": p.get("fromdate"),
                    "to": p.get("todate"),
                    "coordinates": coords,
                    "link": (p.get("url") or {}).get("report"),
                }
            )
        out["gdacs_note"] = f"GDACS current-events feed listed {listed}; {len(out['gdacs'])} active within {days} days" + (" inside the bbox" if bbox else "")
    except Exception as e:
        out["gdacs_error"] = str(e)
    # Field tests 11 (Bend) and 12 (Asheville, Harris County): GDACS timed out, the
    # empty list read as calm and the brief headlined CALM. Empty is only all-clear if every source answered
    out["sources"] = {
        "eonet": {"ok": "eonet_error" not in out, "listed": len(out["eonet"]) if "eonet_error" not in out else None, "error": out.get("eonet_error")},
        "gdacs": {"ok": "gdacs_error" not in out, "listed": len(out["gdacs"]) if "gdacs_error" not in out else None, "error": out.get("gdacs_error")},
    }
    down = [n.upper() for n, v in out["sources"].items() if not v["ok"]]
    found = [f"{len(out[n])} {n.upper()} events in the box" for n in ("eonet", "gdacs") if out[n]]
    out["all_clear"] = not down and not found
    out["all_clear_note"] = (
        f"{' and '.join(down)} did not answer, so an empty list is not all-clear" if down
        else ", ".join(found) if found else "EONET and GDACS both answered with no events")
    return out


# HRRR smoke: NOAA's 3 km CONUS model on the open-data bucket, read as a
# GDAL vrt:// through eoAPI. Bounds from smokehrrr's metadata.ts
HRRR_BBOX = [-134.12, 21.12, -60.9, 52.62]
HRRR_BUCKET = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
HRRR_MAX_HOURS = 48  # only the 00/06/12/18z runs reach it; plain hours stop at 18
# MASSDEN kg/m3 -> ug/m3, then transparent below 1 so clear air shows the basemap
HRRR_EXPR = "b1*1000000000"
HRRR_COLORMAP = json.dumps([
    [[0, 1], [0, 0, 0, 0]], [[1, 5], [255, 224, 102, 70]], [[5, 35], [255, 153, 0, 150]],
    [[35, 150], [214, 40, 0, 205]], [[150, 1000000], [110, 0, 20, 235]],
])
# approximate reading scale, NOT the AQI: the bounds are a judgment call, tune them
SMOKE_SOURCE = {"model": "NOAA HRRR", "band": "MASSDEN (76)", "tiler": EOAPI, "recipe": "https://github.com/hrodmn/smokehrrr"}
SMOKE_SCALE = "under 5 clear, 5 to 35 light, 35 to 150 moderate, above 150 heavy"


def _hrrr_grib_url(date: dt.date, hour: int, fhour: int) -> str:
    return f"{HRRR_BUCKET}/hrrr.{date:%Y%m%d}/conus/hrrr.t{hour:02d}z.wrfsfcf{fhour:02d}.grib2"


def _hrrr_vrt(grib_url: str) -> str:
    return f"vrt://{grib_url}?bands=76"  # band 76 is MASSDEN, and the option belongs inside the vrt value


def _hrrr_tile_template(grib_url: str) -> str:
    q = str(httpx.QueryParams([("url", _hrrr_vrt(grib_url)), ("expression", HRRR_EXPR), ("colormap", HRRR_COLORMAP)]))
    return f"{EOAPI}/external/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?" + q


def smoke_word(ugm3: float | None) -> str:
    return "no data" if ugm3 is None else "clear" if ugm3 < 5 else "light" if ugm3 < 35 else "moderate" if ugm3 < 150 else "heavy"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def smoke_forecast(lat: float, lon: float, hours_ahead: int = 24) -> dict[str, Any]:
    """Smoke near the ground now and hours_ahead from now, at a point in the contiguous US.

    NOAA HRRR near-surface smoke mass density (MASSDEN, GRIB band 76) through
    raster.eoapi.dev's vrt:// GRIB reader, the smokehrrr recipe
    (https://github.com/hrodmn/smokehrrr). CONUS only. Forecast frames are
    model guidance, not observation, and new runs can lag in NOAA's bucket.

    Returns now_ugm3 (the frame nearest the current time, stamped
    now_valid_time) and forecast_ugm3 (µg/m³), a tiles XYZ template for the
    forecast frame (clear air is transparent, so it drapes on a map), and
    a caveat list to read before quoting the numbers.
    """
    w, s, e, n = HRRR_BBOX
    if not (w <= lon <= e and s <= lat <= n):
        return {"error": "HRRR smoke covers the contiguous US only", "coverage_bbox": HRRR_BBOX}
    now = _utcnow()
    hour = dt.timedelta(hours=1)
    try:
        run = None
        for back in range(37):  # smokehrrr probes back 36 h too, but only the long runs are worth a HEAD
            t = (now - back * hour).replace(minute=0, second=0, microsecond=0)
            if t.hour % 6 == 0 and _client.head(_hrrr_grib_url(t.date(), t.hour, 0)).status_code == 200:
                run = t
                break
        if run is None:
            return {"error": "no HRRR run found in NOAA's bucket for the last 36 hours", "source": SMOKE_SOURCE}

        def have(f: int) -> bool:
            return _client.head(_hrrr_grib_url(run.date(), run.hour, f)).status_code == 200

        def read(f: int) -> float | None:
            d = _get_json(
                f"{EOAPI}/external/point/{lon},{lat}",
                params={"url": _hrrr_vrt(_hrrr_grib_url(run.date(), run.hour, f)), "expression": HRRR_EXPR},
                timeout=60,
            )
            v = d["values"][0]
            return None if v is None else round(v, 1)

        want = max(1, min(HRRR_MAX_HOURS, round((now + hours_ahead * hour - run) / hour)))
        # ponytail: tries at most 5 hours at or below the wanted one, not the whole run;
        # a bucket listing would find the true latest hour in one call
        fh = next((f for f in dict.fromkeys((want, want - 1, want - 3, want - 6, want - 12)) if f >= 1 and have(f)), None)
        if fh is None:
            return {"error": f"HRRR run {run:%Y-%m-%dT%H:%MZ} has no forecast frames in NOAA's bucket yet", "source": SMOKE_SOURCE}
        # runs are 6-hourly here, so f00 can be hours old: read the frame nearest the clock
        fh_now = max(0, min(HRRR_MAX_HOURS, round((now - run) / hour)))
        if fh_now and not have(fh_now):
            fh_now = 0
        now_ugm3, forecast_ugm3 = read(fh_now), read(fh)
    except Exception as ex:
        return {"error": f"HRRR smoke lookup failed: {type(ex).__name__}: {ex}", "source": SMOKE_SOURCE}
    valid = run + fh * hour
    caveat = [
        "this is model guidance from NOAA HRRR, not a measurement; a smoke plume's timing and edges can be off by hours and miles",
        "the model grid is 3 km, so a valley or a ridge can read differently from the cell value",
        "HRRR covers the contiguous US only",
        f"approximate reading scale in µg/m³, not the AQI: {SMOKE_SCALE}",
    ]
    if valid < now + hours_ahead * hour - hour:
        caveat.insert(0, f"the frame for {hours_ahead} h ahead is not in NOAA's bucket (the run is old or the newer frames have not landed), so this uses the frame valid {valid:%Y-%m-%dT%H:%MZ}, which is earlier than asked")
    if now_ugm3 is None or forecast_ugm3 is None:
        caveat.append("a reading came back empty (no data at this point), so that number is null")
    return {
        "run": f"{run:%Y-%m-%dT%H:%MZ}",
        "forecast_hour": fh,
        "valid_time": f"{valid:%Y-%m-%dT%H:%MZ}",
        "now_valid_time": f"{run + fh_now * hour:%Y-%m-%dT%H:%MZ}",
        "now_ugm3": now_ugm3,
        "forecast_ugm3": forecast_ugm3,
        "units": "µg/m³ near-surface smoke mass density",
        "tiles": _hrrr_tile_template(_hrrr_grib_url(run.date(), run.hour, fh)),
        "bounds": HRRR_BBOX,
        "source": SMOKE_SOURCE,
        "caveat": caveat,
    }


def weather_summary(lat: float, lon: float, past_days: int = 7) -> dict[str, Any]:
    """Recent and forecast weather for a point (Open-Meteo, no key needed).

    Returns daily max/min temperature, precipitation, max wind speed, and
    dominant wind direction (degrees, meteorological: 0=N, 90=E — the
    direction wind comes FROM) for past_days back and 7 days ahead. Wind
    direction matters for smoke, ash, and plume dispersal questions.
    """
    data = _get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "wind_speed_10m_max,wind_direction_10m_dominant",
            "past_days": past_days,
            "forecast_days": 7,
            "timezone": "auto",
        },
    )
    # Open-Meteo snaps to a grid cell and reports ITS elevation, which at a
    # volcano or a mountain town is a different climate from the place asked
    # for — Fuego returned 8.5C max because the cell sits at 3,704 m, not in
    # the villages below. Returning both stops that being read as the answer.
    out = {"units": data.get("daily_units"), "daily": data.get("daily")}
    grid = {k: data[k] for k in ("latitude", "longitude", "elevation") if k in data}
    if grid:
        out["grid_cell"] = grid
        if grid.get("elevation", 0) > 500:
            out["elevation_note"] = (
                f"readings are for a grid cell at {grid['elevation']:.0f} m "
                f"({grid.get('latitude'):.3f}, {grid.get('longitude'):.3f}) — say so if the "
                "place spans a big elevation range, the valley is warmer than this"
            )
    return out


# ---------------------------------------------------------------- next pass

# offline fallback for the live developmentseed/eo-predictor catalog
# (scripts/satellites/*.json is the maintained source of truth)
_CONSTELLATIONS = {
    "sentinel-2": {"norad_ids": [40697, 42063, 60989], "swath_km": 290, "optical": True},
    "sentinel-1": {"norad_ids": [39634, 62261], "swath_km": 410, "optical": False},
    "landsat": {"norad_ids": [39084, 49260], "swath_km": 185, "optical": True},
}
_TLE_CACHE = Path(os.environ.get(
    "GROUNDSTATION_TLE_CACHE", Path.home() / ".cache" / "groundstation" / "tle.txt"
))
_TLE_MAX_AGE_S = 24 * 3600  # TLEs drift slowly; a day-old set is fine for swath math
_CATALOG_CACHE = _TLE_CACHE.with_name("eo-predictor.json")
_EO_PREDICTOR_API = "https://api.github.com/repos/developmentseed/eo-predictor/contents/scripts/satellites"
EO_PREDICTOR_URL = "https://developmentseed.org/eo-predictor"


def _sun_elevation_deg(when: dt.datetime, lat: float, lon: float) -> float:
    """Sun elevation above the horizon in degrees, from the NOAA approximation.

    Accurate to about 1 degree, which is plenty for "is it daylight"; no
    ephemeris file needed. `when` is UTC.
    """
    import math

    n = when.timetuple().tm_yday
    b = math.radians(360.0 / 365.0 * (n - 81))
    decl = math.radians(23.44 * math.sin(b))
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    solar_h = when.hour + when.minute / 60.0 + when.second / 3600.0 + lon / 15.0 + eot_min / 60.0
    ha = math.radians(15.0 * (solar_h - 12.0))
    phi = math.radians(lat)
    return math.degrees(math.asin(math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)))


def _pass_minima(dists_km: list[float], within_km: float) -> list[int]:
    """Indices of local minima at or under within_km — each is one pass.

    Pure so it's testable offline; the skyfield propagation just feeds it.
    """
    hits = []
    for i in range(1, len(dists_km) - 1):
        if dists_km[i] <= within_km and dists_km[i] <= dists_km[i - 1] and dists_km[i] < dists_km[i + 1]:
            hits.append(i)
    return hits


def _eo_predictor_cfgs(files: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Map eo-predictor satellite JSON (keyed by file stem) to next_pass cfgs."""
    import math

    cfgs, skipped = {}, []
    for name, d in files.items():
        if not d.get("norad_ids") or not d.get("swath_km"):
            skipped.append(name)
            continue
        # only a tasked sensor points; Sentinel-1 and NISAR have a fixed look angle inside their swath
        reach = d["swath_km"] / 2
        if d.get("tasking"):
            reach += (d.get("altitude_km") or 0) * math.tan(math.radians(d.get("off_nadir_deg") or 0))
        cfgs[name] = {
            "norad_ids": d["norad_ids"],
            "swath_km": d["swath_km"],
            "optical": d.get("sensor_type") != "SAR",
            "reach_km": reach,
            "access": d.get("data_access", "unknown"),
        }
    return cfgs, skipped


def _load_constellations() -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    """eo-predictor's catalog (24 h disk cache), else the built-in list."""
    try:
        if _CATALOG_CACHE.exists() and time.time() - _CATALOG_CACHE.stat().st_mtime < _TLE_MAX_AGE_S:
            files = json.loads(_CATALOG_CACHE.read_text(encoding="utf-8"))
        else:
            listing = _client.get(_EO_PREDICTOR_API)
            listing.raise_for_status()
            entries = [e for e in listing.json() if e["name"].endswith(".json")]

            def one(e):
                r = _client.get(e["download_url"])
                r.raise_for_status()
                return e["name"][:-5], r.json()

            with ThreadPoolExecutor(max_workers=8) as pool:
                files = dict(pool.map(one, entries))
            _CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _CATALOG_CACHE.write_text(json.dumps(files), encoding="utf-8")
        cfgs, skipped = _eo_predictor_cfgs(files)
        if cfgs:
            return cfgs, "developmentseed/eo-predictor", skipped
    except Exception:
        _CATALOG_CACHE.unlink(missing_ok=True)  # a corrupt file must not pin the fallback for 24 h
    return _CONSTELLATIONS, "built-in list (eo-predictor catalog unreachable)", []


def _fetch_tles(cfgs: dict[str, dict[str, Any]], path: Path) -> list[str]:
    """Write TLEs for every id in cfgs to path (24 h cache); return ids with no data."""
    if path.exists() and time.time() - path.stat().st_mtime < _TLE_MAX_AGE_S:
        return []

    def one(nid: int) -> str:
        # a decayed satellite 404s and a throttled request is not worth failing the call for
        try:
            r = _client.get(
                "https://celestrak.org/NORAD/elements/gp.php",
                params={"CATNR": nid, "FORMAT": "tle"},
            )
        except httpx.HTTPError:
            return ""
        return r.text.strip() if r.status_code == 200 and "No GP data found" not in r.text else ""

    ids = sorted({n for c in cfgs.values() for n in c["norad_ids"]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(one, ids))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(t for t in texts if t) + "\n", encoding="utf-8")
    return [f"norad-{n}" for n, t in zip(ids, texts) if not t]


def next_pass(
    lat: float,
    lon: float,
    days: int = 3,
    constellations: list[str] | None = None,
) -> dict[str, Any]:
    """Predict upcoming satellite passes over a point, for the constellations EO Predictor shows.

    Answers "when could we next get imagery of this place." Propagates live
    Celestrak TLEs with Skyfield and reports each time a satellite's swath
    band sweeps the point in the next `days` (max 7). Every pass carries
    `sun_elevation_deg` and `daylight` (sun above the horizon at the point at
    pass time). Optical passes with the sun down are dropped, since optical
    sensors do not acquire at night, and counted in `optical_passes_at_night` and named in
    `optical_passes_at_night_list` (satellite, time_utc, sun_elevation_deg);
    radar (Sentinel-1) is kept day or night and carries the flag too.

    Honest limits, and say them when it matters: this is swath geometry, not
    the acquisition plan — a pass is a POSSIBLE capture, not a promised one
    (Sentinel-2 acquires systematically over land, so there it's near-certain;
    Sentinel-1 follows an observation scenario). Cloud cover is its own
    gamble, and imagery lands in the public catalogs hours to a day later.

    The constellation catalog (swath, altitude, off-nadir pointing, NORAD ids)
    is read from developmentseed/eo-predictor; if it is unreachable a built-in
    list of Sentinel-2, Sentinel-1 and Landsat is used and
    `constellations_source` says so. The default is open-data constellations
    only; every loaded one, commercial ones included, is available by name and
    listed in `constellations_available`. Commercial sensors (`access` other
    than "open") only image on order, and a tasked one is counted as a pass
    whenever the target is inside its off-nadir reach. The result links EO
    Predictor (`eo_predictor_url`), the richer interactive view.
    """
    import numpy as np
    from skyfield.api import load, wgs84

    days = min(days, 7)
    cfgs, source, skipped = _load_constellations()
    wanted = constellations or [k for k, c in cfgs.items() if c.get("access", "open") == "open"]
    # one TLE file per catalog source: a fallback-run file must not serve a catalog run
    tle_path = _TLE_CACHE.with_name(f"{_TLE_CACHE.stem}-{'eo-predictor' if cfgs is not _CONSTELLATIONS else 'builtin'}.txt")
    skipped = skipped + _fetch_tles({k: cfgs[k] for k in wanted if k in cfgs}, tle_path)
    sats = load.tle_file(str(tle_path))
    by_id = {s.model.satnum: s for s in sats}

    ts = load.timescale()
    t0 = ts.now()
    step_s = 30.0
    n_steps = int(days * 86400 / step_s)
    times = ts.tt_jd(t0.tt + [i * step_s / 86400.0 for i in range(n_steps)])
    target = wgs84.latlon(lat, lon)

    passes = []
    at_night: list[dict[str, Any]] = []
    for cname in wanted:
        cfg = cfgs.get(cname)
        if not cfg:
            continue
        for nid in cfg["norad_ids"]:
            sat = by_id.get(nid)
            if sat is None:
                continue
            # ground-track distance to the target at every step
            subpoint = wgs84.subpoint_of(sat.at(times))
            lat1, lon1 = np.radians(subpoint.latitude.degrees), np.radians(subpoint.longitude.degrees)
            lat2, lon2 = np.radians(lat), np.radians(lon)
            d = 6371.0 * np.arccos(
                np.clip(
                    np.sin(lat1) * np.sin(lat2) + np.cos(lat1) * np.cos(lat2) * np.cos(lon1 - lon2),
                    -1, 1,
                )
            )
            for i in _pass_minima(d.tolist(), cfg.get("reach_km", cfg["swath_km"] / 2)):
                t = times[i]
                utc = t.utc_datetime()
                sun = _sun_elevation_deg(utc, lat, lon)
                if cfg["optical"] and sun <= 0:
                    at_night.append({"satellite": sat.name, "time_utc": utc.strftime("%Y-%m-%d %H:%M"), "sun_elevation_deg": round(sun, 1)})
                    continue  # optical only acquires in daylight
                passes.append(
                    {
                        "constellation": cname,
                        "satellite": sat.name,
                        "time_utc": utc.strftime("%Y-%m-%d %H:%M"),
                        "sun_elevation_deg": round(sun, 1),
                        "daylight": sun > 0,
                        "track_distance_km": round(float(d[i]), 1),
                        "kind": "optical" if cfg["optical"] else "radar (day or night, sees through cloud)",
                        "access": cfg.get("access", "open"),
                    }
                )
    passes.sort(key=lambda p: p["time_utc"])
    return {
        "point": {"lat": lat, "lon": lon},
        "window_days": days,
        "passes": passes,
        "optical_passes_at_night": len(at_night),
        "optical_passes_at_night_list": at_night,
        "constellations_source": source,
        "constellations_available": list(cfgs),
        "skipped": skipped,
        "eo_predictor_url": EO_PREDICTOR_URL,
        "note": (
            "swath geometry from live TLEs, not the acquisition plan — a pass is a "
            "possible capture, and imagery reaches public catalogs hours to a day later. "
            "Optical passes are kept only when the sun is above the horizon at the point; "
            "a low sun (under about 10 degrees) gives a dim image. "
            f"Commercial sensors only image if tasked. Richer view: {EO_PREDICTOR_URL}"
        ),
    }


def _compass(deg: float | None) -> str:
    """Format dominant wind direction (meteorological: direction wind comes FROM) into a compass word."""
    if deg is None:
        return "unknown"
    deg = float(deg) % 360
    sectors = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((deg + 22.5) % 360) // 45)
    return f"from the {sectors[idx]}"


def _normalize_event_type(category: str | None, type_str: str | None) -> str:
    """Map NASA EONET categories and GDACS event types to a canonical form."""
    cat = (category or "").lower()
    t = (type_str or "").lower()
    if "wildfire" in cat or "wildfire" in t or t == "wf":
        return "wildfire"
    if "storm" in cat or "cyclone" in t or "storm" in t or t == "tc":
        return "storm"
    if "volcano" in cat or "volcano" in t or t == "vo":
        return "volcano"
    if "flood" in cat or "flood" in t or t == "fl":
        return "flood"
    if "earthquake" in cat or "earthquake" in t or t == "eq":
        return "earthquake"
    return "other"


def _conditions_signals(dryness: dict[str, Any], wind: dict[str, Any], events: dict[str, Any]) -> list[str]:
    """Derive verbatim fact strings from dryness, wind, and active events."""
    signals = []

    # Dryness signal
    if "error" in dryness or not dryness:
        signals.append("dryness data unavailable")
    else:
        days_since = dryness.get("days_since_last_rain")
        days_back = dryness.get("days_back", 14)
        if days_since is None:
            signals.append(f"no rain ≥1mm in the last {days_back} days")
        else:
            if days_since == 1:
                signals.append("last rain ≥1mm was 1 day ago")
            else:
                signals.append(f"last rain ≥1mm was {days_since} days ago")

    # Wind signal
    if "error" in wind or not wind:
        signals.append("wind data unavailable")
    else:
        today = wind.get("today", {})
        today_speed = today.get("wind_speed_max")
        today_compass = today.get("wind_direction_compass")
        if today_speed is not None and today_compass:
            signals.append(f"peak wind today {round(today_speed)} km/h {today_compass}")

    # Events signal
    if not events or ("eonet_error" in events and "gdacs_error" in events):
        signals.append("active events data unavailable")
    else:
        eonet_list = events.get("eonet", []) or []
        gdacs_list = events.get("gdacs", []) or []

        counts = {}
        for ev in eonet_list:
            t = _normalize_event_type(ev.get("category"), None)
            counts[t] = counts.get(t, 0) + 1
        for ev in gdacs_list:
            t = _normalize_event_type(None, ev.get("type"))
            counts[t] = counts.get(t, 0) + 1

        plurals = {
            "wildfire": "wildfires",
            "storm": "storms",
            "volcano": "volcanoes",
            "flood": "floods",
            "earthquake": "earthquakes",
            "other": "other events",
        }
        singulars = {
            "wildfire": "wildfire",
            "storm": "storm",
            "volcano": "volcano",
            "flood": "flood",
            "earthquake": "earthquake",
            "other": "other event",
        }

        event_signals = []
        for t in ["wildfire", "storm", "volcano", "flood", "earthquake", "other"]:
            c = counts.get(t, 0)
            if c > 0:
                if c == 1:
                    event_signals.append(f"1 active {singulars[t]} within ~150 km")
                else:
                    event_signals.append(f"{c} active {plurals[t]} within ~150 km")

        if event_signals:
            signals.extend(event_signals)
        else:
            down = [n.upper() for n, v in (events.get("sources") or {}).items() if not v.get("ok")]
            if down:
                signals.append(f"no events from {' or '.join(n for n in ('EONET', 'GDACS') if n not in down)}; {' and '.join(down)} did not answer")
            else:
                signals.append("no active events within ~150 km")

    return signals


def conditions_brief(
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    days_back: int = 14,
) -> dict[str, Any]:
    """Provide a brief summary of current conditions around a location.

    Resolves the place name or coordinates, aggregates dryness, wind, active events,
    latest Sentinel-2 look, and upcoming satellite passes, then generates honest,
    verbatim fact signals.
    """
    # ponytail: no terrain in v1 — add Copernicus DEM relief when a real case wants it
    if place:
        g = geocode(place)
        if "error" in g:
            return g
        lat = g.get("lat")
        lon = g.get("lon")
        bbox = g.get("bbox")
    elif lat is not None and lon is not None:
        bbox = [lon - 0.25, lat - 0.25, lon + 0.25, lat + 0.25]
    else:
        return {"error": "Provide place or lat and lon"}

    if lat is None or lon is None or bbox is None:
        return {"error": "Could not resolve location coordinates or bbox"}

    brief: dict[str, Any] = {}

    # 1. dryness and wind (from weather_summary)
    try:
        weather = weather_summary(lat, lon, past_days=days_back)
        daily = weather.get("daily", {})
        time_arr = daily.get("time", [])

        # Today is index days_back, tomorrow is index days_back + 1.
        # This is positional rather than date-based because weather_summary calls Open-Meteo
        # with timezone=auto. "Today" in the returned array is the location's today, and looking
        # it up by the local machine's date would be wrong whenever the AOI is in a different day.
        # Let's check bounds to prevent index errors
        today_idx = days_back
        tomorrow_idx = days_back + 1

        dryness = {}
        wind = {}

        if today_idx < len(time_arr):
            precip_past_today = daily.get("precipitation_sum", [])[:today_idx + 1]
            total_precip = round(sum(precip_past_today), 1) if precip_past_today else 0.0

            days_since = None
            for i in range(len(precip_past_today)):
                idx = len(precip_past_today) - 1 - i
                val = precip_past_today[idx]
                if val is not None and val >= 1.0:
                    days_since = i
                    break

            dryness = {
                "total_precipitation": total_precip,
                "days_since_last_rain": days_since,
                "days_back": days_back,
            }

            today_speed = daily.get("wind_speed_10m_max", [None])[today_idx]
            today_dir = daily.get("wind_direction_10m_dominant", [None])[today_idx]
            tomorrow_speed = daily.get("wind_speed_10m_max", [None])[tomorrow_idx] if tomorrow_idx < len(time_arr) else None
            tomorrow_dir = daily.get("wind_direction_10m_dominant", [None])[tomorrow_idx] if tomorrow_idx < len(time_arr) else None

            wind = {
                "today": {
                    "wind_speed_max": today_speed,
                    "wind_direction_dominant": today_dir,
                    "wind_direction_compass": _compass(today_dir),
                },
                "tomorrow": {
                    "wind_speed_max": tomorrow_speed,
                    "wind_direction_dominant": tomorrow_dir,
                    "wind_direction_compass": _compass(tomorrow_dir),
                },
            }
            brief["dryness"] = dryness
            brief["wind"] = wind
        else:
            brief["weather_error"] = f"Expected at least {days_back + 1} daily weather entries, got {len(time_arr)}"
    except Exception as e:
        brief["weather_error"] = str(e)

    # 2. active events
    try:
        events_data = active_events(bbox=bbox, pad=BRIEF_PAD_DEG)
        brief["events"] = events_data
        for k in ("sources", "all_clear", "all_clear_note", "pad_deg"):
            if k in events_data:
                brief[k] = events_data[k]
    except Exception as e:
        brief["events_error"] = str(e)

    # 3. latest look (earth-search Sentinel-2)
    try:
        datetime_range = last_days_window(days_back)
        img_res = search_imagery(
            "earth-search",
            ["sentinel-2-l2a"],
            bbox=bbox,
            datetime_range=datetime_range,
            limit=50,
        )
        items = img_res.get("items", [])
        latest_look = None
        if items:
            # the pick search_imagery already made (coverage, cloud, recency band), else the same key over the window
            rec_id = (img_res.get("recommended") or {}).get("id")
            best_item = next((it for it in items if it["id"] == rec_id), None) or min(items, key=_scene_key)

            latest_look = {
                "id": best_item["id"],
                "date": best_item["datetime"][:10],
                "cloud": best_item.get("cloud_cover"),
                "covers_aoi_pct": best_item.get("covers_aoi_pct"),
            }
        brief["latest_look"] = latest_look
    except Exception as e:
        brief["latest_look_error"] = str(e)

    # 4. next looks (next_pass)
    try:
        pass_res = next_pass(lat, lon, days=3)
        passes = pass_res.get("passes", [])
        next_looks = []
        for p in passes[:3]:
            next_looks.append({
                "constellation": p.get("constellation"),
                "satellite": p.get("satellite"),
                "time_utc": p.get("time_utc"),
                "daylight": p.get("daylight"),
                "sun_elevation_deg": p.get("sun_elevation_deg"),
                "track_distance_km": p.get("track_distance_km"),
                "kind": p.get("kind"),
                "access": p.get("access"),
            })
        brief["next_looks"] = next_looks
    except Exception as e:
        brief["next_looks_error"] = str(e)

    # 5. signals & summary & note
    dryness_val = brief.get("dryness") or {"error": brief.get("weather_error", "unavailable")}
    wind_val = brief.get("wind") or {"error": brief.get("weather_error", "unavailable")}
    events_val = brief.get("events") or {"error": brief.get("events_error", "unavailable")}

    signals = _conditions_signals(dryness_val, wind_val, events_val)
    brief["signals"] = signals

    summary_parts = []
    for sig in signals:
        if sig:
            summary_parts.append(sig[0].upper() + sig[1:])
    brief["summary"] = ". ".join(summary_parts) + "." if summary_parts else ""

    # Honesty framing note
    brief["note"] = "current conditions from live sources — not a risk model, not a prediction; a source that did not answer is named in sources and an empty list is all-clear only when all_clear is true"

    return brief
