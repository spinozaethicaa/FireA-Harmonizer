from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import L

COMMON_COLUMNS = [
    "observation_id", "source", "satellite", "instrument", "latitude", "longitude",
    "observed_at_utc", "frp_mw", "confidence_raw", "scan_km", "track_km", "daynight", "version",
]

MODIS_SAT = {"T": "Terra", "TERRA": "Terra", "A": "Aqua", "AQUA": "Aqua"}
VIIRS_SAT = {
    "N": "Suomi NPP", "NPP": "Suomi NPP", "SNPP": "Suomi NPP", "SUOMI NPP": "Suomi NPP",
    "N20": "NOAA-20", "1": "NOAA-20", "NOAA-20": "NOAA-20", "NOAA20": "NOAA-20", "J1": "NOAA-20",
    "N21": "NOAA-21", "2": "NOAA-21", "NOAA-21": "NOAA-21", "NOAA21": "NOAA-21", "J2": "NOAA-21",
}
VIIRS_SAT_TO_SOURCE = {"Suomi NPP": "SNPP", "NOAA-20": "NOAA20", "NOAA-21": "NOAA21"}
VIIRS_CLASS = {"l": "l", "low": "l", "n": "n", "nominal": "n", "h": "h", "high": "h"}


@dataclass
class NormalizeReport:
    input_rows: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    duplicates_removed: int = 0
    output_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    def reject(self, reason: str, n: int = 1):
        if n:
            self.rejected[reason] = self.rejected.get(reason, 0) + int(n)

    def to_dict(self):
        return {
            "input_rows": self.input_rows,
            "rejected": self.rejected,
            "duplicates_removed": self.duplicates_removed,
            "output_rows": self.output_rows,
            "warnings": self.warnings,
        }


