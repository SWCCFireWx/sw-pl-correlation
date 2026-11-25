"""
Wildfire starts from the national WFIGS / IRWIN incident service: point of origin in AZ or NM,
inside a Southwest PSA and not inside an excluded area, tagged with the PSA they started in.
Counted by discovery date (UTC-7). Acres = final size (else current size, else discovery size),
credited to the start day.
"""
import json
import os
import urllib.parse

import numpy as np
import pandas as pd

from common import CFG, STORE, TODAY, http, log

FOLDER = os.path.join(STORE, "fires")


def _query(where, fields, offset):
    q = {"where": where, "outFields": ",".join(fields), "returnGeometry": "true", "outSR": "4326", "f": "json",
         "resultOffset": offset, "resultRecordCount": 2000, "orderByFields": "OBJECTID"}
    js = json.loads(http(CFG["fires"]["service_url"] + "?" + urllib.parse.urlencode(q)))
    if "error" in js:
        raise RuntimeError(js["error"])
    return js


def _year(year):
    t = (f"FireDiscoveryDateTime >= TIMESTAMP '{year}-01-01 07:00:00' AND "
         f"FireDiscoveryDateTime < TIMESTAMP '{year + 1}-01-01 07:00:00'")
    area = "POOState IN (" + ",".join(f"'{s}'" for s in CFG["fires"]["states"]) + ")"
    for fields in (["FireDiscoveryDateTime", "IncidentSize", "FinalAcres", "DiscoveryAcres", "FireCause", "IncidentName"],
                   ["FireDiscoveryDateTime", "IncidentSize", "FinalAcres", "DiscoveryAcres"]):
        try:
            rows, off = [], 0
            while True:
                js = _query(f"IncidentTypeCategory = 'WF' AND {area} AND {t}", fields, off)
                feats = js.get("features", [])
                for f in feats:
                    a = dict(f["attributes"])
                    g = f.get("geometry") or {}
                    a["lon"], a["lat"] = g.get("x"), g.get("y")
                    rows.append(a)
                if not js.get("exceededTransferLimit") and len(feats) < 2000:
                    break
                off += len(feats)
            return rows
        except Exception as e:  # noqa: BLE001
            log(f"fires {year} query error: {str(e)[:150]}")
    return None


def update(progress):
    os.makedirs(FOLDER, exist_ok=True)
    budget = CFG["fires"]["max_years_per_run"]
    for year in range(TODAY.year, CFG["start_year"] - 1, -1):
        key, current = f"fires/{year}", year == TODAY.year
        if (progress["done"].get(key) and not current) or (budget <= 0 and not current):
            continue
        rows = _year(year)
        if rows is not None:
            pd.DataFrame(rows).to_csv(os.path.join(FOLDER, f"{year}.csv.gz"), index=False, compression="gzip")
            progress["done"][key] = TODAY.isoformat()
            log(f"fires {year}: {len(rows)} wildfire records")
        if not current:
            budget -= 1


def load(areas, excl):
    files = sorted(f for f in os.listdir(FOLDER) if f.endswith(".csv.gz")) if os.path.isdir(FOLDER) else []
    frames = [d for d in (pd.read_csv(os.path.join(FOLDER, f)) for f in files) if len(d)]
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["date"] = (pd.to_datetime(df["FireDiscoveryDateTime"], unit="ms", utc=True)
                  + pd.Timedelta(hours=CFG["analysis"]["local_utc_offset_hours"])).dt.tz_localize(None).dt.normalize()
    acres = df["FinalAcres"] if "FinalAcres" in df else pd.Series(np.nan, index=df.index)
    for c in ("IncidentSize", "DiscoveryAcres"):
        if c in df:
            acres = acres.fillna(df[c])
    df["acres"] = acres.fillna(0).clip(lower=0)
    cause = df["FireCause"].fillna("").astype(str).str.lower() if "FireCause" in df else pd.Series("", index=df.index)
    df["lightning"] = cause.str.contains("natural|lightning").astype(int)
    df["human"] = cause.str.contains("human").astype(int)
    lon = pd.to_numeric(df.get("lon"), errors="coerce") if "lon" in df else pd.Series(np.nan, index=df.index)
    lat = pd.to_numeric(df.get("lat"), errors="coerce") if "lat" in df else pd.Series(np.nan, index=df.index)
    df["psa"] = [areas.find(x, y) if pd.notna(x) and pd.notna(y) and areas else None for x, y in zip(lon, lat)]
    ex = [any(e.find(x, y) for e in excl) if pd.notna(x) and pd.notna(y) else False for x, y in zip(lon, lat)]
    n_ex = int(sum(ex))
    if n_ex:
        log(f"Fires: {n_ex} starts inside excluded areas left out")
    df["lon"], df["lat"] = lon, lat
    df = df[~pd.Series(ex, index=df.index)]
    if areas:  # AZ/NM fires outside every SW PSA (e.g. Arizona Strip, Great Basin area) aren't Southwest fires
        outside = df["psa"].isna() & df["lon"].notna()
        if outside.any():
            log(f"Fires: {int(outside.sum())} AZ/NM starts outside the SW PSAs left out")
        df = df[~outside]
    return df


def daily(df, psa=None):
    big = CFG["fires"]["large_fire_acres"]
    d = df if psa is None else df[df["psa"] == psa]
    g = d.groupby("date")
    out = pd.DataFrame({"fire_new_starts": g.size(), "fire_new_start_acres": g["acres"].sum(),
                        "fire_new_large_fires": g["acres"].apply(lambda s: int((s >= big).sum())),
                        "fire_lightning_starts": g["lightning"].sum(), "fire_human_starts": g["human"].sum()})
    idx = pd.date_range(f"{CFG['start_year']}-01-01", pd.Timestamp(TODAY))
    out = out.reindex(idx, fill_value=0)
    out.index.name = "date"
    return out
