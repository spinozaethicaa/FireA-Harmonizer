from __future__ import annotations

import io
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
import pandas as pd

import contextvars

from .config import CACHE_DIR, FIRMS_BASE_URL, L, get_map_key, mask_secret

log = logging.getLogger("app.firms")

SUPPORTED_SOURCES = {
    "MODIS_NRT": {"instrument": "MODIS", "family": "NRT"},
    "MODIS_SP": {"instrument": "MODIS", "family": "SP"},
    "VIIRS_NOAA20_NRT": {"instrument": "VIIRS", "family": "NRT"},
    "VIIRS_NOAA20_SP": {"instrument": "VIIRS", "family": "SP"},
    "VIIRS_SNPP_NRT": {"instrument": "VIIRS", "family": "NRT"},
    "VIIRS_SNPP_SP": {"instrument": "VIIRS", "family": "SP"},
    "VIIRS_NOAA21_NRT": {"instrument": "VIIRS", "family": "NRT"},
    "VIIRS_NOAA21_SP": {"instrument": "VIIRS", "family": "SP"},
}

MAX_DAY_RANGE = 5
MAX_TOTAL_DAYS = 3660
RATE_LIMIT_WAIT_S = 65
RATE_LIMIT_RETRIES = 12
REQUEST_TIMEOUT_S = 60.0
NRT_RECENT_TTL_S = 20 * 60


class FirmsError(Exception):

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(mask_secret(message))
        self.kind = kind
        self.status = status
        self.message = mask_secret(message)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "status": self.status}


