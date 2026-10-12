"""End-to-end benchmark: discover N unique businesses, enrich them, check emails, optionally sync.

Comparable unit with the old agent benchmark (100 businesses discovered and enriched in 616.21 s):
a business counts once it has been found on Google Maps in this run (new, or matched to an existing
lead) and its website, if any, has been crawled.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from leadforge.core.ids import new_ulid
from leadforge.discovery import runner
from leadforge.enrichment import pipeline
from leadforge.enrichment.crawl import CrawlSettings

OLD = {"businesses": 100, "seconds": 616.21, "phone": 0.99, "website": 0.88, "email": 0.52}


@dataclass
class BenchResult:
    label: str
    target: int
    area: str
    stages: dict[str, float] = field(default_factory=dict)
    discovery: dict = field(default_factory=dict)
    enrichment: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    first_record_s: float | None = None
    businesses: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_s(self) -> float:
        return sum(self.stages.values())


def found_in_run(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Distinct businesses resolved from the search jobs this run claimed."""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT c.lead_key FROM candidate c JOIN search_job j ON j.job_id = c.job_id
           WHERE j.claimed_by = ? AND c.lead_key IS NOT NULL""", (run_id,))]


def run_bench(conn: sqlite3.Connection, session_factory, relevance: runner.Relevance, settings: CrawlSettings,
              target: int, country: str, city: str | None, workers: int, sync_fn=None, label: str = "") -> BenchResult:
    res = BenchResult(label or f"{target}", target, f"{city or 'all'}, {country}")
    run_id = f"bench-{new_ulid()}"
    t_start = time.perf_counter()
    first: list[float] = []

    def on_job(job, n, new):
        if not first and n:
            first.append(time.perf_counter() - t_start)

    stats = runner.run(conn, "google_maps_browser", session_factory, relevance, max_jobs=10_000, workers=workers,
                       country=country, city=city, on_job=on_job, stop_when=lambda rid: len(found_in_run(conn, rid)) >= target,
                       run_id=run_id)
    res.stages["discovery"] = time.perf_counter() - t_start
    res.first_record_s = first[0] if first else None
    keys = found_in_run(conn, run_id)
    res.businesses = len(keys)
    res.discovery = {"jobs": stats.jobs_done, "failed": stats.jobs_failed, "candidates": stats.candidates,
                     "new": stats.new, "merged": stats.merged, "review": stats.review, "rejected": stats.rejected,
                     "blocked": stats.blocked}
    if stats.blocked:
        res.notes.append(f"Discovery stopped by a verification page: {stats.blocked}")

    # Enrich every business found in this run that has a website (re-crawl included, for a fair timing).
    marks = ",".join("?" * len(keys)) or "''"
    targets = conn.execute(
        f"""SELECT b.*, c.iso2 FROM business b LEFT JOIN country c ON c.name = b.country
            WHERE b.lead_key IN ({marks}) AND b.website IS NOT NULL AND b.permanently_closed = 0""", keys).fetchall()
    t = time.perf_counter()
    est = pipeline.run(conn, targets, settings) if targets else None
    res.stages["enrichment+email check"] = time.perf_counter() - t
    if est:
        res.enrichment = est.summary()

    if sync_fn:
        t = time.perf_counter()
        sync_fn()
        res.stages["sheet sync"] = time.perf_counter() - t

    n = len(keys) or 1
    q = conn.execute(
        f"""SELECT SUM(phone IS NOT NULL), SUM(website IS NOT NULL),
                   SUM(general_email IS NOT NULL OR decision_maker_email IS NOT NULL),
                   SUM(email_verified = 'Valid')
            FROM business WHERE lead_key IN ({marks})""", keys).fetchone()
    twins = conn.execute(
        f"""SELECT COUNT(*) FROM business b WHERE b.lead_key IN ({marks}) AND EXISTS (
              SELECT 1 FROM business o WHERE o.lead_key != b.lead_key AND (
                (b.google_place_id IS NOT NULL AND o.google_place_id = b.google_place_id) OR
                (b.phone_e164 IS NOT NULL AND o.phone_e164 = b.phone_e164 AND o.name_norm = b.name_norm)))""",
        keys).fetchone()[0]
    res.quality = {"phone": (q[0] or 0) / n, "website": (q[1] or 0) / n, "email": (q[2] or 0) / n,
                   "email_valid_mx": (q[3] or 0) / n, "exact_identity_twins": twins}
    conn.execute("INSERT INTO run(run_id, kind, started_at, finished_at, stats_json) VALUES (?,?,?,?,?)",
                 (f"{run_id}-report", "benchmark", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                  dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), json.dumps(res.__dict__, default=str)))
    conn.commit()
    return res


def report(results: list[BenchResult], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"benchmark_{dt.datetime.now():%Y%m%d-%H%M%S}.md"
    old_per100 = OLD["seconds"]
    lines = ["# leadforge benchmark", "",
             f"Baseline (old agent workflow): 100 businesses in {OLD['seconds']} s = {old_per100:.0f} s per 100; "
             f"phone 99%, website 88%, verified email 52%.", "",
             "| Run | Area | Businesses | Total s | s per 100 | vs old | Discovery s | Enrich s | Sync s | "
             "First record s | Phone | Website | Email | Valid (MX) | Twins |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in results:
        per100 = r.total_s / max(r.businesses, 1) * 100
        q = r.quality
        lines.append(
            f"| {r.label} | {r.area} | {r.businesses} | {r.total_s:.0f} | {per100:.0f} | {old_per100 / per100:.1f}x | "
            f"{r.stages.get('discovery', 0):.0f} | {r.stages.get('enrichment+email check', 0):.0f} | "
            f"{r.stages.get('sheet sync', 0):.0f} | {r.first_record_s or 0:.0f} | {q.get('phone', 0):.0%} | "
            f"{q.get('website', 0):.0%} | {q.get('email', 0):.0%} | {q.get('email_valid_mx', 0):.0%} | "
            f"{q.get('exact_identity_twins', 0)} |")
    lines += ["", "## Details", ""]
    for r in results:
        lines += [f"### {r.label} ({r.area})", "", f"- Discovery: {r.discovery}", f"- Enrichment: {r.enrichment}"]
        lines += [f"- Note: {n}" for n in r.notes]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
