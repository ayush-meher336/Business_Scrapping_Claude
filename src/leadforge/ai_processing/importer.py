"""Validate the agent's results and write accepted ones; anything doubtful goes to human review.

Checks per result: matched to an exported id; allowed category values; source tags exist in that
business's evidence; every material / number / scale claim in the description and hook appears
in the evidence; no generic marketing phrasing; confidence consistent with the findings.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from leadforge.ai_processing.contract import AIResult

GENERIC = re.compile(r"(?i)\b(leading|one[- ]stop[- ]shop|premier|world[- ]class|best[- ]in|industry[- ]leading|"
                     r"top[- ]rated|second to none|state[- ]of[- ]the[- ]art|unparalleled|trusted partner)\b")
CITATION = re.compile(r"\s*\[(S\d+|N\d+)(?:\s*,\s*(?:S\d+|N\d+))*\]")
MATERIALS = ["copper", "brass", "aluminium", "aluminum", "steel", "stainless", "iron", "lead", "zinc", "nickel",
             "tin", "titanium", "battery", "batteries", "e-waste", "ewaste", "electronic", "cable", "wire", "radiator",
             "catalytic", "car", "cars", "vehicle", "vehicles", "white goods", "appliance", "paper", "cardboard",
             "plastic", "tyre", "tire", "glass", "textile", "hot water", "motor", "transformer", "ferrous",
             "non-ferrous", "skip", "demolition", "container", "export", "import", "pickup", "pick-up", "collection"]
SCALE = re.compile(r"(?i)\b(\d[\d,.]*\s*(%|percent|tonnes?|tons?|kg|years?|employees|staff|sites|branches|locations|"
                   r"trucks?|m2|sqm|acres?|hectares?)|largest|biggest|national|international|global|since \d{4})\b")
SYNONYMS = {"aluminum": "aluminium", "tire": "tyre", "ewaste": "e-waste", "pick-up": "pickup", "batteries": "battery",
            "vehicles": "vehicle", "cars": "car"}


@dataclass
class ImportStats:
    files: int = 0
    results: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    irrelevant: int = 0
    missing: int = 0
    unknown_ids: int = 0
    malformed: int = 0
    problems: list[str] = field(default_factory=list)


def _norm(word: str) -> str:
    w = word.casefold()
    return SYNONYMS.get(w, w)


def strip_citations(text: str | None) -> str | None:
    return CITATION.sub("", text).strip() if text else text


def grounding_problems(text: str | None, evidence_text: str, label: str) -> list[str]:
    """Claims in `text` that the evidence does not contain."""
    if not text:
        return []
    probs = []
    ev = evidence_text.casefold()
    ev_norm = " ".join(_norm(w) for w in re.findall(r"[\w-]+", ev))
    clean = strip_citations(text).casefold()
    for m in MATERIALS:
        if re.search(rf"\b{re.escape(m)}\b", clean):
            n = _norm(m)
            if not re.search(rf"\b{re.escape(n)}\b", ev_norm) and not re.search(rf"\b{re.escape(m)}\b", ev):
                probs.append(f"{label}: '{m}' not in evidence")
    for m in SCALE.finditer(clean):
        claim = m.group(0)
        nums = re.findall(r"\d[\d,.]*", claim)
        if nums:
            if not all(n.replace(",", "") in ev.replace(",", "") for n in nums):
                probs.append(f"{label}: '{claim}' not in evidence")
        elif claim.casefold() not in ev:
            probs.append(f"{label}: '{claim}' not in evidence")
    if GENERIC.search(clean):
        probs.append(f"{label}: generic phrasing '{GENERIC.search(clean).group(0)}'")
    return probs


def material_value(values: list[str]) -> str | None:
    """The Leads tab holds one Material Focus value; several metal groups -> 'Mixed metals'."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    metals = {"Ferrous (HMS, shred, cast)", "Non-ferrous (copper, brass, aluminium)", "Stainless steel", "Mixed metals"}
    if all(v in metals for v in values):
        return "Mixed metals"
    return values[0]


def check(r: AIResult, evidence: list[dict], allowed: dict[str, set[str]], max_words: int, hook_words: int) -> list[str]:
    probs: list[str] = []
    tags = {e["src"] for e in evidence}
    ev_text = " ".join(e["text"] for e in evidence)
    if r.industry_type and r.industry_type not in allowed["industry_type"]:
        probs.append(f"industry_type '{r.industry_type}' not an allowed value")
    bad = [m for m in r.material_focus if m not in allowed["material_focus"]]
    if bad:
        probs.append(f"material_focus not allowed: {bad}")
    cited = set(re.findall(r"\b([SN]\d+)\b", " ".join(CITATION.findall(r.description or "") +
                                                     CITATION.findall(r.personalisation_hook or "")))) | set(r.evidence_ids)
    cited |= set(re.findall(r"\[([SN]\d+)", (r.description or "") + (r.personalisation_hook or "")))
    unknown = cited - tags
    if unknown:
        probs.append(f"cites sources not given: {sorted(unknown)}")
    if r.relevant == "yes" and r.description and not re.search(r"\[[SN]\d+", r.description):
        probs.append("description has no source tags")
    if r.description and len(strip_citations(r.description).split()) > max_words + 10:
        probs.append(f"description longer than {max_words} words")
    if r.personalisation_hook and len(strip_citations(r.personalisation_hook).split()) > hook_words + 5:
        probs.append(f"hook longer than {hook_words} words")
    probs += grounding_problems(r.description, ev_text, "description")
    probs += grounding_problems(r.personalisation_hook, ev_text, "hook")
    return probs


