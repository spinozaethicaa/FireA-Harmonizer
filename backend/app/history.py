from __future__ import annotations

import calendar
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import L
from .firms import FirmsClient, FirmsError, availability_index, bbox_str
from .matching import MatchingCancelled
from .normalize import merge_observations, normalize_records

MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
FIRST_YEAR = 2003
VIIRS_START_YEAR = 2012
VIIRS_SOURCES = [("VIIRS_SNPP", 2012, "Suomi NPP"), ("VIIRS_NOAA20", 2018, "NOAA-20"), ("VIIRS_NOAA21", 2023, "NOAA-21")]
SAT_LABEL = {"Suomi NPP": "S-NPP", "NOAA-20": "NOAA-20", "NOAA-21": "NOAA-21"}
MIN_COVER = 0.9
MIN_SPREAD = 5.0
CELL_MIN_SPREAD = 3.0
CAL_PRIOR = 100.0
DEFAULT_SPREAD = (0.108, 1.32)
SPREAD_FLOOR = 0.0225
Z80 = 1.2816
CONST_RATIO = 0.149
DEFAULT_FRP_UNIT = 20.0
DRIFT_MAX_MIN = 15.0
SEASON_RIDGE = 1.0
RECENT_DAYS = 7
GRID_MAX = (24, 16)
CACHE_TTL_S = 6 * 3600
COVERED = {"ok", "empty"}
METRICS = {"count": "", "frp": "_frp"}
SCALE_METRIC = {"": True, "_frp": False}
SIZE_EDGES = [1.5, 2.5, 5.5, 20.5]
N_GRP = 2 * (len(SIZE_EDGES) + 1)
SIZE_TAU = 20.0
SIZE_MIN_LABELS = 30
MATCH_WIN_MIN = 90.0
PASS_GAP_MIN = 10.0
EARTH_KM = 6371.0088
MAX_MODEL_CANDIDATES = 1
XCAL_SAT = "NOAA-21"
XCAL_MIN_MONTHS = 6

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _segments(prefix: str, start: date, end: date, avail: dict) -> list[tuple[str, date, date]]:
    segs = []
    sp, nrt = avail.get(f"{prefix}_SP"), avail.get(f"{prefix}_NRT")
    covered_to = start - timedelta(days=1)
    if sp:
        s = max(start, date.fromisoformat(sp["min_date"]))
        e = min(end, date.fromisoformat(sp["max_date"]))
        if s <= e:
            segs.append((f"{prefix}_SP", s, e))
            covered_to = e
    if nrt and covered_to < end:
        s = max(covered_to + timedelta(days=1), date.fromisoformat(nrt["min_date"]))
        e = min(end, date.fromisoformat(nrt["max_date"]))
        if s <= e:
            segs.append((f"{prefix}_NRT", s, e))
    return segs


def _static(obs: pd.DataFrame) -> pd.Series:
    return obs["raw"].map(lambda r: str(r.get("type", "") or "0").strip()).isin(["1", "2", "3"])


def _robust(base: np.ndarray, value: float, floor: float = MIN_SPREAD) -> tuple[float | None, float | None, float | None]:
    if len(base) < 3:
        return None, None, None
    med = float(np.median(base))
    spread = 1.4826 * float(np.median(np.abs(base - med)))
    if spread <= 0:
        spread = float(base.std(ddof=1)) if len(base) > 1 else 0.0
    spread = max(spread, floor)
    return med, spread, (value - med) / spread


def _fit_spread(h: np.ndarray, a: np.ndarray) -> tuple[float, float]:
    from scipy.optimize import minimize

    res = np.log((a + 1) / (h + 1))

    def nll(q):
        s2 = np.exp(q[0]) + np.exp(q[1]) / (h + 1)
        return 0.5 * float(np.sum(np.log(s2) + res ** 2 / s2))

    o = minimize(nll, [np.log(0.05), np.log(0.5)], method="Nelder-Mead")
    s0, s1 = max(float(np.exp(o.x[0])), SPREAD_FLOOR), float(np.exp(o.x[1]))
    z = np.abs(res) / np.sqrt(s0 + s1 / (h + 1))
    c2 = (float(np.quantile(z, 0.8)) / Z80) ** 2 if len(z) >= 20 else 1.0
    return s0 * c2, s1 * c2


