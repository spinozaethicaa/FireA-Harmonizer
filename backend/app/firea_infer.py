from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from .config import L

log = logging.getLogger("app.firea")
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
SAT_MAP = {"Suomi NPP": "SNPP", "NOAA-20": "NOAA20", "Aqua": "Aqua", "Terra": "Terra", "NOAA-21": "NOAA21"}
MC_DRAWS = 300
MAX_MAP_CLUSTERS = 20000

_lock = threading.Lock()
_cache: dict = {}


def list_models() -> list[str]:
    if not MODELS_DIR.exists():
        return []
    def trained_at(d: Path) -> str:
        try:
            return json.loads((d / "config.json").read_text(encoding="utf-8")).get("trained_at_utc") or ""
        except (OSError, ValueError):
            return ""

    cands = sorted([p for p in MODELS_DIR.glob("FireA-*") if (p / "weights.pt").exists()],
                   key=lambda d: (trained_at(d), d.name), reverse=True)
    return [p.name for p in cands]


def _find_model_dir(name: str | None = None) -> Path | None:
    names = list_models()
    if name is not None:
        return MODELS_DIR / name if name in names else None
    return MODELS_DIR / names[0] if names else None


def load_model(name: str | None = None):
    with _lock:
        d = _find_model_dir(name)
        key = d.name if d is not None else None
        if key in _cache:
            return _cache[key]
        if d is None:
            return None, None, L("FireA modeli bulunamadı (backend/models/FireA-*). Önce `python -m firea.train` çalıştırın.", "FireA model not found (backend/models/FireA-*). Run `python -m firea.train` first.")
        try:
            import torch
            from firea.model import FireA
        except Exception as exc:
            return None, None, L("PyTorch yüklenemedi: ", "PyTorch could not be loaded: ") + str(exc)
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        a = cfg["arch"]
        model = FireA(a["n_tok"], a["n_ctx"], a["d"], a["layers"], a["heads"], a["dropout"])
        model.load_state_dict(torch.load(d / "weights.pt", map_location="cpu", weights_only=True))
        model.eval()
        met = json.loads((d / "metrics.json").read_text(encoding="utf-8")) if (d / "metrics.json").exists() else {}
        _cache[key] = (model, cfg, met)
        return model, cfg, met


def model_summary(cfg: dict, met: dict) -> dict:
    t = met.get("test", {})
    pick = lambda m: {k: m.get(k) for k in ("poisson_deviance", "mae", "auc_detection", "interval80_coverage",
                                            "region_day_total_abs_pct_err_median")} if m else None
    return {
        "name": cfg["name"], "n_params": cfg["n_params"], "trained_at_utc": cfg.get("trained_at_utc"),
        "target": cfg.get("target"),
        "test_regions": [r["name"] for r in cfg.get("regions", []) if r["split"] == "test"],
        "train_regions": [r["name"] for r in cfg.get("regions", []) if r["split"] == "train"],
        "test_metrics": {"FireA": pick(t.get("FireA")), "gbm": pick(t.get("gbm")), "power_law": pick(t.get("power_law"))},
        "split_sizes": met.get("split_sizes"),
    }


def _frame(obs: pd.DataFrame) -> pd.DataFrame:
    raw = obs["raw"]
    get = lambda keys: raw.map(lambda r: next((r.get(k) for k in keys if r.get(k) not in (None, "")), None))
    num = lambda s: pd.to_numeric(s, errors="coerce")
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    f = pd.DataFrame({
        "obs_id": obs["observation_id"].to_numpy(),
        "instr": obs["instrument"].to_numpy(),
        "sat": obs["satellite"].map(SAT_MAP).fillna("?").to_numpy(),
        "t": ((obs["observed_at_utc"] - epoch) / pd.Timedelta(minutes=1)).to_numpy(float),
        "lat": obs["latitude"].to_numpy(float), "lon": obs["longitude"].to_numpy(float),
        "frp": obs["frp_mw"].to_numpy(float), "scan": obs["scan_km"].to_numpy(float),
        "track": obs["track_km"].to_numpy(float),
        "b4": num(get(["bright_ti4", "brightness"])).to_numpy(float),
        "b5": num(get(["bright_ti5", "bright_t31"])).to_numpy(float),
        "conf": obs["confidence_raw"].astype(str).str.strip().str.lower().to_numpy(),
        "day": (obs["daynight"] == "D").astype(np.int8).to_numpy(),
        "type": num(get(["type"])).fillna(0).astype(int).to_numpy(),
        "day_str": obs["observed_at_utc"].dt.strftime("%Y-%m-%d").to_numpy(),
    })
    return f[f["type"] == 0].reset_index(drop=True)


