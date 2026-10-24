"""Lead Analyst agent: `qualify_leads` - fit score + evidence-backed hooks from each lead's website.

Pipeline per lead (<=5 leads at a time, one shared PoliteFetcher):
  crawl.PoliteFetcher.crawl_site -> site_signals -> llm.qualify(icp, lead, signals, page_text).
Hooks are kept only if their evidence snippet really appears on a crawled page (or its title/meta description);
the evidence_url is set to the page it was found on. If the LLM fails (error, timeout, bad JSON, missing keys) the
lead gets a deterministic signals-only review with `fallback: true`. Leads without a usable website get a
signals-only review with `reachable: false` and an `unreachable_reason` (no LLM call: there is no evidence).
The whole step gets ANALYST_BUDGET_S (env, default 60 s; the orchestrator waits 70 s): leads still being reviewed then
are cancelled and get a minimal review (fit 0 = unknown, no hooks, `unreachable_reason: "timed_out"`, "Review timed out")
so the flow continues with a partial result, never an error.

Run:  python agents/lead_analyst.py
"""

import asyncio
import logging
import re
import time
from datetime import date

import llm
from common import env_seconds, make_specialist
from crawl import Page, PoliteFetcher, is_banned, normalise_url

KEYWORDS = {
    "online_ordering": ("order online", "online order", "delivery", "swiggy", "zomato"),
    "menu": ("menu",),
    "events": ("event", "workshop", "live music"),
    "catering": ("catering", "bulk order", "corporate"),
}


def site_signals(page: Page) -> dict:
    """Deterministic facts about a crawled homepage (evolved from the old page_check tool)."""
    html, text = page.html, page.text.lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    meta_desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I)
    years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text)]
    return {
        "final_url": page.url,
        "status": page.status,
        "https": page.url.startswith("https://"),
        "load_ms": page.load_ms,
        "size_kb": round(len(html.encode()) / 1024, 1),
        "title": title.group(1).strip() if title else None,
        "meta_description": meta_desc.group(1).strip() if meta_desc else None,
        "mobile_viewport": bool(re.search(r'<meta[^>]+name=["\']viewport', html, re.I)),
        "latest_year_seen": max(years) if years else None,
        "keywords": {k: any(w in text for w in words) for k, words in KEYWORDS.items()},
        "app_links": sorted({u for u in page.links if "play.google.com" in u or "apps.apple.com" in u})[:4],
    }


NAME = "leadreach-analyst"
log = logging.getLogger(NAME)  # the agent's own logger (uAgents names it after the agent)
CRAWL_CONCURRENCY = 5
MAX_PAGES = 5
MAX_HOOKS = 3
LLM_TIMEOUT_S = 45  # per lead, inside the step budget (a slow LLM falls back to signals); step capped by ANALYST_BUDGET_S
ANALYST_BUDGET_S = env_seconds("ANALYST_BUDGET_S", 60)  # stays below the orchestrator's 70 s qualify_leads wait
PAGE_TEXT_CHARS = 4000  # what llm.qualify sends
MIN_EVIDENCE_CHARS = 10
STOPWORDS = {"the", "and", "with", "for", "that", "has", "have", "are", "its", "their", "from", "into", "offers",
             "serves", "sells", "uses", "shop", "store", "business"}


def merged_signals(pages: list[Page]) -> dict:
    """site_signals of the homepage, with keyword flags, years and app links merged across all crawled pages."""
    signals = site_signals(pages[0])
    for page in pages[1:]:
        extra = site_signals(page)
        signals["keywords"] = {k: v or extra["keywords"][k] for k, v in signals["keywords"].items()}
        years = [y for y in (signals["latest_year_seen"], extra["latest_year_seen"]) if y]
        signals["latest_year_seen"] = max(years) if years else None
        signals["app_links"] = sorted(set(signals["app_links"]) | set(extra["app_links"]))[:4]
    signals["pages_crawled"] = len(pages)
    return signals


def page_text_for_llm(pages: list[Page]) -> str:
    """Each crawled page's URL + text, so the LLM can cite evidence_url; capped at what llm.qualify sends."""
    per_page = PAGE_TEXT_CHARS // len(pages)
    return "\n\n".join(f"[{p.url}]\n{p.text[:per_page]}" for p in pages)[:PAGE_TEXT_CHARS]


