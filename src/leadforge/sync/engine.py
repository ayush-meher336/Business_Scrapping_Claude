"""Two-way sync of one keyed tab: database <-> Google Sheet, three-way merged against a snapshot.

Ownership per column:
  key           identity column; written once.
  research      written by research. If the sheet cell changed since the last sync (a person or another
                machine edited it), the sheet value wins and is pulled into the database.
  sales         owned by people: the sheet always wins; only written when the row is first created.
  shared_append notes: both sides' additions are kept.
  computed      always written from the database.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from leadforge.sync.client import SheetClient


@dataclass
class Column:
    header: str
    field: str
    owner: str


@dataclass
class TabSpec:
    tab: str
    key_header: str
    columns: list[Column]
    fetch: Callable[[sqlite3.Connection], dict[str, dict[str, str]]]      # key -> {header: value}
    apply: Callable[[sqlite3.Connection, str, dict[str, str]], None]      # pull {header: value} into db row
    insert: Callable[[sqlite3.Connection, dict[str, str], str | None], str] | None = None  # row from sheet -> key
    status_fields: set[str] = field(default_factory=set)   # research headers where the database wins on first sync
    before_push: Callable[[sqlite3.Connection, dict[str, dict[str, str]]], None] | None = None

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]


@dataclass
class TabResult:
    tab: str
    pulled_cells: int = 0
    pulled_rows_new: int = 0
    created_from_sheet: int = 0
    updated_cells: int = 0
    appended_rows: int = 0
    conflicts: int = 0


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(round(v, 7))
    return str(v)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _merge_notes(sheet: str, db: str) -> str:
    """Keep every line from both sides, sheet order first."""
    lines = sheet.splitlines()
    extra = [ln for ln in db.splitlines() if ln.strip() and ln not in lines]
    return "\n".join(lines + extra)


def sync_tab(conn: sqlite3.Connection, client: SheetClient, spec: TabSpec, run_id: str) -> TabResult:
    res = TabResult(spec.tab)
    client.ensure_tab(spec.tab, len(spec.columns))
    grid = client.read(spec.tab)

    # Header: map by name, so people may reorder or add their own columns.
    header = grid[0] if grid else []
    pos = {h: i for i, h in enumerate(header) if h}
    missing = [h for h in spec.headers if h not in pos]
    if missing:
        start = len(header) + 1
        client.write_cells(spec.tab, [(1, start, [missing])])
        for i, h in enumerate(missing):
            pos[h] = start - 1 + i
        _log(conn, run_id, spec.tab, "header", len(missing), ", ".join(missing))
    by_header = {c.header: c for c in spec.columns}

    def row_values(r: list[str]) -> dict[str, str]:
        return {h: (r[i] if i < len(r) else "") for h, i in pos.items() if h in by_header}

    snaps = {k: json.loads(v) for k, v in conn.execute(
        "SELECT row_key, values_json FROM sheet_snapshot WHERE tab=?", (spec.tab,))}
    sheet_rows: dict[str, tuple[int, dict[str, str]]] = {}
    keyless: list[tuple[int, dict[str, str]]] = []
    for i, r in enumerate(grid[1:], start=2):
        vals = row_values(r)
        key = vals.get(spec.key_header, "").strip()
        if key:
            if key not in sheet_rows:                 # a repeated key keeps its first row
                sheet_rows[key] = (i, vals)
        elif any(v.strip() for v in vals.values()):
            keyless.append((i, vals))

    db_rows = spec.fetch(conn)
    updates: list[tuple[int, int, list[list]]] = []
    final: dict[str, dict[str, str]] = {}

    # Rows typed into the sheet by hand: give them a key, create them locally.
    created: set[str] = set()
    for rownum, vals in keyless:
        if spec.insert is None:
            continue
        key = spec.insert(conn, vals, None)
        created.add(key)
        res.created_from_sheet += 1
        vals[spec.key_header] = key
        sheet_rows[key] = (rownum, vals)
        updates.append((rownum, pos[spec.key_header] + 1, [[key]]))
    # Rows another machine added.
    for key, (_, vals) in sheet_rows.items():
        if key not in db_rows and key not in created and spec.insert is not None:
            spec.insert(conn, vals, key)
            res.pulled_rows_new += 1
    if res.created_from_sheet or res.pulled_rows_new:
        db_rows = spec.fetch(conn)

    # Pull edits.
    for key, (rownum, s) in sheet_rows.items():
        d = db_rows.get(key)
        if d is None:
            continue
        snap = snaps.get(key)
        pull: dict[str, str] = {}
        for c in spec.columns:
            if c.owner in ("key", "computed"):
                continue
            sv, dv = s.get(c.header, ""), d.get(c.header, "")
            pv = None if snap is None else snap.get(c.header)
            if sv == dv:
                continue
            if c.owner == "sales":
                pull[c.header] = sv
            elif c.owner == "shared_append":
                if pv is not None and sv != pv and dv != pv and sv and dv:
                    merged = _merge_notes(sv, dv)
                    pull[c.header] = merged
                    _conflict(conn, spec.tab, key, c.header, dv, sv, "appended")
                    res.conflicts += 1
                elif (pv is not None and sv != pv) or (pv is None and sv and not dv):
                    pull[c.header] = sv
                elif pv is None and sv and dv:
                    pull[c.header] = _merge_notes(sv, dv)
            else:  # research
                if pv is None:
                    # First sync of this row: fill blanks either way; when both sides hold a value,
                    # the sheet wins except for status fields the latest research check owns.
                    if not sv or c.header in spec.status_fields:
                        continue
                    if dv:
                        _conflict(conn, spec.tab, key, c.header, dv, sv, "sheet_won")
                        res.conflicts += 1
                    pull[c.header] = sv
                elif sv != pv:
                    if dv != pv:
                        _conflict(conn, spec.tab, key, c.header, dv, sv, "sheet_won")
                        res.conflicts += 1
                    pull[c.header] = sv
        if pull:
            spec.apply(conn, key, pull)
            res.pulled_cells += len(pull)
    db_rows = spec.fetch(conn)

    if spec.before_push:
        spec.before_push(conn, {k: v for k, (_, v) in sheet_rows.items()})
        db_rows = spec.fetch(conn)

    # Push: update changed cells in existing rows (runs of adjacent columns per row).
    for key, (rownum, s) in sheet_rows.items():
        d = db_rows.get(key)
        if d is None:
            continue
        want = {}
        for c in spec.columns:
            want[c.header] = s.get(c.header, "") if c.owner == "sales" else d.get(c.header, "")
        changed = sorted(pos[h] for h, v in want.items() if v != s.get(h, "") and h in pos)
        run: list[int] = []
        for p in changed + [None]:
            if run and (p is None or p != run[-1] + 1):
                inv = {pos[h]: h for h in want}
                updates.append((rownum, run[0] + 1, [[want[inv[x]] for x in run]]))
                res.updated_cells += len(run)
                run = []
            if p is not None:
                run.append(p)
        final[key] = want

    # Append rows the sheet does not have yet.
    width = max(pos.values()) + 1 if pos else len(spec.columns)
    new_rows = []
    for key, d in db_rows.items():
        if key in sheet_rows:
            continue
        row = [""] * width
        for h, i in pos.items():
            if h in by_header:
                row[i] = d.get(h, "")
        new_rows.append(row)
        final[key] = {h: d.get(h, "") for h in spec.headers}

    if updates:
        client.write_cells(spec.tab, updates)
        _log(conn, run_id, spec.tab, "update", len(updates), f"{res.updated_cells} cells")
    if new_rows:
        client.append(spec.tab, new_rows)
        res.appended_rows = len(new_rows)
        _log(conn, run_id, spec.tab, "append", len(new_rows), None)

    # Only after the writes succeeded: record what the sheet now holds.
    ts = _now()
    conn.executemany("INSERT OR REPLACE INTO sheet_snapshot VALUES (?,?,?,?)",
                     [(spec.tab, k, json.dumps(v, ensure_ascii=False), ts) for k, v in final.items()])
    if res.pulled_cells or res.pulled_rows_new or res.created_from_sheet:
        _log(conn, run_id, spec.tab, "pull", res.pulled_cells + res.pulled_rows_new + res.created_from_sheet, None)
    conn.commit()
    return res


def _log(conn, run_id, tab, action, rows, detail):
    conn.execute("INSERT INTO sync_log(run_id, tab, action, rows, detail, at) VALUES (?,?,?,?,?,?)",
                 (run_id, tab, action, rows, detail, _now()))


def _conflict(conn, tab, key, field_, dbv, sv, resolution):
    conn.execute("INSERT INTO sync_conflict(tab, row_key, field, db_value, sheet_value, resolution, at) "
                 "VALUES (?,?,?,?,?,?,?)", (tab, key, field_, dbv, sv, resolution, _now()))


def append_only(conn: sqlite3.Connection, client: SheetClient, tab: str, headers: list[str], state_key: str,
                fetch_after: Callable[[sqlite3.Connection, int], list[tuple[int, list]]], run_id: str) -> int:
    """Append rows with an increasing id beyond the last one synced (logs, sources)."""
    client.ensure_tab(tab, len(headers))
    grid = client.read(tab)
    if not grid:
        client.write_cells(tab, [(1, 1, [headers])])
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (state_key,)).fetchone()
    last = int(row[0]) if row else 0
    rows = fetch_after(conn, last)
    if rows:
        client.append(tab, [r for _, r in rows])
        conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)", (state_key, str(rows[-1][0])))
        _log(conn, run_id, tab, "append", len(rows), None)
        conn.commit()
    return len(rows)
