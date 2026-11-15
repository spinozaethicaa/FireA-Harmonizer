from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import PoissonRegressor
from sklearn.metrics import mean_poisson_deviance, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firea.features import CONTEXT_FEATURES, MAX_TOKENS, TOKEN_FEATURES
from firea.model import FireA, count_params, nb_nll, nb_pmf
from firea.regions import REGIONS

DATA = Path(__file__).resolve().parent / "data" / "dataset.npz"
MODELS = Path(__file__).resolve().parent.parent / "models"
SEED = 7
ARCH = {"d": 192, "layers": 4, "heads": 6, "dropout": 0.1}
REGION_CAP = 50_000
MODEL_NAME = "FireA-v1"
torch.set_num_threads(8)


class Data:
    def __init__(self, path=DATA):
        z = np.load(path, allow_pickle=False)
        self.ctx_raw, self.y, self.obs, self.meta = z["ctx"], z["y"], z["obs"], z["meta"]
        self.ri, self.day_id, self.tok, self.off = z["ri"], z["day_id"], z["tok"], z["off"]
        self.dates, self.day_ri = z["dates"], z["day_ri"]
        self.lengths = np.diff(self.off)

    def subset(self, idx):
        idx = np.asarray(idx)
        lens = self.lengths[idx]
        self.tok = np.concatenate([self.tok[self.off[i]:self.off[i + 1]] for i in idx]) if len(idx) else self.tok[:0]
        self.off = np.concatenate([[0], np.cumsum(lens)])
        self.ctx_raw, self.y, self.obs, self.meta = self.ctx_raw[idx], self.y[idx], self.obs[idx], self.meta[idx]
        self.ri, self.day_id = self.ri[idx], self.day_id[idx]
        self.lengths = lens
        return np.arange(len(idx))

    def normalize(self, idx):
        x = self.ctx_raw[idx]
        self.mean = np.nanmean(x, axis=0)
        self.std = np.nanstd(x, axis=0) + 1e-6
        self.ctx = np.nan_to_num((self.ctx_raw - self.mean) / self.std, nan=0.0).clip(-8, 8).astype(np.float32)

    def batch(self, idx):
        L = max(1, int(self.lengths[idx].max()))
        tokens = np.zeros((len(idx), L, self.tok.shape[1]), np.float32)
        mask = np.ones((len(idx), L), bool)
        for b, i in enumerate(idx):
            a, e = self.off[i], self.off[i + 1]
            tokens[b, : e - a] = self.tok[a:e]
            mask[b, : e - a] = False
        mask[:, 0] = False
        return (torch.from_numpy(tokens), torch.from_numpy(mask), torch.from_numpy(self.ctx[idx]),
                torch.from_numpy(self.y[idx].astype(np.float32)))


def batches(data: Data, idx, bs, shuffle, rng):
    idx = np.array(idx)
    if shuffle:
        idx = idx[rng.permutation(len(idx))]
    blocks = []
    for s in range(0, len(idx), bs * 50):
        blk = idx[s: s + bs * 50]
        blk = blk[np.argsort(data.lengths[blk], kind="stable")]
        blocks.extend(blk[i: i + bs] for i in range(0, len(blk), bs))
    if shuffle:
        blocks = [blocks[i] for i in rng.permutation(len(blocks))]
    return blocks


def predict(model, data, idx, bs=1024):
    model.eval()
    mus, als = [], []
    with torch.no_grad():
        for b in batches(data, idx, bs, False, None):
            t, m, c, _ = data.batch(b)
            mu, al = model(t, m, c)
            mus.append((b, mu.numpy(), al.numpy()))
    order = {int(i): k for k, i in enumerate(idx)}
    mu = np.zeros(len(idx))
    al = np.zeros(len(idx))
    for b, m_, a_ in mus:
        for j, i in enumerate(b):
            mu[order[int(i)]] = m_[j]
            al[order[int(i)]] = a_[j]
    return mu, al


def train_model(data, tr, va, epochs_max=24, fixed_epochs=None, bs=512, lr=1e-3, log=print):
    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)
    model = FireA(len(TOKEN_FEATURES), len(CONTEXT_FEATURES), **ARCH)
    with torch.no_grad():
        m0 = max(float(data.y[tr].mean()), 0.05)
        model.head[-1].bias[0] = math.log(math.expm1(m0))
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    n_ep = fixed_epochs or epochs_max
    steps = n_ep * math.ceil(len(tr) / bs)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=lr, total_steps=steps, pct_start=0.1)
    best = (float("inf"), None, 0)
    history = []
    patience = 4
    for ep in range(1, n_ep + 1):
        model.train()
        t0, tot, n = time.time(), 0.0, 0
        for b in batches(data, tr, bs, True, rng):
            t, m, c, y = data.batch(b)
            mu, al = model(t, m, c)
            loss = nb_nll(y, mu, al)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            tot += loss.item() * len(b)
            n += len(b)
        rec = {"epoch": ep, "train_nll": tot / n, "sec": round(time.time() - t0, 1)}
        if va is not None and len(va):
            mu, al = predict(model, data, va)
            rec["val_nll"] = float(nb_nll(torch.tensor(data.y[va], dtype=torch.float64),
                                          torch.tensor(mu, dtype=torch.float64), torch.tensor(al, dtype=torch.float64)))
            if rec["val_nll"] < best[0] - 1e-4:
                best = (rec["val_nll"], {k: v.clone() for k, v in model.state_dict().items()}, ep)
        history.append(rec)
        log(json.dumps(rec), flush=True)
        if fixed_epochs is None and ep - best[2] >= patience:
            break
    if best[1] is not None and fixed_epochs is None:
        model.load_state_dict(best[1])
    return model, best[2] or n_ep, history


