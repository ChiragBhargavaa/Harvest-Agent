"""Polite web fetcher + contact extraction shared by the Lead Finder, Contact Finder and Lead Analyst.

Rules (HACKATHON_README §3.13): robots.txt respected, <=1 request/s per domain, identifying
User-Agent, 15 s timeout, HTML only, <=2 MB, never submit forms or log in, banned hosts skipped.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import aiohttp
import dns.resolver
from bs4 import BeautifulSoup

# Nominatim rejects placeholder contacts (e.g. example.com) with 403, so only add a real one
USER_AGENT = f"LeadReachBot/0.1 (+mailto:{os.environ['REPLY_TO']})" if os.getenv("REPLY_TO") else "LeadReachBot/0.1"
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
        soup = BeautifulSoup(html, "lxml")
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


def mx_ok(domain: str) -> bool:
    for rtype in ("MX", "A"):
        try:
            if dns.resolver.resolve(domain, rtype, lifetime=5):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def name_matches(name: str, page: Page) -> bool:
    """Does the business name plausibly appear on the page (used to verify LLM-suggested websites)?"""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 and w not in {"the", "and", "cafe", "shop"}]
    if not words:
        return False
    haystack = (page.text[:20000] + " " + page.url).lower()
    hits = sum(1 for w in words if w in haystack)
    return hits >= max(1, (len(words) + 1) // 2)
