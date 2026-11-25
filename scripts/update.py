"""
Runs every 15 minutes from GitHub Actions. Each step logs and carries on if it fails; anything
missed is retried next run.

  1. Breakpoints workbook (raw/breakpoints.xlsx) -> PSA key stations + ERC/BI percentile tables
  2. Stations: every RAWS in a current SW PSA (AZ/NM, minus excluded areas) + the workbook's key stations
  3. FEMS: recent days for all stations, then backfill of history (observations only)
  4. Fire starts (WFIGS/IRWIN), lightning (NOAA NLDN tiles), acreage growth (NICC IMSR reports)
  5. Today's Southwest PL from SWCC
  6. Builds Southwest-wide and per-PSA daily tables, map files, the 7-day PL forecast (SW + each PSA)
     from observed data and the archive, and the analysis
"""
import datetime as dt
import json
import os
import re
import sys
import traceback

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import dump_json, CFG, DATA, LOG, STORE, TODAY, http, load_progress, log, minutes_used, save_progress  # noqa: E402
import analyze  # noqa: E402
import bestfit  # noqa: E402
import breakpoints  # noqa: E402
import fems  # noqa: E402
import fires  # noqa: E402
import forecast  # noqa: E402
import import_pl  # noqa: E402
import imsr  # noqa: E402
import lightning  # noqa: E402
import publish  # noqa: E402
import stations as stn  # noqa: E402


