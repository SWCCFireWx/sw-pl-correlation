"""
Merges Southwest PL, area-wide FEMS values and fire activity into one daily table and
measures how well each value lines up with (and leads) the PL.

Periods analyzed (each gets its own full set of results):
  season          March 1 - October 31 (default)
  pre_<MM-DD>     Pre-monsoon fire season: March 1 to the day before the monsoon split
  mon_<MM-DD>     Monsoon season: the split day to October 31
  all             Full year
The monsoon split is computed for each date in config analysis.monsoon_split_options
(July 1 / 8 / 15 by default) so the page can switch between them.

Plain-language method:
  * Spearman (rank) correlation is the headline "match", because PL is a 1-5 ranking.
  * "Lead" = today's value vs. the PL N days later. If a value matches PL better 7-14 days
    out than the same day, it moves BEFORE PL does.
  * Every value also gets a "change over 7 days" version (fire counts get "7-day total").
"""
import datetime as dt
import json
import math
import os
import re
import warnings

from common import dump_json

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
CFG = json.load(open(os.path.join(ROOT, "config.json")))
STORE = os.environ.get("STORE_DIR", os.path.join(ROOT, "store"))
AC = CFG["analysis"]

EXACT = {  # FEMS daily-summary names (after snake_case) -> plain names
    "erc": "Energy Release Component (ERC)", "bi": "Burning Index (BI)", "sc": "Spread Component (SC)",
    "ic": "Ignition Component (IC)", "kbdi": "Keetch-Byram Drought Index (KBDI)", "gsi": "Growing Season Index (GSI)",
    "1_hr_fm": "1-hr fuel moisture", "10_hr_fm": "10-hr fuel moisture", "100_hr_fm": "100-hr fuel moisture",
    "1000_hr_fm": "1000-hr fuel moisture", "herb_fm": "Live herbaceous fuel moisture", "woody_fm": "Live woody fuel moisture",
    "stations_reporting": "Stations reporting",
}
OTHER = {
    "psa_erc_pct_mean": "PSA ERCy percentile - average of the SW PSAs (workbook method)",
    "psa_bi_pct_mean": "PSA BI percentile - average of the SW PSAs (workbook method)",
    "psa_n_erc_90": "Number of PSAs at/above 90th percentile ERCy",
    "psa_n_erc_97": "Number of PSAs at/above 97th percentile ERCy",
    "psa_n_bi_90": "Number of PSAs at/above 90th percentile BI",
    "psa_n_bi_97": "Number of PSAs at/above 97th percentile BI",
    "psa_reporting": "PSAs reporting",
    "psa_erc_avg": "PSA ERCy average (key stations)", "psa_bi_avg": "PSA BI average (key stations)",
    "psa_erc_pct": "PSA ERCy percentile (breakpoints)", "psa_bi_pct": "PSA BI percentile (breakpoints)",
    "lightning_strikes": "Lightning strikes (cloud-to-ground)",
    "imsr_sw_acres_growth": "Acres burned per day, all SW fires (IMSR)",
}
NAMES = [  # substring of the column name -> plain name (first match wins)
    ("energy_release", "Energy Release Component (ERC)"), ("erc", "Energy Release Component (ERC)"),
    ("burning_index", "Burning Index (BI)"), ("spread_component", "Spread Component (SC)"),
    ("ignition_component", "Ignition Component (IC)"), ("kbdi", "Keetch-Byram Drought Index (KBDI)"),
    ("gsi", "Growing Season Index (GSI)"),
    ("thousand_hr", "1000-hr fuel moisture"), ("hundred_hr", "100-hr fuel moisture"),
    ("ten_hr", "10-hr fuel moisture"), ("one_hr", "1-hr fuel moisture"),
    ("herbaceous", "Live herbaceous fuel moisture"), ("woody", "Live woody fuel moisture"),
    ("relative_humidity", "Relative humidity"), ("humidity", "Relative humidity"),
    ("temperature", "Temperature"), ("gust", "Wind gust"), ("wind_speed", "Wind speed"),
    ("wind_azimuth", "Wind direction"), ("precip", "Precipitation"), ("solar", "Solar radiation"),
    ("snow", "Snow flag"), ("stations_reporting", "Stations reporting"),
]
FIRE_NAMES = {
    "fire_new_starts": "New fire starts", "fire_new_start_acres": "Acres of fires started that day",
    "fire_new_large_fires": "New large fires (100+ ac)", "fire_lightning_starts": "Lightning starts",
    "fire_human_starts": "Human-caused starts",
}
SUFFIXES = [("__pct_extreme", None), ("__7d_total", " - 7-day total"), ("__chg7", " - change over 7 days")]


