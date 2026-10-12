"""Identity keys: normalised company name, registrable domain, E.164 phone."""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

import phonenumbers
import tldextract

from leadforge.core.text import strip_accents

# Offline: use the public-suffix snapshot bundled with tldextract, never fetch it.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Hosts that many unrelated businesses share; never usable as identity.
SHARED_HOSTS = {
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "business.site", "linktr.ee", "wixsite.com", "wordpress.com", "blogspot.com", "weebly.com",
    "square.site", "godaddysites.com", "google.com", "goo.gl", "g.page", "sites.google.com",
    "yellowpages.com.au", "yellowpages.com", "yelp.com", "truelocal.com.au", "hotfrog.com.au",
    "indiamart.com", "justdial.com", "tradeindia.com", "alibaba.com", "made-in-china.com",
    "europages.com", "kompass.com", "scrapmonster.com", "recycleinme.com", "wa.me", "whatsapp.com",
}

_LEGAL = re.compile(
    r"\b(pty|ltd|limited|llc|l\.l\.c|inc|incorporated|corp|corporation|co|company|gmbh|ag|bv|nv|sa|sas|sarl|"
    r"srl|spa|plc|pvt|private|pte|sdn|bhd|tbk|pt|cv|jsc|oy|ab|as|aps|kft|sro|s\.r\.o|kk|llp|lp|group|"
    r"enterprises?|trading|traders?)\b\.?", re.I)


def name_norm(name: str | None) -> str:
    """'W & K Scrap Metal Pty Ltd' -> 'w and k scrap metal'."""
    if not name:
        return ""
    s = strip_accents(name).casefold().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = _LEGAL.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


@lru_cache(maxsize=50000)
def registrable_domain(url: str | None) -> str | None:
    """'https://www.shop.example.com.au/x?utm=1' -> 'example.com.au'. None for shared platforms."""
    host = host_of(url)
    if not host:
        return None
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    domain = f"{ext.domain}.{ext.suffix}"
    if domain in SHARED_HOSTS or host in SHARED_HOSTS:
        return None
    if "gov" in ext.suffix.split(".") or ext.domain == "gov":
        return None          # government sites host many unrelated facilities (council centres etc.)
    return domain


def clean_website(url: str | None) -> str | None:
    """Drop Google tracking parameters from a website link."""
    if not url:
        return None
    return re.sub(r"[?&]utm_[^&#]*", "", url).rstrip("?&")


def phone_e164(raw: str | None, region: str | None) -> str | None:
    if not raw:
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
