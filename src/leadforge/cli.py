"""leadforge command line."""
from __future__ import annotations

import sys
from pathlib import Path

import typer

from leadforge.core.config import load_app_config
from leadforge.migration.importer import migrate as run_migration
from leadforge.migration.verify import verify as run_verify, write_report
from leadforge.niches.model import check_against_db, load_niche
from leadforge.storage import db

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; keywords are multilingual
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(no_args_is_help=True, help="Lead research platform")


def _open():
    cfg = load_app_config()
    conn = db.connect(cfg.resolve(cfg.paths.database))
    if db.is_empty(conn):
        typer.echo("Database is empty. Run `leadforge migrate` first.")
        raise typer.Exit(1)
    db.upgrade(conn)
    return cfg, conn


@app.command()
def migrate(
    xlsx: Path = typer.Option(None, help="Workbook to import (defaults to config/app.yaml)"),
    force: bool = typer.Option(False, help="Delete an existing database first"),
) -> None:
    """Import the legacy workbook into SQLite, then verify the round trip."""
    cfg = load_app_config()
    db_path = cfg.resolve(cfg.paths.database)
    xlsx = cfg.resolve(xlsx or cfg.workbook.legacy_xlsx)
    if not xlsx.exists():
        raise typer.BadParameter(f"workbook not found: {xlsx}")

    if db_path.exists():
        conn = db.connect(db_path)
        if not db.is_empty(conn):
            if not force:
                typer.echo(f"{db_path} already holds data. Re-run with --force to rebuild it.")
                raise typer.Exit(1)
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
        else:
            conn.close()

    conn = db.connect(db_path)
    db.init_schema(conn)
    typer.echo(f"Importing {xlsx.name} ...")
    stats = run_migration(conn, xlsx)
    for k, v in stats.items():
        if k != "warnings":
            typer.echo(f"  {k}: {v}")
    if stats["warnings"]:
        typer.echo(f"  warnings: {len(stats['warnings'])} (see report)")
    _verify_and_report(conn, cfg, xlsx)


@app.command()
def verify(xlsx: Path = typer.Option(None)) -> None:
    """Re-check the database against the workbook without importing."""
    cfg, conn = _open()
    _verify_and_report(conn, cfg, cfg.resolve(xlsx or cfg.workbook.legacy_xlsx))


