from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sklearn.neighbors import BallTree

R_EARTH = 6371.0088
PASS_GAP_MIN = 10.0
VIIRS_ADJ_KM = 1.2
TILE_DEG = 0.05
MAX_CLUSTER_EXTENT_KM = 6.0
PAIR_MAX_DT_MIN = 60.0
EVID_MIN_KM, EVID_MAX_KM = 10.0, 1000.0
NBR_KM = 25.0
MAX_TOKENS = 64

TOKEN_FEATURES = ["log_frp", "frp_missing", "b4", "b5", "b45", "scan", "track", "conf_l", "conf_n", "conf_h",
                  "dx_km", "dy_km"]
CONTEXT_FEATURES = ["log_n", "log_frp_sum", "log_frp_max", "log_frp_mean", "extent_km", "v_scan", "v_track",
                    "frac_l", "frac_n", "frac_h", "day", "noaa20", "b4_mean", "b4_max", "b45_mean",
                    "m_scan", "m_track", "dt_h", "abs_dt_h", "sol_sin", "sol_cos",
                    "abs_lat", "season_sin", "season_cos", "log_nbr25"]

PREP_COLUMNS = ["instr", "sat", "t", "lat", "lon", "frp", "scan", "track", "b4", "b5", "conf", "day", "type"]
SAT_CODE = {"A": "Aqua", "AQUA": "Aqua", "T": "Terra", "TERRA": "Terra",
            "N": "SNPP", "NPP": "SNPP", "SUOMI NPP": "SNPP", "N20": "NOAA20", "1": "NOAA20", "NOAA-20": "NOAA20",
            "N21": "NOAA21", "2": "NOAA21", "NOAA-21": "NOAA21"}


def prep(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "latitude" not in {c.lower() for c in df.columns}:
        return pd.DataFrame(columns=PREP_COLUMNS)
    cols = {c.lower(): c for c in df.columns}
    g = lambda c: df[cols[c]] if c in cols else pd.Series([""] * len(df), index=df.index)
    instr = g("instrument").astype(str).str.upper().str[:5]
    if "bright_ti4" in cols:
        instr = instr.where(instr != "", "VIIRS")
    elif "brightness" in cols:
        instr = instr.where(instr != "", "MODIS")
    num = lambda s: pd.to_numeric(s.replace("", np.nan), errors="coerce")
    t = g("acq_time").astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(4)
    ts = pd.to_datetime(g("acq_date").astype(str) + t, format="%Y-%m-%d%H%M", errors="coerce", utc=True)
    out = pd.DataFrame({
        "instr": instr.to_numpy(),
        "sat": g("satellite").astype(str).str.strip().str.upper().map(SAT_CODE).fillna("?").to_numpy(),
        "t": ((ts - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(minutes=1)).to_numpy(float),
        "lat": num(g("latitude")).to_numpy(float),
        "lon": num(g("longitude")).to_numpy(float),
        "frp": num(g("frp")).to_numpy(float),
        "scan": num(g("scan")).to_numpy(float),
        "track": num(g("track")).to_numpy(float),
        "b4": num(g("bright_ti4") if "bright_ti4" in cols else g("brightness")).to_numpy(float),
        "b5": num(g("bright_ti5") if "bright_ti5" in cols else g("bright_t31")).to_numpy(float),
        "conf": g("confidence").astype(str).str.strip().str.lower().to_numpy(),
        "day": (g("daynight").astype(str).str.upper() == "D").astype(np.int8).to_numpy(),
        "type": num(g("type")).fillna(0).astype(int).to_numpy() if "type" in cols else np.zeros(len(df), int),
    })
    ok = np.isfinite(out["t"]) & np.isfinite(out["lat"]) & np.isfinite(out["lon"]) & (out["type"] == 0)
    return out[ok].reset_index(drop=True)


def assign_passes(df: pd.DataFrame) -> np.ndarray:
    pid = np.full(len(df), -1, dtype=np.int64)
    nxt = 0
    for s in df["sat"].unique():
        idx = np.where(df["sat"].to_numpy() == s)[0]
        idx = idx[np.argsort(df["t"].to_numpy()[idx], kind="stable")]
        new = np.ones(len(idx), bool)
        new[1:] = np.diff(df["t"].to_numpy()[idx]) > PASS_GAP_MIN
        pid[idx] = nxt + np.cumsum(new) - 1
        nxt = pid[idx].max() + 1
    return pid


def _rad(lat, lon):
    return np.radians(np.column_stack([lat, lon]))


def _haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * R_EARTH * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def viirs_clusters(v: pd.DataFrame) -> np.ndarray:
    cl = np.full(len(v), -1, dtype=np.int64)
    nxt = 0
    for pid in np.unique(v["pass"].to_numpy()):
        idx = np.where(v["pass"].to_numpy() == pid)[0]
        lat, lon = v["lat"].to_numpy()[idx], v["lon"].to_numpy()[idx]
        if len(idx) == 1:
            cl[idx] = nxt
            nxt += 1
            continue
        tree = BallTree(_rad(lat, lon), metric="haversine")
        nb = tree.query_radius(_rad(lat, lon), r=VIIRS_ADJ_KM / R_EARTH)
        rows = np.repeat(np.arange(len(idx)), [len(x) for x in nb])
        cols = np.concatenate(nb)
        g = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(idx), len(idx)))
        _, lab = connected_components(g, directed=False)
        out = np.empty(len(idx), dtype=np.int64)
        for c in np.unique(lab):
            m = np.where(lab == c)[0]
            ext = _haversine(lat[m].min(), lon[m].min(), lat[m].max(), lon[m].max()) if len(m) > 1 else 0.0
            if ext > MAX_CLUSTER_EXTENT_KM:
                tiles = (np.floor(lat[m] / TILE_DEG).astype(np.int64) * 100000 + np.floor(lon[m] / TILE_DEG).astype(np.int64))
                _, tl = np.unique(tiles, return_inverse=True)
                out[m] = nxt + tl
                nxt += tl.max() + 1
            else:
                out[m] = nxt
                nxt += 1
        cl[idx] = out
    return cl


