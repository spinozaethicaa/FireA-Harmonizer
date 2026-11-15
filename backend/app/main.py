from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import analysis
from .analysis import (STORE, Dataset, analyze, compact_observations, coverage_from_chunks, coverage_from_files,
                       full_observation, new_dataset_id)
from .config import L, get_lang, get_map_key, install_log_masking, mask_secret, set_lang
from .firms import SUPPORTED_SOURCES, FirmsClient, FirmsError, availability_index
from .matching import MatchingCancelled, MatchParams
from .normalize import QualityParams, merge_observations, normalize_records, read_uploaded_csv
from .sample_data import SAMPLE_BBOX, SAMPLE_DAYS, SAMPLE_START, build_sample_frames

install_log_masking()
log = logging.getLogger("app")

app = FastAPI(title="MODIS-VIIRS Hot Spot Harmonization", version=analysis.APP_VERSION)
@app.middleware("http")
async def _lang_mw(request, call_next):
    set_lang(request.query_params.get("lang") or request.headers.get("x-lang"))
    return await call_next(request)


app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])

CLIENT = FirmsClient()


@app.exception_handler(FirmsError)
async def firms_error_handler(_, exc: FirmsError):
    status = {"no_key": 503, "invalid_key": 401, "rate_limit": 429, "timeout": 504, "network": 502,
              "bad_request": 400}.get(exc.kind, 502)
    return JSONResponse(status_code=status, content={"error": exc.to_dict()})


