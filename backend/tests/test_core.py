from __future__ import annotations

import logging
from datetime import date

import httpx
import pandas as pd
import pytest

from app import config
from app.firms import FirmsClient, FirmsError, ResponseCache, split_date_range
from app.matching import MatchParams, haversine_km, run_matching
from app.normalize import QualityParams, apply_quality, merge_observations, normalize_records, parse_acq_time

FAKE_KEY = "TESTKEY0123456789abcdef0123456789"

VIIRS_HDR = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight"
MODIS_HDR = "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight"


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).astype(str)


def modis(lat, lon, d="2025-08-10", t="1050", sat="A", conf="80", frp="20.0"):
    return {"latitude": lat, "longitude": lon, "brightness": "330", "scan": "1.0", "track": "1.0", "acq_date": d,
            "acq_time": t, "satellite": sat, "instrument": "MODIS", "confidence": conf, "version": "6.1NRT",
            "bright_t31": "300", "frp": frp, "daynight": "D"}


def viirs(lat, lon, d="2025-08-10", t="1012", sat="N20", conf="n", frp="5.0"):
    return {"latitude": lat, "longitude": lon, "bright_ti4": "340", "scan": "0.4", "track": "0.4", "acq_date": d,
            "acq_time": t, "satellite": sat, "instrument": "VIIRS", "confidence": conf, "version": "2.0NRT",
            "bright_ti5": "295", "frp": frp, "daynight": "D"}


def norm(rows):
    df, _ = normalize_records(_df(rows), origin="test")
    return df


@pytest.mark.parametrize("raw,expected", [("631", (6, 31)), ("5", (0, 5)), ("45", (0, 45)), ("0", (0, 0)),
                                          ("1205", (12, 5)), ("0631", (6, 31)), ("2359", (23, 59)), ("631.0", (6, 31))])
def test_acq_time_without_leading_zeros(raw, expected):
    assert parse_acq_time(raw) == expected


@pytest.mark.parametrize("raw", ["2460", "2400", "abc", "", "12345", "-5"])
def test_acq_time_invalid(raw):
    assert parse_acq_time(raw) is None


def test_observed_at_is_utc():
    df = norm([viirs("36.9", "31.4", t="631")])
    ts = df["observed_at_utc"].iloc[0]
    assert str(ts.tz) == "UTC" and ts.hour == 6 and ts.minute == 31


def test_missing_frp_not_zero_filled_and_bad_rows_rejected():
    rows = [viirs("36.9", "31.4", frp=""), viirs("95", "31.4"), viirs("36.9", "31.4", t="2470")]
    df, rep = normalize_records(_df(rows), origin="test")
    assert len(df) == 1
    assert pd.isna(df["frp_mw"].iloc[0])
    assert rep.rejected["geçersiz koordinat"] == 1
    assert rep.rejected["geçersiz acq_time"] == 1


def test_duplicate_downloads_do_not_increase_count():
    rows = [viirs("36.90", "31.40"), viirs("36.91", "31.41"), modis("36.90", "31.40")]
    a = norm(rows)
    combined, removed1 = merge_observations(None, a)
    combined, removed2 = merge_observations(combined, norm(rows))
    assert len(combined) == 3
    assert removed2 == 3
    assert sorted(a["observation_id"]) == sorted(norm(rows)["observation_id"])


def test_different_satellites_same_place_not_removed():
    rows = [
        viirs("36.9", "31.4", sat="N20", t="1012"),
        viirs("36.9", "31.4", sat="N", t="1012"),
        viirs("36.9", "31.4", sat="N20", t="2318"),
        modis("36.9", "31.4", sat="T", t="1012"),
        modis("36.9", "31.4", sat="A", t="1012"),
    ]
    combined, removed = merge_observations(None, norm(rows))
    assert len(combined) == 5 and removed == 0


def test_one_modis_links_to_multiple_viirs():
    rows = [modis("36.9000", "31.4000", t="1050"),
            viirs("36.9020", "31.4010", t="1012"), viirs("36.8985", "31.4030", t="1012"),
            viirs("36.9010", "31.3970", t="1012")]
    res = run_matching(norm(rows), MatchParams())
    assert len(res["links"]) == 3
    assert res["links"]["modis_observation_id"].nunique() == 1
    assert len(res["events"]) == 1
    assert res["events"][0]["n_modis"] == 1 and res["events"][0]["n_viirs"] == 3


def test_far_apart_not_merged():
    rows = [modis("36.90", "31.40", t="1050"), viirs("37.00", "31.40", t="1040")]
    res = run_matching(norm(rows), MatchParams())
    assert res["links"].empty
    assert len(res["events"]) == 2


def test_distance_is_in_km_not_degrees():
    d = float(haversine_km(36.9, 31.40, 36.9, 31.412))
    assert 1.0 < d < 1.15


def test_different_times_not_merged():
    rows = [modis("36.90", "31.40", t="1050"), viirs("36.90", "31.40", t="1350")]
    res = run_matching(norm(rows), MatchParams())
    assert res["links"].empty
    rows = [modis("36.90", "31.40", d="2025-08-01"), modis("36.90", "31.40", d="2025-08-10")]
    res = run_matching(norm(rows), MatchParams())
    assert len(res["events"]) == 2