def _verify_and_report(conn, cfg, xlsx: Path) -> None:
    typer.echo("Verifying round trip ...")
    res = run_verify(conn, xlsx)
    for name, expected, actual, ok in res.checks:
        typer.echo(f"  [{'ok' if ok else 'FAIL'}] {name}: workbook {expected}, database {actual}")
    report = write_report(conn, res, cfg.resolve(cfg.paths.exports))
    typer.echo(f"{'PASS' if res.ok else 'FAIL'} - report: {report}")
    if not res.ok:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show what the database holds."""
    _, conn = _open()
    queries = {
        "leads": "SELECT COUNT(*) FROM business",
        "countries": "SELECT COUNT(*) FROM country",
        "cities": "SELECT COUNT(*) FROM geo_place WHERE level='city'",
        "zones": "SELECT COUNT(*) FROM zone",
        "zones done": "SELECT COUNT(*) FROM zone WHERE status='Done'",
        "contacts": "SELECT COUNT(*) FROM contact",
        "research log rows": "SELECT COUNT(*) FROM research_log",
    }
    for label, sql in queries.items():
        typer.echo(f"{label:>18}: {conn.execute(sql).fetchone()[0]:,}")


@app.command("check-niche")
def check_niche(name: str = typer.Argument("scrapyards")) -> None:
    """Validate a niche config against the controlled lists in the database."""
    cfg, conn = _open()
    niche = load_niche(cfg.root, name)
    problems = check_against_db(niche, conn)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(1)
    typer.echo(f"{niche.display_name}: OK ({len(niche.sheet_columns)} sheet columns, "
               f"{len(niche.keyword_packs)} keyword packs)")


# -- Phase 2: geography and keywords ----------------------------------------

def _countries(conn, names: list[str] | None, tier: int | None) -> list[str]:
    if names:
        known = {r[0] for r in conn.execute("SELECT name FROM country")}
        bad = [n for n in names if n not in known]
        if bad:
            raise typer.BadParameter(f"unknown countries: {bad}")
        return names
    sql = "SELECT name FROM country WHERE excluded=0"
    args: tuple = ()
    if tier:
        sql += " AND tier=?"
        args = (tier,)
    return [r[0] for r in conn.execute(sql + " ORDER BY tier, name", args)]


@app.command("geo-import")
def geo_import(
    country: list[str] = typer.Option(None, help="Limit places to these workbook countries (repeatable)"),
    refresh: bool = typer.Option(False, help="Download the GeoNames files again"),
) -> None:
    """Download GeoNames (cached under data/cache/geonames) and load it."""
    from leadforge.geography import geonames
    from leadforge.geography.geocode import resolve_countries
    from leadforge.geography.settings import load_geo_config

    cfg, conn = _open()
    geo = load_geo_config(cfg.root)
    cache = cfg.resolve(cfg.paths.cache) / "geonames"
    typer.echo(f"GeoNames files -> {cache}")
    paths = geonames.download(geo.geonames.base_url, geo.geonames.files, cache, refresh)
    with conn:
        typer.echo(f"  countries: {geonames.load_countries(conn, paths['countryInfo.txt'])}")
        typer.echo(f"  admin1:    {geonames.load_admin1(conn, paths['admin1CodesASCII.txt'])}")
        unmatched = resolve_countries(conn, geo.country_aliases, geo.excluded_countries.names,
                                      geo.excluded_countries.reason)
        codes = None
        if country:
            codes = {r[0] for r in conn.execute(
                f"SELECT iso2 FROM country WHERE name IN ({','.join('?' * len(country))})", country) if r[0]}
        places_file = next(p for n, p in paths.items() if n.startswith("cities"))
        typer.echo(f"  places:    {geonames.load_places(conn, places_file, codes):,}")
    if unmatched:
        typer.echo(f"  {len(unmatched)} workbook countries without an ISO code (add to config/geo.yaml "
                   f"country_aliases): {', '.join(unmatched)}")


@app.command()
def geocode(redo: bool = typer.Option(False, help="Re-geocode places that already have coordinates")) -> None:
    """Attach coordinates to cities and zones, then link leads to zones."""
    from leadforge.geography.coverage import relink_leads
    from leadforge.geography.geocode import geocode_cities, geocode_zones

    _, conn = _open()
    with conn:
        typer.echo(f"cities: {geocode_cities(conn, not redo)}")
        typer.echo(f"zones:  {geocode_zones(conn, not redo)}")
        typer.echo(f"leads newly linked to a zone: {relink_leads(conn)}")


def _target_cities(conn, countries, city, priority):
    sql = """SELECT g.* FROM geo_place g JOIN country c ON c.name = g.country
             WHERE g.level='city' AND c.excluded=0 AND g.lat IS NOT NULL"""
    args: list = []
    if countries:
        sql += f" AND g.country IN ({','.join('?' * len(countries))})"
        args += countries
    if city:
        sql += " AND g.name=?"
        args.append(city)
    if priority.upper() != "ALL":
        sql += " AND g.priority=?"
        args.append(priority.upper())
    # Round-robin: each country's biggest city, then each country's next, ... so partial runs
    # still cover many countries.
    size = "CASE g.size_code WHEN 'X' THEN 0 WHEN 'M' THEN 1 WHEN 'H' THEN 2 ELSE 3 END"
    sql = sql.replace("SELECT g.*", f"SELECT g.*, c.tier AS ctier, ROW_NUMBER() OVER "
                                    f"(PARTITION BY g.country ORDER BY {size}, g.name) AS rn", 1)
    return conn.execute(sql + " ORDER BY rn, ctier, g.country", args).fetchall()


@app.command("build-zones")
def build_zones(
    country: list[str] = typer.Option(None), city: str = typer.Option(None),
    tier: int = typer.Option(None, help="Only countries of this tier"),
    priority: str = typer.Option("A", help="City priority to expand (A, B, C or ALL)"),
    dry_run: bool = typer.Option(False, help="Show what would be added without writing"),
) -> None:
    """Expand cities into research zones (suburbs, industrial areas, nearby towns)."""
    from leadforge.core.ids import new_ulid
    from leadforge.geography.settings import load_geo_config
    from leadforge.geography.zones import ExcludedCountryError, build_city

    cfg, conn = _open()
    settings = load_geo_config(cfg.root).build_settings()
    run_id = f"zones-{new_ulid()}"
    countries = _countries(conn, country, tier) if (country or tier) else None
    totals = {"cities": 0, "new_zones": 0, "new_cities": 0, "skipped": 0}
    with conn:
        for c in _target_cities(conn, countries, city, priority):
            try:
                r = build_city(conn, c["place_id"], settings, run_id, dry_run)
            except ExcludedCountryError as exc:
                typer.echo(f"  skipped: {exc}")
                continue
            totals["cities"] += 1
            if r.skipped:
                totals["skipped"] += 1
                typer.echo(f"  {r.country} / {r.city}: skipped ({r.skipped})")
                continue
            totals["new_zones"] += len(r.new_zones)
            totals["new_cities"] += len(r.new_cities)
            typer.echo(f"  {r.country} / {r.city}: {r.existing} existing ({r.existing_geocoded} geocoded), "
                       f"{r.candidates} candidates, {r.matched_existing} already covered, "
                       f"{r.filtered_no_industry} without industry, "
                       f"+{len(r.new_zones)} zones, +{len(r.new_cities)} cities")
        if dry_run:
            conn.rollback()
    typer.echo(f"{'DRY RUN ' if dry_run else ''}{totals}")


@app.command()
def keywords(niche: str = typer.Argument("scrapyards")) -> None:
    """Sync the keyword library and rebuild yields from the research log."""
    from leadforge.keywords.library import import_legacy_yields, sync_keywords

    cfg, conn = _open()
    n = load_niche(cfg.root, niche)
    with conn:
        typer.echo(f"keywords added: {sync_keywords(conn, n)}")
        typer.echo(f"legacy yields:  {import_legacy_yields(conn, n.niche)}")
    for r in conn.execute(
        """SELECT k.term, k.kind, SUM(y.queries) q, SUM(y.new_uniques) n FROM keyword_yield y
           JOIN keyword k ON k.keyword_id=y.keyword_id GROUP BY k.keyword_id ORDER BY q DESC LIMIT 10"""):
        typer.echo(f"  {r['term']:<28} {r['kind']:<9} {r['q']:>5} queries  {r['n']:>6} new")


@app.command("plan")
def plan_cmd(
    niche: str = typer.Option("scrapyards"), country: list[str] = typer.Option(None),
    tier: int = typer.Option(None), city: str = typer.Option(None),
    source: list[str] = typer.Option(["google_maps_browser"], help="Discovery sources driven by text queries (repeatable). "
                                     "Run through SeleniumBase in Phase 3; no third-party APIs."),
    per_zone: int = typer.Option(4, help="Keywords per zone and source in round 1"),
) -> None:
    """Queue search jobs (zone x keyword x source) for open zones, best expected yield first."""
    from leadforge.core.ids import new_ulid
    from leadforge.keywords.planner import plan
    from leadforge.geography.zones import ExcludedCountryError

    _, conn = _open()
    countries = _countries(conn, country, tier)
    try:
        with conn:
            r = plan(conn, niche, countries, source, f"plan-{new_ulid()}", per_zone, city)
    except ExcludedCountryError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    typer.echo(f"zones considered: {r.zones}, already planned: {r.zones_already_planned}, "
               f"jobs created: {r.jobs_created}")


@app.command()
def coverage(country: str = typer.Option(None), limit: int = typer.Option(40)) -> None:
    """Coverage by city: zones, done, new zones, leads and contact coverage."""
    from leadforge.geography.coverage import city_coverage

    _, conn = _open()
    rows = city_coverage(conn, country)
    typer.echo(f"{'country':<16}{'city':<22}{'sz':<3}{'pr':<3}{'zones':>6}{'done':>6}{'new':>5}"
               f"{'leads':>7}{'web':>6}{'phone':>6}{'email':>6}")
    for r in rows[:limit]:
        typer.echo(f"{r['country'][:15]:<16}{r['city'][:21]:<22}{r['size_code'] or '':<3}{r['priority'] or '':<3}"
                   f"{r['zones']:>6}{r['zones_done']:>6}{r['zones_new']:>5}{r['leads']:>7}"
                   f"{r['with_website']:>6}{r['with_phone']:>6}{r['with_email']:>6}")
    if len(rows) > limit:
        typer.echo(f"... {len(rows) - limit} more (use --limit)")


# -- Phase 3: discovery ------------------------------------------------------

def _session_factory(cfg, source: str, headed: bool):
    from leadforge.discovery.gmaps import MapsSession
    from leadforge.discovery.websearch import WebSearchSession

    d = cfg.discovery
    headless = d.get("headless", True) and not headed
    if source == "google_maps_browser":
        return lambda: MapsSession(headless=headless, max_results=d.get("max_results_per_search", 60),
                                   min_delay_s=d.get("min_delay_seconds", 4), max_delay_s=d.get("max_delay_seconds", 8))
    if source == "web_search_browser":
        return lambda: WebSearchSession(headless=headless)
    raise typer.BadParameter(f"no browser collector for source {source!r}")


@app.command("backfill-identity")
def backfill_identity_cmd() -> None:
    """Compute domain, E.164 phone and normalised name for existing leads (needed for dedup)."""
    from leadforge.dedup.resolver import backfill_identity

    _, conn = _open()
    with conn:
        typer.echo(backfill_identity(conn))


@app.command()
def discover(
    source: str = typer.Option("google_maps_browser", help="google_maps_browser | web_search_browser"),
    jobs: int = typer.Option(20, help="Maximum search jobs to run"),
    workers: int = typer.Option(None, help="Parallel browsers (default from config/app.yaml)"),
    country: str = typer.Option(None), city: str = typer.Option(None),
    niche: str = typer.Option("scrapyards"),
    headed: bool = typer.Option(False, help="Show the browser windows"),
) -> None:
    """Run queued search jobs in the browser and resolve every result into businesses."""
    from leadforge.dedup.resolver import backfill_identity
    from leadforge.discovery.runner import Relevance, run

    cfg, conn = _open()
    n = load_niche(cfg.root, niche)
    with conn:
        backfill_identity(conn)
    relevance = Relevance(n.discovery.include, n.discovery.exclude)

    def progress(job, results, new):
        typer.echo(f"  [{job['job_id']}] {job['query'][:60]:<60} {results:>3} results, {new:>3} new")

    stats = run(conn, source, _session_factory(cfg, source, headed), relevance, jobs,
                workers or cfg.discovery.get("workers", 2), country, city, progress)
    typer.echo(f"jobs done {stats.jobs_done}, failed {stats.jobs_failed}; candidates {stats.candidates}: "
               f"new {stats.new}, merged {stats.merged}, review {stats.review}, rejected {stats.rejected}")
    for e in stats.errors[:5]:
        typer.echo(f"  error: {e}")
    if stats.blocked:
        typer.echo(f"STOPPED: {stats.blocked}\nA person needs to check this in a normal browser before the next run.")
        raise typer.Exit(2)


@app.command("scan-industrial")
def scan_industrial(
    country: list[str] = typer.Option(None), city: str = typer.Option(None),
    tier: int = typer.Option(None), priority: str = typer.Option("A"),
    limit: int = typer.Option(10, help="Maximum cities this run"),
    headed: bool = typer.Option(False),
) -> None:
    """Find industrial estates / business parks on Google Maps for cities with no area data yet."""
    from leadforge.discovery.gmaps import BlockedError
    from leadforge.discovery.industrial import scan_city

    cfg, conn = _open()
    countries = _countries(conn, country, tier) if (country or tier) else None
    have = {r[0] for r in conn.execute("SELECT DISTINCT place_id FROM osm_feature")}
    todo = [c for c in _target_cities(conn, countries, city, priority) if c["place_id"] not in have][:limit]
    factory = _session_factory(cfg, "google_maps_browser", headed)
    with factory() as session:
        for c in todo:
            try:
                n = scan_city(conn, session, c["place_id"], c["name"], c["country"])
            except BlockedError as exc:
                typer.echo(f"STOPPED: {exc}")
                raise typer.Exit(2)
            typer.echo(f"  {c['country']} / {c['name']}: {n} industrial areas")


@app.command("dedup-report")
def dedup_report(sample: int = typer.Option(100, help="Random new leads to export for a manual duplicate check")) -> None:
    """Resolution counts, open review items, and a CSV sample for measuring the duplicate rate by hand."""
    import csv

    cfg, conn = _open()
    for r in conn.execute("SELECT resolution, COUNT(*) n FROM candidate GROUP BY resolution ORDER BY n DESC"):
        typer.echo(f"  {r['resolution'] or 'unresolved':<10} {r['n']:>6}")
    typer.echo(f"  merge rules: {dict(conn.execute('SELECT rule, COUNT(*) FROM merge_log GROUP BY rule').fetchall())}")
    open_reviews = conn.execute("SELECT COUNT(*) FROM dedup_review WHERE status='open'").fetchone()[0]
    typer.echo(f"  open reviews: {open_reviews}")
    # Automatic cross-check: new records that share a place id, domain+suburb or phone with another record.
    dupes = conn.execute("""
        SELECT COUNT(*) FROM business b WHERE b.research_status != 'legacy' AND EXISTS (
          SELECT 1 FROM business o WHERE o.lead_key != b.lead_key AND (
            (b.google_place_id IS NOT NULL AND o.google_place_id = b.google_place_id) OR
            (b.phone_e164 IS NOT NULL AND o.phone_e164 = b.phone_e164 AND o.name_norm = b.name_norm)))""").fetchone()[0]
    total_new = conn.execute("SELECT COUNT(*) FROM business WHERE research_status != 'legacy'").fetchone()[0]
    typer.echo(f"  new records: {total_new}; with an exact-identity twin: {dupes}")
    out = cfg.resolve(cfg.paths.exports) / "dedup_sample.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("""SELECT lead_id, company_name, city, suburb_area, street_address, phone, website, maps_category
                           FROM business WHERE research_status != 'legacy' ORDER BY RANDOM() LIMIT ?""", (sample,))
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["lead_id", "company_name", "city", "suburb", "address", "phone", "website", "category",
                    "is_duplicate_of_lead_id (fill in)"])
        w.writerows(tuple(r) + ("",) for r in rows)
    typer.echo(f"  sample for manual check: {out}")


# -- Phase 4: website enrichment ---------------------------------------------

def _crawl_settings(cfg):
    from leadforge.enrichment.crawl import CrawlSettings

    c, e = cfg.concurrency, cfg.enrichment
    return CrawlSettings(max_pages=e.get("max_pages_per_domain", 5), domain_budget_s=e.get("domain_budget_seconds", 20),
                         timeout_s=e.get("request_timeout_seconds", 10), global_concurrency=c.get("http_global", 30),
                         per_domain=c.get("http_per_domain", 3))


@app.command()
def enrich(
    scope: str = typer.Option("new", help="new (discovered leads) | missing-email | all"),
    limit: int = typer.Option(200), country: str = typer.Option(None), city: str = typer.Option(None),
    retry_failed: bool = typer.Option(False),
    browser: int = typer.Option(10, help="Max JavaScript-only sites to load in a browser afterwards (0 = none)"),
    quiet: bool = typer.Option(False),
) -> None:
    """Crawl lead websites over HTTP (browser only for JS-only sites): emails, phones, contact page, text."""
    from leadforge.enrichment import pipeline

    cfg, conn = _open()
    targets = pipeline.select_targets(conn, scope, country, city, limit, retry_failed)
    if not targets:
        typer.echo("Nothing to enrich for that scope.")
        return
    typer.echo(f"Enriching {len(targets)} websites ...")

    def show(b, res, out):
        if not quiet:
            typer.echo(f"  {b['lead_id']:>6} {b['company_name'][:38]:<38} {res.website_status[:22]:<22} "
                       f"{len(res.pages)}p {out['primary_email'] or '-'}")

    stats = pipeline.run(conn, targets, _crawl_settings(cfg), show)
    typer.echo(stats.summary())
    if browser:
        n = pipeline.browser_fallback(conn, _session_factory(cfg, "google_maps_browser", False), browser, show)
        typer.echo(f"browser fallback: {n} sites")


@app.command("enrich-bench")
def enrich_bench(country: str = typer.Option("Australia"), city: str = typer.Option("Sydney"),
                 limit: int = typer.Option(200)) -> None:
    """Measure email recall: re-crawl leads whose email is already known (no writes)."""
    from leadforge.enrichment import pipeline

    cfg, conn = _open()
    typer.echo(pipeline.bench(conn, country, city, limit, _crawl_settings(cfg)))


@app.command("verify-emails")
def verify_emails_cmd(only_unchecked: bool = typer.Option(True, help="Skip leads already marked Valid/Invalid")) -> None:
    """MX-check the email domains of all leads (Valid = domain accepts mail). DNS only, no SMTP."""
    import asyncio

    from leadforge.enrichment.pipeline import verify_emails

    _, conn = _open()
    sql = "SELECT lead_key FROM business WHERE general_email IS NOT NULL"
    if only_unchecked:
        sql += " AND (email_verified IS NULL OR email_verified IN ('Not checked', ''))"
    keys = [r[0] for r in conn.execute(sql)]
    n = asyncio.run(verify_emails(conn, keys))
    typer.echo(f"checked {len(keys)} leads, status set on {n}")
    for r in conn.execute("SELECT email_verified, COUNT(*) FROM business WHERE general_email IS NOT NULL GROUP BY 1"):
        typer.echo(f"  {r[0] or 'blank':<20} {r[1]}")


if __name__ == "__main__":
    app()
