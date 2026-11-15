from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .config import L

EARTH_R_KM = 6371.0088
FULL_MATRIX_MAX = 2500


@dataclass
class MatchParams:
    cross_dist_km: float = 1.5
    cross_dt_min: float = 60.0
    use_pixel_size: bool = True
    same_pass_adj_km: float = 1.0
    pass_window_min: float = 10.0
    max_cluster_span_min: float = 90.0
    max_cluster_extent_km: float = 10.0
    cont_dist_km: float = 2.0
    cont_gap_h: float = 48.0
    max_event_extent_km: float = 30.0

    def to_dict(self):
        return asdict(self)


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype=float)) for x in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bbox_extent_km(lats, lons) -> float:
    if len(lats) < 2:
        return 0.0
    return float(haversine_km(np.min(lats), np.min(lons), np.max(lats), np.max(lons)))


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


EMPTY_LINKS = pd.DataFrame(columns=[
    "link_id", "modis_observation_id", "viirs_observation_id", "modis_satellite", "viirs_satellite",
    "distance_km", "threshold_km", "threshold_basis", "pixel_relative_distance", "dt_min", "same_cluster", "cluster_id"])


class MatchingCancelled(Exception):
    pass


def run_matching(obs: pd.DataFrame, p: MatchParams, should_cancel=None) -> dict:
    if obs.empty:
        return {"observations": obs.assign(pass_id=None, cluster_id=None, event_id=None,
                                           n_cross_links=0, cross_matched=False, match_flags=""),
                "links": EMPTY_LINKS.copy(), "clusters": [], "continuity": [], "events": []}

    obs = obs.sort_values(["observed_at_utc", "observation_id"]).reset_index(drop=True)
    n = len(obs)
    lat = obs["latitude"].to_numpy(float)
    lon = obs["longitude"].to_numpy(float)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    t = ((obs["observed_at_utc"] - epoch) / pd.Timedelta(minutes=1)).to_numpy(float)
    tstr = obs["observed_at_utc"].dt.strftime("%Y-%m-%dT%H:%MZ").to_numpy()
    instr = obs["instrument"].to_numpy()
    is_modis = instr == "MODIS"
    sat = obs["satellite"].to_numpy()
    ids = obs["observation_id"].to_numpy()
    pix = np.fmax(obs["scan_km"].to_numpy(float), obs["track_km"].to_numpy(float))
    pix_eff = np.where(np.isnan(pix), np.where(is_modis, 1.0, 0.375), pix)

    pass_code = np.zeros(n, dtype=np.int64)
    pass_label: list[str] = []
    for s in pd.unique(sat):
        idx = np.where(sat == s)[0]
        new = np.ones(len(idx), dtype=bool)
        new[1:] = np.diff(t[idx]) > p.pass_window_min
        starts = np.cumsum(new) - 1
        base = len(pass_label)
        first_idx = idx[new]
        pass_label.extend(f"{s}|{tstr[i]}" for i in first_idx)
        pass_code[idx] = base + starts
    pass_id = np.array(pass_label, dtype=object)[pass_code]
    obs["pass_id"] = pass_id

    window = max(p.cross_dt_min, p.pass_window_min)
    brk = np.ones(n, dtype=bool)
    brk[1:] = np.diff(t) > window
    group_starts = np.where(brk)[0]
    group_ends = np.append(group_starts[1:], n)
    Ei, Ej, Ed, Ethr, Ekind, Edt, Epix = [], [], [], [], [], [], []

    def emit(ii, jj):
        d = haversine_km(lat[ii], lon[ii], lat[jj], lon[jj])
        dt = np.abs(t[jj] - t[ii])
        cross = instr[ii] != instr[jj]
        same = pass_code[ii] == pass_code[jj]
        half_pix = 0.5 * (pix_eff[ii] + pix_eff[jj])
        thr_c = np.maximum(p.cross_dist_km, half_pix) if p.use_pixel_size else np.full(d.shape, p.cross_dist_km)
        thr_a = np.maximum(p.same_pass_adj_km, 1.2 * half_pix) if p.use_pixel_size else np.full(d.shape, p.same_pass_adj_km)
        mc = cross & (dt <= p.cross_dt_min) & (d <= thr_c)
        ma = same & (d <= thr_a)
        for mask, thr, kind in ((mc, thr_c, 0), (ma, thr_a, 1)):
            if mask.any():
                Ei.append(ii[mask]); Ej.append(jj[mask]); Ed.append(d[mask]); Ethr.append(thr[mask])
                Ekind.append(np.full(int(mask.sum()), kind, dtype=np.int8)); Edt.append(dt[mask])
                Epix.append(half_pix[mask])

    for gi, (gs, ge) in enumerate(zip(group_starts, group_ends)):
        if should_cancel is not None and gi % 100 == 0 and should_cancel():
            raise MatchingCancelled()
        m = ge - gs
        if m < 2:
            continue
        if m <= FULL_MATRIX_MAX:
            a, b = np.triu_indices(m, k=1)
            ii, jj = a + gs, b + gs
            keep = (t[jj] - t[ii]) <= window
            emit(ii[keep], jj[keep])
        else:
            lat_pad = (max(p.cross_dist_km, p.same_pass_adj_km) + 10.0) / 111.0
            for i in range(gs, ge - 1):
                j_end = np.searchsorted(t, t[i] + window, side="right", sorter=None)
                j_end = min(j_end, ge)
                js = np.arange(i + 1, j_end)
                js = js[np.abs(lat[js] - lat[i]) <= lat_pad]
                if js.size:
                    emit(np.full(js.size, i), js)

    cat = (lambda L, dt: np.concatenate(L) if L else np.array([], dtype=dt))
    E_i, E_j = cat(Ei, np.int64), cat(Ej, np.int64)
    E_d, E_thr, E_dt, E_pix = cat(Ed, float), cat(Ethr, float), cat(Edt, float), cat(Epix, float)
    E_kind = cat(Ekind, np.int8)

    clusters: list[dict] = []
    label = np.full(n, -1, dtype=np.int64)

    def components(members: np.ndarray, eidx: np.ndarray, scale: float):
        local = {int(g): k for k, g in enumerate(members)}
        uf = _UF(len(members))
        sel = eidx[E_d[eidx] <= E_thr[eidx] * scale]
        for a, b in zip(E_i[sel].tolist(), E_j[sel].tolist()):
            uf.union(local[a], local[b])
        roots = np.array([uf.find(k) for k in range(len(members))])
        out = []
        for r in np.unique(roots):
            comp = members[roots == r]
            out.append(comp)
        lab_local = {}
        for k, comp in enumerate(out):
            label[comp] = k
        res = []
        if sel.size:
            li, lj = label[E_i[sel]], label[E_j[sel]]
        for k, comp in enumerate(out):
            ce = sel[(li == k) & (lj == k)] if sel.size else sel
            res.append((comp, ce))
        label[members] = -1
        del lab_local
        return res

    def split_by_time(members: np.ndarray) -> list[np.ndarray]:
        pieces = [members[np.argsort(t[members], kind="stable")]]
        out = []
        while pieces:
            m = pieces.pop()
            if len(m) < 2 or t[m[-1]] - t[m[0]] <= p.max_cluster_span_min:
                out.append(m)
                continue
            cut = int(np.argmax(np.diff(t[m]))) + 1
            pieces.extend([m[:cut], m[cut:]])
        return out

    def edges_within(members: np.ndarray, eidx: np.ndarray) -> np.ndarray:
        if not eidx.size:
            return eidx
        inset = np.zeros(n, dtype=bool)
        inset[members] = True
        return eidx[inset[E_i[eidx]] & inset[E_j[eidx]]]

    def refine(members, eidx, scale, depth, flags):
        stack = [(members, eidx, scale, depth, flags)]
        steps = 0
        while stack:
            steps += 1
            if should_cancel is not None and steps % 200 == 0 and should_cancel():
                raise MatchingCancelled()
            members, eidx, scale, depth, flags = stack.pop()
            for comp, ce in components(members, eidx, scale):
                if len(comp) == 1:
                    clusters.append({"members": comp, "flags": list(dict.fromkeys(flags))})
                    continue
                span = t[comp].max() - t[comp].min()
                extent = bbox_extent_km(lat[comp], lon[comp])
                if span > p.max_cluster_span_min:
                    for piece in split_by_time(comp):
                        stack.append((piece, edges_within(piece, ce), scale, depth,
                                      flags + (L("zaman aralığı sınırı nedeniyle bölündü", "split because of the time-span limit"),)))
                elif extent > p.max_cluster_extent_km and depth < 3:
                    stack.append((comp, ce, scale * 0.7, depth + 1,
                                  flags + (L(f"yayılım > {p.max_cluster_extent_km:g} km; eşik daraltıldı", f"extent > {p.max_cluster_extent_km:g} km; threshold tightened"),)))
                else:
                    f = list(dict.fromkeys(flags))
                    if extent > p.max_cluster_extent_km:
                        f.append(L("zincirleme uyarısı: daraltmaya rağmen yayılım sınırı aşılıyor", "chaining warning: extent limit still exceeded after tightening"))
                    clusters.append({"members": comp, "flags": f})

    refine(np.arange(n), np.arange(len(E_i)), 1.0, 0, ())

    cluster_of = np.empty(n, dtype=object)
    for c in clusters:
        m = c["members"]
        cid = "CL-" + _short_hash(min(ids[m]) + f"|{len(m)}")
        cluster_of[m] = cid
        c["cluster_id"] = cid
        c["t_start"] = float(t[m].min())
        c["t_end"] = float(t[m].max())
    obs["cluster_id"] = cluster_of

    cm = E_kind == 0
    ci, cj = E_i[cm], E_j[cm]
    mi = np.where(is_modis[ci], ci, cj)
    vi = np.where(is_modis[ci], cj, ci)
    n_cross = np.bincount(np.concatenate([ci, cj]), minlength=n) if ci.size else np.zeros(n, dtype=int)
    same_cl = cluster_of[ci] == cluster_of[cj] if ci.size else np.array([], dtype=bool)
    if ci.size:
        links = pd.DataFrame({
            "link_id": ["LN-" + _short_hash(a + b) for a, b in zip(ids[mi], ids[vi])],
            "modis_observation_id": ids[mi],
            "viirs_observation_id": ids[vi],
            "modis_satellite": sat[mi],
            "viirs_satellite": sat[vi],
            "distance_km": np.round(E_d[cm], 3),
            "threshold_km": np.round(E_thr[cm], 3),
            "threshold_basis": np.where(p.use_pixel_size & (E_thr[cm] > p.cross_dist_km), L("piksel boyutu", "pixel size"), L("sabit eşik", "fixed threshold")),
            "pixel_relative_distance": np.round(E_d[cm] / np.maximum(E_pix[cm], 1e-6), 3),
            "dt_min": np.round(E_dt[cm], 1),
            "same_cluster": same_cl,
            "cluster_id": np.where(same_cl, cluster_of[mi], None),
        })
    else:
        links = EMPTY_LINKS.copy()
    obs["n_cross_links"] = n_cross
    obs["cross_matched"] = n_cross > 0
    flags = np.array([""] * n, dtype=object)
    if ci.size:
        pp = pd.DataFrame({"v": vi, "p": pass_code[mi]}).drop_duplicates()
        multi = pp.groupby("v").size()
        flags[multi[multi > 1].index.to_numpy()] = L("birden fazla MODIS geçişiyle eşleşti (belirsiz)", "matched more than one MODIS overpass (ambiguous)")
    obs["match_flags"] = flags

    clusters.sort(key=lambda c: (c["t_start"], c["cluster_id"]))
    events: list[dict] = []
    continuity: list[dict] = []
    gap_min = p.cont_gap_h * 60.0
    cap = max(16, len(clusters))
    ev_box = np.zeros((cap, 4))
    ev_tlast = np.full(cap, -np.inf)
    n_ev = 0
    pad_lat = p.cont_dist_km / 111.0

    for c_idx, c in enumerate(clusters):
        if should_cancel is not None and c_idx % 200 == 0 and should_cancel():
            raise MatchingCancelled()
        m = c["members"]
        mlat, mlon = lat[m], lon[m]
        c_box = (mlat.min(), mlat.max(), mlon.min(), mlon.max())
        pad_lon = pad_lat / max(0.05, np.cos(np.radians(min(abs(c_box[0]), abs(c_box[1])))))
        cands = []
        if n_ev:
            B = ev_box[:n_ev]
            sel = np.where(
                (c["t_start"] - ev_tlast[:n_ev] <= gap_min)
                & (B[:, 0] <= c_box[1] + pad_lat) & (B[:, 1] >= c_box[0] - pad_lat)
                & (B[:, 2] <= c_box[3] + pad_lon) & (B[:, 3] >= c_box[2] - pad_lon)
            )[0]
            for k_ev in sel:
                ev = events[k_ev]
                em = np.asarray(ev["members"])
                recent = em[t[em] >= c["t_start"] - gap_min]
                if recent.size == 0:
                    continue
                dmat = haversine_km(mlat[:, None], mlon[:, None], lat[recent][None, :], lon[recent][None, :])
                k = int(np.argmin(dmat))
                a, b = divmod(k, dmat.shape[1])
                dmin = float(dmat[a, b])
                if dmin <= p.cont_dist_km:
                    cands.append((dmin, -ev["t_last"], ev, int(recent[b])))
        cands.sort(key=lambda x: (x[0], x[1]))
        attached = None
        recs = []
        for dmin, _, ev, nearest_obs in cands:
            rec = {
                "from_cluster_id": cluster_of[nearest_obs],
                "to_cluster_id": c["cluster_id"],
                "event_id": None,
                "_ev": ev,
                "distance_km": round(dmin, 3),
                "gap_h": round((c["t_start"] - t[nearest_obs]) / 60.0, 2),
                "status": None,
                "reason": None,
            }
            if attached is None:
                eb = ev_box[ev["_k"]]
                u = (min(eb[0], c_box[0]), max(eb[1], c_box[1]), min(eb[2], c_box[2]), max(eb[3], c_box[3]))
                if float(haversine_km(u[0], u[2], u[1], u[3])) > p.max_event_extent_km:
                    rec["status"] = "belirsiz"
                    rec["reason"] = L(f"eklenirse olay yayılımı {p.max_event_extent_km:g} km'yi aşardı", f"adding it would exceed the {p.max_event_extent_km:g} km event extent")
                    ev["ambiguous"].append(rec)
                else:
                    attached = ev
                    rec["status"] = "kabul"
                    rec["reason"] = L("en yakın aday olay", "nearest candidate event") if len(cands) > 1 else L("tek aday olay", "only candidate event")
                    ev["members"].extend(m.tolist())
                    ev["clusters"].append(c)
                    ev["t_last"] = max(ev["t_last"], c["t_end"])
                    ev_box[ev["_k"]] = u
                    ev_tlast[ev["_k"]] = ev["t_last"]
            else:
                rec["status"] = "belirsiz"
                rec["reason"] = L("küme birden fazla olayın devamı olabilir; olaylar birleştirilmedi", "the cluster could continue more than one event; events were not merged")
                ev["ambiguous"].append(rec)
            recs.append(rec)
        if attached is None:
            attached = {"members": m.tolist(), "clusters": [c], "t_last": c["t_end"], "ambiguous": [], "_k": n_ev}
            events.append(attached)
            ev_box[n_ev] = c_box
            ev_tlast[n_ev] = c["t_end"]
            n_ev += 1
            for rec in recs:
                rec["_new_ev"] = attached
        for rec in recs:
            if rec["status"] == "belirsiz" and rec["_ev"] is not attached:
                attached["ambiguous"].append(rec)
        continuity.extend(recs)

    event_of = np.empty(n, dtype=object)
    for ev in events:
        em = np.asarray(ev["members"])
        first = em[np.lexsort((ids[em], t[em]))][0]
        ev["event_id"] = "EV-" + _short_hash(ids[first])
        event_of[em] = ev["event_id"]
    cl2ev = {c["cluster_id"]: event_of[c["members"][0]] for c in clusters}
    for rec in continuity:
        rec["event_id"] = rec.pop("_ev")["event_id"]
        rec.pop("_new_ev", None)
        rec["to_event_id"] = cl2ev[rec["to_cluster_id"]]
    obs["event_id"] = event_of

    cl_records = []
    for c in clusters:
        m = c["members"]
        cl_records.append({
            "cluster_id": c["cluster_id"],
            "event_id": event_of[m[0]],
            "start_utc": _iso(obs["observed_at_utc"].iat[int(m[np.argmin(t[m])])]),
            "end_utc": _iso(obs["observed_at_utc"].iat[int(m[np.argmax(t[m])])]),
            "n_modis": int(is_modis[m].sum()),
            "n_viirs": int((~is_modis[m]).sum()),
            "extent_km": round(bbox_extent_km(lat[m], lon[m]), 2),
            "flags": c["flags"],
        })

    links_by_event = (pd.Series(event_of[mi[same_cl]]).value_counts().to_dict() if ci.size else {})
    pas = (obs.assign(_frp=obs["frp_mw"])
              .groupby(["event_id", "pass_id"], sort=False)
              .agg(satellite=("satellite", "first"), instrument=("instrument", "first"),
                   time_utc=("observed_at_utc", "min"), n_obs=("observation_id", "size"),
                   frp_max_mw=("_frp", "max"), frp_pass_sum_mw=("_frp", lambda s: s.sum(min_count=1)),
                   n_frp_missing=("_frp", lambda s: int(s.isna().sum())))
              .reset_index().sort_values("time_utc"))
    passes_by_event: dict[str, list] = {}
    for r in pas.itertuples(index=False):
        passes_by_event.setdefault(r.event_id, []).append({
            "pass_id": r.pass_id, "satellite": r.satellite, "instrument": r.instrument,
            "time_utc": _iso(r.time_utc), "n_obs": int(r.n_obs),
            "frp_max_mw": _r(r.frp_max_mw), "frp_pass_sum_mw": _r(r.frp_pass_sum_mw),
            "n_frp_missing": int(r.n_frp_missing),
        })
    day_str = obs["observed_at_utc"].dt.strftime("%Y-%m-%d").to_numpy()
    frp = obs["frp_mw"].to_numpy(float)
    matched = obs["cross_matched"].to_numpy()
    src = obs["source"].to_numpy()
    flag_arr = obs["match_flags"].to_numpy()

    ev_records = []
    for ev in events:
        em = np.asarray(ev["members"])
        em = em[np.argsort(t[em], kind="stable")]
        n_m = int(is_modis[em].sum())
        days = sorted(set(day_str[em]))
        ev_flags = sorted({f for c in ev["clusters"] for f in c["flags"]})
        n_amb = len({(r["from_cluster_id"], r["to_cluster_id"]) for r in ev["ambiguous"]})
        if n_amb:
            ev_flags.append(L(f"{n_amb} belirsiz devam ilişkisi", f"{n_amb} ambiguous continuity link(s)"))
        if (flag_arr[em] != "").any():
            ev_flags.append(L("birden fazla MODIS geçişiyle eşleşen VIIRS gözlemi var", "contains VIIRS observations matched to more than one MODIS overpass"))
        n_matched = int(matched[em].sum())
        fr = frp[em]
        ev_records.append({
            "event_id": ev["event_id"],
            "first_obs_utc": _iso(obs["observed_at_utc"].iat[int(em[0])]),
            "last_obs_utc": _iso(obs["observed_at_utc"].iat[int(em[-1])]),
            "days": days,
            "n_days": len(days),
            "n_obs": int(len(em)),
            "n_modis": n_m,
            "n_viirs": int(len(em) - n_m),
            "satellites": sorted(set(sat[em])),
            "sources": sorted(set(src[em])),
            "n_cross_matched_obs": n_matched,
            "n_unmatched_obs": int(len(em) - n_matched),
            "n_cross_links": int(links_by_event.get(ev["event_id"], 0)),
            "n_clusters": len(ev["clusters"]),
            "cluster_ids": [c["cluster_id"] for c in ev["clusters"]],
            "centroid_lat": round(float(lat[em].mean()), 5),
            "centroid_lon": round(float(lon[em].mean()), 5),
            "bbox": [round(float(lon[em].min()), 5), round(float(lat[em].min()), 5),
                     round(float(lon[em].max()), 5), round(float(lat[em].max()), 5)],
            "extent_km": round(bbox_extent_km(lat[em], lon[em]), 2),
            "max_single_frp_mw": _r(np.nanmax(fr)) if np.isfinite(fr).any() else None,
            "passes": passes_by_event.get(ev["event_id"], []),
            "flags": ev_flags,
            "ambiguous_links": [{k: v for k, v in r.items()} for r in ev["ambiguous"]],
            "explanation": _explain(ev, p, n_m, len(em) - n_m, n_matched, len(em)),
        })
    ev_records.sort(key=lambda e: e["first_obs_utc"])

    return {"observations": obs, "links": links, "clusters": cl_records,
            "continuity": continuity, "events": ev_records}


