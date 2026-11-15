from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import L, get_lang
from .firms import SUPPORTED_SOURCES, ChunkResult
from .matching import MatchingCancelled, MatchParams, run_matching
from .normalize import COMMON_COLUMNS, QualityParams, apply_quality

APP_VERSION = "0.2.0"
COVERED = {"ok", "empty", "uploaded", "sample"}
MAP_POINT_LIMIT = 25000

COMPACT_COLS = ["observation_id", "source", "satellite", "instrument", "latitude", "longitude",
                "observed_at_utc", "frp_mw", "confidence_raw", "quality_pass", "event_id",
                "cross_matched", "n_cross_links"]


@dataclass
class Dataset:
    dataset_id: str
    kind: str
    label: str
    bbox: tuple | None
    start: date
    end: date
    sources: dict[str, str]
    observations: pd.DataFrame
    coverage: dict[str, dict[str, str]]
    coverage_source: dict[str, dict[str, str]] = field(default_factory=dict)
    normalize_reports: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    duplicates_removed: int = 0
    warnings: list[str] = field(default_factory=list)
    availability: dict | None = None
    segments: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def meta(self) -> dict:
        chunk_stats: dict[str, int] = {}
        for c in self.chunks:
            chunk_stats[c["status"]] = chunk_stats.get(c["status"], 0) + (c["day_range"] if c["status"] == "out_of_range" else 1)
        errors = [c for c in self.chunks if c["status"] == "error"]
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "is_sample": self.kind == "sample",
            "label": self.label,
            "bbox": list(self.bbox) if self.bbox else None,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "sources": self.sources,
            "segments": self.segments,
            "n_observations": int(len(self.observations)),
            "normalize_reports": self.normalize_reports,
            "chunk_stats": chunk_stats,
            "n_chunks": len(self.chunks),
            "n_from_cache": sum(1 for c in self.chunks if c.get("from_cache")),
            "errors": errors[:20],
            "n_errors": len(errors),
            "duplicates_removed": self.duplicates_removed,
            "warnings": self.warnings,
            "availability": self.availability,
            "created_at": self.created_at,
            "products_versions": _versions(self.observations),
        }


def _versions(obs: pd.DataFrame) -> dict:
    if obs.empty:
        return {}
    return {src: sorted(set(g["version"])) for src, g in obs.groupby("source")}


class Store:
    def __init__(self, max_datasets=8, max_results=6):
        self._lock = threading.Lock()
        self.datasets: OrderedDict[str, Dataset] = OrderedDict()
        self.results: OrderedDict[str, dict] = OrderedDict()
        self.max_datasets = max_datasets
        self.max_results = max_results

    def add(self, ds: Dataset):
        with self._lock:
            self.datasets[ds.dataset_id] = ds
            while len(self.datasets) > self.max_datasets:
                self.datasets.popitem(last=False)

    def get(self, did: str) -> Dataset | None:
        return self.datasets.get(did)

    def put_result(self, key: str, r: dict):
        with self._lock:
            self.results[key] = r
            self.results.move_to_end(key)
            while len(self.results) > self.max_results:
                self.results.popitem(last=False)

    def get_result(self, key: str) -> dict | None:
        r = self.results.get(key)
        if r is not None:
            self.results.move_to_end(key)
        return r


STORE = Store()
VM_CACHE: "OrderedDict[str, dict]" = OrderedDict()


def virtual_modis_for(ds: Dataset, should_cancel=None) -> dict:
    if ds.dataset_id in VM_CACHE:
        return VM_CACHE[ds.dataset_id]
    try:
        from .firea_infer import run_virtual_modis
        res = run_virtual_modis(ds.observations, ds.start, ds.end, should_cancel=should_cancel)
    except MatchingCancelled:
        raise
    except Exception as exc:
        import logging
        logging.getLogger("app").exception("FireA çıkarımı başarısız")
        res = {"available": False, "reason": L("FireA çıkarımı başarısız: ", "FireA inference failed: ") + str(exc)}
    if not res.get("available"):
        return res
    VM_CACHE[ds.dataset_id] = res
    while len(VM_CACHE) > 8:
        VM_CACHE.popitem(last=False)
    return res


def new_dataset_id() -> str:
    return "DS-" + uuid.uuid4().hex[:10]


def coverage_from_chunks(chunks: list[ChunkResult]) -> tuple[dict, dict]:
    cov: dict[str, dict[str, str]] = {"MODIS": {}, "VIIRS": {}}
    src: dict[str, dict[str, str]] = {"MODIS": {}, "VIIRS": {}}
    for ch in chunks:
        instr = SUPPORTED_SOURCES[ch.source]["instrument"]
        for d in ch.dates:
            k = d.isoformat()
            if cov[instr].get(k) in COVERED and ch.status == "out_of_range":
                continue
            cov[instr][k] = ch.status
            src[instr][k] = ch.source
    return cov, src


