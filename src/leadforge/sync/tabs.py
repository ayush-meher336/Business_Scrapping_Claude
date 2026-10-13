"""Tab definitions: which database fields map to which sheet columns, and who owns them."""
from __future__ import annotations

import sqlite3

from leadforge.core.ids import new_ulid
from leadforge.niches.model import NicheConfig
from leadforge.sync.engine import Column, TabSpec, cell

LEAD_KEY = "Lead Key"
REAL_FIELDS = {"lat", "lon", "maps_rating"}
INT_FIELDS = {"lead_id", "zone_id", "permanently_closed"}
LEAD_STATUS_FIELDS = {"Website Status", "Last Verified", "Email Verified", "Research Status", "Last Researched At",
                      "Last Updated At", "Confidence"}
BLOCKED_OUTREACH = {"Bounced", "Unsubscribed / Do not contact"}


# -- computed columns --------------------------------------------------------
def ok_to_email(conn: sqlite3.Connection, b: sqlite3.Row, excluded: set[str], suppressed: set[str]) -> tuple[str, str]:
    """('Yes'|'No', reason). Workbook rule + suppression list + excluded countries."""
    email = b["general_email"] or b["decision_maker_email"]
    if not email:
        return "No", "no email"
    if b["opt_out_notice"] == "Yes":
        return "No", "opt-out notice on site"
    if b["email_verified"] != "Valid":
        return "No", f"email {b['email_verified'] or 'not checked'}"
    if b["outreach_status"] in BLOCKED_OUTREACH:
        return "No", b["outreach_status"]
    dom = email.split("@", 1)[-1].lower()
    if email.lower() in suppressed or dom in suppressed:
        return "No", "on suppression list"
    if b["country"] in excluded:
        return "No", "country excluded"
    return "Yes", ""


def duplicate_flags(conn: sqlite3.Connection) -> dict[str, str]:
    ids = {r[0]: r[1] for r in conn.execute("SELECT lead_key, lead_id FROM business")}
    out = {}
    for r in conn.execute("SELECT lead_key, match_key FROM dedup_review WHERE status='open'"):
        out[r[0]] = f"Possible duplicate of {ids.get(r[1], '?')}"
    return out


