"""
Builds docs/data/sw_pl.csv (columns: date,pl,source) from
  1. raw/PL_Levels.xlsx   - the Southwest PL history sheet (2004 onward)
  2. store/pl_live.csv    - days read live from the SWCC/NICC pages by update.py

Sheet layout expected (same as the one provided):
  Row 1      : blank, 2004, 2005, ... (one column per year)
  Rows 2-366 : month/day in column A, PL for that day in each year column
  (summary rows below - "Count Days", "Average First" - are ignored)

The sheet has no Feb 29 row. For leap years Feb 29 is filled from the surrounding days:
  - if Feb 28 and Mar 1 are the same PL, Feb 29 gets that PL
  - if they differ, Feb 29 gets the average rounded UP (the more cautious level)
Filled days are marked source=leap_fill.

The sheet pre-fills the current year to Dec 31, so days after today are dropped.
Live readings (what SWCC actually posted that day) replace the sheet's value for that day.
"""
import calendar
import datetime as dt
import math
import os
import sys

import openpyxl
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(ROOT, "raw", "PL_Levels.xlsx")
STORE = os.environ.get("STORE_DIR", os.path.join(ROOT, "store"))
LIVE = os.path.join(STORE, "pl_live.csv")
OUT = os.path.join(ROOT, "docs", "data", "sw_pl.csv")


def read_sheet(today):
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    year_cols = {}
    for i, v in enumerate(rows[0]):
        try:
            y = int(v)
            if 1990 <= y <= 2100:
                year_cols[i] = y
        except (TypeError, ValueError):
            pass
    recs = {}
    for r in rows[1:]:
        md = r[0]
        if not isinstance(md, (dt.datetime, dt.date)):
            break
        for ci, year in year_cols.items():
            v = r[ci] if ci < len(r) else None
            if v is None:
                continue
            try:
                d = dt.date(year, md.month, md.day)
            except ValueError:
                continue
            if d <= today:
                recs[d] = (int(round(float(v))), "sheet")
    # leap-day fill
    for year in sorted(set(year_cols.values())):
        d29 = dt.date(year, 2, 29) if calendar.isleap(year) else None
        if not d29 or d29 in recs or d29 > today:
            continue
        a, b = recs.get(dt.date(year, 2, 28)), recs.get(dt.date(year, 3, 1))
        if a and b:
            val = a[0] if a[0] == b[0] else math.ceil((a[0] + b[0]) / 2)
            recs[d29] = (int(val), "leap_fill")
    return recs


def main(today=None):
    today = today or dt.date.today()
    recs = read_sheet(today)
    if os.path.exists(LIVE):
        live = pd.read_csv(LIVE, parse_dates=["date"])
        for d, pl in zip(live["date"].dt.date, live["pl"]):
            recs[d] = (int(pl), "live")   # an observed reading beats the sheet's pre-filled value
    df = pd.DataFrame([(d, p, s) for d, (p, s) in sorted(recs.items())], columns=["date", "pl", "source"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"PL: {len(df)} days {df.date.min()} to {df.date.max()}, "
          f"{(df.source == 'leap_fill').sum()} leap days filled, {(df.source == 'live').sum()} live days")


if __name__ == "__main__":
    sys.exit(main())