def step(name, fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log(f"{name} failed:\n{traceback.format_exc()[-700:]}")
        return default


# ---------------------------------------------------------------- Southwest PL (live)
def scrape_sw_pl():
    for url in CFG["pl_live_sources"]:
        try:
            html = http(url, timeout=60)
        except Exception as e:  # noqa: BLE001
            log(f"PL page {url} failed: {e}")
            continue
        text = re.sub(r"&nbsp;|\s+", " ", re.sub(r"<[^>]+>", " ", html))
        m = re.search(r"Southwest\s+([1-5])\s+(\d{1,2}/\d{1,2}/\d{2,4})", text)
        if m:
            since = None
            for fmt in ("%m/%d/%y", "%m/%d/%Y"):
                try:
                    since = dt.datetime.strptime(m.group(2), fmt).date()
                    break
                except ValueError:
                    pass
            return int(m.group(1)), since, url
        m = re.search(r"Southwest\s*(?:Area)?\s*(?:\(PL\s*)?([1-5])\b", text)
        if m:
            return int(m.group(1)), None, url
    return None, None, None


def update_pl():
    path = os.path.join(STORE, "pl_live.csv")
    live = pd.read_csv(path, parse_dates=["date"]) if os.path.exists(path) else pd.DataFrame(columns=["date", "pl"])
    level, since, src = scrape_sw_pl()
    if level is None:
        log("Southwest PL: couldn't read today's level; will retry")
        return
    days = [pd.Timestamp(TODAY)]
    if since and since <= TODAY and not live.empty:
        days = list(pd.date_range(max(pd.Timestamp(since), live["date"].max() + pd.Timedelta(days=1)), pd.Timestamp(TODAY)))
    live = pd.concat([live[~live["date"].isin(days)], pd.DataFrame({"date": days, "pl": level})]).sort_values("date")
    live.to_csv(path, index=False, date_format="%Y-%m-%d")
    log(f"Southwest PL {level}" + (f" (since {since})" if since else "") + f" from {src}")


# ---------------------------------------------------------------- build
def build(stations, psa_gj, areas, excl, bp):
    ids_all = [s["station_id"] for s in stations]
    area_ids = [s["station_id"] for s in stations if s.get("in_area")]
    key_ids = [s["station_id"] for s in stations if s.get("key")]
    long = fems.load_long(ids_all)

    parts = []
    if long is not None and len(long):
        fd = fems.area_daily(long, area_ids)
        fd.to_csv(os.path.join(DATA, "fems_daily.csv"), float_format="%.2f")
        parts.append(fd)

    # official PSA ERCy / BI (workbook method)
    psa_off, psa_fc = pd.DataFrame(), None
    if bp and long is not None and len(long):
        erc_col = fems.find_col(long.columns, "erc", "energy_release_component")
        bi_col = fems.find_col(long.columns, "bi", "burning_index")
        if erc_col and bi_col:
            psa_off = breakpoints.psa_daily(long[long["station"].isin(key_ids)], bp, erc_col, bi_col)
            if len(psa_off):
                psa_off.to_csv(os.path.join(DATA, "psa_official.csv"), index=False, float_format="%.2f")
                parts.append(breakpoints.sw_summary(psa_off))
        else:
            log(f"PSA averages: couldn't find ERC/BI columns in FEMS data ({[c for c in long.columns][:10]})")

    fire_df = fires.load(areas, excl)
    if fire_df is not None:
        fsw = fires.daily(fire_df)
        fsw.to_csv(os.path.join(DATA, "fires_daily.csv"))
        parts.append(fsw)

    ltg_sw, ltg_long, ltg_cov = lightning.daily()
    if ltg_sw is not None:
        parts.append(ltg_sw.rename("lightning_strikes").to_frame())

    im = imsr.daily()
    if im is not None:
        parts.append(im[["imsr_sw_acres_growth"]])
        im.to_csv(os.path.join(DATA, "imsr_daily.csv"), float_format="%.1f")

    idx = pd.date_range(f"{CFG['start_year']}-01-01", pd.Timestamp(TODAY))
    sw = pd.concat([p.reindex(idx) for p in parts], axis=1) if parts else pd.DataFrame(index=idx)
    sw.index.name = "date"
    sw.to_csv(os.path.join(DATA, "sw_inputs.csv"), float_format="%.3f")

    # per-PSA inputs
    os.makedirs(os.path.join(DATA, "psa"), exist_ok=True)
    os.makedirs(os.path.join(STORE, "psa_inputs"), exist_ok=True)
    psa_tables = {}
    codes = sorted({s.get("psa") for s in stations if s.get("psa")} | set(psa_off["psa"].unique() if len(psa_off) else []))
    for code in codes:
        pp = []
        ids = [s["station_id"] for s in stations if s.get("psa") == code and s.get("in_area")]
        if long is not None and ids:
            pp.append(fems.area_daily(long, ids))
        if len(psa_off):
            o = psa_off[psa_off["psa"] == code].set_index("date")[["erc_avg", "bi_avg", "erc_pct", "bi_pct"]]
            pp.append(o.add_prefix("psa_"))
        if fire_df is not None:
            pp.append(fires.daily(fire_df, code))
        if ltg_long is not None:
            lt = ltg_long[ltg_long["psa"] == code].set_index("date")["strikes"].reindex(idx).fillna(0)
            lt[~ltg_cov.reindex(idx).fillna(False)] = float("nan")
            pp.append(lt.rename("lightning_strikes").to_frame())
        if pp:
            t = pd.concat([p.reindex(idx) for p in pp], axis=1)
            t.index.name = "date"
            t.to_csv(os.path.join(STORE, "psa_inputs", f"{code}_inputs.csv"), float_format="%.3f")
            psa_tables[code] = t

    step("map files", lambda: publish.write_all(stations, psa_gj, long, psa_off, psa_fc, fire_df, ltg_long, ltg_cov,
                                                {c: analyze.friendly(c) for c in (long.columns if long is not None else [])}))

    # forecast
    pl = pd.read_csv(os.path.join(DATA, "sw_pl.csv"), parse_dates=["date"]).set_index("date")["pl"]
    step("forecast", lambda: forecast.run(sw, psa_tables, pl))
    return codes


def main():
    progress = load_progress()
    bp = step("breakpoints", breakpoints.load)
    if bp:
        dump_json({k: v for k, v in bp.items() if k != "modified"}, os.path.join(DATA, "breakpoints.json"), indent=0)
        log(f"Breakpoints: {len(bp['stations'])} key stations, {len(bp['erc'])} PSAs ({bp['erc_label']})")
    res = step("stations", lambda: stn.get(progress, bp), ([], None, []))
    stations, areas, excl = res
    psa_gj = json.load(open(os.path.join(STORE, "sw_psas.geojson"))) if os.path.exists(os.path.join(STORE, "sw_psas.geojson")) else None
    save_progress(progress)

    step("FEMS", lambda: fems.update(progress, stations)); save_progress(progress)  # noqa: E702
    step("fires", lambda: fires.update(progress)); save_progress(progress)  # noqa: E702
    step("lightning", lambda: lightning.update(progress, areas, excl, CFG["lightning"]["max_files_per_run"])); save_progress(progress)  # noqa: E702
    step("IMSR", lambda: imsr.update(progress, CFG["imsr"]["max_minutes_per_run"], CFG["imsr"]["max_days_per_run"])); save_progress(progress)  # noqa: E702
    step("PL", update_pl)
    import_pl.main()

    codes = step("build", lambda: build(stations, psa_gj, areas, excl, bp), [])

    ids = [s["station_id"] for s in stations]
    need = len(ids) * (TODAY.year - CFG["start_year"] + 1) * 2
    have = sum(1 for k in progress["done"] if k.count("/") == 2 and k.split("/")[1] in set(ids))
    hist_path = os.path.join(STORE, "run_log.json")
    hist = (json.load(open(hist_path)) if os.path.exists(hist_path) else []) + LOG
    json.dump(hist[-400:], open(hist_path, "w"), indent=0)
    imsr_days = sum(1 for k in progress["done"] if k.startswith("imsr/"))
    total_days = (TODAY - dt.date(CFG["start_year"], 1, 1)).days
    dump_json({
        "updated_utc": dt.datetime.utcnow().isoformat(timespec="minutes") + "Z",
        "stations": [{k: s.get(k) for k in ("station_id", "station_name", "state", "psa", "key", "in_area", "latitude", "longitude")} for s in stations],
        "fems_backfill_pct": round(100 * have / need, 1) if need else 0,
        "imsr_backfill_pct": round(100 * imsr_days / total_days, 1) if total_days else 0,
        "lightning_files": sorted(k[4:] for k in progress["done"] if k.startswith("ltg/")),
        "fire_years_done": sorted(k for k in progress["done"] if k.startswith("fires/")),
        "psas": codes or [],
        "run_minutes": round(minutes_used(), 1),
        "log": hist[-80:],
    }, os.path.join(DATA, "status.json"), indent=0)
    analyze.main(codes or [], progress)
    save_progress(progress)
    step("best fit", lambda: bestfit.run(codes or [], progress, analyze))
    save_progress(progress)


if __name__ == "__main__":
    sys.exit(main())