def parse_acq_time(value) -> tuple[int, int] | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            return None
    else:
        if s.endswith(".0"):
            s = s[:-2]
        if not s.isdigit() or len(s) > 4:
            return None
        s = s.zfill(4)
        h, m = int(s[:2]), int(s[2:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.replace({"": np.nan}), errors="coerce")


def _detect_instrument(row_instr: str, columns: set[str]) -> str | None:
    v = (row_instr or "").strip().upper()
    if v.startswith("MODIS"):
        return "MODIS"
    if v.startswith("VIIRS"):
        return "VIIRS"
    if "bright_ti4" in columns:
        return "VIIRS"
    if "brightness" in columns:
        return "MODIS"
    return None


def stable_observation_id(instrument, satellite, acq_date, hh, mm, lat, lon, version) -> str:
    key = f"{instrument}|{satellite}|{acq_date}|{hh:02d}{mm:02d}|{lat:.5f}|{lon:.5f}|{version}"
    return "OBS-" + hashlib.sha1(key.encode()).hexdigest()[:16]


def normalize_records(raw: pd.DataFrame, origin: str, declared_source: str | None = None
                      ) -> tuple[pd.DataFrame, NormalizeReport]:
    rep = NormalizeReport(input_rows=len(raw))
    if raw.empty:
        return pd.DataFrame(columns=COMMON_COLUMNS + ["origin", "raw"]), rep

    df = raw.copy()
    df.columns = [c.strip().lower() if c != "_source" else c for c in df.columns]
    cols = set(df.columns)
    for required in ("latitude", "longitude", "acq_date", "acq_time"):
        if required not in cols:
            raise ValueError(L("Zorunlu sütun eksik: ", "Required column missing: ") + str(required))
    for c in ("satellite", "instrument", "confidence", "version", "frp", "scan", "track", "daynight"):
        if c not in cols:
            df[c] = ""
    raw_cols = [c for c in raw.columns if c != "_source"]

    lat = _to_float(df["latitude"])
    lon = _to_float(df["longitude"])
    bad_coord = lat.isna() | lon.isna() | (lat.abs() > 90) | (lon.abs() > 180)
    rep.reject(L("geçersiz koordinat", "invalid coordinate"), bad_coord.sum())

    dates = pd.to_datetime(df["acq_date"].str.strip(), format="%Y-%m-%d", errors="coerce")
    bad_date = dates.isna()
    rep.reject(L("geçersiz acq_date", "invalid acq_date"), (bad_date & ~bad_coord).sum())

    times = df["acq_time"].map(parse_acq_time)
    bad_time = times.isna()
    rep.reject(L("geçersiz acq_time", "invalid acq_time"), (bad_time & ~bad_coord & ~bad_date).sum())

    instr_col = df["instrument"].astype(str).str.strip().str.upper()
    fallback = "VIIRS" if "bright_ti4" in cols else ("MODIS" if "brightness" in cols else None)
    instruments = pd.Series(np.where(instr_col.str.startswith("MODIS"), "MODIS",
                                     np.where(instr_col.str.startswith("VIIRS"), "VIIRS", fallback or "")),
                            index=df.index)
    bad_instr = instruments == ""
    rep.reject("cihaz (MODIS/VIIRS) belirlenemedi", (bad_instr & ~bad_coord & ~bad_date & ~bad_time).sum())

    keep = ~(bad_coord | bad_date | bad_time | bad_instr)
    df, lat, lon, dates, times, instruments = (x[keep] for x in (df, lat, lon, dates, times, instruments))
    if df.empty:
        return pd.DataFrame(columns=COMMON_COLUMNS + ["origin", "raw"]), rep

    frp = _to_float(df["frp"])
    scan = _to_float(df["scan"])
    track = _to_float(df["track"])
    n_missing_frp = int(frp.isna().sum())
    if n_missing_frp:
        rep.warnings.append(L(f"{n_missing_frp} kayıtta FRP eksik/okunamadı; boş bırakıldı (0 ile doldurulmadı).", f"FRP missing/unreadable in {n_missing_frp} records; left empty (not filled with 0)."))

    sat_raw = df["satellite"].astype(str).str.strip()
    sat_up = sat_raw.str.upper()
    satellite = pd.Series(np.where(instruments == "MODIS", sat_up.map(MODIS_SAT), sat_up.map(VIIRS_SAT)),
                          index=df.index)
    satellite = satellite.where(satellite.notna(), sat_raw.where(sat_raw != "", "bilinmiyor"))
    version = df["version"].astype(str).str.strip()
    hh = times.map(lambda x: x[0]).astype(int)
    mm = times.map(lambda x: x[1]).astype(int)
    observed = (dates + pd.to_timedelta(hh, unit="h") + pd.to_timedelta(mm, unit="m")).dt.tz_localize("UTC")

    fam = np.where(version.str.upper().str.contains("NRT"), "NRT", "SP")
    inferred = np.where(instruments == "MODIS", "MODIS_" + fam,
                        "VIIRS_" + satellite.map(VIIRS_SAT_TO_SOURCE).fillna("UNKNOWN").to_numpy() + "_" + fam)
    if "_source" in df.columns:
        given = df["_source"].astype(str)
        source = np.where(given.str.len() > 0, given, inferred)
    elif declared_source:
        source = np.full(len(df), declared_source)
    else:
        source = inferred

    date_s = dates.dt.strftime("%Y-%m-%d").to_numpy()
    obs_ids = [stable_observation_id(i, s_, d_, h_, m_, la_, lo_, v_) for i, s_, d_, h_, m_, la_, lo_, v_ in
               zip(instruments.to_numpy(), satellite.to_numpy(), date_s, hh.to_numpy(), mm.to_numpy(),
                   lat.to_numpy(), lon.to_numpy(), version.to_numpy())]
    lower_map = {c: c.strip().lower() for c in raw_cols}
    raw_records = df[[lower_map[c] for c in raw_cols]].rename(columns={v: k for k, v in lower_map.items()}).to_dict("records")
    daynight = df["daynight"].astype(str).str.strip()

    out = pd.DataFrame({
        "observation_id": obs_ids,
        "source": source,
        "satellite": satellite.to_numpy(),
        "instrument": instruments.to_numpy(),
        "latitude": lat.to_numpy(float),
        "longitude": lon.to_numpy(float),
        "observed_at_utc": observed.to_numpy(),
        "frp_mw": frp.to_numpy(float),
        "confidence_raw": df["confidence"].astype(str).str.strip().to_numpy(),
        "scan_km": scan.to_numpy(float),
        "track_km": track.to_numpy(float),
        "daynight": daynight.where(daynight != "", None).to_numpy(),
        "version": version.to_numpy(),
        "origin": origin,
        "raw": raw_records,
    })
    out["observed_at_utc"] = pd.to_datetime(out["observed_at_utc"], utc=True)
    rep.output_rows = len(out)
    return out, rep


def merge_observations(existing: pd.DataFrame | None, new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if existing is None or existing.empty:
        combined = new
    else:
        combined = pd.concat([existing, new], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset="observation_id", keep="first").reset_index(drop=True)
    return combined, before - len(combined)


@dataclass
class QualityParams:
    modis_min_confidence: int = 30
    viirs_classes: tuple[str, ...] = ("n", "h")
    daynight: str = "all"


def apply_quality(obs: pd.DataFrame, q: QualityParams) -> tuple[pd.DataFrame, dict]:
    obs = obs.copy()
    reason = pd.Series([None] * len(obs), index=obs.index, dtype=object)
    is_m = obs["instrument"] == "MODIS"
    conf_num = pd.to_numeric(obs["confidence_raw"], errors="coerce")
    reason[is_m & conf_num.isna()] = L("MODIS confidence okunamadı", "MODIS confidence unreadable")
    reason[is_m & (conf_num < q.modis_min_confidence)] = f"MODIS confidence < {q.modis_min_confidence}"
    cls = obs["confidence_raw"].astype(str).str.strip().str.lower().map(VIIRS_CLASS)
    reason[~is_m & cls.isna()] = L("VIIRS confidence sınıfı okunamadı", "VIIRS confidence class unreadable")
    for c in ("l", "n", "h"):
        if c not in q.viirs_classes:
            reason[~is_m & (cls == c)] = L(f"VIIRS sınıfı '{c}' seçili değil", f"VIIRS class '{c}' not selected")
    if q.daynight in ("D", "N"):
        reason[reason.isna() & (obs["daynight"] != q.daynight)] = L(f"gündüz/gece filtresi ({q.daynight})", f"day/night filter ({q.daynight})")
    obs["quality_excluded_reason"] = reason
    obs["quality_pass"] = reason.isna().to_numpy()
    summary = {}
    for instr in ("MODIS", "VIIRS"):
        sub = obs[obs["instrument"] == instr]
        excl = sub[~sub["quality_pass"]]
        summary[instr] = {
            "total": int(len(sub)),
            "kept": int(sub["quality_pass"].sum()),
            "excluded": int(len(excl)),
            "excluded_by_reason": {k: int(v) for k, v in excl["quality_excluded_reason"].value_counts().items()},
        }
    return obs, summary


def read_uploaded_csv(content: bytes) -> pd.DataFrame:
    text = content.decode("utf-8-sig", errors="replace")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
