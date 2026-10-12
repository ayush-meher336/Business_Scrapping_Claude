"""Enrichment run: pick businesses, crawl their websites, save evidence, check email domains.

Writes follow the research rules: empty fields are filled, filled fields are never overwritten,
except the status fields (website status, last verified, email verified) which describe the
latest check.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, field

import trafilatura

from leadforge.core.ids import new_ulid
from leadforge.enrichment import emailcheck
from leadforge.dedup.normalize import phone_e164, registrable_domain
from leadforge.enrichment.crawl import CrawlResult, CrawlSettings, PageResult, crawl_many, url_hash
from leadforge.enrichment.extract import extract, is_role, rank_emails

TEXT_LIMIT = 20000
ROLE_TYPE = "Role-based (info@, sales@)"
NAMED_TYPE = "Named person - published by company"


@dataclass
class EnrichStats:
    businesses: int = 0
    live: int = 0
    with_email: int = 0
    new_email: int = 0
    with_phone: int = 0
    needs_browser: int = 0
    unreachable: int = 0
    seconds: float = 0.0
    pages: int = 0
    page_ms: list[int] = field(default_factory=list)

    def summary(self) -> dict:
        ms = sorted(self.page_ms)
        pct = (lambda q: ms[min(int(len(ms) * q), len(ms) - 1)] if ms else 0)
        return {"businesses": self.businesses, "live": self.live, "with_email": self.with_email,
                "new_email": self.new_email, "with_phone": self.with_phone, "needs_browser": self.needs_browser,
                "unreachable": self.unreachable, "pages": self.pages, "seconds": round(self.seconds, 1),
                "sites_per_second": round(self.businesses / self.seconds, 2) if self.seconds else 0,
                "page_ms_median": pct(0.5), "page_ms_p95": pct(0.95)}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def select_targets(conn: sqlite3.Connection, scope: str, country: str | None, city: str | None,
                   limit: int, retry_failed: bool = False) -> list[sqlite3.Row]:
    """scope: new (discovered, not yet enriched) | missing-email (any lead with a website, no email) | all."""
    where = ["b.website IS NOT NULL", "b.website != ''", "b.permanently_closed = 0"]
    if scope == "new":
        where.append("b.research_status = 'discovered'")
    elif scope == "missing-email":
        where.append("b.general_email IS NULL AND b.decision_maker_email IS NULL")
    statuses = "('pending','failed')" if retry_failed else "('pending')"
    where.append(f"(t.lead_key IS NULL OR t.status IN {statuses})")
    args: list = []
    if country:
        where.append("b.country = ?")
        args.append(country)
    if city:
        where.append("b.city = ?")
        args.append(city)
    return conn.execute(
        f"""SELECT b.*, c.iso2 FROM business b LEFT JOIN country c ON c.name = b.country
            LEFT JOIN enrich_task t ON t.lead_key = b.lead_key WHERE {' AND '.join(where)}
            ORDER BY b.lead_id LIMIT ?""", (*args, limit)).fetchall()


def clean_text(html: str | None) -> str | None:
    if not html:
        return None
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True)
    except Exception:
        text = None
    return text[:TEXT_LIMIT] if text else None


def save_result(conn: sqlite3.Connection, b: sqlite3.Row, res: CrawlResult, run_id: str) -> dict:
    """Persist one crawl. Returns {'primary_email': ..., 'new_email': bool}."""
    ts = now()
    for p in res.pages:
        conn.execute(
            """INSERT OR REPLACE INTO page(url_hash, lead_key, url, final_url, http_status, page_type, fetched_at,
               ms, via_browser, text, jsonld, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (url_hash(p.url), b["lead_key"], p.url, p.final_url, p.status, p.page_type, ts, p.ms,
             int(p.via_browser), clean_text(p.html) if p.status == 200 else None,
             json.dumps(res.found.jsonld[:3], ensure_ascii=False) if p.page_type == "home" and res.found.jsonld
             else None, p.error))

    source_id = None
    if res.found.emails or res.found.phones:
        cur = conn.execute(
            """INSERT INTO source_record(lead_key, source_category, source_name, source_url, evidence_type,
               fields_supported, date_accessed, verification_status, run_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (b["lead_key"], "Company website - Contact page" if res.contact_page else "Company website - Footer",
             "company_website", res.contact_page or res.website, "website_contact_details",
             ",".join(k for k, v in (("email", res.found.emails), ("phone", res.found.phones)) if v),
             ts[:10], "published on own site", run_id))
        source_id = cur.lastrowid

    ranked = rank_emails(res.found.emails, res.site_domain)
    for e in ranked:
        dom = e.split("@", 1)[1]
        own = bool(res.site_domain) and (dom == res.site_domain or dom.endswith("." + res.site_domain))
        conn.execute(
            """INSERT INTO contact(lead_key, kind, value, role, role_based, syntax_ok, source_record_id, source_url,
               context, on_own_domain) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(lead_key, kind, value) DO UPDATE SET source_url=excluded.source_url,
               context=excluded.context, on_own_domain=excluded.on_own_domain""",
            (b["lead_key"], "email", e, "general", int(is_role(e)), 1, source_id, res.contact_page or res.website,
             res.found.emails[e], int(own)))
    for ph, ctx in res.found.phones.items():
        conn.execute("""INSERT OR IGNORE INTO contact(lead_key, kind, value, role, source_record_id, source_url, context)
                        VALUES (?,?,?,?,?,?,?)""", (b["lead_key"], "phone", ph, "general", source_id,
                                                    res.contact_page or res.website, ctx))

    jf = res.jsonld
    own_e164 = b["phone_e164"] or phone_e164(b["phone"], b["iso2"]) if b["phone"] else b["phone_e164"]
    fill = {
        "general_email": ranked[0] if ranked else None,
        "email_type": (ROLE_TYPE if is_role(ranked[0]) else NAMED_TYPE) if ranked else None,
        "phone": next(iter(res.found.phones), None),
        "phone_e164": own_e164 or next(iter(res.found.phones), None),
        "contact_page_url": res.contact_page,
        "linkedin_company_page": res.found.linkedin,
        "street_address": jf.get("street"),
        "postcode": jf.get("postcode"),
        "opt_out_notice": "Yes" if res.found.opt_out else ("No" if res.website_status == "Live" else None),
    }
    sets = {k: v for k, v in fill.items() if v and not b[k]}
    if b["general_email"] and ranked:
        extra = [e for e in ranked if e != b["general_email"]]
    else:
        extra = ranked[1:]
    if extra and not b["additional_emails"]:
        sets["additional_emails"] = "; ".join(extra[:10])
    phones = list(res.found.phones)
    known = {b["phone_e164"], sets.get("phone_e164")}
    more = [p for p in phones if p not in known]
    if more and not b["additional_phones"]:
        sets["additional_phones"] = "; ".join(more[:10])
    if res.found.opt_out and b["opt_out_notice"] != "Yes":
        sets["opt_out_notice"] = "Yes"            # an opt-out notice always wins: it blocks outreach
    # Status fields always reflect the latest check.
    sets["website_status"] = res.website_status
    sets["last_verified"] = ts[:10]
    sets["last_researched_at"] = ts
    sets["last_updated_at"] = ts
    if b["research_status"] == "discovered":
        sets["research_status"] = "enriched"
    conn.execute(f"UPDATE business SET {', '.join(f'{k}=?' for k in sets)} WHERE lead_key=?",
                 (*sets.values(), b["lead_key"]))

    status = "needs_browser" if res.needs_browser and not res.found.emails else "done"
    conn.execute(
        """INSERT INTO enrich_task(lead_key, status, attempts, pages, used_browser, ms, error, run_id, updated_at)
           VALUES (?,?,1,?,?,?,?,?,?) ON CONFLICT(lead_key) DO UPDATE SET status=excluded.status,
           attempts=attempts+1, pages=excluded.pages, used_browser=excluded.used_browser, ms=excluded.ms,
           error=excluded.error, run_id=excluded.run_id, updated_at=excluded.updated_at""",
        (b["lead_key"], status, len(res.pages), int(any(p.via_browser for p in res.pages)), res.ms,
         res.pages[0].error if res.pages and res.pages[0].error else None, run_id, ts))
    return {"primary_email": sets.get("general_email") or b["general_email"], "new_email": "general_email" in sets}


async def verify_emails(conn: sqlite3.Connection, lead_keys: list[str]) -> int:
    """MX-check every email contact of these businesses and set Email Verified on the business."""
    if not lead_keys:
        return 0
    marks = ",".join("?" * len(lead_keys))
    rows = conn.execute(f"SELECT contact_id, lead_key, value FROM contact WHERE kind='email' AND lead_key IN ({marks})",
                        lead_keys).fetchall()
    domains = {r["value"].split("@", 1)[1] for r in rows}
    biz = conn.execute(f"SELECT lead_key, general_email FROM business WHERE lead_key IN ({marks})", lead_keys).fetchall()
    domains |= {b["general_email"].split("@", 1)[1] for b in biz if b["general_email"] and "@" in b["general_email"]}
    mx = await emailcheck.check_domains(domains, conn)
    for r in rows:
        ok = mx.get(r["value"].split("@", 1)[1])
        conn.execute("UPDATE contact SET mx_ok=?, verify_status=? WHERE contact_id=?",
                     (None if ok is None else int(ok), emailcheck.status_for(ok), r["contact_id"]))
    n = 0
    for b in biz:
        if b["general_email"] and "@" in b["general_email"]:
            ok = mx.get(b["general_email"].split("@", 1)[1])
            if ok is not None:
                conn.execute("UPDATE business SET email_verified=? WHERE lead_key=?",
                             (emailcheck.status_for(ok), b["lead_key"]))
                n += 1
    conn.commit()
    return n


def run(conn: sqlite3.Connection, targets: list[sqlite3.Row], settings: CrawlSettings, on_result=None,
        transport=None) -> EnrichStats:
    stats = EnrichStats(businesses=len(targets))
    run_id = f"enrich-{new_ulid()}"
    conn.execute("INSERT INTO run(run_id, kind, started_at, config_json) VALUES (?,?,?,?)",
                 (run_id, "research", now(), json.dumps({"kind": "enrich", "targets": len(targets),
                                                         "settings": settings.__dict__})))
    conn.commit()
    by_key = {b["lead_key"]: b for b in targets}
    start = time.perf_counter()

    def handle(res: CrawlResult) -> None:
        b = by_key[res.lead_key]
        with conn:
            out = save_result(conn, b, res, run_id)
        stats.pages += len(res.pages)
        stats.page_ms += [p.ms for p in res.pages if p.status]
        stats.live += res.website_status == "Live" or res.website_status.startswith("Redirects")
        stats.unreachable += res.website_status.startswith("Unreachable")
        stats.with_email += bool(out["primary_email"])
        stats.new_email += out["new_email"]
        stats.with_phone += bool(res.found.phones or b["phone"])
        stats.needs_browser += res.needs_browser and not res.found.emails
        if on_result:
            on_result(b, res, out)

    items = [(b["lead_key"], b["website"], b["iso2"]) for b in targets]
    asyncio.run(_run_all(conn, items, settings, handle, transport, list(by_key)))
    stats.seconds = time.perf_counter() - start
    conn.execute("UPDATE run SET finished_at=?, stats_json=? WHERE run_id=?",
                 (now(), json.dumps(stats.summary()), run_id))
    conn.commit()
    return stats


async def _run_all(conn, items, settings, handle, transport, keys):
    await crawl_many(items, settings, handle, transport)
    await verify_emails(conn, keys)


def browser_fallback(conn: sqlite3.Connection, session_factory, limit: int, on_result=None) -> int:
    """Sites that render nothing without JavaScript: load home + contact page in SeleniumBase."""
    rows = conn.execute("""SELECT b.*, c.iso2 FROM enrich_task t JOIN business b ON b.lead_key = t.lead_key
                           LEFT JOIN country c ON c.name = b.country WHERE t.status = 'needs_browser'
                           ORDER BY b.lead_id LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return 0
    run_id = f"enrich-browser-{new_ulid()}"
    conn.execute("INSERT INTO run(run_id, kind, started_at) VALUES (?,?,?)", (run_id, "research", now()))
    done = 0
    with session_factory() as s:
        for b in rows:
            res = CrawlResult(b["lead_key"], b["website"], registrable_domain(b["website"]))
            for url, kind in [(b["website"], "home")] + ([(b["contact_page_url"], "contact")] if b["contact_page_url"] else []):
                try:
                    s.sb.open(url)
                    s.sb.sleep(2)
                    html = s.sb.get_page_source()
                except Exception as exc:
                    res.pages.append(PageResult(url, None, None, kind, 0, error=str(exc)[:300], via_browser=True))
                    continue
                res.pages.append(PageResult(url, s.sb.get_current_url(), 200, kind, 0, html=html, via_browser=True))
                f = extract(html, url, b["iso2"], res.site_domain)
                res.merge(f)
                if kind == "home" and not b["contact_page_url"]:
                    contact = next((u for u, k in f.links if k == "contact"), None)
                    if contact:
                        try:
                            s.sb.open(contact)
                            s.sb.sleep(2)
                            html2 = s.sb.get_page_source()
                            res.pages.append(PageResult(contact, s.sb.get_current_url(), 200, "contact", 0,
                                                        html=html2, via_browser=True))
                            res.merge(extract(html2, contact, b["iso2"], res.site_domain))
                        except Exception:
                            pass
            res.website_status = "Live" if any(p.status == 200 for p in res.pages) else f"Unreachable ({now()[:10]})"
            with conn:
                out = save_result(conn, b, res, run_id)
                conn.execute("UPDATE enrich_task SET status='done', used_browser=1 WHERE lead_key=?", (b["lead_key"],))
            done += 1
            if on_result:
                on_result(b, res, out)
    asyncio.run(verify_emails(conn, [b["lead_key"] for b in rows]))
    return done


def bench(conn: sqlite3.Connection, country: str | None, city: str | None, limit: int,
          settings: CrawlSettings) -> dict:
    """Re-crawl leads that already have a general email (no writes) and measure recall against it."""
    where, args = ["b.website IS NOT NULL", "b.general_email IS NOT NULL"], []
    if country:
        where.append("b.country=?")
        args.append(country)
    if city:
        where.append("b.city=?")
        args.append(city)
    rows = conn.execute(f"""SELECT b.lead_key, b.website, b.general_email, c.iso2 FROM business b
                            LEFT JOIN country c ON c.name=b.country WHERE {' AND '.join(where)}
                            ORDER BY RANDOM() LIMIT ?""", (*args, limit)).fetchall()
    want = {r["lead_key"]: {e.strip().lower() for e in r["general_email"].replace(",", ";").split(";") if "@" in e}
            for r in rows}
    hit = any_email = live = 0
    ms: list[int] = []

    def handle(res: CrawlResult) -> None:
        nonlocal hit, any_email, live
        found = set(res.found.emails)
        hit += bool(want[res.lead_key] & found)
        any_email += bool(found)
        live += res.website_status == "Live" or res.website_status.startswith("Redirects")
        ms.extend(p.ms for p in res.pages if p.status)

    start = time.perf_counter()
    asyncio.run(crawl_many([(r["lead_key"], r["website"], r["iso2"]) for r in rows], settings, handle))
    secs = time.perf_counter() - start
    ms.sort()
    n = len(rows) or 1
    return {"sites": len(rows), "seconds": round(secs, 1), "sites_per_second": round(len(rows) / secs, 2) if secs else 0,
            "live": live, "found_any_email": any_email, "found_known_email": hit,
            "recall_known_email": round(hit / n, 3), "any_email_rate": round(any_email / n, 3),
            "page_ms_median": ms[len(ms) // 2] if ms else 0, "page_ms_p95": ms[int(len(ms) * 0.95)] if ms else 0}
