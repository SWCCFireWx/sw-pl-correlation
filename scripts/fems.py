"""
FEMS daily NFDRS + weather: fetch, store per station, and build daily tables.

Requests follow FEMS limits: up to 10 stations x 1 year per request for backfill; all stations at
once for the most recent days (under 2 weeks). Observations only - no FEMS forecasts are used.
Station-level values are stored in STORE/fems/<kind>_<year>.csv.gz (columns: station, date, values).
"""
import datetime as dt
import glob
import io
import os
import re
import time
import urllib.parse

import pandas as pd

from common import CFG, STORE, TODAY, http, log, minutes_used, save_progress

ENDPOINTS = {"nfdr": ["download-nfdr-daily-summary/", "download-nfdr-dailysummary/"],
             "wx": ["download-wx-daily-summary/", "download-wx-dailysummary/"]}
FOLDER = os.path.join(STORE, "fems")


def fetch(kind, ids, start, end, dataset="observation"):
    last = None
    for base in CFG["fems"]["base_urls"]:
        for ep in ENDPOINTS[kind]:
            q = {"dataset": dataset, "startDate": start.isoformat(), "endDate": end.isoformat(),
                 "dataFormat": "csv", "stationIds": ",".join(ids)}
            if kind == "nfdr":
                q["fuelModels"] = ",".join(CFG["fems"]["fuel_models"])
            try:
                text = http(base + ep + "?" + urllib.parse.urlencode(q))
            except Exception as e:  # noqa: BLE001
                last = repr(e)
                continue
            lines = [l for l in text.strip().splitlines() if l.strip()]
            if "<html" in text[:500].lower() or not lines or lines[0].count(",") < 2:
                last = f"unexpected reply: {text[:150]!r}"
                continue
            return text
    raise RuntimeError(last)


def snake(s):
    s = re.sub(r"\(.*?\)", "", str(s))
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


SKIP = ("station_name", "latitude", "longitude", "elevation", "flag", "nfdr_type", "nfdrtype", "qa", "time",
        "aspect", "slope", "nesdis", "wrcc", "wims", "county", "state", "azimuth")


def parse(text, kind, name2id):
    df = pd.read_csv(io.StringIO(text), low_memory=False)
    if df.empty:
        return pd.DataFrame()
    df.columns = [snake(c) for c in df.columns]
    tcol = next((c for c in df.columns if c in ("nfdr_type", "nfdrtype")), None)
    sid = next((c for c in df.columns if c in ("station_id", "stationid", "wims_id")), None) or \
        next((c for c in df.columns if "station" in c and "name" not in c), None) or \
        next((c for c in df.columns if "station" in c), None)
    dcol = next((c for c in df.columns if "date" in c and "zone" not in c), None) or \
        next((c for c in df.columns if "observation_time" in c or c == "time" or c.endswith("_time")), None)
    if not sid or not dcol:
        raise RuntimeError(f"can't find station/date columns in {list(df.columns)[:12]}")
    dates = pd.to_datetime(df[dcol].astype(str).str.slice(0, 10), errors="coerce")
    fuel = next((c for c in df.columns if "fuel" in c and "model" in c), None)
    stn = df[sid].astype(str).str.strip()
    out = pd.DataFrame({"station": [name2id.get(x.upper(), x) for x in stn], "date": dates})
    if tcol is not None:
        out["obs_type"] = df[tcol].astype(str).str.upper().str.slice(0, 1)
    multi = fuel is not None and df[fuel].nunique() > 1
    prefix = "fems" if kind == "nfdr" else "wx"
    for c in df.columns:
        if c in (sid, dcol, fuel, tcol) or "station" in c or any(w in c for w in SKIP):
            continue
        v = pd.to_numeric(df[c], errors="coerce")
        if v.notna().mean() < 0.5:
            continue
        if multi:
            for fm, sub in v.groupby(df[fuel]):
                out.loc[sub.index, f"{prefix}_{c}_fm{str(fm).lower()}"] = sub
        else:
            out[f"{prefix}_{c}"] = v
    out = out.dropna(subset=["date"])
    keys = ["station", "date"] + (["obs_type"] if "obs_type" in out else [])
    return out.groupby(keys, as_index=False).mean(numeric_only=True)


def _store(kind, year):
    return os.path.join(FOLDER, f"{kind}_{year}.csv.gz")


def merge(kind, year, new):
    if new.empty:
        return
    new = new.drop(columns=[c for c in ("obs_type",) if c in new])
    p = _store(kind, year)
    if os.path.exists(p):
        old = pd.read_csv(p, parse_dates=["date"], dtype={"station": str})
        k = set(zip(new["station"], new["date"]))
        old = old[[x not in k for x in zip(old["station"], old["date"])]]
        new = pd.concat([old, new], ignore_index=True)
    new.sort_values(["date", "station"]).to_csv(p, index=False, float_format="%.2f", compression="gzip")


