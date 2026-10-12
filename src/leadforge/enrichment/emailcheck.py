"""Email checks without SMTP probing: syntax + the domain's mail servers (MX, falling back to A).

Decision (12 Oct 2026): `Valid` means the address is well-formed and its domain accepts mail
(MX ok). There is no inbox-level check, so `Valid` does not prove the mailbox exists.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3

import dns.asyncresolver
import dns.exception
import dns.resolver

VALID, INVALID = "Valid", "Invalid / Bounced"
CACHE_DAYS = 30


async def _lookup(domain: str, resolver: dns.asyncresolver.Resolver) -> tuple[bool, str]:
    try:
        ans = await resolver.resolve(domain, "MX")
        hosts = sorted(str(r.exchange).rstrip(".") for r in ans)
        # A "null MX" (RFC 7505) explicitly says the domain takes no mail.
        return (bool(hosts) and hosts != [""], ",".join(hosts))
    except dns.resolver.NoAnswer:
        try:
            await resolver.resolve(domain, "A")    # implicit MX: mail goes to the A record
            return True, "(A record)"
        except dns.exception.DNSException:
            return False, ""
    except dns.resolver.NXDOMAIN:
        return False, "NXDOMAIN"
    except dns.exception.DNSException as exc:
        raise LookupError(str(exc)) from exc       # timeouts etc.: unknown, not "invalid"


async def check_domains(domains: set[str], conn: sqlite3.Connection, concurrency: int = 20) -> dict[str, bool | None]:
    """{domain: True/False, or None when DNS could not answer}. Uses and fills mx_cache."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CACHE_DAYS)).isoformat()
    out: dict[str, bool | None] = {}
    todo = []
    for d in domains:
        row = conn.execute("SELECT mx_ok FROM mx_cache WHERE domain=? AND checked_at > ?", (d, cutoff)).fetchone()
        if row:
            out[d] = bool(row[0])
        else:
            todo.append(d)
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5.0
    sem = asyncio.Semaphore(concurrency)

    async def one(d: str):
        async with sem:
            try:
                ok, hosts = await _lookup(d, resolver)
            except LookupError:
                return d, None, ""
            return d, ok, hosts

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for d, ok, hosts in await asyncio.gather(*(one(d) for d in todo)):
        out[d] = ok
        if ok is not None:
            conn.execute("INSERT OR REPLACE INTO mx_cache VALUES (?,?,?,?)", (d, int(ok), hosts, now))
    conn.commit()
    return out


def status_for(mx_ok: bool | None) -> str:
    if mx_ok is None:
        return "Not checked"
    return VALID if mx_ok else INVALID