@app.exception_handler(HTTPException)
async def http_error_handler(_, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"kind": "http", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(Exception)
async def generic_error_handler(_, exc: Exception):
    log.exception("Beklenmeyen hata")
    return JSONResponse(status_code=500, content={"error": {"kind": "internal", "message": mask_secret(str(exc))}})


class QualityIn(BaseModel):
    modis_min_confidence: int = Field(30, ge=0, le=100)
    viirs_classes: list[str] = ["n", "h"]
    daynight: str = "all"

    def to_params(self) -> QualityParams:
        cls = tuple(sorted({c for c in self.viirs_classes if c in ("l", "n", "h")}))
        return QualityParams(self.modis_min_confidence, cls, self.daynight if self.daynight in ("D", "N") else "all")


class MatchIn(BaseModel):
    cross_dist_km: float = Field(1.5, gt=0, le=20)
    cross_dt_min: float = Field(60, ge=0, le=720)
    use_pixel_size: bool = True
    same_pass_adj_km: float = Field(1.0, gt=0, le=10)
    pass_window_min: float = Field(10, gt=0, le=60)
    max_cluster_span_min: float = Field(90, gt=0, le=1440)
    max_cluster_extent_km: float = Field(10, gt=0, le=200)
    cont_dist_km: float = Field(2.0, gt=0, le=50)
    cont_gap_h: float = Field(48, gt=0, le=240)
    max_event_extent_km: float = Field(30, gt=0, le=500)

    def to_params(self) -> MatchParams:
        return MatchParams(**self.model_dump())


class FetchIn(BaseModel):
    mode: str = "nrt"
    viirs_satellite: str = "NOAA20"
    use_modis: bool = True
    use_viirs: bool = True
    bbox: list[float] = [27.0, 35.5, 32.5, 38.0]
    start: date
    end: date
    quality: QualityIn = QualityIn()
    match: MatchIn = MatchIn()


class AnalyzeIn(BaseModel):
    dataset_id: str
    quality: QualityIn = QualityIn()
    match: MatchIn = MatchIn()


class Job:
    def __init__(self):
        self.id = "JOB-" + uuid.uuid4().hex[:10]
        self.status = "running"
        self.phase = "plan"
        self.done = 0
        self.total = 0
        self.rows = 0
        self.message = L("Hazırlanıyor…", "Preparing…")
        self.result: dict | None = None
        self.error: dict | None = None
        self.cancel = False
        self.lock = threading.Lock()
        self.started = time.time()

    def set_phase(self, phase: str, message: str):
        if self.status == "running":
            self.phase, self.message = phase, message

    def should_cancel(self) -> bool:
        return self.cancel

    def finish(self, result: dict):
        if self.status == "running":
            self.result, self.status, self.message = result, "done", L("Tamamlandı.", "Done.")

    def fail(self, error: dict):
        if self.status == "running":
            self.error, self.status = error, "error"

    def to_dict(self):
        return {"job_id": self.id, "status": self.status, "phase": self.phase, "done": self.done,
                "total": self.total, "rows": self.rows, "message": self.message,
                "elapsed_s": round(time.time() - self.started, 1),
                "result": self.result if self.status == "done" else None, "error": self.error}


def run_job(job: "Job", fn):
    try:
        job.finish(fn())
    except MatchingCancelled:
        job.status = "cancelled"
    except FirmsError as exc:
        job.fail(exc.to_dict())
    except Exception as exc:
        log.exception("Arka plan işi başarısız")
        job.fail({"kind": "internal", "message": mask_secret(str(exc))})


def start_job(fn) -> "Job":
    job = Job()
    for k in [k for k, j in JOBS.items() if j.status != "running" and time.time() - j.started > 3600]:
        JOBS.pop(k, None)
    JOBS[job.id] = job
    lang = get_lang()

    def target():
        set_lang(lang)
        run_job(job, lambda: fn(job))

    threading.Thread(target=target, daemon=True).start()
    return job


JOBS: dict[str, Job] = {}


def plan_segments(req: FetchIn, avail: dict | None) -> tuple[dict[str, list[tuple[str, date, date]]], list[str]]:
    viirs_sat = req.viirs_satellite if req.viirs_satellite in ("NOAA20", "SNPP", "NOAA21") else "NOAA20"
    prefixes = {}
    if req.use_modis:
        prefixes["MODIS"] = "MODIS"
    if req.use_viirs:
        prefixes["VIIRS"] = f"VIIRS_{viirs_sat}"
    if not prefixes:
        raise FirmsError("bad_request", L("En az bir sensör seçilmeli.", "Select at least one sensor."))
    notes = []
    segs: dict[str, list[tuple[str, date, date]]] = {}
    for instr, pre in prefixes.items():
        if req.mode == "nrt":
            segs[instr] = [(f"{pre}_NRT", req.start, req.end)]
        elif req.mode == "sp":
            segs[instr] = [(f"{pre}_SP", req.start, req.end)]
        elif req.mode == "combined":
            sp = (avail or {}).get(f"{pre}_SP")
            if not sp:
                raise FirmsError("bad_request", L(f"{pre}_SP kullanılabilirliği bilinmiyor; birleşik mod kullanılamaz.", f"{pre}_SP availability is unknown; combined mode cannot be used."))
            sp_max = date.fromisoformat(sp["max_date"])
            s = []
            if req.start <= sp_max:
                s.append((f"{pre}_SP", req.start, min(req.end, sp_max)))
            if req.end > sp_max:
                s.append((f"{pre}_NRT", max(req.start, sp_max + timedelta(days=1)), req.end))
            segs[instr] = s
            if len(s) == 2:
                notes.append(f"{instr}: {s[0][1]}–{s[0][2]} {s[0][0]} (standart), {s[1][1]}–{s[1][2]} {s[1][0]} (NRT). "
                             f"İki ürün açıkça ayrılmıştır; her gözlemin 'source' alanı kullandığı ürünü gösterir.")
        else:
            raise FirmsError("bad_request", f"Bilinmeyen mod: {req.mode}")
        for src, _, _ in segs[instr]:
            if src not in SUPPORTED_SOURCES:
                raise FirmsError("bad_request", f"Desteklenmeyen kaynak: {src}")
    return segs, notes


def run_fetch_job(job: Job, req: FetchIn) -> dict:
    if True:
        warnings: list[str] = []
        avail_idx = None
        avail_err = None
        job.message = L("Ürün kullanılabilirliği kontrol ediliyor…", "Checking product availability…")
        try:
            avail_idx = availability_index(CLIENT.availability("ALL"))
        except FirmsError as exc:
            if exc.kind in ("invalid_key", "no_key"):
                raise
            avail_err = exc.to_dict()
            warnings.append(L(f"data_availability kontrol edilemedi ({exc.message}); tarih kapsamı doğrulanmadı.", f"data_availability could not be checked ({exc.message}); date coverage not verified."))

        segs, notes = plan_segments(req, avail_idx)
        warnings.extend(notes)
        total = 0
        for instr_segs in segs.values():
            for src, s, e in instr_segs:
                _, plan = CLIENT.plan_area(src, req.bbox, s, e, avail_idx)
                total += len(plan)
        job.total = total
        job.phase = "download"
        job.message = L(f"{total} istek parçası indirilecek (önbellekte olanlar hızlı gelir).", f"{total} request chunks to download (cached ones are fast).")

        def on_progress(source, chunk, message):
            with job.lock:
                if chunk is not None:
                    job.done += 1
                    job.rows += chunk.rows
                    job.message = f"{source} {chunk.start} (+{chunk.day_range} {L('gün', 'd')}): " + (
                        {"ok": L(f"{chunk.rows} kayıt", f"{chunk.rows} records"), "empty": L("sıfır tespit", "zero detections")}.get(chunk.status, L("hata", "error")))
                if message:
                    job.message = message

        def fetch_instr(instr_segs):
            outs = []
            for src, s, e in instr_segs:
                outs.append((src, CLIENT.fetch_area(src, req.bbox, s, e, availability=avail_idx,
                                                    on_progress=on_progress, should_cancel=lambda: job.cancel)))
            return outs

        with ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(fetch_instr, segs.values()))
        if job.cancel:
            raise MatchingCancelled()

        job.phase = "normalize"
        job.message = L("Kayıtlar normalize ediliyor…", "Normalizing records…")
        obs = None
        reports, chunks = [], []
        dup = 0
        for outs in results:
            for src, outcome in outs:
                chunks.extend(outcome.chunks)
                n_oor = sum(1 for c in outcome.chunks if c.status == "out_of_range")
                if n_oor:
                    a = (avail_idx or {}).get(src) or {}
                    warnings.append(f"{src}: istenen {n_oor} gün ürünün kullanılabilir aralığı dışında "
                                    f"({a.get('min_date')} – {a.get('max_date')}); bu günler indirilmedi, başka tarihe geçilmedi.")
                if outcome.frames:
                    raw = pd.concat(outcome.frames, ignore_index=True)
                    norm, rep = normalize_records(raw, origin="api")
                    reports.append({"source": src, **rep.to_dict()})
                    obs, d = merge_observations(obs, norm)
                    dup += d
        if obs is None:
            obs, _ = normalize_records(pd.DataFrame(), origin="api")

        cov, cov_src = coverage_from_chunks(chunks)
        sources = {instr: " + ".join(s for s, _, _ in v) for instr, v in segs.items()}
        mode_label = {"nrt": "NRT", "sp": L("Standart (SP)", "Standard (SP)"), "combined": L("SP + NRT birleşik", "SP + NRT combined")}[req.mode]
        ds = Dataset(
            dataset_id=new_dataset_id(), kind="api", label=f"FIRMS API · {mode_label}",
            bbox=tuple(req.bbox), start=req.start, end=req.end, sources=sources, observations=obs,
            coverage=cov, coverage_source=cov_src, normalize_reports=reports,
            chunks=[c.to_dict() for c in chunks], duplicates_removed=dup, warnings=warnings,
            availability={"sources": {s: (avail_idx or {}).get(s) for v in segs.values() for s, _, _ in v},
                          "error": avail_err},
            segments=[{"instrument": i, "source": s, "start": a.isoformat(), "end": b.isoformat()}
                      for i, v in segs.items() for s, a, b in v],
        )
        STORE.add(ds)
        r = analyze(ds, req.quality.to_params(), req.match.to_params(),
                    should_cancel=job.should_cancel, on_phase=job.set_phase)
        return r["summary"]


