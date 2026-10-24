"""leadreach-finder - Agentverse HOSTED port of LeadReach / Harvest Agent agents/lead_finder.py.

Single self-contained file. Uses the pre-provided global `agent` (do not construct Agent or call run()).
Secrets (Agentverse -> agent -> Secrets): ASI1_API_KEY (required); optional FINDER_BUDGET_S, LEADREACH_WEB_SEARCH_TIMEOUT, REPLY_TO, ASI1_MODEL
Protocol: leadreach-tools 0.1.0, ToolRequest -> ToolResponse (identical to the local agents, same digest).
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlparse, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp
from bs4 import BeautifulSoup
from uagents import Context, Model, Protocol

try:
    import lxml  # noqa: F401
    _BS_PARSER = "lxml"
except ImportError:  # pragma: no cover - stdlib parser fallback
    _BS_PARSER = "html.parser"
try:
    import dns.asyncresolver as _dns_async
except ImportError:  # pragma: no cover
    _dns_async = None



# ======================= hosted runtime glue (replaces common.py) =======================
# Agentverse hosted: the global `agent` is pre-provided. No Agent(...), no agent.run(), no .env, no local files.

class ToolRequest(Model):
    tool: str              # "find_leads" | "find_contacts" | "qualify_leads" | "draft_emails" | "send_emails"
    args_json: str = "{}"  # JSON-encoded arguments (keeps the schema stable)


class ToolResponse(Model):
    ok: bool
    result: str = ""       # JSON matching the tool's result schema
    error: str = ""


def env_bool(name: str, default: bool = True) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_seconds(name: str, default: float) -> float:
    """A positive number of seconds from the environment; missing, invalid or <= 0 -> default."""
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def redact(text: str) -> str:
    """Strip API keys / bearer tokens from error text before it is logged or stored."""
    text = str(text)
    for name in ("ASI1_API_KEY", "RESEND_API_KEY", "FOURSQUARE_API_KEY", "GOOGLE_PLACES_API_KEY"):
        secret = os.environ.get(name, "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return re.sub(r"(?i)\b(sk-|bearer\s+)[\w.-]{6,}", r"\1***", text)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set - add it as an Agentverse agent secret")
    return value


async def with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Retry an async call with exponential backoff."""
    last_err = None
    for i in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 - we re-raise the last one
            last_err = err
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** i))
    raise last_err


REQUEST_ID_KEY = "_request_id"  # in args_json: one uuid4 hex per logical step; a resend reuses it (tools never see it)
MAX_REMEMBERED_REQUESTS = 100
STORED_REPLIES = 10            # last replies also kept in ctx.storage (survive a hosted restart)
STORE_KEY = "leadreach_replies"


def tool_runner(tools):
    """-> answer(req, logger, storage) -> ToolResponse. Never raises. A request whose `_request_id` was seen before
    awaits the in-flight run (or returns its cached reply) instead of redoing the work."""
    runs = {}

    async def run(req, args, logger):
        fn = tools.get(req.tool)
        if fn is None:
            return ToolResponse(ok=False, error=f"unknown tool '{req.tool}'")
        start = time.monotonic()
        try:
            result = await fn(args)
            logger.info(f"{req.tool} done in {time.monotonic() - start:.1f}s")
            return ToolResponse(ok=True, result=json.dumps(result))
        except NotImplementedError as err:
            logger.warning(str(err))
            return ToolResponse(ok=False, error=str(err))
        except Exception as err:  # noqa: BLE001 - report every failure back to the orchestrator
            error = redact(f"{type(err).__name__}: {err}")[:400]
            logger.error(f"{req.tool} failed in {time.monotonic() - start:.1f}s: {error}")
            return ToolResponse(ok=False, error=error)

    async def answer(req, logger, storage=None):
        try:
            args = json.loads(req.args_json or "{}")
        except ValueError as err:
            return ToolResponse(ok=False, error=f"invalid args_json: {err}")
        rid = str(args.pop(REQUEST_ID_KEY, "") or "") if isinstance(args, dict) else ""
        if rid and storage is not None and rid not in runs:
            try:
                stored = (storage.get(STORE_KEY) or {}).get(rid)
            except Exception:  # noqa: BLE001
                stored = None
            if stored:
                logger.info(f"dedupe hit (storage) {rid[:8]}")
                return ToolResponse(**stored)
        task = runs.get(rid) if rid else None
        if task is not None:
            logger.info(f"dedupe hit {rid[:8]}")
        else:
            task = asyncio.ensure_future(run(req, args, logger))
            if rid:
                runs[rid] = task
                while len(runs) > MAX_REMEMBERED_REQUESTS:
                    runs.pop(next(iter(runs)))
        reply = await asyncio.shield(task)  # a cancelled waiter never cancels the shared run
        if rid and storage is not None:
            try:
                saved = storage.get(STORE_KEY) or {}
                saved[rid] = {"ok": reply.ok, "result": reply.result, "error": reply.error}
                while len(saved) > STORED_REPLIES:
                    saved.pop(next(iter(saved)))
                storage.set(STORE_KEY, saved)
            except Exception as err:  # noqa: BLE001 - storage is best effort
                logger.warning(f"could not store reply: {type(err).__name__}")
        return reply

    return answer


