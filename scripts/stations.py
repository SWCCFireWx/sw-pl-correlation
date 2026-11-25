"""
Which stations to use, and the boundaries used to decide.

  * Southwest PSA polygons: national PSA boundary layer, GACCUnitID = USNMSWC (refreshed weekly)
  * Excluded areas (config 'exclude_areas'; default Grand Canyon NP, which is Great Basin GACC)
  * Area stations: every RAWS in AZ/NM (FEMS station metadata) inside a SW PSA and outside excluded areas
  * Key stations: the PSA stations listed in the breakpoints workbook. These are always fetched
    (they drive the official PSA ERCy/BI averages), even if outside AZ/NM, but only area
    stations go into the Southwest-wide medians.
"""
import json
import os
import urllib.parse

import pandas as pd

from common import CFG, STORE, TODAY, Areas, http, is_fresh, log


def _geojson(url, where, path, progress, key, fields="*"):
    if not (os.path.exists(path) and is_fresh(progress, key)):
        q = {"where": where, "outFields": fields, "returnGeometry": "true", "outSR": "4326", "f": "geojson"}
        try:
            gj = json.loads(http(url + "?" + urllib.parse.urlencode(q), timeout=120))
            if gj.get("features"):
                json.dump(gj, open(path, "w"))
                progress["notes"][key] = TODAY.isoformat()
            else:
                log(f"{key}: boundary query returned no features")
        except Exception as e:  # noqa: BLE001
            log(f"{key}: boundary download failed ({str(e)[:150]}); using cached copy if any")
    return json.load(open(path)) if os.path.exists(path) else None


def psa_areas(progress):
    a = CFG["fems"]["auto"]
    gj = _geojson(a["psa_service_url"], f"GACCUnitID = '{a['gacc_unit_id']}'", os.path.join(STORE, "sw_psas.geojson"),
                  progress, "psas_refreshed", "PSANationalCode,PSANAME")
    return gj, Areas(gj, lambda p: p.get("PSANationalCode"))