@app.get("/api/health")
def health():
    return {"ok": True, "version": analysis.APP_VERSION, "map_key_configured": get_map_key() is not None}


@app.get("/api/availability")
def availability(refresh: bool = False):
    rows = CLIENT.availability("ALL", use_cache=not refresh)
    return {"products": availability_index(rows)}


@app.post("/api/fetch")
def fetch(req: FetchIn):
    if req.end < req.start:
        raise FirmsError("bad_request", L("Bitiş tarihi başlangıçtan önce olamaz.", "The end date cannot be before the start date."))
    from .firms import MAX_TOTAL_DAYS, validate_bbox
    validate_bbox(req.bbox)
    if (req.end - req.start).days + 1 > MAX_TOTAL_DAYS:
        raise FirmsError("bad_request", L(f"Tek sorguda en fazla {MAX_TOTAL_DAYS} gün indirilebilir.", f"At most {MAX_TOTAL_DAYS} days can be downloaded in one query."))
    if get_map_key() is None:
        raise FirmsError("no_key", L("FIRMS MAP_KEY yapılandırılmamış.", "FIRMS MAP_KEY is not configured."))
    job = start_job(lambda j: run_fetch_job(j, req))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("İş bulunamadı.", "Job not found.")})
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("İş bulunamadı.", "Job not found.")})
    job.cancel = True
    if job.status == "running":
        job.status = "cancelled"
        job.message = L("İptal edildi.", "Cancelled.")
    return {"ok": True}


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    obs = None
    reports = []
    dup = 0
    warnings = []
    for f in files:
        content = await f.read()
        try:
            raw = read_uploaded_csv(content)
            norm, rep = normalize_records(raw, origin=f"upload:{f.filename}")
        except Exception as exc:
            raise HTTPException(400, detail={"kind": "bad_file", "message": f"{f.filename}: {exc}"}) from exc
        reports.append({"file": f.filename, **rep.to_dict()})
        obs, d = merge_observations(obs, norm)
        dup += d
    if obs is None or obs.empty:
        raise HTTPException(400, detail={"kind": "empty_file", "message": L("Yüklenen dosyalarda geçerli kayıt bulunamadı.", "No valid records found in the uploaded files.")})
    cov, dmin, dmax = coverage_from_files(obs, "uploaded")
    for missing in {"MODIS", "VIIRS"} - set(obs["instrument"]):
        warnings.append(L(f"Yüklenen dosyalarda {missing} kaydı yok; takvimde {missing} 'veri yok' görünür.", f"The uploaded files contain no {missing} records; {missing} appears as 'no data' in the calendar."))
    warnings.append(L("Dosya kapsamı, dosyadaki ilk ve son tespit tarihinden çıkarıldı (varsayım).", "File coverage was inferred from the first and last detection dates in the file (assumption)."))
    srcs = {instr: ", ".join(sorted(set(g["source"]))) for instr, g in obs.groupby("instrument")}
    if len({s.split("_")[-1] for s in obs["source"]}) > 1:
        warnings.append(L("Yüklenen dosyalarda NRT ve SP ürünleri birlikte var.", "The uploaded files contain both NRT and SP products."))
    ds = Dataset(dataset_id=new_dataset_id(), kind="upload",
                 label=L("Yüklenen CSV: ", "Uploaded CSV: ") + ", ".join(f.filename or "?" for f in files),
                 bbox=None, start=dmin, end=dmax, sources=srcs, observations=obs, coverage=cov,
                 normalize_reports=reports, duplicates_removed=dup, warnings=warnings)
    STORE.add(ds)
    return {"dataset_id": ds.dataset_id}


