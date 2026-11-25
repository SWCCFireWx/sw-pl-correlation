"""
Reads the SW fire-danger breakpoints workbook (raw/breakpoints.xlsx) and reproduces the
"PSA Average ERCy & BI" calculation from it.

To update: replace raw/breakpoints.xlsx with a newer copy of the workbook (same layout).
Sheets are found by name, so the file name and tab order don't matter:
  * a tab whose name contains "ERC" and "BREAK"  -> ERC percentile table, one row per PSA
  * a tab whose name contains "BI" and "BREAK"   -> BI percentile table
      layout: A1 label (e.g. "ERCs 8/25"), B1.. percentiles ("10th", "20th", ... "97th"),
              then rows "PSA 1", "PSA 6N", ... with the ERC/BI value at each percentile
  * a tab whose name contains "Calc PSA"         -> key stations: Station id | Name | PSA
  * a tab whose name contains "START HERE"       -> (optional) the FEMS data-pull URL; if present,
      only stations in its stationIds list are used, exactly like the workbook does

PSA average, as in the workbook:  ERCy = ROUNDUP(sum of station ERCs) / stations,  BI = sum / stations.
(Stations that didn't report that day are left out of both the sum and the count.)

Percentile class, as in the workbook's 'Format PSA' tab:
  ERC: >=97th -> 97, >=90th -> 90, >=85th -> 85, >=75th -> 75, >=50th -> 50, else 45
  BI : >=97th -> 97, >=90th -> 90, >=75th -> 75, >=50th -> 50, else 45
"""
import math
import os
import re
import urllib.parse

import openpyxl

from common import ROOT, psa_code

XLSX = os.path.join(ROOT, "raw", "breakpoints.xlsx")
ERC_CLASSES = [97, 90, 85, 75, 50]
BI_CLASSES = [97, 90, 75, 50]


def _sheet(wb, *words):
    for ws in wb.worksheets:
        n = ws.title.upper()
        if all(w.upper() in n for w in words):
            return ws
    return None


def _table(ws):
    rows = list(ws.iter_rows(values_only=True))
    pcts = []
    for v in rows[0][1:]:
        m = re.match(r"\s*(\d+)", str(v or ""))
        pcts.append(int(m.group(1)) if m else None)
    out = {}
    for r in rows[1:]:
        code = psa_code(r[0])
        if not code:
            continue
        vals = {}
        for p, v in zip(pcts, r[1:]):
            if p is not None and isinstance(v, (int, float)):
                vals[p] = float(v)
        if vals:
            out[code] = dict(sorted(vals.items()))
    return str(rows[0][0] or "").strip(), out


def load():
    if not os.path.exists(XLSX):
        return None
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    erc_ws, bi_ws, st_ws = _sheet(wb, "ERC", "BREAK"), _sheet(wb, "BI", "BREAK"), _sheet(wb, "CALC PSA")
    if not (erc_ws and bi_ws and st_ws):
        raise RuntimeError("breakpoints.xlsx: couldn't find the ERC BREAK POINTS, BI BREAK POINTS and Calc PSA tabs")
    erc_label, erc = _table(erc_ws)
    bi_label, bi = _table(bi_ws)

    pulled = None
    url_ws = _sheet(wb, "START HERE")
    if url_ws:
        for r in url_ws.iter_rows(values_only=True):
            for v in r:
                if isinstance(v, str) and "stationIds=" in v:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(v.strip()).query)
                    pulled = set(q.get("stationIds", [""])[0].split(","))
    stations = []
    for r in st_ws.iter_rows(min_row=2, values_only=True):
        if not r or r[0] is None or not str(r[0]).strip().isdigit():
            continue
        sid, name, code = str(r[0]).strip(), str(r[1] or "").strip(), psa_code(r[2])
        if not code:
            continue
        if pulled is not None and sid not in pulled:
            continue
        stations.append({"station_id": sid, "station_name": name, "psa": code})
    # the tables may name a PSA without its N/S letter (e.g. "PSA 14" for SW-14N)
    for tbl in (erc, bi):
        for code in {s["psa"] for s in stations}:
            if code not in tbl and code[-1] in "NS" and code[:-1] in tbl:
                tbl[code] = tbl[code[:-1]]
    return {"erc": erc, "bi": bi, "erc_label": erc_label, "bi_label": bi_label, "stations": stations,
            "source": os.path.basename(XLSX), "modified": os.path.getmtime(XLSX)}


