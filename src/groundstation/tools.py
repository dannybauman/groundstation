"""Plain tool functions over the cloud-native geospatial stack.

Every public function here is MCP-ready: typed args, a docstring that doubles
as the tool description, JSON-serializable returns. The same functions back
the MCP server (server.py) and the briefing generator (briefing/brief.py),
and follow the developmentseed/mcp-toolsets shape so they can be dropped into
that scaffold as a toolset later.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

UA = {"User-Agent": "groundstation/0.1 (Development Seed labs prototype)"}
# titiler.xyz is a shared community endpoint with rate limits (429s under heavy
# use) — point GROUNDSTATION_TITILER at your own TiTiler deployment for real work
TITILER = os.environ.get("GROUNDSTATION_TITILER", "https://titiler.xyz")

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


def slugify(text: str, max_len: int = 60) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower())[:max_len].strip("-")


def _resolve_bbox(place: str | None, bbox: list[float] | None) -> list[float] | dict[str, Any]:
    if bbox is None and place:
        g = geocode(place)
        if "error" in g:
            return g
        bbox = g["bbox"]
    if bbox is None:
        return {"error": "Provide bbox or place"}
    return bbox


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


def _union_coverage_pct(aoi: list[float], boxes: list[list[float]]) -> float:
    """Union coverage of the AOI by several bboxes — exact for axis-aligned
    rectangles (coordinate-sweep grid), so overlapping tiles never double-count."""
    aw, as_, ae, an = aoi
    aoi_area = (ae - aw) * (an - as_)
    if aoi_area <= 0:
        return 0.0
    clipped = []
    for b in boxes:
        if not b or len(b) < 4:
            continue
        w, s, e, n = max(aw, b[0]), max(as_, b[1]), min(ae, b[2]), min(an, b[3])
        if e > w and n > s:
            clipped.append((w, s, e, n))
    if not clipped:
        return 0.0
    xs = sorted({v for r in clipped for v in (r[0], r[2])})
    ys = sorted({v for r in clipped for v in (r[1], r[3])})
    covered = 0.0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            if any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in clipped):
                covered += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return round(100.0 * covered / aoi_area, 1)


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
        if day and it.get("bbox") and it.get("collection"):
            groups.setdefault((it["collection"], day), []).append(it)
    for coll, day in sorted(groups, key=lambda k: k[1], reverse=True):
        remaining = list(groups[(coll, day)])
        chosen: list[dict[str, Any]] = []
        boxes: list[list[float]] = []
        covered = 0.0
        while remaining:
            best, best_gain = None, 0.05  # require a real gain, not float noise
            for it in remaining:
                gain = _union_coverage_pct(aoi_bbox, boxes + [it["bbox"]]) - covered
                if gain > best_gain:
                    best, best_gain = it, gain
            if best is None:
                break
            chosen.append(best)
            boxes.append(best["bbox"])
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


def _pc_mosaic_id(collection_id: str, item_id: str) -> str:
    # PC's registered-mosaic tiler is its canonical rendering path; registered
    # searches persist server-side, so map artifacts built on them stay shareable
    key = f"{collection_id}:{item_id}"
    if key not in _pc_mosaic_cache:
        r = _client.post(
            "https://planetarycomputer.microsoft.com/api/data/v1/mosaic/register",
            json={"collections": [collection_id], "ids": [item_id]},
        )
        r.raise_for_status()
        _pc_mosaic_cache[key] = r.json()["id"]
    return _pc_mosaic_cache[key]


def _get_json(url: str, **kwargs: Any) -> Any:
    r = _client.get(url, **kwargs)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- geocoding


def geocode(query: str) -> dict[str, Any]:
    """Resolve a place name to coordinates and a bounding box.

    Tries Development Seed's Gazet small-model geocoder first, then falls back
    to OSM Nominatim. Use concise names ("Barotse Floodplain", "Chelan County")
    rather than long phrases. Returns {name, lat, lon, bbox: [w, s, e, n], source}.
    """
    # gazet.ds.io currently fronts the Streamlit demo, not the FastAPI /search;
    # this branch activates as soon as a JSON endpoint is exposed (or set GAZET_URL).
    # Once it answers non-JSON we stop asking for a while — geocode is a hot path.
    global _gazet_skip_until
    gazet = os.environ.get("GAZET_URL", "https://gazet.ds.io/search")
    try:
        if time.time() < _gazet_skip_until:
            raise ValueError("gazet marked down")
        r = _client.get(gazet, params={"q": query})
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", ""):
            _gazet_skip_until = time.time() + 900
            raise ValueError("gazet returned non-JSON")
        data = r.json()
        feats = data.get("features") or []
        if feats:
            f = feats[0]
            props = f.get("properties", {})
            geom = f.get("geometry", {})
            bbox = f.get("bbox") or props.get("bbox")
            lon, lat = None, None
            if geom.get("type") == "Point":
                lon, lat = geom["coordinates"][:2]
            if bbox and lon is None:
                lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            if bbox is None and lon is not None:
                d = 0.25
                bbox = [lon - d, lat - d, lon + d, lat + d]
            if lon is not None:
                return {
                    "name": props.get("name") or query,
                    "lat": lat,
                    "lon": lon,
                    "bbox": bbox,
                    "source": "gazet",
                }
    except Exception:
        pass

    def _nominatim(q: str) -> list:
        return _get_json(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 1},
        )

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
    results = []
    for q in dict.fromkeys(attempts):
        results = _nominatim(q)
        if results:
            break
    if not results:
        return {"error": f"No geocoding result for {query!r}"}
    r0 = results[0]
    s, n, w, e = (float(x) for x in r0["boundingbox"])
    return {
        "name": r0.get("display_name", query),
        "lat": float(r0["lat"]),
        "lon": float(r0["lon"]),
        "bbox": [w, s, e, n],
        "source": "nominatim",
    }


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
    return [{"catalog": k, **v} for k, v in CATALOGS.items()]


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


def search_datasets(keywords: str, catalog: str | None = None) -> list[dict[str, Any]]:
    """Find dataset collections matching keywords, across all catalogs or one.

    Case-insensitive match against collection id, title, description, and
    keywords. Returns up to 20 hits with {catalog, id, title, summary}.
    """
    terms = [t.lower() for t in keywords.split() if t]
    hits = []
    for cat in [catalog] if catalog else list(CATALOGS):
        try:
            cols = _collections(cat)
        except Exception as e:
            hits.append({"catalog": cat, "error": str(e)})
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
            if all(t in hay for t in terms):
                hits.append(
                    {
                        "catalog": cat,
                        "id": c.get("id"),
                        "title": c.get("title"),
                        "summary": (c.get("description") or "")[:240],
                    }
                )
    return hits[:20]


def describe_collection(catalog: str, collection_id: str) -> dict[str, Any]:
    """Get a collection's description, extent, and asset/band layout."""
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


