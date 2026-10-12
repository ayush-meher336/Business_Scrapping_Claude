"""Phase 3: Maps parsing, web-search parsing, resolver rules, runner. All data here is invented."""
import pytest

from leadforge.dedup.normalize import clean_website, name_norm, phone_e164, registrable_domain
from leadforge.dedup.resolver import Context, backfill_identity, resolve
from leadforge.discovery.gmaps import BlockedError, looks_blocked, parse_feed
from leadforge.discovery.industrial import is_area
from leadforge.discovery.runner import Relevance, run
from leadforge.discovery.websearch import name_from_title, parse_results
from leadforge.keywords.library import sync_keywords
from leadforge.keywords.planner import plan
from leadforge.niches.model import load_niche
from test_geography import conn  # noqa: F401  (fixture: migrated + GeoNames)
from test_migration import workbook  # noqa: F401
from test_niche import ROOT


def card(name, pid, lat, lon, category, address, phone, website=None, rating="4.5"):
    web = (f'<a data-value="Website" aria-label="Visit {name}\'s website" href="{website}?utm_source=google"></a>'
           if website else "")
    return (f'<div class="Nv2PK"><a class="hfpxzc" aria-label="{name}" href="https://www.google.com/maps/place/x/'
            f'data=!4m7!3m6!1s0x6b12:0x{pid[-4:]}!8m2!3d{lat}!4d{lon}!16s%2Fg%2F1!19s{pid}?hl=en"></a>'
            f'<div>{name}</div><span>{rating}</span><span>{category}</span><span>·</span><span></span>'
            f'<span>{address}</span><span>Closed</span><span>· Opens 7 am Tue</span><span>·</span>'
            f'<span>{phone}</span>{web}<span>Directions</span></div>')


FEED = ('<div role="feed">'
        + card("Acme Scrap Metal Pty Ltd", "ChIJacme0001", -33.8457, 150.8854, "Scrap metal dealer",
               "28 Hallstrom Pl", "+61 2 9756 2636", "https://www.acmescrap.com.au/")
        + card("Bright Car Wash", "ChIJwash0002", -33.85, 150.89, "Car wash", "1 Main St", "+61 2 9000 0000")
        + card("Copper Kings", "ChIJcopp0003", -33.8437, 150.8926, "Recycling center", "Unit 4/65 Elizabeth St",
               "+61 414 900 577", rating="")
        + "</div>")


@pytest.fixture
def ctx_conn(conn):  # noqa: F811
    from leadforge.geography.geocode import geocode_cities, geocode_zones
    geocode_cities(conn)
    geocode_zones(conn)
    backfill_identity(conn)
    conn.execute("INSERT INTO run(run_id, kind, started_at) VALUES ('t', 'research', 'now')")
    return conn


def ctx(area="Wetherill Park", zone_id=1):
    return Context(zone_id=zone_id, country="Australia", iso2="AU", region="Oceania", state="NSW", city="Sydney",
                   area=area, zone_lat=-33.851, zone_lon=150.90, zone_radius_m=2000, keyword="scrap metal",
                   run_id="t", job_id=None)


def raw(**kw):
    base = {"source": "google_maps_browser", "source_type": "Google Maps listing"}
    return {**base, **kw}


# -- parsing ---------------------------------------------------------------
def test_parse_feed_reads_every_field_without_opening_cards():
    rs = parse_feed(FEED)
    assert [r.name for r in rs] == ["Acme Scrap Metal Pty Ltd", "Bright Car Wash", "Copper Kings"]
    a = rs[0]
    assert (a.category, a.address, a.phone, a.place_id) == ("Scrap metal dealer", "28 Hallstrom Pl",
                                                            "+61 2 9756 2636", "ChIJacme0001")
    assert a.lat == pytest.approx(-33.8457) and a.rating == 4.5
    assert a.website.startswith("https://www.acmescrap.com.au/")
    assert rs[2].rating is None and rs[2].address == "Unit 4/65 Elizabeth St"


