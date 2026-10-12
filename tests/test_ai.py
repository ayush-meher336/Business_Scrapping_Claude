"""Phase 5: batch export and validated import of agent results. All data invented."""
import json

import pytest

from leadforge.ai_processing.export import export
from leadforge.ai_processing.importer import grounding_problems, import_results, material_value, strip_citations
from leadforge.niches.model import load_niche
from test_geography import conn  # noqa: F401
from test_migration import workbook  # noqa: F401
from test_niche import ROOT

ABOUT = ("W & K Scrap Metal has bought and processed scrap for 35 years. We accept copper, brass, aluminium, "
         "steel, white goods and hot water tanks, and offer skip bin hire across western Sydney.")


@pytest.fixture
def ai(conn, tmp_path):  # noqa: F811
    for lead_id in (1, 2):
        key = conn.execute("SELECT lead_key FROM business WHERE lead_id=?", (lead_id,)).fetchone()[0]
        conn.execute("""INSERT INTO page(url_hash, lead_key, url, page_type, fetched_at, text, http_status)
                        VALUES (?,?,?,?,?,?,200)""", (f"h{lead_id}", key, f"https://x{lead_id}.com/about", "about",
                                                       "now", ABOUT if lead_id == 1 else "Copper buyer. Call us."))
    conn.execute("UPDATE business SET notes='35+ years buying scrap; skip bins.' WHERE lead_id=1")
    conn.commit()
    niche = load_niche(ROOT, "scrapyards")
    paths = export(conn, niche, tmp_path, batch_size=20, limit=10)
    batch = json.loads(paths[0].read_text(encoding="utf-8"))
    return conn, tmp_path, niche, batch


def keys(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT lead_id, lead_key FROM business")}


def write(tmp_path, batch, results):
    (tmp_path / "results" / f"{batch['batch_id']}.result.json").write_text(json.dumps(results), encoding="utf-8")


def run_import(conn, tmp_path, niche):
    return import_results(conn, tmp_path, niche.description.max_words, niche.description.hook_max_words,
                          niche.industry_enum, niche.material_enum)


def test_export_batch_is_self_contained(ai):
    conn, _, _, batch = ai
    assert len(batch["records"]) == 2 and batch["results_file"].startswith("results/")
    assert "Scrap Yard / Metal Recycler" in batch["allowed"]["industry_type"]
    rec = batch["records"][0]
    assert {e["src"] for e in rec["evidence"]} >= {"S1", "N1"}         # website page + prior notes
    assert "Use ONLY the text" in batch["instructions"]
    assert conn.execute("SELECT COUNT(*) FROM ai_task WHERE status='exported'").fetchone()[0] == 2
    assert export(conn, load_niche(ROOT, "scrapyards"), ai[1], 20, 10) == []   # not exported twice


def test_good_result_accepted_bad_ones_reviewed(ai):
    conn, tmp_path, niche, batch = ai
    k = keys(conn)
    write(tmp_path, batch, [
        {"id": k[1], "relevant": "yes", "industry_type": "Scrap Yard / Metal Recycler",
         "material_focus": ["Mixed metals"],
         "description": "Buys copper, brass, aluminium and steel scrap [S1], plus white goods and hot water tanks [S1]; also hires skip bins [S1].",
         "personalisation_hook": "Takes hot water tanks and offers skip bin hire [S1].",
         "evidence_ids": ["S1"], "confidence": "high", "concerns": []},
        # Invented material + generic phrase + a source it was not given -> review, not published.
        {"id": k[2], "relevant": "yes", "industry_type": "Scrap Buyer / Trader", "material_focus": [],
         "description": "A leading buyer of copper and lead batteries [S1] with 40 trucks [S7].",
         "evidence_ids": ["S1", "S7"], "confidence": "high", "concerns": []},
        {"id": "not-in-batch", "relevant": "yes", "confidence": "high"},
    ])
    s = run_import(conn, tmp_path, niche)
    assert (s.accepted, s.review, s.unknown_ids) == (1, 1, 1)
    b1 = conn.execute("SELECT * FROM business WHERE lead_id=1").fetchone()
    assert b1["company_description"].startswith("Buys copper") and "[S1]" not in b1["company_description"]
    assert b1["material_focus"] == "Mixed metals" and b1["human_review_required"] == "No"
    b2 = conn.execute("SELECT * FROM business WHERE lead_id=2").fetchone()
    assert b2["company_description"] is None and b2["human_review_required"] == "Yes"
    assert "lead" in b2["review_notes"] and "S7" in b2["review_notes"] and "AI draft" in b2["review_notes"]
    probs = conn.execute("SELECT problems FROM ai_result WHERE lead_key=?", (k[2],)).fetchone()[0]
    assert "generic phrasing" in probs and "'40 trucks' not in evidence" in probs


def test_disallowed_value_rejected_and_missing_reported(ai):
    conn, tmp_path, niche, batch = ai
    k = keys(conn)
    write(tmp_path, batch, [{"id": k[1], "relevant": "yes", "industry_type": "Spaceship Dealer",
                             "description": "Buys copper [S1].", "confidence": "high"}])
    s = run_import(conn, tmp_path, niche)
    assert s.rejected == 1 and s.missing == 1
    assert conn.execute("SELECT industry_type FROM business WHERE lead_id=1").fetchone()[0] is None
    assert conn.execute("SELECT status FROM ai_batch").fetchone()[0] == "partial"


def test_helpers():
    assert strip_citations("Buys copper [S1] and brass [S1, S2].") == "Buys copper and brass."
    assert grounding_problems("Buys aluminum [S1]", "we buy aluminium", "d") == []
    assert grounding_problems("Operating since 1985", "family owned", "d")
    assert material_value(["Batteries"]) == "Batteries"
    assert material_value(["Stainless steel", "Non-ferrous (copper, brass, aluminium)"]) == "Mixed metals"