def friendly(col):
    base, tail = col, []
    changed = True
    while changed:
        changed = False
        for s, txt in SUFFIXES:
            if base.endswith(s):
                base = base[: -len(s)]
                tail.insert(0, (s, txt))
                changed = True
    if base in FIRE_NAMES:
        label = FIRE_NAMES[base]
    elif base in OTHER:
        label = OTHER[base]
    else:
        src = "FEMS" if base.startswith("fems_") else "Weather" if base.startswith("wx_") else ""
        raw = base.split("_", 1)[1] if "_" in base else base
        core = re.sub(r"_fm[a-z]$", "", raw)
        label = EXACT.get(core) or next((txt for key, txt in NAMES if key in raw), raw.replace("_", " "))
        stat = next((w for w in ("max", "min", "avg", "mean", "total", "sum") if re.search(rf"(^|_){w}(_|$)", raw)), "")
        fm = re.search(r"_fm([a-z])$", raw)
        label = f"{src}: {label}" + (f" ({stat})" if stat else "") + (f" [fuel model {fm.group(1).upper()}]" if fm else "")
    for s, txt in tail:
        if s == "__pct_extreme":
            low = any(w in base for w in ("_fm", "moisture", "humidity", "precip", "gsi", "lfi"))
            label = f"% of stations in their {'driest' if low else 'highest'} 10%: " + label.split(": ", 1)[-1]
        else:
            label += txt
    return label


def strength(r):
    if r is None or (isinstance(r, float) and math.isnan(r)):
        return "not enough data"
    a = abs(r)
    word = "strong" if a >= 0.6 else "moderate" if a >= 0.4 else "weak" if a >= 0.2 else "little or none"
    return word if a < 0.2 else f"{word}, {'rises' if r > 0 else 'falls'} as PL rises"


# ---------------------------------------------------------------- periods
def mmdd(index):
    return index.strftime("%m-%d")


def periods():
    s, e = AC["season_start"], AC["season_end"]
    out = {"season": ("Mar–Oct fire year", lambda m: (m >= s) & (m <= e)),
           "all": ("Full year", lambda m: np.ones(len(m), dtype=bool))}
    for sp in AC["monsoon_split_options"]:
        out[f"pre_{sp}"] = (f"Pre-monsoon ({s} to day before {sp})", lambda m, sp=sp: (m >= s) & (m < sp))
        out[f"mon_{sp}"] = (f"Monsoon ({sp} to {e})", lambda m, sp=sp: (m >= sp) & (m <= e))
    return out


# ---------------------------------------------------------------- data
def load(scope="SW"):
    pl = pd.read_csv(os.path.join(DATA, "sw_pl.csv"), parse_dates=["date"]).set_index("date")[["pl"]]
    parts = [pl]
    p = os.path.join(DATA, "sw_inputs.csv") if scope == "SW" else os.path.join(STORE, "psa_inputs", f"{scope}_inputs.csv")
    if os.path.exists(p):
        parts.append(pd.read_csv(p, parse_dates=["date"]).set_index("date"))
    df = pd.concat(parts, axis=1).sort_index()
    df = df[(df.index >= pd.Timestamp(f"{CFG['start_year']}-01-01")) & (df.index <= pd.Timestamp(dt.date.today()))]
    return df


def add_derived(df):
    new = {}
    skip = ("fems_stations_reporting", "psa_reporting")
    for c in [c for c in df.columns if c != "pl" and c not in skip]:
        if c.startswith("fire_") or c in ("lightning_strikes", "imsr_sw_acres_growth"):
            new[c + "__7d_total"] = df[c].rolling(7, min_periods=7).sum()
        else:
            new[c + "__chg7"] = df[c] - df[c].shift(7)
    return pd.concat([df, pd.DataFrame(new, index=df.index)], axis=1)