def test_blocked_detection():
    assert looks_blocked("https://www.google.com/sorry/index?continue=x", "")
    assert looks_blocked("https://www.google.com/maps", "Our systems have detected unusual traffic")
    assert not looks_blocked("https://www.google.com/maps/search/x", "Results")


def test_parse_web_results():
    html = ('<div class="result results_links"><a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fwww.acmescrap.com.au%2Fcontact&rut=x">Acme Scrap Metal | Scrap Buyers Sydney</a>'
            '<a class="result__snippet">We buy copper and brass.</a></div>'
            '<div class="result result--ad"><a class="result__a" href="https://ads.example">Ad</a></div>')
    rs = parse_results(html)
    assert len(rs) == 1 and rs[0].website == "https://www.acmescrap.com.au/contact"
    assert rs[0].name == "Acme Scrap Metal" and "copper" in rs[0].snippet
    assert name_from_title("Kings Recycling - Home") == "Kings Recycling"


def test_normalisers():
    assert name_norm("W & K Scrap Metal Pty Ltd") == "w and k scrap metal"
    assert registrable_domain("https://shop.acme.com.au/x?y=1") == "acme.com.au"
    assert registrable_domain("https://www.facebook.com/acme") is None
    assert phone_e164("02 9756 2636", "AU") == "+61297562636"
    assert clean_website("https://a.com/?utm_source=google&utm_medium=x") == "https://a.com/"
    assert is_area({"name": "Momentum Industrial Estate", "category": "Business park"})
    assert not is_area({"name": "Bob's Tyres", "category": "Tyre shop"})
    assert not is_area({"name": "Industrial Park", "category": "Industrial estate"})


# -- resolver --------------------------------------------------------------
def test_merges_into_legacy_lead_by_domain_suburb_and_name(ctx_conn):
    # Fixture lead 1: "W & K Scrap Metal", https://wk.com.au/, Wetherill Park.
    r = resolve(ctx_conn, raw(name="W&K Scrap Metal", website="https://www.wk.com.au/?utm_source=google",
                              lat=-33.8457, lon=150.8854, address="28 Hallstrom Pl", phone="+61 2 9999 1111",
                              place_id="ChIJwk"), ctx())
    assert r.action == "merged" and r.reason == "2:domain+suburb+name"
    b = ctx_conn.execute("SELECT * FROM business WHERE lead_id=1").fetchone()
    assert b["google_place_id"] == "ChIJwk" and b["lat"] == pytest.approx(-33.8457)   # gaps filled
    assert b["phone"] == "02 9756 2636"                                                  # never overwritten
    assert ctx_conn.execute("SELECT COUNT(*) FROM source_record WHERE lead_key=?", (b["lead_key"],)).fetchone()[0] == 2
    # Seen again: now matched by place id.
    assert resolve(ctx_conn, raw(name="W & K", place_id="ChIJwk"), ctx()).reason == "1:place_id"


