"""Go/no-go probe for the lead sources: OpenStreetMap counts + ASI:One web-search quality.

    .venv/bin/python scripts/probe_sources.py [city] [business type]
    .venv/bin/python scripts/probe_sources.py Jaipur cafe

Needs ASI1_API_KEY in .env for the web-search part (OSM works without it).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

import geo  # noqa: E402
import llm  # noqa: E402
from crawl import PoliteFetcher, is_banned, name_matches, normalise_url  # noqa: E402

CITY = sys.argv[1] if len(sys.argv) > 1 else "Jaipur"
TYPE = sys.argv[2] if len(sys.argv) > 2 else "cafe"


async def main():
    place = await geo.geocode(CITY)
    tags = geo.tags_for([TYPE])
    osm = await geo.find_businesses(tags, place["bbox"])
    print(f"OSM {CITY} {tags}: {len(osm)} businesses | website {sum(1 for x in osm if x['website'])} | "
          f"email {sum(1 for x in osm if x['osm_emails'])} | phone {sum(1 for x in osm if x['phone'])}")

    if not os.getenv("ASI1_API_KEY"):
        print("ASI1_API_KEY not set - skipping the web-search probe.")
        return
    icp = {"city": CITY, "business_types": [TYPE]}
    queries = [f"best {TYPE}s in {CITY}", f"specialty {TYPE} {CITY} official website", f"new {TYPE}s opened in {CITY}"]
    total = with_url = verified = 0
    async with PoliteFetcher() as fetcher:
        for q in queries:
            try:
                found = await llm.discover_businesses(icp, q)
            except Exception as err:  # noqa: BLE001
                print(f"  '{q}': web search failed: {err}")
                continue
            print(f"  '{q}': {len(found)} businesses")
            for b in found:
                total += 1
                url = normalise_url(b.get("website"))
                if not url or is_banned(url):
                    continue
                with_url += 1
                try:
                    page = await fetcher.fetch(url)
                except Exception:  # noqa: BLE001
                    page = None
                ok = bool(page and page.status < 400 and name_matches(b["name"], page))
                verified += ok
                print(f"    {'OK ' if ok else 'BAD'} {b['name'][:40]:40} {url}")
    print(f"\nWeb search: {total} businesses, {with_url} with a URL, {verified} verified by crawling.")
    print("GO" if verified >= 3 else "NO-GO: rely on OSM websites only (and say so in the demo).")


if __name__ == "__main__":
    asyncio.run(main())
