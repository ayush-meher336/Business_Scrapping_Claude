import pytest

from leadforge.core.text import haversine_km, norm_place
from leadforge.geography import geonames, osm
from leadforge.geography.coverage import evaluate_zone, relink_leads
from leadforge.geography.geocode import admin_matches, geocode_cities, geocode_zones, name_variants, resolve_countries
from leadforge.geography.zones import BuildSettings, ExcludedCountryError, SizeRule, build_city
from leadforge.keywords.library import country_languages, import_legacy_yields, keywords_for_country, sync_keywords
from leadforge.keywords.planner import extend_round, make_query, plan
from leadforge.migration.importer import migrate
from leadforge.niches.model import load_niche
from leadforge.storage import db
from test_migration import workbook  # noqa: F401  (fixture)
from test_niche import ROOT

RULES = {"X": SizeRule(45, "suburbs", 0), "M": SizeRule(30, "suburbs", 1000),
         "H": SizeRule(25, "suburbs", 1000), "S": SizeRule(40, "towns", 2000)}


def settings(surrounding_pop=100000, surrounding_km=90, industrial_km=50.0):
    return BuildSettings(RULES, 1.5, surrounding_pop, surrounding_km, industrial_km)

# geonameid, name, asciiname, alternates, lat, lon, class, code, cc, cc2, admin1, ..., population
PLACES = [
    (2147714, "Sydney", "Sydney", "Sidney", -33.8679, 151.2073, "PPLA", "AU", "02", 4627345),
    (2147000, "Wetherill Park", "Wetherill Park", "", -33.8510, 150.9000, "PPLX", "AU", "02", 6000),
    (2147001, "Smithfield", "Smithfield", "", -33.8530, 150.9420, "PPLX", "AU", "02", 9000),
    (2147002, "Yennora", "Yennora", "", -33.8620, 150.9690, "PPLX", "AU", "02", 1500),
    (2147003, "Wollongong", "Wollongong", "", -34.4240, 150.8935, "PPL", "AU", "02", 260000),
    (2147004, "Smithfield", "Smithfield", "", -16.8300, 145.6900, "PPL", "AU", "04", 5000),  # QLD namesake
]