def test_new_branch_review_and_reject(ctx_conn):
    n0 = ctx_conn.execute("SELECT COUNT(*) FROM business").fetchone()[0]
    new = resolve(ctx_conn, raw(name="Copper Kings", lat=-33.84, lon=150.89, phone="+61 414 900 577",
                                place_id="ChIJck"), ctx())
    assert new.action == "new"
    b = ctx_conn.execute("SELECT * FROM business WHERE lead_key=?", (new.lead_key,)).fetchone()
    assert b["lead_id"] == 3 and b["research_status"] == "discovered" and b["phone_e164"] == "+61414900577"
    assert b["located_in_zone"] == "Yes - premises in this suburb"

    # Same phone + similar name, different listing -> merge (rule 3).
    m = resolve(ctx_conn, raw(name="Copper Kings Recycling", phone="0414 900 577", place_id="ChIJck2"), ctx())
    assert m.action == "merged" and m.reason == "3:phone+name"

    # Same website, 20 km away -> separate branch linked by group_domain (rule 4).
    ctx_conn.execute("UPDATE business SET street_address='9 Other St' WHERE lead_id=2")   # legacy Safari Copper
    resolve(ctx_conn, raw(name="Safari Copper", website="https://safari.com.au/", lat=-33.84, lon=150.89, address="108 Newton Rd",
                          place_id="ChIJs1"), ctx())
    br = resolve(ctx_conn, raw(name="Safari Copper Penrith", website="https://safari.com.au/penrith", address="5 Lee Holm Rd",
                               lat=-33.75, lon=150.69, place_id="ChIJs2"), ctx("Penrith", None))
    assert br.action == "new" and br.reason == "4:domain-other-site"
    assert ctx_conn.execute("SELECT COUNT(*) FROM business WHERE group_domain='safari.com.au'").fetchone()[0] >= 2

    # Near-identical name, no shared identifier -> kept, but queued for a human (rule 5).
    rv = resolve(ctx_conn, raw(name="Copper King", place_id="ChIJck3", lat=-33.70, lon=151.0), ctx())
    assert rv.action == "review"
    assert ctx_conn.execute("SELECT COUNT(*) FROM dedup_review WHERE status='open'").fetchone()[0] == 1

    rej = resolve(ctx_conn, raw(name="Bright Car Wash", place_id="ChIJcw"), ctx(), relevant=False,
                  reject_reason="category not in niche: Car wash")
    assert rej.action == "rejected" and rej.lead_key is None
    assert ctx_conn.execute("SELECT COUNT(*) FROM business").fetchone()[0] == n0 + 4
    assert ctx_conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 6


def test_shared_phone_is_not_identity(ctx_conn):
    for i, nm in enumerate(["Alpha Metals", "Bravo Recycling", "Charlie Salvage"]):
        resolve(ctx_conn, raw(name=nm, phone="1300 000 000", place_id=f"ChIJsh{i}"), ctx())
    r = resolve(ctx_conn, raw(name="Alpha Metals Group", phone="1300 000 000", place_id="ChIJsh9"), ctx())
    assert r.reason != "3:phone+name"     # call-centre number shared by 3 businesses


# -- runner ----------------------------------------------------------------
class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def search(self, query):
        page = self.pages(query)
        if page == "BLOCK":
            raise BlockedError("verification page")
        return parse_feed(page)


def _queue(conn, jobs=2):
    sync_keywords(conn, load_niche(ROOT, "scrapyards"))
    conn.execute("UPDATE zone SET status='Not started'")
    plan(conn, "scrapyards", ["Australia"], ["google_maps_browser"], "p", per_zone=jobs)
    conn.commit()


def test_runner_end_to_end(ctx_conn):
    _queue(ctx_conn)
    n = load_niche(ROOT, "scrapyards")
    rel = Relevance(n.discovery.include, n.discovery.exclude)
    stats = run(ctx_conn, "google_maps_browser", lambda: FakeSession(lambda q: FEED), rel, max_jobs=10, workers=2)
    assert stats.jobs_done == 4 and stats.blocked is None
    assert stats.rejected == 4                      # the car wash, once per job
    assert stats.new >= 1                           # Copper Kings / Acme only once each
    names = [r[0] for r in ctx_conn.execute("SELECT company_name FROM business WHERE research_status='discovered'")]
    assert len(names) == len(set(names))
    assert ctx_conn.execute("SELECT COUNT(*) FROM search_job WHERE status='done'").fetchone()[0] == 4
    assert ctx_conn.execute("SELECT SUM(queries) FROM keyword_yield WHERE source='google_maps_browser'").fetchone()[0] == 4
    assert ctx_conn.execute("SELECT status FROM zone WHERE zone_id=1").fetchone()[0] == "In progress"


