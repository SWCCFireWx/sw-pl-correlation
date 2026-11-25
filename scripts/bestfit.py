"""
Searches for the best correlation with the Southwest PL - for the Southwest and for each PSA.

What it tries
  * every value (FEMS, weather, PSA ERCy/BI percentiles, fire starts, acres burned)
  * every smoothing: as-is, 3/7/14/30-day average (fire counts and acres: 7/14/30-day totals),
    7- and 14-day change
  * every lead: today's value vs the PL 0, 3, 5, 7, 10, 14, 21 days later
  * every period: Mar-Oct, pre-monsoon, monsoon (default split)
  * the best single value, the best pair and the best trio: every combination of the 10 strongest
    distinct values at the 3 most promising lead times

How it avoids fooling itself
  * single values are ranked by rank correlation (Spearman) AND by how consistent it is year to
    year (share of years with the same sign; median of the per-year correlations)
  * combinations are scored only on years the model did not see (train on all other years, predict
    the left-out year, repeat for every year) - so adding values only "wins" if it helps on new data
  * PL breakpoints for the winning score are found on the data and reported with how often they put
    the day in the right PL, compared with always guessing the most common PL

Results: docs/data/bestfit/<scope>.json (refreshed once a day).
"""
import datetime as dt
import json
import math
import os
import warnings

import numpy as np
import pandas as pd

from common import CFG, DATA, STORE, TODAY, dump_json, log

LEADS = [0, 3, 5, 7, 10, 14, 21]
SUMMED = ("fire_", "imsr_sw_acres_growth")
SKIP = ("fems_stations_reporting", "psa_reporting")