# ---------- evidence checks ----------

def _norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return " ".join(text.split())


def _corpus(page: Page, is_home: bool, signals: dict) -> str:
    parts = [page.text]
    if is_home:  # title / meta description come from the homepage HTML and may not be in the visible text
        parts += [signals.get("title") or "", signals.get("meta_description") or ""]
    return _norm(" ".join(parts))


def find_evidence(evidence: str, pages: list[Page], signals: dict, preferred_url: str = "") -> str | None:
    """-> URL of the crawled page containing the snippet (every '...'-separated fragment), or None."""
    fragments = [_norm(f).strip(" \"'.,;:") for f in re.split(r"\.{3}|…", evidence or "")]
    fragments = [f for f in fragments if f]
    if not fragments or sum(len(f) for f in fragments) < MIN_EVIDENCE_CHARS:
        return None
    ordered = sorted(enumerate(pages), key=lambda ip: ip[1].url != preferred_url)  # the cited page first
    for i, page in ordered:
        corpus = _corpus(page, i == 0, signals)
        if all(f in corpus for f in fragments):
            return page.url
    return None


def _words(text: str, limit: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:limit]) + ("…" if len(words) > limit else "")


def supported_hooks(hooks, pages: list[Page], signals: dict) -> list[dict]:
    """Keep only LLM hooks whose evidence was observed on a crawled page; never invent or repair evidence text."""
    kept, seen = [], set()
    for h in hooks if isinstance(hooks, list) else []:
        if not isinstance(h, dict) or not str(h.get("observation") or "").strip():
            continue
        evidence = str(h.get("evidence") or "").strip()
        url = find_evidence(evidence, pages, signals, str(h.get("evidence_url") or ""))
        if url is None or _norm(evidence) in seen:
            continue
        seen.add(_norm(evidence))
        kept.append({"observation": _words(h["observation"], 25), "evidence": evidence, "evidence_url": url,
                     "angle": _words(h.get("angle"), 20)})
        if len(kept) == MAX_HOOKS:
            break
    return kept


# ---------- deterministic (signals-only) scoring ----------

