from __future__ import annotations

import numpy as np
import pandas as pd

SAMPLE_START = "2025-08-10"
SAMPLE_DAYS = 4
SAMPLE_BBOX = (27.0, 35.5, 32.5, 38.0)


def _modis_row(rng, lat, lon, date, hhmm, sat):
    return {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "brightness": f"{rng.uniform(310, 360):.1f}",
        "scan": f"{rng.uniform(1.0, 1.6):.1f}", "track": f"{rng.uniform(1.0, 1.3):.1f}",
        "acq_date": date, "acq_time": str(hhmm), "satellite": sat, "instrument": "MODIS",
        "confidence": str(int(rng.uniform(20, 100))), "version": "6.1NRT",
        "bright_t31": f"{rng.uniform(290, 305):.1f}", "frp": f"{rng.uniform(5, 120):.1f}", "daynight": "D" if 600 <= hhmm <= 1600 else "N",
    }


def _viirs_row(rng, lat, lon, date, hhmm):
    return {
        "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}", "bright_ti4": f"{rng.uniform(310, 367):.2f}",
        "scan": f"{rng.uniform(0.38, 0.55):.2f}", "track": f"{rng.uniform(0.36, 0.5):.2f}",
        "acq_date": date, "acq_time": str(hhmm), "satellite": "N20", "instrument": "VIIRS",
        "confidence": rng.choice(["l", "n", "n", "h"]), "version": "2.0NRT",
        "bright_ti5": f"{rng.uniform(285, 300):.2f}", "frp": f"{rng.uniform(1, 40):.2f}" if rng.random() > 0.05 else "",
        "daynight": "D" if 600 <= hhmm <= 1600 else "N",
    }


def build_sample_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(20250810)
    dates = pd.date_range(SAMPLE_START, periods=SAMPLE_DAYS, freq="D").strftime("%Y-%m-%d").tolist()
    modis, viirs = [], []
    for k, d in enumerate(dates[:3]):
        clat, clon = 36.86 + 0.012 * k, 31.42 + 0.015 * k
        for _ in range(3):
            modis.append(_modis_row(rng, clat + rng.normal(0, 0.006), clon + rng.normal(0, 0.006), d, 1050 + k, "A"))
        for _ in range(9):
            viirs.append(_viirs_row(rng, clat + rng.normal(0, 0.005), clon + rng.normal(0, 0.005), d, 1012 + k))
        for _ in range(4):
            viirs.append(_viirs_row(rng, clat + rng.normal(0, 0.004), clon + rng.normal(0, 0.004), d, 2318))
    for k, d in enumerate(dates[1:3]):
        for _ in range(2):
            modis.append(_modis_row(rng, 36.88 + rng.normal(0, 0.005), 28.21 + rng.normal(0, 0.005), d, 831, "T"))
        for _ in range(5):
            viirs.append(_viirs_row(rng, 36.88 + rng.normal(0, 0.004), 28.21 + rng.normal(0, 0.004), d, 1048))
    viirs.append(_viirs_row(rng, 37.55, 29.9, dates[0], 5))
    modis.append(_modis_row(rng, 36.35, 30.2, dates[3], 631, "T"))
    return pd.DataFrame(modis), pd.DataFrame(viirs)
