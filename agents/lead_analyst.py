"""Lead Analyst agent: `qualify_leads` - fit score + evidence-backed hooks from each lead's website.

`site_signals` (deterministic page checks) works now; the ASI:One scoring (llm.qualify) is M3 work.

Run:  python agents/lead_analyst.py
"""

import re

from common import make_specialist
from crawl import Page

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


async def qualify_leads(args: dict) -> dict:
    if not isinstance(args.get("leads"), list) or not args["leads"]:
        raise ValueError("missing 'leads' list")
    raise NotImplementedError("qualify_leads is not implemented yet (milestone M3)")


agent = make_specialist(
    name="leadreach-analyst",
    seed_env="ANALYST_SEED",
    port_env="ANALYST_PORT",
    default_port=8004,
    profile="lead_analyst.md",
    description="Reviews a business's website and scores how well it fits a brand, with evidence-backed outreach hooks.",
    tools={"qualify_leads": qualify_leads},
)

if __name__ == "__main__":
    agent.run()