def update(progress, stations):
    os.makedirs(FOLDER, exist_ok=True)
    ids = [s["station_id"] for s in stations]
    name2id = {str(s.get("station_name", "")).strip().upper(): s["station_id"] for s in stations}
    per, budget = CFG["fems"]["stations_per_request"], CFG["fems"]["max_requests_per_run"]
    stop_at = minutes_used() + CFG["fems"]["max_minutes_per_run"]

    # (a) recent days, all stations
    start = TODAY - dt.timedelta(days=CFG["fems"]["recent_days"])
    for kind in ("nfdr", "wx"):
        got = 0
        nb = CFG["fems"].get("recent_batch_size", 10)
        for i in range(0, len(ids), nb):
            try:
                df = parse(fetch(kind, ids[i:i + nb], start, TODAY), kind, name2id)
                for y, part in df.groupby(df["date"].dt.year):
                    merge(kind, int(y), part)
                got += len(df)
            except Exception as e:  # noqa: BLE001
                log(f"FEMS recent {kind} batch {i // nb + 1} failed: {str(e)[:200]}")
        log(f"FEMS recent {kind}: {got} station-days refreshed")

    # (b) backfill, newest years first
    for year in range(TODAY.year, CFG["start_year"] - 1, -1):
        for kind in ("nfdr", "wx"):
            stale = (TODAY - dt.timedelta(days=1)).isoformat()
            todo = [s for s in ids if not (progress["done"].get(f"{kind}/{s}/{year}") and
                                           (year < TODAY.year or progress["done"][f"{kind}/{s}/{year}"] >= stale))]
            while todo:
                if budget <= 0 or minutes_used() > stop_at:
                    return
                batch, todo = todo[:per], todo[per:]
                try:
                    df = parse(fetch(kind, batch, dt.date(year, 1, 1), min(dt.date(year, 12, 31), TODAY)), kind, name2id)
                    merge(kind, year, df)
                    stamp = dt.datetime.utcnow().isoformat(timespec="minutes")
                    have = set(df["station"]) if len(df) else set()
                    for s in batch:
                        progress["done"][f"{kind}/{s}/{year}"] = stamp
                    unknown = sorted(x for x in have if x not in set(ids))
                    if unknown:
                        log(f"FEMS {kind} {year}: station names not matched to IDs: {unknown[:5]}")
                    log(f"FEMS {kind} {year}: {len(have & set(batch))}/{len(batch)} stations with data")
                except Exception as e:  # noqa: BLE001
                    log(f"FEMS {kind} {year} stations {batch[0]}..: {str(e)[:200]}")
                budget -= 1
                save_progress(progress)
                time.sleep(1)


def load_long(station_ids=None):
    """All stored station-days, NFDRS + weather side by side."""
    files = sorted(glob.glob(os.path.join(FOLDER, "*.csv.gz")))
    if not files:
        return None
    parts = {"nfdr": [], "wx": []}
    for f in files:
        parts[os.path.basename(f).split("_")[0]].append(pd.read_csv(f, parse_dates=["date"], dtype={"station": str}))
    long = None
    for lst in parts.values():
        if lst:
            d = pd.concat(lst, ignore_index=True)
            long = d if long is None else long.merge(d, on=["station", "date"], how="outer")
    long = long[long["date"] >= pd.Timestamp(f"{CFG['start_year']}-01-01")]
    if station_ids is not None:
        long = long[long["station"].isin(set(station_ids))]
    return long


LOW_IS_DANGER = ("_fm", "moisture", "relative_humidity", "humidity", "precip", "rain", "gsi", "lfi")


def area_daily(long, station_ids):
    """Area-wide value per day = median across reporting stations; plus share of stations at an
    extreme for their own history (top 10%, or bottom 10% for moistures / RH / rain / GSI)."""
    d = long[long["station"].isin(set(station_ids))]
    if d.empty:
        return pd.DataFrame()
    cols = [c for c in d.columns if c not in ("station", "date")]
    g = d.groupby("date")
    out = g[cols].median()
    out["fems_stations_reporting"] = g["station"].nunique()
    fcols = [c for c in cols if c.startswith("fems_")]
    if fcols:
        pct = d.groupby("station")[fcols].rank(pct=True)
        ext = {}
        for c in fcols:
            low = any(w in c for w in LOW_IS_DANGER)
            flag = ((pct[c] <= 0.10) if low else (pct[c] >= 0.90)).where(pct[c].notna())
            ext[c + "__pct_extreme"] = flag.groupby(d["date"]).mean() * 100
        out = pd.concat([out, pd.DataFrame(ext)], axis=1)
    out.index.name = "date"
    return out.sort_index()


def find_col(cols, *names):
    for n in names:
        for c in cols:
            if c == f"fems_{n}" or c.endswith(f"_{n}") and c.startswith("fems_"):
                return c
    return None