def _matches(phrase: str, text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if len(w) > 2 and w not in STOPWORDS]
    return bool(words) and sum(w in text for w in words) >= max(1, (len(words) + 1) // 2)


def timed_out_review(lead: dict) -> dict:
    """A lead the step ran out of time for: fit unknown (0), no hooks, no claims."""
    return {"lead_id": lead["id"], "reachable": False, "unreachable_reason": "timed_out", "signals": {},
            "fit_score": 0, "hooks": [], "summary": "Review timed out; fit unknown.", "fallback": True}


def signals_review(icp: dict, lead: dict, signals: dict, pages: list[Page]) -> tuple[int, str]:
    """-> (fit_score, summary) from observable facts only. Used when the LLM is unavailable or there is no site."""
    category_fit = bool(lead.get("category")) and lead["category"] in (icp.get("osm_tags") or [])
    if not pages:
        score = 30 if category_fit else 15
        return score, _words("No website to review; score from the business category only." if category_fit
                             else "No website to review; fit unknown.", 40)
    text = _norm(" ".join(p.text for p in pages))
    criteria = [c for c in icp.get("fit_criteria") or [] if _matches(c, text)]
    types = [t for t in icp.get("business_types") or [] if _matches(t, text)]
    keywords = [k.replace("_", " ") for k, v in (signals.get("keywords") or {}).items() if v]
    year = signals.get("latest_year_seen")
    recent = bool(year) and year >= date.today().year - 1
    score = (35 + 10 * category_fit + 5 * bool(types) + min(30, 10 * len(criteria)) + 5 * bool(signals.get("https"))
             + 5 * bool(signals.get("mobile_viewport")) + 5 * recent + 5 * bool(keywords))
    facts = ["site is live" + (" (https)" if signals.get("https") else "")]
    if recent:
        facts.append(f"updated {year}")
    if criteria:
        facts.append("matches: " + ", ".join(criteria))
    if keywords:
        facts.append("mentions: " + ", ".join(keywords))
    return min(score, 100), _words("Signals-only review: " + "; ".join(facts) + ".", 40)


# ---------- the tool ----------

async def _crawl(fetcher, url: str) -> tuple[list[Page], str | None]:
    """-> (pages, unreachable reason). Never raises."""
    try:
        if not await fetcher.allowed(url):
            return [], "robots_disallowed"
        pages = await fetcher.crawl_site(url, max_pages=MAX_PAGES)
    except Exception:  # noqa: BLE001 - network errors: record and move on
        return [], "unreachable"
    if pages and is_banned(pages[0].url):  # homepage redirected to a banned host
        return [], "banned_host"
    pages = [p for p in pages if p.status < 400 and not is_banned(p.url)]
    return pages, None if pages else "unreachable"


async def _llm_review(icp: dict, lead: dict, signals: dict, pages: list[Page]) -> dict | None:
    """-> {fit_score, hooks, summary} from the LLM with unsupported hooks dropped, or None on any LLM failure."""
    try:
        data = await asyncio.wait_for(llm.qualify(icp, lead, signals, page_text_for_llm(pages)), LLM_TIMEOUT_S)
        score = max(0, min(100, int(round(float(data["fit_score"])))))
        hooks = supported_hooks(data["hooks"], pages, signals)
    except Exception:  # noqa: BLE001 - error, timeout, bad JSON, missing/invalid keys -> signals-only fallback
        return None
    return {"fit_score": score, "hooks": hooks, "summary": _words(data.get("summary"), 40)}


async def review_lead(icp: dict, lead: dict, fetcher, sem: asyncio.Semaphore) -> dict:
    website = normalise_url(lead.get("website"))
    pages, reason = [], None
    async with sem:  # <=5 leads crawled + scored at a time
        if not website:
            reason = "no_website"
        elif is_banned(website):
            reason = "banned_host"
        else:
            pages, reason = await _crawl(fetcher, website)
        signals = merged_signals(pages) if pages else {}
        review = await _llm_review(icp, lead, signals, pages) if pages else None
    fallback = review is None
    score, summary = signals_review(icp, lead, signals, pages)
    if fallback:
        review = {"fit_score": score, "hooks": [], "summary": summary}
    elif not review["summary"]:  # LLM gave a score but no summary
        review["summary"] = summary
    return {"lead_id": lead["id"], "reachable": bool(pages), "unreachable_reason": reason, "signals": signals,
            "fit_score": review["fit_score"], "hooks": review["hooks"], "summary": review["summary"],
            "fallback": fallback}


async def qualify_leads(args: dict) -> dict:
    leads, icp = args.get("leads"), args.get("icp")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    if not isinstance(icp, dict):
        raise ValueError("missing 'icp'")
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)
    start = time.monotonic()

    async with PoliteFetcher() as fetcher:  # one fetcher: shared robots cache + per-domain rate limit
        async def one(lead: dict) -> dict:
            try:
                return await review_lead(icp, lead, fetcher, sem)
            except Exception:  # noqa: BLE001 - a per-lead failure never fails the whole step
                score, summary = signals_review(icp, lead, {}, [])
                return {"lead_id": lead["id"], "reachable": False, "unreachable_reason": "unreachable", "signals": {},
                        "fit_score": score, "hooks": [], "summary": summary, "fallback": True}

        tasks = [asyncio.create_task(one(lead)) for lead in leads]
        _, pending = await asyncio.wait(tasks, timeout=ANALYST_BUDGET_S)
        for task in pending:  # over budget: return what we have
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    reviews = [timed_out_review(lead) if task in pending else task.result() for lead, task in zip(leads, tasks, strict=True)]
    log.info(f"qualify_leads done in {time.monotonic() - start:.1f}s: {len(reviews)} reviews, "
             f"{sum(not r['fallback'] for r in reviews)} by the LLM, {len(pending)} timed out")
    return {"reviews": sorted(reviews, key=lambda r: -r["fit_score"])}  # best fit first (stable)


agent = make_specialist(
    name=NAME,
    seed_env="ANALYST_SEED",
    port_env="ANALYST_PORT",
    default_port=8004,
    profile="lead_analyst.md",
    description="Reviews a business's website and scores how well it fits a brand, with evidence-backed outreach hooks.",
    tools={"qualify_leads": qualify_leads},
)

if __name__ == "__main__":
    agent.run()