# -- Leads --------------------------------------------------------------------
def leads_spec(niche: NicheConfig) -> TabSpec:
    cols = [Column(c.header, c.field, "key" if c.field == "lead_key" else c.owner) for c in niche.sheet_columns]
    if not any(c.field == "lead_key" for c in cols):
        cols.append(Column(LEAD_KEY, "lead_key", "key"))
    cols.append(Column("OK to Email reason", "ok_reason", "computed"))
    db_fields = [c.field for c in cols if c.field not in ("duplicate_flag", "ok_to_email", "ok_reason")]
    key_header = next(c.header for c in cols if c.field == "lead_key")

    def fetch(conn):
        excluded = {r[0] for r in conn.execute("SELECT name FROM country WHERE excluded=1")}
        suppressed = {r[0].lower() for r in conn.execute("SELECT value FROM suppression")}
        dups = duplicate_flags(conn)
        out = {}
        for b in conn.execute("SELECT * FROM business"):
            ok, why = ok_to_email(conn, b, excluded, suppressed)
            row = {}
            for c in cols:
                if c.field == "ok_to_email":
                    row[c.header] = ok
                elif c.field == "ok_reason":
                    row[c.header] = why
                elif c.field == "duplicate_flag":
                    row[c.header] = dups.get(b["lead_key"], "")
                else:
                    row[c.header] = cell(b[c.field])
            out[b["lead_key"]] = row
        return out

    def to_db(field: str, v: str):
        if v == "":
            return None
        if field in REAL_FIELDS:
            try:
                return float(v)
            except ValueError:
                return None
        if field in INT_FIELDS:
            try:
                return int(float(v))
            except ValueError:
                return None
        return v

    by_header = {c.header: c for c in cols}

    def apply(conn, key, values):
        sets = {by_header[h].field: to_db(by_header[h].field, v) for h, v in values.items()
                if by_header[h].field in db_fields and by_header[h].field not in ("lead_key", "lead_id")}
        if sets:
            conn.execute(f"UPDATE business SET {', '.join(f'{k}=?' for k in sets)} WHERE lead_key=?",
                         (*sets.values(), key))

    def insert(conn, values, key):
        lead_id = to_db("lead_id", values.get(next((c.header for c in cols if c.field == "lead_id"), ""), ""))
        name = values.get(next(c.header for c in cols if c.field == "company_name"), "") or "(unnamed)"
        if key is None and lead_id is not None:
            # A keyless row from the original workbook: adopt the lead it already is (same ID and name).
            same = conn.execute("SELECT lead_key, company_name FROM business WHERE lead_id=?", (lead_id,)).fetchone()
            if same and (same["company_name"] or "").strip().casefold() == name.strip().casefold():
                return same["lead_key"]
        new_key = key or new_ulid()
        holder = conn.execute("SELECT lead_key FROM business WHERE lead_id=?", (lead_id,)).fetchone()             if lead_id is not None else None
        if holder:
            published = conn.execute("SELECT 1 FROM sheet_snapshot WHERE tab='Leads' AND row_key=?",
                                     (holder["lead_key"],)).fetchone()
            if key and not published:
                # The sheet's ID is already shared; our unsynced lead moves to the next free number.
                top = conn.execute("SELECT COALESCE(MAX(lead_id), 0) + 1 FROM business").fetchone()[0]
                conn.execute("UPDATE business SET lead_id=? WHERE lead_key=?", (top, holder["lead_key"]))
            else:
                lead_id = None
        if lead_id is None:
            lead_id = conn.execute("SELECT COALESCE(MAX(lead_id), 0) + 1 FROM business").fetchone()[0]
        fields = {"lead_key": new_key, "lead_id": lead_id, "company_name": name}
        for h, v in values.items():
            c = by_header.get(h)
            if c and c.field in db_fields and c.field not in fields and v != "":
                fields[c.field] = to_db(c.field, v)
        fields.setdefault("research_status", "synced" if key else "manual")   # keep the sheet's own status
        if fields.get("zone_id") is not None and not conn.execute(
                "SELECT 1 FROM zone WHERE zone_id=?", (fields["zone_id"],)).fetchone():
            fields["zone_id"] = None                                          # zone not known on this machine yet
        conn.execute(f"INSERT INTO business({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     tuple(fields.values()))
        return new_key

    def before_push(conn, sheet_rows):
        """Two machines may give the same Lead ID to different new leads: renumber local ones."""
        id_header = next((c.header for c in cols if c.field == "lead_id"), None)
        if not id_header:
            return
        taken = {}
        for k, v in sheet_rows.items():
            if v.get(id_header, "").strip():
                try:
                    taken[int(float(v[id_header]))] = k
                except ValueError:
                    pass
        top = max([0, *taken, conn.execute("SELECT COALESCE(MAX(lead_id),0) FROM business").fetchone()[0]])
        for b in conn.execute("SELECT lead_key, lead_id FROM business").fetchall():
            if b["lead_key"] not in sheet_rows and b["lead_id"] in taken and taken[b["lead_id"]] != b["lead_key"]:
                top += 1
                conn.execute("UPDATE business SET lead_id=? WHERE lead_key=?", (top, b["lead_key"]))

    return TabSpec("Leads", key_header, cols, fetch, apply, insert, LEAD_STATUS_FIELDS, before_push)


# -- Search Zones -------------------------------------------------------------
ZONE_COLUMNS = [
    Column("Zone ID", "zone_id", "key"), Column("Country", "country", "research"),
    Column("City / Hub", "city", "research"), Column("Suburb / Area", "area", "research"),
    Column("City size", "city_size", "research"), Column("City Priority", "priority", "research"),
    Column("Google Maps search", "search_query", "research"), Column("Leads collected", "leads", "computed"),
    Column("Status", "status", "sales"), Column("Assigned to", "assigned_to", "sales"),
    Column("Date done", "date_done", "sales"), Column("Notes", "notes", "shared_append"),
    Column("Zone kind", "kind", "research"), Column("Zone source", "source", "research"),
    Column("Latitude", "lat", "research"), Column("Longitude", "lon", "research"),
]


def zones_spec() -> TabSpec:
    by_header = {c.header: c for c in ZONE_COLUMNS}

    def fetch(conn):
        counts = dict(conn.execute("SELECT zone_id, COUNT(*) FROM business WHERE zone_id IS NOT NULL GROUP BY zone_id"))
        out = {}
        for z in conn.execute("SELECT * FROM zone WHERE alias_of IS NULL"):
            out[str(z["zone_id"])] = {c.header: cell(counts.get(z["zone_id"], 0) if c.field == "leads" else z[c.field])
                                      for c in ZONE_COLUMNS}
        return out

    def apply(conn, key, values):
        sets = {}
        for h, v in values.items():
            f = by_header[h].field
            if f in ("zone_id", "leads"):
                continue
            sets[f] = (float(v) if v else None) if f in ("lat", "lon") else (v or None)
        if sets:
            conn.execute(f"UPDATE zone SET {', '.join(f'{k}=?' for k in sets)} WHERE zone_id=?", (*sets.values(), int(key)))

    def insert(conn, values, key):
        zid = int(key) if key and key.isdigit() else conn.execute("SELECT COALESCE(MAX(zone_id),0)+1 FROM zone").fetchone()[0]
        conn.execute("""INSERT INTO zone(zone_id, country, city, area, status, assigned_to, notes, source)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (zid, values.get("Country") or "", values.get("City / Hub") or "", values.get("Suburb / Area") or "",
                      values.get("Status") or "Not started", values.get("Assigned to") or None,
                      values.get("Notes") or None, "sheet"))
        return str(zid)

    return TabSpec("Search Zones", "Zone ID", ZONE_COLUMNS, fetch, apply, insert)


# -- Cities -------------------------------------------------------------------
CITY_COLUMNS = [
    Column("Place Key", "place_id", "key"), Column("Loc ID", "legacy_loc_id", "research"),
    Column("Country", "country", "research"), Column("State / Province", "state_province", "research"),
    Column("City / Hub", "name", "research"), Column("Size", "size_code", "research"),
    Column("City Priority", "priority", "research"), Column("Zones", "zones", "computed"),
    Column("Leads collected", "leads", "computed"), Column("Status", "status", "sales"),
    Column("Assigned to", "assigned_to", "sales"), Column("Notes", "notes", "shared_append"),
    Column("Latitude", "lat", "research"), Column("Longitude", "lon", "research"),
]


def cities_spec() -> TabSpec:
    by_header = {c.header: c for c in CITY_COLUMNS}

    def fetch(conn):
        zones = dict(conn.execute("SELECT place_id, COUNT(*) FROM zone WHERE alias_of IS NULL GROUP BY place_id"))
        leads = {(r[0], r[1]): r[2] for r in conn.execute("SELECT country, city, COUNT(*) FROM business GROUP BY 1, 2")}
        out = {}
        for g in conn.execute("SELECT * FROM geo_place WHERE level='city'"):
            row = {}
            for c in CITY_COLUMNS:
                if c.field == "zones":
                    row[c.header] = cell(zones.get(g["place_id"], 0))
                elif c.field == "leads":
                    row[c.header] = cell(leads.get((g["country"], g["name"]), 0))
                else:
                    row[c.header] = cell(g[c.field])
            out[g["place_id"]] = row
        return out

    def apply(conn, key, values):
        sets = {}
        for h, v in values.items():
            f = by_header[h].field
            if f in ("place_id", "zones", "leads"):
                continue
            if f in ("lat", "lon"):
                sets[f] = float(v) if v else None
            elif f == "legacy_loc_id":
                sets[f] = int(float(v)) if v else None
            else:
                sets[f] = v or None
        if sets:
            conn.execute(f"UPDATE geo_place SET {', '.join(f'{k}=?' for k in sets)} WHERE place_id=?", (*sets.values(), key))

    def insert(conn, values, key):
        pid = key or new_ulid()
        conn.execute("""INSERT INTO geo_place(place_id, level, name, country, state_province, size_code, status,
                        assigned_to, notes, source) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (pid, "city", values.get("City / Hub") or "(unnamed)", values.get("Country") or "",
                      values.get("State / Province") or None, values.get("Size") or None,
                      values.get("Status") or "Not started", values.get("Assigned to") or None,
                      values.get("Notes") or None, "sheet"))
        return pid

    return TabSpec("Cities", "Place Key", CITY_COLUMNS, fetch, apply, insert)


# -- Lead_Sources (append-only) ----------------------------------------------
SOURCE_HEADERS = ["Source Record ID", "Lead ID", "Company Name", "Source Category", "Source Name", "Source URL",
                  "Evidence Type", "Fields Supported", "Date Accessed", "Verification Status", "Confidence",
                  "Research Run ID", "Notes", "Lead Key"]


def sources_after(conn: sqlite3.Connection, last_id: int) -> list[tuple[int, list]]:
    rows = conn.execute(
        """SELECT s.*, b.lead_id, b.company_name FROM source_record s JOIN business b ON b.lead_key = s.lead_key
           WHERE s.source_record_id > ? ORDER BY s.source_record_id""", (last_id,)).fetchall()
    return [(r["source_record_id"], [r["source_record_id"], r["lead_id"], r["company_name"], r["source_category"],
                                     r["source_name"], r["source_url"], r["evidence_type"], r["fields_supported"],
                                     r["date_accessed"], r["verification_status"], r["confidence"], r["run_id"],
                                     r["notes"], r["lead_key"]]) for r in rows]
