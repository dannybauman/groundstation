"""Living worlds: a place as an open-world 3D scene, built from public data.

The core (a few km round the place) carries lidar ground and every treetop the
lidar saw (USGS 3DEP on Planetary Computer) where the US has flown it, with a
NAIP aerial photo; anywhere else it falls back to Copernicus 30 m terrain, the
clearest Sentinel-2 summer pass, and trees placed from the photo's vegetation
with estimated heights. The far ring (tens of km) is Copernicus terrain under
that same Sentinel-2 pass. Towns and peaks come from OpenStreetMap.

The page (static/world.html) grows the forest in the browser the way open-world
engines do: species recipes instanced at every tree, two-layer wind, per-tree
random variation on the GPU, impostors at mid distance, trees streamed in round
the camera to the horizon, a physical sky with sun shadows, cloud shadows and a
rippling river. Everything is embedded in one HTML file, so it opens with no
server and shares as-is.

Reads go through rio-tiler (Reader.part reprojects each source onto the one
metric grid), so a world is the same open stack as every other artifact here.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import math
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

PC = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{c}"
ES = "https://earth-search.aws.element84.com/v1"
UA = {"User-Agent": "groundstation-living-world/1"}
TEMPLATE = Path(__file__).parent / "static" / "world.html"


def _json(url: str, body: dict | None = None, timeout: int = 90) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**UA, **({"Content-Type": "application/json"} if body is not None else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


_tokens: dict[str, str] = {}


def _sign(collection: str, href: str) -> str:
    if collection not in _tokens:
        _tokens[collection] = _json(PC_TOKEN.format(c=collection))["token"]
    return href + ("&" if "?" in href else "?") + _tokens[collection]


def _search(root: str, collections: list[str], bbox: list[float], extra: dict | None = None, limit: int = 50) -> list[dict]:
    return _json(f"{root}/search", {"collections": collections, "bbox": bbox, "limit": limit, **(extra or {})}).get("features", [])


def utm_crs(lon: float, lat: float) -> str:
    return f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"


def read_grid(hrefs: list[str], crs: str, bounds: tuple, w: int, h: int, bands: tuple | None = None,
              resampling: str = "bilinear", nodata: float | None = None) -> np.ndarray | None:
    """Read sources onto one grid with rio-tiler; later sources fill earlier gaps (nan)."""
    import rasterio
    from rio_tiler.io import Reader

    out = None
    with rasterio.Env(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        for href in hrefs:
            try:
                with Reader(href) as src:
                    img = src.part(bounds, dst_crs=crs, bounds_crs=crs, width=w, height=h, indexes=bands or (1,),
                                   nodata=nodata, resampling_method=resampling, reproject_method=resampling)
            except Exception:  # one unreadable tile is not the end of a world
                continue
            a = np.ma.filled(img.array.astype(np.float32), np.nan)
            a = a if bands else a[0]
            out = a if out is None else np.where(np.isnan(out), a, out)
    return out


def local_maxima(h: np.ndarray, radius: int) -> np.ndarray:
    """Treetops: cells that are the highest within `radius` cells."""
    pad = np.pad(h, radius, mode="edge")
    best = np.full(h.shape, -np.inf, dtype=np.float32)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                best = np.maximum(best, pad[radius + dy: radius + dy + h.shape[0], radius + dx: radius + dx + h.shape[1]])
    return h >= best


def despike(h: np.ndarray, limit: float = 40.0, reach: int = 7, passes: int = 6) -> np.ndarray:
    """A laser return off something in the air comes back as a pillar: replace cells far
    from the median of a wide ring round them, repeating so a wide patch is eaten in."""
    gh, gw = h.shape
    for _ in range(passes):
        pad = np.pad(h, reach, mode="edge")
        ring = np.stack([pad[dy:dy + gh, dx:dx + gw] for dy in range(0, 2 * reach + 1, 2) for dx in range(0, 2 * reach + 1, 2)])
        med = np.median(ring, axis=0)
        bad = np.abs(h - med) > limit
        if not bad.any():
            break
        h = np.where(bad, med, h).astype(np.float32)
    return h


def jpeg_b64(rgb: np.ndarray) -> str:
    from rio_tiler.models import ImageData

    arr = np.clip(np.nan_to_num(rgb), 0, 255).astype("uint8")
    return base64.b64encode(ImageData(arr).render(img_format="JPEG", QUALITY=88)).decode()


def _geocode(place: str) -> tuple[float, float, str]:
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    hits = _json(f"https://nominatim.openstreetmap.org/search?{q}")
    if not hits:
        raise ValueError(f'no place found for "{place}"')
    return float(hits[0]["lon"]), float(hits[0]["lat"]), hits[0]["display_name"]


def _landmarks(bbox: list[float], name: str, lon: float, lat: float) -> list[dict]:
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    q = f'[out:json][timeout:40];(node["place"~"city|town|village"]({s},{w},{n},{e});node["natural"="peak"]["name"]({s},{w},{n},{e}););out body;'
    try:
        els = _json("https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": q}), timeout=60).get("elements", [])
    except Exception:
        els = []
    num = lambda v: float(re.sub(r"[^0-9.]", "", v) or 0)
    towns = sorted([x for x in els if x.get("tags", {}).get("place") in ("city", "town", "village") and x["tags"].get("name")],
                   key=lambda x: -num(x["tags"].get("population", "0")))
    peaks = sorted([x for x in els if x.get("tags", {}).get("natural") == "peak" and x["tags"].get("ele")],
                   key=lambda x: -num(x["tags"]["ele"]))
    out = [{"name": name, "lon": lon, "lat": lat, "focus": True}]
    for x in towns[:5] + peaks[:2]:
        if math.hypot(x["lon"] - lon, x["lat"] - lat) > 0.004:
            out.append({"name": x["tags"]["name"], "lon": x["lon"], "lat": x["lat"], "peak": x["tags"].get("natural") == "peak"})
    return out


def _buildings(core: list[float], crs: str, cx: float, cy: float, cap: int = 4000) -> list[dict]:
    """Every building OpenStreetMap has in the core: footprint in local metres, height from
    its height tag, else its storeys at 3.2 m, else 6 m."""
    from rasterio.warp import transform as warp_xy

    s, w, n, e = core[1], core[0], core[3], core[2]
    q = f'[out:json][timeout:60];way["building"]({s},{w},{n},{e});out geom;'
    try:
        els = _json("https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": q}), timeout=90).get("elements", [])
    except Exception:
        return []
    out = []
    for el in els[:cap]:
        g = el.get("geometry") or []
        if len(g) < 4:
            continue
        t = el.get("tags", {})
        try:
            hgt = float(re.sub(r"[^0-9.]", "", t.get("height", "")) or 0) or float(t.get("building:levels", 0) or 0) * 3.2 or 6.0
        except ValueError:
            hgt = 6.0
        xs, ys = warp_xy("EPSG:4326", crs, [q_["lon"] for q_ in g], [q_["lat"] for q_ in g])
        out.append({"p": [[round(x - cx, 1), round(y - cy, 1)] for x, y in zip(xs, ys)][:-1], "h": round(min(hgt, 200), 1), "k": t.get("building", "yes")})
    return out


def _best_sentinel(far: list[float]) -> tuple[str, list[dict], float, bool]:
    """The clearest summer day whose scenes together cover the far box."""
    year, best = dt.date.today().year, None
    for y in (year, year - 1):
        feats = _search(ES, ["sentinel-2-l2a"], far, {"datetime": f"{y}-06-15T00:00:00Z/{y}-09-15T23:59:59Z",
                                                      "query": {"eo:cloud_cover": {"lt": 8}}}, limit=200)
        days: dict[str, list[dict]] = {}
        for f in feats:
            days.setdefault(f["properties"]["datetime"][:10], []).append(f)
        for day, fs in days.items():
            u = [min(f["bbox"][0] for f in fs), min(f["bbox"][1] for f in fs), max(f["bbox"][2] for f in fs), max(f["bbox"][3] for f in fs)]
            covers = u[0] <= far[0] and u[1] <= far[1] and u[2] >= far[2] and u[3] >= far[3]
            cloud = sum(f["properties"].get("eo:cloud_cover", 0) for f in fs) / len(fs)
            if best is None or (0 if covers else 1, cloud) < (0 if best[3] else 1, best[2]):
                best = (day, fs, cloud, covers)
        if best and best[3]:
            break
    if not best:
        raise ValueError("no clear Sentinel-2 summer pass over this place in the last two summers")
    return best


def build_world(place: str, radius_km: float = 25, core_km: float = 4, name: str | None = None,
                profile: dict | None = None, at: tuple[float, float] | None = None) -> dict[str, Any]:
    """Gather and shape every layer of a world; returns the page's embedded blocks and its record."""
    from rasterio.warp import transform as warp_xy, transform_bounds

    lon, lat, found = (at[0], at[1], place) if at else _geocode(place)
    name = name or place.split(",")[0].strip()
    crs = utm_crs(lon, lat)
    kx, ky = 111.32 * math.cos(math.radians(lat)), 110.57
    core = [lon - core_km / 2 / kx, lat - core_km * 0.4 / ky, lon + core_km / 2 / kx, lat + core_km * 0.4 / ky]
    far = [lon - radius_km / kx, lat - radius_km / ky, lon + radius_km / kx, lat + radius_km / ky]
    core_b, far_b = transform_bounds("EPSG:4326", crs, *core), transform_bounds("EPSG:4326", crs, *far)
    cx, cy = (core_b[0] + core_b[2]) / 2, (core_b[1] + core_b[3]) / 2
    rel = lambda b: [round(b[0] - cx, 1), round(b[1] - cy, 1), round(b[2] - cx, 1), round(b[3] - cy, 1)]
    in_us = 15 < lat < 72 and -170 < lon < -60
    blocks: dict[str, str] = {}

    # far: Copernicus terrain, Sentinel-2 true colour
    dems = [f["assets"]["data"]["href"] for f in _search(ES, ["cop-dem-glo-30"], far)]
    fw = 1024
    fh = int(round(fw * (far_b[3] - far_b[1]) / (far_b[2] - far_b[0])))
    far_h = read_grid(dems, crs, far_b, fw, fh, nodata=-32767)
    if far_h is None:
        raise ValueError("no terrain could be read for this place")
    far_h = np.nan_to_num(far_h, nan=float(np.nanmin(far_h))).astype(np.float32)
    s2_day, s2, s2_cloud, s2_covers = _best_sentinel(far)
    pw = 4096
    ph = int(round(pw * (far_b[3] - far_b[1]) / (far_b[2] - far_b[0])))
    blocks["far_photo"] = jpeg_b64(read_grid([f["assets"]["visual"]["href"] for f in s2], crs, far_b, pw, ph, bands=(1, 2, 3), nodata=0))
    blocks["far_heights"] = base64.b64encode(far_h.tobytes()).decode()

    # core ground: lidar where flown, else the 30 m terrain
    cell = 3.0
    gw, gh = int(round((core_b[2] - core_b[0]) / cell)), int(round((core_b[3] - core_b[1]) / cell))
    fill = read_grid(dems, crs, core_b, gw, gh, nodata=-32767)
    dtm = _search(PC, ["3dep-lidar-dtm"], core) if in_us else []
    ground = read_grid([_sign("3dep-lidar-dtm", f["assets"]["data"]["href"]) for f in dtm], crs, core_b, gw, gh) if dtm else None
    hag_items = _search(PC, ["3dep-lidar-hag"], core) if dtm else []
    hag = read_grid([_sign("3dep-lidar-hag", f["assets"]["data"]["href"]) for f in hag_items], crs, core_b, gw, gh) if hag_items else None
    lidar = np.isfinite(ground) if ground is not None else np.zeros((gh, gw), bool)
    if ground is not None:
        lidar &= ~(np.isfinite(fill) & (np.abs(ground - fill) > 120))
        ground = np.where(lidar, ground, fill)
    else:
        ground = fill
    ground = despike(np.nan_to_num(ground, nan=float(np.nanmin(fill))).astype(np.float32))
    blocks["core_heights"] = base64.b64encode(ground.tobytes()).decode()

    # core photo: NAIP where it exists, else the Sentinel pass, with red and near-infrared for vegetation
    cpw = 4096
    cph = int(round(cpw * (core_b[3] - core_b[1]) / (core_b[2] - core_b[0])))
    naip = _search(PC, ["naip"], core, {"sortby": [{"field": "datetime", "direction": "desc"}]}) if in_us else []
    if naip:
        year = naip[0]["properties"]["datetime"][:4]
        img = read_grid([_sign("naip", f["assets"]["image"]["href"]) for f in naip if f["properties"]["datetime"][:4] == year],
                        crs, core_b, cpw, cph, bands=(1, 2, 3, 4), resampling="lanczos", nodata=0)
        rgb, red, nir, photo = img[:3], img[0], img[3], f"NAIP {year}"
    else:
        rgb = read_grid([f["assets"]["visual"]["href"] for f in s2], crs, core_b, cpw, cph, bands=(1, 2, 3), resampling="lanczos", nodata=0)
        red = read_grid([f["assets"]["red"]["href"] for f in s2], crs, core_b, cpw, cph, nodata=0)
        nir = read_grid([f["assets"]["nir"]["href"] for f in s2], crs, core_b, cpw, cph, nodata=0)
        photo = f"Sentinel-2 {s2_day}"
    blocks["core_photo"] = jpeg_b64(rgb)

    # trees: every treetop the lidar saw; elsewhere from the photo's green
    rng = np.random.default_rng(7)
    have = np.isfinite(hag) if hag is not None else np.zeros((gh, gw), bool)
    hagc = np.where(have & (np.nan_to_num(hag) > 0) & (np.nan_to_num(hag) < 110), np.nan_to_num(hag), 0).astype(np.float32) if hag is not None else np.zeros((gh, gw), np.float32)
    ys, xs = np.nonzero(local_maxima(hagc, 1) & (hagc > 5))
    measured = np.stack([core_b[0] + (xs + 0.5) * cell - cx, core_b[3] - (ys + 0.5) * cell - cy, ground[ys, xs], hagc[ys, xs], np.zeros(len(xs))], axis=1)
    ndvi = (nir - red) / np.maximum(nir + red, 1)
    veg = ndvi[(np.arange(gh)[:, None] * cph / gh).astype(int).clip(0, cph - 1), (np.arange(gw)[None, :] * cpw / gw).astype(int).clip(0, cpw - 1)]
    ey, ex = np.nonzero((~have) & np.isfinite(veg) & (veg > 0.32) & (rng.random(veg.shape) < 0.11))
    jit = lambda n: (rng.random(n) - 0.5) * cell
    est_h = np.clip(18 + (veg[ey, ex] - 0.32) * 80 + rng.normal(0, 5, len(ey)), 8, 55)
    estimated = np.stack([core_b[0] + (ex + 0.5) * cell + jit(len(ex)) - cx, core_b[3] - (ey + 0.5) * cell + jit(len(ey)) - cy, ground[ey, ex], est_h, np.ones(len(ex))], axis=1)
    trees = np.concatenate([measured, estimated]).astype(np.float32)
    blocks["trees"] = base64.b64encode(trees.tobytes()).decode()

    buildings = _buildings(core, crs, cx, cy)
    marks = _landmarks(far, name, lon, lat)
    for m in marks:
        x, y = warp_xy("EPSG:4326", crs, [m["lon"]], [m["lat"]])
        m["xy"] = [round(x[0] - cx, 1), round(y[0] - cy, 1)]
    meta = {
        "name": name, "place": found, "crs": crs, "centre_lonlat": [lon, lat],
        "core": {"extent": rel(core_b), "grid": [gw, gh], "photo": [cpw, cph], "lidar_share": round(float(lidar.mean()), 3)},
        "far": {"extent": rel(far_b), "grid": [fw, fh], "photo": [pw, ph]},
        "trees": {"measured": int(len(measured)), "estimated": int(len(estimated)), "tallest_m": round(float(trees[:, 3].max()), 1) if len(trees) else 0},
        "sources": {"ground": "USGS 3DEP lidar" if lidar.any() else "Copernicus DEM 30 m", "core_photo": photo,
                    "far_photo": f"Sentinel-2 {s2_day}", "terrain": "Copernicus DEM GLO-30"},
        "landmarks": marks, "buildings": buildings, "profile": profile or None,
    }
    caveats = []
    if not lidar.any():
        caveats.append("no lidar here: the ground is 30 m terrain and every tree is placed from the photo's vegetation with an estimated height")
    elif lidar.mean() < 0.9:
        caveats.append(f"lidar covers {lidar.mean() * 100:.0f}% of the core; trees elsewhere are placed from the photo with estimated heights")
    if not naip:
        caveats.append(f"the core photo is Sentinel-2 at 10 m, not an aerial photo; up close the ground is soft")
    if not s2_covers:
        caveats.append(f"no single clear summer day covered the whole distance; edges may be blank")
    if not profile:
        caveats.append("no study of the place was given: the forest uses the default Pacific Northwest species; pass profile= to grow the place's own")
    caveats.append("trees are generated from recipes and rules (species by distance to water, wind, variety); their positions and heights are data, their shapes are not")
    return {"meta": meta, "blocks": blocks, "caveats": caveats, "found": found, "sentinel": {"day": s2_day, "cloud_pct": round(s2_cloud, 2)}}


def write_world_html(world: dict[str, Any], path: str | Path) -> Path:
    """The template with the world's data embedded as base64 blocks: one file, no server."""
    page = TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", world["meta"]["name"]).replace("__WORLD__", "world")
    blocks = [f'<script type="application/json" id="w-meta">{json.dumps(world["meta"])}</script>']
    blocks += [f'<script type="application/octet-stream" id="w-{k}">{v}</script>' for k, v in world["blocks"].items()]
    page = page.replace('<script type="module">', "\n".join(blocks) + '\n<script type="module">', 1)
    p = Path(path)
    p.write_text(page, encoding="utf-8")
    return p
