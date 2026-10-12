"""Deterministic extraction from one HTML page: links, emails, phones, JSON-LD, opt-out notices."""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

import phonenumbers
from selectolax.lexbor import LexborHTMLParser

from leadforge.dedup.normalize import registrable_domain

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[|\(|\{)\s*at\s*(?:\]|\)|\})\s*"
    r"([A-Za-z0-9-]+(?:(?:\s*(?:\[|\(|\{)\s*dot\s*(?:\]|\)|\})\s*|\.)[A-Za-z0-9-]+)+)",
    re.I)
BAD_EMAIL_PARTS = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?)$|example\.|sentry|wixpress|@domain\.|@email\.|yourname|"
    r"youremail|@sentry|@2x|u003e|noreply|no-reply|donotreply|@mysite|@test\.|@localhost", re.I)
TECH_DOMAINS = {"wix.com", "wixpress.com", "sentry.io", "squarespace.com", "godaddy.com", "w3.org", "schema.org",
                "googleapis.com", "cloudflare.com", "wordpress.org", "jquery.com", "gstatic.com", "shopify.com"}
ROLE_LOCAL = {"info", "sales", "admin", "office", "enquiries", "enquiry", "inquiries", "inquiry", "contact",
              "contactus", "accounts", "recycling", "scrap", "hello", "mail", "support", "service", "orders",
              "purchasing", "export", "exports", "import", "general", "reception", "metals", "buying", "trade"}

OPT_OUT_RE = re.compile(
    r"(no unsolicited|unsolicited (commercial )?(e-?mails?|messages?|marketing|solicitations?) (are|is|will)? ?(not|never)|"
    r"do not (send|email) (us )?(any )?(unsolicited|marketing|spam|promotional)|"
    r"we do not (accept|want|wish to receive) unsolicited|not consent to (receive )?(unsolicited|marketing))", re.I)

PARKED_RE = re.compile(r"(domain (is )?for sale|this domain (may be|is) for sale|parked free|buy this domain|"
                       r"domain has expired|website coming soon|under construction)", re.I)

PAGE_KEYWORDS = {
    "contact": ["contact", "kontakt", "contacto", "contato", "contatti", "lien-he", "hubungi", "iletisim",
                "联系", "お問い合わせ", "संपर्क", "reach-us", "get-in-touch", "enquir", "location", "find-us"],
    "about": ["about", "company", "who-we-are", "profile", "uber-uns", "quienes", "sobre", "chi-siamo",
              "tentang", "hakkimizda", "关于"],
    "products": ["material", "what-we-buy", "we-buy", "services", "products", "metals", "prices", "recycl",
                 "scrap", "accept"],
    "location": ["branch", "locations", "yards", "depots", "sites"],
}
PAGE_PRIORITY = {"contact": 0, "about": 1, "products": 2, "location": 3}


@dataclass
class Found:
    emails: dict[str, str] = field(default_factory=dict)       # email -> context (mailto|text|jsonld|obfuscated|cf)
    phones: dict[str, str] = field(default_factory=dict)       # E.164 -> raw/context
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, page_type) internal candidates
    jsonld: list[dict] = field(default_factory=list)
    linkedin: str | None = None
    opt_out: bool = False
    parked: bool = False
    title: str | None = None
    visible_chars: int = 0
    has_scripts_shell: bool = False


def decode_cfemail(hexstr: str) -> str | None:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return None


def clean_email(raw: str) -> str | None:
    e = unquote(raw).strip().strip(".,;:()[]<>\"'").lower()
    e = e.split("?")[0]
    if not EMAIL_RE.fullmatch(e) or BAD_EMAIL_PARTS.search(e):
        return None
    if e.split("@", 1)[1] in TECH_DOMAINS:
        return None
    return e


def is_role(email: str) -> bool:
    local = email.split("@", 1)[0]
    return local in ROLE_LOCAL or re.split(r"[._-]", local)[0] in ROLE_LOCAL


def classify_link(url: str, text: str) -> str | None:
    hay = (urlsplit(url).path + " " + text).casefold()
    for kind, words in PAGE_KEYWORDS.items():
        if any(w in hay for w in words):
            return kind
    return None


def _walk_jsonld(obj, out: list[dict]) -> None:
    if isinstance(obj, list):
        for o in obj:
            _walk_jsonld(o, out)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            _walk_jsonld(obj["@graph"], out)
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and (x in ("Organization", "Corporation") or "Business" in x or x.endswith("Store")
                                       or x in ("RecyclingCenter", "AutoWrecker", "ProfessionalService"))
               for x in types):
            out.append(obj)


def parse_jsonld(tree) -> list[dict]:
    out: list[dict] = []
    for s in tree.css('script[type="application/ld+json"]'):
        raw = s.text() or ""
        try:
            _walk_jsonld(json.loads(raw), out)
        except (json.JSONDecodeError, RecursionError):
            continue
    return out


