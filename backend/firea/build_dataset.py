from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firea.features import (CONTEXT_FEATURES, assign_passes, cluster_table, label_clusters, prep,
                            token_arrays, viirs_clusters)
from firea.regions import REGIONS, all_days

DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_PATH = DATA_DIR / "dataset.npz"


def load_day(reg, d):
    from firea.download import make_client
    client = make_client()
    frames = {}
    for src in ("MODIS_SP", "VIIRS_SNPP_SP", "VIIRS_NOAA20_SP"):
        out = client.fetch_area(src, reg.bbox, d, d)
        ch = out.chunks[0]
        if ch.status == "error":
            frames[src] = None
        else:
            frames[src] = pd.concat(out.frames, ignore_index=True) if out.frames else pd.DataFrame()
    return frames


def process(args):
    ri, di = args
    reg = REGIONS[ri]
    d = all_days(reg)[di]
    frames = load_day(reg, d)
    if frames["MODIS_SP"] is None:
        return None
    m = prep(frames["MODIS_SP"])
    vs = [prep(frames[s]) for s in ("VIIRS_SNPP_SP", "VIIRS_NOAA20_SP") if frames[s] is not None]
    vs = [x for x in vs if not x.empty]
    if not vs:
        return None
    v = pd.concat(vs, ignore_index=True)
    v = v[v["instr"] == "VIIRS"].reset_index(drop=True)
    if v.empty:
        return None
    aqua = m[(m["instr"] == "MODIS") & (m["sat"] == "Aqua")].reset_index(drop=True)
    v["pass"] = assign_passes(v)
    if not aqua.empty:
        aqua["pass"] = assign_passes(aqua)
    v["cl"] = viirs_clusters(v)
    tab = cluster_table(v)
    lab = label_clusters(v, tab, aqua)
    tab = tab.join(lab)
    tab["abs_dt_h"] = tab["dt_h"].abs()
    tok, off = token_arrays(v, tab)
    stats = {"region": reg.key, "date": d.isoformat(), "n_viirs": int(len(v)), "n_aqua": int(len(aqua)),
             "n_clusters": int(len(tab)), "n_observable": int(tab["observable"].sum()),
             "y_sum": int(tab.loc[tab["observable"], "y"].sum())}
    return {
        "ctx": tab[CONTEXT_FEATURES].to_numpy(np.float32),
        "y": tab["y"].to_numpy(np.int64),
        "obs": tab["observable"].to_numpy(bool),
        "meta": tab[["lat", "lon", "t", "n", "frp_sum"]].to_numpy(np.float64),
        "tok": tok, "off": off, "ri": ri, "date": d.isoformat(), "stats": stats,
    }


def main(workers: int = 6):
    jobs = [(ri, di) for ri, reg in enumerate(REGIONS) for di in range(len(all_days(reg)))]
    t0 = time.time()
    parts = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for k, res in enumerate(ex.map(process, jobs, chunksize=2)):
            if res is not None:
                parts.append(res)
            if (k + 1) % 40 == 0:
                print(f"{k + 1}/{len(jobs)} bölge-gün işlendi · {time.time() - t0:.0f} sn", flush=True)
    ctx = np.concatenate([p["ctx"] for p in parts])
    y = np.concatenate([p["y"] for p in parts])
    obs = np.concatenate([p["obs"] for p in parts])
    meta = np.concatenate([p["meta"] for p in parts])
    ri = np.concatenate([np.full(len(p["y"]), p["ri"], np.int16) for p in parts])
    day_id = np.concatenate([np.full(len(p["y"]), k, np.int32) for k, p in enumerate(parts)])
    tok = np.concatenate([p["tok"] for p in parts])
    off_list, base = [], 0
    for p in parts:
        off_list.append(p["off"][:-1] + base)
        base += p["off"][-1]
    off = np.concatenate(off_list + [np.array([base])])
    np.savez_compressed(OUT_PATH, ctx=ctx, y=y, obs=obs, meta=meta, ri=ri, day_id=day_id, tok=tok, off=off,
                        dates=np.array([p["date"] for p in parts]), day_ri=np.array([p["ri"] for p in parts]))
    stats = [p["stats"] for p in parts]
    (DATA_DIR / "dataset_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Kaydedildi: {OUT_PATH.name} · {len(y):,} küme · {obs.sum():,} gözlenebilir · {len(tok):,} piksel · "
          f"{time.time() - t0:.0f} sn")
    df = pd.DataFrame(stats)
    df["reg"] = df["region"]
    print(df.groupby("reg")[["n_viirs", "n_aqua", "n_clusters", "n_observable", "y_sum"]].sum().to_string())


if __name__ == "__main__":
    main()
