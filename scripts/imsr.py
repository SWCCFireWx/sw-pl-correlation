"""
Southwest acreage growth from NICC's daily Incident Management Situation Reports (IMSR, PDF).

Each report's "Fires and Acres Year-to-Date" table has a Southwest row. The change in Southwest
year-to-date acres between two reports = acres burned in between (all fires, new and ongoing).
When reports are weekly (low PL), that week's acres are spread evenly over its days and marked.

Where the reports are:
  1990-2022: https://famprod.nwcg.gov/batchout/IMSRS_from_1990_to_2022/YYYY/YYYYMMDDIMSR.pdf
  2013-now : https://www.nifc.gov/sites/default/files/NICC/1-Incident%20Information/IMSR/YYYY/Month/...
             (file names changed over the years; several spellings are tried)
  today    : https://www.nifc.gov/nicc-files/sitreprt.pdf

Report layouts changed over the years, so the reader is deliberately tolerant and records what
it found per report in store/imsr/parsed.csv (with a text snippet) so problems can be checked.
Note: before the 2021 boundary change, the IMSR "Southwest" row included west Texas.
"""
import calendar
import datetime as dt
import io
import os
import re

import pandas as pd

from common import CFG, STORE, TODAY, http_bytes, log, minutes_used

FOLDER = os.path.join(STORE, "imsr")
PARSED = os.path.join(FOLDER, "parsed.csv")
NIFC = "https://www.nifc.gov/sites/default/files/NICC/1-Incident%20Information/IMSR/"
AREAS = ["Alaska", "Northwest", "Northern California", "Southern California", "Northern Rockies",
         "Great Basin", "Eastern Great Basin", "Western Great Basin", "Southwest", "Rocky Mountain",
         "Eastern", "Southern", "Total", "TOTAL"]


def candidate_urls(d):
    y, mon = d.year, calendar.month_name[d.month]
    out = []
    if y <= 2022:
        out.append(f"https://famprod.nwcg.gov/batchout/IMSRS_from_1990_to_2022/{y}/{d:%Y%m%d}IMSR.pdf")
    base = f"{NIFC}{y}/{mon}/"
    stamp = f"{d:%m%d%Y}"
    out += [f"{base}IMSR_CY{d:%y}_{stamp}.pdf", f"{base}IMSR_CY{d:%y}_{stamp}_0.pdf",
            f"{base}IMSR_CY{d:%y}_{stamp}_1.pdf", f"{base}{d:%Y%m%d}IMSR.pdf"]
    return out


def pdf_text(raw):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


NUM = r"-?\d{1,3}(?:,\d{3})+|-?\d+(?:\.\d+)?"


def _nums(s):
    return [float(x.replace(",", "")) for x in re.findall(NUM, s)]


def _area_slice(text, start):
    """Text from 'Southwest' (at/after start) up to the next area name."""
    m = re.search(r"\bSouthwest\b", text[start:])
    if not m:
        return None
    a = start + m.end()
    nxt = re.search(r"\b(" + "|".join(re.escape(x) for x in AREAS if x != "Southwest") + r")\b", text[a:])
    return text[a: a + (nxt.start() if nxt else 400)]


def _fires_acres(chunk):
    """Total fires and acres from one area row, whichever layout it uses."""
    if chunk is None:
        return None, None
    up = chunk.upper()
    if "FIRES" in up and "ACRES" in up:
        f_i, a_i = up.index("FIRES"), up.index("ACRES")
        if a_i > f_i:
            fires_part, acres_part = chunk[f_i + 5:a_i], chunk[a_i + 5:]
            nf, na = _nums(fires_part), _nums(acres_part)
            if nf and na and len(nf) == len(na):        # row-wise: FIRES n n n / ACRES n n n
                return nf[-1], na[-1]
            n = nf + na
            if not nf and na and len(na) % 2 == 0:       # labels first, then fires/acres pairs
                return na[-2], na[-1]
            if n and len(n) % 2 == 0:
                return n[-2], n[-1]
    n = _nums(chunk)
    if len(n) >= 2:
        return n[-2], n[-1]                              # last pair = total fires, total acres
    return None, None


def parse(text, d):
    out = {"date": d.isoformat(), "sw_pl": None, "sw_new_fires": None, "ytd_fires": None, "ytd_acres": None, "snippet": ""}
    # newer reports: summary table with a PL column ("GACC PL Incidents ..."; row "SWCC 1 4 17,222 ...")
    if re.search(r"GACC\s+PL\s+Incidents", text):
        t = re.search(r"\bSWCC\s+([1-5])\s+-?\d", text)
        if t:
            out["sw_pl"] = int(t.group(1))
    m = re.search(r"Southwest Area \(PL\s*([1-5])\)", text)
    if m:
        out["sw_pl"] = int(m.group(1))
        m2 = re.search(r"New fires:\s*(" + NUM + ")", text[m.end(): m.end() + 400])
        if m2:
            out["sw_new_fires"] = float(m2.group(1).replace(",", ""))
    ytd = None
    for pat in (r"Fires and Acres\s*(?:\(?\s*)Year[- ]to[- ]Date", r"Year[- ]to[- ]Date\s+(?:Fires|Statistics)", r"Year[- ]to[- ]Date"):
        m = re.search(pat, text, re.I)
        if m:
            ytd = m.end()
            break
    if ytd is not None:
        chunk = _area_slice(text, ytd)
        f, a = _fires_acres(chunk)
        out["ytd_fires"], out["ytd_acres"] = f, a
        out["snippet"] = re.sub(r"\s+", " ", (chunk or ""))[:160]
    return out


