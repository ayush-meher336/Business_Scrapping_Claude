"""Build agent batches: compact, labelled evidence per business plus the full instructions."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from leadforge.ai_processing.contract import OUTPUT_SCHEMA, PROMPT_VERSION, instructions
from leadforge.enrichment.extract import CHALLENGE_RE, PARKED_RE
from leadforge.core.ids import new_ulid
from leadforge.niches.model import NicheConfig

CHARS_PER_BUSINESS = 1800         # spec: roughly 1,000-2,000 characters of cleaned text per business
PAGE_ORDER = {"about": 0, "home": 1, "products": 2, "contact": 3, "location": 4, "other": 5}


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def build_evidence(conn: sqlite3.Connection, b: sqlite3.Row, budget: int = CHARS_PER_BUSINESS) -> list[dict]:
    ev: list[dict] = []
    listing = "; ".join(x for x in (
        f"Google Maps category: {b['maps_category']}" if b["maps_category"] else "",
        f"Address: {b['street_address']}, {b['suburb_area'] or ''} {b['city'] or ''}".strip(", ") if b["street_address"] else "",
        f"Permanently closed on Google Maps" if b["permanently_closed"] else "",
    ) if x)
    if listing:
        ev.append({"src": "S0", "type": "listing", "url": b["google_maps_url"] or b["source_url"], "text": listing})
    pages = conn.execute("SELECT url, final_url, page_type, text FROM page WHERE lead_key=? AND text IS NOT NULL",
                         (b["lead_key"],)).fetchall()
    pages = [p for p in pages if not (CHALLENGE_RE.search(p["text"][:3000]) or PARKED_RE.search(p["text"][:3000]))]
    pages = sorted(pages, key=lambda p: PAGE_ORDER.get(p["page_type"], 9))
    left = budget - len(listing)
    share = max(left // max(len(pages), 1), 300) if pages else 0
    for i, p in enumerate(pages, start=1):
        if left <= 0:
            break
        text = _squeeze(p["text"])[: min(share, left)]
        if text:
            ev.append({"src": f"S{i}", "type": f"website:{p['page_type']}", "url": p["final_url"] or p["url"], "text": text})
            left -= len(text)
    if b["notes"]:
        ev.append({"src": "N1", "type": "prior_research_notes", "url": None, "text": _squeeze(b["notes"])[:500]})
    return ev


def select(conn: sqlite3.Connection, limit: int, country: str | None, redo: bool) -> list[sqlite3.Row]:
    where = ["EXISTS (SELECT 1 FROM page p WHERE p.lead_key=b.lead_key AND p.text IS NOT NULL)",
             "COALESCE(b.website_status, '') NOT IN ('Blocked', 'Parked')",
             "NOT EXISTS (SELECT 1 FROM ai_task t WHERE t.lead_key=b.lead_key AND t.status='exported')"]
    if not redo:
        where.append("b.company_description IS NULL")
        where.append("NOT EXISTS (SELECT 1 FROM ai_task t WHERE t.lead_key=b.lead_key AND t.status IN ('accepted','review'))")
    args: list = []
    if country:
        where.append("b.country=?")
        args.append(country)
    return conn.execute(f"SELECT b.* FROM business b WHERE {' AND '.join(where)} ORDER BY b.lead_id LIMIT ?",
                        (*args, limit)).fetchall()


def export(conn: sqlite3.Connection, niche: NicheConfig, out_dir: Path, batch_size: int, limit: int,
           country: str | None = None, redo: bool = False) -> list[Path]:
    rows = select(conn, limit, country, redo)
    enum = lambda name: [r[0] for r in conn.execute(
        "SELECT value FROM enum_value WHERE list_name=? ORDER BY position", (name,))]
    allowed = {"industry_type": enum(niche.industry_enum), "material_focus": enum(niche.material_enum)}
    text = instructions(niche.display_name, niche.qualification.relevant_if, niche.qualification.irrelevant_if,
                        niche.description.must_mention, niche.description.max_words, niche.description.hook_max_words,
                        allowed["industry_type"], allowed["material_focus"])
    (out_dir / "batches").mkdir(parents=True, exist_ok=True)
    (out_dir / "results").mkdir(parents=True, exist_ok=True)
    paths, now = [], dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        batch_id = f"b{dt.date.today():%Y%m%d}-{new_ulid()[-8:].lower()}"
        path = out_dir / "batches" / f"{batch_id}.json"
        conn.execute("INSERT INTO ai_batch(batch_id, prompt_version, niche, path, created_at) VALUES (?,?,?,?,?)",
                     (batch_id, PROMPT_VERSION, niche.niche, str(path), now))
        records = []
        for b in chunk:
            ev = build_evidence(conn, b)
            records.append({"id": b["lead_key"], "name": b["company_name"],
                            "location": ", ".join(x for x in (b["suburb_area"], b["city"], b["country"]) if x),
                            "website": b["website"], "evidence": ev})
            conn.execute("INSERT INTO ai_task(lead_key, batch_id, evidence) VALUES (?,?,?)",
                         (b["lead_key"], batch_id, json.dumps(ev, ensure_ascii=False)))
        payload = {"batch_id": batch_id, "prompt_version": PROMPT_VERSION, "niche": niche.niche,
                   "results_file": f"results/{batch_id}.result.json", "instructions": text,
                   "allowed": allowed, "output_schema": OUTPUT_SCHEMA, "records": records}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        paths.append(path)
    conn.commit()
    return paths