# ---------------------------------------------------------------- item search


def _compact_item(catalog: str, item: dict[str, Any]) -> dict[str, Any]:
    props = item.get("properties", {})
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
        "assets": sorted(item.get("assets", {}).keys()),
        "self_url": self_url,
    }


def _bbox_coverage_pct(aoi: list[float], item_bbox: list[float] | None) -> float | None:
    # ponytail: bbox-overlap approximation — a tilted scene footprint covers
    # less than its bbox suggests, and antimeridian-crossing boxes aren't
    # handled; good enough to separate "clips the corner" from "covers the
    # whole AOI" at city scale, swap in real footprint geometry if it matters.
    if not item_bbox or len(item_bbox) < 4:
        return None
    aoi_area = (aoi[2] - aoi[0]) * (aoi[3] - aoi[1])
    if aoi_area <= 0:
        return None
    w = max(aoi[0], item_bbox[0])
    s = max(aoi[1], item_bbox[1])
    e = min(aoi[2], item_bbox[2])
    n = min(aoi[3], item_bbox[3])
    if e <= w or n <= s:
        return 0.0
    return round(100.0 * (e - w) * (n - s) / aoi_area, 1)


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
    covers_aoi_pct — how much of the searched area its bbox covers (100 =
    full coverage; a low value means the scene only clips the area, so say
    so or pick a fuller scene). When no single scene covers the AOI, the
    response also carries full_coverage_set: the newest same-day scene set
    whose union does — render its items as toggleable layers in one
    render_map call instead of answering with a cropped scene.
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
        body["datetime"] = datetime_range
    if max_cloud_cover is not None:
        body["query"] = {"eo:cloud_cover": {"lte": max_cloud_cover}}
    base = CATALOGS[catalog]["stac"]
    r = _client.post(f"{base}/search", json=body)
    if r.status_code == 400 and "sortby" in body:
        body.pop("sortby")  # some APIs reject sortby; retry without
        r = _client.post(f"{base}/search", json=body)
    r.raise_for_status()
    feats = r.json().get("features", [])
    items = [_compact_item(catalog, f) for f in feats]
    for it in items:
        it["covers_aoi_pct"] = _bbox_coverage_pct(bbox, it.get("bbox"))
    result = {"bbox": bbox, "count": len(feats), "items": items}
    best_single = max((it.get("covers_aoi_pct") or 0.0) for it in items) if items else 0.0
    if items and best_single < FULL_COVERAGE_PCT:
        full = find_full_coverage_set(items, bbox)
        if full and len(full["items"]) > 1:
            result["full_coverage_set"] = full
    return result


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
) -> dict[str, Any]:
    """Get a browser-openable PNG preview URL for a STAC item.

    Routes to the right raster backend per catalog: titiler.xyz for
    Earth Search, NASA VEDA's raster API, or Planetary Computer's signing
    data API. Optional rescale like "0,3000" for non-visual assets, and
    expression + colormap_name for index previews (NDVI etc., same shapes
    tile_url_template takes).
    """
    assets = assets or ([] if expression else _default_assets(collection_id))
    if expression and not rescale:
        rescale = "-1,1"  # normalized-difference indices are unreadable unscaled
    backend = CATALOGS[catalog]["raster"]
    if backend == "pc":
        item = _get_json(
            f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
        )
        rp = item.get("assets", {}).get("rendered_preview", {}).get("href")
        if rp:
            return {"preview_url": rp, "backend": "pc-data-api"}
        return {"error": "No rendered_preview asset on this Planetary Computer item"}
    if backend == "veda":
        params = [("assets", a) for a in assets or ["cog_default"]]
        if rescale:
            params.append(("rescale", rescale))
        if colormap_name:
            params.append(("colormap_name", colormap_name))
        params.append(("max_size", str(max_size)))
        q = str(httpx.QueryParams(params))
        return {
            "preview_url": f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}/preview.png?{q}",
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
    q = str(httpx.QueryParams(params))
    return {"preview_url": f"{TITILER}/stac/preview.png?{q}", "backend": "titiler-xyz"}