def _neighbours(v: pd.DataFrame, tab: pd.DataFrame) -> np.ndarray:
    out = np.zeros(len(tab))
    pos = pd.Series(np.arange(len(tab)), index=tab.index)
    for pid, tg in tab.groupby("pass"):
        vp = v[v["pass"].to_numpy() == pid]
        tree = BallTree(_rad(vp["lat"].to_numpy(), vp["lon"].to_numpy()), metric="haversine")
        cnt = tree.query_radius(_rad(tg["lat"].to_numpy(), tg["lon"].to_numpy()), r=NBR_KM / R_EARTH, count_only=True)
        out[pos.loc[tg.index].to_numpy()] = np.maximum(cnt - tg["n"].to_numpy(), 0)
    return out


def cluster_table(v: pd.DataFrame) -> pd.DataFrame:
    conf = v["conf"].to_numpy()
    w = v.assign(
        cl=v["cl"], frp0=v["frp"].fillna(0.0),
        cl_l=(conf == "l").astype(float), cl_n=(conf == "n").astype(float), cl_h=(conf == "h").astype(float),
        b45=v["b4"] - v["b5"],
    )
    g = w.groupby("cl")
    tab = pd.DataFrame({
        "n": g.size(),
        "frp_sum": g["frp0"].sum(), "frp_max": g["frp0"].max(), "frp_mean": g["frp0"].mean(),
        "lat": g["lat"].mean(), "lon": g["lon"].mean(),
        "lat_min": g["lat"].min(), "lat_max": g["lat"].max(), "lon_min": g["lon"].min(), "lon_max": g["lon"].max(),
        "t": g["t"].mean(), "sat": g["sat"].first(), "pass": g["pass"].first(),
        "v_scan": g["scan"].mean(), "v_track": g["track"].mean(),
        "frac_l": g["cl_l"].mean(), "frac_n": g["cl_n"].mean(), "frac_h": g["cl_h"].mean(),
        "day": g["day"].mean().round(),
        "b4_mean": g["b4"].mean(), "b4_max": g["b4"].max(), "b45_mean": g["b45"].mean(),
    })
    tab["extent_km"] = np.where(tab["n"] > 1, _haversine(tab["lat_min"], tab["lon_min"], tab["lat_max"], tab["lon_max"]), 0.0)
    utc_h = (tab["t"] / 60.0) % 24
    sol = (utc_h + tab["lon"] / 15.0) % 24
    tab["sol_sin"] = np.sin(2 * np.pi * sol / 24)
    tab["sol_cos"] = np.cos(2 * np.pi * sol / 24)
    tab["noaa20"] = (tab["sat"] == "NOAA20").astype(float)
    tab["abs_lat"] = tab["lat"].abs()
    doy = pd.to_datetime(tab["t"], unit="m", utc=True).dt.day_of_year.to_numpy(float)
    doy = np.where(tab["lat"].to_numpy() < 0, doy + 182.6, doy)
    tab["season_sin"] = np.sin(2 * np.pi * doy / 365.25)
    tab["season_cos"] = np.cos(2 * np.pi * doy / 365.25)
    tab["log_nbr25"] = np.log1p(_neighbours(v, tab))
    tab["log_n"] = np.log1p(tab["n"])
    tab["log_frp_sum"] = np.log1p(tab["frp_sum"])
    tab["log_frp_max"] = np.log1p(tab["frp_max"])
    tab["log_frp_mean"] = np.log1p(tab["frp_mean"])
    return tab.drop(columns=["lat_min", "lat_max", "lon_min", "lon_max"])


