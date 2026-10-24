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
import os
import re

import geo
import llm
from common import make_specialist
from crawl import PoliteFetcher, is_banned, name_matches, normalise_url

MAX_LIMIT = 15
VERIFY_CONCURRENCY = 5
MAX_QUERIES = 3
# one live ASI:One web-search call takes ~30-60 s; a slower one becomes a note, not a stalled step
WEB_SEARCH_TIMEOUT_S = float(os.getenv("LEADREACH_WEB_SEARCH_TIMEOUT", "90"))
STOP_WORDS = {"the", "and", "cafe", "café", "restaurant", "pvt", "ltd", "private", "limited"}


def _norm_name(name: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def _rank(lead: dict) -> int:
    return (4 if lead.get("osm_emails") else 0) + (2 if lead.get("website") else 0) + (1 if lead.get("phone") else 0)


def _why(err: BaseException) -> str:
    return f"timed out after {WEB_SEARCH_TIMEOUT_S:g}s" if isinstance(err, asyncio.TimeoutError) else str(err)


async def _discover_all(icp: dict, queries: list[str]) -> list:
    """Run the web-search queries concurrently; each result is a list of businesses or the exception it raised."""
    return await asyncio.gather(
        *(asyncio.wait_for(llm.discover_businesses(icp, q), WEB_SEARCH_TIMEOUT_S) for q in queries),
        return_exceptions=True)


async def _verify_websites(leads: list[dict], notes: list[str]) -> None:
    """Crawl each unverified website once; keep it only if reachable and the business name appears."""
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    todo = [lead for lead in leads if lead.get("website") and lead.get("website_source") == "web_candidate"]

    async with PoliteFetcher() as fetcher:
        async def check(lead: dict):
            async with sem:
                try:
                    page = await fetcher.fetch(lead["website"])
                except Exception:  # noqa: BLE001 - unreachable candidate
                    page = None
            if page and page.status < 400 and name_matches(lead["name"], page):
                lead["website"], lead["website_source"] = page.url, "web_verified"
            else:
                lead["website"], lead["website_source"] = None, None

        await asyncio.gather(*(check(lead) for lead in todo))
    rejected = sum(1 for lead in todo if not lead["website"])
    if todo:
        notes.append(f"Web search suggested {len(todo)} websites; {len(todo) - rejected} verified by crawling.")


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

    # 1. OpenStreetMap
    place = await geo.geocode(city)
    tags = geo.tags_for(icp.get("business_types", []), icp.get("osm_tags"))
    try:
        osm = await geo.find_businesses(tags, place["bbox"])
        notes.append(f"OpenStreetMap: {len(osm)} businesses for {', '.join(tags) or 'no tags'}; "
                     f"{sum(1 for x in osm if x['website'])} list a website.")
    except Exception as err:  # noqa: BLE001 - partial success: continue with web search
        osm = []
        notes.append(f"OpenStreetMap failed ({err}); using web search only.")
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
    queries = (icp.get("search_queries") or [])[:MAX_QUERIES]
    for query, found in zip(queries, await _discover_all(icp, queries), strict=True):
        if isinstance(found, BaseException):
            notes.append(f"Web search failed for '{query}': {_why(found)}")
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
    if missing:
        try:
            found = await asyncio.wait_for(llm.find_websites(missing, city), WEB_SEARCH_TIMEOUT_S)
            for lead in missing:
                url = normalise_url(found.get(lead["id"]))
                if url and not is_banned(url):
                    lead["website"], lead["website_source"] = url, "web_candidate"
        except Exception as err:  # noqa: BLE001
            notes.append(f"Website lookup failed: {_why(err)}")

    # 4. Verify every LLM-suggested website; web-only businesses need a verified site to stay
    await _verify_websites(shortlist, notes)
    shortlist = [lead for lead in shortlist if lead["source"] == "osm" or lead.get("website")]
    shortlist.sort(key=_rank, reverse=True)

    picked = [lead for lead in shortlist if lead.get("website") or lead.get("osm_emails")][:limit]
    call_list = [lead for lead in shortlist if lead not in picked and lead.get("phone")][:limit]
    if len(picked) < limit:
        notes.append(f"Only {len(picked)} leads have a website or email; {len(call_list)} more have just a phone.")
    return {"leads": picked, "call_list": call_list, "source_notes": notes, "city": place["name"]}


agent = make_specialist(
    name="leadreach-finder",
    seed_env="FINDER_SEED",
    port_env="FINDER_PORT",
    default_port=8002,
    profile="lead_finder.md",
    description="Finds local businesses in a city that match a brand's ideal customer (OpenStreetMap + ASI:One web search).",
    tools={"find_leads": find_leads},
)

if __name__ == "__main__":
    agent.run()
