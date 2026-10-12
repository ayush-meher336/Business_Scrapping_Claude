"""Run queued search jobs through browser workers and resolve every hit as it arrives.

Threads only drive browsers. The main thread owns the database: it claims jobs, hands them to
workers, and writes each finished job (candidates, resolutions, yields) in one transaction, so a
crash never loses more than the jobs in flight. Claimed jobs left behind by a crash return to the
queue on the next run.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from leadforge.core.ids import new_ulid
from leadforge.dedup.resolver import Context, resolve
from leadforge.discovery.gmaps import BlockedError

SOURCE_TYPES = {
    "google_maps_browser": "Google Maps listing",
    "web_search_browser": "Other",
}
LEASE_MINUTES = 15


@dataclass
class Relevance:
    include: list[str]
    exclude: list[str]

    def check(self, raw: dict) -> tuple[bool, str]:
        text = " ".join(str(raw.get(k) or "") for k in ("category", "name", "snippet"))
        for pat in self.exclude:
            if re.search(pat, text, re.I):
                return False, f"excluded: {pat}"
        if any(re.search(p, text, re.I) for p in self.include):
            return True, ""
        return False, f"category not in niche: {raw.get('category') or 'unknown'}"


@dataclass
class RunStats:
    jobs_done: int = 0
    jobs_failed: int = 0
    candidates: int = 0
    new: int = 0
    merged: int = 0
    review: int = 0
    rejected: int = 0
    blocked: str | None = None
    errors: list[str] = field(default_factory=list)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def release_stale_claims(conn: sqlite3.Connection) -> int:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
    return conn.execute("UPDATE search_job SET status='pending', claimed_by=NULL, claimed_at=NULL "
                        "WHERE status='claimed' AND claimed_at < ?", (cutoff,)).rowcount


def claim(conn: sqlite3.Connection, source: str, worker: str, country: str | None, city: str | None) -> sqlite3.Row | None:
    sql = """SELECT j.job_id FROM search_job j JOIN zone z ON z.zone_id = j.zone_id
             WHERE j.status='pending' AND j.source=?"""
    args: list = [source]
    if country:
        sql += " AND z.country=?"
        args.append(country)
    if city:
        sql += " AND z.city=?"
        args.append(city)
    row = conn.execute(sql + " ORDER BY j.priority DESC, j.job_id LIMIT 1", args).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE search_job SET status='claimed', claimed_by=?, claimed_at=?, attempts=attempts+1 "
                 "WHERE job_id=? AND status='pending'", (worker, _now(), row["job_id"]))
    conn.commit()
    return conn.execute(
        """SELECT j.*, z.country, z.city, z.area, z.lat AS zlat, z.lon AS zlon, z.radius_m, k.term,
                  c.iso2, c.region, g.state_province
           FROM search_job j JOIN zone z ON z.zone_id=j.zone_id JOIN keyword k ON k.keyword_id=j.keyword_id
           LEFT JOIN country c ON c.name=z.country LEFT JOIN geo_place g ON g.place_id=z.place_id
           WHERE j.job_id=?""", (row["job_id"],)).fetchone()


def store_results(conn: sqlite3.Connection, job: sqlite3.Row, results: list[dict], relevance: Relevance,
                  run_id: str, stats: RunStats) -> int:
    """Resolve one job's results in one transaction; returns the number of new businesses."""
    ctx = Context(zone_id=job["zone_id"], country=job["country"], iso2=job["iso2"], region=job["region"],
                  state=job["state_province"], city=job["city"], area=job["area"], zone_lat=job["zlat"],
                  zone_lon=job["zlon"], zone_radius_m=job["radius_m"], keyword=job["term"], run_id=run_id,
                  job_id=job["job_id"])
    new = 0
    with conn:
        for raw in results:
            raw = {**raw, "source": job["source"], "source_type": SOURCE_TYPES.get(job["source"], "Other")}
            ok, why = relevance.check(raw)
            r = resolve(conn, raw, ctx, relevant=ok, reject_reason=why)
            setattr(stats, r.action, getattr(stats, r.action) + 1)
            new += r.action in ("new", "review")
        stats.candidates += len(results)
        conn.execute("UPDATE search_job SET status='done', results=?, new_uniques=?, finished_at=?, error=NULL "
                     "WHERE job_id=?", (len(results), new, _now(), job["job_id"]))
        conn.execute(
            """INSERT INTO keyword_yield(keyword_id, country, size_code, source, queries, results, new_uniques)
               VALUES (?, ?, COALESCE((SELECT city_size FROM zone WHERE zone_id=?), '*'), ?, 1, ?, ?)
               ON CONFLICT(keyword_id, country, size_code, source) DO UPDATE SET
               queries=queries+1, results=results+excluded.results, new_uniques=new_uniques+excluded.new_uniques""",
            (job["keyword_id"], job["country"], job["zone_id"], job["source"], len(results), new))
        conn.execute("UPDATE zone SET status='In progress' WHERE zone_id=? AND status IN ('Not started','Pending')",
                     (job["zone_id"],))
    return new