def coverage_from_files(obs: pd.DataFrame, status: str) -> tuple[dict, date | None, date | None]:
    cov: dict[str, dict[str, str]] = {"MODIS": {}, "VIIRS": {}}
    if obs.empty:
        return cov, None, None
    dmin = obs["observed_at_utc"].min().date()
    dmax = obs["observed_at_utc"].max().date()
    for instr, g in obs.groupby("instrument"):
        a, b = g["observed_at_utc"].min().date(), g["observed_at_utc"].max().date()
        d = a
        while d <= b:
            cov[instr][d.isoformat()] = status
            d += timedelta(days=1)
    return cov, dmin, dmax


def params_hash(q: QualityParams, m: MatchParams) -> str:
    return hashlib.sha1(json.dumps([asdict(q), asdict(m)], sort_keys=True).encode()).hexdigest()[:12]


def analyze(ds: Dataset, q: QualityParams, m: MatchParams, should_cancel=None, on_phase=None) -> dict:
    key = f"{ds.dataset_id}:{params_hash(q, m)}:{get_lang()}"
    hit = STORE.get_result(key)
    if hit is not None:
        return hit
    if ds.observations.empty:
        obs_all = ds.observations.assign(quality_pass=pd.Series(dtype=bool),
                                         quality_excluded_reason=pd.Series(dtype=object))
        qsum = {i: {"total": 0, "kept": 0, "excluded": 0, "excluded_by_reason": {}} for i in ("MODIS", "VIIRS")}
    else:
        obs_all, qsum = apply_quality(ds.observations, q)
    kept = obs_all[obs_all["quality_pass"]]
    if on_phase:
        on_phase("analyze", L(f"{len(kept):,} gözlem eşleştiriliyor…", f"Matching {len(kept):,} observations…"))
    res = run_matching(kept, m, should_cancel=should_cancel)
    mobs = res["observations"]

    assign_cols = ["pass_id", "cluster_id", "event_id", "n_cross_links", "cross_matched", "match_flags"]
    merged = obs_all.copy()
    if len(merged):
        a = mobs.set_index("observation_id")[assign_cols] if len(mobs) else None
        for c in assign_cols:
            merged[c] = merged["observation_id"].map(a[c]) if a is not None else None
        merged["n_cross_links"] = merged["n_cross_links"].fillna(0).astype(int)
        merged["cross_matched"] = merged["cross_matched"].fillna(False).astype(bool)
        merged["match_flags"] = merged["match_flags"].fillna("")
        merged = merged.sort_values("observed_at_utc").reset_index(drop=True)
    else:
        for c in assign_cols:
            merged[c] = pd.Series(dtype=object)
    merged["_day"] = merged["observed_at_utc"].dt.strftime("%Y-%m-%d") if len(merged) else pd.Series(dtype=str)

    events = res["events"]
    calendar = build_calendar(ds, merged, res["links"])
    if on_phase:
        on_phase("firea", L("Harmonize (FireA) hesaplanıyor…", "Computing harmonization (FireA)…"))
    vm = virtual_modis_for(ds, should_cancel=should_cancel)
    vm_summary = {k: v for k, v in vm.items() if k != "clusters"}
    summary = {
        "analysis_key": key,
        "dataset": ds.meta(),
        "quality_params": asdict(q),
        "match_params": m.to_dict(),
        "quality_summary": qsum,
        "events": [_event_lite(e) for e in events],
        "calendar": calendar,
        "stats": {
            "n_observations_total": int(len(merged)),
            "n_observations_used": int(len(kept)),
            "n_events": len(events),
            "n_cross_links": int(len(res["links"])),
            "n_ambiguous_continuity": sum(1 for c in res["continuity"] if c["status"] == "belirsiz"),
            "n_multi_modis_viirs": int((merged["match_flags"] != "").sum()) if len(merged) else 0,
            "map_point_limit": MAP_POINT_LIMIT,
        },
        "time_note": L("Tüm zamanlar UTC'dir (FIRMS acq_date/acq_time). Takvim günleri UTC gününe göredir; "
                       "yerel saat ayrıca Türkiye saati (TSİ, UTC+3) olarak gösterilir.",
                       "All times are UTC (FIRMS acq_date/acq_time). Calendar days follow the UTC day; "
                       "local time is also shown as Turkey time (UTC+3)."),
        "virtual_modis": vm_summary,
    }
    out = {"summary": summary, "vm": vm, "merged": merged, "links": res["links"], "events": {e["event_id"]: e for e in events},
           "events_list": events, "continuity": res["continuity"], "clusters": res["clusters"]}
    STORE.put_result(key, out)
    return out


