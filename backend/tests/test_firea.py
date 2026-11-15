from __future__ import annotations

import numpy as np
import pandas as pd
import torch

from firea.features import (CONTEXT_FEATURES, MAX_TOKENS, TOKEN_FEATURES, assign_passes, cluster_table,
                            label_clusters, prep, token_arrays, viirs_clusters)
from firea.model import FireA, nb_nll, nb_pmf

T0 = 27_000_000.0


def vrow(lat, lon, t, sat="SNPP", frp=5.0):
    return {"instr": "VIIRS", "sat": sat, "t": t, "lat": lat, "lon": lon, "frp": frp, "scan": 0.4, "track": 0.4,
            "b4": 340.0, "b5": 295.0, "conf": "n", "day": 1, "type": 0}


def arow(lat, lon, t, scan=1.2):
    return {"instr": "MODIS", "sat": "Aqua", "t": t, "lat": lat, "lon": lon, "frp": 20.0, "scan": scan, "track": 1.1,
            "b4": 330.0, "b5": 300.0, "conf": "80", "day": 1, "type": 0}


def build(vrows, arows):
    v = pd.DataFrame(vrows)
    a = pd.DataFrame(arows)
    v["pass"] = assign_passes(v)
    a["pass"] = assign_passes(a)
    v["cl"] = viirs_clusters(v)
    tab = cluster_table(v)
    return v, a, tab, label_clusters(v, tab, a)


def test_prep_parses_time_and_drops_static_sources():
    raw = pd.DataFrame({
        "latitude": ["10", "10.1", "10.2"], "longitude": ["20", "20", "20"], "bright_ti4": ["330", "331", "332"],
        "scan": ["0.4"] * 3, "track": ["0.4"] * 3, "acq_date": ["2021-08-15"] * 3, "acq_time": ["5", "631", "1205"],
        "satellite": ["N", "N20", "N"], "instrument": ["VIIRS"] * 3, "confidence": ["n", "h", "l"],
        "version": ["2"] * 3, "bright_ti5": ["295"] * 3, "frp": ["1.5", "", "3"], "daynight": ["N", "D", "D"],
        "type": ["0", "0", "2"],
    })
    p = prep(raw)
    assert len(p) == 2
    base = (pd.Timestamp("2021-08-15", tz="UTC") - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(minutes=1)
    assert list(p["t"] - base) == [5.0, 6 * 60 + 31.0]
    assert list(p["sat"]) == ["SNPP", "NOAA20"]
    assert np.isnan(p["frp"].iloc[1])


def test_labels_counts_and_observability_without_leakage():
    vrows = (
        [vrow(10.0 + 0.003 * k, 20.0, T0) for k in range(6)]
        + [vrow(10.5, 20.0, T0)]
        + [vrow(40.0, 60.0, T0)]
        + [vrow(-20.0, 20.0, T0)]
    )
    arows = [arow(10.004, 20.0, T0 + 5), arow(10.010, 20.002, T0 + 5),
             arow(11.4, 20.0, T0 + 5),
             arow(-20.0, 20.001, T0 + 5)]
    v, a, tab, lab = build(vrows, arows)
    cid = {round(r.lat, 1): i for i, r in tab.iterrows()}
    A, B, C, D = cid[10.0], cid[10.5], cid[40.0], cid[-20.0]
    assert lab.at[A, "observable"] and lab.at[A, "y"] == 2
    assert lab.at[B, "observable"] and lab.at[B, "y"] == 0
    assert not lab.at[C, "observable"]
    assert not lab.at[D, "observable"]
    assert abs(lab.at[B, "dt_h"] - 5 / 60) < 1e-6
    assert 1.1 < lab.at[B, "m_scan"] < 1.3


def test_pair_requires_close_time():
    vrows = [vrow(10.0, 20.0, T0), vrow(10.5, 20.0, T0)]
    arows = [arow(10.0, 20.0, T0 + 120), arow(10.5, 20.0, T0 + 120)]
    _, _, tab, lab = build(vrows, arows)
    assert not lab["observable"].any()


def test_large_components_are_tiled():
    vrows = [vrow(10.0, 20.0 + k * 0.0082, T0) for k in range(45)]
    v = pd.DataFrame(vrows)
    v["pass"] = assign_passes(v)
    v["cl"] = viirs_clusters(v)
    assert v["cl"].nunique() >= 5


def test_token_arrays_cap_and_order():
    vrows = [vrow(10.0 + 0.001 * k, 20.0, T0, frp=float(k)) for k in range(80)]
    v = pd.DataFrame(vrows)
    v["pass"] = assign_passes(v)
    v["cl"] = viirs_clusters(v)
    tab = cluster_table(v)
    tok, off = token_arrays(v, tab)
    assert tok.shape[1] == len(TOKEN_FEATURES)
    assert (np.diff(off) <= MAX_TOKENS).all()
    assert tab["n"].sum() == 80


def test_model_forward_and_nb():
    m = FireA(len(TOKEN_FEATURES), len(CONTEXT_FEATURES), d=32, layers=1, heads=2)
    tok = torch.randn(3, 5, len(TOKEN_FEATURES))
    mask = torch.zeros(3, 5, dtype=torch.bool)
    mask[1, 3:] = True
    mu, al = m(tok, mask, torch.randn(3, len(CONTEXT_FEATURES)))
    assert mu.shape == (3,) and (mu > 0).all() and (al > 0).all()
    loss = nb_nll(torch.tensor([0.0, 2.0, 5.0]), mu, al)
    loss.backward()
    pmf = nb_pmf(200, np.array([3.0]), np.array([0.5]))
    assert abs(float(pmf.sum()) - 1.0) < 1e-6
    assert abs(float((pmf * torch.arange(201)).sum()) - 3.0) < 1e-3


def test_virtual_modis_end_to_end_if_model_present():
    import pytest
    from app.firea_infer import _find_model_dir, run_virtual_modis
    from app.normalize import merge_observations, normalize_records
    from app.sample_data import build_sample_frames
    if _find_model_dir() is None:
        pytest.skip("Eğitilmiş FireA modeli yok")
    obs = None
    for raw in build_sample_frames():
        n, _ = normalize_records(raw, origin="sample")
        obs, _ = merge_observations(obs, n)
    res = run_virtual_modis(obs, "2025-08-10", "2025-08-13")
    assert res["available"] and res["n_clusters"] > 0
    for c in res["clusters"]:
        assert 0.0 <= c["p_detect"] <= 1.0
        assert c["modis_like_mean"] >= 0.0
        assert c["modis_like_lo"] <= c["modis_like_hi"]
    for d in res["daily"]:
        assert d["virtual_lo"] <= d["virtual_hi"]
    big = max(res["clusters"], key=lambda c: c["n_viirs"])
    single = [c for c in res["clusters"] if c["n_viirs"] == 1]
    if single:
        assert big["modis_like_mean"] > min(c["modis_like_mean"] for c in single)