@app.post("/api/sample")
def sample():
    mdf, vdf = build_sample_frames()
    obs = None
    reports = []
    for name, raw in (("MODIS (sentetik)", mdf), ("VIIRS (sentetik)", vdf)):
        norm, rep = normalize_records(raw, origin="sample")
        reports.append({"file": name, **rep.to_dict()})
        obs, _ = merge_observations(obs, norm)
    start = date.fromisoformat(SAMPLE_START)
    end = start + timedelta(days=SAMPLE_DAYS - 1)
    cov = {i: {(start + timedelta(days=k)).isoformat(): "sample" for k in range(SAMPLE_DAYS)} for i in ("MODIS", "VIIRS")}
    ds = Dataset(dataset_id=new_dataset_id(), kind="sample", label=L("ÖRNEK VERİ (sentetik, gerçek gözlem değil)", "SAMPLE DATA (synthetic, not real observations)"),
                 bbox=SAMPLE_BBOX, start=start, end=end,
                 sources={"MODIS": L("ÖRNEK (MODIS_NRT biçimi)", "SAMPLE (MODIS_NRT format)"), "VIIRS": L("ÖRNEK (VIIRS_NOAA20_NRT biçimi)", "SAMPLE (VIIRS_NOAA20_NRT format)")},
                 observations=obs, coverage=cov, normalize_reports=reports,
                 warnings=[L("Bu veri sentetiktir; yalnızca arayüz/algoritma denemesi içindir.", "This data is synthetic and only for trying out the interface and algorithm.")])
    STORE.add(ds)
    return {"dataset_id": ds.dataset_id}