def register_specialist(*, name: str, description: str, tools: dict):
    """Attach the LeadReach tool protocol to the pre-provided hosted `agent` (same name/version/models as local)."""
    proto = Protocol(name="leadreach-tools", version="0.1.0")
    answer = tool_runner(tools)

    @proto.on_message(ToolRequest, replies=ToolResponse)
    async def handle_request(ctx: Context, sender: str, req: ToolRequest):
        ctx.logger.info(f"{req.tool} request from {sender[:16]}...")
        try:
            reply = await answer(req, ctx.logger, ctx.storage)
        except Exception as err:  # noqa: BLE001 - e.g. the shared run was cancelled
            reply = ToolResponse(ok=False, error=redact(f"{type(err).__name__}: {err}")[:400])
        await ctx.send(sender, reply)

    agent.include(proto, publish_manifest=True)

    @agent.on_event("startup")
    async def show_address(ctx: Context):
        ctx.logger.info(f"{name} ({description[:60]}) address: {agent.address}")

    return proto


# ======================= crawl.py =======================
# Nominatim rejects placeholder contacts (e.g. example.com) with 403, so only add a real one
USER_AGENT = f"LeadReachBot/0.1 (+mailto:{os.environ.get('REPLY_TO', '')})" if os.getenv("REPLY_TO") else "LeadReachBot/0.1"
TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_BYTES = 2 * 1024 * 1024
MIN_INTERVAL_S = 1.0