def compute_statistics(
    catalog: str,
    collection_id: str,
    item_id: str,
    expression: str | None = None,
    assets: list[str] | None = None,
    aoi_geojson: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute pixel statistics (min/max/mean/std/histogram) for a STAC item.

    expression is band math over asset names, e.g. "(nir-red)/(nir+red)" for
    NDVI on Sentinel-2 (Earth Search asset names; Planetary Computer uses band
    ids like B04/B08 — check describe_collection). Optionally clip to an AOI
    GeoJSON Feature. Backed by the catalog's tiler statistics endpoint.
    """
    backend = CATALOGS[catalog]["raster"]
    params: list[tuple[str, str]] = [("max_size", "512")]
    if expression:
        if backend == "pc":  # PC accepts named-asset expressions directly
            params += [("expression", expression), ("asset_as_band", "true")]
            assets = assets or _expression_idents(expression)
        else:
            expression, assets = _expression_to_bands(expression, assets)
            params.append(("expression", expression))
    for a in assets or _default_assets(collection_id) or ["visual"]:
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

    return _slim(stats)


def tile_url_template(
    catalog: str,
    collection_id: str,
    item_id: str,
    assets: list[str] | None = None,
    rescale: str | None = None,
    colormap_name: str | None = None,
    expression: str | None = None,
) -> str:
    """XYZ tile URL template ({z}/{x}/{y}) for a STAC item, for web maps.

    For index layers (NDVI etc.) pass expression over asset names, e.g.
    "(nir-red)/(nir+red)" with colormap_name="rdylgn" — supported on
    earth-search; ignored for veda/planetary-computer. Index expressions
    default to rescale="-1,1" (without a rescale they render blank).
    """
    # with an expression, assets must be the expression's bands, never defaults
    assets = assets or ([] if expression else _default_assets(collection_id))
    if expression and not rescale:
        rescale = "-1,1"  # normalized-difference indices are unreadable unscaled
    backend = CATALOGS[catalog]["raster"]
    if backend == "pc":
        # item-tiles render empty for reprojection-heavy sources (MODIS
        # sinusoidal, GOES geostationary); the registered mosaic handles all
        # collections. Costs one registration POST per unique item (cached).
        mid = _pc_mosaic_id(collection_id, item_id)
        params = [("collection", collection_id)] + [("assets", a) for a in assets]
        if rescale:
            params.append(("rescale", rescale))
        if colormap_name:
            params.append(("colormap_name", colormap_name))
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
    --k-data: #1d4e8f; --k-access: #7a5c2e; --k-tiling: #CF3F02; --k-viz: #2a5c45;
    --k-standard: #6b4c8f; --k-infra: #4a4440; }"""

# the stack layer chunk — injected into _MAP_TEMPLATE only when stack_layer=True,
# so default artifacts carry zero extra bytes
_STACK_CHUNK = """<button id="stack-toggle" aria-expanded="false">Stack</button>
<aside id="stack" aria-label="Technology stack">
  <h2>The stack behind this map</h2>
  <p class="stack-sub">what is actually on screen · tap any entry for what it is and what it talks to</p>
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
  .dot.k-data { background: var(--k-data); } .dot.k-access { background: var(--k-access); }
  .dot.k-tiling { background: var(--k-tiling); } .dot.k-viz { background: var(--k-viz); }
  .dot.k-standard { background: var(--k-standard); } .dot.k-infra { background: var(--k-infra); }
  .s-head { display: flex; flex-direction: column; min-width: 0; }
  .stack-name { font-weight: 600; }
  .stack-name .role { font: 500 9px/1 "Roboto Mono", monospace; letter-spacing: .1em; text-transform: uppercase;
    color: var(--accent); border: 1px solid currentColor; border-radius: 3px; padding: 2px 4px; margin-left: 6px;
    vertical-align: 1px; }
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
</style></head><body>
<div id="map"></div>
<div id="panel"><h1>__TITLE__</h1><p>__SUBTITLE__</p><div id="toggles"></div></div>
<div id="credit">groundstation · Development Seed labs · STAC + TiTiler</div>
__STACK__
<script>
const LAYERS = __LAYERS__;
const BBOX = __BBOX__;
const BASE_STYLE = () => ({ version: 8, sources: { basemap: { type: "raster",
    tiles: ["https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png"], tileSize: 256,
    attribution: "&copy; OpenStreetMap &copy; CARTO" } },
  layers: [{ id: "basemap", type: "raster", source: "basemap" }] });
const MAP_OPTS = { style: BASE_STYLE(), bounds: [[BBOX[0], BBOX[1]], [BBOX[2], BBOX[3]]],
  fitBoundsOptions: { padding: 40 } };

function addLayerTo(m, l, i) {
  const id = "layer" + i;
  if (l.type === "raster") {
    m.addSource(id, { type: "raster", tiles: [l.tiles], tileSize: 256,
      ...(l.bounds ? { bounds: l.bounds } : {}) });
    m.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": l.opacity ?? 1 } });
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
        .setHTML("<b>" + (p.title || p.name || "") + "</b><br>" + (p.description || p.date || ""))
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
     <div class="side-label" style="left:12px">◂ ${rasters[1].name}</div>
     <div class="side-label" style="right:12px">${rasters[0].name} ▸</div>`);
  const mapR = new maplibregl.Map({ container: "map", ...MAP_OPTS });
  const mapL = new maplibregl.Map({ container: "mapL", style: BASE_STYLE(),
    bounds: MAP_OPTS.bounds, fitBoundsOptions: MAP_OPTS.fitBoundsOptions });
  window.gsMaps = [mapL, mapR];  // exposed for scripted screenshots and checks
  mapR.addControl(new maplibregl.NavigationControl());
  mapR.on("load", () => { addLayerTo(mapR, rasters[0], rasters[0].i); geojsons.forEach(g => addLayerTo(mapR, g, g.i)); });
  mapL.on("load", () => { addLayerTo(mapL, rasters[1], rasters[1].i); geojsons.forEach(g => addLayerTo(mapL, g, g.i)); });
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
} else {
  const map = new maplibregl.Map({ container: "map", ...MAP_OPTS });
  window.gsMaps = [map];  // exposed for scripted screenshots and checks
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
      row.appendChild(cb); row.appendChild(document.createTextNode(" " + l.name));
      toggles.appendChild(row);
    });
  });
}
</script></body></html>
"""


def _resolve_layer(l: dict[str, Any]) -> dict[str, Any]:
    """An {"type": "item"} layer becomes a raster layer with tiles + scene bounds."""
    if l.get("type") != "item":
        return l
    item_bounds = l.get("bbox")
    if item_bounds is None:
        # scene footprint bounds stop the map requesting (and the tiler
        # serving 404s for) every out-of-footprint tile in the viewport
        try:
            item = _get_json(
                f"{CATALOGS[l['catalog']]['stac']}/collections/{l['collection_id']}/items/{l['item_id']}"
            )
            item_bounds = item.get("bbox")
        except Exception:
            item_bounds = None
    return {
        "type": "raster",
        "name": l.get("name") or l["item_id"],
        "tiles": tile_url_template(
            l["catalog"],
            l["collection_id"],
            l["item_id"],
            l.get("assets"),
            l.get("rescale"),
            l.get("colormap_name"),
            l.get("expression"),
        ),
        "opacity": l.get("opacity", 1),
        **({"bounds": item_bounds} if item_bounds else {}),
    }


def _artifact_path(out_path: str | None, prefix: str, title: str) -> str:
    if out_path:
        return out_path
    out_dir = Path(os.environ.get("GROUNDSTATION_OUT", Path.cwd() / "demo"))
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{prefix}-{slugify(title)}.html")


def _stack_panel_html(entries: list[dict[str, Any]]) -> str:
    from html import escape

    labels = {"data": "data", "access": "access", "tiling": "tiling", "viz": "visualization",
              "standard": "standards", "infra": "infrastructure"}
    parts, last = [], None
    for e in entries:
        if e["kind"] != last:
            parts.append(f'<div class="stack-group">{labels[e["kind"]]}</div>')
            last = e["kind"]
        # escape everything — instance lines carry caller-supplied collection
        # ids, and even curated stack.md text shouldn't be able to break markup
        # ponytail: click-to-expand via native <details>, not hover — hover has
        # no touch equivalent and details is keyboard-accessible for free
        spk = e.get("speaks-to", "")
        parts.append(
            f'<details class="stack-entry"><summary><span class="dot k-{e["kind"]}"></span><span class="s-head">'
            f'<span class="stack-name">{escape(e["name"])}<span class="role">{escape(e["ds-role"])}</span></span>'
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
) -> str | None:
    from groundstation import stack as _stack

    try:
        components = _stack.parse_stack()
    except (FileNotFoundError, ValueError):
        # a malformed curated file must not take the map down with it — the
        # parser stays loud (evals hit it directly), the artifact stays alive
        return None
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
    facts = {
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
        **(extra_facts or {}),
    }
    entries = _stack.stack_instances(components, facts)
    if not entries:
        return None
    return _STACK_CHUNK.replace("__ENTRIES__", _stack_panel_html(entries))


def render_map(
    title: str,
    bbox: list[float],
    layers: list[dict[str, Any]],
    subtitle: str = "",
    out_path: str | None = None,
    compare: bool | None = None,
    stack_layer: bool = False,
    stack_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a self-contained interactive HTML map and return its file path.

    layers is a list of:
      {"type": "item", "name": ..., "catalog": ..., "collection_id": ...,
       "item_id": ..., "assets": [...], "rescale": "0,3000", "colormap_name": ...,
       "expression": "(nir-red)/(nir+red)",  # expression for index layers
       "bbox": [w, s, e, n]}  # pass the item's bbox when you have it (from
                              # search results) — it skips a STAC re-fetch
      {"type": "raster", "name": ..., "tiles": "https://..{z}/{x}/{y}.."}
      {"type": "geojson", "name": ..., "data": <FeatureCollection>, "color": "#hex"}
    Item layers resolve to the right tiling backend automatically. The HTML
    is shareable: MapLibre + live tile URLs, no server of ours required.

    compare: True renders two raster layers as a draggable swipe (before/after),
    False stacks them as toggleable overlays. Default None auto-decides: swipe
    only when both rasters come from the SAME collection (a comparison), overlay
    when they differ (e.g. burn severity over imagery — pass opacity ~0.75).

    stack_layer: True adds a "Stack" toggle revealing the technologies behind
    the map — the actual collections, tiler, formats, and buckets on screen,
    joined from docs/stack.md. Attribution to projects, never people.
    stack_facts: honest extras the panel can't see from the layers alone —
    pass {"geocoded": True} if you resolved the place via geocode, and
    {"events": True} if an events layer came from active_events. Only claim
    what actually happened.
    """
    resolved = []
    raster_collections: list[str | None] = []
    for l in layers:
        if l.get("type") == "item":
            raster_collections.append(l.get("collection_id"))
        elif l.get("type") == "raster":
            raster_collections.append(None)
        resolved.append(_resolve_layer(l))
    if compare is None:
        # swipe only for a true comparison: two rasters of the same collection
        compare = (
            len(raster_collections) == 2
            and raster_collections[0] is not None
            and raster_collections[0] == raster_collections[1]
        )
    stack_html, stack_note = "", None
    if stack_layer:
        stack_html = _stack_chunk_for(resolved, layers, stack_facts)
        if stack_html is None:
            stack_html, stack_note = "", "stack layer skipped: docs/stack.md missing, malformed, or nothing to attribute"
    html = (
        _MAP_TEMPLATE.replace("__BRAND__", _BRAND_CSS)
        .replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__LAYERS__", json.dumps(resolved))
        .replace("__BBOX__", json.dumps(bbox))
        .replace("__COMPARE__", json.dumps(compare))
        # __STACK__ goes last: its entries carry caller-supplied strings, and
        # replacing it earlier would let a literal "__LAYERS__" inside them be
        # macro-expanded by the later template substitutions
        .replace("__STACK__", stack_html)
    )
    out_path = _artifact_path(out_path, "map", title)
    Path(out_path).write_text(html, encoding="utf-8")
    out = {"map_path": out_path, "layers": [l["name"] for l in resolved]}
    if stack_note:
        out["note"] = stack_note
    return out


# ---------------------------------------------------------------- 3D artifact

TERRAIN_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

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
  #credit { position: absolute; bottom: 24px; left: 12px; z-index: 2; font: 11px "Roboto Mono", monospace;
    color: var(--mid); background: rgba(247,244,239,.85); padding: 2px 8px; border-radius: 6px; }
</style></head><body>
<div id="map"></div>
<div id="panel"><h1>__TITLE__</h1><p>__SUBTITLE__</p>
  <label>Terrain exaggeration: <span id="exagValue">__EXAGGERATION__</span>&times;</label>
  <input id="exaggeration" type="range" min="1" max="3" step="0.1" value="__EXAGGERATION__">
  <button id="flythrough">Fly through</button><button id="reset">Reset view</button>
</div>
<div id="credit">groundstation · Development Seed labs · STAC + TiTiler · terrain: AWS Terrarium (open)</div>
__STACK__
<script>
const STYLE = __STYLE__;
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
  map.easeTo({ center: CENTER, pitch: 60, duration: 0 });
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
    stack_layer: bool = False,
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

    stack_layer: same "Stack" toggle as render_map, and here the panel also
    names the Terrarium terrain actually under the imagery.
    """
    # ponytail: one draped layer; add a second when a real ask needs it
    resolved = _resolve_layer(layer)
    style = {
        "version": 8,
        "sources": {
            "basemap": {
                "type": "raster",
                "tiles": ["https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png"],
                "tileSize": 256,
                "attribution": "&copy; OpenStreetMap &copy; CARTO",
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
    stack_html, stack_note = "", None
    if stack_layer:
        stack_html = _stack_chunk_for([resolved], [layer], {"terrain": True})
        if stack_html is None:
            stack_html, stack_note = "", "stack layer skipped: docs/stack.md missing, malformed, or nothing to attribute"
    html = (
        _MAP3D_TEMPLATE.replace("__BRAND__", _BRAND_CSS)
        .replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__STYLE__", json.dumps(style))
        .replace("__BBOX__", json.dumps(bbox))
        .replace("__EXAGGERATION__", json.dumps(round(float(exaggeration), 2)))
        # __STACK__ last, same trust boundary as render_map: entries carry
        # caller-supplied collection ids that must never be macro-expanded
        .replace("__STACK__", stack_html)
    )
    out_path = _artifact_path(out_path, "map3d", title)
    Path(out_path).write_text(html, encoding="utf-8")
    out = {"path": out_path, "title": title}
    if stack_note:
        out["note"] = stack_note
    return out


# ---------------------------------------------------------------- postcards

_POSTCARD_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__PLACE__ · __DATE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
__BRAND__
  body { margin: 0; padding: 32px 16px; background: var(--paper);
    font: 15px/1.5 "Roboto", system-ui, sans-serif; color: var(--ink); }
  .card { max-width: 720px; margin: 0 auto; background: #fff; border-radius: 12px;
    border-top: 3px solid var(--accent);
    overflow: hidden; box-shadow: 0 2px 14px rgba(0,0,0,.15); }
  .card img { display: block; width: 100%; background: var(--ink); }
  .body { padding: 18px 22px 22px; }
  h1 { font: 700 22px/1.2 "Roboto Condensed", "Roboto", system-ui, sans-serif; margin: 0 0 4px; }
  .meta { color: var(--mid); font-size: 14px; margin: 0 0 12px; }
  .caption { margin: 0 0 16px; }
  .credit { border-top: 1px solid var(--rule); padding-top: 12px; font: 12px/1.6 "Roboto Mono", monospace; color: var(--mid); }
  .credit div { margin: 2px 0; }
  .stack-list { margin-top: 8px; font-size: 11px; }
  .stack-list b { color: var(--ink); font-weight: 600; }
</style></head><body>
<div class="card">
  <img src="data:image/png;base64,__IMAGE__" alt="__PLACE__, __DATE__">
  <div class="body">
    <h1>__PLACE__</h1>
    <p class="meta">__DATE__ · __COLLECTION__</p>
    __CAPTION__
    <div class="credit">
      <div>Development Seed labs · STAC · TiTiler · __SOURCE__</div>
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


def _stack_credit_for(catalog: str, collection_id: str, tiler_host: str | None) -> str | None:
    """The back-of-postcard stack listing: static credit lines, no links, no toggle.

    A postcard is a still — no MapLibre, no live tiles — so the join only
    claims the pipeline that produced the embedded pixels. Links stay out on
    purpose: the card's no-live-URLs guarantee covers the credit block too.
    """
    from html import escape

    from groundstation import stack as _stack

    try:
        components = _stack.parse_stack()
    except (FileNotFoundError, ValueError):
        return None
    entries = _stack.stack_instances(components, {
        "catalogs": [catalog],
        "collections_by_catalog": {catalog: [collection_id]},
        "tiler_hosts": [tiler_host] if tiler_host else [],
    })
    if not entries:
        return None
    lines = "".join(
        f"<div><b>{escape(e['name'])}</b> — {escape(e['instance'])}</div>" for e in entries
    )
    return f'<div class="stack-list"><div>the stack behind this card:</div>{lines}</div>'


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
) -> str:
    import base64

    return (
        _POSTCARD_TEMPLATE.replace("__BRAND__", _BRAND_CSS)
        .replace("__IMAGE__", base64.b64encode(png).decode())
        .replace("__PLACE__", place)
        .replace("__DATE__", date)
        .replace("__COLLECTION__", collection_id)
        .replace("__SOURCE__", source)
        .replace("__CAPTION__", f'<p class="caption">{caption}</p>' if caption else "")
        .replace("__LICENSE__", f"<div>license: {license_}</div>" if license_ else "")
        # __STACK__ last: its lines carry the caller-supplied collection id
        .replace("__STACK__", stack_html or "")
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
    stack_layer: bool = False,
) -> dict[str, Any]:
    """Write a share-ready card for one scene: pixels embedded, attribution baked in.

    Unlike a map artifact, a postcard has no live dependencies — the image is
    embedded as a data URI, so it never goes blank when a signed tile URL
    expires, and it carries no local paths. Use it when someone wants
    something they could post. Where and whether to post stays their call.

    date is what you want printed (usually the scene date, "2026-07-10").
    assets/rescale/colormap_name/expression take the same shapes as
    tile_url_template, so an NDVI card is expression + colormap_name.

    stack_layer: True prints a compact stack listing in the credit block —
    the static back-of-postcard form of the map artifacts' Stack panel, with
    no links, so the card stays free of live URLs.
    """
    p = preview_item(
        catalog, collection_id, item_id, assets, rescale,
        max_size=1024, colormap_name=colormap_name, expression=expression,
    )
    if "preview_url" not in p:
        return p
    r = _client.get(p["preview_url"], timeout=90)
    r.raise_for_status()
    # ponytail: license from collection metadata only; per-item attribution
    # when a catalog needs it
    try:
        license_ = _shareable_license(describe_collection(catalog, collection_id).get("license"))
    except Exception:
        license_ = None
    stack_html, stack_note = None, None
    if stack_layer:
        from urllib.parse import urlsplit

        stack_html = _stack_credit_for(catalog, collection_id, urlsplit(p["preview_url"]).netloc)
        if stack_html is None:
            stack_note = "stack listing skipped: docs/stack.md missing, malformed, or nothing to attribute"
    html = _postcard_html(
        r.content, place, date, collection_id,
        _catalog_source(catalog, collection_id), caption, license_, stack_html,
    )
    if out_path is None:
        out_dir = Path(os.environ.get("GROUNDSTATION_OUT", Path.cwd() / "demo"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"postcard-{slugify(place)}-{slugify(date)}.html")
    Path(out_path).write_text(html, encoding="utf-8")
    out = {"path": out_path}
    if stack_note:
        out["note"] = stack_note
    return out


def compare_dates(
    place: str | None = None,
    bbox: list[float] | None = None,
    window_before: str = "",
    window_after: str = "",
    expression: str = "(nir-red)/(nir+red)",
    max_cloud_cover: float = 40,
    label: str | None = None,
    stack_layer: bool = False,
) -> dict[str, Any]:
    """Compare two time windows over a place: matched scenes, index delta, swipe map.

    The most common Earth question is "what changed?" — this answers it in one
    call. Windows are RFC3339 ranges ("2026-06-01T00:00:00Z/2026-06-30T23:59:59Z");
    window_after defaults to the last 14 days, window_before to roughly one month
    earlier. Picks Sentinel-2 scenes from the SAME MGRS tile (best AOI coverage,
    lowest cloud), computes the expression's stats for both dates, and writes a
    side-by-side swipe map. Returns scenes, means, delta, and map_path.
    stack_layer passes through to the swipe map, and since this tool geocodes
    the place itself, the panel truthfully claims Gazet/Nominatim.
    """
    geocoded = bbox is None and bool(place)
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
        return r.get("items", [])

    after_items, before_items = _search(window_after), _search(window_before)
    if not after_items or not before_items:
        return {"error": f"Not enough scenes: {len(after_items)} after, {len(before_items)} before. Widen windows or cloud limit."}

    shared = {_mgrs_tile(i["id"]) for i in after_items} & {_mgrs_tile(i["id"]) for i in before_items}
    if not shared:
        return {"error": "No shared Sentinel-2 tile between the two windows — try wider windows."}
    best_tile = max(
        shared,
        key=lambda t: max(_coverage(i, bbox) for i in after_items if _mgrs_tile(i["id"]) == t),
    )

    def pick(items: list[dict[str, Any]]) -> dict[str, Any]:
        return min(
            (i for i in items if _mgrs_tile(i["id"]) == best_tile),
            key=lambda i: i.get("cloud_cover") or 100,
        )

    a, b = pick(after_items), pick(before_items)

    def _mean(it: dict[str, Any]) -> float | None:
        try:
            s = compute_statistics("earth-search", it["collection"], it["id"], expression=expression)
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
    m = render_map(
        title=f"{label or place or 'AOI'} — {a['datetime'][:10]} vs {b['datetime'][:10]}",
        subtitle=f"{expression} · drag the divider · tile {best_tile}",
        bbox=bbox,
        layers=[layer(a, f"{a['datetime'][:10]} (after)"), layer(b, f"{b['datetime'][:10]} (before)")],
        stack_layer=stack_layer,
        stack_facts={"geocoded": geocoded},
    )
    delta = round(mean_after - mean_before, 4) if mean_after is not None and mean_before is not None else None
    return {
        "tile": best_tile,
        "after": {"id": a["id"], "date": a["datetime"][:10], "cloud": a.get("cloud_cover"), "mean": mean_after},
        "before": {"id": b["id"], "date": b["datetime"][:10], "cloud": b.get("cloud_cover"), "mean": mean_before},
        "delta": delta,
        "delta_pct": round(delta / abs(mean_before) * 100, 1) if delta is not None and mean_before else None,
        "expression": expression,
        "map_path": m["map_path"],
    }


# ---------------------------------------------------------------- monitoring


def active_events(bbox: list[float] | None = None, days: int = 30, pad: float = 0.0) -> dict[str, Any]:
    """Current natural events and disaster alerts, optionally filtered to a bbox.

    Combines NASA EONET (wildfires, storms, volcanoes, floods...) and GDACS
    global disaster alerts. bbox is [w, s, e, n]; pad widens it by that many
    degrees on every side (nearby events matter). Returns compact event lists
    with coordinates suitable for mapping.
    """
    if bbox and pad:
        bbox = [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]
    out: dict[str, Any] = {"eonet": [], "gdacs": []}
    try:
        params: dict[str, Any] = {"days": days, "status": "open"}
        if bbox:
            w, s, e, n = bbox
            params["bbox"] = f"{w},{n},{e},{s}"  # EONET wants lonmin,latmax,lonmax,latmin
        data = _get_json("https://eonet.gsfc.nasa.gov/api/v3/events", params=params)
        for ev in data.get("events", [])[:50]:
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
        data = _get_json("https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP")
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
    except Exception as e:
        out["gdacs_error"] = str(e)
    return out


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
    return {"units": data.get("daily_units"), "daily": data.get("daily")}
