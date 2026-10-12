from pathlib import Path

from leadforge.niches.model import load_niche

ROOT = Path(__file__).resolve().parents[1]


def test_scrapyards_niche_loads():
    cfg = load_niche(ROOT, "scrapyards")
    headers = [c.header for c in cfg.sheet_columns]
    assert headers[0] == "Lead ID" and "OK to Email?" in headers
    assert len(headers) == len(set(headers))
    owners = {c.header: c.owner for c in cfg.sheet_columns}
    assert owners["Outreach Status"] == "sales" and owners["Notes"] == "shared_append"
