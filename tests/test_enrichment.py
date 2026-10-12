"""Phase 4: extraction, crawling (mock HTTP), saving rules, email checks. All sites are invented."""
import asyncio

import httpx
import pytest

from leadforge.enrichment import emailcheck, pipeline
from leadforge.enrichment.crawl import CrawlSettings, crawl_many
from leadforge.enrichment.extract import decode_cfemail, extract, rank_emails
from test_geography import conn  # noqa: F401
from test_migration import workbook  # noqa: F401

HOME = """<html><head><title>Acme Scrap</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme Scrap",
 "telephone":"+61 2 9756 2636","address":{"@type":"PostalAddress","streetAddress":"28 Hallstrom Pl","postalCode":"2164"}}</script>
<script></script><script></script></head><body>
<nav><a href="/contact-us">Contact</a> <a href="/about">About us</a> <a href="/what-we-buy">Materials</a>
<a href="/blog/news">News</a> <a href="https://other.com/contact">Partner</a> <a href="/brochure.pdf">PDF</a></nav>
<p>We buy copper, brass and aluminium. Same-day pickup across Sydney.</p>
<img src="logo@2x.png"><a href="https://www.linkedin.com/company/acme-scrap/?x=1">LinkedIn</a>
<footer>Email <a href="/cdn-cgi/l/email-protection#__CF__">[email protected]</a></footer></body></html>"""
CONTACT = """<html><body><h1>Contact</h1><p>Call 02 9756 2637 or email sales [at] acmescrap [dot] com.au</p>
<a href="mailto:john.smith@acmescrap.com.au?subject=hi">John</a> <p>Owner mobile 0414 900 577</p>
<p>Please note: we do not accept unsolicited marketing emails.</p></body></html>"""


