"""Web search results through a SeleniumBase browser (DuckDuckGo's HTML results page).

Finds official websites and directory listings for a zone x keyword query. Results become
candidates with a website and a title-derived name; they have no coordinates, so they resolve
mainly by domain and phone. Verification pages are never bypassed.
"""
from __future__ import annotations

import random
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass

from selectolax.lexbor import LexborHTMLParser

from leadforge.discovery.gmaps import BlockedError

_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")


@dataclass
class WebResult:
    name: str
    website: str
    snippet: str | None = None
    title: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def result_url(href: str) -> str | None:
    """DuckDuckGo wraps targets as //duckduckgo.com/l/?uddg=<url>."""
    if not href:
        return None
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        return q.get("uddg", [None])[0]
    return href if href.startswith("http") else None


def name_from_title(title: str) -> str:
    """'W&K Scrap Metal | Scrap Metal Recycling Sydney' -> 'W&K Scrap Metal'."""
    parts = _TITLE_SPLIT.split(title.strip())
    return parts[0].strip() if parts else title.strip()


def parse_results(html: str) -> list[WebResult]:
    tree = LexborHTMLParser(html)
    out = []
    for res in tree.css("div.result"):
        if "result--ad" in (res.attributes.get("class") or ""):
            continue                                         # sponsored
        a = res.css_first("a.result__a")
        if a is None:
            continue
        url = result_url(a.attributes.get("href") or "")
        if not url:
            continue
        title = a.text(strip=True)
        snip = res.css_first(".result__snippet")
        out.append(WebResult(name=name_from_title(title), website=url, title=title,
                             snippet=snip.text(strip=True) if snip else None))
    return out


class WebSearchSession:
    def __init__(self, headless: bool = True, min_delay_s: float = 5.0, max_delay_s: float = 10.0):
        self.headless, self.min_delay_s, self.max_delay_s = headless, min_delay_s, max_delay_s
        self._cm = None
        self.sb = None
        self._last = 0.0

    def __enter__(self) -> "WebSearchSession":
        from seleniumbase import SB
        self._cm = SB(headless=self.headless, locale_code="en")
        self.sb = self._cm.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._cm is not None:
            self._cm.__exit__(*exc)

    def search(self, query: str) -> list[WebResult]:
        wait = random.uniform(self.min_delay_s, self.max_delay_s) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.sb.open("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query))
        time.sleep(1.5)
        html = self.sb.get_page_source()
        if "anomaly" in html.casefold() and "result__a" not in html:
            raise BlockedError(f"DuckDuckGo asked for verification on: {query}")
        return parse_results(html)