def test_runner_stops_on_verification_page(ctx_conn):
    _queue(ctx_conn)
    rel = Relevance(["scrap"], [])
    stats = run(ctx_conn, "google_maps_browser", lambda: FakeSession(lambda q: "BLOCK"), rel, max_jobs=10, workers=1)
    assert stats.blocked and stats.jobs_done == 0
    assert ctx_conn.execute("SELECT COUNT(*) FROM search_job WHERE status='blocked'").fetchone()[0] == 1
    assert ctx_conn.execute("SELECT COUNT(*) FROM search_job WHERE status='claimed'").fetchone()[0] == 0


def test_same_address_and_name_merges_despite_other_phone(ctx_conn):
    # Fixture lead 2 "Safari Copper" has no street; give it one, as a legacy row would.
    ctx_conn.execute("UPDATE business SET street_address='110 Fairfield St', city='Sydney' WHERE lead_id=2")
    r = resolve(ctx_conn, raw(name="Safari Copper Recycling", address="110 Fairfield St", phone="+61 457 000 566",
                              website="https://other.com.au/", place_id="ChIJsf"), ctx())
    assert r.action == "merged" and r.reason == "2b:address+name"


def test_suburb_comes_from_coordinates_not_the_search_zone(ctx_conn):
    # Searched from Wetherill Park, but the business sits next to the Smithfield zone.
    r = resolve(ctx_conn, raw(name="Faraway Metals", lat=-33.8531, lon=150.9421, place_id="ChIJfm"), ctx())
    b = ctx_conn.execute("SELECT suburb_area, zone_id, located_in_zone FROM business WHERE lead_key=?",
                         (r.lead_key,)).fetchone()
    assert (b["suburb_area"], b["zone_id"]) == ("Smithfield", 2)
    assert b["located_in_zone"] == "Yes - premises in this suburb"
    assert registrable_domain("https://www.nsw.gov.au/recycling") is None


def test_same_premises_variants_merge(ctx_conn):
    first = resolve(ctx_conn, raw(name="Weekend Transfer Station", address="50/54 Moore St", phone="+61 2 9367 9210",
                                  place_id="ChIJw1", lat=-33.70, lon=151.0), ctx())
    # Facility name inside the other listing's address, different phone and name.
    r = resolve(ctx_conn, raw(name="Leichhardt Community Recycling Centre", place_id="ChIJw2",
                              address="Weekend Transfer Station, 50-54 Moore St", phone="+61 2 9392 5000"), ctx())
    assert r.action == "merged" and r.lead_key == first.lead_key and r.reason == "2b:address+name-in-address"
    # Renamed business: same address and phone, different name.
    resolve(ctx_conn, raw(name="Star Metal NSW", address="53 Vallance St", phone="+61 2 9833 8981",
                          place_id="ChIJs1", lat=-33.71, lon=151.0), ctx())
    r2 = resolve(ctx_conn, raw(name="Sunrise Scrap Metal Recycling", address="53 Vallance St",
                               phone="+61 2 9833 8981", place_id="ChIJs9"), ctx())
    assert r2.action == "merged" and r2.reason in ("2b:address+phone", "3:phone+name")
    # Same building, unrelated business: not merged.
    r3 = resolve(ctx_conn, raw(name="Zed Batteries", address="Unit 9/53 Vallance St", phone="+61 2 1111 2222",
                               place_id="ChIJz1"), ctx())
    assert r3.action in ("new", "review")


def test_service_area_listing_merges_by_domain(ctx_conn):
    a = resolve(ctx_conn, raw(name="KM Scrap Metals Cromer", address="Unit 15/7 Villiers Pl", lat=-33.75, lon=151.28,
                              website="https://kmscrap.com.au/", place_id="ChIJkm1"), ctx())
    r = resolve(ctx_conn, raw(name="KM Scrap Metals Pty Ltd", lat=-33.73, lon=151.0,
                              website="https://www.kmscrap.com.au/", place_id="ChIJkm2"), ctx())
    assert r.action == "merged" and r.lead_key == a.lead_key and r.reason == "2c:domain+no-address"
