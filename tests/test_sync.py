"""Phase 6: two-way sheet sync against an in-memory sheet. Invented data."""
import pytest

from leadforge.niches.model import load_niche
from leadforge.sync.client import MemorySheets, col_letter
from leadforge.sync.run import run_sync
from test_geography import conn  # noqa: F401
from test_migration import workbook  # noqa: F401
from test_niche import ROOT


@pytest.fixture
def env(conn):  # noqa: F811
    return conn, MemorySheets(), load_niche(ROOT, "scrapyards")


def table(sheet, tab):
    grid = sheet.tabs[tab]
    head = grid[0]
    return [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in grid[1:]]


def by_name(sheet):
    return {r["Company Name"]: r for r in table(sheet, "Leads")}


def edit(sheet, tab, name_col, name, header, value):
    grid = sheet.tabs[tab]
    c = grid[0].index(header)
    for r in grid[1:]:
        if r[grid[0].index(name_col)] == name:
            while len(r) <= c:
                r.append("")
            r[c] = value


def test_first_sync_writes_everything_then_is_idempotent(env):
    conn, sheet, niche = env
    out = run_sync(conn, sheet, niche)
    assert out["Leads"]["appended_rows"] == 2 and out["Search Zones"]["appended_rows"] == 2
    rows = by_name(sheet)
    w = rows["W & K Scrap Metal"]
    assert w["Lead ID"] == "1" and len(w["Lead Key"]) == 26 and w["OK to Email?"] == "No"
    assert w["OK to Email reason"] == "email Not checked"
    calls = sheet.calls
    again = run_sync(conn, sheet, niche)
    assert again["Leads"]["updated_cells"] == 0 and again["Leads"]["appended_rows"] == 0
    assert sheet.calls - calls <= 6                        # reads only, no writes


def test_sales_edits_win_and_research_updates_flow(env):
    conn, sheet, niche = env
    run_sync(conn, sheet, niche)
    edit(sheet, "Leads", "Company Name", "W & K Scrap Metal", "Outreach Status", "Email 1 sent")
    edit(sheet, "Leads", "Company Name", "W & K Scrap Metal", "Priority", "A - High fit")
    # Meanwhile research finds a description and a valid email.
    conn.execute("UPDATE business SET company_description='Buys copper.', email_verified='Valid' WHERE lead_id=1")
    # ... and tries (wrongly) to change a sales field locally: the sheet must keep the person's value.
    conn.execute("UPDATE business SET priority='C - Low fit' WHERE lead_id=1")
    conn.commit()
    run_sync(conn, sheet, niche)
    b = conn.execute("SELECT * FROM business WHERE lead_id=1").fetchone()
    assert b["outreach_status"] == "Email 1 sent" and b["priority"] == "A - High fit"
    row = by_name(sheet)["W & K Scrap Metal"]
    assert row["Company Description"] == "Buys copper." and row["Priority"] == "A - High fit"
    assert row["OK to Email?"] == "Yes"


def test_sheet_edit_of_research_field_wins_and_is_logged(env):
    conn, sheet, niche = env
    run_sync(conn, sheet, niche)
    edit(sheet, "Leads", "Company Name", "Safari Copper", "Phone", "02 1111 2222")
    conn.execute("UPDATE business SET phone='02 3333 4444' WHERE lead_id=2")     # both changed
    conn.commit()
    run_sync(conn, sheet, niche)
    assert conn.execute("SELECT phone FROM business WHERE lead_id=2").fetchone()[0] == "02 1111 2222"
    assert by_name(sheet)["Safari Copper"]["Phone"] == "02 1111 2222"
    assert conn.execute("SELECT resolution FROM sync_conflict WHERE field='Phone'").fetchone()[0] == "sheet_won"


def test_notes_from_both_sides_are_kept(env):
    conn, sheet, niche = env
    run_sync(conn, sheet, niche)
    edit(sheet, "Leads", "Company Name", "Safari Copper", "Notes", "Called 12 Oct, call back Friday.")
    conn.execute("UPDATE business SET notes='[auto 2026-10-12] website re-checked' WHERE lead_id=2")
    conn.commit()
    run_sync(conn, sheet, niche)
    notes = conn.execute("SELECT notes FROM business WHERE lead_id=2").fetchone()[0]
    assert "call back Friday" in notes and "website re-checked" in notes