def pct_class(value, table, classes):
    if value is None or (isinstance(value, float) and math.isnan(value)) or not table:
        return None
    for c in classes:
        if c in table and value >= table[c]:
            return c
    return 45


def pct_continuous(value, table):
    """Linear interpolation inside the breakpoint table (10th..97th); 0-100 outside it."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or not table:
        return None
    ps = sorted(table)
    vs = [table[p] for p in ps]
    if value <= vs[0]:
        return max(0.0, ps[0] * (value / vs[0])) if vs[0] > 0 else float(ps[0])
    for i in range(1, len(ps)):
        if value <= vs[i]:
            span = vs[i] - vs[i - 1]
            return ps[i - 1] + (ps[i] - ps[i - 1]) * ((value - vs[i - 1]) / span if span else 1)
    top, prev = vs[-1], vs[-2] if len(vs) > 1 else vs[-1] - 1
    return min(100.0, ps[-1] + 3 * (value - top) / max(top - prev, 1e-6))


def psa_daily(long, bp, erc_col, bi_col):
    """long: station, date, <erc_col>, <bi_col> (key stations) -> one row per PSA per day."""
    import pandas as pd
    if long is None or long.empty or not bp:
        return pd.DataFrame()
    st = pd.DataFrame(bp["stations"])
    d = long.merge(st[["station_id", "psa"]], left_on="station", right_on="station_id", how="inner")
    rows = []
    for (code, date), g in d.groupby(["psa", "date"]):
        e = g[erc_col].dropna() if erc_col in g else pd.Series(dtype=float)
        b = g[bi_col].dropna() if bi_col in g else pd.Series(dtype=float)
        erc_avg = math.ceil(e.sum()) / len(e) if len(e) else None
        bi_avg = b.sum() / len(b) if len(b) else None
        rows.append({"psa": code, "date": date, "erc_avg": erc_avg, "bi_avg": bi_avg, "n_stations": int(max(len(e), len(b))),
                     "erc_pct": pct_continuous(erc_avg, bp["erc"].get(code)),
                     "bi_pct": pct_continuous(bi_avg, bp["bi"].get(code)),
                     "erc_class": pct_class(erc_avg, bp["erc"].get(code), ERC_CLASSES),
                     "bi_class": pct_class(bi_avg, bp["bi"].get(code), BI_CLASSES)})
    return pd.DataFrame(rows)


def sw_summary(psa_df):
    """Southwest-wide daily values from the PSA table."""
    import pandas as pd
    if psa_df.empty:
        return pd.DataFrame()
    g = psa_df.groupby("date")
    out = pd.DataFrame({
        "psa_erc_pct_mean": g["erc_pct"].mean(),
        "psa_bi_pct_mean": g["bi_pct"].mean(),
        "psa_n_erc_90": g["erc_class"].apply(lambda s: int((s >= 90).sum())),
        "psa_n_erc_97": g["erc_class"].apply(lambda s: int((s >= 97).sum())),
        "psa_n_bi_90": g["bi_class"].apply(lambda s: int((s >= 90).sum())),
        "psa_n_bi_97": g["bi_class"].apply(lambda s: int((s >= 97).sum())),
        "psa_reporting": g["psa"].nunique(),
    })
    out.index = pd.to_datetime(out.index)
    out.index.name = "date"
    return out
