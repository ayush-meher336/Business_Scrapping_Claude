"""Import the legacy workbook into SQLite.

Every non-blank row is first stored verbatim in `legacy_row`, so nothing can be lost.
Structured tables (business, contact, source_record, country, geo_place, zone, ...) are
then built from those rows.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from leadforge.core.ids import new_ulid
from leadforge.migration import workbook as wbm
from leadforge.migration.workbook import Tab, is_blank

LEGACY_RUN_ID = "legacy"
SUMMARY_LABELS = {"total", "totals", "grand total"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ROLE_LOCAL_PARTS = {
    "info", "sales", "admin", "office", "enquiries", "enquiry", "inquiries", "inquiry",
    "contact", "contactus", "accounts", "recycling", "scrap", "hello", "mail", "support",
    "service", "orders", "purchasing", "export", "exports", "import", "general", "reception",
}


def to_text(value: Any) -> str | None:
    """Normalise a cell to the text stored in TEXT columns."""
    if is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def to_int(value: Any) -> int | None:
    if is_blank(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def split_emails(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in EMAIL_RE.findall(text):
        e = m.strip(".").lower()
        if e not in seen:
            seen.append(e)
    return seen


def is_role_based(email: str) -> bool:
    local = email.split("@", 1)[0]
    return local in ROLE_LOCAL_PARTS or local.split(".")[0] in ROLE_LOCAL_PARTS


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().casefold())


class Importer:
    def __init__(self, conn: sqlite3.Connection, xlsx: Path):
        self.conn = conn
        self.xlsx = xlsx
        self.stats: dict[str, Any] = {}
        self.warnings: list[str] = []

    # -- entry point -------------------------------------------------------
    def run(self) -> dict[str, Any]:
        tabs = wbm.read_values(self.xlsx)
        formulas = wbm.read_formulas(self.xlsx, tabs)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        migrate_run = f"migrate-{new_ulid()}"

        with self.conn:
            self.conn.execute(
                "INSERT INTO run(run_id, kind, started_at, config_json) VALUES (?,?,?,?)",
                (LEGACY_RUN_ID, "legacy", now, json.dumps({"note": "pre-platform research"})),
            )
            self.conn.execute(
                "INSERT INTO run(run_id, kind, started_at, config_json) VALUES (?,?,?,?)",
                (migrate_run, "migrate", now, json.dumps({"xlsx": str(self.xlsx)})),
            )
            self._legacy_rows(tabs)
            self._legacy_formulas(formulas)
            self._lists(tabs["Lists"])
            self._sources(tabs["Sources"])
            self._compliance(tabs["Compliance"])
            self._countries(tabs["Countries"])
            city_index = self._cities(tabs["Cities"])
            zone_index = self._zones(tabs["Search Zones"], city_index)
            self._leads(tabs["Leads"], zone_index, now)
            self._research_log(tabs["Research Log"])
            self.stats["warnings"] = self.warnings
            self.conn.execute(
                "UPDATE run SET finished_at=?, stats_json=? WHERE run_id=?",
                (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 json.dumps(self.stats, ensure_ascii=False), migrate_run),
            )
        self.stats["run_id"] = migrate_run
        return self.stats

    # -- lossless layer ----------------------------------------------------
    def _legacy_rows(self, tabs: dict[str, Tab]) -> None:
        rows = [
            (tab.name, n, wbm.row_json(values))
            for tab in tabs.values() for n, values in tab.rows
        ]
        self.conn.executemany("INSERT INTO legacy_row VALUES (?,?,?)", rows)
        self.stats["legacy_rows"] = len(rows)

    def _legacy_formulas(self, formulas: dict[str, dict[str, str]]) -> None:
        self.conn.executemany(
            "INSERT INTO legacy_formula VALUES (?,?,?)",
            [(t, c, f) for t, cols in formulas.items() for c, f in cols.items()],
        )

    # -- reference tabs ----------------------------------------------------
    def _lists(self, tab: Tab) -> None:
        header_num, header = tab.rows[0]
        columns: dict[str, list[Any]] = defaultdict(list)
        for _, values in tab.data_after(header_num):
            for i, h in enumerate(header):
                if h is not None and i < len(values) and not is_blank(values[i]):
                    columns[str(h)].append(values[i])

        size_cols = {"Size code", "Meaning", "Target leads per city"}
        enum_rows = []
        for name, vals in columns.items():
            if name in size_cols:
                continue
            for pos, v in enumerate(vals):
                enum_rows.append((name, pos, to_text(v)))
        self.conn.executemany("INSERT OR IGNORE INTO enum_value VALUES (?,?,?)", enum_rows)

        codes = [c for c in columns.get("Size code", []) if isinstance(c, str) and len(c) <= 2]
        meanings = columns.get("Meaning", [])
        targets = columns.get("Target leads per city", [])
        sizes = [
            (code, to_text(meanings[i]) if i < len(meanings) else code,
             to_int(targets[i]) if i < len(targets) else None)
            for i, code in enumerate(codes)
        ]
        self.conn.executemany("INSERT INTO city_size VALUES (?,?,?)", sizes)
        self.stats["enum_lists"] = len({r[0] for r in enum_rows})
        self.stats["enum_values"] = len(enum_rows)

    def _sources(self, tab: Tab) -> None:
        header_num, header = tab.header_row("Source")
        wbm.check_headers(tab, header, wbm.SOURCES_HEADERS)
        rows = [tuple(to_text(v) for v in values[:5]) for _, values in tab.data_after(header_num)]
        self.conn.executemany("INSERT INTO source VALUES (?,?,?,?,?)", rows)
        self.stats["sources"] = len(rows)

    def _compliance(self, tab: Tab) -> None:
        header_num, header = tab.header_row("Market")
        wbm.check_headers(tab, header, wbm.COMPLIANCE_HEADERS)
        rows = [tuple(to_text(v) for v in values[:4]) for _, values in tab.data_after(header_num)]
        self.conn.executemany("INSERT INTO compliance_rule VALUES (?,?,?,?)", rows)
        self.stats["compliance_markets"] = len(rows)

    # -- geography ---------------------------------------------------------
    def _countries(self, tab: Tab) -> None:
        header_num, header = tab.header_row("Tier")
        wbm.check_headers(tab, header, wbm.COUNTRIES_HEADERS)
        count = 0
        self.country_place: dict[str, str] = {}
        for n, v in tab.data_after(header_num):
            name = to_text(v[2])
            if not name:
                self.warnings.append(f"Countries row {n}: no country name, kept in legacy_row only")
                continue
            if name.casefold() in SUMMARY_LABELS:
                self.warnings.append(f"Countries row {n}: summary row {name!r}, kept in legacy_row only")
                continue
            if name in self.country_place:
                self.warnings.append(f"Countries row {n}: duplicate country {name!r}, kept in legacy_row only")
                continue
            self.conn.execute(
                """INSERT INTO country(name, tier, region, trade_role, languages, local_keywords,
                   cold_email_rule, notes, legacy_target_leads, legacy_row) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (name, to_int(v[0]), to_text(v[1]), to_text(v[3]), to_text(v[4]), to_text(v[5]),
                 to_text(v[6]), to_text(v[7]), to_int(v[11]), n),
            )
            place_id = new_ulid()
            self.country_place[name] = place_id
            self.conn.execute(
                "INSERT INTO geo_place(place_id, level, name, country, source) VALUES (?,?,?,?,?)",
                (place_id, "country", name, name, "legacy_workbook"),
            )
            count += 1
        self.stats["countries"] = count

    def _country_place(self, name: str) -> str:
        if name not in self.country_place:
            place_id = new_ulid()
            self.country_place[name] = place_id
            self.conn.execute(
                "INSERT INTO geo_place(place_id, level, name, country, source) VALUES (?,?,?,?,?)",
                (place_id, "country", name, name, "legacy_workbook"),
            )
            self.warnings.append(f"Country {name!r} used in Cities/Zones but absent from Countries tab; created")
        return self.country_place[name]

    def _cities(self, tab: Tab) -> dict[tuple[str, str], list[str]]:
        header_num, header = tab.header_row("Loc ID")
        wbm.check_headers(tab, header, wbm.CITIES_HEADERS)
        index: dict[tuple[str, str], list[str]] = defaultdict(list)
        count = 0
        for n, v in tab.data_after(header_num):
            country, city = to_text(v[4]), to_text(v[6])
            if not country or not city:
                self.warnings.append(f"Cities row {n}: missing country or city, kept in legacy_row only")
                continue
            place_id = new_ulid()
            self.conn.execute(
                """INSERT INTO geo_place(place_id, parent_id, level, name, country, state_province,
                   size_code, city_type, who_to_target, areas_to_search, industry_profile,
                   suggested_searches, priority, status, assigned_to, notes, source, legacy_loc_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (place_id, self._country_place(country), "city", city, country, to_text(v[5]),
                 to_text(v[7]), to_text(v[8]), to_text(v[9]), to_text(v[10]), to_text(v[11]),
                 to_text(v[12]), to_text(v[2]), to_text(v[19]), to_text(v[20]), to_text(v[21]),
                 "legacy_workbook", to_int(v[0])),
            )
            index[(_norm(country), _norm(city))].append(place_id)
            count += 1
        self.stats["cities"] = count
        return index

    def _zones(self, tab: Tab, city_index: dict[tuple[str, str], list[str]]) -> dict[tuple[str, str, str], int]:
        header_num, header = tab.header_row("Zone ID")
        wbm.check_headers(tab, header, wbm.ZONES_HEADERS)
        index: dict[tuple[str, str, str], int] = {}
        count = unlinked = ambiguous = 0
        for n, v in tab.data_after(header_num):
            zone_id = to_int(v[0])
            country, city, area = to_text(v[2]), to_text(v[3]), to_text(v[4])
            if zone_id is None or not (country and city and area):
                self.warnings.append(f"Search Zones row {n}: missing ID/country/city/area, kept in legacy_row only")
                continue
            places = city_index.get((_norm(country), _norm(city)), [])
            place_id = places[0] if len(places) == 1 else None
            if not places:
                unlinked += 1
            elif len(places) > 1:
                ambiguous += 1
            self.conn.execute(
                """INSERT INTO zone(zone_id, place_id, country, city, area, city_size, priority,
                   search_query, status, assigned_to, date_done, notes, legacy_leads_collected)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (zone_id, place_id, country, city, area, to_text(v[5]), to_text(v[6]),
                 to_text(v[7]), to_text(v[9]), to_text(v[10]), to_text(v[11]), to_text(v[12]),
                 to_int(v[8])),
            )
            index.setdefault((_norm(country), _norm(city), _norm(area)), zone_id)
            count += 1
        self.stats["zones"] = count
        self.stats["zones_without_city"] = unlinked
        self.stats["zones_ambiguous_city"] = ambiguous
        return index

    # -- leads -------------------------------------------------------------
    def _leads(self, tab: Tab, zone_index: dict[tuple[str, str, str], int], now: str) -> None:
        header_num, header = tab.header_row("Lead ID")
        wbm.check_headers(tab, header, [h for h, _ in wbm.LEADS_HEADERS])
        fields = [f for _, f in wbm.LEADS_HEADERS]
        company_idx = fields.index("company_name")

        used_ids: set[int] = set()
        count = no_company = contacts = sources = zone_links = 0
        for n, v in tab.data_after(header_num):
            if is_blank(v[company_idx]):
                no_company += 1
                continue
            rec = {f: to_text(v[i]) for i, f in enumerate(fields)}
            # Lead ID was =ROW()-1: freeze the cached value; fall back to the same rule.
            lead_id = to_int(v[0]) or (n - 1)
            if lead_id in used_ids:
                self.warnings.append(f"Leads row {n}: Lead ID {lead_id} already used; assigned a new one")
                lead_id = max(used_ids) + 1
            used_ids.add(lead_id)
            rec["lead_id"] = lead_id

            lead_key = new_ulid()
            zone_id = zone_index.get(
                (_norm(rec["country"]), _norm(rec["city"]), _norm(rec["suburb_area"]))
            )
            zone_links += zone_id is not None
            cols = ["lead_key", *fields, "zone_id", "first_discovered_at", "last_updated_at", "legacy_row"]
            vals = [lead_key, *[rec[f] for f in fields], zone_id,
                    rec["date_collected"] or now, now, n]
            self.conn.execute(
                f"INSERT INTO business({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
            )

            source_id = None
            if rec["source_url"] or rec["source_type"]:
                cur = self.conn.execute(
                    """INSERT INTO source_record(lead_key, source_category, source_name, source_url,
                       evidence_type, fields_supported, date_accessed, verification_status, run_id, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (lead_key, rec["source_type"], rec["source_type"], rec["source_url"],
                     "legacy_primary_source", "contact details", rec["last_verified"] or rec["date_collected"],
                     "legacy", LEGACY_RUN_ID, "Imported from the Leads tab Source URL / Source Type"),
                )
                source_id = cur.lastrowid
                sources += 1

            for role, col in (("general", "general_email"), ("decision_maker", "decision_maker_email")):
                for email in split_emails(rec[col]):
                    contacts += self._contact(lead_key, "email", email, role,
                                              is_role_based(email), rec["email_verified"], source_id)
            if rec["phone"]:
                contacts += self._contact(lead_key, "phone", rec["phone"], "general", None, None, source_id)
            count += 1

        self.stats["leads"] = count
        self.stats["lead_rows_without_company"] = no_company
        self.stats["leads_linked_to_zone"] = zone_links
        self.stats["contacts"] = contacts
        self.stats["source_records"] = sources
        self.stats["max_lead_id"] = max(used_ids) if used_ids else 0

    def _contact(self, lead_key, kind, value, role, role_based, verify_status, source_id) -> int:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO contact(lead_key, kind, value, role, role_based, syntax_ok,
               verify_status, source_record_id) VALUES (?,?,?,?,?,?,?,?)""",
            (lead_key, kind, value, role,
             None if role_based is None else int(role_based),
             1 if kind == "email" else None, verify_status, source_id),
        )
        return cur.rowcount

    def _research_log(self, tab: Tab) -> None:
        header_num, header = tab.header_row("Date")
        wbm.check_headers(tab, header, wbm.LOG_HEADERS)
        rows = [
            (LEGACY_RUN_ID, *(to_text(x) for x in v[:9]), n)
            for n, v in tab.data_after(header_num)
        ]
        self.conn.executemany(
            """INSERT INTO research_log(run_id, date, country, city, zone, keyword, source,
               results_reviewed, new_companies_added, notes, legacy_row) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.stats["research_log"] = len(rows)


def migrate(conn: sqlite3.Connection, xlsx: Path) -> dict[str, Any]:
    return Importer(conn, xlsx).run()