def test_continuity_links_next_day_but_keeps_clusters_separate():
    rows = [modis("36.90", "31.40", d="2025-08-10"), modis("36.905", "31.405", d="2025-08-11")]
    res = run_matching(norm(rows), MatchParams())
    assert len(res["events"]) == 1
    assert res["events"][0]["n_clusters"] == 2
    assert res["events"][0]["n_days"] == 2


def test_chaining_does_not_create_giant_event():
    rows = []
    for k in range(40):
        d = (pd.Timestamp("2025-07-01") + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
        rows.append(viirs("36.9", f"{30.0 + k * 0.0168:.4f}", d=d))
    res = run_matching(norm(rows), MatchParams())
    assert all(e["extent_km"] <= MatchParams().max_event_extent_km for e in res["events"])
    assert len(res["events"]) >= 2
    assert any(r["status"] == "belirsiz" for r in res["continuity"])


def test_ambiguous_continuity_not_merged():
    rows = [viirs("36.9", "31.400", d="2025-08-10"), viirs("36.9", "31.4336", d="2025-08-10"),
            viirs("36.9", "31.4168", d="2025-08-11")]
    res = run_matching(norm(rows), MatchParams(cont_dist_km=2.0))
    assert len(res["events"]) == 2
    amb = [r for r in res["continuity"] if r["status"] == "belirsiz"]
    assert len(amb) == 1


def test_unmatched_observations_kept():
    rows = [modis("36.90", "31.40"), viirs("38.0", "29.0")]
    res = run_matching(norm(rows), MatchParams())
    assert len(res["observations"]) == 2
    assert res["observations"]["event_id"].notna().all()


def test_quality_filter_sensor_specific():
    rows = [modis("36.9", "31.4", conf="25"), modis("36.9", "31.5", conf="85"),
            viirs("36.9", "31.4", conf="l"), viirs("36.9", "31.6", conf="h")]
    obs, summary = apply_quality(norm(rows), QualityParams(modis_min_confidence=30, viirs_classes=("n", "h")))
    assert summary["MODIS"]["excluded"] == 1 and summary["VIIRS"]["excluded"] == 1
    assert set(obs["confidence_raw"]) == {"25", "85", "l", "h"}


def _client(handler, tmp_path):
    return FirmsClient(cache=ResponseCache(tmp_path / "c.sqlite"), transport=httpx.MockTransport(handler),
                       map_key=FAKE_KEY, pause_s=0)


AVAIL = {"MODIS_NRT": {"data_id": "MODIS_NRT", "min_date": "2025-08-01", "max_date": "2025-08-20"}}


def test_split_date_range_respects_5_days():
    parts = split_date_range(date(2025, 8, 1), date(2025, 8, 12))
    assert [n for _, n in parts] == [5, 5, 2]


def test_zero_detections_vs_error_vs_out_of_range(tmp_path):
    def handler(req: httpx.Request):
        if "/2025-08-10" in req.url.path:
            return httpx.Response(200, text=MODIS_HDR + "\n")
        return httpx.Response(500, text="Internal error")
    c = _client(handler, tmp_path)
    out_empty = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 10), date(2025, 8, 10), AVAIL)
    assert out_empty.chunks[0].status == "empty"
    out_err = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 11), date(2025, 8, 11), AVAIL)
    assert out_err.chunks[0].status == "error" and out_err.chunks[0].error["kind"] == "http"
    out_oor = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 25), date(2025, 8, 25), AVAIL)
    assert out_oor.chunks[0].status == "out_of_range"


def test_invalid_key_and_rate_limit_classified(tmp_path, monkeypatch):
    import app.firms as firms_mod
    monkeypatch.setattr(firms_mod, "RATE_LIMIT_RETRIES", 0)
    c = _client(lambda r: httpx.Response(200, text="Invalid MAP_KEY."), tmp_path)
    out = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 1), date(2025, 8, 12), AVAIL)
    assert {ch.error["kind"] for ch in out.chunks} == {"invalid_key"}
    assert sum(ch.day_range for ch in out.chunks) == 12
    (tmp_path / "x").mkdir()
    c = _client(lambda r: httpx.Response(429, text="Exceeding allowed transaction limit."), tmp_path / "x")
    out = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 1), date(2025, 8, 1), AVAIL)
    assert out.chunks[0].error["kind"] == "rate_limit"


def test_timeout_classified(tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    c = _client(handler, tmp_path)
    out = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 1), date(2025, 8, 1), AVAIL)
    assert out.chunks[0].error["kind"] == "timeout"


def test_cache_prevents_redownload(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, text=MODIS_HDR + "\n36.9,31.4,330,1.0,1.0,2025-08-10,1050,A,MODIS,80,6.1NRT,300,20,D\n")
    c = _client(handler, tmp_path)
    a = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 10), date(2025, 8, 10), AVAIL)
    b = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 10), date(2025, 8, 10), AVAIL)
    assert len(calls) == 1 and b.chunks[0].from_cache and len(b.frames[0]) == 1


