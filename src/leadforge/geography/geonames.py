"""Download (with cache) and load GeoNames dumps into SQLite."""
from __future__ import annotations

import csv
import io
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

from leadforge.core.text import norm_place

USER_AGENT = "leadforge/0.1 (lead research platform)"
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def download(base_url: str, files: Iterable[str], cache_dir: Path, refresh: bool = False) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in files:
        dest = cache_dir / name
        if refresh or not dest.exists():
            req = urllib.request.Request(base_url + name, headers={"User-Agent": USER_AGENT})
            tmp = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
        paths[name] = dest
    return paths


def _rows(text_lines: Iterable[str]) -> Iterable[list[str]]:
    for line in text_lines:
        if not line or line.startswith("#"):
            continue
        yield line.rstrip("\n").split("\t")


def load_countries(conn: sqlite3.Connection, path: Path) -> int:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for r in _rows(fh):
            # ISO, ISO3, ISO-Numeric, fips, Country, Capital, Area, Population, Continent, tld,
            # CurrencyCode, CurrencyName, Phone, Postal fmt, Postal regex, Languages, ...
            rows.append((r[0], r[4], r[15] if len(r) > 15 else None, r[12], int(r[7] or 0)))
    conn.executemany("INSERT OR REPLACE INTO gn_country VALUES (?,?,?,?,?)", rows)
    return len(rows)


def load_admin1(conn: sqlite3.Connection, path: Path) -> int:
    with open(path, encoding="utf-8") as fh:
        rows = [(r[0], r[1], r[2]) for r in _rows(fh)]
    conn.executemany("INSERT OR REPLACE INTO gn_admin1 VALUES (?,?,?)", rows)
    return len(rows)


def _open_places(path: Path) -> io.TextIOBase:
    if path.suffix == ".zip":
        zf = zipfile.ZipFile(path)
        inner = next(n for n in zf.namelist() if n.endswith(".txt"))
        return io.TextIOWrapper(zf.open(inner), encoding="utf-8")
    return open(path, encoding="utf-8")


def _latin(s: str) -> bool:
    return all(ord(c) < 0x250 for c in s)


def load_places(conn: sqlite3.Connection, path: Path, countries: set[str] | None = None) -> int:
    """Load populated places. Alternate names are kept only in Latin script, for matching."""
    if countries is not None:
        marks = ",".join("?" * len(countries))
        ids = f"SELECT geonameid FROM gn_place WHERE country_code IN ({marks})"
        conn.execute(f"DELETE FROM gn_name WHERE geonameid IN ({ids})", tuple(countries))
        conn.execute(f"DELETE FROM gn_place WHERE country_code IN ({marks})", tuple(countries))
    places, names, n = [], [], 0
    with _open_places(path) as fh:
        for r in _rows(fh):
            # geonameid, name, asciiname, alternatenames, lat, lon, feature class, feature code,
            # country code, cc2, admin1, admin2, admin3, admin4, population, elevation, dem, tz, modified
            if r[6] != "P" or (countries is not None and r[8] not in countries):
                continue
            gid = int(r[0])
            places.append((gid, r[1], r[2], float(r[4]), float(r[5]), r[7], r[8], r[10], int(r[14] or 0)))
            keys = {norm_place(r[1]), norm_place(r[2])}
            keys |= {norm_place(a) for a in r[3].split(",") if a and _latin(a)}
            names.extend((gid, k) for k in keys if k)
            n += 1
            if len(places) >= 20000:
                _flush(conn, places, names)
    _flush(conn, places, names)
    return n


def _flush(conn, places, names):
    conn.executemany("INSERT OR REPLACE INTO gn_place VALUES (?,?,?,?,?,?,?,?,?)", places)
    conn.executemany("INSERT OR IGNORE INTO gn_name VALUES (?,?)", names)
    places.clear()
    names.clear()
