from __future__ import annotations

import threading
from datetime import date, timedelta

import pandas as pd
import pytest

from app import history
from app.firms import ChunkResult, FetchOutcome
from app.history import run_climatology
from app.matching import MatchingCancelled

HDR_M = ["latitude", "longitude", "brightness", "scan", "track", "acq_date", "acq_time", "satellite", "instrument",
         "confidence", "version", "bright_t31", "frp", "daynight", "type"]
HDR_V = ["latitude", "longitude", "bright_ti4", "scan", "track", "acq_date", "acq_time", "satellite", "instrument",
         "confidence", "version", "bright_ti5", "frp", "daynight", "type"]


def n_fires(d: date) -> int:
    if d.year == 2021 and d.month == 8:
        return 200
    return 20 if d.month in (7, 8, 9) else 2


class StubClient:
    def __init__(self, fail=None, drift_from=None):
        self.fail = fail or set()
        self.drift_from = drift_from

    def availability(self, sensor="ALL", use_cache=True):
        return [{"data_id": "MODIS_SP", "min_date": "2000-11-01", "max_date": "2030-12-31"},
                {"data_id": "VIIRS_SNPP_SP", "min_date": "2012-01-20", "max_date": "2030-12-31"},
                {"data_id": "VIIRS_NOAA20_SP", "min_date": "2018-04-01", "max_date": "2030-12-31"},
                {"data_id": "VIIRS_NOAA21_SP", "min_date": "2023-03-01", "max_date": "2030-12-31"}]

    def plan_area(self, src, bbox, s, e, availability=None):
        return [], [(s, (e - s).days + 1)]

    def fetch_area(self, src, bbox, s, e, availability=None, on_progress=None, should_cancel=None):
        n_days = (e - s).days + 1
        if (src, s.year) in self.fail:
            ch = ChunkResult(src, s, n_days, "error", error={"kind": "http", "message": "x", "status": 500})
            return FetchOutcome(frames=[], chunks=[ch])
        rows = []
        d = s
        while d <= e:
            if d.day == 15:
                k_max = n_fires(d) * (1 if src.startswith("MODIS") else 5)
                for k in range(k_max):
                    if src.startswith("MODIS"):
                        t_m = "1200" if self.drift_from and d.year >= self.drift_from else "1050"
                        rows.append([f"{36.8 + k * 0.01:.4f}", "31.4", "330", "1.1", "1.0", d.isoformat(), t_m, "A",
                                     "MODIS", "80", "6.1", "300", "20", "D", "0"])
                    else:
                        rows.append([f"{36.8 + k * 0.002:.5f}", "31.40", "340", "0.4", "0.4", d.isoformat(), "1040", "N20" if "NOAA20" in src else "N21" if "NOAA21" in src else "N",
                                     "VIIRS", "n", "2", "295", "5", "D", "0"])
            d += timedelta(days=1)
        df = pd.DataFrame(rows, columns=HDR_M if src.startswith("MODIS") else HDR_V)
        df["_source"] = src
        ch = ChunkResult(src, s, n_days, "ok" if len(df) else "empty", rows=len(df))
        if on_progress:
            on_progress(src, ch, None)
        return FetchOutcome(frames=[df] if len(df) else [], chunks=[ch])


class StubJob:
    def __init__(self, cancel_after=None):
        self.total = self.done = self.rows = 0
        self.message = ""
        self.lock = threading.Lock()
        self.calls = 0
        self.cancel_after = cancel_after

    def set_phase(self, phase, msg):
        self.message = msg

    def should_cancel(self):
        self.calls += 1
        return self.cancel_after is not None and self.calls > self.cancel_after


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    history._cache.clear()
    monkeypatch.setattr(history, "MAX_MODEL_CANDIDATES", 1)
    yield
    history._cache.clear()


def cell(res, y, m):
    return next(r for r in res["months"] if r["year"] == y and r["month"] == m)


def test_climatology_calendar_season_and_anomaly():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    assert res["first_year"] == 2003
    a08 = cell(res, 2021, 8)
    assert a08["aqua"] == 200 and a08["viirs"] == 1000
    assert a08["combined_source"] == "Aqua-MODIS" and a08["combined"] == 200 and a08["z_aqua"] >= 2
    assert a08["harm"] is not None and a08["harm_lo"] <= a08["harm"] <= a08["harm_hi"]
    assert cell(res, 2005, 8)["combined_source"] == "Aqua-MODIS" and cell(res, 2005, 8)["viirs"] is None
    top3 = {s["month"] for s in sorted(res["season"], key=lambda s: -(s["median"] or 0))[:3]}
    assert top3 == {7, 8, 9} and {7, 8, 9} <= set(res["critical_months"])
    assert next(s for s in res["season"] if s["month"] == 8)["critical"]
    assert res["ratio_viirs_to_aqua_median"] == 5.0
    assert res["recent"] is not None and res["recent"]["n_years"] >= 3


def test_missing_coverage_is_none_not_zero():
    res = run_climatology(StubClient(fail={("MODIS_SP", 2015)}), (27, 35.5, 32.5, 38), StubJob())
    assert cell(res, 2015, 8)["aqua"] is None
    assert cell(res, 2015, 8)["combined"] is not None
    assert cell(res, 2015, 8)["combined_source"] == "harmonize VIIRS"
    assert cell(res, 2016, 8)["aqua"] == 20


def test_climatology_cache_and_cancel():
    res1 = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    res2 = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob(cancel_after=0))
    assert res2 is res1
    history._cache.clear()
    with pytest.raises(MatchingCancelled):
        run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob(cancel_after=2))


