"""Shared settings, logging, downloads, storage paths and simple geometry for all scripts."""
import datetime as dt
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = json.load(open(os.path.join(ROOT, "config.json")))
STORE = os.environ.get("STORE_DIR", os.path.join(ROOT, "store"))
DATA = os.path.join(ROOT, "docs", "data")
PROGRESS = os.path.join(STORE, "progress.json")
UA = {"User-Agent": "sw-pl-correlation (GitHub Actions research project)"}
TODAY = dt.date.today()
T0 = time.time()
LOG = []

for d in (STORE, DATA):
    os.makedirs(d, exist_ok=True)


def log(msg):
    print(msg, flush=True)
    LOG.append(f"{dt.datetime.utcnow():%Y-%m-%d %H:%M}Z {msg}")


def minutes_used():
    return (time.time() - T0) / 60


def http_bytes(url, data=None, timeout=180, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict(UA, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http(url, data=None, timeout=180, headers=None):
    return http_bytes(url, data, timeout, headers).decode("utf-8", errors="replace")


def nan_free(o):
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: nan_free(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [nan_free(v) for v in o]
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return nan_free(o.item())
    except ImportError:
        pass
    return o


def dump_json(obj, path, **kw):
    """Write JSON that browsers can always read (no NaN/Infinity)."""
    with open(path, "w") as fh:
        json.dump(nan_free(obj), fh, allow_nan=False, **kw)


def load_progress():
    if os.path.exists(PROGRESS):
        p = json.load(open(PROGRESS))
    else:
        p = {}
    p.setdefault("done", {})
    p.setdefault("notes", {})
    return p


def save_progress(p):
    json.dump(p, open(PROGRESS, "w"), indent=0, sort_keys=True)


def is_fresh(progress, key, days=7):
    return progress["notes"].get(key, "") >= (TODAY - dt.timedelta(days=days)).isoformat()


def psa_code(s):
    """'SW-06N', 'PSA 6N', 'SW06N', 'sw6n' -> 'SW06N'."""
    import re
    s = str(s or "").upper().replace("PSA", "").replace("SW", "").replace("-", "").replace(" ", "")
    m = re.match(r"^0*(\d+)([NS]?)$", s)
    return f"SW{int(m.group(1)):02d}{m.group(2)}" if m else None


# ------------------------------------------------------------------ geometry (lon/lat, GeoJSON)
def _in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _bbox(poly):
    xs = [p[0] for p in poly[0]]
    ys = [p[1] for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


class Areas:
    """A set of GeoJSON polygons with a fast 'which one contains this point' lookup."""

    def __init__(self, geojson, key_fn):
        self.items = []
        for f in (geojson or {}).get("features", []):
            g = f.get("geometry") or {}
            polys = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
            for poly in polys:
                if poly:
                    self.items.append((key_fn(f.get("properties", {})), _bbox(poly), poly))

    def find(self, lon, lat):
        if lon is None or lat is None:
            return None
        for key, (x0, y0, x1, y1), poly in self.items:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_ring(lon, lat, poly[0]) \
                    and not any(_in_ring(lon, lat, h) for h in poly[1:]):
                return key
        return None

    def __bool__(self):
        return bool(self.items)


def simplify_geojson(gj, decimals=3):
    """Round coordinates and drop repeated points (keeps the map file small)."""
    def ring(r):
        out = []
        for p in r:
            q = [round(p[0], decimals), round(p[1], decimals)]
            if not out or q != out[-1]:
                out.append(q)
        return out
    feats = []
    for f in gj.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") == "Polygon":
            c = [ring(r) for r in g["coordinates"]]
        elif g.get("type") == "MultiPolygon":
            c = [[ring(r) for r in p] for p in g["coordinates"]]
        else:
            continue
        feats.append({"type": "Feature", "properties": f.get("properties", {}), "geometry": {"type": g["type"], "coordinates": c}})
    return {"type": "FeatureCollection", "features": feats}
