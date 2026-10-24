"""Lead Finder agent: `find_leads` - local businesses that match a brand's ideal-customer profile.

Sources (no Google key needed):
  1. OpenStreetMap: Nominatim geocodes the city, Overpass lists businesses by tag (name, address,
     and website/email/phone tags when present).
  2. ASI:One LLM with web search: discovers more businesses and finds official websites for OSM
     businesses that lack one. These are CANDIDATES - a website only counts after we crawl it and
     the business name appears on the page; web-only businesses without a verified site are dropped.

Run:  python agents/lead_finder.py
"""

import asyncio
import logging
import os
import re
import time
from urllib.parse import urlsplit

import geo
import llm
from common import env_seconds, make_specialist, redact
from crawl import PoliteFetcher, is_banned, name_matches, normalise_url

NAME = "leadreach-finder"
log = logging.getLogger(NAME)  # the agent's own logger (uAgents names it after the agent)
MAX_LIMIT = 15
MAX_QUERIES = 2          # ASI:One web-search calls per request (+ at most 1 find_websites call)
MAX_VERIFY = 8           # LLM-suggested websites crawled per request
VERIFY_CONCURRENCY = 8
VERIFY_TIMEOUT_S = 10    # per website fetch
VERIFY_RESERVE_S = 12    # budget kept free for verification after discovery
# Whole step; the orchestrator waits 75 s, then resends once, then runs find_leads in-process (HACKATHON_README §3.6).
FINDER_BUDGET_S = env_seconds("FINDER_BUDGET_S", 65)
# one live ASI:One web-search call takes ~30-60 s; a slower one becomes a note, not a stalled step
WEB_SEARCH_TIMEOUT_S = float(os.getenv("LEADREACH_WEB_SEARCH_TIMEOUT", "90"))
STOP_WORDS = {"the", "and", "cafe", "café", "restaurant", "pvt", "ltd", "private", "limited"}