def cf(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


def site(handler_overrides=None, robots="User-agent: *\nDisallow: /private\n"):
    pages = {"/": HOME.replace("__CF__", cf("info@acmescrap.com.au")), "/contact-us": CONTACT,
             "/about": "<html><body><p>Family owned since 1985.</p></body></html>",
             "/what-we-buy": "<html><body><p>Copper, brass, batteries.</p></body></html>", "/robots.txt": robots}
    pages.update(handler_overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "down.example":
            raise httpx.ConnectError("no route")
        if host == "slow.example":
            return httpx.Response(200, html="<html><body>ok</body></html>")
        body = pages.get(path)
        if body is None:
            return httpx.Response(404, text="not found")
        ctype = "text/plain" if path.endswith(".txt") else "text/html; charset=utf-8"
        return httpx.Response(200, content=body.encode(), headers={"content-type": ctype})
    return httpx.MockTransport(handler)


def test_extract_everything_from_home_page():
    f = extract(HOME.replace("__CF__", cf("info@acmescrap.com.au")), "https://www.acmescrap.com.au/", "AU",
                "acmescrap.com.au")
    assert f.emails == {"info@acmescrap.com.au": "cf"}                      # decoded, image name ignored
    assert "+61297562636" in f.phones                                       # from JSON-LD
    kinds = {k for _, k in f.links}
    assert kinds == {"contact", "about", "products"}                        # blog + external + pdf dropped
    assert f.linkedin == "https://www.linkedin.com/company/acme-scrap/"
    assert decode_cfemail(cf("a@b.co")) == "a@b.co"


def test_extract_contact_page_obfuscation_and_opt_out():
    f = extract(CONTACT, "https://acmescrap.com.au/contact-us", "AU", "acmescrap.com.au")
    assert set(f.emails) == {"sales@acmescrap.com.au", "john.smith@acmescrap.com.au"}
    assert {"+61297562637", "+61414900577"} <= set(f.phones)
    assert f.opt_out
    assert rank_emails({"john.smith@acmescrap.com.au": "mailto", "sales@acmescrap.com.au": "obfuscated",
                        "acme@gmail.com": "text"}, "acmescrap.com.au")[0] == "sales@acmescrap.com.au"


def test_crawl_respects_page_budget_robots_and_failures():
    results = {}
    items = [("k1", "https://www.acmescrap.com.au/", "AU"), ("k2", "http://down.example/", "AU")]
    asyncio.run(crawl_many(items, CrawlSettings(max_pages=3), lambda r: results.__setitem__(r.lead_key, r),
                           transport=site()))
    r = results["k1"]
    assert r.website_status == "Live" and len(r.pages) == 3                 # home + 2 best pages
    assert [p.page_type for p in r.pages[1:]] == ["contact", "about"]
    assert {"info@acmescrap.com.au", "sales@acmescrap.com.au"} <= set(r.found.emails)
    assert r.found.opt_out and r.contact_page.endswith("/contact-us")
    assert results["k2"].website_status.startswith("Unreachable")

    blocked = {}
    asyncio.run(crawl_many([("k3", "https://www.acmescrap.com.au/", "AU")], CrawlSettings(),
                           lambda r: blocked.__setitem__(r.lead_key, r),
                           transport=site(robots="User-agent: *\nDisallow: /\n")))
    assert blocked["k3"].pages[0].error == "robots.txt disallows"


def test_save_fills_gaps_never_overwrites_and_checks_mx(conn, monkeypatch):  # noqa: F811
    async def fake_lookup(domain, resolver):
        return (domain == "acmescrap.com.au", "mx.acmescrap.com.au")
    monkeypatch.setattr(emailcheck, "_lookup", fake_lookup)
    conn.execute("UPDATE business SET website='https://www.acmescrap.com.au/', research_status='discovered', "
                 "general_email=NULL, phone='02 9999 0000' WHERE lead_id=1")
    conn.execute("UPDATE business SET website=NULL WHERE lead_id=2")
    conn.commit()
    targets = pipeline.select_targets(conn, "new", None, None, 10)
    assert [t["lead_id"] for t in targets] == [1]
    stats = pipeline.run(conn, targets, CrawlSettings(max_pages=5), transport=site())
    assert stats.with_email == 1 and stats.new_email == 1
    b = conn.execute("SELECT * FROM business WHERE lead_id=1").fetchone()
    assert b["general_email"] == "info@acmescrap.com.au" and b["email_type"].startswith("Role-based")
    assert b["phone"] == "02 9999 0000"                                     # existing value kept
    assert "+61297562636" in b["additional_phones"]
    assert b["opt_out_notice"] == "Yes" and b["website_status"] == "Live"
    assert b["email_verified"] == "Valid" and b["research_status"] == "enriched"
    assert b["street_address"] == "28 Hallstrom Pl"                         # from JSON-LD (was empty)
    assert b["contact_page_url"].endswith("/contact-us")
    assert conn.execute("SELECT COUNT(*) FROM page WHERE lead_key=? AND text IS NOT NULL",
                        (b["lead_key"],)).fetchone()[0] >= 2
    mx = {r[0]: r[1] for r in conn.execute("SELECT value, verify_status FROM contact WHERE kind='email'")}
    assert mx["info@acmescrap.com.au"] == "Valid"
    assert conn.execute("SELECT status FROM enrich_task").fetchone()[0] == "done"
    assert pipeline.select_targets(conn, "new", None, None, 10) == []        # not picked again


def test_email_status_mapping():
    assert emailcheck.status_for(True) == "Valid"
    assert emailcheck.status_for(False) == "Invalid / Bounced"
    assert emailcheck.status_for(None) == "Not checked"


def test_malformed_website_does_not_stop_the_batch():
    results = {}
    items = [("bad", "http://[broken/", "AU"), ("ok", "https://www.acmescrap.com.au/", "AU")]
    asyncio.run(crawl_many(items, CrawlSettings(max_pages=2), lambda r: results.__setitem__(r.lead_key, r),
                           transport=site()))
    assert results["ok"].website_status == "Live"
    assert results["bad"].pages[0].error
