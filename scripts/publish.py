"""Writes the files the map and briefing pages read (docs/data/map/...)."""
import json
import math
import os

import numpy as np
import pandas as pd

from common import CFG, DATA, simplify_geojson

MAP = os.path.join(DATA, "map")


def _dump(obj, fh, **kw):
    """json.dump that never writes NaN/Infinity (invalid JSON for browsers)."""
    def fix(o):
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if isinstance(o, dict):
            return {k: fix(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [fix(v) for v in o]
        return o
    json.dump(fix(obj), fh, allow_nan=False, **kw)
QS = [0.03, 0.10, 0.25, 0.50, 0.75, 0.90, 0.97]


def _num(v, nd=1):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return round(float(v), nd)


def map_columns(long):
    """FEMS columns for the map (config order) plus the first few weather columns."""
    cols = []
    for v in CFG["map"]["variables"]:
        c = f"fems_{v}"
        if c in long.columns:
            cols.append(c)
    if not cols:  # unknown naming: take NFDRS columns as they come
        cols = [c for c in long.columns if c.startswith("fems_")][:12]
    order = [f"wx_{v}" for v in CFG["map"].get("weather_variables", [])]
    wx = [c for c in order if c in long.columns]
    wx += [c for c in long.columns if c.startswith("wx_") and c not in wx][: max(0, CFG["map"]["weather_variables_max"] - len(wx))]
    return cols + wx


def write_all(stations, psa_geojson, long, psa_off, psa_fc, fires_df, labels):
    os.makedirs(MAP, exist_ok=True)
    if psa_geojson:
        _dump(simplify_geojson(psa_geojson), open(os.path.join(MAP, "psas.geojson"), "w"), separators=(",", ":"))

    cols = map_columns(long) if long is not None else []
    # station list + each station's own history quantiles (for "how unusual is today here")
    meta = []
    q = long.groupby("station")[cols].quantile(QS).unstack() if long is not None and cols else None
    for s in stations:
        sid = s["station_id"]
        row = {"id": sid, "name": s.get("station_name"), "psa": s.get("psa"), "key": bool(s.get("key")),
               "in_area": bool(s.get("in_area")), "lat": _num(s.get("latitude"), 4), "lon": _num(s.get("longitude"), 4),
               "elev": s.get("elevation"), "state": s.get("state"), "q": {}}
        if q is not None and sid in q.index:
            for c in cols:
                row["q"][c] = [_num(q.loc[sid, (c, x)]) for x in QS]
        meta.append(row)
    _dump({"vars": cols, "labels": {c: labels.get(c, c) for c in cols}, "quantiles": QS, "stations": meta},
              open(os.path.join(MAP, "stations.json"), "w"), separators=(",", ":"))

    # one values file per year
    years = []
    if long is not None and cols:
        for y, g in long.groupby(long["date"].dt.year):
            dates = pd.date_range(f"{y}-01-01", min(pd.Timestamp(f"{y}-12-31"), g["date"].max()))
            ids = sorted(g["station"].unique())
            vals = {}
            for c in cols:
                piv = g.pivot_table(index="station", columns="date", values=c, aggfunc="mean").reindex(index=ids, columns=dates)
                nd = 0 if any(k in c for k in ("erc", "bi", "sc", "ic", "kbdi")) else 1
                vals[c] = [[_num(v, nd) for v in r] for r in piv.to_numpy()]
            _dump({"dates": [d.strftime("%Y-%m-%d") for d in dates], "stations": ids, "vars": vals},
                      open(os.path.join(MAP, f"values_{y}.json"), "w"), separators=(",", ":"))
            years.append(int(y))

    # PSA official values by day
    out = {"dates": [], "psa": {}, "years": years}
    if psa_off is not None and len(psa_off):
        dates = pd.date_range(psa_off["date"].min(), psa_off["date"].max())
        out["dates"] = [d.strftime("%Y-%m-%d") for d in dates]
        for code, g in psa_off.groupby("psa"):
            g = g.set_index("date").reindex(dates)
            out["psa"][code] = {k: [_num(v, 1) for v in g[k]] for k in ("erc_avg", "bi_avg", "erc_pct", "bi_pct", "erc_class", "bi_class")}
    _dump(out, open(os.path.join(MAP, "psa_daily.json"), "w"), separators=(",", ":"))

    # fire starts by year
    if fires_df is not None and len(fires_df):
        for y, g in fires_df.groupby(fires_df["date"].dt.year):
            rows = [[d.strftime("%Y-%m-%d"), _num(x, 3), _num(yv, 3), _num(a, 1), int(lt), p if isinstance(p, str) else None,
                     (str(n) if isinstance(n, str) else "")]
                    for d, x, yv, a, lt, p, n in zip(g["date"], g["lon"], g["lat"], g["acres"], g["lightning"], g["psa"],
                                                     g["IncidentName"] if "IncidentName" in g else [""] * len(g))]
            _dump(rows, open(os.path.join(MAP, f"fires_{y}.json"), "w"), separators=(",", ":"))