def _norm_name(name: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def _domain_key(url: str | None) -> str | None:
    """Lowercase host without scheme, "www.", port, path, query or fragment; None if no url."""
    if not url:
        return None
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.removeprefix("www.") or None


def dedupe_leads(leads: list[dict], notes: list[str]) -> list[dict]:
    """Keep the first (best-ranked) lead per website domain or normalised name; merge the rest into it."""
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    for lead in leads:
        keys = [k for k in (f"d:{_domain_key(lead.get('website')) or ''}", f"n:{_norm_name(lead.get('name') or '')}")
                if k[2:]]
        first = next((seen[k] for k in keys if k in seen), None)
        if first is None:
            first = lead
            kept.append(lead)
        else:  # copy only data the duplicate actually has
            if not first.get("website") and lead.get("website"):
                first["website"], first["website_source"] = lead["website"], lead.get("website_source")
            if not first.get("phone") and lead.get("phone"):
                first["phone"] = lead["phone"]
            first["osm_emails"] = list(dict.fromkeys((first.get("osm_emails") or []) + (lead.get("osm_emails") or [])))
        for k in keys:
            seen.setdefault(k, first)
    if merged := len(leads) - len(kept):
        notes.append(f"Merged {merged} duplicate lead(s) (same website domain or name).")
    return kept


def _rank(lead: dict) -> int:
    return (4 if lead.get("osm_emails") else 0) + (2 if lead.get("website") else 0) + (1 if lead.get("phone") else 0)


def _why(err: BaseException, limit: float | None = None) -> str:
    limit = WEB_SEARCH_TIMEOUT_S if limit is None else limit
    return f"timed out after {limit:g}s" if isinstance(err, asyncio.TimeoutError) else redact(err)


def _left(deadline: float, reserve: float = 0.0) -> float:
    return max(0.01, deadline - reserve - time.monotonic())


async def _discover_all(icp: dict, queries: list[str], limit: float) -> list:
    """Run the web-search queries concurrently; each result is a list of businesses or the exception it raised."""
    return await asyncio.gather(*(asyncio.wait_for(llm.discover_businesses(icp, q), limit) for q in queries),
                                return_exceptions=True)


async def _osm(icp: dict, city: str, limit: float, notes: list[str]) -> tuple[dict, list[dict]]:
    """-> (place, OSM leads). Never raises: a failed or slow lookup becomes a note."""
    place = {"name": city, "bbox": None}
    try:
        async def lookup():
            found = await geo.geocode(city)
            tags = geo.tags_for(icp.get("business_types", []), icp.get("osm_tags"))
            return found, tags, await geo.find_businesses(tags, found["bbox"])

        place, tags, osm = await asyncio.wait_for(lookup(), limit)
        notes.append(f"OpenStreetMap: {len(osm)} businesses for {', '.join(tags) or 'no tags'}; "
                     f"{sum(1 for x in osm if x['website'])} list a website.")
        return place, osm
    except Exception as err:  # noqa: BLE001 - partial success: continue with web search
        notes.append(f"OpenStreetMap failed ({_why(err, limit)}); using web search only.")
        return place, []


async def _verify_websites(leads: list[dict], notes: list[str], limit: float) -> int:
    """Crawl up to MAX_VERIFY candidate websites concurrently; keep one only if reachable and the business name appears.
    Candidates not checked within `limit` s stay, marked `verified: false` (shown as unverified). -> number checked."""
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    todo = [lead for lead in leads if lead.get("website") and lead.get("website_source") == "web_candidate"]
    for lead in todo[MAX_VERIFY:]:  # over the cap: not a usable website
        lead["website"], lead["website_source"] = None, None
    todo = todo[:MAX_VERIFY]
    if not todo:
        return 0

    async with PoliteFetcher() as fetcher:
        async def check(lead: dict):
            async with sem:
                try:
                    page = await asyncio.wait_for(fetcher.fetch(lead["website"]), VERIFY_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - unreachable or slow candidate
                    page = None
            if page and page.status < 400 and name_matches(lead["name"], page):
                lead["website"], lead["website_source"], lead["verified"] = page.url, "web_verified", True
            else:
                lead["website"], lead["website_source"] = None, None

        tasks = [asyncio.create_task(check(lead)) for lead in todo]
        _, pending = await asyncio.wait(tasks, timeout=limit)
        for task in pending:  # over budget: keep the candidate, unverified
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    unchecked = [lead for lead, task in zip(todo, tasks, strict=True) if task in pending]
    for lead in unchecked:
        lead["verified"] = False
    verified = sum(1 for lead in todo if lead.get("website_source") == "web_verified")
    notes.append(f"Web search suggested {len(todo)} websites; {verified} verified by crawling"
                 + (f", {len(unchecked)} unverified (time budget)." if unchecked else "."))
    return len(todo) - len(unchecked)


async def find_leads(args: dict) -> dict:
    icp = args.get("icp") or {}
    city = (icp.get("city") or args.get("city") or "").strip()
    if not city:
        raise ValueError("missing city")
    if not icp.get("business_types") and not icp.get("osm_tags"):
        raise ValueError("missing business_types in icp")
    limit = max(1, min(int(args.get("limit") or MAX_LIMIT), MAX_LIMIT))
    exclude = set(args.get("exclude_ids") or [])
    notes: list[str] = []
    by_name: dict[str, dict] = {}
    start = time.monotonic()
    deadline = start + FINDER_BUDGET_S

    # 1+2. OpenStreetMap and ASI:One web search run concurrently, leaving VERIFY_RESERVE_S for verification
    queries = (icp.get("search_queries") or [])[:MAX_QUERIES]
    discover_limit = _left(deadline, VERIFY_RESERVE_S)
    web_limit = min(WEB_SEARCH_TIMEOUT_S, discover_limit)
    t0 = time.monotonic()
    (place, osm), web = await asyncio.gather(_osm(icp, city, discover_limit, notes), _discover_all(icp, queries, web_limit))
    log.info(f"find_leads osm: {len(osm)} businesses ({time.monotonic() - t0:.1f}s)")
    log.info(f"find_leads web_search: {sum(len(x) for x in web if not isinstance(x, BaseException))} businesses "
             f"from {len(queries)} queries, {sum(isinstance(x, BaseException) for x in web)} failed "
             f"({time.monotonic() - t0:.1f}s)")
    banned_osm = 0
    for lead in osm:
        url = normalise_url(lead.get("website"))
        if url and is_banned(url):  # e.g. a zomato.com page in the OSM website tag: treat as no website
            url, banned_osm = None, banned_osm + 1
        lead["website"], lead["website_source"] = url, "osm" if url else None
        by_name.setdefault(_norm_name(lead["name"]), lead)
    if banned_osm:
        notes.append(f"Ignored {banned_osm} OpenStreetMap website tag(s) pointing at directories or social media.")

    # 2. ASI:One web search discovery (candidates); queries run concurrently, merged in query order
    web_found = 0
    for query, found in zip(queries, web, strict=True):
        if isinstance(found, BaseException):
            notes.append(f"Web search failed for '{query}': {_why(found, web_limit)}")
            continue
        for b in found:
            key = _norm_name(b["name"])
            url = normalise_url(b.get("website"))
            url = None if url and is_banned(url) else url
            if key in by_name:
                existing = by_name[key]
                if url and not existing.get("website"):
                    existing["website"], existing["website_source"] = url, "web_candidate"
                continue
            web_found += 1
            by_name[key] = {
                "id": f"web:{_slug(b['name'])}", "name": b["name"], "category": None,
                "address": b.get("area"), "lat": None, "lon": None,
                "website": url, "website_source": "web_candidate" if url else None,
                "osm_emails": [], "phone": None, "source": "web", "source_ref": b.get("evidence_url"),
            }
    if web_found:
        notes.append(f"Web search: {web_found} extra businesses not in OpenStreetMap.")

    leads = [lead for lead in by_name.values() if lead["id"] not in exclude]
    leads.sort(key=_rank, reverse=True)
    shortlist = leads[: limit * 2]

    # 3. Find official websites for promising leads that have none (batch web search)
    missing = [lead for lead in shortlist if not lead.get("website")][:10]
    lookup_left = _left(deadline, VERIFY_RESERVE_S)
    if missing and lookup_left > 1:  # one batch call, only while the budget allows
        lookup_limit = min(WEB_SEARCH_TIMEOUT_S, lookup_left)
        try:
            found = await asyncio.wait_for(llm.find_websites(missing, city), lookup_limit)
            for lead in missing:
                url = normalise_url(found.get(lead["id"]))
                if url and not is_banned(url):
                    lead["website"], lead["website_source"] = url, "web_candidate"
        except Exception as err:  # noqa: BLE001
            notes.append(f"Website lookup failed: {_why(err, lookup_limit)}")
    elif missing:
        notes.append("Website lookup skipped (time budget).")

    # 4. Verify LLM-suggested websites; web-only businesses need a verified (or, over budget, unverified) site
    t0 = time.monotonic()
    checked = await _verify_websites(shortlist, notes, _left(deadline))
    log.info(f"find_leads verify: {checked} websites checked ({time.monotonic() - t0:.1f}s)")
    shortlist = [lead for lead in shortlist if lead["source"] == "osm" or lead.get("website")]
    shortlist.sort(key=_rank, reverse=True)
    shortlist = dedupe_leads(shortlist, notes)

    picked = [lead for lead in shortlist if lead.get("website") or lead.get("osm_emails")][:limit]
    call_list = [lead for lead in shortlist if lead not in picked and lead.get("phone")][:limit]
    if len(picked) < limit:
        notes.append(f"Only {len(picked)} leads have a website or email; {len(call_list)} more have just a phone.")
    log.info(f"find_leads done: {len(picked)} leads, {len(call_list)} phone-only ({time.monotonic() - start:.1f}s)")
    if not picked and not call_list:
        raise RuntimeError("no leads found: " + " ".join(notes)[:300])
    return {"leads": picked, "call_list": call_list, "source_notes": notes, "city": place["name"]}


agent = make_specialist(
    name=NAME,
    seed_env="FINDER_SEED",
    port_env="FINDER_PORT",
    default_port=8002,
    profile="lead_finder.md",
    description="Finds local businesses in a city that match a brand's ideal customer (OpenStreetMap + ASI:One web search).",
    tools={"find_leads": find_leads},
)

if __name__ == "__main__":
    agent.run()
