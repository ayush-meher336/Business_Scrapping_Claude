"""Concurrent website crawl (HTTP first) for many businesses at once.

Per business: homepage, then up to `max_pages - 1` relevant internal pages (contact, about,
materials, locations) fetched concurrently, within a per-domain limit and a time budget. Pages
are parsed as they arrive; nothing here touches the database.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
import urllib.robotparser
from dataclasses import dataclass, field

import httpx

from leadforge.dedup.normalize import registrable_domain
from leadforge.enrichment.extract import Found, extract, jsonld_fields, pick_links

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36 leadforge/0.1")
ROBOTS_AGENT = "leadforge"
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "just a moment...", "attention required", "captcha")


@dataclass
class CrawlSettings:
    max_pages: int = 5
    domain_budget_s: float = 20.0
    timeout_s: float = 10.0
    global_concurrency: int = 30
    per_domain: int = 3
    max_bytes: int = 2_000_000


@dataclass
class PageResult:
    url: str
    final_url: str | None
    status: int | None
    page_type: str
    ms: int
    html: str | None = None
    error: str | None = None
    via_browser: bool = False


@dataclass
class CrawlResult:
    lead_key: str
    website: str
    site_domain: str | None
    pages: list[PageResult] = field(default_factory=list)
    found: Found = field(default_factory=Found)
    website_status: str = "Unreachable"
    needs_browser: bool = False
    blocked: bool = False
    ms: int = 0

    def merge(self, f: Found) -> None:
        for e, ctx in f.emails.items():
            self.found.emails.setdefault(e, ctx)
        for p, ctx in f.phones.items():
            self.found.phones.setdefault(p, ctx)
        self.found.jsonld += f.jsonld
        self.found.linkedin = self.found.linkedin or f.linkedin
        self.found.opt_out = self.found.opt_out or f.opt_out

    @property
    def contact_page(self) -> str | None:
        for p in self.pages:
            if p.page_type == "contact" and p.status == 200:
                return p.final_url or p.url
        return None

    @property
    def jsonld(self) -> dict:
        return jsonld_fields(self.found.jsonld)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def today() -> str:
    return dt.date.today().isoformat()


class Crawler:
    def __init__(self, settings: CrawlSettings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.sem = asyncio.Semaphore(settings.global_concurrency)
        # Businesses in flight; a business's time budget starts only once it holds a slot.
        self.biz_sem = asyncio.Semaphore(max(settings.global_concurrency // 2, 1))
        self.domain_sems: dict[str, asyncio.Semaphore] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(settings.timeout_s), transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en;q=0.9,*;q=0.5"},
            limits=httpx.Limits(max_connections=settings.global_concurrency * 2), verify=True)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _dsem(self, host: str) -> asyncio.Semaphore:
        if host not in self.domain_sems:
            self.domain_sems[host] = asyncio.Semaphore(self.s.per_domain)
        return self.domain_sems[host]

    async def allowed(self, url: str) -> bool:
        parts = httpx.URL(url)
        origin = f"{parts.scheme}://{parts.host}"
        if origin not in self.robots:
            rp = None
            try:
                r = await self.client.get(origin + "/robots.txt", timeout=5)
                if r.status_code == 200 and len(r.content) < 500_000:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self.robots[origin] = rp
        rp = self.robots[origin]
        return True if rp is None else rp.can_fetch(ROBOTS_AGENT, url)

    async def fetch(self, url: str, page_type: str) -> PageResult:
        t0 = time.perf_counter()
        try:
            host = httpx.URL(url).host or ""
        except (httpx.InvalidURL, ValueError) as exc:
            return PageResult(url, None, None, page_type, 0, error=f"invalid URL: {exc}"[:300])
        async with self.sem, self._dsem(host):
            try:
                if not await self.allowed(url):
                    return PageResult(url, None, None, page_type, 0, error="robots.txt disallows")
                r = await self.client.get(url)
                ctype = r.headers.get("content-type", "")
                html = r.text[: self.s.max_bytes] if ("html" in ctype or not ctype) else None
                return PageResult(url, str(r.url), r.status_code, page_type,
                                  int((time.perf_counter() - t0) * 1000), html=html)
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeDecodeError) as exc:
                return PageResult(url, None, None, page_type, int((time.perf_counter() - t0) * 1000),
                                  error=f"{type(exc).__name__}: {exc}"[:300])

    async def crawl(self, lead_key: str, website: str, region: str | None) -> CrawlResult:
        t0 = time.perf_counter()
        if "://" not in website:
            website = "http://" + website
        res = CrawlResult(lead_key, website, registrable_domain(website))
        async with self.biz_sem:
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(self._crawl(res, region), timeout=self.s.domain_budget_s)
            except asyncio.TimeoutError:
                pass                                  # keep whatever arrived within the budget
            except Exception as exc:                  # one broken site never stops the batch
                res.website_status = f"Error ({type(exc).__name__})"
                if not res.pages:
                    res.pages.append(PageResult(website, None, None, "home", 0, error=str(exc)[:300]))
        res.ms = int((time.perf_counter() - t0) * 1000)
        return res

    async def _crawl(self, res: CrawlResult, region: str | None) -> None:
        home = await self.fetch(res.website, "home")
        res.pages.append(home)
        if home.html is None or home.status is None:
            res.website_status = f"Unreachable ({today()})" if home.status is None else f"HTTP {home.status}"
            return
        low = home.html[:20000].casefold()
        if home.status in (403, 429, 503) and any(m in low for m in CHALLENGE_MARKERS):
            res.blocked, res.needs_browser = True, True
            res.website_status = "Blocked"
            return
        if home.status >= 400:
            res.website_status = f"HTTP {home.status}"
            return
        final_domain = registrable_domain(home.final_url)
        site = final_domain or res.site_domain
        f = extract(home.html, home.final_url or home.url, region, site)
        if f.challenge:
            res.blocked, res.needs_browser = True, True
            res.website_status = "Blocked"
            home.html = None                         # never keep a bot-check page as content
            return
        res.merge(f)
        if f.parked:
            res.website_status = "Parked"
            return
        res.website_status = (f"Redirects to {final_domain}" if final_domain and res.site_domain
                              and final_domain != res.site_domain else "Live")
        if f.has_scripts_shell:
            res.needs_browser = True
        seen = {res.website.rstrip("/"), (home.final_url or "").rstrip("/")}
        for p in await asyncio.gather(*(self.fetch(u, k) for u, k in pick_links(f.links, seen, self.s.max_pages - 1))):
            res.pages.append(p)
            if p.html and p.status == 200:
                res.merge(extract(p.html, p.final_url or p.url, region, site))


async def crawl_many(items: list[tuple[str, str, str | None]], settings: CrawlSettings, on_result,
                     transport: httpx.AsyncBaseTransport | None = None) -> None:
    """items: (lead_key, website, region). `on_result(CrawlResult)` is called as each finishes."""
    crawler = Crawler(settings, transport)
    try:
        tasks = [asyncio.create_task(crawler.crawl(*it)) for it in items]
        try:
            for fut in asyncio.as_completed(tasks):
                on_result(await fut)
        finally:
            for t in tasks:                           # stop the rest cleanly before closing the client
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await crawler.aclose()
