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
TITILER = "https://titiler.xyz"

CATALOGS: dict[str, dict[str, str]] = {
    "earth-search": {
        "stac": "https://earth-search.aws.element84.com/v1",
        "raster": "titiler-xyz",
        "notes": "Element 84 Earth Search: Sentinel-2 L2A/L1C, Sentinel-1, Landsat, NAIP, Copernicus DEM.",
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
    gazet = os.environ.get("GAZET_URL", "https://gazet.ds.io/search")
    try:
        data = _get_json(gazet, params={"q": query})
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
    url: str | None = f"{base}/collections"
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
            score = sum(t in hay for t in terms)
            if score == len(terms):
                hits.append(
                    {
                        "catalog": cat,
                        "id": c.get("id"),
                        "title": c.get("title"),
                        "summary": (c.get("description") or "")[:240],
                        "_score": score,
                    }
                )
    hits.sort(key=lambda h: -h.get("_score", 0))
    for h in hits:
        h.pop("_score", None)
    return hits[:20]


def describe_collection(catalog: str, collection_id: str) -> dict[str, Any]:
    """Get a collection's description, extent, and asset/band layout."""
    base = CATALOGS[catalog]["stac"]
    c = _get_json(f"{base}/collections/{collection_id}")
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
    preview_item, compute_statistics, or render_map.
    """
    if bbox is None and place:
        g = geocode(place)
        if "error" in g:
            return g
        bbox = g["bbox"]
    if bbox is None:
        return {"error": "Provide bbox or place"}
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
    return {"bbox": bbox, "count": len(feats), "items": [_compact_item(catalog, f) for f in feats]}


# ---------------------------------------------------------------- rasters


def _expression_to_bands(expression: str, assets: list[str] | None) -> tuple[str, list[str]]:
    # titiler.xyz /stac merges the assets list into bands b1..bN, so a friendly
    # expression like (nir-red)/(nir+red) must become (b1-b2)/(b1+b2) with
    # assets=[nir, red] in matching order.
    funcs = {"where", "abs", "sqrt", "min", "max", "log", "exp", "sin", "cos", "b"}
    idents = [
        t
        for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expression)
        if t not in funcs and not re.fullmatch(r"b\d+", t)
    ]
    if not assets:
        assets = list(dict.fromkeys(idents))  # order of first appearance
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
) -> dict[str, Any]:
    """Get a browser-openable PNG preview URL for a STAC item.

    Routes to the right raster backend per catalog: titiler.xyz for
    Earth Search, NASA VEDA's raster API, or Planetary Computer's signing
    data API. Optional rescale like "0,3000" for non-visual assets.
    """
    assets = assets or _default_assets(collection_id)
    if catalog == "planetary-computer":
        item = _get_json(
            f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
        )
        rp = item.get("assets", {}).get("rendered_preview", {}).get("href")
        if rp:
            return {"preview_url": rp, "backend": "pc-data-api"}
        return {"error": "No rendered_preview asset on this Planetary Computer item"}
    if catalog == "veda":
        params = [("assets", a) for a in assets or ["cog_default"]]
        if rescale:
            params.append(("rescale", rescale))
        params.append(("max_size", str(max_size)))
        q = "&".join(f"{k}={v}" for k, v in params)
        return {
            "preview_url": f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}/preview.png?{q}",
            "backend": "veda-raster-api",
        }
    # earth-search and any catalog with public item URLs -> titiler.xyz
    self_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    params = [("url", self_url)] + [("assets", a) for a in assets]
    if rescale:
        params.append(("rescale", rescale))
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
    NDVI on Sentinel-2 (Earth Search asset names). Optionally clip to an AOI
    GeoJSON Feature. Backed by TiTiler /stac/statistics.
    """
    if catalog == "planetary-computer":
        return {"error": "statistics not wired for planetary-computer yet; use earth-search or veda"}
    self_url = f"{CATALOGS[catalog]['stac']}/collections/{collection_id}/items/{item_id}"
    params: list[tuple[str, str]] = [("url", self_url), ("max_size", "512")]
    if expression:
        expression, assets = _expression_to_bands(expression, assets)
        params.append(("expression", expression))
    for a in assets or _default_assets(collection_id) or ["visual"]:
        params.append(("assets", a))
    base = (
        f"https://openveda.cloud/api/raster/collections/{collection_id}/items/{item_id}/statistics"
        if catalog == "veda"
        else f"{TITILER}/stac/statistics"
    )
    if catalog == "veda":
        params = [p for p in params if p[0] != "url"]
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
    "(nir-red)/(nir+red)" with rescale="-1,1" and colormap_name="rdylgn" —
    supported on earth-search; ignored for veda/planetary-computer.
    """
    # with an expression, assets must be the expression's bands, never defaults
    assets = assets or ([] if expression else _default_assets(collection_id))
    if catalog == "planetary-computer":
        params = [("collection", collection_id), ("item", item_id)] + [
            ("assets", a) for a in assets
        ]
        if rescale:
            params.append(("rescale", rescale))
        q = str(httpx.QueryParams(params))
        return (
            "https://planetarycomputer.microsoft.com/api/data/v1/item/tiles/"
            "WebMercatorQuad/{z}/{x}/{y}@1x.png?" + q
        )
    if catalog == "veda":
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

_MAP_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js" integrity="sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznwA0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj" crossorigin="anonymous"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet" integrity="sha384-MinO0mNliZ3vwppuPOUnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V" crossorigin="anonymous">
<style>
  html, body, #map { margin: 0; height: 100%; }
  #panel { position: absolute; top: 12px; left: 12px; z-index: 2; background: rgba(255,255,255,.94);
    padding: 12px 16px; border-radius: 10px; font: 14px/1.45 system-ui, sans-serif; max-width: 340px;
    box-shadow: 0 2px 10px rgba(0,0,0,.18); }
  #panel h1 { font-size: 15px; margin: 0 0 4px; }
  #panel p { margin: 4px 0 8px; color: #444; }
  #panel label { display: block; margin: 2px 0; cursor: pointer; }
  #credit { position: absolute; bottom: 24px; left: 12px; z-index: 2; font: 11px system-ui, sans-serif;
    color: #333; background: rgba(255,255,255,.8); padding: 2px 8px; border-radius: 6px; }
</style></head><body>
<div id="map"></div>
<div id="panel"><h1>__TITLE__</h1><p>__SUBTITLE__</p><div id="toggles"></div></div>
<div id="credit">groundstation · Development Seed labs · STAC + TiTiler</div>
<script>
const LAYERS = __LAYERS__;
const BBOX = __BBOX__;
const map = new maplibregl.Map({
  container: "map",
  style: { version: 8, sources: { basemap: { type: "raster",
      tiles: ["https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png"], tileSize: 256,
      attribution: "&copy; OpenStreetMap &copy; CARTO" } },
    layers: [{ id: "basemap", type: "raster", source: "basemap" }] },
  bounds: [[BBOX[0], BBOX[1]], [BBOX[2], BBOX[3]]], fitBoundsOptions: { padding: 40 }
});
map.addControl(new maplibregl.NavigationControl());
map.on("load", () => {
  const toggles = document.getElementById("toggles");
  LAYERS.forEach((l, i) => {
    const id = "layer" + i;
    if (l.type === "raster") {
      map.addSource(id, { type: "raster", tiles: [l.tiles], tileSize: 256 });
      map.addLayer({ id, type: "raster", source: id,
        paint: { "raster-opacity": l.opacity ?? 1 } });
    } else if (l.type === "geojson") {
      map.addSource(id, { type: "geojson", data: l.data });
      map.addLayer({ id: id + "-fill", type: "circle", source: id,
        paint: { "circle-radius": 7, "circle-color": l.color || "#d63b3b",
                 "circle-opacity": .85, "circle-stroke-width": 1.5, "circle-stroke-color": "#fff" },
        filter: ["==", ["geometry-type"], "Point"] });
      map.addLayer({ id: id + "-line", type: "line", source: id,
        paint: { "line-color": l.color || "#d63b3b", "line-width": 2 },
        filter: ["!=", ["geometry-type"], "Point"] });
      map.on("click", id + "-fill", (e) => {
        const p = e.features[0].properties;
        new maplibregl.Popup().setLngLat(e.lngLat)
          .setHTML("<b>" + (p.title || p.name || "") + "</b><br>" + (p.description || p.date || ""))
          .addTo(map);
      });
    }
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
</script></body></html>
"""


def render_map(
    title: str,
    bbox: list[float],
    layers: list[dict[str, Any]],
    subtitle: str = "",
    out_path: str | None = None,
) -> dict[str, Any]:
    """Write a self-contained interactive HTML map and return its file path.

    layers is a list of:
      {"type": "item", "name": ..., "catalog": ..., "collection_id": ...,
       "item_id": ..., "assets": [...], "rescale": "0,3000", "colormap_name": ...,
       "expression": "(nir-red)/(nir+red)"}  # expression for index layers
      {"type": "raster", "name": ..., "tiles": "https://..{z}/{x}/{y}.."}
      {"type": "geojson", "name": ..., "data": <FeatureCollection>, "color": "#hex"}
    Item layers resolve to the right tiling backend automatically. The HTML
    is shareable: MapLibre + live tile URLs, no server of ours required.
    """
    resolved = []
    for l in layers:
        if l.get("type") == "item":
            resolved.append(
                {
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
                }
            )
        else:
            resolved.append(l)
    html = (
        _MAP_TEMPLATE.replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__LAYERS__", json.dumps(resolved))
        .replace("__BBOX__", json.dumps(bbox))
    )
    if out_path is None:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title.lower())[:60]
        out_dir = Path(os.environ.get("GROUNDSTATION_OUT", Path.cwd() / "demo"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"map-{safe}.html")
    Path(out_path).write_text(html, encoding="utf-8")
    return {"map_path": out_path, "layers": [l["name"] for l in resolved]}


# ---------------------------------------------------------------- monitoring


def active_events(bbox: list[float] | None = None, days: int = 30) -> dict[str, Any]:
    """Current natural events and disaster alerts, optionally filtered to a bbox.

    Combines NASA EONET (wildfires, storms, volcanoes, floods...) and GDACS
    global disaster alerts. bbox is [w, s, e, n]. Returns compact event lists
    with coordinates suitable for mapping.
    """
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

    Returns daily max/min temperature, precipitation, and max wind for
    past_days back and 7 days ahead — enough to flag anomalies in a briefing.
    """
    data = _get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
            "past_days": past_days,
            "forecast_days": 7,
            "timezone": "auto",
        },
    )
    return {"units": data.get("daily_units"), "daily": data.get("daily")}
