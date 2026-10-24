"""leadreach-writer - Agentverse HOSTED port of LeadReach / Harvest Agent agents/copywriter.py.

Single self-contained file. Uses the pre-provided global `agent` (do not construct Agent or call run()).
Secrets (Agentverse -> agent -> Secrets): ASI1_API_KEY (required); optional LEADREACH_DRAFT_LLM_TIMEOUT, ASI1_MODEL
Protocol: leadreach-tools 0.1.0, ToolRequest -> ToolResponse (identical to the local agents, same digest).
"""

import asyncio
import json
import logging
import os
import re
import time

import aiohttp
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
agent = agent  # noqa: F821

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


# ======================= copywriter.py =======================
NAME = "leadreach-writer"
log = logging.getLogger(NAME)  # the agent's own logger (uAgents names it after the agent)
DRAFT_CONCURRENCY = 15  # = the ≤15 leads per request (§3.6): one wave
# A live ASI:One draft call can take 30+ s, and an in-flight call cancelled by this timeout is never logged by
# httpx. One wave x 2 attempts x 40 s = 80 s fits the orchestrator's 60 s wait + 30 s resend (deduped, same run),
# and a local fallback run fits the 180 s step worst case (HACKATHON_README §3.6).
LLM_TIMEOUT_S = env_seconds("LEADREACH_DRAFT_LLM_TIMEOUT", 40)
DEFAULT_MAX_WORDS = 150  # same default as draft_email
MAX_SUBJECT_WORDS, MAX_SUBJECT_CHARS = 8, 60
MIN_HOOK_MATCH_CHARS = 10
PROFILE_GAP = "config/brand_profile.yaml"  # in every problem caused by an unfilled profile field
# Product qualities the LLM likes to add; each must appear in the brief / ICP product / known profile facts / review.
CLAIM_TERMS = (
    "single origin", "fresh roasted", "freshly roasted", "small batch", "organic", "artisan", "artisanal",
    "handcrafted", "hand crafted", "handmade", "award winning", "certified", "fair trade", "ethically sourced",
    "sustainably sourced", "direct trade", "premium", "specialty grade", "locally roasted", "farm to cup", "wholesale",
    # generic product-quality claims (e.g. for a trimmer)
    "cordless", "waterproof", "long battery", "battery life", "professional grade", "salon grade", "precision",
    "ergonomic", "lightweight", "light weight", "durable", "best in class", "fastest", "quietest", "warranty",
    "guarantee", "guaranteed",
)
PRICE_RE = re.compile(r"₹\s*\d|\b(?:rs|inr|usd)\.?\s*\d|\$\s*\d|\d[\d,.]*\s*(?:/-|rupees\b|rs\b|inr\b)"
                      r"|\b(?:price[sd]?|pricing|discount(?:s|ed)?|costs?|costing|affordabl[ey]|cheap(?:er|est|ly)?)\b"
                      r"|%\s*off\b|\bper\s*cent\s+off\b", re.I)
# Claims about the brand's existing customers. "your customers" (the lead's own) never matches.
SOCIAL_PROOF_RE = re.compile(
    r"\bour\s+(?:existing\s+|current\s+|happy\s+)?(?:customers?|clients?|users|partners)\b"
    r"|\b(?:trusted|used|loved|chosen|preferred|relied\s+on)\s+by\b"
    r"|\b(?:salons?|barbers?|barber\s+shops?|shops?|stores?|businesses|brands|customers|clients|cafes|outlets)"
    r"\s+(?:\w+\s+){0,3}?already\s+(?:use|using|love|trust|stock|rely)"
    r"|\b\d[\d,]*\s*\+?\s*(?:other\s+)?(?:salons?|barbers?|barber\s+shops?|shops|stores|businesses|brands|customers|"
    r"clients|cafes|outlets|users)\b"
    r"|\b\d[\d,]*\s*\+\s*[a-z]"
    r"|(?<!your\s)(?<!their\s)\b(?:customers|clients)\s+love\b"
    r"|\btestimonials?\b|\b(?:happy|satisfied|loyal)\s+(?:customers|clients)\b", re.I)


