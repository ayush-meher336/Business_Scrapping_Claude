"""Long-running, concurrent operation of the whole pipeline.

Independent workers, each in its own thread with its own SQLite connection (WAL mode allows one
writer and many readers; writes are short transactions):

  discovery   keeps the search queue topped up (plan -> scan industrial areas -> build zones, moving
              through cities and countries by priority) and runs Google Maps searches; saturation
              rules move it on from areas that stopped yielding. A verification page pauses it.
  enrichment  crawls websites of newly discovered leads, checks email domains, browser fallback.
  ai_export   keeps a few evidence batches waiting in data/ai/batches/ for the AI agent.
  ai_import   imports any result file the agent has written (validated, see AGENTS.md).
  sync        two-way sync with the shared Google Sheet every few minutes.

State lives in the database, so stopping (Ctrl+C, or creating data/STOP) and restarting resumes
where it left off. Progress is written to data/autopilot_status.json and data/autopilot.log.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from leadforge.core.config import AppConfig
from leadforge.core.ids import new_ulid
from leadforge.storage import db

log = logging.getLogger("leadforge.autopilot")


@dataclass
class AutopilotSettings:
    countries: list[str] = field(default_factory=list)  # empty = all non-excluded, by tier
    priorities: list[str] = field(default_factory=lambda: ["A", "B", "C"])
    min_pending_jobs: int = 40          # top the queue up below this
    jobs_per_cycle: int = 20
    scan_cities_per_cycle: int = 3
    enrich_batch: int = 150
    enrich_idle_s: int = 60
    browser_fallback: int = 10
    ai_outstanding_batches: int = 3     # batches waiting for the agent at any time
    ai_batch_size: int = 20
    ai_import_every_s: int = 30
    sync_every_s: int = 900
    blocked_pause_s: int = 1800         # after a verification page, wait this long before trying again
    with_sync: bool = True


class Autopilot:
    def __init__(self, cfg: AppConfig, niche_name: str, settings: AutopilotSettings):
        self.cfg, self.niche_name, self.s = cfg, niche_name, settings
        self.stop = threading.Event()
        self.status: dict = {"started_at": _now(), "workers": {}}
        self.lock = threading.Lock()
        self.data = cfg.resolve(Path("data"))
        self.stop_file = self.data / "STOP"

    # -- plumbing ------------------------------------------------------------
    def conn(self):
        return db.connect(self.cfg.resolve(self.cfg.paths.database))

    def note(self, worker: str, **kw) -> None:
        with self.lock:
            w = self.status["workers"].setdefault(worker, {})
            w.update(kw, updated_at=_now())
            self.status["updated_at"] = _now()
            path = self.data / "autopilot_status.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status, indent=1, default=str), encoding="utf-8")
            tmp.replace(path)

    def sleep(self, seconds: float) -> bool:
        """Interruptible sleep; returns False when stopping."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop.is_set() or self.stop_file.exists():
                self.stop.set()
                return False
            time.sleep(min(2, end - time.monotonic()))
        return True

    def loop(self, name: str, fn) -> None:
        """Run `fn` repeatedly; an error is logged and retried later, never fatal to other workers."""
        failures = 0
        while not self.stop.is_set():
            try:
                wait = fn()
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                wait = min(60 * failures, 900)
                log.error("%s failed: %s\n%s", name, exc, traceback.format_exc())
                self.note(name, state="error", error=str(exc)[:500], retry_in_s=wait)
            if not self.sleep(wait or 1):
                break
        self.note(name, state="stopped")

    # -- workers -------------------------------------------------------------
    def countries(self, conn) -> list[str]:
        if self.s.countries:
            return self.s.countries
        return [r[0] for r in conn.execute("SELECT name FROM country WHERE excluded=0 ORDER BY tier, name")]

    def replenish(self, conn, niche) -> str:
        """Make sure there are searches to run: plan open zones, else create zones for the next cities."""
        from leadforge.discovery.industrial import scan_city
        from leadforge.geography.settings import load_geo_config
        from leadforge.geography.zones import build_city
        from leadforge.keywords.planner import plan
        from leadforge.cli import _session_factory, _target_cities

        countries = self.countries(conn)
        pending = conn.execute("SELECT COUNT(*) FROM search_job WHERE status='pending' AND source='google_maps_browser'").fetchone()[0]
        if pending >= self.s.min_pending_jobs:
            return f"{pending} searches queued"
        for country in countries:
            with conn:
                r = plan(conn, niche.niche, [country], ["google_maps_browser"], f"plan-{new_ulid()}", 4)
            pending += r.jobs_created
            if pending >= self.s.min_pending_jobs:
                return f"planned searches up to {pending}"
        # No open zones left to plan: give the next cities industrial-area evidence and zones.
        settings = load_geo_config(self.cfg.root).build_settings()
        have = {r[0] for r in conn.execute("SELECT DISTINCT place_id FROM osm_feature")}
        tried = {r[0].split(":", 1)[1] for r in conn.execute("SELECT key FROM sync_state WHERE key LIKE 'zones_tried:%'")}
        for prio in self.s.priorities:
            todo = [c for c in _target_cities(conn, countries, None, prio)
                    if c["place_id"] not in tried][: self.s.scan_cities_per_cycle]
            if not todo:
                continue
            run_id = f"zones-{new_ulid()}"
            with _session_factory(self.cfg, "google_maps_browser", False)() as session:
                for c in todo:
                    if self.stop.is_set():
                        break
                    if c["place_id"] not in have:
                        scan_city(conn, session, c["place_id"], c["name"], c["country"])
                    with conn:
                        build_city(conn, c["place_id"], settings, run_id)
                        conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                                     (f"zones_tried:{c['place_id']}", _now()))
            return f"created zones for {len(todo)} priority-{prio} cities"
        return "no cities left in scope"

    def w_discovery(self) -> float:
        from leadforge.discovery.gmaps import BlockedError
        from leadforge.discovery.runner import Relevance, run
        from leadforge.niches.model import load_niche
        from leadforge.cli import _saturation, _session_factory

        conn = self.conn()
        try:
            niche = load_niche(self.cfg.root, self.niche_name)
            try:
                msg = self.replenish(conn, niche)
            except BlockedError as exc:
                self.note("discovery", state="paused", reason=f"verification page: {exc}",
                          resume_at=_later(self.s.blocked_pause_s))
                return self.s.blocked_pause_s
            stats = run(conn, "google_maps_browser", _session_factory(self.cfg, "google_maps_browser", False),
                        Relevance(niche.discovery.include, niche.discovery.exclude), self.s.jobs_per_cycle,
                        self.cfg.discovery.get("workers", 2), saturation=_saturation(self.cfg))
            if stats.blocked:
                self.note("discovery", state="paused", reason=f"verification page: {stats.blocked}",
                          resume_at=_later(self.s.blocked_pause_s))
                log.warning("discovery paused: %s", stats.blocked)
                return self.s.blocked_pause_s
            self.note("discovery", state="running", queue=msg, last_jobs=stats.jobs_done, last_new=stats.new,
                      last_merged=stats.merged, last_rejected=stats.rejected, closed_areas=stats.saturation[-5:],
                      total_new=_add(self.status, "discovery", "total_new", stats.new))
            if stats.jobs_done == 0 and msg == "no cities left in scope":
                return 3600
            return 5
        finally:
            conn.close()

    def w_enrichment(self) -> float:
        from leadforge.enrichment import pipeline
        from leadforge.cli import _crawl_settings, _session_factory

        conn = self.conn()
        try:
            targets = pipeline.select_targets(conn, "new", None, None, self.s.enrich_batch)
            if not targets:
                n = pipeline.browser_fallback(conn, _session_factory(self.cfg, "google_maps_browser", False),
                                              self.s.browser_fallback) if self.s.browser_fallback else 0
                self.note("enrichment", state="idle", browser_fallback=n)
                return self.s.enrich_idle_s
            st = pipeline.run(conn, targets, _crawl_settings(self.cfg))
            self.note("enrichment", state="running", last=st.summary(),
                      total_sites=_add(self.status, "enrichment", "total_sites", st.businesses))
            return 1
        finally:
            conn.close()

    def ai_dir(self) -> Path:
        return self.cfg.resolve(Path("data/ai"))

    def outstanding(self, conn) -> list[str]:
        return [r[0] for r in conn.execute("SELECT batch_id FROM ai_batch WHERE status='exported'")
                if not (self.ai_dir() / "results" / f"{r[0]}.result.json").exists()]

    def w_ai_export(self) -> float:
        from leadforge.ai_processing.export import export
        from leadforge.niches.model import load_niche

        conn = self.conn()
        try:
            waiting = self.outstanding(conn)
            need = self.s.ai_outstanding_batches - len(waiting)
            made = []
            if need > 0:
                made = export(conn, load_niche(self.cfg.root, self.niche_name), self.ai_dir(), self.s.ai_batch_size,
                              need * self.s.ai_batch_size)
            self.note("ai_export", state="running", waiting_for_agent=len(waiting) + len(made),
                      batches=[p.name for p in made] or None)
            return 60
        finally:
            conn.close()

    def w_ai_import(self) -> float:
        from leadforge.ai_processing.importer import import_results
        from leadforge.niches.model import load_niche

        conn = self.conn()
        try:
            n = load_niche(self.cfg.root, self.niche_name)
            st = import_results(conn, self.ai_dir(), n.description.max_words, n.description.hook_max_words,
                                n.industry_enum, n.material_enum)
            if st.files:
                self.note("ai_import", state="running", last=st.__dict__,
                          total_accepted=_add(self.status, "ai_import", "total_accepted", st.accepted))
            return self.s.ai_import_every_s
        finally:
            conn.close()

    def w_sync(self) -> float:
        from leadforge.niches.model import load_niche
        from leadforge.sync.client import client_from_env
        from leadforge.sync.run import run_sync

        conn = self.conn()
        try:
            out = run_sync(conn, client_from_env(self.cfg.root), load_niche(self.cfg.root, self.niche_name))
            self.note("sync", state="running", last={k: {kk: v for kk, v in r.items() if v} for k, r in out.items()})
            return self.s.sync_every_s
        finally:
            conn.close()

    # -- entry point ---------------------------------------------------------
    def run(self) -> None:
        if self.stop_file.exists():
            self.stop_file.unlink()
        workers = {"discovery": self.w_discovery, "enrichment": self.w_enrichment,
                   "ai_export": self.w_ai_export, "ai_import": self.w_ai_import}
        if self.s.with_sync:
            workers["sync"] = self.w_sync
        threads = [threading.Thread(target=self.loop, args=(name, fn), name=name, daemon=True)
                   for name, fn in workers.items()]
        for t in threads:
            t.start()
            time.sleep(2)          # stagger start-up
        log.info("autopilot running: %s", ", ".join(workers))
        try:
            while any(t.is_alive() for t in threads):
                if self.stop_file.exists():
                    self.stop.set()
                time.sleep(2)
        except KeyboardInterrupt:
            log.info("stopping (finishing current steps)...")
            self.stop.set()
        for t in threads:
            t.join(timeout=600)
        log.info("autopilot stopped")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _later(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _add(status: dict, worker: str, key: str, n: int) -> int:
    return (status["workers"].get(worker, {}).get(key) or 0) + (n or 0)