def _explain(ev, p: MatchParams, n_m: int, n_v: int, n_matched: int, n_tot: int) -> str:
    nc = len(ev["clusters"])
    sens = [s for s, k in (("MODIS", n_m), ("VIIRS", n_v)) if k]
    pix = L(" (piksel boyutu büyükse eşik piksel boyutuna göre genişletildi)", " (widened to the pixel size for large pixels)") if p.use_pixel_size else ""
    parts = [
        L(f"{nc} yakın-zamanlı kümeden oluşan aday olay ({' + '.join(sens)}).",
          f"Candidate event made of {nc} near-simultaneous cluster(s) ({' + '.join(sens)})."),
        L(f"Küme içi bağlantılar: aynı geçişte ≤{p.same_pass_adj_km:g} km komşuluk ve sensörler arası "
          f"≤{p.cross_dist_km:g} km / ≤{p.cross_dt_min:g} dk eşleşme{pix}.",
          f"Links within clusters: same-pass adjacency ≤{p.same_pass_adj_km:g} km and cross-sensor matches "
          f"≤{p.cross_dist_km:g} km / ≤{p.cross_dt_min:g} min{pix}."),
    ]
    if nc > 1:
        parts.append(L(f"Kümeler arası devam: en yakın gözlemler ≤{p.cont_dist_km:g} km ve boşluk ≤{p.cont_gap_h:g} sa; "
                       f"olay yayılımı ≤{p.max_event_extent_km:g} km ile sınırlı.",
                       f"Continuity between clusters: nearest observations ≤{p.cont_dist_km:g} km and gap ≤{p.cont_gap_h:g} h; "
                       f"event extent limited to ≤{p.max_event_extent_km:g} km."))
    parts.append(L(f"{n_matched}/{n_tot} gözlemin karşı sensörde yakın-zamanlı eşi var.", f"{n_matched}/{n_tot} observations have a near-simultaneous match in the other sensor."))
    if ev["ambiguous"]:
        parts.append(L("Bazı devam ilişkileri belirsiz bırakıldı (olaylar birleştirilmedi).", "Some continuity links were left ambiguous (events were not merged)."))
    return " ".join(parts)
