from __future__ import annotations

import contextvars
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
CACHE_DIR = BACKEND_DIR / "cache"

load_dotenv(PROJECT_ROOT / ".env", override=False)

FIRMS_BASE_URL = os.environ.get("FIRMS_BASE_URL", "https://firms.modaps.eosdis.nasa.gov")

_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default="tr")


def set_lang(value: str | None) -> None:
    _LANG.set("en" if str(value or "").lower().startswith("en") else "tr")


def get_lang() -> str:
    return _LANG.get()


def L(tr: str, en: str) -> str:
    return en if _LANG.get() == "en" else tr

for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def get_map_key() -> str | None:
    for name in ("FIRMS_MAP_KEY", "MAP_KEY"):
        val = os.environ.get(name, "").strip().strip('"').strip("'")
        if val and val != "your_map_key_here":
            return val
    return None


_URL_KEY_PATTERNS = [
    re.compile(r"(/api/[a-z_]+/csv/)([^/\s?]+)"),
    re.compile(r"(MAP_KEY=)([^&\s]+)", re.IGNORECASE),
]


# The FIRMS key must never reach logs, API responses or exports.
def mask_secret(text: str) -> str:
    if not text:
        return text
    key = get_map_key()
    if key:
        text = text.replace(key, "***")
    for pat in _URL_KEY_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "***", text)
    return text


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask_secret(msg)
        if masked != msg:
            record.msg = masked
            record.args = None
        return True


def install_log_masking() -> None:
    f = MaskingFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
