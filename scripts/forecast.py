"""
7-day Southwest PL forecast from observed FEMS values and the archive - one for the Southwest as a
whole and one for each PSA.

Only the Southwest has a PL, so "the forecast for a PSA" means: using ONLY that PSA's own data,
what are the chances the Southwest PL is 1, 2, 3, 4 or 5 on each of the next 7 days? Comparing the
PSAs shows which parts of the Southwest are pushing toward a higher PL.

No weather forecasts are used. For each day ahead (1-7) a separate model learns from the archive how
today's situation turned out that many days later. Inputs (all known today):
  * today's Southwest PL
  * PSA ERCy / BI percentiles (workbook method), FEMS ERC, BI, 1000-hr FM, KBDI (median of stations)
  * how those changed over the last 7 days
  * new fire starts in the last 7 days
  * time of year, and pre-monsoon / monsoon / off-season for the target day

Honesty check (refreshed daily): each model is tested year by year - trained on the other years,
then asked to predict the left-out year - and compared with the simplest forecast, "PL stays where
it is". PL is very persistent, so that's hard to beat; the useful numbers are how many real PL
changes the model saw coming, and how often it was right when it called one.
"""
import datetime as dt
import json
import math
import os
import warnings

import numpy as np
import pandas as pd

from common import dump_json, CFG, DATA, STORE, TODAY, log

H = CFG["forecast"]["horizons"]
SW_INPUTS = ["psa_erc_pct_mean", "psa_bi_pct_mean", "psa_n_erc_90", "psa_n_erc_97", "psa_n_bi_90",
             "fems_erc", "fems_bi", "fems_1000_hr_fm", "fems_kbdi"]
PSA_INPUTS = ["psa_erc_pct", "psa_bi_pct", "psa_erc_avg", "psa_bi_avg", "fems_erc", "fems_bi", "fems_1000_hr_fm", "fems_kbdi"]
CHANGE_OF = ("psa_erc_pct_mean", "psa_erc_pct", "fems_erc", "fems_1000_hr_fm")


def _features(df, inputs):
    X = pd.DataFrame(index=df.index)
    X["pl_now"] = df["pl"]
    for k in range(1, 6):
        X[f"pl_is_{k}"] = (df["pl"] == k).astype(float)
    used = []
    for c in inputs:
        if c in df and df[c].notna().sum() > 200:
            X[c] = df[c]
            used.append(c)
            if c in CHANGE_OF:
                X[c + "_chg7"] = df[c] - df[c].shift(7)
    if "fire_new_starts" in df:
        X["fires_last_7d"] = df["fire_new_starts"].rolling(7, min_periods=7).sum()
        used.append("fire_new_starts")
    return X, used


def _season(dates):
    a = CFG["analysis"]
    md = dates.strftime("%m-%d")
    sp = a["default_monsoon_split"]
    doy = dates.dayofyear.to_numpy()
    return pd.DataFrame({"pre_monsoon": ((md >= a["season_start"]) & (md < sp)).astype(float),
                         "monsoon": ((md >= sp) & (md <= a["season_end"])).astype(float),
                         "doy_sin": np.sin(2 * math.pi * doy / 365.25), "doy_cos": np.cos(2 * math.pi * doy / 365.25)},
                        index=dates)


def _model():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=3000))


def _xy(X, pl, h):
    tgt = X.index + pd.Timedelta(days=h)
    Xh = X.copy()
    s = _season(pd.DatetimeIndex(tgt))
    for c in s:
        Xh[c] = s[c].to_numpy()
    y = pl.reindex(tgt).to_numpy()
    return Xh, y


