from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

YEARS = range(2019, 2025)
DAYS_PER_REGION = 26
YEAR_ROUND_PER_MONTH = 2
SEED = 20261113
SOURCES = ("MODIS_SP", "VIIRS_SNPP_SP", "VIIRS_NOAA20_SP")


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    bbox: tuple[float, float, float, float]
    months: tuple[int, ...]
    biome: str
    split: str = "train"


REGIONS: list[Region] = [
    Region("east_med", "Doğu Akdeniz (Türkiye, Yunanistan)", (19, 34, 45, 43), (6, 7, 8, 9), "Akdeniz çalılık/orman", "test"),
    Region("iberia", "İber Yarımadası", (-10, 36, 4, 44), (6, 7, 8, 9, 10), "Akdeniz çalılık/orman"),
    Region("california", "Kaliforniya", (-125, 32, -114, 42), (7, 8, 9, 10, 11), "Akdeniz çalılık/orman"),
    Region("canada", "Kanada boreal", (-125, 52, -100, 62), (5, 6, 7, 8, 9), "Boreal orman"),
    Region("siberia", "Sibirya", (100, 50, 130, 62), (4, 5, 6, 7, 8, 9), "Boreal orman", "test"),
    Region("amazon", "Amazon yay", (-65, -15, -48, -5), (7, 8, 9, 10), "Tropikal orman / ormansızlaşma"),
    Region("cerrado", "Cerrado", (-50, -22, -42, -12), (7, 8, 9, 10), "Tropikal savan"),
    Region("central_africa", "Orta Afrika (Angola, Zambiya, KDC)", (15, -15, 30, -5), (5, 6, 7, 8, 9), "Savan"),
    Region("sahel", "Batı Afrika / Sahel", (-10, 7, 10, 14), (11, 12, 1, 2), "Savan"),
    Region("se_asia", "Güneydoğu Asya (Myanmar, Tayland, Laos)", (95, 15, 106, 23), (2, 3, 4), "Tropikal kuru orman / tarım"),
    Region("north_australia", "Kuzey Avustralya", (125, -20, 145, -12), (5, 6, 7, 8, 9, 10, 11), "Savan", "test"),
    Region("punjab", "Hindistan / Pencap", (73, 28, 78, 32), (4, 5, 10, 11), "Tarım yakmaları"),
    Region("east_europe", "Doğu Avrupa bozkırı", (28, 44, 45, 54), (3, 4, 5, 7, 8, 9), "Tarım / bozkır"),
    Region("mexico", "Meksika / Orta Amerika", (-100, 14, -86, 22), (3, 4, 5), "Tropikal kuru orman"),
    Region("indonesia", "Endonezya (Sumatra, Kalimantan)", (100, -5, 118, 3), (8, 9, 10), "Turbalık / tropikal orman"),
]


def sample_days(region: Region, n: int = DAYS_PER_REGION, seed: int = SEED) -> list[date]:
    rng = random.Random(f"{seed}:{region.key}")
    pool = []
    for y in YEARS:
        d = date(y, 1, 1)
        while d.year == y:
            if d.month in region.months:
                pool.append(d)
            d += timedelta(days=1)
    return sorted(rng.sample(pool, n))


def sample_days_year_round(region: Region, per_month: int = YEAR_ROUND_PER_MONTH, seed: int = SEED) -> list[date]:
    rng = random.Random(f"{seed}:{region.key}:yr")
    season = set(sample_days(region))
    out = []
    for m in range(1, 13):
        pool = []
        for y in YEARS:
            d = date(y, m, 1)
            while d.month == m:
                if d not in season:
                    pool.append(d)
                d += timedelta(days=1)
        out.extend(rng.sample(pool, per_month))
    return sorted(out)


def all_days(region: Region) -> list[date]:
    return sorted(set(sample_days(region)) | set(sample_days_year_round(region)))
