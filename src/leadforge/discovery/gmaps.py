"""Google Maps results through a SeleniumBase browser.

One search = open the results page, scroll the results feed to the end (or a cap), then read
every card in a single pass. Cards already carry name, category, street address, phone,
website, coordinates and place id, so individual listings are not opened.

No CAPTCHA or bot-check is ever solved or bypassed: if Google shows one, the search raises
`BlockedError`, the runner stops, and a human decides what to do.
"""
from __future__ import annotations

import random
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field

from selectolax.lexbor import LexborHTMLParser

FEED = 'div[role="feed"]'
_PLACE_ID = re.compile(r"!19s(ChIJ[\w-]+)")
_FID = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
_LAT = re.compile(r"!3d(-?\d+\.\d+)")
_LON = re.compile(r"!4d(-?\d+\.\d+)")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")
_RATING = re.compile(r"^\d\.\d$")
_REVIEWS = re.compile(r"^\(([\d,]+)\)$")
_HOURS = re.compile(r"^(·\s*)?(open|closed|opens|closes|open 24 hours|24 hours|temporarily closed)\b", re.I)
_NOISE = {"", "·", "Website", "Directions", "Sponsored", "Order online", "Reserve a table", "Menu", "No reviews"}
_ICONS = re.compile("[-]")   # Google's icon-font glyphs (private use area)


class BlockedError(RuntimeError):
    """Google is asking for human verification; stop and hand over to a person."""


@dataclass
class MapsResult:
    name: str
    maps_url: str
    place_id: str | None = None
    fid: str | None = None
    lat: float | None = None
    lon: float | None = None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    reviews: int | None = None
    permanently_closed: bool = False
    extra: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def search_url(query: str, lang: str = "en") -> str:
    return f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}?hl={lang}"


def _float(rx: re.Pattern, s: str) -> float | None:
    m = rx.search(s)
    return float(m.group(1)) if m else None


def parse_card(anchor) -> MapsResult | None:
    href = anchor.attributes.get("href") or ""
    name = (anchor.attributes.get("aria-label") or "").strip()
    if not name or "/maps/place/" not in href:
        return None
    card = anchor.parent
    m_pid, m_fid = _PLACE_ID.search(href), _FID.search(href)
    r = MapsResult(name=name, maps_url=href.split("?")[0], place_id=m_pid.group(1) if m_pid else None,
                   fid=m_fid.group(1) if m_fid else None, lat=_float(_LAT, href), lon=_float(_LON, href))
    for a in card.css("a"):
        if a.attributes.get("data-value") == "Website":
            r.website = a.attributes.get("href")

    rest: list[str] = []
    for frag in (_ICONS.sub("", f).strip("  ") for f in card.text(separator="|", strip=True).split("|")):
        if frag in _NOISE or frag == name:
            continue
        if r.rating is None and _RATING.match(frag):
            r.rating = float(frag)
        elif _REVIEWS.match(frag):
            r.reviews = int(_REVIEWS.match(frag).group(1).replace(",", ""))
        elif frag.casefold() == "permanently closed":
            r.permanently_closed = True
        elif _HOURS.match(frag) or frag.startswith("· "):
            continue
        elif r.phone is None and _PHONE.match(frag) and sum(c.isdigit() for c in frag) >= 7:
            r.phone = frag
        else:
            rest.append(frag)
    if rest:
        r.category = rest[0]
    if len(rest) > 1:
        r.address = rest[1]
    r.extra = rest[2:]
    return r


def parse_feed(html: str) -> list[MapsResult]:
    tree = LexborHTMLParser(html)
    out, seen = [], set()
    for a in tree.css("a.hfpxzc"):
        r = parse_card(a)
        if r and (r.place_id or r.maps_url) not in seen:
            seen.add(r.place_id or r.maps_url)
            out.append(r)
    return out


def looks_blocked(url: str, page_text: str) -> bool:
    return "/sorry/" in url or "unusual traffic" in page_text.casefold() or "not a robot" in page_text.casefold()


class MapsSession:
    """One browser. Use as a context manager; call `search` repeatedly."""

    def __init__(self, headless: bool = True, max_results: int = 60, min_delay_s: float = 4.0,
                 max_delay_s: float = 8.0, scroll_pause_s: float = 1.5, lang: str = "en"):
        self.headless, self.max_results, self.lang = headless, max_results, lang
        self.min_delay_s, self.max_delay_s, self.scroll_pause_s = min_delay_s, max_delay_s, scroll_pause_s
        self._cm = None
        self.sb = None
        self._last = 0.0

    def __enter__(self) -> "MapsSession":
        from seleniumbase import SB  # imported lazily: tests and non-browser commands do not need Chrome
        self._cm = SB(headless=self.headless, locale_code=self.lang)
        self.sb = self._cm.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._cm is not None:
            self._cm.__exit__(*exc)

    def _pace(self) -> None:
        wait = random.uniform(self.min_delay_s, self.max_delay_s) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _reject_consent(self) -> None:
        # Privacy-preserving choice on Google's consent page, when one is shown.
        for sel in ('button[aria-label="Reject all"]', 'button[aria-label="Alle ablehnen"]'):
            if self.sb.is_element_visible(sel):
                self.sb.click(sel)
                time.sleep(2)
                return

    def search(self, query: str) -> list[MapsResult]:
        self._pace()
        sb = self.sb
        sb.open(search_url(query, self.lang))
        time.sleep(2.5)
        self._reject_consent()
        text = sb.execute_script("return document.body ? document.body.innerText.slice(0, 4000) : ''")
        if looks_blocked(sb.get_current_url(), text):
            raise BlockedError(f"Google asked for verification on: {query}")
        if not sb.is_element_present(FEED):
            # A single exact match opens the place panel directly instead of a list.
            if "/maps/place/" in sb.get_current_url():
                return self._single_place(sb)
            return []
        last, stall = 0, 0
        while True:
            count = sb.execute_script(f"return document.querySelectorAll('{FEED} a.hfpxzc').length")
            ended = sb.execute_script(
                f"const f=document.querySelector('{FEED}'); return f ? /end of the list/i.test(f.innerText) : true")
            if ended or count >= self.max_results:
                break
            stall = stall + 1 if count == last else 0
            if stall >= 3:
                break
            last = count
            sb.execute_script(f"const f=document.querySelector('{FEED}'); if (f) f.scrollBy(0, 4000);")
            time.sleep(self.scroll_pause_s)
        html = sb.execute_script(f"const f=document.querySelector('{FEED}'); return f ? f.outerHTML : ''")
        return parse_feed(html)[: self.max_results]

    def _single_place(self, sb) -> list[MapsResult]:
        url = sb.get_current_url()
        name = sb.execute_script("const h=document.querySelector('h1'); return h ? h.innerText : ''")
        if not name:
            return []
        m_pid, m_fid = _PLACE_ID.search(url), _FID.search(url)
        return [MapsResult(name=name.strip(), maps_url=url.split("?")[0],
                           place_id=m_pid.group(1) if m_pid else None, fid=m_fid.group(1) if m_fid else None,
                           lat=_float(_LAT, url), lon=_float(_LON, url))]