def forecast_scope(df, inputs, backtest):
    out = {"days": [], "backtest": [], "inputs_used": []}
    if df["pl"].dropna().empty:
        return out
    X, used = _features(df, inputs)
    out["inputs_used"] = used
    base = df["pl"].dropna().index.max()
    # use the latest day that has the inputs (FEMS may lag the PL by a day)
    have = X.dropna()
    if have.empty:
        out["note"] = "Not enough FEMS history yet."
        return out
    base = min(base, have.index.max())
    base_pl = int(df.at[base, "pl"])
    out.update(base_date=base.strftime("%Y-%m-%d"), base_pl=base_pl,
               today={c: (None if pd.isna(X.at[base, c]) else round(float(X.at[base, c]), 1)) for c in X.columns if not c.startswith("pl_is")})
    years = sorted(set(X.dropna().index.year))
    for h in range(1, H + 1):
        Xh, y = _xy(X, df["pl"], h)
        ok = Xh.notna().all(axis=1).to_numpy() & ~np.isnan(y)
        date = (base + pd.Timedelta(days=h)).strftime("%Y-%m-%d")
        if ok.sum() < CFG["forecast"]["min_training_days"] or len(set(y[ok])) < 2:
            out["days"].append({"h": h, "date": date, "most_likely": base_pl, "probs": {str(base_pl): 1.0},
                                "p_up": 0.0, "p_down": 0.0, "method": "PL stays the same (not enough history yet)"})
            continue
        Xt, yt = Xh[ok], y[ok].astype(int)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = _model().fit(Xt, yt)
            p = m.predict_proba(Xh.loc[[base]].fillna(Xt.median()))[0]
        cls = [int(c) for c in m.classes_]
        out["days"].append({"h": h, "date": date, "method": "model",
                            "probs": {str(c): round(float(v), 3) for c, v in zip(cls, p)},
                            "most_likely": cls[int(np.argmax(p))],
                            "p_up": round(float(sum(v for c, v in zip(cls, p) if c > base_pl)), 3),
                            "p_down": round(float(sum(v for c, v in zip(cls, p) if c < base_pl)), 3)})
        if backtest:
            yrs = pd.Index(Xt.index.year)
            pred, per, act = [], [], []
            for yr in years:
                tr, te = yrs != yr, yrs == yr
                if te.sum() == 0 or len(set(yt[tr])) < 2:
                    continue
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    pred += list(_model().fit(Xt[tr], yt[tr]).predict(Xt[te]))
                per += list(Xt[te]["pl_now"].astype(int))
                act += list(yt[te])
            if act:
                pred, per, act = map(np.array, (pred, per, act))
                chg, called = act != per, pred != per
                out["backtest"].append({"h": h, "n": int(len(act)), "acc_model": round(float((pred == act).mean()), 3),
                                        "acc_persist": round(float((per == act).mean()), 3), "changes": int(chg.sum()),
                                        "caught": int(((pred == act) & chg).sum()), "called": int(called.sum()),
                                        "called_right": int(((pred == act) & called).sum())})
    return out


def run(sw, psa_tables, pl):
    """sw: SW daily inputs; psa_tables: {code: daily inputs}; pl: SW PL series."""
    cache = os.path.join(STORE, "forecast_backtest.json")
    old = json.load(open(cache)) if os.path.exists(cache) else {}
    do_bt = old.get("date") != TODAY.isoformat()
    res = {"issued_utc": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
           "method": "Observed FEMS values + archive only (no weather forecasts)", "sw": None, "psa": {}}
    res["sw"] = forecast_scope(sw.join(pl, how="left"), SW_INPUTS, do_bt)
    for code, t in sorted(psa_tables.items()):
        try:
            res["psa"][code] = forecast_scope(t.join(pl, how="left"), PSA_INPUTS, do_bt)
        except Exception as e:  # noqa: BLE001
            log(f"Forecast {code} failed: {str(e)[:120]}")
    if do_bt:
        json.dump({"date": TODAY.isoformat(), "sw": res["sw"].get("backtest", []),
                   "psa": {k: v.get("backtest", []) for k, v in res["psa"].items()}}, open(cache, "w"))
    else:  # reuse today's backtest
        res["sw"]["backtest"] = old.get("sw", [])
        for k, v in res["psa"].items():
            v["backtest"] = old.get("psa", {}).get(k, [])
    dump_json(res, os.path.join(DATA, "forecast.json"), indent=0)
    # archive: one entry per base date (latest run that day wins) - lets past forecasts be checked
    apath = os.path.join(STORE, "forecast_archive.json")
    arch = json.load(open(apath)) if os.path.exists(apath) else {}
    def brief(f):
        return {"pl": f.get("base_pl"), "days": [[d["h"], d["most_likely"], d.get("p_up", 0), d.get("p_down", 0)] for d in f.get("days", [])]}
    if res["sw"].get("base_date"):
        arch[res["sw"]["base_date"]] = {"sw": brief(res["sw"]), "psa": {k: brief(v) for k, v in res["psa"].items()}}
    json.dump(arch, open(apath, "w"), separators=(",", ":"))
    dump_json(arch, os.path.join(DATA, "forecast_archive.json"), separators=(",", ":"))
    d = res["sw"].get("days", [])
    if d:
        log("Forecast SW: " + ", ".join(f"+{x['h']}d PL{x['most_likely']} (up {x.get('p_up', 0):.0%})" for x in d))
    return res