def run(conn: sqlite3.Connection, source: str, session_factory: Callable[[], object], relevance: Relevance,
        max_jobs: int, workers: int = 1, country: str | None = None, city: str | None = None,
        on_job: Callable[[sqlite3.Row, int, int], None] | None = None) -> RunStats:
    """`session_factory()` returns a context manager whose value has `.search(query) -> list[result]`."""
    stats = RunStats()
    run_id = f"discover-{new_ulid()}"
    conn.execute("INSERT INTO run(run_id, kind, started_at, config_json) VALUES (?,?,?,?)",
                 (run_id, "research", _now(), json.dumps({"source": source, "max_jobs": max_jobs,
                                                          "workers": workers, "country": country, "city": city})))
    release_stale_claims(conn)
    conn.commit()

    local = threading.local()
    sessions: list = []
    lock = threading.Lock()
    stop = threading.Event()

    def work(query: str) -> list[dict]:
        if stop.is_set():
            raise RuntimeError("stopped")
        if not hasattr(local, "session"):
            cm = session_factory()
            local.session = cm.__enter__()
            with lock:
                sessions.append(cm)
        return [r.as_dict() if hasattr(r, "as_dict") else r for r in local.session.search(query)]

    pending: dict[Future, sqlite3.Row] = {}
    claimed = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                while not stop.is_set() and len(pending) < workers and claimed < max_jobs:
                    job = claim(conn, source, run_id, country, city)
                    if job is None:
                        break
                    claimed += 1
                    pending[pool.submit(work, job["query"])] = job
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    job = pending.pop(fut)
                    try:
                        results = fut.result()
                    except BlockedError as exc:
                        stop.set()
                        stats.blocked = str(exc)
                        conn.execute("UPDATE search_job SET status='blocked', error=? WHERE job_id=?",
                                     (str(exc), job["job_id"]))
                        conn.commit()
                        continue
                    except Exception as exc:  # one failed search must not end the run
                        stats.jobs_failed += 1
                        stats.errors.append(f"job {job['job_id']}: {exc}")
                        status = "failed" if job["attempts"] >= 3 else "pending"
                        conn.execute("UPDATE search_job SET status=?, error=?, claimed_by=NULL WHERE job_id=?",
                                     (status, str(exc)[:500], job["job_id"]))
                        conn.commit()
                        continue
                    new = store_results(conn, job, results, relevance, run_id, stats)
                    stats.jobs_done += 1
                    if on_job:
                        on_job(job, len(results), new)
    finally:
        # Jobs that never ran go back to the queue.
        conn.execute("UPDATE search_job SET status='pending', claimed_by=NULL WHERE status='claimed' AND claimed_by=?",
                     (run_id,))
        conn.execute("UPDATE run SET finished_at=?, stats_json=? WHERE run_id=?",
                     (_now(), json.dumps(stats.__dict__, default=str), run_id))
        conn.commit()
        for cm in sessions:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
    return stats