@pytest.fixture
def conn(tmp_path, workbook):  # noqa: F811
    c = db.connect(tmp_path / "g.db")
    db.init_schema(c)
    migrate(c, workbook)
    (tmp_path / "countryInfo.txt").write_text(
        "#ISO\tISO3\n" + "\t".join(["AU", "AUS", "036", "AS", "Australia", "Canberra", "7686850", "25687041",
                                     "OC", ".au", "AUD", "Dollar", "61", "", "", "en-AU", "", "", ""]) + "\n",
        encoding="utf-8")
    (tmp_path / "admin1.txt").write_text("AU.02\tNew South Wales\tNew South Wales\t1\nAU.04\tQueensland\tQueensland\t2\n",
                                         encoding="utf-8")
    lines = []
    for gid, name, ascii_, alt, lat, lon, code, cc, a1, pop in PLACES:
        lines.append("\t".join(map(str, [gid, name, ascii_, alt, lat, lon, "P", code, cc, "", a1, "", "", "",
                                         pop, "", "", "Australia/Sydney", "2024-01-01"])))
    (tmp_path / "cities.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    geonames.load_countries(c, tmp_path / "countryInfo.txt")
    geonames.load_admin1(c, tmp_path / "admin1.txt")
    geonames.load_places(c, tmp_path / "cities.txt")
    resolve_countries(c, {}, ["Iran"], "sanctions")
    c.commit()
    yield c
    c.close()


def test_text_helpers():
    assert norm_place("St. Mary's") == norm_place("Saint Marys") == "st marys"
    assert name_variants("Kandla / Gandhidham (port)")[1:] == ["kandla gandhidham", "kandla", "gandhidham"]
    assert admin_matches("NSW", "New South Wales") and admin_matches("Vic", "Victoria")
    assert 20 < haversine_km(-33.8679, 151.2073, -33.851, 150.90) < 30
    assert country_languages("English, Hindi, Gujarati") == ["en", "hi", "gu"]


def test_geocode_and_zone_build(conn):
    assert geocode_cities(conn)["matched"] == 1
    z = geocode_zones(conn)
    assert z["matched"] == 2           # Smithfield resolves to the NSW one, near Sydney
    smithfield = conn.execute("SELECT lat FROM zone WHERE area='Smithfield'").fetchone()
    assert smithfield["lat"] == pytest.approx(-33.853)

    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    r = build_city(conn, place, settings(), "run1")   # existing zones are the industrial evidence
    assert r.existing == 2 and r.matched_existing == 2
    assert r.new_zones == ["Yennora"]          # the only uncovered suburb
    assert r.new_cities == ["Wollongong"]      # 63 km out, > 100k, not in the Cities tab
    new = conn.execute("SELECT zone_id, status, source FROM zone WHERE area='Yennora'").fetchone()
    assert new["zone_id"] == 3 and new["status"] == "Not started" and new["source"] == "geonames"

    # Re-running adds nothing.
    assert build_city(conn, place, settings(), "run2").new_zones == []


def test_industrial_areas_become_zones(conn):
    geocode_cities(conn)
    geocode_zones(conn)
    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    feats = [
        {"osm_ref": "gmaps:1", "kind": "industrial", "name": "Huntingwood Estate", "lat": -33.84, "lon": 150.99},
        {"osm_ref": "gmaps:2", "kind": "industrial", "name": "Wetherill Park Industrial", "lat": -33.8511, "lon": 150.9001},
    ]
    osm.store(conn, place, feats)
    r = build_city(conn, place, settings(10**9, industrial_km=1.5), "run1")
    assert "Huntingwood Estate" in r.new_zones
    assert "Wetherill Park Industrial" not in r.new_zones   # within 1.5 km of an existing zone
    assert conn.execute("SELECT kind FROM zone WHERE area='Huntingwood Estate'").fetchone()[0] == "industrial"


def test_suburbs_need_industrial_cluster(conn):
    geocode_cities(conn)
    geocode_zones(conn)
    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    feats = [
        {"osm_ref": "node/1", "kind": "suburb", "name": "Quietvale", "lat": -33.70, "lon": 151.10},
        {"osm_ref": "way/2", "kind": "industrial", "name": "Bus Depot", "lat": -33.701, "lon": 151.101},
        {"osm_ref": "node/3", "kind": "suburb", "name": "Workton", "lat": -33.75, "lon": 151.00},
    ] + [{"osm_ref": f"way/1{i}", "kind": "industrial", "name": f"Factory {i}", "lat": -33.75 + i / 1000,
          "lon": 151.00} for i in range(3)]
    osm.store(conn, place, feats)
    r = build_city(conn, place, BuildSettings(RULES, 1.5, 10**9, 90, 1.5), "run1", dry_run=True)
    assert "Workton" in r.new_zones           # three separate industrial sites nearby
    assert "Quietvale" not in r.new_zones     # one depot is not an industrial area
    assert "Factory 0" not in r.new_zones     # single sites are evidence, not zones


def test_zone_geocode_falls_back_to_osm(conn):
    geocode_cities(conn)
    conn.execute("UPDATE zone SET area='Pinkenba' WHERE area='Smithfield'")   # not in GeoNames
    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    osm.store(conn, place, [{"osm_ref": "node/9", "kind": "suburb", "name": "Pinkenba", "lat": -33.9, "lon": 151.1}])
    z = geocode_zones(conn)
    assert z["matched"] == 1 and z["matched_osm"] == 1
    assert conn.execute("SELECT source_ref FROM zone WHERE area='Pinkenba'").fetchone()[0] == "osm:node/9"


def test_excluded_country_is_refused(conn):
    conn.execute("UPDATE country SET excluded=1, excluded_reason='test' WHERE name='Australia'")
    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    with pytest.raises(ExcludedCountryError):
        build_city(conn, place, settings(), "r")
    with pytest.raises(ExcludedCountryError):
        plan(conn, "scrapyards", ["Australia"], ["google_maps_browser"], "r")


def test_keywords_yields_and_plan(conn):
    niche = load_niche(ROOT, "scrapyards")
    added = sync_keywords(conn, niche)
    assert added["core"] >= 6 and added["local"] >= 1
    kws = {k["term_norm"] for k in keywords_for_country(conn, "scrapyards", "Australia")}
    assert "scrap metal" in kws and "kabadiwala" not in kws        # Hindi pack not used for Australia
    y = import_legacy_yields(conn, "scrapyards")
    assert y["credited"] == 1                                        # "scrap metal Wetherill Park"

    # Zones are legacy 'Done'; reopen one to plan it.
    conn.execute("UPDATE zone SET status='Not started' WHERE area='Smithfield'")
    r = plan(conn, "scrapyards", ["Australia"], ["google_maps_browser", "web_search_browser"], "p1", per_zone=3)
    assert r.zones == 1 and r.jobs_created == 6
    jobs = conn.execute("SELECT query, priority FROM search_job ORDER BY priority DESC").fetchall()
    assert jobs[0]["query"].startswith("scrap metal Smithfield")     # proven keyword ranks first
    assert plan(conn, "scrapyards", ["Australia"], ["google_maps_browser"], "p2").jobs_created == 0  # idempotent
    zid = conn.execute("SELECT zone_id FROM zone WHERE area='Smithfield'").fetchone()[0]
    assert extend_round(conn, zid, "scrapyards", ["google_maps_browser"], "p3", per_round=2) == 2
    assert conn.execute("SELECT MAX(round) FROM search_job").fetchone()[0] == 2


def test_completion_rule(conn):
    niche = load_niche(ROOT, "scrapyards")
    sync_keywords(conn, niche)
    conn.execute("UPDATE zone SET status='Not started' WHERE area='Smithfield'")
    zid = conn.execute("SELECT zone_id FROM zone WHERE area='Smithfield'").fetchone()[0]
    plan(conn, "scrapyards", ["Australia"], ["google_maps_browser"], "p", per_zone=3)
    assert evaluate_zone(conn, zid, ["google_maps_browser"]).status == "unchanged"

    # One empty search does not close a zone.
    first = conn.execute("SELECT job_id FROM search_job ORDER BY job_id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE search_job SET status='done', results=0, new_uniques=0 WHERE job_id=?", (first,))
    assert evaluate_zone(conn, zid, ["google_maps_browser"]).status == "In progress"

    conn.execute("UPDATE search_job SET status='done', results=40, new_uniques=1")
    v = evaluate_zone(conn, zid, ["google_maps_browser", "web_search_browser"])
    assert v.status == "In progress" and "web_search_browser" in v.reason
    assert evaluate_zone(conn, zid, ["google_maps_browser"]).status == "Done"

    conn.execute("UPDATE search_job SET new_uniques=0")
    extend_round(conn, zid, "scrapyards", ["google_maps_browser"], "p", per_round=2)
    conn.execute("UPDATE search_job SET status='done', results=10, new_uniques=0 WHERE status='pending'")
    assert evaluate_zone(conn, zid, ["google_maps_browser"]).status == "Exhausted"


def test_relink_leads_uses_normalised_names(conn):
    conn.execute("UPDATE business SET zone_id=NULL, suburb_area='SMITHFIELD ' WHERE lead_id=2")
    assert relink_leads(conn) == 1


def test_make_query():
    assert make_query("scrap metal", "Sydney", "Sydney") == "scrap metal Sydney"
    assert make_query("scrap yard", "Smithfield", "Sydney") == "scrap yard Smithfield Sydney"


def test_city_without_any_evidence_waits_for_maps_scan(conn):
    geocode_cities(conn)
    conn.execute("UPDATE business SET zone_id=NULL")
    conn.execute("DELETE FROM zone")
    place = conn.execute("SELECT place_id FROM geo_place WHERE name='Sydney'").fetchone()[0]
    assert build_city(conn, place, settings(), "r").skipped.startswith("no industrial evidence")