@app.post("/api/analyze_job")
def analyze_job(req: AnalyzeIn):
    ds = STORE.get(req.dataset_id)
    if not ds:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("Veri seti bulunamadı (sunucu yeniden başlatılmış olabilir).", "Dataset not found (the server may have restarted).")})
    q, m = req.quality.to_params(), req.match.to_params()
    job = start_job(lambda j: analyze(ds, q, m, should_cancel=j.should_cancel, on_phase=j.set_phase)["summary"])
    job.set_phase("analyze", L("Analiz başlatıldı…", "Analysis started…"))
    return {"job_id": job.id}


class ClimatologyIn(BaseModel):
    bbox: list[float] = [27.0, 35.5, 32.5, 38.0]
    refresh: bool = False


@app.post("/api/climatology_job")
def climatology_job(req: ClimatologyIn):
    from .firms import validate_bbox
    from .history import run_climatology
    bbox = validate_bbox(req.bbox)
    if get_map_key() is None:
        raise FirmsError("no_key", L("FIRMS MAP_KEY yapılandırılmamış.", "FIRMS MAP_KEY is not configured."))
    job = start_job(lambda j: run_climatology(CLIENT, bbox, j, refresh=req.refresh))
    job.set_phase("plan", L("Yıllar ve ürün kapsamı belirleniyor…", "Determining years and product coverage…"))
    return {"job_id": job.id}


@app.get("/api/report")
def report(key: str):
    from .report import build_report
    r = _result(key)
    pdf = build_report(r)
    fn = f"{key.split(':')[0]}_ozet_rapor.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/analyze")
def run_analyze(req: AnalyzeIn):
    ds = STORE.get(req.dataset_id)
    if not ds:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("Veri seti bulunamadı (sunucu yeniden başlatılmış olabilir).", "Dataset not found (the server may have restarted).")})
    return analyze(ds, req.quality.to_params(), req.match.to_params())["summary"]


def _result(key: str) -> dict:
    r = STORE.get_result(key)
    if r is None:
        raise HTTPException(404, detail={"kind": "analysis_expired", "message": L("Analiz sonucu bellekte yok; yeniden hesaplanmalı.", "The analysis result is no longer in memory; it must be recomputed.")})
    return r


@app.get("/api/observations")
def observations(key: str, day: str | None = None, event_id: str | None = None, include_excluded: bool = True,
                 limit: int = analysis.MAP_POINT_LIMIT):
    m = _result(key)["merged"]
    if day:
        m = m[m["_day"] == day]
    if event_id:
        m = m[m["event_id"] == event_id]
    if not include_excluded and len(m):
        m = m[m["quality_pass"]]
    total = int(len(m))
    if total > limit:
        return {"total": total, "truncated": True, "observations": []}
    return {"total": total, "truncated": False, "observations": compact_observations(m)}