def nb_interval(mu, alpha, q_lo=0.1, q_hi=0.9, k_max=None):
    k_max = k_max or int(max(20, np.percentile(mu, 99.9) * 6 + 20))
    k_max = min(k_max, 2000)
    lo, hi, p0 = np.zeros(len(mu)), np.zeros(len(mu)), np.zeros(len(mu))
    for s in range(0, len(mu), 4096):
        pmf = nb_pmf(k_max, mu[s:s + 4096], alpha[s:s + 4096]).numpy()
        cdf = pmf.cumsum(1)
        lo[s:s + 4096] = (cdf < q_lo).sum(1)
        hi[s:s + 4096] = (cdf < q_hi).sum(1)
        p0[s:s + 4096] = pmf[:, 0]
    return lo, hi, p0


def poisson_interval(mu, q_lo=0.1, q_hi=0.9):
    from scipy.stats import poisson
    return poisson.ppf(q_lo, mu), poisson.ppf(q_hi, mu), np.exp(-mu)


def metrics(y, mu, lo, hi, p0, day_id):
    det = (y >= 1).astype(int)
    p_det = 1 - p0
    out = {
        "n_clusters": int(len(y)),
        "poisson_deviance": float(mean_poisson_deviance(y, np.maximum(mu, 1e-6))),
        "mae": float(np.mean(np.abs(y - mu))),
        "rmse": float(np.sqrt(np.mean((y - mu) ** 2))),
        "auc_detection": float(roc_auc_score(det, p_det)) if 0 < det.sum() < len(det) else None,
        "brier_detection": float(np.mean((p_det - det) ** 2)),
        "interval80_coverage": float(np.mean((y >= lo) & (y <= hi))),
        "observed_detection_rate": float(det.mean()),
        "predicted_detection_rate": float(p_det.mean()),
    }
    days = np.unique(day_id)
    ys = np.array([y[day_id == d].sum() for d in days])
    ms = np.array([mu[day_id == d].sum() for d in days])
    keep = ys >= 10
    out["region_day_total_ratio_median"] = float(np.median(ms[keep] / ys[keep])) if keep.any() else None
    out["region_day_total_abs_pct_err_median"] = float(np.median(np.abs(ms[keep] - ys[keep]) / ys[keep]) * 100) if keep.any() else None
    out["n_region_days_eval"] = int(keep.sum())
    out["total_observed"] = int(y.sum())
    out["total_predicted"] = float(mu.sum())
    return out


def baselines(data, tr, te):
    res = {}
    i_logn = CONTEXT_FEATURES.index("log_n")
    glm = PoissonRegressor(alpha=0.0, max_iter=300).fit(data.ctx_raw[tr][:, [i_logn]], data.y[tr])
    mu = glm.predict(data.ctx_raw[te][:, [i_logn]])
    lo, hi, p0 = poisson_interval(mu)
    res["power_law"] = metrics(data.y[te], mu, lo, hi, p0, data.day_id[te])
    res["power_law"]["coef"] = {"a": float(glm.intercept_), "b": float(glm.coef_[0])}
    gbm = HistGradientBoostingRegressor(loss="poisson", max_iter=400, learning_rate=0.06, max_leaf_nodes=31,
                                        random_state=SEED, early_stopping=True, validation_fraction=0.15)
    gbm.fit(np.nan_to_num(data.ctx_raw[tr], nan=-1), data.y[tr])
    mu = gbm.predict(np.nan_to_num(data.ctx_raw[te], nan=-1))
    lo, hi, p0 = poisson_interval(mu)
    res["gbm"] = metrics(data.y[te], mu, lo, hi, p0, data.day_id[te])
    return res