def test_rows_from_another_machine_and_hand_typed_rows(env):
    conn, sheet, niche = env
    run_sync(conn, sheet, niche)
    head = sheet.tabs["Leads"][0]
    other = [""] * len(head)
    other[head.index("Lead Key")] = "01OTHERMACHINEKEY000000000"
    other[head.index("Lead ID")] = "3"
    other[head.index("Company Name")] = "Northside Metals"
    typed = [""] * len(head)
    typed[head.index("Company Name")] = "Walk-in Scrap Co"
    sheet.tabs["Leads"] += [other, typed]
    # This machine meanwhile created its own lead 3: it must be renumbered, not clash.
    conn.execute("INSERT INTO business(lead_key, lead_id, company_name, country) VALUES ('01LOCALKEY', 3, 'Local New', 'Australia')")
    conn.commit()
    out = run_sync(conn, sheet, niche)
    assert out["Leads"]["pulled_rows_new"] == 1 and out["Leads"]["created_from_sheet"] == 1
    ids = {r["company_name"]: r["lead_id"] for r in conn.execute("SELECT company_name, lead_id FROM business")}
    assert ids["Northside Metals"] == 3 and ids["Local New"] > 3 and ids["Walk-in Scrap Co"] > 3
    assert len(set(ids.values())) == len(ids)
    rows = by_name(sheet)
    assert rows["Walk-in Scrap Co"]["Lead Key"] and rows["Local New"]["Lead ID"] == str(ids["Local New"])


def test_keyless_workbook_rows_are_adopted_not_duplicated(env):
    conn, sheet, niche = env
    # The original workbook uploaded as-is: no Lead Key column.
    sheet.tabs["Leads"] = [["Lead ID", "Company Name", "Phone", "Outreach Status"],
                           ["1", "W & K Scrap Metal", "02 9756 2636", "Follow-up 1 sent"],
                           ["2", "Safari Copper", "02 9756 2636", "Not contacted"]]
    out = run_sync(conn, sheet, niche)
    assert conn.execute("SELECT COUNT(*) FROM business").fetchone()[0] == 2
    assert out["Leads"]["appended_rows"] == 0
    rows = by_name(sheet)
    assert all(len(r["Lead Key"]) == 26 for r in rows.values())
    assert conn.execute("SELECT outreach_status FROM business WHERE lead_id=1").fetchone()[0] == "Follow-up 1 sent"


def test_zone_status_from_sheet_and_sources_append(env):
    conn, sheet, niche = env
    run_sync(conn, sheet, niche)
    edit(sheet, "Search Zones", "Suburb / Area", "Smithfield", "Status", "Skip")
    run_sync(conn, sheet, niche)
    assert conn.execute("SELECT status FROM zone WHERE area='Smithfield'").fetchone()[0] == "Skip"
    n = len(sheet.tabs["Lead_Sources"])
    assert n == 1 + conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0]
    run_sync(conn, sheet, niche)
    assert len(sheet.tabs["Lead_Sources"]) == n          # nothing appended twice


def test_col_letter():
    assert [col_letter(n) for n in (1, 26, 27, 52, 53)] == ["A", "Z", "AA", "AZ", "BA"]


def fake_webapp(sheet: MemorySheets):
    """Behaves like the deployed Code.gs: POST -> 302 -> JSON result."""
    import json as _json

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            req = _json.loads(request.content)
            try:
                if req["action"] == "ensure_tab":
                    sheet.ensure_tab(req["tab"], req.get("cols", 26))
                    out = {"created": False}
                elif req["action"] == "read":
                    out = {"values": sheet.read(req["tab"])}
                elif req["action"] == "write_cells":
                    sheet.write_cells(req["tab"], [tuple(u) for u in req["updates"]])
                    out = {}
                elif req["action"] == "append":
                    sheet.append(req["tab"], req["rows"])
                    out = {}
                else:
                    raise ValueError("unknown action")
                out["status"] = "success"
            except Exception as exc:  # noqa: BLE001
                out = {"status": "error", "message": str(exc)}
            handler.last = out
            return httpx.Response(302, headers={"Location": "https://script.googleusercontent.com/echo"})
        return httpx.Response(200, json=handler.last)
    return httpx.MockTransport(handler)


def test_sync_through_apps_script_client(env):
    from leadforge.sync.client import AppsScriptSheets
    conn, sheet, niche = env
    client = AppsScriptSheets("https://script.google.com/macros/s/x/exec", transport=fake_webapp(sheet))
    out = run_sync(conn, client, niche)
    assert out["Leads"]["appended_rows"] == 2
    edit(sheet, "Leads", "Company Name", "Safari Copper", "Outreach Status", "Email 1 sent")
    run_sync(conn, client, niche)
    assert conn.execute("SELECT outreach_status FROM business WHERE lead_id=2").fetchone()[0] == "Email 1 sent"
    again = run_sync(conn, client, niche)
    assert again["Leads"]["updated_cells"] == 0


def test_apps_script_errors_are_reported(env):
    from leadforge.sync.client import AppsScriptSheets
    import httpx
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "error", "message": "tab not found: X"}))
    with pytest.raises(RuntimeError, match="tab not found"):
        AppsScriptSheets("https://x/exec", transport=t).read("X")
    html = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Sign in</html>"))
    with pytest.raises(RuntimeError, match="did not return JSON"):
        AppsScriptSheets("https://x/exec", transport=html).read("Leads")