@app.get("/api/virtual_modis")
def virtual_modis(key: str, day: str | None = None):
    vm = _result(key)["vm"]
    if not vm.get("available"):
        return {"available": False, "reason": vm.get("reason"), "clusters": []}
    cl = vm["clusters"]
    if day:
        cl = [c for c in cl if c["date"] == day]
    return {"available": True, "clusters": cl, "n_total": vm.get("n_clusters", 0)}


@app.get("/api/firea/model")
def firea_model():
    from .firea_infer import load_model, model_summary
    model, cfg, met = load_model()
    if model is None:
        return {"available": False, "reason": met}
    return {"available": True, "model": model_summary(cfg, met), "best_epoch": cfg.get("best_epoch"),
            "arch": cfg.get("arch"), "context_features": cfg.get("context_features"),
            "token_features": cfg.get("token_features")}


@app.get("/api/docs/model_card")
def docs_model_card():
    from .firea_infer import _find_model_dir
    d = _find_model_dir()
    card = d / L("MODEL_CARD.tr.md", "MODEL_CARD.md") if d is not None else None
    if card is not None and not card.exists():
        card = d / "MODEL_CARD.md"
    if card is None or not card.exists():
        raise HTTPException(404, detail={"kind": "not_found", "message": L("Model kartı bulunamadı.", "Model card not found.")})
    return PlainTextResponse(card.read_text(encoding="utf-8"))


@app.get("/api/event")
def event_detail(key: str, event_id: str):
    r = _result(key)
    ev = r["events"].get(event_id)
    if not ev:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("Olay bulunamadı.", "Event not found.")})
    members = r["merged"][r["merged"]["event_id"] == event_id]
    return {"event": ev, "members": compact_observations(members)}


@app.get("/api/observation")
def observation_detail(key: str, id: str):
    r = _result(key)
    m = r["merged"]
    row = m[m["observation_id"] == id]
    if row.empty:
        raise HTTPException(404, detail={"kind": "not_found", "message": L("Gözlem bulunamadı.", "Observation not found.")})
    links = r["links"]
    if len(links):
        sel = links[(links["modis_observation_id"] == id) | (links["viirs_observation_id"] == id)]
        sel = sel.astype(object).where(sel.notna(), None).to_dict("records")
    else:
        sel = []
    return {"observation": full_observation(row.iloc[0]), "links": sel}


@app.get("/api/export")
def export(key: str, kind: str):
    r = _result(key)
    base = key.split(":")[0]
    if kind == "observations":
        body, mt, fn = analysis.export_observations_csv(r["merged"].drop(columns=["_day"])), "text/csv", f"{base}_gozlemler.csv"
    elif kind == "events":
        body, mt, fn = analysis.export_events_csv(r["events_list"]), "text/csv", f"{base}_aday_olaylar.csv"
    elif kind == "links":
        body, mt, fn = analysis.export_links_json(r), "application/json", f"{base}_eslestirmeler.json"
    elif kind == "virtual_modis":
        vm = r["vm"]
        if not vm.get("available"):
            raise HTTPException(400, detail={"kind": "bad_request", "message": vm.get("reason", L("FireA kullanılamıyor.", "FireA is unavailable."))})
        import pandas as _pd
        daily = _pd.DataFrame(vm["daily"])
        daily.insert(0, "model", vm["model"]["name"])
        body, mt, fn = daily.to_csv(index=False), "text/csv", f"{base}_modis_gibi_gunluk.csv"
    elif kind == "metadata":
        body, mt, fn = analysis.export_metadata_json(r["summary"]), "application/json", f"{base}_metaveri.json"
    else:
        raise HTTPException(400, detail={"kind": "bad_request", "message": L("Bilinmeyen dışa aktarım türü.", "Unknown export type.")})
    body = mask_secret(body)
    return Response(content=body.encode("utf-8-sig" if mt == "text/csv" else "utf-8"), media_type=mt,
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})