def test_key_masked_in_errors_and_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FIRMS_MAP_KEY", FAKE_KEY)
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FAKE_KEY}/MODIS_NRT/27,35.5,32.5,38/1/2025-08-10"

    def handler(req):
        raise httpx.ConnectError(f"failed for {url}", request=req)
    c = FirmsClient(cache=ResponseCache(tmp_path / "c.sqlite"), transport=httpx.MockTransport(handler), pause_s=0)
    out = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 10), date(2025, 8, 10), AVAIL)
    msg = out.chunks[0].error["message"]
    assert FAKE_KEY not in msg and "/csv/***/" in msg

    config.install_log_masking()
    lg = logging.getLogger("app.test")
    with caplog.at_level(logging.INFO):
        lg.info("istek: %s", url)
    assert FAKE_KEY not in caplog.text
    assert "MAP_KEY=***" in config.mask_secret(f"?MAP_KEY={FAKE_KEY}")


def test_health_does_not_expose_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("FIRMS_MAP_KEY", FAKE_KEY)
    r = TestClient(app).get("/api/health")
    assert FAKE_KEY not in r.text and r.json()["map_key_configured"] is True


def test_rate_limit_retries_then_succeeds(tmp_path, monkeypatch):
    import app.firms as firms_mod
    monkeypatch.setattr(firms_mod, "RATE_LIMIT_WAIT_S", 0)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="Exceeding allowed transaction limit.")
        return httpx.Response(200, text=MODIS_HDR + "\n")
    c = _client(handler, tmp_path)
    msgs = []
    out = c.fetch_area("MODIS_NRT", (27, 35.5, 32.5, 38), date(2025, 8, 10), date(2025, 8, 10), AVAIL,
                       on_progress=lambda s, ch, m: msgs.append(m))
    assert out.chunks[0].status == "empty" and len(calls) == 2
    assert any(m and "işlem sınırı" in m for m in msgs)


def test_long_range_planning_allowed(tmp_path):
    c = _client(lambda r: httpx.Response(200, text=MODIS_HDR + "\n"), tmp_path)
    avail = {"MODIS_SP": {"data_id": "MODIS_SP", "min_date": "2000-11-01", "max_date": "2026-08-31"}}
    oor, plan = c.plan_area("MODIS_SP", (27, 35.5, 32.5, 38), date(2020, 1, 1), date(2026, 11, 13), avail)
    assert sum(n for _, n in plan) == (date(2026, 8, 31) - date(2020, 1, 1)).days + 1
    assert len(oor) == (date(2026, 11, 13) - date(2026, 8, 31)).days
    assert all(n <= 5 for _, n in plan)


def test_combined_mode_segments_do_not_overlap():
    from app.main import FetchIn, plan_segments
    avail = {"MODIS_SP": {"max_date": "2026-08-31"}, "VIIRS_NOAA20_SP": {"max_date": "2026-07-31"}}
    segs, notes = plan_segments(FetchIn(mode="combined", start="2020-01-01", end="2026-11-13"), avail)
    assert segs["MODIS"] == [("MODIS_SP", date(2020, 1, 1), date(2026, 8, 31)),
                             ("MODIS_NRT", date(2026, 9, 1), date(2026, 11, 13))]
    assert segs["VIIRS"][1] == ("VIIRS_NOAA20_NRT", date(2026, 8, 1), date(2026, 11, 13))
    assert len(notes) == 2


def test_matching_honours_cancel():
    from app.matching import MatchingCancelled
    rows = [modis("36.90", "31.40"), viirs("36.901", "31.401")]
    with pytest.raises(MatchingCancelled):
        run_matching(norm(rows), MatchParams(), should_cancel=lambda: True)


def test_job_cancel_is_immediate_and_result_ignored():
    import threading
    import time as _t
    from fastapi.testclient import TestClient
    from app.main import JOBS, app, start_job

    gate = threading.Event()

    def slow(job):
        gate.wait(5)
        return {"result": "should not appear"}

    job = start_job(slow)
    c = TestClient(app)
    assert c.get(f"/api/jobs/{job.id}").json()["status"] == "running"
    c.post(f"/api/jobs/{job.id}/cancel")
    assert c.get(f"/api/jobs/{job.id}").json()["status"] == "cancelled"
    gate.set()
    _t.sleep(0.2)
    j = c.get(f"/api/jobs/{job.id}").json()
    assert j["status"] == "cancelled" and j["result"] is None
    JOBS.pop(job.id, None)


def test_analyze_job_completes():
    import time as _t
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    did = c.post("/api/sample").json()["dataset_id"]
    jid = c.post("/api/analyze_job", json={"dataset_id": did}).json()["job_id"]
    for _ in range(100):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] != "running":
            break
        _t.sleep(0.1)
    assert j["status"] == "done" and j["result"]["stats"]["n_events"] > 0
    assert "elapsed_s" in j