class ResponseCache:
    def __init__(self, path=None):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = str(path or (CACHE_DIR / "firms_cache.sqlite"))
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS area_cache (
                       cache_key TEXT PRIMARY KEY, source TEXT, bbox TEXT,
                       start_date TEXT, day_range INTEGER, body TEXT,
                       fetched_at REAL, expires_at REAL)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS availability_cache (
                       sensor TEXT PRIMARY KEY, body TEXT, fetched_at REAL)"""
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_area(self, key: str) -> tuple[str, float] | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT body, fetched_at, expires_at FROM area_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        body, fetched_at, expires_at = row
        if expires_at is not None and expires_at < time.time():
            return None
        return body, fetched_at

    def put_area(self, key, source, bbox, start, day_range, body, ttl: float | None):
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO area_cache VALUES (?,?,?,?,?,?,?,?)",
                (key, source, bbox, start, day_range, body, now, (now + ttl) if ttl else None),
            )

    def get_availability(self, sensor: str, max_age_s: float = 3600) -> str | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT body, fetched_at FROM availability_cache WHERE sensor=?", (sensor,)
            ).fetchone()
        if row and time.time() - row[1] < max_age_s:
            return row[0]
        return None

    def put_availability(self, sensor: str, body: str):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO availability_cache VALUES (?,?,?)",
                (sensor, body, time.time()),
            )


def bbox_str(bbox: tuple[float, float, float, float]) -> str:
    w, s, e, n = bbox
    return ",".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in (w, s, e, n))


def validate_bbox(bbox) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(v) for v in bbox)
    except Exception as exc:
        raise FirmsError("bad_request", L("Bölge kutusu 4 sayıdan oluşmalı: west,south,east,north", "The area box must have 4 numbers: west,south,east,north")) from exc
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise FirmsError("bad_request", L("Geçersiz bölge kutusu: west<east, south<north ve sınırlar içinde olmalı.", "Invalid area box: needs west<east, south<north and values within bounds."))
    return w, s, e, n


def split_date_range(start: date, end: date, max_days: int = MAX_DAY_RANGE) -> list[tuple[date, int]]:
    out = []
    cur = start
    while cur <= end:
        n = min(max_days, (end - cur).days + 1)
        out.append((cur, n))
        cur += timedelta(days=n)
    return out


def _classify_text_error(body: str, status: int) -> FirmsError | None:
    low = body.strip().lower()[:500]
    if "invalid map_key" in low or "invalid key" in low or "map_key is needed" in low:
        return FirmsError("invalid_key", L("FIRMS MAP_KEY geçersiz veya eksik.", "FIRMS MAP_KEY is invalid or missing."), status)
    if "exceeding allowed transaction limit" in low or "transaction limit" in low or status == 429:
        return FirmsError("rate_limit", L("FIRMS işlem sınırı aşıldı (5000 işlem / 10 dk). Birkaç dakika sonra tekrar deneyin.", "FIRMS transaction limit exceeded (5000 per 10 min). Try again in a few minutes."), status)
    if "invalid" in low and ("source" in low or "sensor" in low):
        return FirmsError("bad_request", L("FIRMS isteği reddetti: ", "FIRMS rejected the request: ") + body.strip()[:200], status)
    return None


@dataclass
class ChunkResult:
    source: str
    start: date
    day_range: int
    status: str
    rows: int = 0
    from_cache: bool = False
    error: dict | None = None
    note: str | None = None

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.day_range)]

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "start": self.start.isoformat(),
            "day_range": self.day_range,
            "status": self.status,
            "rows": self.rows,
            "from_cache": self.from_cache,
            "error": self.error,
            "note": self.note,
        }


@dataclass
class FetchOutcome:
    frames: list[pd.DataFrame] = field(default_factory=list)
    chunks: list[ChunkResult] = field(default_factory=list)


class FirmsClient:
    def __init__(self, cache: ResponseCache | None = None, transport: httpx.BaseTransport | None = None,
                 map_key: str | None = None, pause_s: float = 0.2, workers: int = 3):
        self.cache = cache or ResponseCache()
        self._transport = transport
        self._explicit_key = map_key
        self.pause_s = pause_s
        self.workers = workers

    def _key(self) -> str:
        key = self._explicit_key or get_map_key()
        if not key:
            raise FirmsError("no_key", L("FIRMS MAP_KEY yapılandırılmamış (.env içinde FIRMS_MAP_KEY veya MAP_KEY).", "FIRMS MAP_KEY is not configured (FIRMS_MAP_KEY or MAP_KEY in .env)."))
        return key

    def _get(self, path: str) -> str:
        url = f"{FIRMS_BASE_URL}{path}"
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=self._transport, follow_redirects=True) as client:
                resp = client.get(url)
        except httpx.TimeoutException as exc:
            raise FirmsError("timeout", L(f"FIRMS isteği zaman aşımına uğradı ({REQUEST_TIMEOUT_S:.0f} sn).", f"FIRMS request timed out ({REQUEST_TIMEOUT_S:.0f} s).")) from exc
        except httpx.HTTPError as exc:
            raise FirmsError("network", L("FIRMS'e bağlanılamadı: ", "Could not connect to FIRMS: ") + mask_secret(str(exc))) from exc
        body = resp.text
        err = _classify_text_error(body, resp.status_code)
        if err:
            raise err
        if resp.status_code >= 400:
            raise FirmsError("http", f"FIRMS HTTP {resp.status_code}: {mask_secret(body.strip()[:200])}", resp.status_code)
        return body

    def availability(self, sensor: str = "ALL", use_cache: bool = True) -> list[dict]:
        body = self.cache.get_availability(sensor) if use_cache else None
        if body is None:
            body = self._get(f"/api/data_availability/csv/{self._key()}/{sensor}")
            self.cache.put_availability(sensor, body)
        return parse_availability(body)

    def plan_area(self, source: str, bbox, start: date, end: date,
                  availability: dict[str, dict] | None = None) -> tuple[list[ChunkResult], list[tuple[date, int]]]:
        if source not in SUPPORTED_SOURCES:
            raise FirmsError("bad_request", f"Desteklenmeyen kaynak: {source}")
        validate_bbox(bbox)
        if end < start:
            raise FirmsError("bad_request", L("Bitiş tarihi başlangıçtan önce olamaz.", "The end date cannot be before the start date."))
        if (end - start).days + 1 > MAX_TOTAL_DAYS:
            raise FirmsError("bad_request", L(f"Tek sorguda en fazla {MAX_TOTAL_DAYS} gün indirilebilir.", f"At most {MAX_TOTAL_DAYS} days can be downloaded in one query."))
        avail = (availability or {}).get(source)
        a_min = date.fromisoformat(avail["min_date"]) if avail and avail.get("min_date") else None
        a_max = date.fromisoformat(avail["max_date"]) if avail and avail.get("max_date") else None
        oor: list[ChunkResult] = []
        in_range: list[date] = []
        d = start
        while d <= end:
            if (a_min and d < a_min) or (a_max and d > a_max):
                oor.append(ChunkResult(source, d, 1, "out_of_range",
                                       note=L(f"{source} kullanılabilir aralığı: {a_min}..{a_max}", f"{source} available range: {a_min}..{a_max}")))
            else:
                in_range.append(d)
            d += timedelta(days=1)
        chunks: list[tuple[date, int]] = []
        if in_range:
            blocks: list[tuple[date, date]] = []
            bs = prev = in_range[0]
            for d in in_range[1:]:
                if (d - prev).days != 1:
                    blocks.append((bs, prev))
                    bs = d
                prev = d
            blocks.append((bs, prev))
            for b_start, b_end in blocks:
                chunks.extend(split_date_range(b_start, b_end))
        return oor, chunks

    def fetch_area(self, source: str, bbox, start: date, end: date,
                   availability: dict[str, dict] | None = None,
                   on_progress=None, should_cancel=None) -> FetchOutcome:
        bbox = validate_bbox(bbox)
        bstr = bbox_str(bbox)
        oor, plan = self.plan_area(source, bbox, start, end, availability)
        out = FetchOutcome(chunks=list(oor))
        today = datetime.now(timezone.utc).date()
        fatal: dict = {}
        cancelled = lambda: bool(fatal) or bool(should_cancel and should_cancel())

        def one(item):
            c_start, n = item
            if cancelled():
                err = fatal.get("err") or {"kind": "cancelled", "message": L("Kullanıcı iptal etti.", "Cancelled by the user."), "status": None}
                return ChunkResult(source, c_start, n, "error", error=err,
                                   note=L("Önceki hata nedeniyle istenmedi.", "Not requested because of an earlier error.") if fatal else None), None
            ckey = f"{source}|{bstr}|{c_start.isoformat()}|{n}"
            cached = self.cache.get_area(ckey)
            from_cache = cached is not None
            body = cached[0] if cached else None
            if body is None:
                if self.pause_s:
                    time.sleep(self.pause_s)
                for attempt in range(RATE_LIMIT_RETRIES + 1):
                    try:
                        body = self._get(f"/api/area/csv/{self._key()}/{source}/{bstr}/{n}/{c_start.isoformat()}")
                        break
                    except FirmsError as exc:
                        if exc.kind == "rate_limit" and attempt < RATE_LIMIT_RETRIES and not cancelled():
                            if on_progress:
                                on_progress(source, None, L(f"FIRMS işlem sınırı: {RATE_LIMIT_WAIT_S} sn bekleniyor (deneme {attempt + 1}/{RATE_LIMIT_RETRIES})",
                                                            f"FIRMS rate limit: waiting {RATE_LIMIT_WAIT_S} s (attempt {attempt + 1}/{RATE_LIMIT_RETRIES})"))
                            self._sleep(RATE_LIMIT_WAIT_S, should_cancel)
                            continue
                        if exc.kind in ("invalid_key", "no_key", "rate_limit"):
                            fatal["err"] = exc.to_dict()
                        return ChunkResult(source, c_start, n, "error", error=exc.to_dict()), None
            try:
                df = parse_area_csv(body)
            except FirmsError as exc:
                return ChunkResult(source, c_start, n, "error", error=exc.to_dict()), None
            if not from_cache:
                c_end = c_start + timedelta(days=n - 1)
                recent = SUPPORTED_SOURCES[source]["family"] == "NRT" and c_end >= today - timedelta(days=2)
                self.cache.put_area(ckey, source, bstr, c_start.isoformat(), n, body,
                                    NRT_RECENT_TTL_S if recent else None)
            df["_source"] = source
            return ChunkResult(source, c_start, n, "ok" if len(df) else "empty", rows=len(df), from_cache=from_cache), df

        def run(item):
            ch, df = one(item)
            if on_progress:
                on_progress(source, ch, None)
            return ch, df

        if self.workers > 1 and len(plan) > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs = [ex.submit(contextvars.copy_context().run, run, item) for item in plan]
                results = [f.result() for f in futs]
        else:
            results = [run(it) for it in plan]
        for ch, df in results:
            out.chunks.append(ch)
            if df is not None:
                out.frames.append(df)
        return out

    @staticmethod
    def _sleep(seconds: float, should_cancel=None):
        end_t = time.time() + seconds
        while time.time() < end_t:
            if should_cancel and should_cancel():
                return
            time.sleep(0.5)


def parse_availability(body: str) -> list[dict]:
    txt = body.strip()
    if not txt:
        return []
    try:
        df = pd.read_csv(io.StringIO(txt))
    except Exception as exc:
        raise FirmsError("bad_response", L("data_availability yanıtı CSV olarak okunamadı.", "The data_availability response could not be read as CSV.")) from exc
    cols = {c.lower(): c for c in df.columns}
    id_col = cols.get("data_id") or cols.get("sensor") or df.columns[0]
    min_col = cols.get("min_date")
    max_col = cols.get("max_date")
    if not (min_col and max_col):
        raise FirmsError("bad_response", L("data_availability beklenmeyen sütunlar: ", "data_availability unexpected columns: ") + str(list(df.columns)))
    return [
        {"data_id": str(r[id_col]), "min_date": str(r[min_col])[:10], "max_date": str(r[max_col])[:10]}
        for _, r in df.iterrows()
    ]


def parse_area_csv(body: str) -> pd.DataFrame:
    txt = body.lstrip("﻿").strip()
    if not txt:
        return pd.DataFrame()
    first = txt.splitlines()[0].lower()
    if "latitude" not in first or "acq_date" not in first:
        raise FirmsError("bad_response", L("FIRMS yanıtı beklenen CSV değil: ", "The FIRMS response is not the expected CSV: ") + mask_secret(txt[:160]))
    return pd.read_csv(io.StringIO(txt), dtype=str, keep_default_na=False)


def availability_index(rows: list[dict]) -> dict[str, dict]:
    return {r["data_id"]: r for r in rows}


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