def exclusions(progress):
    out = []
    for i, area in enumerate(CFG.get("exclude_areas", [])):
        gj = _geojson(area["url"], area["where"], os.path.join(STORE, f"exclude_{i}.geojson"), progress, f"exclude_{i}_refreshed")
        if gj:
            out.append(Areas(gj, lambda p, n=area["name"]: n))
        elif area.get("fallback_bbox"):
            x0, y0, x1, y1 = area["fallback_bbox"]
            log(f"Exclusion '{area['name']}': using the fallback box")
            box = {"features": [{"properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}}]}
            out.append(Areas(box, lambda p, n=area["name"]: n))
    return out


def excluded(lon, lat, excl):
    for ex in excl:
        hit = ex.find(lon, lat)
        if hit:
            return hit
    return None


FIELDS = ("station_id station_name state county_name latitude longitude elevation period_record_start "
          "period_record_stop station_status network_name agency unit")


def _metadata(states):
    a = CFG["fems"]["auto"]
    found = []
    for st in states:
        page = 0
        while True:
            q = ('query { stationMetaData(stateId: "%s", networkName: "%s", per_page: 200, page: %d) '
                 '{ _metadata { page page_count total_count } data { %s } } }' % (st, a["network"], page, FIELDS))
            js = json.loads(http(CFG["fems"]["graphql_url"], data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json"}, timeout=90))
            if js.get("errors"):
                raise RuntimeError(js["errors"])
            block = js["data"]["stationMetaData"]
            found += block["data"] or []
            page += 1
            if page >= (block["_metadata"].get("page_count") or 1):
                break
    return found


def get(progress, bp):
    """Returns list of station dicts with keys: station_id, station_name, state, latitude, longitude,
    psa, in_area (bool: counts toward SW-wide values), key (bool: workbook PSA station)."""
    path = os.path.join(STORE, "stations.json")
    _, areas = psa_areas(progress)
    excl = exclusions(progress)
    key_rows = {s["station_id"]: s for s in (bp or {}).get("stations", [])}

    if CFG["fems"]["station_mode"] == "auto" and not (os.path.exists(path) and is_fresh(progress, "stations_refreshed")):
        try:
            a = CFG["fems"]["auto"]
            meta = _metadata(a["states"])
            start = f"{CFG['start_year']}-01-01"
            excl_ids = set(map(str, a["exclude_station_ids"]))
            keep, outside, dropped = [], 0, []
            for s in meta:
                sid = str(s.get("station_id") or "").strip()
                if not sid or sid in excl_ids or s.get("state") not in a["states"]:
                    continue
                if (s.get("period_record_stop") or "9999")[:10] < start:
                    continue
                try:
                    lon, lat = float(s["longitude"]), float(s["latitude"])
                except (TypeError, ValueError, KeyError):
                    continue
                ex = excluded(lon, lat, excl)
                if ex:
                    dropped.append(f"{s.get('station_name')} ({sid})")
                    continue
                code = areas.find(lon, lat) if areas else None
                if areas and not code:
                    outside += 1
                    continue
                s.update(station_id=sid, psa=code, in_area=True)
                keep.append(s)
            for extra in a["include_station_ids"]:
                if str(extra) not in {s["station_id"] for s in keep}:
                    keep.append({"station_id": str(extra), "station_name": "(added in config)", "in_area": True})
            if keep:
                json.dump(keep, open(path, "w"), indent=0)
                progress["notes"]["stations_refreshed"] = TODAY.isoformat()
                by = pd.Series([s.get("psa") or "?" for s in keep]).value_counts().sort_index().to_dict()
                log(f"Stations: {len(keep)} RAWS in SW PSAs {by}; {outside} AZ/NM stations outside the PSAs")
                if dropped:
                    log(f"Stations left out (excluded areas): {', '.join(dropped)}")
        except Exception as e:  # noqa: BLE001
            log(f"Station discovery failed ({str(e)[:200]}); using previous list")

    if CFG["fems"]["station_mode"] != "auto":
        st = [{"station_id": str(s), "in_area": True} for s in CFG["fems"]["manual_station_ids"]]
    elif os.path.exists(path):
        st = json.load(open(path))
    else:
        st = []
    by_id = {s["station_id"]: s for s in st}
    for sid, k in key_rows.items():
        if sid in by_id:
            by_id[sid]["key"] = True
            by_id[sid]["key_psa"] = k["psa"]
            by_id[sid].setdefault("psa", k["psa"])
        else:  # in the workbook but not an area station (e.g. outside AZ/NM): fetch for the PSA average only
            by_id[sid] = {"station_id": sid, "station_name": k["station_name"].upper(), "psa": k["psa"],
                          "key_psa": k["psa"], "key": True, "in_area": False}
    missing = [s for s in by_id.values() if s.get("latitude") is None]
    cache = os.path.join(STORE, "extra_station_meta.json")
    extra = json.load(open(cache)) if os.path.exists(cache) else {}
    need = [s["station_id"] for s in missing if s["station_id"] not in extra]
    if need:
        try:
            q = ('query { stationMetaData(stationIds: "%s", per_page: 200) { data { %s } } }' % (",".join(need), FIELDS))
            js = json.loads(http(CFG["fems"]["graphql_url"], data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json"}, timeout=90))
            for m in (js.get("data") or {}).get("stationMetaData", {}).get("data") or []:
                extra[str(m["station_id"])] = m
            json.dump(extra, open(cache, "w"))
        except Exception as e:  # noqa: BLE001
            log(f"Key-station metadata lookup failed: {str(e)[:120]}")
    for s in missing:
        m = extra.get(s["station_id"])
        if m:
            for k in ("latitude", "longitude", "state", "elevation", "station_name"):
                if m.get(k) is not None and (k != "station_name" or not s.get(k)):
                    s[k] = m[k]
    return list(by_id.values()), areas, excl
