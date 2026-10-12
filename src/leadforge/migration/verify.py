"""Round-trip check: re-read the workbook and prove the database holds every row and field."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from leadforge.migration import workbook as wbm
from leadforge.migration.importer import SUMMARY_LABELS, to_text
from leadforge.migration.workbook import is_blank

MAX_LISTED = 25


@dataclass
class VerifyResult:
    checks: list[tuple[str, int, int, bool]] = field(default_factory=list)  # name, expected, actual, ok
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c[3] for c in self.checks) and not self.mismatches

    def check(self, name: str, expected: int, actual: int) -> None:
        self.checks.append((name, expected, actual, expected == actual))


def verify(conn: sqlite3.Connection, xlsx: Path) -> VerifyResult:
    res = VerifyResult()
    tabs = wbm.read_values(xlsx)

    # 1. Lossless layer: every non-blank row stored byte-for-byte (as JSON).
    stored = {
        (r["tab"], r["row_num"]): r["values_json"]
        for r in conn.execute("SELECT tab, row_num, values_json FROM legacy_row")
    }
    expected_rows = {(t.name, n): wbm.row_json(v) for t in tabs.values() for n, v in t.rows}
    res.check("Raw rows stored (all tabs)", len(expected_rows), len(stored))
    for key, js in expected_rows.items():
        if stored.get(key) != js:
            res.mismatches.append(f"legacy_row {key[0]} row {key[1]} differs or is missing")
    for key in stored.keys() - expected_rows.keys():
        res.mismatches.append(f"legacy_row {key[0]} row {key[1]} not in workbook")

    # 2. Structured tables: counts.
    def data_rows(tab: str, first_header: str, key_cols: list[int]) -> int:
        t = tabs[tab]
        hn, _ = t.header_row(first_header)
        return sum(1 for _, v in t.data_after(hn)
                   if all(not is_blank(v[i]) for i in key_cols)
                   and not (tab == "Countries" and str(v[2]).casefold() in SUMMARY_LABELS))

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    res.check("Leads (rows with Company Name)", data_rows("Leads", "Lead ID", [6]), count("SELECT COUNT(*) FROM business"))
    res.check("Countries", data_rows("Countries", "Tier", [2]), count("SELECT COUNT(*) FROM country"))
    res.check("Cities", data_rows("Cities", "Loc ID", [4, 6]),
              count("SELECT COUNT(*) FROM geo_place WHERE level='city' AND source='legacy_workbook'"))
    res.check("Search Zones", data_rows("Search Zones", "Zone ID", [0, 2, 3, 4]), count("SELECT COUNT(*) FROM zone WHERE source='legacy_workbook'"))
    res.check("Research Log", data_rows("Research Log", "Date", []), count("SELECT COUNT(*) FROM research_log"))
    res.check("Sources", data_rows("Sources", "Source", []), count("SELECT COUNT(*) FROM source"))
    res.check("Compliance markets", data_rows("Compliance", "Market", []), count("SELECT COUNT(*) FROM compliance_rule"))

    # 3. Structured tables: every one of the 39 lead fields matches its source cell.
    fields = [f for _, f in wbm.LEADS_HEADERS]
    by_row = {r["legacy_row"]: r for r in conn.execute("SELECT * FROM business")}
    leads = tabs["Leads"]
    hn, _ = leads.header_row("Lead ID")
    field_mismatches = 0
    for n, v in leads.data_after(hn):
        if is_blank(v[6]):
            continue
        rec = by_row.get(n)
        if rec is None:
            res.mismatches.append(f"Leads row {n}: no business record")
            continue
        for i, f in enumerate(fields):
            want = to_text(v[i])
            got = rec[f]
            if f == "lead_id":
                want_id = int(float(want)) if want else n - 1
                if got != want_id:
                    field_mismatches += 1
                    res.mismatches.append(f"Leads row {n}: Lead ID {want_id} stored as {got}")
            elif want != got:
                field_mismatches += 1
                if field_mismatches <= MAX_LISTED:
                    res.mismatches.append(f"Leads row {n} {f}: {want!r} != {got!r}")
    res.check("Lead field mismatches", 0, field_mismatches)
    res.check("Duplicate Lead IDs", 0, count(
        "SELECT COUNT(*) FROM (SELECT lead_id FROM business GROUP BY lead_id HAVING COUNT(*) > 1)"))
    return res


def write_report(conn: sqlite3.Connection, res: VerifyResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"migration_report_{stamp}.md"
    run = conn.execute(
        "SELECT run_id, stats_json FROM run WHERE kind='migrate' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    stats = json.loads(run["stats_json"]) if run and run["stats_json"] else {}

    lines = [f"# Migration report {stamp}", "", f"Result: **{'PASS' if res.ok else 'FAIL'}**", "",
             "| Check | Workbook | Database | OK |", "| --- | ---: | ---: | --- |"]
    lines += [f"| {n} | {e} | {a} | {'yes' if ok else 'NO'} |" for n, e, a, ok in res.checks]
    lines += ["", "## Import statistics", ""]
    lines += [f"- {k}: {v}" for k, v in stats.items() if k != "warnings"]
    lines += ["", "## Warnings", ""]
    lines += [f"- {w}" for w in stats.get("warnings", [])] or ["- none"]
    lines += ["", "## Mismatches", ""]
    lines += [f"- {m}" for m in res.mismatches[:200]] or ["- none"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
