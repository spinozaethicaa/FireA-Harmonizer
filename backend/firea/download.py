from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import FIRMS_BASE_URL, get_map_key
from app.firms import FirmsClient, FirmsError, ResponseCache
from firea.regions import REGIONS, SOURCES, all_days

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_PATH = DATA_DIR / "firms_train_cache.sqlite"
MANIFEST_PATH = DATA_DIR / "manifest.json"


def make_client() -> FirmsClient:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return FirmsClient(cache=ResponseCache(CACHE_PATH), pause_s=0.0, workers=1)


def key_status() -> dict:
    try:
        r = httpx.get(f"{FIRMS_BASE_URL}/mapserver/mapkey_status/", params={"MAP_KEY": get_map_key()}, timeout=30)
        j = r.json()
        return {"current_transactions": j.get("current_transactions"), "transaction_limit": j.get("transaction_limit")}
    except Exception:
        return {}


def tasks():
    for reg in REGIONS:
        for d in all_days(reg):
            for src in SOURCES:
                yield reg, d, src


def main(workers: int = 5):
    client = make_client()
    all_tasks = list(tasks())
    print(f"{len(all_tasks)} istek (bölge×gün×ürün). Kota: {key_status()}", flush=True)
    manifest = []
    t0 = time.time()
    done = 0

    def run(task):
        reg, d, src = task
        out = client.fetch_area(src, reg.bbox, d, d)
        ch = out.chunks[0]
        return {"region": reg.key, "date": d.isoformat(), "source": src, "status": ch.status,
                "rows": ch.rows, "from_cache": ch.from_cache, "error": ch.error}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run, t) for t in all_tasks]
        for f in as_completed(futs):
            try:
                rec = f.result()
            except FirmsError as exc:
                rec = {"status": "error", "error": exc.to_dict()}
            manifest.append(rec)
            done += 1
            if done % 50 == 0 or done == len(all_tasks):
                rows = sum(m.get("rows", 0) for m in manifest)
                errs = sum(1 for m in manifest if m.get("status") == "error")
                print(f"{done}/{len(all_tasks)} · {rows:,} satır · {errs} hata · {time.time() - t0:.0f} sn", flush=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    errs = [m for m in manifest if m.get("status") == "error"]
    print(f"Bitti. {len(errs)} hata. Kota: {key_status()}", flush=True)
    if errs:
        print("İlk hatalar:", errs[:3])


if __name__ == "__main__":
    main()