def _event_lite(e: dict) -> dict:
    keys = ("event_id", "first_obs_utc", "last_obs_utc", "days", "n_days", "n_obs", "n_modis", "n_viirs",
            "n_cross_matched_obs", "n_cross_links", "n_clusters", "centroid_lat", "centroid_lon", "bbox",
            "extent_km", "max_single_frp_mw")
    d = {k: e[k] for k in keys}
    d["n_flags"] = len(e["flags"])
    return d


def compact_observations(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    c = df[COMPACT_COLS].copy()
    c["observed_at_utc"] = c["observed_at_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    c["latitude"] = c["latitude"].round(5)
    c["longitude"] = c["longitude"].round(5)
    c["frp_mw"] = c["frp_mw"].round(2)
    c = c.astype(object).where(c.notna(), None)
    return c.to_dict("records")


def full_observation(r: pd.Series) -> dict:
    o = {}
    for k, v in r.items():
        if k == "_day":
            continue
        if k == "observed_at_utc":
            o[k] = pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(v, (dict, list, str)) or v is None:
            o[k] = v
        elif isinstance(v, (np.bool_, bool)):
            o[k] = bool(v)
        elif isinstance(v, (np.integer,)):
            o[k] = int(v)
        elif pd.isna(v):
            o[k] = None
        elif isinstance(v, (np.floating, float)):
            o[k] = float(v)
        else:
            o[k] = v
    return o


def build_calendar(ds: Dataset, obs: pd.DataFrame, links: pd.DataFrame) -> dict:
    link_count: dict[str, int] = {}
    link_events: dict[str, int] = {}
    if len(links) and len(obs):
        idx = obs.set_index("observation_id")
        ld = links.assign(_day=links["modis_observation_id"].map(idx["_day"]),
                          _ev=links["modis_observation_id"].map(idx["event_id"]))
        link_count = ld.groupby("_day").size().to_dict()
        link_events = ld[ld["same_cluster"]].groupby("_day")["_ev"].nunique().to_dict()
    counts = {}
    if len(obs):
        g = obs.groupby(["_day", "instrument"])["quality_pass"].agg(["sum", "size"])
        counts = {k: (int(v["sum"]), int(v["size"])) for k, v in g.iterrows()}
        ev_per_day = obs[obs["event_id"].notna()].groupby("_day")["event_id"].nunique().to_dict()
    else:
        ev_per_day = {}
    days = []
    d = ds.start
    while d <= ds.end:
        k = d.isoformat()
        sensors = {}
        detected, covered = {}, {}
        for instr in ("MODIS", "VIIRS"):
            st = ds.coverage.get(instr, {}).get(k, "not_queried")
            used, total = counts.get((k, instr), (0, 0))
            sensors[instr] = {"coverage": st, "product": ds.coverage_source.get(instr, {}).get(k),
                              "n_detections": used, "n_excluded_quality": total - used}
            detected[instr] = used > 0
            covered[instr] = st in COVERED
        if detected["MODIS"] and detected["VIIRS"]:
            cls = "both"
        elif detected["MODIS"]:
            cls = "modis_only"
        elif detected["VIIRS"]:
            cls = "viirs_only"
        elif covered["MODIS"] and covered["VIIRS"]:
            cls = "none"
        else:
            cls = "unavailable"
        days.append({
            "date": k,
            "sensors": sensors,
            "class": cls,
            "partial_coverage": cls != "unavailable" and not (covered["MODIS"] and covered["VIIRS"]),
            "n_events": int(ev_per_day.get(k, 0)),
            "n_cross_links": int(link_count.get(k, 0)),
            "n_events_with_cross_match": int(link_events.get(k, 0)),
        })
        d += timedelta(days=1)
    return {"timezone": "UTC", "days": days}


def export_observations_csv(merged: pd.DataFrame) -> str:
    if merged.empty:
        return ",".join(COMMON_COLUMNS) + "\n"
    df = merged.copy()
    df["observed_at_utc"] = df["observed_at_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = pd.DataFrame(list(df["raw"])).add_prefix("raw_")
    base_cols = COMMON_COLUMNS + ["origin", "quality_pass", "quality_excluded_reason", "pass_id",
                                  "cluster_id", "event_id", "cross_matched", "n_cross_links", "match_flags"]
    out = pd.concat([df[base_cols].reset_index(drop=True), raw.reset_index(drop=True)], axis=1)
    return out.to_csv(index=False)


def export_events_csv(events: list[dict]) -> str:
    rows = []
    for e in events:
        rows.append({
            "event_id": e["event_id"],
            "first_obs_utc": e["first_obs_utc"],
            "last_obs_utc": e["last_obs_utc"],
            "days_utc": ";".join(e["days"]),
            "n_days": e["n_days"],
            "n_modis": e["n_modis"],
            "n_viirs": e["n_viirs"],
            "satellites": ";".join(e["satellites"]),
            "sources": ";".join(e["sources"]),
            "n_cross_matched_obs": e["n_cross_matched_obs"],
            "n_unmatched_obs": e["n_unmatched_obs"],
            "n_cross_links": e["n_cross_links"],
            "n_clusters": e["n_clusters"],
            "centroid_lat": e["centroid_lat"],
            "centroid_lon": e["centroid_lon"],
            "obs_bbox_w_s_e_n": ";".join(str(x) for x in e["bbox"]),
            "obs_extent_km": e["extent_km"],
            "max_single_frp_mw": e["max_single_frp_mw"],
            "n_ambiguous_links": len(e["ambiguous_links"]),
            "flags": " | ".join(e["flags"]),
            "explanation": e["explanation"],
        })
    return pd.DataFrame(rows).to_csv(index=False) if rows else "event_id\n"


def export_links_json(r: dict) -> str:
    merged = r["merged"]
    mem = merged[merged["event_id"].notna()]
    membership = pd.DataFrame({
        "event_id": mem["event_id"], "cluster_id": mem["cluster_id"], "observation_id": mem["observation_id"],
        "instrument": mem["instrument"], "satellite": mem["satellite"],
        "observed_at_utc": mem["observed_at_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cross_matched": mem["cross_matched"],
    }).to_dict("records") if len(mem) else []
    links = r["links"].astype(object).where(r["links"].notna(), None).to_dict("records")
    return json.dumps({
        "note": L("Olay-gözlem üyelikleri, sensörler arası bağlantılar ve kümeler arası devam ilişkileri. Zamanlar UTC.", "Event–observation memberships, cross-sensor links and cluster continuity links. Times are UTC."),
        "event_membership": membership,
        "cross_sensor_links": links,
        "continuity_links": r["continuity"],
        "clusters": r["clusters"],
    }, ensure_ascii=False, default=str)


def export_metadata_json(summary: dict) -> str:
    ds = summary["dataset"]
    return json.dumps({
        "application": {"name": L("MODIS–VIIRS Sıcak Nokta Uyumlaştırma Prototipi", "MODIS–VIIRS Hot Spot Harmonization Prototype"), "version": APP_VERSION},
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": {k: ds.get(k) for k in ("dataset_id", "kind", "is_sample", "label", "bbox", "start", "end",
                                           "sources", "segments", "products_versions", "duplicates_removed",
                                           "warnings", "normalize_reports", "chunk_stats", "n_chunks",
                                           "n_errors", "errors", "availability")},
        "quality_filters": summary["quality_params"],
        "quality_summary": summary["quality_summary"],
        "algorithm_parameters": summary["match_params"],
        "firea_virtual_modis": ({"model": summary["virtual_modis"].get("model"),
                                 "totals": summary["virtual_modis"].get("totals"),
                                 "conditions": L("Δt=0; MODIS geometrisi eğitim dağılımından 16 temsilci üzerinden ortalandı; "
                                                 "girdi tüm VIIRS güven sınıfları (statik kaynaklar hariç).",
                                                 "Δt=0; MODIS geometry averaged over 16 representatives from the training distribution; "
                                                 "input is all VIIRS confidence classes (static sources excluded).")}
                                if summary.get("virtual_modis", {}).get("available") else
                                {"available": False, "reason": summary.get("virtual_modis", {}).get("reason")}),
        "stats": summary["stats"],
        "time_note": summary["time_note"],
        "caveats": [
            L("Eşikler deneysel parametrelerdir, bilimsel olarak doğrulanmamıştır.", "Thresholds are experimental parameters and have not been scientifically validated."),
            L("Aday olaylar gözlem kümeleridir; ilk/son gözlem gerçek başlangıç/sönme zamanı değildir.", "Candidate events are groups of observations; first/last observation is not the real ignition/extinction time."),
            L("Olay sınır kutuları yanmış alan değildir.", "Event bounding boxes are not burned areas."),
            L("Tespit kaydı olmayan gün yangın yokluğunu doğrulamaz (bulut/gözlem fırsatı bilgisi yok).", "A day without detections does not verify the absence of fire (no cloud/observation-opportunity information)."),
            L("FRP değerleri farklı zamanlar arasında toplanmamıştır.", "FRP values are never summed across different times."),
        ],
    }, ensure_ascii=False, indent=1, default=str)
