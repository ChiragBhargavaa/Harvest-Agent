"""OpenStreetMap lookups: Nominatim (city -> bbox) and Overpass (businesses by tag inside the bbox).

Usage policies: identify with a User-Agent, no parallel requests, Nominatim <=1 req/s,
back off 30 s on Overpass 429/504, cache results (24 h here, in-process).
Data © OpenStreetMap contributors, ODbL.
"""

import asyncio
import re
import time

import aiohttp

from crawl import USER_AGENT

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
CACHE_TTL_S = 24 * 3600
HTTP_TIMEOUT_S = 10  # per request; the lead finder has a 65 s budget for the whole step
OVERPASS_TIMEOUT_S = 30  # Overpass is slow under load; 10 s returned nothing live
_cache: dict[str, tuple[float, object]] = {}
_lock = asyncio.Lock()  # one OSM request at a time from this process

# Common business types -> OSM tag filters. The LLM may also return raw "key=value" tags.
OSM_TAGS = {
    "cafe": ["amenity=cafe"], "coffee shop": ["amenity=cafe"], "restaurant": ["amenity=restaurant"],
    "bakery": ["shop=bakery"], "bar": ["amenity=bar", "amenity=pub"], "hotel": ["tourism=hotel"],
    "gym": ["leisure=fitness_centre"], "salon": ["shop=hairdresser", "shop=beauty"], "spa": ["leisure=spa"],
    "supermarket": ["shop=supermarket"], "grocery": ["shop=convenience", "shop=greengrocer"],
    "clothing store": ["shop=clothes"], "boutique": ["shop=boutique", "shop=clothes"],
    "pharmacy": ["amenity=pharmacy"], "clinic": ["amenity=clinic", "amenity=doctors"],
    "dentist": ["amenity=dentist"], "school": ["amenity=school"], "coworking": ["amenity=coworking_space"],
    "office": ["office=company"], "electronics store": ["shop=electronics"], "jewellery": ["shop=jewelry"],
    "furniture store": ["shop=furniture"], "pet shop": ["shop=pet"], "florist": ["shop=florist"],
    "sweet shop": ["shop=confectionery"], "fast food": ["amenity=fast_food"], "ice cream": ["amenity=ice_cream"],
}
TAG_RE = re.compile(r"^[a-z_:]+=[A-Za-z0-9_;:-]+$")


def tags_for(business_types: list[str], extra_tags: list[str] | None = None) -> list[str]:
    tags: list[str] = []
    for t in business_types:
        tags += OSM_TAGS.get(t.strip().lower(), [])
    tags += [t for t in (extra_tags or []) if TAG_RE.match(t)]
    return list(dict.fromkeys(tags))


def _cached(key: str):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL_S else None


async def geocode(city: str, country: str = "India") -> dict:
    """-> {"name", "lat", "lon", "bbox": [south, north, west, east]}"""
    key = f"geo:{city.lower()}:{country.lower()}"
    if (hit := _cached(key)) is not None:
        return hit
    async with _lock, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        params = {"q": f"{city}, {country}", "format": "json", "limit": 1}
        async with s.get(NOMINATIM_URL, params=params, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as r:
            r.raise_for_status()
            data = await r.json()
        await asyncio.sleep(1)  # Nominatim: max 1 request per second
    if not data:
        raise ValueError(f"could not find city '{city}' on OpenStreetMap")
    d = data[0]
    result = {"name": d["display_name"], "lat": float(d["lat"]), "lon": float(d["lon"]),
              "bbox": [float(x) for x in d["boundingbox"]]}
    _cache[key] = (time.time(), result)
    return result


def _overpass_query(tags: list[str], bbox: list[float]) -> str:
    s, n, w, e = bbox
    parts = "".join(f'nwr["{k}"="{v}"]["name"]({s},{w},{n},{e});' for k, v in (t.split("=", 1) for t in tags))
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S - 5}];({parts});out center tags;"


async def find_businesses(tags: list[str], bbox: list[float]) -> list[dict]:
    """-> list of lead dicts (source="osm") with whatever contact tags OSM has."""
    if not tags:
        return []
    query = _overpass_query(tags, bbox)
    if (hit := _cached(query)) is not None:
        return hit
    data, last_err = None, None
    async with _lock, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        for url in OVERPASS_URLS:
            try:
                async with s.post(url, data={"data": query}, timeout=aiohttp.ClientTimeout(total=OVERPASS_TIMEOUT_S)) as r:
                    if r.status in (429, 504):
                        last_err = RuntimeError(f"Overpass busy ({r.status})")
                        await asyncio.sleep(30)
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_err = err
    if data is None:
        raise RuntimeError(f"OpenStreetMap Overpass unavailable: {last_err}")
    leads = [_to_lead(el) for el in data.get("elements", [])]
    leads = [lead for lead in leads if lead]
    _cache[query] = (time.time(), leads)
    return leads


def _to_lead(el: dict) -> dict | None:
    t = el.get("tags", {})
    name = t.get("name:en") or t.get("name")
    if not name:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    addr = t.get("addr:full") or ", ".join(
        x for x in (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb"), t.get("addr:city")) if x
    )
    category = next((f"{k}={t[k]}" for k in ("amenity", "shop", "tourism", "leisure", "office") if k in t), None)
    osm_ref = f"https://www.openstreetmap.org/{el['type']}/{el['id']}"
    return {
        "id": f"osm:{el['type']}/{el['id']}",
        "name": name,
        "category": category,
        "address": addr or None,
        "lat": lat,
        "lon": lon,
        "website": t.get("website") or t.get("contact:website"),
        "osm_emails": [e.strip() for e in (t.get("email") or t.get("contact:email") or "").split(";") if e.strip()],
        "phone": t.get("phone") or t.get("contact:phone"),
        "source": "osm",
        "source_ref": osm_ref,
    }
