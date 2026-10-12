"""Keyword library: niche packs + per-country local terms, with historic yield."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from leadforge.core.text import norm_place, norm_term
from leadforge.niches.model import NicheConfig

LANGUAGE_CODES = {
    "english": "en", "hindi": "hi", "gujarati": "gu", "marathi": "mr", "tamil": "ta", "punjabi": "pa",
    "bengali": "bn", "telugu": "te", "urdu": "ur", "arabic": "ar", "chinese": "zh", "mandarin": "zh",
    "cantonese": "zh", "japanese": "ja", "korean": "ko", "thai": "th", "vietnamese": "vi",
    "indonesian": "id", "malay": "ms", "bahasa": "id", "tagalog": "tl", "filipino": "tl",
    "spanish": "es", "portuguese": "pt", "french": "fr", "german": "de", "italian": "it", "dutch": "nl",
    "polish": "pl", "turkish": "tr", "greek": "el", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "czech": "cs", "romanian": "ro", "hungarian": "hu", "russian": "ru", "hebrew": "he",
    "persian": "fa", "farsi": "fa", "swahili": "sw", "maori": "mi",
}

# Prior expected new uniques per query, by kind, before any yield data exists.
KIND_PRIOR = {"core": 0.6, "local": 0.55, "synonym": 0.4, "material": 0.3}
PRIOR_WEIGHT = 2


def country_languages(text: str | None) -> list[str]:
    codes = []
    for part in re.split(r"[,;/()]", text or ""):
        code = LANGUAGE_CODES.get(part.strip().casefold())
        if code and code not in codes:
            codes.append(code)
    return codes or ["en"]


def sync_keywords(conn: sqlite3.Connection, niche: NicheConfig) -> dict[str, int]:
    """Upsert keywords from the niche packs and the Countries tab. Never deletes yield history."""
    added = defaultdict(int)

    def add(language, country, term, kind, source):
        t = norm_term(term)
        if not t:
            return
        cur = conn.execute(
            """INSERT OR IGNORE INTO keyword(niche, language, country, term, term_norm, kind, source)
               VALUES (?,?,?,?,?,?,?)""",
            (niche.niche, language, country, term.strip(), t, kind, source),
        )
        added[kind] += cur.rowcount

    for pack in niche.keyword_packs.values():
        for kind, terms in (("core", pack.core), ("synonym", pack.synonyms),
                            ("material", pack.materials), ("exclusion", pack.exclusions)):
            for term in terms:
                add(pack.language, None, term, kind, "niche_config")

    pack_terms = {r[0] for r in conn.execute(
        "SELECT term_norm FROM keyword WHERE niche=? AND source='niche_config'", (niche.niche,))}
    # Drop local copies of pack terms left by earlier syncs (only if no job uses them yet).
    dupes = """SELECT keyword_id FROM keyword WHERE niche=? AND source='countries_tab'
               AND term_norm IN (SELECT term_norm FROM keyword WHERE niche=? AND source='niche_config')
               AND keyword_id NOT IN (SELECT keyword_id FROM search_job)"""
    conn.execute(f"DELETE FROM keyword_yield WHERE keyword_id IN ({dupes})", (niche.niche, niche.niche))
    conn.execute(f"DELETE FROM keyword WHERE keyword_id IN ({dupes})", (niche.niche, niche.niche))
    for row in conn.execute("SELECT name, local_keywords FROM country WHERE local_keywords IS NOT NULL").fetchall():
        for term in re.split(r"[,;|]", row["local_keywords"]):
            if norm_term(term) not in pack_terms:     # already covered by a pack keyword
                add("local", row["name"], term, "local", "countries_tab")
    return dict(added)


def keywords_for_country(conn: sqlite3.Connection, niche: str, country: str) -> list[sqlite3.Row]:
    row = conn.execute("SELECT languages FROM country WHERE name=?", (country,)).fetchone()
    langs = country_languages(row["languages"] if row else None)
    if "en" not in langs:
        langs.append("en")
    marks = ",".join("?" * len(langs))
    rows = conn.execute(
        f"""SELECT * FROM keyword WHERE niche=? AND active=1 AND kind != 'exclusion'
            AND ((country IS NULL AND language IN ({marks})) OR country = ?)""",
        (niche, *langs, country),
    ).fetchall()
    # A local term that repeats a pack term adds nothing.
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["country"] is None):
        if r["term_norm"] not in seen:
            seen.add(r["term_norm"])
            out.append(r)
    return out


def source_slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "unknown").casefold()).strip("_")


_ADDED_RE = re.compile(r"\b(\d+)\s+(?:new\s+)?(?:companies\s+|places\s+)?added\b", re.I)
_NON_SEARCH = re.compile(r"^(batch total|audit|sitecheck|suburb \+ website check)", re.I)


def new_count(log: sqlite3.Row) -> int | None:
    """New businesses a log row produced, or None when the row does not say."""
    if log["new_companies_added"] is not None:
        return _int(log["new_companies_added"])
    m = _ADDED_RE.search(log["notes"] or "")
    return int(m.group(1)) if m else None


def import_legacy_yields(conn: sqlite3.Connection, niche: str) -> dict[str, int]:
    """Credit research-log searches to the keywords they contain.

    Only real search rows whose new-business count is known are used. A row that ran several
    keywords at once splits its results and new businesses evenly between them.
    """
    conn.execute(
        "DELETE FROM keyword_yield WHERE keyword_id IN (SELECT keyword_id FROM keyword WHERE niche=?)", (niche,))
    size_of = {
        (r["country"], norm_place(r["name"])): r["size_code"] or "*"
        for r in conn.execute("SELECT country, name, size_code FROM geo_place WHERE level='city'")
    }
    cache: dict[str, list[sqlite3.Row]] = {}
    totals = defaultdict(lambda: [0, 0.0, 0.0])
    stats = defaultdict(int)
    for log in conn.execute("SELECT * FROM research_log WHERE keyword IS NOT NULL").fetchall():
        if _NON_SEARCH.match(log["keyword"].strip()):
            stats["skipped_not_a_search"] += 1
            continue
        new = new_count(log)
        if new is None:
            stats["skipped_unknown_new_count"] += 1
            continue
        country = log["country"] or ""
        if country not in cache:
            cache[country] = sorted(keywords_for_country(conn, niche, country),
                                    key=lambda r: len(r["term_norm"]), reverse=True)
        q = norm_term(log["keyword"])
        matched: list[sqlite3.Row] = []
        for k in cache[country]:
            if k["term_norm"] in q and not any(k["term_norm"] in m["term_norm"] for m in matched):
                matched.append(k)
        if not matched:
            stats["skipped_no_keyword"] += 1
            continue
        share = 1 / len(matched)
        size = size_of.get((country, norm_place(log["city"])), "*")
        for k in matched:
            t = totals[(k["keyword_id"], country, size, source_slug(log["source"]))]
            t[0] += 1
            t[1] += _int(log["results_reviewed"]) * share
            t[2] += new * share
        stats["credited"] += 1
    conn.executemany(
        "INSERT INTO keyword_yield VALUES (?,?,?,?,?,?,?)",
        [(*k, q, round(r), round(n)) for k, (q, r, n) in totals.items()],
    )
    stats["yield_rows"] = len(totals)
    return dict(stats)


def _int(v) -> int:
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else 0


def expected_yield(conn: sqlite3.Connection, kw: sqlite3.Row, country: str) -> float:
    """Smoothed new-uniques-per-query: country history first, then all countries, then the kind prior."""
    prior = KIND_PRIOR.get(kw["kind"], 0.3)
    for where, args in (("country=?", (country,)), ("1=1", ())):
        r = conn.execute(
            f"SELECT SUM(queries) q, SUM(new_uniques) n FROM keyword_yield WHERE keyword_id=? AND {where}",
            (kw["keyword_id"], *args),
        ).fetchone()
        if r["q"]:
            return (r["n"] + prior * PRIOR_WEIGHT) / (r["q"] + PRIOR_WEIGHT)
    return prior