def run_virtual_modis(obs: pd.DataFrame, start, end, should_cancel=None, aqua_lst_h: float | None = None,
                      with_arrays: bool = False, model_name: str | None = None) -> dict:
    model, cfg, met = load_model(model_name)
    if model is None:
        return {"available": False, "reason": met}
    import torch
    from firea.features import assign_passes, cluster_table, token_arrays, viirs_clusters
    from firea.model import nb_pmf

    days = pd.date_range(start, end, freq="D").strftime("%Y-%m-%d").tolist()
    base = {"available": True, "model": model_summary(cfg, met), "days": days}
    f = _frame(obs) if len(obs) else pd.DataFrame()
    aqua_daily = (f[(f["instr"] == "MODIS") & (f["sat"] == "Aqua")].groupby("day_str").size()
                  if len(f) else pd.Series(dtype=int))
    v = f[(f["instr"] == "VIIRS") & f["sat"].isin(["SNPP", "NOAA20", "NOAA21"])].reset_index(drop=True) if len(f) else f
    if v is None or len(v) == 0:
        base.update(clusters=[], daily=[{"date": d, "aqua_observed": int(aqua_daily.get(d, 0)), "viirs_pixels": 0,
                                          "virtual_mean": 0.0, "virtual_lo": 0, "virtual_hi": 0} for d in days],
                    n_clusters=0, note=L("VIIRS (S-NPP/NOAA-20) gözlemi yok.", "No VIIRS (S-NPP/NOAA-20) observations."),
                    monthly={m: {"mean": 0.0, "lo": 0, "hi": 0} for m in sorted({d[:7] for d in days})},
                    totals={"viirs_pixels": 0, "virtual_mean": 0.0, "virtual_lo": 0, "virtual_hi": 0,
                            "aqua_observed": int(aqua_daily.sum()) if len(aqua_daily) else 0})
        return base

    v["pass"] = assign_passes(v)
    v["cl"] = viirs_clusters(v)
    tab = cluster_table(v)
    tok, off = token_arrays(v, tab)
    feats = cfg["context_features"]
    mean, std = np.array(cfg["ctx_mean"]), np.array(cfg["ctx_std"])
    geo = np.array(cfg["geometry_samples"])
    G, K = len(geo), len(tab)

    ctx_base = tab.reindex(columns=feats).copy()
    if aqua_lst_h is None:
        ctx_base["dt_h"] = 0.0
        ctx_base["abs_dt_h"] = 0.0
    else:
        lst = ((tab["t"].to_numpy(float) / 60.0) % 24 + tab["lon"].to_numpy(float) / 15.0) % 24
        dt = (aqua_lst_h - lst + 6) % 12 - 6
        ctx_base["dt_h"] = dt
        ctx_base["abs_dt_h"] = np.abs(dt)
    mu = np.zeros((K, G))
    al = np.zeros((K, G))
    lengths = np.diff(off)
    order = np.argsort(lengths, kind="stable")
    bs = 512
    with torch.no_grad():
        for s in range(0, K, bs):
            if should_cancel is not None and should_cancel():
                from .matching import MatchingCancelled
                raise MatchingCancelled()
            idx = order[s:s + bs]
            n_tok = max(1, int(lengths[idx].max()))
            tokens = np.zeros((len(idx), n_tok, tok.shape[1]), np.float32)
            mask = np.ones((len(idx), n_tok), bool)
            for b, i in enumerate(idx):
                a, e = off[i], off[i + 1]
                tokens[b, :e - a] = tok[a:e]
                mask[b, :e - a] = False
            z = model.encode(torch.from_numpy(tokens), torch.from_numpy(mask))
            for g in range(G):
                c = ctx_base.iloc[idx].copy()
                c["m_scan"], c["m_track"] = geo[g, 0], geo[g, 1]
                x = np.nan_to_num((c.to_numpy(float) - mean) / std, nan=0.0).clip(-8, 8).astype(np.float32)
                m_, a_ = model.decode(z, torch.from_numpy(x))
                mu[idx, g] = m_.numpy()
                al[idx, g] = a_.numpy()

    mu_mean = mu.mean(1)
    k_max = int(min(3000, max(30, np.percentile(mu.max(1), 99.9) * 6 + 30)))
    lo = np.zeros(K, int)
    hi = np.zeros(K, int)
    p0 = np.zeros(K)
    for s in range(0, K, 2048):
        pmf = nb_pmf(k_max, mu[s:s + 2048].reshape(-1), al[s:s + 2048].reshape(-1)).numpy()
        pmf = pmf.reshape(-1, G, k_max + 1).mean(1)
        cdf = pmf.cumsum(1)
        lo[s:s + 2048] = (cdf < 0.1).sum(1)
        hi[s:s + 2048] = (cdf < 0.9).sum(1)
        p0[s:s + 2048] = pmf[:, 0]

    rng = np.random.default_rng(0)
    passes = tab["pass"].to_numpy()
    upass, pinv = np.unique(passes, return_inverse=True)
    gidx = rng.integers(0, G, size=(len(upass), MC_DRAWS))[pinv]
    m_s = np.take_along_axis(mu, gidx, 1)
    a_s = np.take_along_axis(al, gidx, 1)
    lam = rng.gamma(shape=1.0 / a_s, scale=a_s * m_s)
    draws = rng.poisson(lam)
    tday = pd.to_datetime(tab["t"].to_numpy(), unit="m", utc=True).strftime("%Y-%m-%d")
    viirs_daily = v.groupby("day_str").size()
    daily = []
    for d in days:
        sel = tday == d
        tot = draws[sel].sum(0) if sel.any() else np.zeros(MC_DRAWS)
        daily.append({
            "date": d,
            "aqua_observed": int(aqua_daily.get(d, 0)),
            "viirs_pixels": int(viirs_daily.get(d, 0)),
            "viirs_clusters": int(sel.sum()),
            "virtual_mean": round(float(mu_mean[sel].sum()), 2),
            "virtual_lo": int(np.percentile(tot, 10)),
            "virtual_hi": int(np.percentile(tot, 90)),
        })

    tmon = np.array([d[:7] for d in tday])
    monthly = {}
    for mth in sorted({d[:7] for d in days}):
        sel = tmon == mth
        tot = draws[sel].sum(0) if sel.any() else np.zeros(MC_DRAWS)
        monthly[mth] = {"mean": round(float(mu_mean[sel].sum()), 2),
                        "lo": int(np.percentile(tot, 10)), "hi": int(np.percentile(tot, 90))}
    base["monthly"] = monthly
    if with_arrays:
        base["arrays"] = {"lat": tab["lat"].to_numpy(float), "lon": tab["lon"].to_numpy(float), "day": np.asarray(tday),
                          "mu": mu_mean, "p_det": 1.0 - p0, "frp": tab["frp_sum"].to_numpy(float),
                          "n": tab["n"].to_numpy(int), "daytime": tab["day"].to_numpy(float) > 0.5,
                          "t": tab["t"].to_numpy(float),
                          "pix_t": v["t"].to_numpy(float), "pix_lat": v["lat"].to_numpy(float),
                          "pix_lon": v["lon"].to_numpy(float),
                          "pix_cl": v["cl"].map(pd.Series(np.arange(len(tab)), index=tab.index)).to_numpy(int)}

    ts = pd.to_datetime(tab["t"].to_numpy(), unit="m", utc=True).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = np.argsort(-mu_mean)[:MAX_MAP_CLUSTERS]
    clusters = [{
        "cluster": int(tab.index[i]), "lat": round(float(tab["lat"].iat[i]), 5), "lon": round(float(tab["lon"].iat[i]), 5),
        "time_utc": ts[i], "date": tday[i], "satellite": tab["sat"].iat[i], "n_viirs": int(tab["n"].iat[i]),
        "frp_sum_mw": round(float(tab["frp_sum"].iat[i]), 1),
        "modis_like_mean": round(float(mu_mean[i]), 2), "modis_like_lo": int(lo[i]), "modis_like_hi": int(hi[i]),
        "p_detect": round(float(1 - p0[i]), 3),
    } for i in keep]
    base.update(clusters=clusters, daily=daily, n_clusters=int(K),
                totals={"viirs_pixels": int(len(v)), "virtual_mean": round(float(mu_mean.sum()), 1),
                        "virtual_lo": int(np.percentile(draws.sum(0), 10)),
                        "virtual_hi": int(np.percentile(draws.sum(0), 90)),
                        "aqua_observed": int(aqua_daily.sum()) if len(aqua_daily) else 0})
    return base