def jsonld_fields(objs: list[dict]) -> dict:
    """Flatten the useful fields of the first organisation-like object(s)."""
    f: dict = {}
    for o in objs:
        for key in ("telephone", "email", "url", "name", "description"):
            v = o.get(key)
            if isinstance(v, list):
                v = v[0] if v else None
            if isinstance(v, str) and key not in f:
                f[key] = v.strip()
        addr = o.get("address")
        if isinstance(addr, list):
            addr = addr[0] if addr else None
        if isinstance(addr, dict) and "street" not in f:
            f["street"] = addr.get("streetAddress")
            f["postcode"] = addr.get("postalCode")
            f["locality"] = addr.get("addressLocality")
    return f


def extract(html: str, page_url: str, region: str | None, site_domain: str | None) -> Found:
    found = Found()
    tree = LexborHTMLParser(html)
    t = tree.css_first("title")
    found.title = t.text(strip=True) if t else None

    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        low = href.casefold()
        if low.startswith("mailto:"):
            e = clean_email(href[7:])
            if e:
                found.emails.setdefault(e, "mailto")
        elif low.startswith("tel:"):
            num = _phone(href[4:], region)
            if num:
                found.phones.setdefault(num, "tel")
        elif "linkedin.com/company/" in low and not found.linkedin:
            found.linkedin = href.split("?")[0]
        elif not low.startswith(("javascript:", "#", "whatsapp:", "sms:")):
            url = urljoin(page_url, href).split("#")[0]
            if registrable_domain(url) == site_domain and site_domain:
                kind = classify_link(url, a.text(strip=True))
                if kind:
                    found.links.append((url, kind))

    for el in tree.css("[data-cfemail]"):
        e = clean_email(decode_cfemail(el.attributes.get("data-cfemail") or "") or "")
        if e:
            found.emails.setdefault(e, "cf")
    for a in tree.css('a[href*="/cdn-cgi/l/email-protection#"]'):
        e = clean_email(decode_cfemail((a.attributes.get("href") or "").rsplit("#", 1)[-1]) or "")
        if e:
            found.emails.setdefault(e, "cf")

    found.jsonld = parse_jsonld(tree)
    jf = jsonld_fields(found.jsonld)
    if jf.get("email"):
        e = clean_email(jf["email"].replace("mailto:", ""))
        if e:
            found.emails.setdefault(e, "jsonld")
    if jf.get("telephone"):
        num = _phone(jf["telephone"], region)
        if num:
            found.phones.setdefault(num, "jsonld")

    scripts = len(tree.css("script"))
    for node in tree.css("script, style, noscript, svg"):
        node.decompose()
    body = tree.body
    text = htmllib.unescape(body.text(separator=" ", strip=True)) if body else ""
    found.visible_chars = len(text)
    found.has_scripts_shell = found.visible_chars < 400 and scripts >= 3

    for m in EMAIL_RE.finditer(text):
        e = clean_email(m.group(0))
        if e:
            found.emails.setdefault(e, "text")
    for m in OBFUSCATED_RE.finditer(text):
        dom = re.sub(r"\s*(?:\[|\(|\{)\s*dot\s*(?:\]|\)|\})\s*", ".", m.group(2), flags=re.I)
        e = clean_email(f"{m.group(1)}@{dom}")
        if e:
            found.emails.setdefault(e, "obfuscated")
    if region:
        try:
            for match in phonenumbers.PhoneNumberMatcher(text[:50000], region, leniency=phonenumbers.Leniency.VALID):
                found.phones.setdefault(
                    phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164), "text")
                if len(found.phones) >= 10:
                    break
        except Exception:
            pass
    found.opt_out = bool(OPT_OUT_RE.search(text))
    found.parked = bool(PARKED_RE.search(text[:3000])) and found.visible_chars < 3000
    return found


def _phone(raw: str, region: str | None) -> str | None:
    try:
        n = phonenumbers.parse(unquote(raw).strip(), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(n):
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


def rank_emails(emails: dict[str, str], site_domain: str | None) -> list[str]:
    """Best general address first: own domain, role-based, found in mailto/JSON-LD."""
    def score(e: str) -> tuple:
        dom = e.split("@", 1)[1]
        own = site_domain is not None and (dom == site_domain or dom.endswith("." + site_domain))
        ctx = emails[e]
        return (not own, not is_role(e), ctx not in ("mailto", "jsonld", "cf"), e)
    return sorted(emails, key=score)


def pick_links(links: list[tuple[str, str]], seen: set[str], limit: int) -> list[tuple[str, str]]:
    out, used = [], set(seen)
    for url, kind in sorted(links, key=lambda t: (PAGE_PRIORITY.get(t[1], 9), len(t[0]))):
        key = url.rstrip("/")
        if key in used or re.search(r"\.(pdf|jpe?g|png|zip|docx?)$", key, re.I):
            continue
        used.add(key)
        out.append((url, kind))
        if len(out) >= limit:
            break
    return out
