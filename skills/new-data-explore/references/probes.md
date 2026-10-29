# Probe recipes

Small reads first. Run these with `uv run python` from the groundstation repo, which already has every library named here.

## A COG or GeoTIFF over HTTP

```bash
curl -sI "$URL" | grep -i content-length      # size before anything else
uv run rio info "/vsicurl/$URL"                # CRS, transform, nodata, dtype, bands
```

A positive 5th transform value (y pixel size) means south-up. `nodata: null` means read the data card for the fill value.

## A local GeoTIFF: shares, nodata-safe

```python
import rasterio, numpy as np
from rasterio.enums import Resampling
with rasterio.open(path) as r:
    a = r.read(out_shape=(r.count, r.height // 8, r.width // 8), resampling=Resampling.nearest)
    nd = r.nodata
valid = (a != nd).all(0) if nd is not None else np.ones(a.shape[1:], bool)
```

Then compute shares only over `valid`.

## Tiles onto one grid (south-up safe)

```python
from rasterio.warp import reproject, transform_bounds
from rasterio.transform import from_origin
ub = transform_bounds("EPSG:4326", crs, W, S, E, N)
tr = from_origin(ub[0], ub[3], res, res)
out = np.full((bands, h, w), nodata, dtype)
for f in files:
    with rasterio.open(f) as r:
        reproject(rasterio.band(r, list(range(1, bands + 1))), out, dst_transform=tr, dst_crs=crs,
                  dst_nodata=nodata, resampling=Resampling.nearest, init_dest_nodata=False)
```

`init_dest_nodata=False` keeps earlier tiles when a later one only partly covers the grid.

## A Zarr store on S3-compatible storage, anonymous

```python
import zarr
from obstore.store import S3Store
zarr.config.set({"array.sharding_coalesce_max_gap_bytes": 0})  # fetch only the bytes asked for
s3 = S3Store.from_url("s3://bucket/path/store.zarr", endpoint="https://data.source.coop", skip_signature=True)
root = zarr.open_group(zarr.storage.ObjectStore(s3, read_only=True), mode="r")
print(dict(root.attrs)); print(list(root.group_keys())[:5])
```

Then one pixel, timed:

```python
from pyproj import Transformer
g = root[f"utm{int((lon + 180) // 6) + 1:02d}"]
x, y = Transformer.from_crs("EPSG:4326", g.attrs["proj:code"], always_xy=True).transform(lon, lat)
a, _, c, _, e, f = g.attrs["spatial:transform"][:6]
col, row = int((x - c) / a), int((y - f) / e)
q = g["embeddings"][0, :, row, col]
```

A window is the same slice with ranges; time one pixel first and scale.

## Labels on embeddings: the pairing check

Nearest-centroid on cosine similarity, enough to see whether a pairing works before reaching for a real classifier:

```python
X = E.reshape(dims, -1).T                     # pixels x dims, decoded
idx = np.flatnonzero(confident_labels_mask.ravel()); rng.shuffle(idx)
train, test = idx[:4000], idx[4000:]
C = np.stack([X[train][y[train] == k].mean(0) for k in classes])
C /= np.linalg.norm(C, axis=1, keepdims=True)
pred = np.array(classes)[(X / np.linalg.norm(X, axis=1, keepdims=True).clip(1e-6) @ C.T).argmax(1)]
agreement = (pred[test] == y[test]).mean()
```

Report agreement on held-out pixels and each class's share in both maps; a class the classifier over-calls is a caveat, not a finding.