def _load(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    if not isinstance(data, list):
        raise ValueError("results file must be a JSON array of result objects")
    return data


def import_results(conn: sqlite3.Connection, ai_dir: Path, max_words: int, hook_words: int,
                   industry_enum: str, material_enum: str) -> ImportStats:
    stats = ImportStats()
    allowed = {
        "industry_type": {r[0] for r in conn.execute("SELECT value FROM enum_value WHERE list_name=?", (industry_enum,))},
        "material_focus": {r[0] for r in conn.execute("SELECT value FROM enum_value WHERE list_name=?", (material_enum,))},
    }
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    batches = conn.execute("SELECT * FROM ai_batch WHERE status != 'imported'").fetchall()
    for batch in batches:
        path = ai_dir / "results" / f"{batch['batch_id']}.result.json"
        if not path.exists():
            continue
        stats.files += 1
        try:
            raw = _load(path)
        except (ValueError, json.JSONDecodeError) as exc:
            stats.malformed += 1
            stats.problems.append(f"{path.name}: {exc}")
            continue
        tasks = {t["lead_key"]: t for t in conn.execute("SELECT * FROM ai_task WHERE batch_id=?", (batch["batch_id"],))}
        seen: set[str] = set()
        with conn:
            for item in raw:
                stats.results += 1
                try:
                    r = AIResult.model_validate(item)
                except ValidationError as exc:
                    stats.malformed += 1
                    stats.problems.append(f"{batch['batch_id']}: malformed result ({exc.errors()[0]['msg']})")
                    continue
                task = tasks.get(r.id)
                if task is None or r.id in seen:
                    # Never attach a result to a business that was not in this batch.
                    stats.unknown_ids += 1
                    stats.problems.append(f"{batch['batch_id']}: unknown or repeated id {r.id}")
                    continue
                seen.add(r.id)
                evidence = json.loads(task["evidence"])
                probs = check(r, evidence, allowed, max_words, hook_words)
                _apply(conn, batch, r, probs, now, stats)
            for lead_key in tasks.keys() - seen:
                stats.missing += 1
                conn.execute("UPDATE ai_task SET status='missing' WHERE lead_key=? AND batch_id=?",
                             (lead_key, batch["batch_id"]))
            conn.execute("UPDATE ai_batch SET status=?, imported_at=? WHERE batch_id=?",
                         ("imported" if not tasks.keys() - seen else "partial", now, batch["batch_id"]))
    return stats


def _apply(conn, batch, r: AIResult, probs: list[str], now: str, stats: ImportStats) -> None:
    b = conn.execute("SELECT * FROM business WHERE lead_key=?", (r.id,)).fetchone()
    grounded = not any("not in evidence" in p or "cites sources" in p or "generic" in p for p in probs)
    schema_bad = any("not an allowed value" in p or "not allowed" in p for p in probs)
    if schema_bad:
        decision = "rejected"
    elif r.relevant == "no" and r.confidence == "high":
        decision = "accepted"
    elif probs or r.confidence == "low" or r.relevant == "unsure":
        decision = "review"
    else:
        decision = "accepted"
    conn.execute("INSERT OR REPLACE INTO ai_result VALUES (?,?,?,?,?,?,?,?)",
                 (r.id, batch["batch_id"], batch["prompt_version"], r.model_dump_json(), int(grounded),
                  "; ".join(probs) or None, decision, now))
    conn.execute("UPDATE ai_task SET status=? WHERE lead_key=? AND batch_id=?", (decision, r.id, batch["batch_id"]))
    setattr(stats, decision, getattr(stats, decision) + 1)
    if decision == "rejected":
        return

    sets: dict = {"confidence": r.confidence, "last_researched_at": now, "last_updated_at": now}
    notes = [f"AI {now[:10]}: " + "; ".join(r.concerns + probs)] if (r.concerns or probs) else []
    if decision == "accepted" and r.relevant == "yes":
        if r.description and not b["company_description"]:
            sets["company_description"] = strip_citations(r.description)
        if r.personalisation_hook and not b["personalisation_hook"]:
            sets["personalisation_hook"] = strip_citations(r.personalisation_hook)
        if r.industry_type and not b["industry_type"]:
            sets["industry_type"] = r.industry_type
        mv = material_value(r.material_focus)
        if mv and not b["material_focus"]:
            sets["material_focus"] = mv
        sets["human_review_required"] = "No"
        sets["research_status"] = "qualified" if b["research_status"] != "legacy" else b["research_status"]
    elif decision == "accepted" and r.relevant == "no":
        stats.irrelevant += 1
        sets["human_review_required"] = "No"
        sets["research_status"] = "irrelevant" if b["research_status"] != "legacy" else b["research_status"]
        notes.insert(0, f"AI {now[:10]}: not relevant to niche")
    else:
        sets["human_review_required"] = "Yes"
        if b["research_status"] != "legacy":
            sets["research_status"] = "needs_review"
        # Keep the draft for the reviewer without publishing it as the description.
        if r.description:
            notes.append(f"AI draft: {strip_citations(r.description)}")
    if notes:
        prev = b["review_notes"] or ""
        sets["review_notes"] = (prev + "\n" if prev else "") + "\n".join(notes)
    conn.execute(f"UPDATE business SET {', '.join(f'{k}=?' for k in sets)} WHERE lead_key=?", (*sets.values(), r.id))
