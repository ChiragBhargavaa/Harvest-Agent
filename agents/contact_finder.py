"""Contact Finder agent: `find_contacts` - published emails/phones for each lead (polite crawl + MX check).

Pipeline per lead: crawl.PoliteFetcher.crawl_site -> crawl.extract_emails / extract_phones -> crawl.mx_ok.
Emails come only from crawled pages or OSM tags, never from the LLM, and patterns are never guessed.

Confidence (HACKATHON_README §3.8):
  high   = owner/manager/sales/orders address on the business's own domain, published on its own site;
  medium = generic address (info@, hello@, ...) on its own domain, or a free-mail address published on its own site;
  low    = found only on a third-party page (incl. OSM tags), or an address on someone else's domain
           (e.g. the web designer's) that appears on the business's site.
Emails whose domain has no MX/A record are dropped. Leads with no usable email go to `skipped` with a reason.

Run:  python agents/contact_finder.py
"""

import asyncio
import re

from common import make_specialist
from crawl import (
    EMAIL_RE,
    FAKE_EMAIL_HINTS,
    FREEMAIL,
    PoliteFetcher,
    domain_of,
    extract_emails,
    extract_phones,
    guess_role,
    is_banned,
    mx_ok,
    normalise_url,
)

CRAWL_CONCURRENCY = 5
MAX_PAGES = 5
# crawl.guess_role -> Contact.role (§3.8)
ROLE_MAP = {"founder": "owner", "booking": "orders", "hello": "info", "support": "contact"}
HIGH_ROLES = {"owner", "manager", "sales", "orders"}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
ROLE_RANK = {"owner": 0, "manager": 0, "sales": 1, "orders": 1, "info": 2, "contact": 2}


def _clean_email(email: str) -> str | None:
    email = email.strip().removeprefix("mailto:").split("?")[0].strip(".,;:").lower()
    if not EMAIL_RE.fullmatch(email) or any(h in email for h in FAKE_EMAIL_HINTS):
        return None
    return email


def _same_site(domain: str, own: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) or d.endswith("." + domain) for d in own)


def classify(email: str, on_own_site: bool, own_domains: set[str]) -> tuple[str, str]:
    """-> (role, confidence) for an email found on a page (own site or not)."""
    role = guess_role(email)
    role = ROLE_MAP.get(role, role)
    domain = email.split("@")[1]
    if not on_own_site:
        return role, "low"
    if _same_site(domain, own_domains):
        return role, "high" if role in HIGH_ROLES else "medium"
    if domain in FREEMAIL:
        return role, "medium"
    return role, "low"


async def _crawl(fetcher, url: str, max_pages: int) -> tuple[list, str | None]:
    """-> (pages, skip reason). Never raises."""
    try:
        if not await fetcher.allowed(url):
            return [], "robots_disallowed"
        pages = await fetcher.crawl_site(url, max_pages=max_pages)
    except Exception:  # noqa: BLE001 - network errors: record and move on
        return [], "unreachable"
    if pages and is_banned(pages[0].url):  # homepage redirected to a banned host
        return [], "banned_host"
    pages = [p for p in pages if p.status < 400 and not is_banned(p.url)]
    return pages, None if pages else "unreachable"


async def _contacts_for(lead: dict, fetcher, sem: asyncio.Semaphore, mx_cache: dict, max_pages: int) -> tuple[list, str | None]:
    """-> (contacts, skip reason if none). Never raises."""
    website = normalise_url(lead.get("website"))
    found: list[tuple[str, str, bool]] = []  # (email, source_url, on_own_site)
    phones: set[str] = set()
    own: set[str] = set()
    reason = None
    if not website:
        reason = "no_website"
    elif is_banned(website):
        reason = "banned_host"
    else:
        own.add(domain_of(website))
        async with sem:
            pages, reason = await _crawl(fetcher, website, max_pages)
        if pages:
            own.add(domain_of(pages[0].url))  # final homepage URL after redirects
        for page in pages:
            on_own = _same_site(domain_of(page.url), own)
            found += [(e, page.url, on_own) for e in extract_emails(page)]
            phones.update(extract_phones(page))
    phones.update(re.sub(r"[\s-]", "", p) for p in (lead.get("phone") or "").split(";") if p.strip())

    best: dict[str, dict] = {}
    for email, url, on_own in found:
        role, confidence = classify(email, on_own, own)
        contact = {"lead_id": lead["id"], "email": email, "role": role, "person_name": None, "source": "website",
                   "source_url": url, "confidence": confidence, "mx_ok": True}
        prev = best.get(email)
        if prev is None or CONFIDENCE_RANK[confidence] < CONFIDENCE_RANK[prev["confidence"]]:
            best[email] = contact
    for raw in lead.get("osm_emails") or []:
        email = _clean_email(raw)
        if email and email not in best:  # OSM is a third-party source: low unless also on the site
            role = guess_role(email)
            best[email] = {"lead_id": lead["id"], "email": email, "role": ROLE_MAP.get(role, role),
                           "person_name": None, "source": "osm", "source_url": lead.get("source_ref"),
                           "confidence": "low", "mx_ok": True}
    if not best:
        return [], reason or "no_email_found"

    for domain in {e.split("@")[1] for e in best}:
        if domain not in mx_cache:  # one blocking DNS lookup per domain per call, off the event loop
            mx_cache[domain] = asyncio.ensure_future(asyncio.to_thread(mx_ok, domain))
    contacts = []
    for email, contact in best.items():
        try:
            ok = await mx_cache[email.split("@")[1]]
        except Exception:  # noqa: BLE001 - DNS failure counts as no MX
            ok = False
        if ok:
            contacts.append(dict(contact, phones=sorted(phones)))
    if not contacts:
        return [], "mx_failed"
    contacts.sort(key=lambda c: (CONFIDENCE_RANK[c["confidence"]], ROLE_RANK.get(c["role"], 3), c["email"]))
    return contacts, None


async def find_contacts(args: dict) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    max_pages = max(1, min(int(args.get("max_pages_per_site") or MAX_PAGES), MAX_PAGES))
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)
    mx_cache: dict = {}

    async with PoliteFetcher() as fetcher:  # one fetcher: shared robots cache + per-domain rate limit
        async def one(lead: dict) -> tuple[list, str | None]:
            try:
                return await _contacts_for(lead, fetcher, sem, mx_cache, max_pages)
            except Exception:  # noqa: BLE001 - a per-lead failure never fails the whole step
                return [], "unreachable"

        results = await asyncio.gather(*(one(lead) for lead in leads))

    contacts, skipped = [], []
    for lead, (found, reason) in zip(leads, results, strict=True):
        contacts += found
        if reason and not found:
            skipped.append({"lead_id": lead["id"], "reason": reason})
    return {"contacts": contacts, "skipped": skipped}


agent = make_specialist(
    name="leadreach-contacts",
    seed_env="CONTACT_SEED",
    port_env="CONTACT_PORT",
    default_port=8003,
    profile="contact_finder.md",
    description="Finds the published contact emails and phone numbers of local businesses by politely crawling their websites.",
    tools={"find_contacts": find_contacts},
)

if __name__ == "__main__":
    agent.run()