def _interval(h: np.ndarray, spread: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    sd = np.sqrt(spread[0] + spread[1] / (h + 1))
    return np.maximum(0, (h + 1) * np.exp(-Z80 * sd) - 1), (h + 1) * np.exp(Z80 * sd) - 1


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _prob_beyond(value: float, thr_hi: float, thr_lo: float, spread, unit: float) -> tuple[float, float]:
    u = value / unit
    sd = math.sqrt(spread[0] + spread[1] / (u + 1))
    p_hi = 1 - _phi((math.log(thr_hi / unit + 1) - math.log(u + 1)) / sd)
    p_lo = _phi((math.log(thr_lo / unit + 1) - math.log(u + 1)) / sd) if thr_lo > 0 else 0.0
    return p_hi, p_lo


def _season_x(months) -> np.ndarray:
    m = np.asarray(months, float)
    return np.column_stack([np.ones(len(m)), np.cos(2 * np.pi * m / 12), np.sin(2 * np.pi * m / 12)])


def _fit_season(mo: np.ndarray, a: np.ndarray, h: np.ndarray) -> np.ndarray:
    from scipy.optimize import minimize

    X = _season_x(mo)

    def nll(beta):
        mu = h * np.exp(X @ beta) + 1e-6
        return float(np.sum(mu - a * np.log(mu))) + SEASON_RIDGE * float(np.sum(beta[1:] ** 2))

    return minimize(nll, np.zeros(3), method="BFGS").x


def _seam_beta(a: np.ndarray, h: np.ndarray) -> np.ndarray:
    return np.array([np.log((a.sum() + CAL_PRIOR) / (h.sum() + CAL_PRIOR)), 0.0, 0.0])


def _factor(beta: np.ndarray, months) -> np.ndarray:
    return np.exp(_season_x(months) @ beta)


def _enough(pairs: list[dict]) -> bool:
    return len(pairs) >= 36 and len({r["year"] for r in pairs}) >= 4 and sum(r["aqua"] for r in pairs) >= 100


def _validate(pairs: list[dict], sfx: str, unit: float) -> dict:
    yr = np.array([r["year"] for r in pairs])
    mo = np.array([r["month"] for r in pairs])
    a = np.array([r["aqua" + sfx] for r in pairs], float) / unit
    # For counts the size correction is refitted without the held-out year ("_cv").
    h = np.array([r["harm_raw_frp"] if sfx else r["_cv"] for r in pairs], float) / unit
    raw = np.array([r["harm_raw_frp"] if sfx else r["harm_fire"] for r in pairs], float) / unit
    v = np.array([np.nan if r["viirs" + sfx] is None else r["viirs" + sfx] for r in pairs], float) / unit
    big = np.array([r["aqua"] for r in pairs], float) >= 20
    multi = np.array([len(r.get("harm_sats") or []) >= 2 for r in pairs])
    pred, lo, hi, const, season = (np.zeros_like(a) for _ in range(5))
    for y0 in sorted(set(yr)):
        tr, te = yr != y0, yr == y0
        beta = _fit_season(mo[tr], a[tr], h[tr]) if SCALE_METRIC[sfx] else np.zeros(3)
        sp = _fit_spread(h[tr] * _factor(beta, mo[tr]), a[tr])
        pred[te] = h[te] * _factor(beta, mo[te])
        lo[te], hi[te] = _interval(pred[te], sp)
        if sfx:
            ok = tr & np.isfinite(v)
            const[te] = v[te] * (a[ok].sum() / max(v[ok].sum(), 1e-9))
        else:
            season[te] = raw[te] * _factor(_fit_season(mo[tr], a[tr], raw[tr]), mo[te])
    if not sfx:
        const = v * CONST_RATIO

    def mape(p, sel=None):
        m = big & np.isfinite(p) if sel is None else big & np.isfinite(p) & sel
        return round(float(np.median(np.abs(p[m] - a[m]) / a[m]) * 100), 1) if m.any() else None

    def year_err(p, sel=None, how=np.median):
        errs = []
        for y0 in sorted(set(yr)):
            m = (yr == y0) & np.isfinite(p) if sel is None else (yr == y0) & np.isfinite(p) & sel
            if m.sum() >= 10 and big[m].any():
                errs.append(abs(p[m].sum() - a[m].sum()) / a[m].sum())
        return round(float(how(errs)) * 100, 1) if errs else None

    def mae(p, sel=None):
        m = np.isfinite(p) if sel is None else np.isfinite(p) & sel
        return round(float(np.mean(np.abs(p[m] - a[m])) * unit), 2) if m.any() else None

    out = {
        "n_months": int(len(a)), "n_years": int(len(set(yr))), "n_active_months": int(big.sum()),
        "years": [int(min(yr)), int(max(yr))], "method": "leave-one-year-out",
        "const_kind": "local_ratio" if sfx else "global_ratio",
        "harm_month_err_pct": mape(pred), "raw_month_err_pct": mape(raw), "const_month_err_pct": mape(const),
        "harm_year_err_pct": year_err(pred), "raw_year_err_pct": year_err(raw), "const_year_err_pct": year_err(const),
        "harm_year_err_mean_pct": year_err(pred, how=np.mean),
        "harm_mae": mae(pred), "raw_mae": mae(raw), "const_mae": mae(const),
        "coverage80_raw": None,
        "coverage80_calibrated": round(float(np.mean((a >= np.floor(lo)) & (a <= np.ceil(hi)))), 3),
        "fire_a_bias_pct": round(float((raw.sum() / a.sum() - 1) * 100), 1) if a.sum() > 0 else None,
        "multi_sat": None,
    }
    if not sfx:
        raw_lo = np.array([r["harm_raw_lo"] for r in pairs], float)
        raw_hi = np.array([r["harm_raw_hi"] for r in pairs], float)
        out["coverage80_raw"] = round(float(np.mean((a >= raw_lo) & (a <= raw_hi))), 3)
        out.update(season_month_err_pct=mape(season), season_year_err_pct=year_err(season),
                   season_year_err_mean_pct=year_err(season, how=np.mean), season_mae=mae(season))
    if multi.sum() >= 24:
        my = sorted(set(yr[multi]))
        out["multi_sat"] = {"n_months": int(multi.sum()), "years": [int(my[0]), int(my[-1])],
                            "n_active_months": int((big & multi).sum()),
                            "harm_month_err_pct": mape(pred, multi), "harm_year_err_pct": year_err(pred, multi),
                            "harm_year_err_mean_pct": year_err(pred, multi, how=np.mean), "harm_mae": mae(pred, multi)}
    return out


def _groups(n, daytime) -> np.ndarray:
    return np.digitize(np.asarray(n, float), SIZE_EDGES) * 2 + np.asarray(daytime, bool).astype(int)


def _match_aqua(arr: dict, aq_t, aq_lat, aq_lon, aq_scan, aq_track) -> np.ndarray:
    from sklearn.neighbors import BallTree

    y = np.zeros(len(arr["mu"]))
    if len(aq_t) == 0 or len(arr.get("pix_t", [])) == 0:
        return y
    order = np.argsort(arr["pix_t"], kind="stable")
    pt = arr["pix_t"][order]
    pxy = np.radians(np.column_stack([arr["pix_lat"], arr["pix_lon"]]))[order]
    pcl = arr["pix_cl"][order]
    ao = np.argsort(aq_t, kind="stable")
    cuts = np.flatnonzero(np.diff(aq_t[ao]) > PASS_GAP_MIN) + 1
    r_assign = np.maximum(1.5, 0.5 * np.nan_to_num(np.fmax(aq_scan, aq_track), nan=1.0) + 0.75)
    for seg in np.split(ao, cuts):
        a = np.searchsorted(pt, aq_t[seg].min() - MATCH_WIN_MIN)
        b = np.searchsorted(pt, aq_t[seg].max() + MATCH_WIN_MIN, side="right")
        if b <= a:
            continue
        d, j = BallTree(pxy[a:b], metric="haversine").query(np.radians(np.column_stack([aq_lat[seg], aq_lon[seg]])), k=1)
        hit = d[:, 0] * EARTH_KM <= r_assign[seg]
        np.add.at(y, pcl[a:b][j[hit, 0]], 1.0)
    return y


def _loyo_mae(a: np.ndarray, h: np.ndarray, mo: np.ndarray, yr: np.ndarray) -> float:
    pred = np.zeros_like(a)
    for y0 in sorted(set(yr)):
        tr, te = yr != y0, yr == y0
        pred[te] = h[te] * _factor(_fit_season(mo[tr], a[tr], h[tr]), mo[te])
    return float(np.mean(np.abs(pred - a)))


def _size_factors(lab: dict, years) -> tuple[np.ndarray, float]:
    Y, MU = np.zeros(N_GRP), np.zeros(N_GRP)
    for y in years:
        if y in lab:
            Y += lab[y][0]
            MU += lab[y][1]
    if Y.sum() < SIZE_MIN_LABELS or MU.sum() <= 0:
        return np.ones(N_GRP), 0.0
    k = Y.sum() / MU.sum()
    return (Y + SIZE_TAU * k) / (MU + SIZE_TAU), float(Y.sum())


def _cell_month_group_sum(spec: dict, lat, lon, months, values, grp) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    if len(values) == 0:
        return out
    n_cells = spec["nx"] * spec["ny"]
    idx = np.asarray(grp, int) * n_cells + _cell(spec, np.asarray(lat, float), np.asarray(lon, float))
    months = np.asarray(months)
    values = np.asarray(values, float)
    for m in np.unique(months):
        sel = months == m
        out[int(m)] = np.bincount(idx[sel], weights=values[sel], minlength=N_GRP * n_cells).reshape(N_GRP, n_cells)
    return out


def _calibrate(monthly: list[dict], drifted: set[int], sfx: str, unit: float) -> dict:
    pairs = [r for r in monthly if not r["partial"] and r["aqua" + sfx] is not None and r["harm_raw" + sfx] is not None
             and r["year"] not in drifted]
    a = np.array([r["aqua" + sfx] for r in pairs], float) / unit
    h = np.array([r["harm_raw" + sfx] for r in pairs], float) / unit
    mo = np.array([r["month"] for r in pairs])
    local = _enough(pairs)
    if local:
        beta = _fit_season(mo, a, h) if SCALE_METRIC[sfx] else np.zeros(3)
        spread = _fit_spread(h * _factor(beta, mo), a)
    else:
        beta = _seam_beta(a, h) if len(pairs) and SCALE_METRIC[sfx] else np.zeros(3)
        spread = DEFAULT_SPREAD
    nd = 0 if sfx else 1
    for r in monthly:
        if r["harm_raw" + sfx] is None:
            r["harm" + sfx] = r["harm" + sfx + "_lo"] = r["harm" + sfx + "_hi"] = None
            continue
        hk = r["harm_raw" + sfx] * float(_factor(beta, [r["month"]])[0])
        lo, hi = _interval(np.array([hk / unit]), spread)
        r["harm" + sfx] = round(hk, nd)
        r["harm" + sfx + "_lo"] = round(float(np.floor(lo[0] * unit)), nd)
        r["harm" + sfx + "_hi"] = round(float(np.ceil(hi[0] * unit)), nd)
    return {"beta": beta, "spread": spread, "unit": unit, "local": local, "n_pairs": len(pairs), "scaled": SCALE_METRIC[sfx],
            "validation": _validate(pairs, sfx, unit) if local else None}


def _overpass_offset(o: pd.DataFrame) -> float | None:
    day = o[o["daynight"] == "D"]
    lst = day["observed_at_utc"].dt.hour + day["observed_at_utc"].dt.minute / 60 + day["longitude"] / 15
    aq, sn = lst[day["satellite"] == "Aqua"], lst[day["satellite"] == "Suomi NPP"]
    if len(aq) < 10 or len(sn) < 10:
        return None
    return round(float(np.median(aq) - np.median(sn)) * 60, 1)


def _grid_spec(bbox) -> dict:
    w, s, e, n = bbox
    size = max(0.05, math.ceil(max((e - w) / GRID_MAX[0], (n - s) / GRID_MAX[1]) / 0.05 - 1e-9) * 0.05)
    return {"west": w, "south": s, "size": round(size, 2), "nx": max(1, math.ceil((e - w) / size - 1e-9)),
            "ny": max(1, math.ceil((n - s) / size - 1e-9))}


def _cell(spec: dict, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    ix = np.clip(((lon - spec["west"]) / spec["size"]).astype(int), 0, spec["nx"] - 1)
    iy = np.clip(((lat - spec["south"]) / spec["size"]).astype(int), 0, spec["ny"] - 1)
    return iy * spec["nx"] + ix


def _cell_month_sum(spec: dict, lat, lon, months, values) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    if len(values) == 0:
        return out
    cells = _cell(spec, np.asarray(lat, float), np.asarray(lon, float))
    months = np.asarray(months)
    values = np.asarray(values, float)
    for m in np.unique(months):
        sel = months == m
        out[int(m)] = np.bincount(cells[sel], weights=values[sel], minlength=spec["nx"] * spec["ny"])
    return out


def _annotate(monthly: list[dict], k: str, sfx: str, cal: dict, floor: float) -> None:
    key = k + sfx
    by_month = {m: [r for r in monthly if r["month"] == m and not r["partial"]] for m in range(1, 13)}
    for r in monthly:
        r[f"z_{key}"] = None
        if k in ("combined", "harm"):
            r[f"p_hi_{key}"] = r[f"p_lo_{key}"] = None
        if r[key] is None:
            continue
        base = np.array([x[key] for x in by_month[r["month"]] if x["year"] != r["year"] and x[key] is not None], float)
        med, spread, z = _robust(base, float(r[key]), floor)
        if z is None:
            continue
        r[f"z_{key}"] = round(z, 2)
        if k not in ("combined", "harm"):
            continue
        harmonized = k == "harm" or r["combined_source"] == "harmonize VIIRS"
        if harmonized:
            p_hi, p_lo = _prob_beyond(float(r[key]), med + 2 * spread, med - 2 * spread, cal["spread"], cal["unit"])
        else:
            p_hi, p_lo = float(z >= 2), float(z <= -2)
        r[f"p_hi_{key}"], r[f"p_lo_{key}"] = round(p_hi, 3), round(p_lo, 3)


def _season(monthly: list[dict], key: str) -> tuple[list[dict], list[int], float]:
    by_month = {m: [r for r in monthly if r["month"] == m and not r["partial"]] for m in range(1, 13)}
    season = []
    for m in range(1, 13):
        vals = np.array([r[key] for r in by_month[m] if r[key] is not None], float)
        season.append({"month": m, "name": MONTHS_TR[m - 1],
                       "median": round(float(np.median(vals)), 1) if len(vals) else None,
                       "q25": round(float(np.percentile(vals, 25)), 1) if len(vals) else None,
                       "q75": round(float(np.percentile(vals, 75)), 1) if len(vals) else None,
                       "n_years": int(len(vals))})
    tot = sum(s["median"] or 0 for s in season)
    critical, share = [], 0.0
    if tot > 0:
        for s in sorted(season, key=lambda x: -(x["median"] or 0)):
            critical.append(s["month"])
            share += (s["median"] or 0) / tot
            if share >= 0.7:
                break
    for s in season:
        s["share"] = round((s["median"] or 0) / tot, 3) if tot > 0 else 0.0
        s["critical"] = s["month"] in critical
    return season, sorted(critical), round(share, 3)


def _xcal_factor(monthly: list[dict], value) -> float | None:
    ref = tgt = 0.0
    n = 0
    for r in monthly:
        e = r["_est"]
        if XCAL_SAT in e and "Suomi NPP" in e and "NOAA-20" in e and not r["partial"]:
            ref += (value(e["Suomi NPP"]) + value(e["NOAA-20"])) / 2
            tgt += value(e[XCAL_SAT])
            n += 1
    if n < XCAL_MIN_MONTHS or tgt <= 0 or ref <= 0:
        return None
    return ref / tgt


def _choice_stats(res: dict) -> dict:
    v = res["calibration"].get("validation") or {}
    return {k: v.get(k) for k in ("harm_mae", "harm_month_err_pct", "harm_year_err_pct", "harm_year_err_mean_pct",
                                  "coverage80_calibrated", "raw_mae", "raw_month_err_pct")}


def run_climatology(client: FirmsClient, bbox, job, refresh: bool = False) -> dict:
    from .firea_infer import list_models

    key = bbox_str(bbox)
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    names = list_models()[:MAX_MODEL_CANDIDATES] or [None]
    runs = []
    for i, name in enumerate(names):
        res = _run_once(client, bbox, job, name, f"{i + 1}/{len(names)}" if len(names) > 1 else "")
        runs.append((name, res))
    scored = [(n, r) for n, r in runs if (r["calibration"].get("validation") or {}).get("harm_mae") is not None]
    # Pick the model with the lowest leave-one-year-out monthly error; without validation keep the newest model.
    name, result = min(scored, key=lambda x: x[1]["calibration"]["validation"]["harm_mae"]) if scored else runs[0]
    result["model_choice"] = {"chosen": name, "criterion": "harm_mae",
                              "candidates": [{"name": n, **_choice_stats(r)} for n, r in runs]}
    with _lock:
        _cache[key] = (time.time(), result)
    return result


def _run_once(client: FirmsClient, bbox, job, model_name: str | None, tag: str) -> dict:
    from .firea_infer import run_virtual_modis

    with job.lock:
        job.done = 0
        job.rows = 0

    today = date.today()
    last_day = today - timedelta(days=1)
    avail = availability_index(client.availability("ALL"))
    years = list(range(FIRST_YEAR, today.year + 1))
    spec = _grid_spec(bbox)
    n_cells = spec["nx"] * spec["ny"]
    plans = {}
    total = 0
    for y in years:
        s, e = date(y, 1, 1), min(date(y, 12, 31), last_day)
        segs = {"MODIS": _segments("MODIS", s, e, avail)}
        for prefix, start_y, sat in VIIRS_SOURCES:
            segs[sat] = _segments(prefix, s, e, avail) if y >= start_y else []
        plans[y] = (s, e, segs)
        for lst in segs.values():
            for src, a, b in lst:
                total += len(client.plan_area(src, bbox, a, b, avail)[1])
    if total == 0:
        raise FirmsError("bad_request", L("Seçilen bölge için FIRMS ürün kapsamı bulunamadı.", "No FIRMS product coverage found for the selected area."))
    job.total = total
    job.set_phase("download", L(f"{len(years)} yıllık kayıt indiriliyor (ilk sefer uzun sürebilir)…", f"Downloading {len(years)} years of records (the first run can take a while)…"))

    def on_progress(source, chunk, message):
        with job.lock:
            if chunk is not None:
                job.done += 1
                job.rows += chunk.rows
            if message:
                job.message = message

    def fetch(segs):
        frames, cover = [], {}
        for src, a, b in segs:
            out = client.fetch_area(src, bbox, a, b, availability=avail, on_progress=on_progress,
                                    should_cancel=job.should_cancel)
            frames.extend(out.frames)
            for ch in out.chunks:
                if ch.status in COVERED:
                    for d in ch.dates:
                        cover[d.isoformat()] = src
        return frames, cover

    daily: dict[str, dict] = {}
    monthly: list[dict] = []
    grid_raw: dict[str, dict] = {}
    drift: dict[int, float | None] = {}
    lab: dict[int, list[np.ndarray]] = {}
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    sats_used: set[str] = set()
    for y in years:
        if job.should_cancel():
            raise MatchingCancelled()
        s, e, segs = plans[y]
        job.message = L(f"{y}: indiriliyor…", f"{y}: downloading…")
        names = list(segs)
        with ThreadPoolExecutor(max_workers=len(names)) as ex:
            fetched = dict(zip(names, ex.map(fetch, [segs[n] for n in names])))
        if job.should_cancel():
            raise MatchingCancelled()
        mcov = fetched["MODIS"][1]
        sat_cov = {sat: fetched[sat][1] for _, _, sat in VIIRS_SOURCES}
        obs = None
        for frames, _ in fetched.values():
            if frames:
                n, _ = normalize_records(pd.concat(frames, ignore_index=True), origin="api")
                obs, _ = merge_observations(obs, n)
        days = pd.date_range(s, e, freq="D").strftime("%Y-%m-%d").tolist()
        aq_n = aq_f = modis_all = vn = vf = pd.Series(dtype=float)
        aq_grid: dict[str, dict[int, np.ndarray]] = {"count": {}, "frp": {}}
        sat_est: dict[str, dict] = {}
        if obs is not None and not obs.empty:
            o = obs[~_static(obs)]
            dstr = o["observed_at_utc"].dt.strftime("%Y-%m-%d")
            is_m = o["instrument"] == "MODIS"
            is_aq = is_m & (o["satellite"] == "Aqua")
            is_sn = ~is_m & (o["satellite"] == "Suomi NPP")
            frp = o["frp_mw"].fillna(0.0)
            aq_n = dstr[is_aq].value_counts()
            aq_f = frp[is_aq].groupby(dstr[is_aq]).sum()
            modis_all = dstr[is_m].value_counts()
            vn = dstr[is_sn].value_counts()
            vf = frp[is_sn].groupby(dstr[is_sn]).sum()
            drift[y] = _overpass_offset(o)
            aq = o[is_aq]
            aq_mon = aq["observed_at_utc"].dt.month.to_numpy()
            aq_grid["count"] = _cell_month_sum(spec, aq["latitude"], aq["longitude"], aq_mon, np.ones(len(aq)))
            aq_grid["frp"] = _cell_month_sum(spec, aq["latitude"], aq["longitude"], aq_mon, aq["frp_mw"].fillna(0.0))
            for sat, cov in sat_cov.items():
                if not cov or not (o["satellite"] == sat).any():
                    continue
                job.message = L(f"{y}: harmonize (FireA{' ' + tag if tag else ''}, {SAT_LABEL[sat]})…",
                                f"{y}: harmonizing (FireA{' ' + tag if tag else ''}, {SAT_LABEL[sat]})…")
                sub = obs[(obs["instrument"] == "MODIS") | (obs["satellite"] == sat)]
                vm = run_virtual_modis(sub, s, e, should_cancel=job.should_cancel, with_arrays=True, model_name=model_name)
                if not vm.get("available"):
                    continue
                arr = vm.get("arrays") or {"day": np.array([]), "mu": np.array([]), "p_det": np.array([]),
                                            "frp": np.array([]), "lat": np.array([]), "lon": np.array([]),
                                            "n": np.array([]), "daytime": np.array([], bool)}
                day_s = pd.Series(arr["day"])
                mu = np.asarray(arr["mu"], float)
                fdet = np.asarray(arr["p_det"]) * np.asarray(arr["frp"])
                mons = np.array([int(d[5:7]) for d in arr["day"]], int)
                grp = _groups(arr["n"], arr["daytime"])
                codes, udays = pd.factorize(np.asarray(arr["day"]))
                by_day = np.zeros((len(udays), N_GRP))
                np.add.at(by_day, (codes, grp), mu)
                if len(mu) and len(aq):
                    yk = _match_aqua(arr, ((aq["observed_at_utc"] - epoch) / pd.Timedelta(minutes=1)).to_numpy(float),
                                     aq["latitude"].to_numpy(float), aq["longitude"].to_numpy(float),
                                     aq["scan_km"].to_numpy(float), aq["track_km"].to_numpy(float))
                    seen = np.isin(np.asarray(arr["day"]), list(mcov))
                    acc = lab.setdefault(y, [np.zeros(N_GRP), np.zeros(N_GRP)])
                    acc[0] += np.bincount(grp[seen], weights=yk[seen], minlength=N_GRP)
                    acc[1] += np.bincount(grp[seen], weights=mu[seen], minlength=N_GRP)
                sat_est[sat] = {
                    "g_d": {d: by_day[i] for i, d in enumerate(udays)},
                    "frp_d": pd.Series(fdet).groupby(day_s).sum().to_dict() if len(day_s) else {},
                    "monthly": vm.get("monthly", {}),
                    "grid": {"count": _cell_month_group_sum(spec, arr["lat"], arr["lon"], mons, mu, grp),
                             "frp": _cell_month_sum(spec, arr["lat"], arr["lon"], mons, fdet)},
                }
        for sat, cov in sat_cov.items():
            if cov and sat not in sat_est:
                sat_est[sat] = {"g_d": {}, "frp_d": {}, "monthly": {}, "grid": {"count": {}, "frp": {}}}
        zero_g = np.zeros(N_GRP)
        for d in days:
            daily[d] = {
                "aqua": int(aq_n.get(d, 0)) if d in mcov else None,
                "viirs": int(vn.get(d, 0)) if d in sat_cov["Suomi NPP"] else None,
                "_h": {sat: sat_est[sat]["g_d"].get(d, zero_g) for sat in sat_est if d in sat_cov[sat]},
            }
        for mth in range(1, 13):
            md = [d for d in days if int(d[5:7]) == mth]
            if not md:
                continue
            n_full = calendar.monthrange(y, mth)[1]
            mcover = sum(1 for d in md if d in mcov) / n_full
            vcover = sum(1 for d in md if d in sat_cov["Suomi NPP"]) / n_full
            ym = f"{y}-{mth:02d}"
            ests = [sat for sat in sat_est if sum(1 for d in md if d in sat_cov[sat]) / n_full >= MIN_COVER]
            ok_m, ok_v, ok_sn = mcover >= MIN_COVER, bool(ests), vcover >= MIN_COVER
            msum = lambda series: float(sum(series.get(d, 0) for d in md))
            mon = lambda sat: sat_est[sat]["monthly"].get(ym, {"mean": 0.0, "lo": 0, "hi": 0})
            row = {
                "ym": ym, "year": y, "month": mth, "partial": len(md) < n_full,
                "modis_cover": round(mcover, 3), "viirs_cover": round(vcover, 3),
                "aqua": int(msum(aq_n)) if ok_m else None,
                "aqua_frp": round(msum(aq_f)) if ok_m else None,
                "modis_all": int(msum(modis_all)) if ok_m else None,
                "viirs": int(msum(vn)) if ok_sn else None,
                "viirs_frp": round(msum(vf)) if ok_sn else None,
                "_est": {x: {"g": np.sum([sat_est[x]["g_d"].get(d, zero_g) for d in md], axis=0),
                             "frp": float(sum(sat_est[x]["frp_d"].get(d, 0.0) for d in md)),
                             "lo": float(mon(x)["lo"]), "hi": float(mon(x)["hi"])} for x in ests},
                "products": sorted({src for d in md for src in [mcov.get(d)] + [c.get(d) for c in sat_cov.values()] if src}),
            }
            monthly.append(row)
            empty = {"count": np.zeros((N_GRP, n_cells)), "frp": np.zeros(n_cells)}
            grid_raw[ym] = {metric: {"aqua": aq_grid[metric].get(mth, np.zeros(n_cells)) if ok_m else None,
                                     "sats": {x: sat_est[x]["grid"][metric].get(mth, empty[metric]) for x in ests}}
                            for metric in METRICS}

    prev = None
    for y in years:
        if drift.get(y) is None and prev is not None and prev > DRIFT_MAX_MIN:
            drift[y] = prev
        prev = drift.get(y, prev)
    drifted = {y for y, d in drift.items() if d is not None and d > DRIFT_MAX_MIN}

    clean_years = [y for y in years if y not in drifted]
    size_r, n_labels = _size_factors(lab, clean_years)
    cv_r = {y: _size_factors(lab, [q for q in clean_years if q != y])[0] for y in clean_years}
    size_check = None
    if n_labels > 0:
        rows = [r for r in monthly if not r["partial"] and r["aqua"] is not None and r["year"] in clean_years
                and "Suomi NPP" in r["_est"]]
        if len(rows) >= 36:
            a = np.array([r["aqua"] for r in rows], float)
            mo = np.array([r["month"] for r in rows])
            yr = np.array([r["year"] for r in rows])
            g = lambda r, f: float(np.mean([float(e["g"] @ f) for k, e in r["_est"].items() if k != XCAL_SAT]))
            with_size = _loyo_mae(a, np.array([g(r, cv_r.get(r["year"], size_r)) for r in rows]), mo, yr)
            without = _loyo_mae(a, np.array([g(r, np.ones(N_GRP)) for r in rows]), mo, yr)
            size_check = {"mae_with": round(with_size, 2), "mae_without": round(without, 2)}
            # Keep the cluster-size correction only where it lowers the cross-validated error.
            if with_size >= without:
                size_r, cv_r = np.ones(N_GRP), {}
    xcal = {"count": _xcal_factor(monthly, lambda e: float(e["g"] @ size_r)),
            "frp": _xcal_factor(monthly, lambda e: e["frp"])}

    def sat_weight(sat: str, metric: str) -> float | None:
        if sat != XCAL_SAT:
            return 1.0
        return xcal[metric]

    for r in monthly:
        est = r.pop("_est")
        use = [x for x in est if sat_weight(x, "count") is not None]
        r["harm_sats"] = [SAT_LABEL[x] for x in use]
        sats_used.update(r["harm_sats"])
        if not use:
            r["harm_raw"] = r["harm_raw_lo"] = r["harm_raw_hi"] = r["harm_raw_frp"] = r["harm_fire"] = r["_cv"] = None
            continue
        wts = [sat_weight(x, "count") for x in use]
        r["harm_raw"] = round(float(np.mean([float(est[x]["g"] @ size_r) * w for x, w in zip(use, wts)])), 1)
        r["harm_fire"] = round(float(np.mean([float(est[x]["g"].sum()) * w for x, w in zip(use, wts)])), 1)
        r["_cv"] = float(np.mean([float(est[x]["g"] @ cv_r.get(r["year"], size_r)) * w for x, w in zip(use, wts)]))
        r["harm_raw_lo"] = float(np.mean([est[x]["lo"] * w for x, w in zip(use, wts)]))
        r["harm_raw_hi"] = float(np.mean([est[x]["hi"] * w for x, w in zip(use, wts)]))
        use_f = [x for x in est if sat_weight(x, "frp") is not None]
        r["harm_raw_frp"] = round(float(np.mean([est[x]["frp"] * sat_weight(x, "frp") for x in use_f])))
        for metric in METRICS:
            sats = grid_raw[r["ym"]][metric].pop("sats")
            ok = [x for x in sats if sat_weight(x, metric) is not None]
            vals = [(size_r @ sats[x] if metric == "count" else sats[x]) * sat_weight(x, metric) for x in ok]
            grid_raw[r["ym"]][metric]["harm"] = np.mean(vals, axis=0) if ok else None
    for v in daily.values():
        h = v.pop("_h")
        ok = [x for x in h if sat_weight(x, "count") is not None]
        v["harm"] = float(np.mean([float(h[x] @ size_r) * sat_weight(x, "count") for x in ok])) if ok else None

    ok_rows = [r for r in monthly if r["aqua"] and r["year"] not in drifted]
    n_aq = sum(r["aqua"] for r in ok_rows)
    frp_unit = round(sum(r["aqua_frp"] for r in ok_rows) / n_aq, 1) if n_aq >= 50 else DEFAULT_FRP_UNIT
    cals = {"count": _calibrate(monthly, drifted, "", 1.0), "frp": _calibrate(monthly, drifted, "_frp", frp_unit)}

    for r in monthly:
        r["aqua_drift_min"] = drift.get(r["year"])
        r.pop("harm_raw_lo", None)
        r.pop("harm_raw_hi", None)
        r.pop("_cv", None)
        aqua_ok = r["aqua"] is not None and r["year"] not in drifted
        if aqua_ok:
            src = "Aqua-MODIS"
        elif r["harm"] is not None:
            src = "harmonize VIIRS"
        elif r["aqua"] is not None:
            src = "Aqua-MODIS"
        else:
            src = None
        r["combined_source"] = src
        for sfx in METRICS.values():
            r["combined" + sfx] = (None if src is None else
                                   float(r["aqua" + sfx]) if src == "Aqua-MODIS" else r["harm" + sfx])

    beta_c = cals["count"]["beta"]
    for d, v in daily.items():
        if v["harm"] is not None:
            v["harm"] *= float(_factor(beta_c, [int(d[5:7])])[0])
        v["aqua_drifted"] = int(d[:4]) in drifted

    floors = {"count": MIN_SPREAD, "frp": MIN_SPREAD * frp_unit}
    for metric, sfx in METRICS.items():
        for k in ("combined", "aqua", "harm", "viirs"):
            _annotate(monthly, k, sfx, cals[metric], floors[metric])

    season, critical, share = _season(monthly, "combined")
    season_frp, critical_frp, share_frp = _season(monthly, "combined_frp")

    recent = None
    last_cov = max((d for d, v in daily.items() if v["harm"] is not None or v["aqua"] is not None), default=None)
    if last_cov:
        end_d = date.fromisoformat(last_cov)
        win = [end_d - timedelta(days=i) for i in range(RECENT_DAYS)]

        def window_value(year):
            vals, src = [], None
            for d0 in win:
                try:
                    v = daily.get(d0.replace(year=year).isoformat())
                except ValueError:
                    return None, None
                if v is None:
                    return None, None
                if v["aqua"] is not None and not v["aqua_drifted"]:
                    vals.append(v["aqua"])
                    src = src or "Aqua-MODIS"
                elif v["harm"] is not None:
                    vals.append(v["harm"])
                    src = "harmonize VIIRS"
                else:
                    return None, None
            return float(sum(vals)), src

        cur, src = window_value(end_d.year)
        base = np.array([b for b in (window_value(y)[0] for y in years if y < end_d.year) if b is not None], float)
        if cur is not None:
            med, spread, z = _robust(base, cur)
            p_hi = p_lo = None
            if z is not None:
                if src == "harmonize VIIRS":
                    p_hi, p_lo = _prob_beyond(cur, med + 2 * spread, med - 2 * spread, cals["count"]["spread"], 1.0)
                else:
                    p_hi, p_lo = float(z >= 2), float(z <= -2)
            recent = {"start": win[-1].isoformat(), "end": end_d.isoformat(), "value": round(cur, 1), "source": src,
                      "baseline_median": None if med is None else round(med, 1),
                      "robust_spread": None if spread is None else round(spread, 1),
                      "robust_z": None if z is None else round(z, 2), "n_years": int(len(base)),
                      "p_high": None if p_hi is None else round(p_hi, 3), "p_low": None if p_lo is None else round(p_lo, 3)}

    grid_out = {**spec, "cell_min_spread": {"count": CELL_MIN_SPREAD, "frp": CELL_MIN_SPREAD * frp_unit},
                "count": {}, "frp": {}, "source": {}}
    for r in monthly:
        g = grid_raw.get(r["ym"])
        if g is None or r["combined_source"] is None:
            continue
        grid_out["source"][r["ym"]] = "a" if r["combined_source"] == "Aqua-MODIS" else "h"
        for metric in METRICS:
            if r["combined_source"] == "Aqua-MODIS" and g[metric]["aqua"] is not None:
                vals = g[metric]["aqua"]
            elif g[metric]["harm"] is not None:
                vals = g[metric]["harm"] * float(_factor(cals[metric]["beta"], [r["month"]])[0])
            else:
                continue
            nd = 1 if metric == "count" else 0
            grid_out[metric][r["ym"]] = [[int(i), round(float(vals[i]), nd)] for i in np.nonzero(vals > 0.05)[0]]

    overlap = [(r["viirs"] / r["aqua"], r["harm_raw"] / r["aqua"]) for r in monthly
               if r["aqua"] and r["aqua"] >= 20 and r["harm_raw"] is not None and r["viirs"] is not None]
    annual = []
    for y in years:
        rows = [r for r in monthly if r["year"] == y]

        def ysum(k):
            vals = [r[k] for r in rows]
            return None if not vals or any(v is None for v in vals) else round(float(sum(vals)), 1)

        annual.append({"year": y, "partial": any(r["partial"] for r in rows) or len(rows) < 12,
                       **{k: ysum(k) for k in ("aqua", "viirs", "harm", "combined", "aqua_frp", "viirs_frp", "harm_frp", "combined_frp")}})

    def cal_out(c: dict) -> dict:
        return {"season_factor": [round(float(x), 3) for x in _factor(c["beta"], range(1, 13))],
                "spread": [round(c["spread"][0], 4), round(c["spread"][1], 4)], "unit": c["unit"],
                "method": ("seasonal" if c["local"] else ("seam" if c["n_pairs"] else "none")) if c["scaled"] else "uncalibrated",
                "validation": c["validation"]}

    sat_order = [SAT_LABEL[sat] for _, _, sat in VIIRS_SOURCES if SAT_LABEL[sat] in sats_used]
    result = {
        "bbox": list(bbox), "first_year": FIRST_YEAR, "viirs_start_year": VIIRS_START_YEAR,
        "last_day": last_cov, "months": monthly, "annual": annual,
        "season": season, "critical_months": critical, "critical_share": share,
        "season_frp": season_frp, "critical_months_frp": critical_frp, "critical_share_frp": share_frp,
        "recent": recent,
        "ratio_viirs_to_aqua_median": round(float(np.median([a for a, _ in overlap])), 2) if overlap else None,
        "ratio_harmonized_to_aqua_median": round(float(np.median([b for _, b in overlap])), 2) if overlap else None,
        "n_overlap_months": len(overlap),
        "anomaly_floor": floors,
        "calibration": {**cal_out(cals["count"]), "viirs_sats": sat_order,
                        "xcal_noaa21": None if xcal["count"] is None else round(xcal["count"], 3),
                        "size_correction": {"applied": bool(n_labels > 0 and not np.allclose(size_r, 1.0)), "n_labels": int(n_labels),
                                            "check": size_check,
                                            "sizes": ["1", "2", "3-5", "6-20", ">20"],
                                            "day": [round(float(x), 2) for x in size_r[1::2]],
                                            "night": [round(float(x), 2) for x in size_r[0::2]]},
                        "aqua_drift_min": {str(y): d for y, d in sorted(drift.items()) if d is not None},
                        "drifted_years": sorted(drifted)},
        "calibration_frp": cal_out(cals["frp"]),
        "grid": grid_out,
        "model": model_name,
    }
    return result
