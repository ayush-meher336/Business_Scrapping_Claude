"""Text normalisation shared by geocoding, keywords and (later) deduplication."""
from __future__ import annotations

import math
import re
import unicodedata

_TOKEN_MAP = {"saint": "st", "sainte": "ste", "mount": "mt", "fort": "ft", "the": ""}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_place(s: str | None) -> str:
    """'St. Mary's' and 'Saint Marys' -> 'st marys'."""
    if not s:
        return ""
    s = strip_accents(s).casefold().replace("'", "").replace("’", "")
    s = _PUNCT.sub(" ", s)
    tokens = [_TOKEN_MAP.get(t, t) for t in s.split()]
    return " ".join(t for t in tokens if t)


def norm_term(s: str) -> str:
    """Keyword normalisation: case, accents (Latin only), spacing. Non-Latin scripts kept as is."""
    s = unicodedata.normalize("NFKC", s).strip().casefold()
    return re.sub(r"\s+", " ", s)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lon, max_lon) enclosing a circle; used to pre-filter SQL."""
    dlat = km / 111.0
    dlon = km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon
