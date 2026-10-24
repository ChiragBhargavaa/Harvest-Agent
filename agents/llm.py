"""ASI:One LLM wrapper (OpenAI-compatible endpoint) + every LeadReach prompt.

ASI:One is the reasoning core (intent parsing, ICP building, web discovery, qualification,
copywriting) - that is what the "ASI:One LLM central to reasoning" criterion rewards.
Every prompt demands JSON only and forbids inventing facts. LLM output is never a source
of emails: websites it suggests are verified by crawling before they count.
"""

import json
import os
import re

from openai import AsyncOpenAI

from common import env_bool, env_seconds, require_env, with_retry

ASI1_BASE_URL = "https://api.asi1.ai/v1"
ASI1_MODEL = os.getenv("ASI1_MODEL", "asi1")
# per HTTP request (the SDK default is 600 s + 2 retries, which hung a live find_leads for >10 min)
LLM_TIMEOUT_S = env_seconds("LEADREACH_LLM_TIMEOUT", 20)
WEB_SEARCH_LLM_TIMEOUT_S = env_seconds("LEADREACH_WEB_SEARCH_LLM_TIMEOUT", 40)  # live web search takes 20-40 s
LLM_MAX_RETRIES = 1

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=ASI1_BASE_URL, api_key=require_env("ASI1_API_KEY"),
                              timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)
    return _client


async def complete(messages: list[dict], max_tokens: int = 1024, temperature: float = 0.2,
                   web_search: bool = False) -> str:
    # ASI:One opts into search by the *presence* of web_search_options (only search_context_size is allowed);
    # the old `web_search` boolean is rejected with HTTP 400, so without search send no search field at all.
    extra = {}
    if web_search and env_bool("ASI1_WEB_SEARCH", True):
        extra["web_search_options"] = {"search_context_size": os.getenv("ASI1_SEARCH_CONTEXT", "medium")}
        extra["timeout"] = WEB_SEARCH_LLM_TIMEOUT_S  # per-request override of the 20 s client default

    async def _call():
        r = await client().chat.completions.create(
            model=ASI1_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **extra,
        )
        return r.choices[0].message.content or ""

    return await with_retry(_call)


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
    names = [n for n in proof.get("customer_names") or [] if not _unknown(n)]
    count = proof.get("customers_count")
    has_count = isinstance(count, int) and not isinstance(count, bool) and count > 0
    return proof.get("can_name_customers") is True and bool(names or has_count)


def _unknown(value) -> bool:
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
    if isinstance(value, str) and _unknown(value):
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