def token_arrays(v: pd.DataFrame, tab: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    v = v.copy()
    v["frp_missing"] = v["frp"].isna().astype(float)
    v["log_frp"] = np.log1p(v["frp"].fillna(0.0))
    v["b4n"] = (v["b4"].fillna(330) - 330) / 30
    v["b5n"] = (v["b5"].fillna(295) - 295) / 15
    v["b45n"] = (v["b4"].fillna(330) - v["b5"].fillna(295) - 35) / 25
    c = v["conf"].to_numpy()
    v["conf_l"], v["conf_n"], v["conf_h"] = (c == "l") * 1.0, (c == "n") * 1.0, (c == "h") * 1.0
    cen = tab[["lat", "lon"]]
    clat = v["cl"].map(cen["lat"]).to_numpy()
    clon = v["cl"].map(cen["lon"]).to_numpy()
    v["dy_km"] = (v["lat"].to_numpy() - clat) * 111.2
    v["dx_km"] = (v["lon"].to_numpy() - clon) * 111.2 * np.cos(np.radians(clat))
    v = v.sort_values(["cl", "log_frp"], ascending=[True, False])
    v["rank"] = v.groupby("cl").cumcount()
    v = v[v["rank"] < MAX_TOKENS]
    feats = v[["log_frp", "frp_missing", "b4n", "b5n", "b45n", "scan", "track", "conf_l", "conf_n", "conf_h",
               "dx_km", "dy_km"]].to_numpy(np.float32)
    feats = np.nan_to_num(feats, nan=0.0)
    counts = v.groupby("cl").size().reindex(tab.index).fillna(0).astype(int).to_numpy()
    order = np.argsort(tab.index.to_numpy(), kind="stable")
    assert (order == np.arange(len(order))).all(), "tab küme numarasına göre sıralı olmalı"
    offsets = np.concatenate([[0], np.cumsum(counts)])
    return feats, offsets


def label_clusters(v: pd.DataFrame, tab: pd.DataFrame, aqua: pd.DataFrame) -> pd.DataFrame:
    K = len(tab)
    pos = {cid: i for i, cid in enumerate(tab.index)}
    observable = np.zeros(K, bool)
    best_dt = np.full(K, np.inf)
    best_ap = np.full(K, -1, dtype=np.int64)
    m_scan = np.full(K, np.nan)
    m_track = np.full(K, np.nan)
    counts: dict[tuple[int, int], int] = {}
    if not aqua.empty and K:
        a_pass_t = aqua.groupby("pass")["t"].agg(["min", "max"])
        a_groups = {ap: g for ap, g in aqua.groupby("pass")}
        for vp, cl_tab in tab.groupby("pass"):
            vt = cl_tab["t"].mean()
            cands = a_pass_t[(a_pass_t["min"] - PAIR_MAX_DT_MIN <= vt) & (a_pass_t["max"] + PAIR_MAX_DT_MIN >= vt)]
            if cands.empty:
                continue
            vpix = v[v["pass"] == vp]
            vtree = BallTree(_rad(vpix["lat"].to_numpy(), vpix["lon"].to_numpy()), metric="haversine")
            cl_ids = cl_tab.index.to_numpy()
            cl_t = cl_tab["t"].to_numpy()
            cl_xy = _rad(cl_tab["lat"].to_numpy(), cl_tab["lon"].to_numpy())
            for ap in cands.index:
                ad = a_groups[ap]
                a_t, a_scan, a_track = ad["t"].to_numpy(), ad["scan"].to_numpy(), ad["track"].to_numpy()
                atree = BallTree(_rad(ad["lat"].to_numpy(), ad["lon"].to_numpy()), metric="haversine")
                inds, dists = atree.query_radius(cl_xy, r=EVID_MAX_KM / R_EARTH, return_distance=True)
                for k in range(len(cl_ids)):
                    dkm = dists[k] * R_EARTH
                    # Evidence that MODIS looked comes only from detections 10-300 km away, never from the
                    # cluster's own detections, so whether a cluster is used never depends on its label.
                    ring_m = dkm >= EVID_MIN_KM
                    if not ring_m.any():
                        continue
                    ring = inds[k][ring_m]
                    dt = float(a_t[ring].mean() - cl_t[k])
                    i = pos[cl_ids[k]]
                    if abs(dt) > PAIR_MAX_DT_MIN or abs(dt) >= abs(best_dt[i]):
                        continue
                    wts = 1.0 / np.maximum(dkm[ring_m], 1.0)
                    observable[i] = True
                    best_dt[i] = dt
                    best_ap[i] = ap
                    m_scan[i] = float(np.average(a_scan[ring], weights=wts))
                    m_track[i] = float(np.average(a_track[ring], weights=wts))
                d, j = vtree.query(_rad(ad["lat"].to_numpy(), ad["lon"].to_numpy()), k=1)
                dkm = d[:, 0] * R_EARTH
                r_assign = np.maximum(1.5, 0.5 * np.fmax(a_scan, a_track) + 0.75)
                hit = dkm <= r_assign
                for cid, c in pd.Series(vpix["cl"].to_numpy()[j[hit, 0]]).value_counts().items():
                    counts[(int(cid), int(ap))] = int(c)
    y = np.array([counts.get((int(cid), int(best_ap[i])), 0) if observable[i] else 0
                  for i, cid in enumerate(tab.index)], dtype=np.int64)
    return pd.DataFrame({"observable": observable, "y": y, "m_scan": m_scan, "m_track": m_track,
                         "dt_h": np.where(observable, best_dt / 60.0, np.nan)}, index=tab.index)