def _rank(a):
    from scipy.stats import rankdata
    return rankdata(a)


def corr_np(x, y):
    """Spearman and Pearson on the days both values exist (numpy, fast)."""
    ok = ~(np.isnan(x) | np.isnan(y))
    n = int(ok.sum())
    if n < 30:
        return None, None, n
    xv, yv = x[ok], y[ok]
    if np.unique(xv).size < 3 or np.unique(yv).size < 2:
        return None, None, n
    def pr(a, b):
        a = a - a.mean()
        b = b - b.mean()
        d = math.sqrt(float((a * a).sum() * (b * b).sum()))
        return float((a * b).sum() / d) if d else None
    return pr(_rank(xv), _rank(yv)), pr(xv, yv), n


def rank(df, mask):
    keep = np.asarray(mask(mmdd(df.index)))
    variables = [c for c in df.columns if c not in ("pl", "fems_stations_reporting", "psa_reporting") and df[c].notna().sum() >= 30]
    targets = {lag: df["pl"].shift(-lag).to_numpy(dtype=float)[keep] for lag in AC["lags_days"]}
    rows = []
    for v in variables:
        x = df[v].to_numpy(dtype=float)[keep]
        e = {"var": v, "lags": {}}
        for lag, y in targets.items():
            rs, rp, n = corr_np(x, y)
            e["lags"][str(lag)] = {"spearman": rs, "pearson": rp, "n": n}
        vals = {k: z["spearman"] for k, z in e["lags"].items() if z["spearman"] is not None}
        if vals:
            b = max(vals, key=lambda k: abs(vals[k]))
            e.update(best_lag=int(b), best_r=vals[b], same_day_r=vals.get("0"), strength=strength(vals[b]))
            e["leads"] = bool(int(b) > 0 and abs(vals[b]) >= 0.2 and e["same_day_r"] is not None
                              and abs(vals[b]) - abs(e["same_day_r"]) >= 0.02)
        else:
            e.update(best_lag=None, best_r=None, same_day_r=None, strength="not enough data", leads=False)
        rows.append(e)
    rows.sort(key=lambda e: -abs(e["best_r"]) if e["best_r"] is not None else 0)
    return rows


def by_pl(df, mask):
    d = df[np.asarray(mask(mmdd(df.index)))]
    cols = [c for c in d.columns if c != "pl"]
    out = {c: {} for c in cols}
    for level, g in d.groupby("pl"):
        q = g[cols].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
        n = g[cols].notna().sum()
        for c in cols:
            if n[c] >= 5:
                out[c][str(int(level))] = {"n": int(n[c]), "p10": round(q.at[0.1, c], 3), "p25": round(q.at[0.25, c], 3),
                                           "median": round(q.at[0.5, c], 3), "p75": round(q.at[0.75, c], 3), "p90": round(q.at[0.9, c], 3)}
    return out


def zscores(df):
    """Each value as standard deviations from its calendar-month normal (computed once)."""
    cols = [c for c in df.columns if c != "pl"]
    m = df.index.month
    mean = df[cols].groupby(m).transform("mean")
    sd = df[cols].groupby(m).transform("std").replace(0, np.nan)
    return (df[cols] - mean) / sd


def raise_events(df, mask, z):
    """Average path of each value from 30 days before to 10 days after each PL increase in the
    period, as standard deviations from that calendar month's normal."""
    pl = df["pl"]
    ups = pl[(pl.diff() > 0)].index
    ups = ups[np.asarray(mask(mmdd(ups)))]
    events, last = [], None
    for d in ups:
        if last is None or (d - last).days >= 10:
            events.append(d)
        last = d
    window = list(range(-30, 11))
    out = {"days": window, "n_events": len(events), "events": [e.strftime("%Y-%m-%d") for e in events], "curves": {}}
    if not events:
        return out
    pos = {d: i for i, d in enumerate(z.index)}
    idx = np.array([[pos.get(e + pd.Timedelta(days=k), -1) for k in window] for e in events])
    Z = z.to_numpy(dtype=float)
    Zp = np.vstack([Z, np.full((1, Z.shape[1]), np.nan)])  # row -1 -> NaN
    arr = Zp[idx]                                          # events x window x vars
    cnt = np.sum(~np.isnan(arr), axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean = np.nanmean(arr, axis=0)
    for j, v in enumerate(z.columns):
        out["curves"][v] = [None if (c < 3 or np.isnan(m)) else round(float(m), 3) for m, c in zip(mean[:, j], cnt[:, j])]
    return out


def clean(x):
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else round(x, 4)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean(v) for v in x]
    return x