def _norm(text) -> str:
    text = str(text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip(" \"'.,;:")


def _known(value) -> str:
    """A brand-profile value, or "" if it is missing or still a ❓ placeholder."""
    value = str(value or "").strip()
    return "" if not value or "❓" in value else value


def _hook_used(value) -> str | None:
    value = str(value or "").strip()
    return None if value.lower() in ("", "null", "none") else value


def hook_matches(hook_used: str, hooks: list[dict]) -> bool:
    """hook_used must be (or quote) a review hook's observation, evidence or angle."""
    used = _norm(hook_used)
    for h in hooks or []:
        for field in (h.get("observation"), h.get("evidence"), h.get("angle")):
            f = _norm(field)
            if f and (used == f or (len(used) >= MIN_HOOK_MATCH_CHARS and used in f)
                      or (len(f) >= MIN_HOOK_MATCH_CHARS and f in used)):
                return True
    return False


def _words(text) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9%]+", str(text or "").lower())) + " "


def unsupported_claims(text: str, said: str, profile: dict, review: dict) -> list[str]:
    """CLAIM_TERMS in `text` that no allowed source contains (❓/empty profile fields don't count)."""
    allowed = _words(" ".join([said, json.dumps(known_facts(profile)[0], ensure_ascii=False),
                               json.dumps(review.get("hooks") or [], ensure_ascii=False), str(review.get("summary") or "")]))
    used = _words(text)
    return [t for t in CLAIM_TERMS if f" {t} " in used and f" {t} " not in allowed]


def validate(draft: dict, profile: dict, review: dict, said: str = "") -> list[str]:
    """Deterministic checks from §3.12. -> problems (empty = pass). Problems are phrased for the LLM to fix.
    `said` = the user's own product wording (ICP product + brief)."""
    brand, email = profile.get("brand") or {}, profile.get("email") or {}
    subject, body = str(draft.get("subject") or "").strip(), str(draft.get("body_text") or "").strip()
    problems = []
    max_words = int(email.get("max_words") or DEFAULT_MAX_WORDS)
    if not body:
        problems.append("body_text is empty")
    elif len(body.split()) > max_words:
        problems.append(f"body is {len(body.split())} words; the limit is {max_words}")
    if not subject:
        problems.append("subject is empty")
    elif len(subject.split()) > MAX_SUBJECT_WORDS or len(subject) > MAX_SUBJECT_CHARS:
        problems.append(f"subject must be <= {MAX_SUBJECT_WORDS} words and <= {MAX_SUBJECT_CHARS} characters")
    unsubscribe = _known(email.get("unsubscribe_line"))
    if not unsubscribe:
        problems.append("the brand profile has no unsubscribe_line (fill in config/brand_profile.yaml)")
    elif unsubscribe not in body:
        problems.append(f'body must contain this exact line: "{unsubscribe}"')
    for key, label in (("sender_name", "sender name"), ("name", "brand name")):
        value = _known(brand.get(key))
        if not value:
            problems.append(f"the brand profile's {label} is not filled in (brand.{key} in config/brand_profile.yaml)")
        elif value.lower() not in body.lower():
            problems.append(f'sign the email with the {label} "{value}"')
    if any(ch in subject + body for ch in "{}❓"):
        problems.append("remove placeholders ({...} or ❓); use only known facts")
    if not price_allowed(profile) and PRICE_RE.search(subject + " " + body):
        problems.append("do not mention prices")
    if not social_proof_allowed(profile) and SOCIAL_PROOF_RE.search(subject + " " + body):
        problems.append("do not mention existing customers, clients or testimonials (who or how many already use it)")
    used = _hook_used(draft.get("hook_used"))
    if used and not hook_matches(used, review.get("hooks") or []):
        problems.append("hook_used must be one of the observed hooks (or null)")
    for term in unsupported_claims(subject + " " + body, said, profile, review):
        problems.append(f'do not call the product "{term}": the brief and brand profile never say so')
    return problems