def transforms(s, name):
    out = {"as-is": s}
    if name.startswith(SUMMED) or name in SUMMED:
        for w in (7, 14, 30):
            out[f"{w}-day total"] = s.rolling(w, min_periods=w).sum()
    else:
        for w in (3, 7, 14, 30):
            out[f"{w}-day average"] = s.rolling(w, min_periods=max(2, w // 2)).mean()
        out["7-day change"] = s - s.shift(7)
        out["14-day change"] = s - s.shift(14)
    return out


def _rank(a):
    from scipy.stats import rankdata
    return rankdata(a)


def spearman(x, y):
    ok = ~(np.isnan(x) | np.isnan(y))
    if ok.sum() < 60:
        return None, int(ok.sum())
    a, b = _rank(x[ok]), _rank(y[ok])
    a, b = a - a.mean(), b - b.mean()
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return (float((a * b).sum() / d) if d else None), int(ok.sum())


def period_masks(index):
    a = CFG["analysis"]
    md = index.strftime("%m-%d")
    sp = a["default_monsoon_split"]
    return {"season": (md >= a["season_start"]) & (md <= a["season_end"]),
            f"pre_{sp}": (md >= a["season_start"]) & (md < sp),
            f"mon_{sp}": (md >= sp) & (md <= a["season_end"])}


def thresholds(score, pl):
    """Cut points on the score that best split PL <=k from >k, for each k; accuracy of the result."""
    levels = sorted(set(pl.astype(int)))
    cuts = []
    for k in levels[:-1]:
        lo, hi = score[pl <= k], score[pl > k]
        if len(lo) < 5 or len(hi) < 5:
            continue
        cand = np.unique(np.quantile(score, np.linspace(0.02, 0.98, 97)))
        best, bc = -1, None
        for c in cand:
            bal = 0.5 * ((lo < c).mean() + (hi >= c).mean())   # balanced accuracy for this split
            if bal > best:
                best, bc = bal, c
        cuts.append({"from_pl": int(k), "to_pl": int(k) + 1, "cut": float(bc), "balanced_acc": round(float(best), 3)})
    cuts = sorted(cuts, key=lambda c: c["from_pl"])
    # make them non-decreasing
    for i in range(1, len(cuts)):
        cuts[i]["cut"] = max(cuts[i]["cut"], cuts[i - 1]["cut"])
    pred = np.full(len(score), levels[0])
    for c in cuts:
        pred = np.where(score >= c["cut"], c["to_pl"], pred)
    common = pd.Series(pl).mode().iloc[0]
    return cuts, {"acc": round(float((pred == pl).mean()), 3),
                  "within_one": round(float((np.abs(pred - pl) <= 1).mean()), 3),
                  "baseline_acc": round(float((pl == common).mean()), 3), "baseline_pl": int(common)}


def search(df, scope):
    df = df.copy()
    base = [c for c in df.columns if c != "pl" and c not in SKIP and "__" not in c and df[c].notna().sum() > 300]
    feats = {}
    for c in base:
        for t, s in transforms(df[c], c).items():
            feats[(c, t)] = s
    F = pd.DataFrame({f"{c}|{t}": s for (c, t), s in feats.items()})
    years = df.index.year.to_numpy()
    out = {"scope": scope, "generated": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z", "periods": {}}
    for pname, pmask in period_masks(df.index).items():
        pmask = np.asarray(pmask)
        res = {"single": [], "best": {}}
        # ---- single values
        rows = []
        for lead in LEADS:
            y_all = df["pl"].shift(-lead).to_numpy(dtype=float)
            y = y_all[pmask]
            yrs = years[pmask]
            for key in F.columns:
                x = F[key].to_numpy(dtype=float)[pmask]
                r, n = spearman(x, y)
                if r is None:
                    continue
                per = []
                for yr in np.unique(yrs):
                    m = yrs == yr
                    ry, _ = spearman(x[m], y[m]) if m.sum() >= 60 else (None, 0)
                    if ry is not None:
                        per.append(ry)
                same = float(np.mean([np.sign(p) == np.sign(r) for p in per])) if per else None
                var, t = key.split("|")
                rows.append({"key": key, "var": var, "transform": t, "lead": lead, "r": round(r, 4), "n": n,
                             "years": len(per), "years_same_sign": None if same is None else round(same, 3),
                             "per_year_median": None if not per else round(float(np.median(per)), 4),
                             "score": round(abs(r) * (0.5 + 0.5 * (same or 0)), 4)})
        rows.sort(key=lambda e: -e["score"])
        # keep the best setting per value for the leaderboard, plus the overall top settings
        seen, board = set(), []
        for e in rows:
            if e["var"] not in seen:
                seen.add(e["var"])
                board.append(e)
        res["single"] = board[:40]
        res["top_settings"] = rows[:30]
        if board:
            b = board[0]
            x = F[b["key"]].to_numpy(dtype=float)
            y = df["pl"].shift(-b["lead"]).to_numpy(dtype=float)
            ok = pmask & ~np.isnan(x) & ~np.isnan(y)
            sign = 1 if b["r"] > 0 else -1
            cuts, acc = thresholds(sign * x[ok], y[ok].astype(int))
            for c in cuts:
                c["cut_value"] = round(sign * c["cut"], 3)
            b["breakpoints"], b["fit"] = cuts, acc
            b["direction"] = "higher" if sign > 0 else "lower"
        # ---- best 1, 2 and 3 values (every combination of the strongest candidates, scored on unseen years)
        res["best"] = best_by_size(df, F, rows, pmask, years)
        out["periods"][pname] = res
    return out


SIZES = (1, 2, 3)
CAND_PER_LEAD = 10   # strongest distinct values tried in pairs / trios at each lead
LEADS_TRIED = 3      # the leads where single values did best


def _fit_full(X, y, ok):
    Xm = X[ok].to_numpy(dtype=float)
    mu, sd = Xm.mean(0), Xm.std(0)
    sd[sd == 0] = 1
    Z = np.c_[np.ones(len(Xm)), (Xm - mu) / sd]
    coef = np.linalg.lstsq(Z, y[ok], rcond=None)[0]
    score = np.c_[np.ones(len(X)), (X.to_numpy(dtype=float) - mu) / sd] @ coef
    return mu, sd, coef, score


def best_by_size(df, F, rows, pmask, years):
    """For 1, 2 and 3 values: try every combination of the strongest distinct candidates at the most
    promising leads; keep the one with the best match on left-out years."""
    from itertools import combinations
    lead_rank = []
    for e in rows:
        if e["lead"] not in lead_rank:
            lead_rank.append(e["lead"])
        if len(lead_rank) >= LEADS_TRIED:
            break
    best = {}
    for lead in lead_rank:
        cand, seen_v = [], set()
        for e in rows:
            if e["lead"] == lead and e["var"] not in seen_v:
                seen_v.add(e["var"])
                cand.append(e["key"])
            if len(cand) >= CAND_PER_LEAD:
                break
        y_all = df["pl"].shift(-lead).to_numpy(dtype=float)
        corr = F[cand][pmask].corr().abs()
        for k in SIZES:
            for combo in combinations(cand, k):
                if k > 1 and any(corr.at[a, b] > 0.95 for a, b in combinations(combo, 2)):
                    continue   # near-duplicates add nothing
                cv = _cv_spearman(F[list(combo)], y_all, pmask, years)
                if cv is None:
                    continue
                if str(k) not in best or abs(cv) > abs(best[str(k)]["cv_r"]):
                    best[str(k)] = {"lead": lead, "keys": list(combo), "cv_r": round(cv, 4)}
    out = {}
    for k, b in best.items():
        X = F[b["keys"]]
        y = df["pl"].shift(-b["lead"]).to_numpy(dtype=float)
        ok = pmask & X.notna().all(axis=1).to_numpy() & ~np.isnan(y)
        mu, sd, coef, score = _fit_full(X, y, ok)
        cuts, acc = thresholds(score[ok], y[ok].astype(int))
        if len(b["keys"]) == 1 and coef[1] != 0:   # breakpoints in the value's own units
            for c in cuts:
                c["cut_value"] = round(float(mu[0] + sd[0] * (c["cut"] - coef[0]) / coef[1]), 3)
        r_in, _ = spearman(score[ok], y[ok])
        idx = df.index[ok]
        b.update(features=[{"key": kk, "var": kk.split("|")[0], "transform": kk.split("|")[1], "mean": round(float(m), 4),
                            "sd": round(float(s_), 4), "weight": round(float(w), 4)} for kk, m, s_, w in zip(b["keys"], mu, sd, coef[1:])],
                 intercept=round(float(coef[0]), 4), in_sample_r=None if r_in is None else round(r_in, 4),
                 direction="higher" if coef[1] > 0 else "lower", breakpoints=cuts, fit=acc, n=int(ok.sum()),
                 series={"date": [d.strftime("%Y-%m-%d") for d in idx], "score": [round(float(v), 3) for v in score[ok]],
                         "pl_later": [int(v) for v in y[ok]]})
        out[k] = b
    return out


def _cv_spearman(X, y_all, pmask, years):
    """Leave-one-year-out: fit a straight-line model on other years, score the left-out year."""
    Xa = X.to_numpy(dtype=float)
    ok = pmask & ~np.isnan(Xa).any(axis=1) & ~np.isnan(y_all)
    if ok.sum() < 200:
        return None
    Xo, yo, yr = Xa[ok], y_all[ok], years[ok]
    pred = np.full(len(yo), np.nan)
    for u in np.unique(yr):
        te = yr == u
        tr = ~te
        if tr.sum() < 100:
            continue
        mu, sd = Xo[tr].mean(0), Xo[tr].std(0)
        sd[sd == 0] = 1
        Z = np.c_[np.ones(tr.sum()), (Xo[tr] - mu) / sd]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            c = np.linalg.lstsq(Z, yo[tr], rcond=None)[0]
        pred[te] = np.c_[np.ones(te.sum()), (Xo[te] - mu) / sd] @ c
    r, _ = spearman(pred, yo)
    return r


def run(codes, progress, analyze_mod):
    """SW once a day; PSAs a few per run in rotation. Results kept in the store between runs."""
    folder = os.path.join(STORE, "bestfit")
    os.makedirs(folder, exist_ok=True)
    notes = progress["notes"]
    todo = []
    if notes.get("bestfit_SW") != TODAY.isoformat() or not os.path.exists(os.path.join(folder, "SW.json")):
        todo.append("SW")
    stale = sorted([c for c in codes], key=lambda c: notes.get(f"bestfit_{c}", ""))
    todo += [c for c in stale if notes.get(f"bestfit_{c}") != TODAY.isoformat()][:CFG["analysis"].get("psa_analyses_per_run", 4)]
    for scope in todo:
        try:
            df = analyze_mod.load(scope)
            res = search(df, scope)
            res["labels"] = {k: analyze_mod.friendly(k) for k in df.columns}
            dump_json(res, os.path.join(folder, f"{scope}.json"), separators=(",", ":"))
            notes[f"bestfit_{scope}"] = TODAY.isoformat()
            s = res["periods"].get("season", {}).get("best", {})
            if s:
                log(f"Best fit {scope} (Mar-Oct, unseen years): " + "; ".join(
                    f"{k} value{'s' if k != '1' else ''} r={v['cv_r']:.2f}" for k, v in sorted(s.items())))
        except Exception as e:  # noqa: BLE001
            log(f"Best fit {scope} failed: {str(e)[:150]}")
    import shutil
    os.makedirs(os.path.join(DATA, "bestfit"), exist_ok=True)
    for f in os.listdir(folder):
        shutil.copy2(os.path.join(folder, f), os.path.join(DATA, "bestfit", f))