# Never fetched automatically (ToS / robots / personal-data reasons)
BANNED_HOSTS = (
    "google.", "goo.gl", "justdial.com", "indiamart.com", "linkedin.com", "facebook.com",
    "instagram.com", "x.com", "twitter.com", "zomato.com", "swiggy.com", "sulekha.com",
)
CONTACT_LINK_RE = re.compile(r"contact|about|team|reach|enquir|inquir|connect|location|store", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?(?:0?[6-9]\d{4}[\s-]?\d{5}|0\d{2,4}[\s-]?\d{6,8})")
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
FAKE_EMAIL_HINTS = ("example.", "yourdomain", "domain.com", "email.com", "sentry", "wixpress")
FREEMAIL = ("gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "rediffmail.com", "icloud.com")


def is_banned(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(b in host for b in BANNED_HOSTS)


def normalise_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    return url if parsed.hostname and "." in parsed.hostname else None


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class Page:
    url: str
    status: int
    html: str
    load_ms: int
    text: str = ""
    links: list[str] = field(default_factory=list)


class PoliteFetcher:
    """One instance per tool call: caches robots.txt and rate-limits per domain."""

    def __init__(self):
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _wait_turn(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = time.monotonic() - self._last.get(host, 0)
            if gap < MIN_INTERVAL_S:
                await asyncio.sleep(MIN_INTERVAL_S - gap)
            self._last[host] = time.monotonic()

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                await self._wait_turn(parsed.netloc)
                async with self._session.get(base + "/robots.txt", allow_redirects=True) as resp:
                    if resp.status == 200:
                        rp.parse((await resp.text(errors="ignore")).splitlines())
                    else:
                        rp = None  # no robots.txt -> allowed
            except Exception:  # noqa: BLE001 - unreachable robots.txt -> treat as allowed, fetch decides
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> Page | None:
        """Fetch one HTML page. Returns None if banned, disallowed, non-HTML or too big. Raises on network errors."""
        if is_banned(url) or not await self.allowed(url):
            return None
        await self._wait_turn(urlparse(url).netloc)
        start = time.perf_counter()
        async with self._session.get(url, allow_redirects=True) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower() or int(resp.headers.get("Content-Length") or 0) > MAX_BYTES:
                return None
            raw = await resp.content.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            html = raw.decode(resp.charset or "utf-8", errors="ignore")
            page = Page(str(resp.url), resp.status, html, int((time.perf_counter() - start) * 1000))
        soup = BeautifulSoup(html, _BS_PARSER)
        page.links = [urljoin(page.url, a["href"]) for a in soup.find_all("a", href=True)]
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        page.text = " ".join(soup.get_text(" ").split())
        return page

    async def crawl_site(self, url: str, max_pages: int = 5) -> list[Page]:
        """Homepage plus same-domain contact/about-style pages. Skips pages that fail."""
        home = await self.fetch(url)
        if home is None:
            return []
        pages, seen = [home], {home.url}
        site = domain_of(home.url)
        for link in home.links:
            if len(pages) >= max_pages:
                break
            if link in seen or domain_of(link) != site or not CONTACT_LINK_RE.search(link):
                continue
            seen.add(link)
            try:
                page = await self.fetch(link)
            except Exception:  # noqa: BLE001 - partial success is fine
                continue
            if page:
                pages.append(page)
        return pages


# ---------- extraction ----------

def _deobfuscate(text: str) -> str:
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.I)
    return text


def extract_emails(page: Page) -> list[str]:
    found = set()
    for link in page.links:
        if link.lower().startswith("mailto:"):
            found.add(link[7:].split("?")[0])
    found.update(EMAIL_RE.findall(_deobfuscate(page.text)))
    clean = []
    for e in found:
        e = e.strip().strip(".,;:").lower()
        if e.endswith(ASSET_SUFFIXES) or any(h in e for h in FAKE_EMAIL_HINTS) or not EMAIL_RE.fullmatch(e):
            continue
        clean.append(e)
    return sorted(set(clean))


def extract_phones(page: Page) -> list[str]:
    phones = {link[4:] for link in page.links if link.lower().startswith("tel:")}
    phones.update(m.strip() for m in PHONE_RE.findall(page.text))
    return sorted({re.sub(r"[\s-]", "", p) for p in phones if len(re.sub(r"\D", "", p)) >= 10})


def guess_role(email: str) -> str:
    local = email.split("@")[0]
    for role in ("owner", "founder", "manager", "sales", "orders", "booking", "info", "contact", "hello", "support"):
        if role in local:
            return role
    return "unknown"


async def mx_ok(domain: str) -> bool:
    """Hosted port: async (no threads). MX, then A record; with dnspython missing, a plain address lookup."""
    if _dns_async is not None:
        for rtype in ("MX", "A"):
            try:
                if await _dns_async.resolve(domain, rtype, lifetime=5):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    try:
        return bool(await asyncio.get_running_loop().getaddrinfo(domain, None))
    except Exception:  # noqa: BLE001
        return False


def name_matches(name: str, page: Page) -> bool:
    """Does the business name plausibly appear on the page (used to verify LLM-suggested websites)?"""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 and w not in {"the", "and", "cafe", "shop"}]
    if not words:
        return False
    haystack = (page.text[:20000] + " " + page.url).lower()
    hits = sum(1 for w in words if w in haystack)
    return hits >= max(1, (len(words) + 1) // 2)


# ======================= geo.py =======================
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter")
GEO_CACHE_TTL_S = 24 * 3600
GEO_HTTP_TIMEOUT_S = 10  # per request; the lead finder has a 65 s budget for the whole step
OVERPASS_TIMEOUT_S = 30  # Overpass is slow under load; 10 s returned nothing live
_geo_cache: dict[str, tuple[float, object]] = {}
_geo_lock = asyncio.Lock()  # one OSM request at a time from this process

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


def _geo_cached(key: str):
    hit = _geo_cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < GEO_CACHE_TTL_S else None


async def geocode(city: str, country: str = "India") -> dict:
    """-> {"name", "lat", "lon", "bbox": [south, north, west, east]}"""
    key = f"geo:{city.lower()}:{country.lower()}"
    if (hit := _geo_cached(key)) is not None:
        return hit
    async with _geo_lock, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        params = {"q": f"{city}, {country}", "format": "json", "limit": 1}
        async with s.get(NOMINATIM_URL, params=params, timeout=aiohttp.ClientTimeout(total=GEO_HTTP_TIMEOUT_S)) as r:
            r.raise_for_status()
            data = await r.json()
        await asyncio.sleep(1)  # Nominatim: max 1 request per second
    if not data:
        raise ValueError(f"could not find city '{city}' on OpenStreetMap")
    d = data[0]
    result = {"name": d["display_name"], "lat": float(d["lat"]), "lon": float(d["lon"]),
              "bbox": [float(x) for x in d["boundingbox"]]}
    _geo_cache[key] = (time.time(), result)
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
    if (hit := _geo_cached(query)) is not None:
        return hit
    data, last_err = None, None
    async with _geo_lock, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
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
    _geo_cache[query] = (time.time(), leads)
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


# ======================= llm.py (ASI:One) =======================
ASI1_BASE_URL = "https://api.asi1.ai/v1"
ASI1_MODEL = os.environ.get("ASI1_MODEL", "asi1")
# per HTTP request (hosted port: plain aiohttp instead of the openai SDK; same 20 s / 40 s limits, 1 retry)
ASI1_LLM_TIMEOUT_S = env_seconds("LEADREACH_LLM_TIMEOUT", 20)
WEB_SEARCH_LLM_TIMEOUT_S = env_seconds("LEADREACH_WEB_SEARCH_LLM_TIMEOUT", 40)  # live web search takes 20-40 s
LLM_MAX_RETRIES = 1


async def complete(messages: list[dict], max_tokens: int = 1024, temperature: float = 0.2,
                   web_search: bool = False) -> str:
    # ASI:One opts into search by the *presence* of web_search_options (only search_context_size is allowed).
    body = {"model": ASI1_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    timeout_s = ASI1_LLM_TIMEOUT_S
    if web_search and env_bool("ASI1_WEB_SEARCH", True):
        body["web_search_options"] = {"search_context_size": os.environ.get("ASI1_SEARCH_CONTEXT", "medium")}
        timeout_s = WEB_SEARCH_LLM_TIMEOUT_S

    async def _call():
        headers = {"Authorization": f"Bearer {require_env('ASI1_API_KEY')}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as s:
            async with s.post(f"{ASI1_BASE_URL}/chat/completions", json=body, headers=headers) as r:
                if r.status >= 400:
                    detail = (await r.text())[:200]
                    raise RuntimeError(f"ASI:One HTTP {r.status}: {redact(detail)}")
                data = await r.json(content_type=None)
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    # same as the SDK's timeout + max_retries=1 (one HTTP retry), wrapped in with_retry like the local code
    async def _call_sdk_like():
        for i in range(LLM_MAX_RETRIES + 1):
            try:
                return await _call()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if i == LLM_MAX_RETRIES:
                    raise
        return ""

    return await with_retry(_call_sdk_like)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (handles ```json fences and chatter)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


async def complete_json(system: str, user: str, max_tokens: int = 1200, web_search: bool = False) -> dict:
    """Ask for JSON; on a parse failure retry once with the error appended, then raise ValueError."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    raw = await complete(messages, max_tokens=max_tokens, temperature=0, web_search=web_search)
    try:
        return _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as err:
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": f"That was not valid JSON ({err}). Reply with ONLY the JSON object."}]
        raw = await complete(messages, max_tokens=max_tokens, temperature=0, web_search=web_search)
        try:
            return _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as err2:
            raise ValueError(f"LLM did not return JSON: {err2}") from err2


# ---------------- intent ----------------

INTENTS = [
    "start_campaign", "approve", "reject", "remove_items", "edit_draft", "add_lead", "import_list",
    "update_settings", "pause", "resume", "status", "show_log", "show_lead", "classify_reply", "help", "unknown",
]

PARSE_INTENT_PROMPT = """You convert a user's chat message into a JSON command for LeadReach, an outreach assistant \
that finds local business leads for a brand. Current state: {state}. Pending checkpoint: {gate}. \
Last list shown (1-based): {numbered}. Valid intents: {intents}.
Args by intent:
- start_campaign: {{"brief": "<what the brand sells + who it wants to reach>", "city": "<city or null>"}}
- remove_items / show_lead: {{"ids": [<1-based positions>]}}
- reject: {{"feedback": "<what to change>"}}
- edit_draft: {{"instruction": "<edit>", "general": <true if it should apply to all future drafts>}}
- add_lead: {{"name": "...", "website": "... or null"}}
- update_settings: {{"ask_before": {{"icp|leads|contacts|reviews|drafts|send": bool}}, "daily_cap": int, "dry_run": bool}} \
(only the keys the user mentioned)
Reply ONLY with {{"intent": ..., "args": {{...}}, "confidence": 0..1}}. If unsure use "unknown". \
Never invent businesses or emails."""


async def parse_intent(text: str, state: str, gate: str | None, numbered: list[str]) -> dict:
    system = PARSE_INTENT_PROMPT.format(state=state, gate=gate or "none", numbered=numbered[:20], intents=INTENTS)
    data = await complete_json(system, text, max_tokens=400)
    if data.get("intent") not in INTENTS:
        data["intent"] = "unknown"
    data.setdefault("args", {})
    data.setdefault("confidence", 0.0)
    return data


# ---------------- lead finding ----------------

BUILD_ICP_PROMPT = """You help a brand find local businesses to sell to. From the brand's description, build an \
ideal-customer profile for searching OpenStreetMap and the web in {city}.
Brand profile (may contain ❓ placeholders = unknown): {profile}
Reply ONLY with JSON:
{{"product": "<what the brand sells, <=12 words>",
  "business_types": ["<2-5 plain business types, e.g. cafe, bakery, restaurant, gym, salon, hotel>"],
  "osm_tags": ["<matching OpenStreetMap tags key=value, e.g. amenity=cafe, shop=bakery>"],
  "search_queries": ["<3-5 web search queries that would list such businesses in {city}, \
e.g. 'specialty cafes in C-Scheme {city}'>"],
  "fit_criteria": ["<2-4 observable signs a business is a good fit>"],
  "city": "{city}", "radius_km": <5-15>}}
Use only what the brand said; do not invent claims about the brand."""


async def build_icp(brief: str, city: str, profile: dict) -> dict:
    system = BUILD_ICP_PROMPT.format(city=city, profile=json.dumps(profile, ensure_ascii=False)[:2500])
    data = await complete_json(system, brief, max_tokens=700)
    data["city"] = city
    for key in ("business_types", "osm_tags", "search_queries", "fit_criteria"):
        data[key] = [str(x) for x in (data.get(key) or [])][:6]
    return data


DISCOVER_PROMPT = """Search the web and list real, currently operating businesses in {city} that match: \
{types}. Search query: "{query}".
Only include businesses you found in search results. For each give its official website if one appears in the \
results (not Google Maps, Justdial, Zomato, Swiggy, Instagram or Facebook pages). Never guess a URL.
Reply ONLY with JSON: {{"businesses": [{{"name": "...", "website": "https://... or null", "area": "... or null", \
"evidence_url": "<the search result URL you saw it on>"}}]}} (max 15)."""


async def discover_businesses(icp: dict, query: str) -> list[dict]:
    """Web-search discovery. Results are CANDIDATES: the Lead Finder verifies websites by crawling."""
    system = DISCOVER_PROMPT.format(city=icp["city"], types=", ".join(icp["business_types"]), query=query)
    data = await complete_json(system, query, max_tokens=1500, web_search=True)
    return [b for b in data.get("businesses", []) if isinstance(b, dict) and b.get("name")][:15]


FIND_WEBSITES_PROMPT = """For each business below (all in {city}), search the web for its OFFICIAL website. \
Ignore directories and social media (Google Maps, Justdial, Zomato, Swiggy, Instagram, Facebook, LinkedIn). \
If you cannot find an official site, use null - never guess or construct a URL.
Businesses: {items}
Reply ONLY with JSON: {{"websites": [{{"id": "...", "website": "https://... or null"}}]}}"""


async def find_websites(businesses: list[dict], city: str) -> dict[str, str]:
    """-> {lead_id: candidate_url}. Candidates only; verify with crawl.name_matches()."""
    items = json.dumps([{"id": b["id"], "name": b["name"], "address": b.get("address")} for b in businesses])
    data = await complete_json(FIND_WEBSITES_PROMPT.format(city=city, items=items), "Find the websites.",
                               max_tokens=1200, web_search=True)
    return {w["id"]: w["website"] for w in data.get("websites", []) if isinstance(w, dict) and w.get("website")}


# ---------------- qualification + copy ----------------

QUALIFY_PROMPT = """You review a local business's website for a brand that sells: {product}. \
Fit criteria: {criteria}. Using ONLY the evidence provided, return a fit score and up to 3 hooks that connect an \
observed fact to the brand's offer. Each hook needs an exact evidence snippet and its URL. If nothing relevant is \
observable, return "hooks": []. Never guess.
Reply ONLY with JSON: {{"fit_score": 0-100, "hooks": [{{"observation": "<=25 words", "evidence": "exact snippet", \
"evidence_url": "https://...", "angle": "<=20 words"}}], "summary": "<=40 words"}}"""


async def qualify(icp: dict, lead: dict, signals: dict, page_text: str) -> dict:
    system = QUALIFY_PROMPT.format(product=icp.get("product"), criteria=icp.get("fit_criteria"))
    user = json.dumps({"business": lead.get("name"), "signals": signals, "page_text": page_text[:4000]})
    return await complete_json(system, user, max_tokens=800)


DRAFT_PROMPT = """You write a short, respectful first email from a brand to a local business.
What the brand sells, in the user's own words: {product}
Known brand facts (JSON): {facts}
UNKNOWN brand facts (never guess, invent or fill in; just leave them out): {unknown}
Rules: <= {max_words} words; subject <= 8 words, no clickbait or ALL CAPS; open with ONE specific observation from \
the qualification hooks (paraphrase, don't exaggerate); with no hooks, don't claim anything about the business and \
use "hook_used": null. Describe the product ONLY with the user's wording above and the known facts: never add \
qualities, origins, processes, certifications, awards, customer numbers or service promises (e.g. "single-origin", \
"fresh-roasted", "small-batch", "cordless", "long battery life", "custom wholesale supply") that are not written \
there. {price_rule}{proof_rule}One call to action: {cta}. Sign with the sender name, brand and phone only where they \
are known facts. {unsubscribe_rule}Language: {language}. Follow these user style rules: {style_rules}. \
Never invent facts, numbers, names or features. Reply ONLY with JSON: \
{{"subject": "...", "body_text": "...", "hook_used": "... or null"}}"""


NO_PRICE_RULE = ("Do not mention price, pricing, cost, discounts, '% off', offers on price, or words like affordable or "
                 "cheap. ")
NO_PROOF_RULE = ("Do not mention existing customers, clients or users of the brand, testimonials, or who or how many "
                 "already use it (e.g. 'our customers', 'trusted by', '50+ salons'); the business's own customers "
                 "('your customers') are fine. ")


def price_allowed(profile: dict) -> bool:
    return (profile.get("pricing") or {}).get("mention_price") is True


def social_proof_allowed(profile: dict) -> bool:
    """Only with can_name_customers: true AND customer names or a customer count filled in."""
    proof = profile.get("social_proof") or {}
    names = [n for n in proof.get("customer_names") or [] if not _llm_unknown(n)]
    count = proof.get("customers_count")
    has_count = isinstance(count, int) and not isinstance(count, bool) and count > 0
    return proof.get("can_name_customers") is True and bool(names or has_count)


def _llm_unknown(value) -> bool:
    return value is None or "❓" in str(value) or not str(value).strip()


def known_facts(value, path: str = "", unknown: list[str] | None = None):
    """-> (value with every ❓/empty field removed, dotted paths of the removed fields). Lists/dicts recurse."""
    unknown = [] if unknown is None else unknown
    if isinstance(value, dict):
        kept = {}
        for k, v in value.items():
            sub, _ = known_facts(v, f"{path}.{k}" if path else str(k), unknown)
            if sub not in (None, {}, []):
                kept[k] = sub
        return kept, unknown
    if isinstance(value, list):
        inner: list[str] = []
        kept = [known_facts(v, path, inner)[0] for v in value]
        kept = [v for v in kept if v not in (None, {}, [])]
        if inner:
            unknown.append(path)
        return kept, unknown
    if isinstance(value, str) and _llm_unknown(value):
        unknown.append(path)
        return None, unknown
    return value, unknown


async def draft_email(profile: dict, lead: dict, qualification: dict, style_rules: list[str],
                      problems: list[str] | None = None, product: str = "", brief: str = "") -> dict:
    """`problems` = what the copywriter's validator rejected in the previous attempt (regenerate once).
    `product` (the approved ICP product) and `brief` (the user's own words) are the only product wording allowed;
    ❓/empty brand-profile fields are sent as explicitly unknown, never as text the model could reuse."""
    facts, unknown = known_facts({k: v for k, v in profile.items() if k != "targeting"})
    e, cta = facts.get("email", {}), facts.get("cta", {})
    said = "; ".join(x for x in (str(product or "").strip(), str(brief or "").strip()) if x)
    unsubscribe = e.get("unsubscribe_line")
    system = DRAFT_PROMPT.format(
        product=json.dumps(said or "unknown - describe the offer only with the known facts", ensure_ascii=False),
        facts=json.dumps(facts, ensure_ascii=False), unknown=", ".join(unknown) or "none",
        max_words=e.get("max_words", 150),
        price_rule="" if price_allowed(profile) else NO_PRICE_RULE,
        proof_rule="" if social_proof_allowed(profile) else NO_PROOF_RULE,
        cta=cta.get("text") or "ask if a short call would be useful",
        unsubscribe_rule=f'End with this exact line: "{unsubscribe}". ' if unsubscribe else "",
        language=e.get("language", "English"), style_rules=style_rules,
    )
    payload = {"lead": lead, "qualification": qualification}
    if problems:
        payload["fix_these_problems_in_your_previous_draft"] = problems
    return await complete_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=800)


CLASSIFY_REPLY_PROMPT = """Classify a business's reply to an outreach email. Labels: interested | meeting_requested | \
not_now | not_interested | wrong_contact | unsubscribe | auto_reply | other. \
Reply ONLY with JSON {"label": "...", "reason": "<=15 words", "next_action": "<=15 words"}."""


async def classify_reply(text: str) -> dict:
    return await complete_json(CLASSIFY_REPLY_PROMPT, text, max_tokens=200)


# ======================= lead_finder.py =======================
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
    return await asyncio.gather(*(asyncio.wait_for(discover_businesses(icp, q), limit) for q in queries),
                                return_exceptions=True)


async def _osm(icp: dict, city: str, limit: float, notes: list[str]) -> tuple[dict, list[dict]]:
    """-> (place, OSM leads). Never raises: a failed or slow lookup becomes a note."""
    place = {"name": city, "bbox": None}
    try:
        async def lookup():
            found = await geocode(city)
            tags = tags_for(icp.get("business_types", []), icp.get("osm_tags"))
            return found, tags, await find_businesses(tags, found["bbox"])

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
            found = await asyncio.wait_for(find_websites(missing, city), lookup_limit)
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


register_specialist(
    name='leadreach-finder',
    description="Finds local businesses in a city that match a brand's ideal customer (OpenStreetMap + ASI:One web search).",
    tools={'find_leads': find_leads},
)

