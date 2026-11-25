"""
Cloud-to-ground lightning by PSA and day, from NOAA NCEI's free 'nldn-tiles' summaries
(daily flash counts in 0.1-degree tiles from the National Lightning Detection Network).

  https://www.ncei.noaa.gov/pub/data/swdi/database-csv/v2/
    nldn-tiles-YYYY.csv.gz     full years (a year's file appears some months after it ends)
    nldn-tiles-YYYYMM.csv.gz   monthly files for the current year

NOAA posts these roughly monthly, so the most recent few weeks are always missing. Those days
are left blank (not zero) so they don't look like "no lightning". Lightning is therefore used in
the history/correlation analysis but not in the 7-day forecast.

Each tile's center is matched to a Southwest PSA (and dropped if it falls in an excluded area).
"""
import datetime as dt
import gzip
import io
import os

import pandas as pd

from common import CFG, STORE, TODAY, http_bytes, is_fresh, log

BASE = "https://www.ncei.noaa.gov/pub/data/swdi/database-csv/v2/"
BBOX = (-115.2, 31.0, -102.5, 37.2)  # AZ + NM, generous
FOLDER = os.path.join(STORE, "lightning")


def _files_needed():
    """Newest first: (name, is_monthly)."""
    out = []
    for y in range(TODAY.year, CFG["start_year"] - 1, -1):
        out.append((f"nldn-tiles-{y}", False))
    return out


def _read_tiles(raw_gz, areas, exclusions):
    text = gzip.decompress(raw_gz).decode("utf-8", errors="replace")
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    if not lines:
        return pd.DataFrame(columns=["date", "psa", "strikes"])
    first = [c.strip().upper() for c in lines[0].split(",")]
    has_header = not first[0].replace(".", "").replace("-", "").isdigit()
    df = pd.read_csv(io.StringIO("\n".join(lines)), header=0 if has_header else None, low_memory=False)
    if has_header:
        cols = [str(c).strip().upper() for c in df.columns]
        pick = lambda *ws: next(i for i, c in enumerate(cols) if any(w in c for w in ws))  # noqa: E731
        di, xi, yi, ni = pick("DAY", "DATE"), pick("LON"), pick("LAT"), pick("COUNT", "TOTAL")
    else:
        di, xi, yi, ni = 0, 1, 2, 3
    d = pd.DataFrame({"day": df.iloc[:, di].astype(str).str.replace("-", "").str.slice(0, 8),
                      "lon": pd.to_numeric(df.iloc[:, xi], errors="coerce"),
                      "lat": pd.to_numeric(df.iloc[:, yi], errors="coerce"),
                      "n": pd.to_numeric(df.iloc[:, ni], errors="coerce")})
    d = d[(d.lon >= BBOX[0]) & (d.lon <= BBOX[2]) & (d.lat >= BBOX[1]) & (d.lat <= BBOX[3])]
    tiles = d[["lon", "lat"]].drop_duplicates()
    lookup = {}
    for lon, lat in zip(tiles.lon, tiles.lat):
        p = areas.find(lon, lat)
        if p and not any(ex.find(lon, lat) for ex in exclusions):
            lookup[(lon, lat)] = p
    d["psa"] = [lookup.get((x, y)) for x, y in zip(d.lon, d.lat)]
    d = d.dropna(subset=["psa"])
    d["date"] = pd.to_datetime(d["day"], format="%Y%m%d", errors="coerce")
    return d.groupby(["date", "psa"], as_index=False)["n"].sum().rename(columns={"n": "strikes"})


def update(progress, areas, exclusions, max_files=1):
    """Fetch up to max_files new files this run (plus refresh the current-year months)."""
    os.makedirs(FOLDER, exist_ok=True)
    if not areas:
        log("Lightning: no PSA boundaries yet; skipping")
        return
    fetched = 0
    for name, _ in _files_needed():
        year = int(name[-4:])
        key = f"ltg/{name}"
        if progress["done"].get(key):
            continue
        if fetched >= max_files and year != TODAY.year:
            break
        try:
            raw = http_bytes(BASE + name + ".csv.gz", timeout=300)
            df = _read_tiles(raw, areas, exclusions)
            df.to_csv(os.path.join(FOLDER, name + ".csv.gz"), index=False, compression="gzip")
            progress["done"][key] = TODAY.isoformat()
            # a full-year file replaces that year's monthly pieces
            for m in range(1, 13):
                p = os.path.join(FOLDER, f"nldn-tiles-{year}{m:02d}.csv.gz")
                if os.path.exists(p):
                    os.remove(p)
            log(f"Lightning {year}: {int(df.strikes.sum()):,} strikes in SW PSAs")
            fetched += 1
            continue
        except Exception as e:  # noqa: BLE001
            if "404" not in str(e) and "Not Found" not in str(e):
                log(f"Lightning {name} failed: {str(e)[:150]}")
                continue
        # no annual file yet -> monthly files (current year, or last year until its annual file appears)
        for m in range(1, 13):
            if dt.date(year, m, 1) > TODAY:
                break
            mname = f"nldn-tiles-{year}{m:02d}"
            mkey = f"ltg/{mname}"
            recent = (TODAY - dt.date(year, m, 1)).days < 70
            if progress["done"].get(mkey) and not (recent and not is_fresh(progress, mkey + "_checked", 2)):
                continue
            try:
                raw = http_bytes(BASE + mname + ".csv.gz", timeout=180)
                df = _read_tiles(raw, areas, exclusions)
                df.to_csv(os.path.join(FOLDER, mname + ".csv.gz"), index=False, compression="gzip")
                progress["done"][mkey] = TODAY.isoformat()
                progress["notes"][mkey + "_checked"] = TODAY.isoformat()
                fetched += 1
            except Exception as e:  # noqa: BLE001
                progress["notes"][mkey + "_checked"] = TODAY.isoformat()
                if "404" not in str(e) and "Not Found" not in str(e):
                    log(f"Lightning {mname} failed: {str(e)[:150]}")


def daily():
    """Returns (sw_daily, psa_long, covered). sw_daily: date -> strikes (NaN where not covered).
    psa_long: date, psa, strikes (days with strikes only). covered: date -> True if data exists."""
    if not os.path.isdir(FOLDER):
        return None, None, None
    idx = pd.date_range(f"{CFG['start_year']}-01-01", pd.Timestamp(TODAY))
    covered = pd.Series(False, index=idx)
    frames = []
    for f in sorted(os.listdir(FOLDER)):
        if not f.endswith(".csv.gz"):
            continue
        df = pd.read_csv(os.path.join(FOLDER, f), parse_dates=["date"])
        tag = f.replace("nldn-tiles-", "").replace(".csv.gz", "")
        if len(tag) == 4:                       # full year
            start, end = pd.Timestamp(f"{tag}-01-01"), pd.Timestamp(f"{tag}-12-31")
        else:                                   # one month; may be partial, so stop at its last day of data
            start = pd.Timestamp(f"{tag[:4]}-{tag[4:]}-01")
            end = min(start + pd.offsets.MonthEnd(0), df["date"].max() if len(df) else start - pd.Timedelta(days=1))
        covered[(covered.index >= start) & (covered.index <= end)] = True
        if len(df):
            frames.append(df)
    if not frames:
        return None, None, None
    d = pd.concat(frames).groupby(["date", "psa"], as_index=False)["strikes"].sum()
    sw = d.groupby("date")["strikes"].sum().reindex(idx, fill_value=0).astype(float)
    sw[~covered] = float("nan")   # not loaded / not yet posted by NOAA -> unknown, not zero
    sw.index.name = "date"
    return sw, d, covered