def _report_date(text):
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})", text)
    if m:
        return dt.datetime.strptime(" ".join(m.groups()), "%B %d %Y").date()
    return None


PARSER_VERSION = "2"


def update(progress, max_minutes=3.0, max_days=40):
    os.makedirs(FOLDER, exist_ok=True)
    parsed = pd.read_csv(PARSED, dtype={"date": str}) if os.path.exists(PARSED) else pd.DataFrame()
    if progress["notes"].get("imsr_parser") != PARSER_VERSION:
        # the reader improved: read the last 60 days of reports again
        cut = (TODAY - dt.timedelta(days=60)).isoformat()
        if len(parsed):
            parsed = parsed[parsed["date"] < cut]
            parsed.to_csv(PARSED, index=False)
        for k in [k for k in progress["done"] if k.startswith("imsr/") and k[5:] >= cut]:
            del progress["done"][k]
        progress["notes"]["imsr_parser"] = PARSER_VERSION
        log("IMSR: re-reading the last 60 days of reports with the updated reader")
    have = set(parsed["date"]) if len(parsed) else set()
    new = []
    # 1) today's current report
    try:
        raw = http_bytes("https://www.nifc.gov/nicc-files/sitreprt.pdf", timeout=90)
        text = pdf_text(raw)
        d = _report_date(text)
        if d and d.isoformat() not in have:
            new.append(parse(text, d))
    except Exception as e:  # noqa: BLE001
        log(f"IMSR current report failed: {str(e)[:120]}")
    # 2) backfill, newest first
    start = start_t = minutes_used()
    d = TODAY - dt.timedelta(days=1)
    first = dt.date(CFG["start_year"], 1, 1)
    tried = 0
    while d >= first and tried < max_days and minutes_used() - start_t < max_minutes:
        key = f"imsr/{d.isoformat()}"
        if d.isoformat() in have or progress["done"].get(key):
            d -= dt.timedelta(days=1)
            continue
        tried += 1
        got, transient = None, False
        for url in candidate_urls(d):
            try:
                got = http_bytes(url, timeout=60)
                if got[:4] == b"%PDF":
                    break
                got = None
            except Exception as e:  # noqa: BLE001
                if not ("404" in str(e) or "Not Found" in str(e) or "403" in str(e)):
                    transient = True
        if got:
            try:
                new.append(parse(pdf_text(got), d))
                progress["done"][key] = "ok"
            except Exception as e:  # noqa: BLE001
                log(f"IMSR {d} unreadable: {str(e)[:100]}")
                progress["done"][key] = "unreadable"
        elif not transient:
            progress["done"][key] = "none"   # no report that day (weekly reporting at low PL)
        d -= dt.timedelta(days=1)
    if new:
        parsed = pd.concat([parsed, pd.DataFrame(new)], ignore_index=True).drop_duplicates("date", keep="last")
        parsed.sort_values("date").to_csv(PARSED, index=False)
        ok = sum(1 for r in new if r["ytd_acres"] is not None)
        log(f"IMSR: {len(new)} reports read ({ok} with SW year-to-date acres); {minutes_used() - start:.1f} min")


def daily():
    """date -> sw_acres_growth (acres burned that day, from YTD differences), plus flags."""
    if not os.path.exists(PARSED):
        return None
    p = pd.read_csv(PARSED, parse_dates=["date"]).dropna(subset=["ytd_acres"]).sort_values("date")
    if p.empty:
        return None
    rows = {}
    for year, g in p.groupby(p["date"].dt.year):
        prev_date, prev_acres = pd.Timestamp(f"{year}-01-01") - pd.Timedelta(days=1), 0.0
        for d, a in zip(g["date"], g["ytd_acres"]):
            if a < prev_acres * 0.8:       # likely a misread; skip it
                continue
            days = (d - prev_date).days
            if days <= 0:
                continue
            per = max(a - prev_acres, 0) / days
            for k in range(days):
                day = prev_date + pd.Timedelta(days=k + 1)
                rows[day] = (per, days > 1)
            prev_date, prev_acres = d, a
    s = pd.DataFrame.from_dict(rows, orient="index", columns=["imsr_sw_acres_growth", "imsr_spread"])
    s.index.name = "date"
    pl = p.set_index("date")["sw_pl"]
    s["imsr_sw_pl_check"] = pl.reindex(s.index)
    return s.sort_index()