def run(scope="SW"):
    df = add_derived(load(scope))
    folder = DATA if scope == "SW" else os.path.join(STORE, "psa_out")
    os.makedirs(folder, exist_ok=True)
    stem = "" if scope == "SW" else f"{scope}_"
    daily = {"date": [d.strftime("%Y-%m-%d") for d in df.index]}
    for c in df.columns:
        daily[c] = [None if pd.isna(v) else round(float(v), 3) for v in df[c]]
    dump_json(daily, os.path.join(folder, f"{stem}daily.json"), separators=(",", ":"))

    P = periods()
    z = zscores(df)
    cov = {}
    for c in df.columns:
        s = df[c].dropna()
        cov[c] = {"first": s.index.min().strftime("%Y-%m-%d") if len(s) else None,
                  "last": s.index.max().strftime("%Y-%m-%d") if len(s) else None, "days": int(len(s))}
    result = {
        "scope": scope,
        "generated_utc": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
        "config": {"lags": AC["lags_days"], "season_start": AC["season_start"], "season_end": AC["season_end"],
                   "splits": AC["monsoon_split_options"], "default_split": AC["default_monsoon_split"],
                   "fuel_models": CFG["fems"]["fuel_models"], "start_year": CFG["start_year"]},
        "periods": {k: v[0] for k, v in P.items()},
        "labels": {c: ("Southwest PL" if c == "pl" else friendly(c)) for c in df.columns},
        "coverage": cov,
        "ranking": {k: rank(df, m) for k, (_, m) in P.items()},
        "by_pl": {k: by_pl(df, m) for k, (_, m) in P.items()},
        "raise_events": {k: raise_events(df, m, z) for k, (_, m) in P.items()},
    }
    dump_json(clean(result), os.path.join(folder, f"{stem}analysis.json"), separators=(",", ":"))
    return result


def main(codes=(), progress=None):
    """Southwest every run; PSAs a few per run in rotation (missing ones first)."""
    res = run("SW")
    print("Top matches, Mar-Oct (Southwest):")
    for e in res["ranking"]["season"][:5]:
        if e["best_r"] is not None:
            print(f"  {res['labels'][e['var']]}: r={e['best_r']:.2f} at {e['best_lag']}d ({e['strength']})")
    notes = progress["notes"] if progress is not None else {}
    avail = [c for c in codes if os.path.exists(os.path.join(STORE, "psa_inputs", f"{c}_inputs.csv"))]
    missing = [c for c in avail if not os.path.exists(os.path.join(STORE, "psa_out", f"{c}_analysis.json"))]
    stale = sorted((c for c in avail if c not in missing), key=lambda c: notes.get(f"analyzed_{c}", ""))
    n = AC.get("psa_analyses_per_run", 4) if progress is not None else len(avail)
    for code in (missing + stale)[:max(n, len(missing))]:
        try:
            run(code)
            notes[f"analyzed_{code}"] = dt.datetime.utcnow().isoformat(timespec="minutes")
        except Exception as e:  # noqa: BLE001
            print(f"analysis {code} failed: {e}")
    # publish every PSA's latest results (kept in the store between runs)
    import shutil
    os.makedirs(os.path.join(DATA, "psa"), exist_ok=True)
    src = os.path.join(STORE, "psa_out")
    for f in (os.listdir(src) if os.path.isdir(src) else []):
        if f.split("_")[0] in set(codes) or not codes:
            shutil.copy2(os.path.join(src, f), os.path.join(DATA, "psa", f))


if __name__ == "__main__":
    import glob as _g
    main(sorted(os.path.basename(p).split("_")[0] for p in _g.glob(os.path.join(STORE, "psa_inputs", "*_inputs.csv"))))
