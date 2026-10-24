"""leadreach-contacts - Agentverse HOSTED port of LeadReach / Harvest Agent agents/contact_finder.py.

Single self-contained file. Uses the pre-provided global `agent` (do not construct Agent or call run()).
Secrets (Agentverse -> agent -> Secrets): none required; optional CONTACT_BUDGET_S, REPLY_TO
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


# ======================= contact_finder.py =======================
NAME = "leadreach-contacts"
log = logging.getLogger(NAME)  # the agent's own logger (uAgents names it after the agent)
CRAWL_CONCURRENCY = 5
MAX_PAGES = 3
CONTACT_BUDGET_S = env_seconds("CONTACT_BUDGET_S", 50)  # whole step; stays below the orchestrator's 60 s wait
MX_TIMEOUT_S = 5
CONFIDENCES = ["high", "medium", "low"]
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
            mx_cache[domain] = asyncio.ensure_future(mx_ok(domain))
    contacts = []
    for email, contact in best.items():
        try:  # shield: the cached lookup stays usable for the domain's other emails after a timeout
            ok = await asyncio.wait_for(asyncio.shield(mx_cache[email.split("@")[1]]), MX_TIMEOUT_S)
        except asyncio.TimeoutError:  # slow DNS: keep the email, unverified, one confidence level lower
            ok = None
            contact = dict(contact, mx_ok=None, confidence=CONFIDENCES[
                min(CONFIDENCE_RANK[contact["confidence"]] + 1, len(CONFIDENCES) - 1)])
        except Exception:  # noqa: BLE001 - DNS failure counts as no MX
            ok = False
        if ok is not False:
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

    start = time.monotonic()

    async with PoliteFetcher() as fetcher:  # one fetcher: shared robots cache + per-domain rate limit
        async def one(lead: dict) -> tuple[list, str | None]:
            try:
                return await _contacts_for(lead, fetcher, sem, mx_cache, max_pages)
            except Exception:  # noqa: BLE001 - a per-lead failure never fails the whole step
                return [], "unreachable"

        tasks = [asyncio.create_task(one(lead)) for lead in leads]
        _, pending = await asyncio.wait(tasks, timeout=CONTACT_BUDGET_S)
        for task in pending:  # over budget: return what we have
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results = [([], "timed_out") if task in pending else task.result() for task in tasks]

    contacts, skipped = [], []
    for lead, (found, reason) in zip(leads, results, strict=True):
        contacts += found
        if reason and not found:
            skipped.append({"lead_id": lead["id"], "reason": reason})
    log.info(f"find_contacts done in {time.monotonic() - start:.1f}s: {len(contacts)} contacts, {len(skipped)} skipped "
             f"({len(pending)} timed out)")
    return {"contacts": contacts, "skipped": skipped}


register_specialist(
    name='leadreach-contacts',
    description='Finds the published contact emails and phone numbers of local businesses by politely crawling their websites.',
    tools={'find_contacts': find_contacts},
)