def _as_draft(lead_id: str, to: str, raw: dict, warnings: list[str]) -> dict:
    body = str(raw.get("body_text") or "").strip()
    return {"lead_id": lead_id, "to": to, "subject": str(raw.get("subject") or "").strip(), "body_text": body,
            "hook_used": _hook_used(raw.get("hook_used")), "word_count": len(body.split()), "warnings": warnings,
            "approved": False}


def _why(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):  # str() is empty
        return f"timed out after {LLM_TIMEOUT_S:g}s"
    return redact(f"{type(err).__name__}: {err}")[:200]


async def _llm_draft(profile: dict, lead: dict, review: dict, style_rules: list[str], product: str, brief: str,
                     problems: list[str] | None = None) -> dict:
    raw = await asyncio.wait_for(draft_email(profile, lead, review, style_rules, problems=problems,
                                                 product=product, brief=brief), LLM_TIMEOUT_S)
    if not isinstance(raw, dict):
        raise ValueError("LLM draft is not a JSON object")
    return raw


async def draft_one(profile: dict, lead: dict, contact: dict | None, review: dict, style_rules: list[str],
                    sem: asyncio.Semaphore, product: str = "", brief: str = "") -> tuple[dict | None, dict | None]:
    """-> (draft, None) or (None, skipped entry). Never raises. Contact confidence doesn't matter here (§3.4)."""
    if not contact or not contact.get("email"):
        return None, {"lead_id": lead["id"], "reason": "no_contact"}
    said = "; ".join(x for x in (product, brief) if x)
    context = dict(lead)
    phones = list((contact or {}).get("phones") or [])
    if phones:
        context["phones"] = phones
    elif lead.get("phone") and not context.get("phones"):
        context["phones"] = [lead.get("phone")]
    for key in ("website", "instagram", "facebook"):
        value = (contact or {}).get(key) or (None if context.get(key) else lead.get(key))
        if (contact or {}).get(key):
            context[key] = contact[key]
        elif value and not context.get(key):
            context[key] = value
    if contact.get("email"):
        context["email"] = contact["email"]
    async with sem:
        try:
            raw = await _llm_draft(profile, context, review, style_rules, product, brief)
        except Exception as err:  # noqa: BLE001 - error, timeout, bad JSON -> report this lead, keep going
            log.warning(f"no draft for {lead['id']}: {_why(err)}")
            return None, {"lead_id": lead["id"], "reason": "llm_failed", "detail": _why(err)}
        problems = validate(raw, profile, review, said)
        if any(PROFILE_GAP not in p for p in problems):  # regenerate once with the problems passed back
            try:
                retry = await _llm_draft(profile, context, review, style_rules, product, brief, problems=problems)
                raw, problems = retry, validate(retry, profile, review, said)
            except Exception as err:  # noqa: BLE001 - keep the first draft and its warnings
                log.warning(f"regenerating the draft for {lead['id']} failed, keeping the first one: {_why(err)}")
    return _as_draft(lead["id"], contact["email"], raw, problems), None


async def draft_emails(args: dict) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    profile = args.get("profile") if isinstance(args.get("profile"), dict) else {}
    style_rules = [str(r) for r in args.get("style_rules") or []]
    product = str((args.get("icp") or {}).get("product") or "").strip()
    brief = str(args.get("brief") or "").strip()
    contacts, reviews = {}, {}
    for c in args.get("contacts") or []:  # sorted per lead by find_contacts: the first one is the primary contact
        contacts.setdefault(c.get("lead_id"), c)
    for r in args.get("reviews") or []:
        reviews.setdefault(r.get("lead_id"), r)
    sem = asyncio.Semaphore(DRAFT_CONCURRENCY)
    results = await asyncio.gather(*(draft_one(profile, lead, contacts.get(lead["id"]), reviews.get(lead["id"], {}),
                                               style_rules, sem, product, brief) for lead in leads))
    drafts = [d for d, _ in results if d]
    drafts.sort(key=lambda d: -(reviews.get(d["lead_id"], {}).get("fit_score") or 0))  # best fit first (stable)
    return {"drafts": drafts, "skipped": [s for _, s in results if s]}


register_specialist(
    name='leadreach-writer',
    description='Writes short, evidence-based, personal outreach emails from a brand to local businesses.',
    tools={'draft_emails': draft_emails},
)