def main(epochs: int = 5, eval_log: str | None = None):
    t_start = time.time()
    data = Data()
    test_ri = {i for i, r in enumerate(REGIONS) if r.split == "test"}
    obs_idx = np.where(data.obs & (data.lengths > 0))[0]
    capped = []
    for ri in np.unique(data.ri[obs_idx]):
        sel = obs_idx[data.ri[obs_idx] == ri]
        if len(sel) > REGION_CAP:
            sel = np.sort(np.random.default_rng(SEED + int(ri)).choice(sel, REGION_CAP, replace=False))
        capped.append(sel)
    obs_idx = np.sort(np.concatenate(capped))
    is_test = np.isin(data.ri[obs_idx], list(test_ri))
    pool, te = obs_idx[~is_test], obs_idx[is_test]
    rng = np.random.default_rng(SEED)
    pool_days = np.unique(data.day_id[pool])
    va_days = set(rng.choice(pool_days, size=max(1, int(0.15 * len(pool_days))), replace=False).tolist())
    in_va = np.isin(data.day_id[pool], list(va_days))
    tr, va = pool[~in_va], pool[in_va]
    print(f"Bölge sınırı {REGION_CAP:,} sonrası gözlenebilir küme: {len(obs_idx):,} (eğitim {len(tr):,} · doğrulama {len(va):,} · test {len(te):,})")
    print(f"Ortalama Aqua tespiti/küme: eğitim {data.y[tr].mean():.3f} · test {data.y[te].mean():.3f}")

    if eval_log:
        # Reuse the evaluation already logged by an earlier run on the same dataset and split.
        hist, base, m_fire, per_region = [], {}, None, {}
        for line in Path(eval_log).read_text(encoding="utf-8").splitlines():
            if line.startswith("{\"epoch\""):
                hist.append(json.loads(line))
            elif line.startswith("TEST FireA:"):
                m_fire = json.loads(line.split(":", 1)[1])
            elif line.startswith("TEST "):
                k, v = line[5:].split(":", 1)
                base[k] = json.loads(v)
        assert m_fire is not None and hist, "eval log incomplete"
        best_ep = max(h["epoch"] for h in hist)
    else:
        data.normalize(tr)
        model, best_ep, hist = train_model(data, tr, va, epochs_max=epochs)
        mu, al = predict(model, data, te)
        lo, hi, p0 = nb_interval(mu, al)
        m_fire = metrics(data.y[te], mu, lo, hi, p0, data.day_id[te])
        per_region = {}
        for ri in sorted(test_ri):
            sel = data.ri[te] == ri
            if sel.sum() > 20:
                per_region[REGIONS[ri].key] = metrics(data.y[te][sel], mu[sel], lo[sel], hi[sel], p0[sel], data.day_id[te][sel])
        base = baselines(data, tr, te)
        print("TEST FireA:", json.dumps(m_fire))
        for k, v in base.items():
            print(f"TEST {k}:", json.dumps(v))

    # East Med is left out of the deployed model too, so validations in that region stay independent.
    hold = {i for i, r in enumerate(REGIONS) if r.key == "east_med"}
    all_idx = obs_idx[~np.isin(data.ri[obs_idx], list(hold))]
    if eval_log:
        all_idx = data.subset(all_idx)
    data.normalize(all_idx)
    final, _, hist_final = train_model(data, all_idx, None, fixed_epochs=best_ep)
    n_params = count_params(final)
    name = MODEL_NAME
    out = MODELS / name
    out.mkdir(parents=True, exist_ok=True)
    torch.save(final.state_dict(), out / "weights.pt")

    ims, imt = CONTEXT_FEATURES.index("m_scan"), CONTEXT_FEATURES.index("m_track")
    g = data.ctx_raw[all_idx][:, [ims, imt]]
    g = g[np.isfinite(g).all(1)]
    g = g[np.argsort(g[:, 0])]
    geo = [g[int((k + 0.5) / 16 * len(g))].tolist() for k in range(16)]

    config = {
        "name": name, "n_params": n_params, "arch": final.config,
        "token_features": TOKEN_FEATURES, "context_features": CONTEXT_FEATURES, "max_tokens": MAX_TOKENS,
        "ctx_mean": data.mean.tolist(), "ctx_std": data.std.tolist(),
        "geometry_samples": geo, "best_epoch": best_ep,
        "trained_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": "Aqua-MODIS tespit sayısı / VIIRS geçiş kümesi (|Δt| ≤ 60 dk)",
        "regions": [{"key": r.key, "name": r.name, "biome": r.biome, "split": r.split, "bbox": r.bbox} for r in REGIONS],
    }
    (out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    metrics_out = {
        "evaluation": "Test bölgeleri eğitime girmedi (coğrafi dışarıda bırakma). Değerler değerlendirme modelinden.",
        "split_sizes": {"train": int(len(tr)), "val": int(len(va)), "test": int(len(te)), "final": int(len(all_idx))},
        "final_excludes": ["east_med"],
        "region_cap": REGION_CAP,
        "test": {"FireA": m_fire, **base},
        "test_per_region_FireA": per_region,
        "history_eval": hist, "history_final": hist_final,
        "train_minutes": round((time.time() - t_start) / 60, 1),
    }
    (out / "metrics.json").write_text(json.dumps(metrics_out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Kaydedildi: {out} · {n_params:,} parametre · {metrics_out['train_minutes']} dk")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--eval-log", default=None)
    a = ap.parse_args()
    main(a.epochs, a.eval_log)