def test_robust_z_ignores_outlier():
    import numpy as np
    med, spread, z = history._robust(np.array([2, 3, 2, 4, 3, 500.0]), 3.0)
    assert med == 3.0 and abs(z) < 1


def test_report_pdf():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    did = c.post("/api/sample").json()["dataset_id"]
    key = c.post("/api/analyze", json={"dataset_id": did}).json()["analysis_key"]
    r = c.get("/api/report", params={"key": key})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF" and len(r.content) > 5000


def test_calibration_and_validation():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    cal = res["calibration"]
    assert cal["method"] == "seasonal" and len(cal["season_factor"]) == 12 and cal["drifted_years"] == []
    val = cal["validation"]
    assert val["method"] == "leave-one-year-out" and val["n_months"] > 100
    assert 0 <= val["coverage80_calibrated"] <= 1 and val["harm_month_err_pct"] is not None
    assert all("harm_raw_lo" not in r for r in res["months"])


def test_interval_widens_for_small_counts():
    lo, hi = history._interval(history.np.array([5.0, 500.0]), history.DEFAULT_SPREAD)
    assert lo[0] <= 5 <= hi[0] and lo[1] <= 500 <= hi[1]
    assert (hi[0] - lo[0]) / 5 > (hi[1] - lo[1]) / 500


def test_two_viirs_satellites_are_averaged_not_summed():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    c15, c21 = cell(res, 2015, 8), cell(res, 2021, 8)
    assert c15["harm_sats"] == ["S-NPP"] and c21["harm_sats"] == ["S-NPP", "NOAA-20"]
    assert c21["viirs"] == 1000
    assert c21["harm_raw"] < 1.5 * cell(res, 2017, 8)["harm_raw"] * 10


def test_aqua_orbit_drift_switches_combined_to_harmonized():
    res = run_climatology(StubClient(drift_from=2024), (27, 35.5, 32.5, 38), StubJob())
    cal = res["calibration"]
    assert 2024 in cal["drifted_years"] and 2023 not in cal["drifted_years"]
    assert cell(res, 2024, 8)["combined_source"] == "harmonize VIIRS" and cell(res, 2024, 8)["aqua_drift_min"] > 15
    assert cell(res, 2023, 8)["combined_source"] == "Aqua-MODIS"
    assert cal["validation"]["years"][1] <= 2023


def test_noaa21_is_cross_calibrated_and_averaged():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    assert cell(res, 2024, 8)["harm_sats"] == ["S-NPP", "NOAA-20", "NOAA-21"]
    assert abs(res["calibration"]["xcal_noaa21"] - 1.0) < 0.05
    assert res["calibration"]["viirs_sats"] == ["S-NPP", "NOAA-20", "NOAA-21"]


def test_frp_probability_and_grid():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    c = cell(res, 2021, 8)
    assert c["aqua_frp"] == 200 * 20 and c["harm_frp"] is not None and c["combined_frp"] == c["aqua_frp"]
    assert c["p_hi_combined"] == 1.0 and 0 <= c["p_hi_harm"] <= 1
    assert res["calibration_frp"]["validation"] is not None
    g = res["grid"]
    assert g["nx"] * g["ny"] > 1 and g["source"]["2021-08"] == "a"
    assert sum(v for _, v in g["count"]["2021-08"]) == 200


def test_size_correction_learned_and_cross_validated():
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    sc = res["calibration"]["size_correction"]
    assert sc["applied"] and sc["n_labels"] > 100 and len(sc["day"]) == 5 and len(sc["night"]) == 5
    val = res["calibration"]["validation"]
    assert val["season_month_err_pct"] is not None and val["multi_sat"]["n_months"] >= 24
    assert all("_cv" not in r and "_est" not in r for r in res["months"])
    assert cell(res, 2021, 8)["harm_fire"] is not None


def test_match_aqua_counts_only_nearby_detections_in_time_window():
    np = history.np
    arr = {"mu": np.array([1.0, 1.0]), "pix_t": np.array([1000.0, 1000.0, 1000.0]),
           "pix_lat": np.array([36.0, 36.001, 37.0]), "pix_lon": np.array([30.0, 30.0, 31.0]), "pix_cl": np.array([0, 0, 1])}
    y = history._match_aqua(arr, np.array([1010.0, 1012.0, 1500.0, 1011.0]), np.array([36.0005, 36.002, 37.0, 36.5]),
                            np.array([30.0, 30.001, 31.0, 30.5]), np.array([1.0, 1.0, 1.0, 1.0]), np.array([1.0, 1.0, 1.0, 1.0]))
    assert y.tolist() == [2.0, 0.0]


def test_size_factors_need_enough_labels():
    np = history.np
    few = {2015: [np.full(history.N_GRP, 1.0), np.full(history.N_GRP, 2.0)]}
    r, n = history._size_factors(few, [2015])
    assert n == 0.0 and np.allclose(r, 1.0)
    many = {2015: [np.full(history.N_GRP, 50.0), np.full(history.N_GRP, 100.0)]}
    r, n = history._size_factors(many, [2015])
    assert n == 50.0 * history.N_GRP and np.allclose(r, 0.5)


def test_model_choice_picks_lowest_validation_error(monkeypatch):
    from app import firea_infer
    names = firea_infer.list_models()
    if len(names) < 2:
        pytest.skip("needs two FireA models")
    monkeypatch.setattr(history, "MAX_MODEL_CANDIDATES", 2)
    res = run_climatology(StubClient(), (27, 35.5, 32.5, 38), StubJob())
    mc = res["model_choice"]
    assert len(mc["candidates"]) == 2 and mc["chosen"] in names[:2]
    best = min(mc["candidates"], key=lambda c: c["harm_mae"])
    assert mc["chosen"] == best["name"] and res["model"] == mc["chosen"]
