"""Harvest Agent — Agentverse hosted orchestrator (paste this whole file into Build).

The hosted runtime pre-creates `agent`. Do not build another agent object and do not start a run loop.
Specialist steps run on LEAD_*_AGENT_ADDRESS when that secret is set; otherwise (or on timeout)
they run in-process. There is no repo import and no module-level mutable cache: durable state
is ctx.storage. DRY_RUN sending is hard-coded on. Secrets come only from os.getenv.
"""

import asyncio
import copy
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import dns.resolver
from bs4 import BeautifulSoup
from uagents import Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

# Agentverse injects `agent` before this file runs. Tests assign it on the module first.
agent = agent  # noqa: F821

log = logging.getLogger("harvest-agent")

IST = timezone(timedelta(hours=5, minutes=30))
ASI1_URL = "https://api.asi1.ai/v1/chat/completions"
MAX_BYTES = 2 * 1024 * 1024
MIN_INTERVAL_S = 1.0
CACHE_TTL_S = 24 * 3600
OSM_CACHE_KEY = "osm_cache"
CAP_KEY = "demo_caps"
OUTBOX_KEY = "dry_run_outbox"
LIMIT_MSG = (
    "Daily demo limit reached — try tomorrow, or self-host with your own ASI1 key: "
    "https://github.com/ChiragBhargavaa/Harvest-Agent"
)

# Copied verbatim from agents/common.py so Model.build_schema_digest matches leadreach-tools 0.1.0.
class ToolRequest(Model):
    tool: str              # "find_leads" | "find_contacts" | "qualify_leads" | "draft_emails" | "send_emails"
    args_json: str = "{}"  # JSON-encoded arguments (keeps the schema stable)


class ToolResponse(Model):
    ok: bool
    result: str = ""       # JSON matching the tool's result schema
    error: str = ""


REQUEST_ID_KEY = "_request_id"

BRAND_PROFILE = {
    "brand": {
        "name": "❓ brand name",
        "website": "❓ https://...",
        "sender_name": "❓ your name",
        "sender_role": "❓ e.g. Founder",
        "phone": "❓ optional",
        "city": "❓ home city",
    },
    "offer": {
        "product": "a new trimmer",
        "one_liner": "❓ one sentence value proposition",
        "value_props": ["❓", "❓"],
    },
    "ideal_customers": [
        {"type": "hair salon", "why": "❓ why they fit"},
        {"type": "barber shop", "why": "❓ why they fit"},
    ],
    "social_proof": {"customers_count": None, "can_name_customers": False, "customer_names": []},
    "pricing": {"mention_price": False, "price_note": ""},
    "cta": {"text": "Would a 15-minute call or a free sample next week be useful?", "link": ""},
    "email": {
        "language": "English",
        "max_words": 140,
        "unsubscribe_line": "If you'd prefer not to hear from me again, just reply 'unsubscribe'.",
    },
    "targeting": {"default_city": "Jodhpur", "radius_km": 10, "batch_size": 15},
}

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

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_QUERY_TIMEOUT = 8
OVERPASS_OUT_LIMIT = 40
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


def now():
    return datetime.now(timezone.utc)


def ist_today() -> str:
    return datetime.now(IST).date().isoformat()


def user_agent() -> str:
    reply = os.getenv("REPLY_TO", "").strip()
    contact = f"+mailto:{reply}" if reply else "+https://github.com/ChiragBhargavaa/Harvest-Agent"
    return f"LeadReachBot/0.1 ({contact})"


def env_bool(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    try:
        value = int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def redact(text: str) -> str:
    text = str(text)
    for name in ("ASI1_API_KEY", "RESEND_API_KEY", "FOURSQUARE_API_KEY", "GOOGLE_PLACES_API_KEY"):
        secret = os.getenv(name, "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return re.sub(r"(?i)\b(sk-|bearer\s+)[\w.-]{6,}", r"\1***", text)


def load_profile() -> dict:
    return copy.deepcopy(BRAND_PROFILE)


def chat_text(text: str, end_session: bool = False) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=content)


def ack(msg: ChatMessage) -> ChatAcknowledgement:
    return ChatAcknowledgement(timestamp=now(), acknowledged_msg_id=msg.msg_id)


def text_of(msg: ChatMessage) -> str:
    return "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()


async def http_request(method: str, url: str, *, params=None, data=None, json_body=None,
                       headers=None, timeout: float = 15):
    """One HTTP call via the stdlib, off the event loop. Tests replace this function."""

    def _do():
        target = url
        if params:
            target = target + ("&" if "?" in target else "?") + urlencode(params)
        hdrs = {k: v for k, v in (headers or {}).items() if v}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode()
        elif data is not None:
            body = data
        req = Request(target, data=body, headers=hdrs, method=method.upper())
        try:
            with urlopen(req, timeout=timeout) as resp:
                return {"status": getattr(resp, "status", 200), "headers": dict(resp.headers),
                        "content": resp.read(MAX_BYTES + 1), "url": resp.geturl()}
        except HTTPError as err:
            raw = err.read(MAX_BYTES + 1) if err.fp else b""
            return {"status": err.code, "headers": dict(err.headers or {}), "content": raw, "url": target}

    try:
        return await asyncio.to_thread(_do)
    except URLError as err:
        raise RuntimeError(f"HTTP failed for {url}: {err.reason}") from err


def response_json(res: dict):
    return json.loads((res.get("content") or b"").decode("utf-8", errors="replace") or "null")


# ---------- state ----------

LOG_CAP = 500
MAX_REDOS = 2
IDLE, SETUP, BUILDING_ICP, AWAIT_ICP = "IDLE", "SETUP", "BUILDING_ICP", "AWAIT_ICP"
FINDING, AWAIT_LEADS = "FINDING", "AWAIT_LEADS"
FINDING_CONTACTS, AWAIT_CONTACTS = "FINDING_CONTACTS", "AWAIT_CONTACTS"
QUALIFYING, AWAIT_REVIEWS = "QUALIFYING", "AWAIT_REVIEWS"
DRAFTING, AWAIT_DRAFTS = "DRAFTING", "AWAIT_DRAFTS"
AWAIT_SEND, SENDING, DONE, PAUSED = "AWAIT_SEND", "SENDING", "DONE", "PAUSED"
STEP_FAILED = "STEP_FAILED"
GATE_STATES = {
    "icp": (BUILDING_ICP, AWAIT_ICP),
    "leads": (FINDING, AWAIT_LEADS),
    "contacts": (FINDING_CONTACTS, AWAIT_CONTACTS),
    "reviews": (QUALIFYING, AWAIT_REVIEWS),
    "drafts": (DRAFTING, AWAIT_DRAFTS),
    "send": (SENDING, AWAIT_SEND),
}
GATE_ORDER = ["icp", "leads", "contacts", "reviews", "drafts", "send"]
WAITING_TO_GATE = {wait: gate for gate, (_, wait) in GATE_STATES.items()}
ACTIVE_STATES = {
    BUILDING_ICP, AWAIT_ICP, FINDING, AWAIT_LEADS, FINDING_CONTACTS, AWAIT_CONTACTS,
    QUALIFYING, AWAIT_REVIEWS, DRAFTING, AWAIT_DRAFTS, SENDING, AWAIT_SEND, PAUSED, STEP_FAILED,
}
DEFAULT_CAMPAIGN = {
    "campaign_id": None,
    "state": IDLE,
    "paused_from": None,
    "brief": "",
    "target": {"city": None, "radius_km": 10, "limit": 15, "language": "English"},
    "icp": None,
    "leads": [], "contacts": [], "reviews": [], "drafts": [], "call_list": [], "source_notes": [],
    "removed_ids": [], "approved_low_conf": [], "draft_cursor": 0, "redo_count": {},
    "last_list": [], "last_list_names": [], "failed_gate": None,
    "settings": {"ask_before": {g: True for g in GATE_ORDER}, "daily_cap": 5, "dry_run": True},
    "style_rules": [],
    "pending_style_rule": None,
    "draft_skipped": [],
    "log": [],
}


def storage_key(sender: str) -> str:
    return "user:owner" if env_bool("OWNER_MODE", False) else f"user:{sender}"


def fresh_campaign() -> dict:
    camp = copy.deepcopy(DEFAULT_CAMPAIGN)
    camp["settings"]["daily_cap"] = min(env_int("DAILY_CAP", 5), 50)
    camp["settings"]["dry_run"] = True  # hosted demo never sends
    return camp


def load(ctx: Context, sender: str) -> dict:
    camp = fresh_campaign()
    stored = ctx.storage.get(storage_key(sender))
    if isinstance(stored, dict):
        camp.update(stored)
    if not isinstance(camp.get("settings"), dict):
        camp["settings"] = copy.deepcopy(DEFAULT_CAMPAIGN["settings"])
    camp["settings"]["dry_run"] = True
    camp.setdefault("log", [])
    return camp


def save(ctx: Context, sender: str, camp: dict) -> None:
    camp["log"] = (camp.get("log") or [])[-LOG_CAP:]
    camp["settings"]["dry_run"] = True
    ctx.storage.set(storage_key(sender), camp)


def new_campaign(camp: dict) -> dict:
    fresh = fresh_campaign()
    fresh["settings"] = copy.deepcopy(camp.get("settings") or fresh["settings"])
    fresh["settings"]["dry_run"] = True
    fresh["style_rules"] = list(camp.get("style_rules") or [])
    fresh["log"] = list(camp.get("log") or [])
    fresh["campaign_id"] = "c_" + datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    return fresh


def audit(camp: dict, step: str, action: str, actor: str = "user", detail: str = "") -> None:
    camp.setdefault("log", []).append({
        "ts": datetime.now(IST).isoformat(timespec="seconds"), "step": step,
        "action": action, "actor": actor, "detail": detail[:300],
    })


def pending_gate(camp: dict) -> str | None:
    return WAITING_TO_GATE.get(camp.get("state"))


def campaign_active(camp: dict) -> bool:
    return camp.get("state") in ACTIVE_STATES


def load_caps(ctx: Context) -> dict:
    today = ist_today()
    raw = ctx.storage.get(CAP_KEY)
    if not isinstance(raw, dict) or raw.get("ist_day") != today:
        return {"ist_day": today, "total": 0, "users": {}}
    users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
    return {"ist_day": today, "total": int(raw.get("total") or 0), "users": dict(users)}


def over_cap(ctx: Context, sender: str) -> bool:
    caps = load_caps(ctx)
    return caps["total"] >= env_int("DAILY_CAP_TOTAL", 20) or int(caps["users"].get(sender, 0)) >= env_int(
        "DAILY_CAP_PER_USER", 3)


def bump_cap(ctx: Context, sender: str) -> None:
    caps = load_caps(ctx)
    caps["total"] = int(caps["total"]) + 1
    caps["users"][sender] = int(caps["users"].get(sender, 0)) + 1
    ctx.storage.set(CAP_KEY, caps)


# ---------- cards ----------

MAX_ROWS = 15


def card(narration: str, kind: str, payload: dict, terminal: bool = False) -> ChatMessage:
    meta = {
        "card_protocol_version": "1",
        "requires_card_interaction": "false" if terminal else "true",
        "card_kind": kind,
        "card_payload": json.dumps(payload, ensure_ascii=False),
    }
    if terminal:
        meta["is_terminal"] = "true"
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=[
        TextContent(type="text", text=narration),
        MetadataContent(type="metadata", metadata=meta),
    ])


def _numbered(rows: list[dict]) -> str:
    return "\n".join(f"- **{r['label']}** - {r['value']}" for r in rows)


def setup_form(default_city: str | None, brief: str = "") -> ChatMessage:
    payload = {
        "title": "New lead campaign",
        "fields": [
            {"name": "brief", "kind": "text", "label": "What do you sell, and to which businesses?", "required": True},
            {"name": "city", "kind": "text", "label": "City", "required": True},
            {"name": "limit", "kind": "number", "label": "How many leads per batch (max 15)"},
            {"name": "language", "kind": "select", "label": "Email language",
             "options": [{"value": "English", "label": "English"}, {"value": "Hinglish", "label": "Hinglish"},
                         {"value": "Hindi", "label": "Hindi"}]},
        ],
        "submit_cta": {"label": "Find leads", "selection": {"action": "start_campaign", "step": "setup"}},
    }
    narration = (
        "Let's set up a lead campaign. Tell me **what you sell**, **which businesses** you want to reach, and the "
        f"**city**{f' (default: {default_city})' if default_city else ''}.\n\n"
        "Example: *\"I sell organic coffee beans - find cafés and bakeries in Jaipur\"*."
    )
    return card(narration, "form", payload)


def settings_form(settings: dict) -> ChatMessage:
    ask = settings["ask_before"]
    fields = [{"name": f"ask_{g}", "kind": "checkbox", "label": f"Ask me before moving on from: {g}"} for g in ask]
    fields += [{"name": "daily_cap", "kind": "number", "label": f"Daily send cap (now {settings['daily_cap']})"},
               {"name": "dry_run", "kind": "checkbox", "label": "Dry run (never actually send)"}]
    payload = {"title": "Approval settings", "fields": fields,
               "submit_cta": {"label": "Save settings", "selection": {"action": "update_settings", "step": "settings"}}}
    on = [g for g, v in ask.items() if v]
    narration = (f"Approvals are on for: {', '.join(on) or 'nothing'}. Daily cap {settings['daily_cap']}, "
                 f"dry run {'on' if settings['dry_run'] else 'off'}.\n\n"
                 "Change them here, or just say e.g. *\"stop asking me about contacts\"* or *\"cap at 8 a day\"*.")
    return card(narration, "form", payload)


def review_card(title: str, rows: list[dict], step: str, approve_label: str, reject_label: str = "Redo",
                intro: str = "", extra_hint: str = "") -> ChatMessage:
    if len(rows) > MAX_ROWS:
        title = f"{title} (showing {MAX_ROWS} of {len(rows)})"
    rows = rows[:MAX_ROWS]
    payload = {
        "title": title,
        "summary_rows": rows,
        "approve_cta": {"label": approve_label, "selection": {"action": "approve", "step": step}, "primary": True},
        "reject_cta": {"label": reject_label, "selection": {"action": "reject", "step": step}},
    }
    narration = (f"{intro}\n\n" if intro else "") + f"**{title}**\n{_numbered(rows)}\n\n" + (
        "Reply **approve**, **remove 2, 5**, or **redo** with what to change."
        + (f" {extra_hint}" if extra_hint else "")
    )
    return card(narration, "review", payload)


def detail_card(title: str, rows: list[dict], ctas: list[dict], narration: str, terminal: bool = False) -> ChatMessage:
    return card(narration, "detail", {"title": title, "summary_rows": rows[:MAX_ROWS], "ctas": ctas}, terminal)


def draft_card(draft: dict, index: int, total: int, note: str = "") -> ChatMessage:
    rows = [
        {"label": "To", "value": draft.get("to") or "-"},
        {"label": "Subject", "value": draft.get("subject", "")},
        {"label": "Hook used", "value": draft.get("hook_used") or "-"},
        {"label": "Body", "value": draft.get("body_text", "")},
    ]
    if draft.get("warnings"):
        rows.append({"label": "⚠ Warnings", "value": "; ".join(draft["warnings"])})
    lead_id = draft.get("lead_id")
    payload = {
        "title": f"Draft {index}/{total}",
        "summary_rows": rows,
        "approve_cta": {"label": "Approve", "selection": {"action": "approve", "step": "drafts", "lead_id": lead_id},
                        "primary": True},
        "reject_cta": {"label": "Skip", "selection": {"action": "reject", "step": "drafts", "lead_id": lead_id}},
    }
    warn = f"⚠ {'; '.join(draft['warnings'])}\n\n" if draft.get("warnings") else ""
    narration = ((f"{note}\n\n" if note else "") + f"**Draft {index}/{total}** to {draft.get('to')}\n\n"
                 f"**Subject:** {draft.get('subject')}\n\n{draft.get('body_text')}\n\n{warn}"
                 "Reply **approve**, **skip**, **approve all**, or tell me how to edit it "
                 "(e.g. *\"shorter and warmer\"*).")
    return card(narration, "review", payload)


def style_rule_card(rule: str) -> ChatMessage:
    payload = {
        "title": "Use this for every draft?",
        "summary_rows": [{"label": "Style rule", "value": rule}],
        "approve_cta": {"label": "Yes, all drafts", "selection": {"action": "approve", "step": "style_rule"},
                        "primary": True},
        "reject_cta": {"label": "Just this draft", "selection": {"action": "reject", "step": "style_rule"}},
    }
    narration = (f"Should I remember **\"{rule}\"** as a style rule for every draft in this campaign "
                 "(and rewrite the drafts you haven't approved yet)?\n\n"
                 "Reply **yes** to save it, or **just this one** to apply it to this draft only.")
    return card(narration, "review", payload)


def status_card(camp: dict) -> ChatMessage:
    s = camp["settings"]
    rows = [
        {"label": "State", "value": camp["state"]},
        {"label": "City", "value": (camp.get("target") or {}).get("city") or "-"},
        {"label": "Leads / contacts / drafts",
         "value": f"{len(camp.get('leads') or [])} / {len(camp.get('contacts') or [])} / {len(camp.get('drafts') or [])}"},
        {"label": "Approvals on", "value": ", ".join(g for g, v in s["ask_before"].items() if v) or "none"},
        {"label": "Daily cap · dry run", "value": f"{s['daily_cap']} · {'on' if s['dry_run'] else 'off'}"},
    ]
    for entry in (camp.get("log") or [])[-5:]:
        rows.append({"label": f"{entry['ts'][11:16]} {entry['step']}", "value": f"{entry['action']} ({entry['actor']})"})
    return detail_card("Campaign status", rows, [], "**Campaign status**\n" + _numbered(rows), terminal=True)
# ---------- LLM (ASI:One over HTTPS; the key is only os.getenv("ASI1_API_KEY")) ----------

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
DISCOVER_PROMPT = """Search the web and list real, currently operating businesses in {city} that match: \
{types}. Search query: "{query}".
Only include businesses you found in search results. For each give its official website if one appears in the \
results (not Google Maps, Justdial, Zomato, Swiggy, Instagram or Facebook pages). Never guess a URL.
Reply ONLY with JSON: {{"businesses": [{{"name": "...", "website": "https://... or null", "area": "... or null", \
"evidence_url": "<the search result URL you saw it on>"}}]}} (max 15)."""
FIND_WEBSITES_PROMPT = """For each business below (all in {city}), search the web for its OFFICIAL website. \
Ignore directories and social media (Google Maps, Justdial, Zomato, Swiggy, Instagram, Facebook, LinkedIn). \
If you cannot find an official site, use null - never guess or construct a URL.
Businesses: {items}
Reply ONLY with JSON: {{"websites": [{{"id": "...", "website": "https://... or null"}}]}}"""
QUALIFY_PROMPT = """You review a local business's website for a brand that sells: {product}. \
Fit criteria: {criteria}. Using ONLY the evidence provided, return a fit score and up to 3 hooks that connect an \
observed fact to the brand's offer. Each hook needs an exact evidence snippet and its URL. If nothing relevant is \
observable, return "hooks": []. Never guess.
Reply ONLY with JSON: {{"fit_score": 0-100, "hooks": [{{"observation": "<=25 words", "evidence": "exact snippet", \
"evidence_url": "https://...", "angle": "<=20 words"}}], "summary": "<=40 words"}}"""
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


def _unknown(value) -> bool:
    return value is None or "❓" in str(value) or not str(value).strip()


def social_proof_allowed(profile: dict) -> bool:
    proof = profile.get("social_proof") or {}
    names = [n for n in proof.get("customer_names") or [] if not _unknown(n)]
    count = proof.get("customers_count")
    has_count = isinstance(count, int) and not isinstance(count, bool) and count > 0
    return proof.get("can_name_customers") is True and bool(names or has_count)


def known_facts(value, path: str = "", unknown: list | None = None):
    unknown = [] if unknown is None else unknown
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            sub, _ = known_facts(item, f"{path}.{key}" if path else str(key), unknown)
            if sub not in (None, {}, []):
                kept[key] = sub
        return kept, unknown
    if isinstance(value, list):
        inner: list[str] = []
        kept = [known_facts(item, path, inner)[0] for item in value]
        kept = [item for item in kept if item not in (None, {}, [])]
        if inner:
            unknown.append(path)
        return kept, unknown
    if isinstance(value, str) and _unknown(value):
        unknown.append(path)
        return None, unknown
    return value, unknown


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


async def complete(messages: list[dict], max_tokens: int = 1024, temperature: float = 0.2,
                   web_search: bool = False) -> str:
    key = os.getenv("ASI1_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ASI1_API_KEY is not set — add it as an Agentverse secret")
    payload = {
        "model": os.getenv("ASI1_MODEL", "asi1").strip() or "asi1",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    timeout = env_seconds("LEADREACH_LLM_TIMEOUT", 15)
    if web_search and env_bool("ASI1_WEB_SEARCH", True):
        payload["web_search_options"] = {"search_context_size": os.getenv("ASI1_SEARCH_CONTEXT", "medium")}
        timeout = env_seconds("LEADREACH_WEB_SEARCH_LLM_TIMEOUT", 20)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _call():
        res = await http_request("POST", ASI1_URL, json_body=payload, headers=headers, timeout=timeout)
        if res["status"] >= 400:
            raise RuntimeError(f"ASI:One HTTP {res['status']}")
        data = response_json(res)
        return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    last = None
    for attempt in range(2):
        try:
            return await _call()
        except Exception as err:  # noqa: BLE001
            last = err
            if attempt == 0 and not web_search:
                await asyncio.sleep(0.2)
                continue
            break
    raise last


async def complete_json(system: str, user: str, max_tokens: int = 1200, web_search: bool = False) -> dict:
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


async def parse_intent(text: str, state: str, gate: str | None, numbered: list[str]) -> dict:
    system = PARSE_INTENT_PROMPT.format(state=state, gate=gate or "none", numbered=numbered[:20], intents=INTENTS)
    data = await complete_json(system, text, max_tokens=400)
    if data.get("intent") not in INTENTS:
        data["intent"] = "unknown"
    data.setdefault("args", {})
    data.setdefault("confidence", 0.0)
    return data


async def build_icp(brief: str, city: str, profile: dict) -> dict:
    system = BUILD_ICP_PROMPT.format(city=city, profile=json.dumps(profile, ensure_ascii=False)[:2500])
    data = await complete_json(system, brief, max_tokens=700)
    data["city"] = city
    for key in ("business_types", "osm_tags", "search_queries", "fit_criteria"):
        data[key] = [str(item) for item in (data.get(key) or [])][:6]
    return data


async def discover_businesses(icp: dict, query: str) -> list[dict]:
    system = DISCOVER_PROMPT.format(city=icp["city"], types=", ".join(icp.get("business_types") or []), query=query)
    data = await complete_json(system, query, max_tokens=1500, web_search=True)
    return [b for b in data.get("businesses", []) if isinstance(b, dict) and b.get("name")][:15]


async def find_websites(businesses: list[dict], city: str) -> dict:
    items = json.dumps([{"id": b["id"], "name": b["name"], "address": b.get("address")} for b in businesses])
    data = await complete_json(FIND_WEBSITES_PROMPT.format(city=city, items=items), "Find the websites.",
                               max_tokens=1200, web_search=True)
    return {w["id"]: w["website"] for w in data.get("websites", []) if isinstance(w, dict) and w.get("website")}


async def qualify(icp: dict, lead: dict, signals: dict, page_text: str) -> dict:
    system = QUALIFY_PROMPT.format(product=icp.get("product"), criteria=icp.get("fit_criteria"))
    user = json.dumps({"business": lead.get("name"), "signals": signals, "page_text": page_text[:4000]})
    return await complete_json(system, user, max_tokens=800)


async def draft_email(profile: dict, lead: dict, qualification: dict, style_rules: list, problems: list | None = None,
                      product: str = "", brief: str = "") -> dict:
    facts, unknown = known_facts({k: v for k, v in profile.items() if k != "targeting"})
    email, cta = facts.get("email", {}), facts.get("cta", {})
    said = "; ".join(x for x in (str(product or "").strip(), str(brief or "").strip()) if x)
    unsubscribe = email.get("unsubscribe_line")
    system = DRAFT_PROMPT.format(
        product=json.dumps(said or "unknown - describe the offer only with the known facts", ensure_ascii=False),
        facts=json.dumps(facts, ensure_ascii=False), unknown=", ".join(unknown) or "none",
        max_words=email.get("max_words", 150),
        price_rule="" if price_allowed(profile) else NO_PRICE_RULE,
        proof_rule="" if social_proof_allowed(profile) else NO_PROOF_RULE,
        cta=cta.get("text") or "ask if a short call would be useful",
        unsubscribe_rule=f'End with this exact line: "{unsubscribe}". ' if unsubscribe else "",
        language=email.get("language", "English"), style_rules=style_rules,
    )
    payload = {"lead": lead, "qualification": qualification}
    if problems:
        payload["fix_these_problems_in_your_previous_draft"] = problems
    return await complete_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=800)


# ---------- crawl ----------

def is_banned(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(item in host for item in BANNED_HOSTS)


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
    """Per-call robots cache and per-domain gap. Not stored on the module."""

    def __init__(self):
        self._robots: dict = {}
        self._last: dict = {}
        self._locks: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def _wait_turn(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = time.monotonic() - self._last.get(host, 0)
            if gap < MIN_INTERVAL_S:
                await asyncio.sleep(MIN_INTERVAL_S - gap)
            self._last[host] = time.monotonic()

    async def _get(self, url: str, timeout: float):
        return await http_request("GET", url, headers={"User-Agent": user_agent()}, timeout=timeout)

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = None
            try:
                await self._wait_turn(parsed.netloc)
                res = await self._get(base + "/robots.txt", 10)
                if res["status"] == 200:
                    rp = RobotFileParser()
                    rp.parse(res["content"].decode("utf-8", errors="ignore").splitlines())
            except Exception:  # noqa: BLE001
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(user_agent(), url)

    async def fetch(self, url: str) -> Page | None:
        if is_banned(url) or not await self.allowed(url):
            return None
        await self._wait_turn(urlparse(url).netloc)
        start = time.perf_counter()
        res = await self._get(url, 10)
        ctype = ""
        headers = res.get("headers") or {}
        for key, value in headers.items():
            if str(key).lower() == "content-type":
                ctype = str(value)
            if str(key).lower() == "content-length":
                try:
                    if int(value) > MAX_BYTES:
                        return None
                except (TypeError, ValueError):
                    pass
        if "html" not in ctype.lower():
            return None
        raw = res.get("content") or b""
        if len(raw) > MAX_BYTES:
            return None
        html = raw.decode("utf-8", errors="ignore")
        page = Page(str(res.get("url") or url), int(res.get("status") or 0), html,
                    int((time.perf_counter() - start) * 1000))
        soup = BeautifulSoup(html, "html.parser")
        page.links = [urljoin(page.url, a["href"]) for a in soup.find_all("a", href=True)]
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        page.text = " ".join(soup.get_text(" ").split())
        return page

    async def crawl_site(self, url: str, max_pages: int = 5) -> list:
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
            except Exception:  # noqa: BLE001
                continue
            if page:
                pages.append(page)
        return pages


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
    for email in found:
        email = email.strip().strip(".,;:").lower()
        if email.endswith(ASSET_SUFFIXES) or any(hint in email for hint in FAKE_EMAIL_HINTS) or not EMAIL_RE.fullmatch(email):
            continue
        clean.append(email)
    return sorted(set(clean))


def extract_phones(page: Page) -> list[str]:
    phones = {link[4:] for link in page.links if link.lower().startswith("tel:")}
    phones.update(match.strip() for match in PHONE_RE.findall(page.text))
    return sorted({re.sub(r"[\s-]", "", phone) for phone in phones if len(re.sub(r"\D", "", phone)) >= 10})


def guess_role(email: str) -> str:
    local = email.split("@")[0]
    for role in ("owner", "founder", "manager", "sales", "orders", "booking", "info", "contact", "hello", "support"):
        if role in local:
            return role
    return "unknown"


def mx_ok(domain: str) -> bool:
    """dnspython is on the hosted allowlist, so MX (then A) is checked. Timeout -> caller marks unverified."""
    for rtype in ("MX", "A"):
        try:
            if dns.resolver.resolve(domain, rtype, lifetime=5):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def name_matches(name: str, page: Page) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 and w not in {"the", "and", "cafe", "shop"}]
    if not words:
        return False
    haystack = (page.text[:20000] + " " + page.url).lower()
    hits = sum(1 for word in words if word in haystack)
    return hits >= max(1, (len(words) + 1) // 2)


# ---------- geo (24h cache in ctx.storage, not a module dict) ----------

def _cache_box(ctx) -> dict:
    if ctx is None:
        return {}
    box = ctx.storage.get(OSM_CACHE_KEY)
    return box if isinstance(box, dict) else {}


def _cache_get(box: dict, key: str):
    hit = box.get(key)
    if isinstance(hit, dict) and time.time() - float(hit.get("t") or 0) < CACHE_TTL_S:
        return hit.get("v")
    return None


def _cache_put(ctx, box: dict, key: str, value) -> None:
    box[key] = {"t": time.time(), "v": value}
    if len(box) > 20:
        for old in sorted(box, key=lambda item: (box[item] or {}).get("t") or 0)[: len(box) - 20]:
            box.pop(old, None)
    if ctx is not None:
        ctx.storage.set(OSM_CACHE_KEY, box)


def tags_for(business_types: list, extra_tags: list | None = None) -> list[str]:
    tags: list[str] = []
    for item in business_types or []:
        tags += OSM_TAGS.get(str(item).strip().lower(), [])
    tags += [item for item in (extra_tags or []) if TAG_RE.match(str(item))]
    return list(dict.fromkeys(tags))


async def geocode(city: str, ctx=None, lock: asyncio.Lock | None = None, country: str = "India") -> dict:
    key = f"geo:{city.lower()}:{country.lower()}"
    box = _cache_box(ctx)
    if (hit := _cache_get(box, key)) is not None:
        return hit
    if lock is None:
        lock = asyncio.Lock()
    async with lock:
        params = {"q": f"{city}, {country}", "format": "json", "limit": 1}
        res = await http_request("GET", NOMINATIM_URL, params=params,
                                 headers={"User-Agent": user_agent()}, timeout=env_seconds("LEADREACH_OSM_HTTP_TIMEOUT", 8))
        await asyncio.sleep(1)  # Nominatim: at most 1 request per second
    if res["status"] >= 400:
        raise RuntimeError(f"Nominatim HTTP {res['status']}")
    data = response_json(res)
    if not data:
        raise ValueError(f"could not find city '{city}' on OpenStreetMap")
    row = data[0]
    result = {"name": row["display_name"], "lat": float(row["lat"]), "lon": float(row["lon"]),
              "bbox": [float(item) for item in row["boundingbox"]]}
    _cache_put(ctx, box, key, result)
    return result


def _overpass_query(tags: list[str], bbox: list) -> str:
    """Small bbox query: at most 4 tag filters and 40 named elements, server timeout 8s."""
    south, north, west, east = bbox
    parts = "".join(
        f'nwr["{key}"="{value}"]["name"]({south},{west},{north},{east});'
        for key, value in (item.split("=", 1) for item in list(tags)[:4])
    )
    return f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT}];({parts});out center tags {OVERPASS_OUT_LIMIT};"


def _to_lead(el: dict) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return None
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    addr = tags.get("addr:full") or ", ".join(
        x for x in (tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb"), tags.get("addr:city")) if x
    )
    category = next((f"{key}={tags[key]}" for key in ("amenity", "shop", "tourism", "leisure", "office") if key in tags), None)
    return {
        "id": f"osm:{el['type']}/{el['id']}",
        "name": name,
        "category": category,
        "address": addr or None,
        "lat": lat,
        "lon": lon,
        "website": tags.get("website") or tags.get("contact:website"),
        "osm_emails": [e.strip() for e in (tags.get("email") or tags.get("contact:email") or "").split(";") if e.strip()],
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "source": "osm",
        "source_ref": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
    }


async def find_businesses(tags: list[str], bbox: list, ctx=None, lock: asyncio.Lock | None = None,
                          deadline: float | None = None) -> list[dict]:
    """Try Overpass mirrors in order. Each mirror gets one quick retry. Stops at `deadline`."""
    if not tags:
        return []
    query = _overpass_query(tags, bbox)
    box = _cache_box(ctx)
    if (hit := _cache_get(box, query)) is not None:
        return hit
    if lock is None:
        lock = asyncio.Lock()
    per_try = min(10.0, env_seconds("LEADREACH_OVERPASS_TIMEOUT", 9))
    pause = env_seconds("LEADREACH_OVERPASS_RETRY_PAUSE", 0.4)
    data, last_err, served = None, None, None
    headers = {"User-Agent": user_agent()}
    async with lock:
        for url in OVERPASS_URLS:
            for attempt in (1, 2):
                if deadline is not None and time.monotonic() >= deadline - 0.5:
                    last_err = TimeoutError("finder budget")
                    break
                timeout = per_try if deadline is None else min(per_try, max(1.0, deadline - time.monotonic()))
                try:
                    res = await http_request("POST", url, data={"data": query}, headers=headers, timeout=timeout)
                except Exception as err:  # noqa: BLE001
                    last_err = err
                    res = None
                if res is not None and res["status"] < 400:
                    data = response_json(res)
                    served = urlsplit(url).hostname or url
                    break
                if res is not None:
                    last_err = RuntimeError(f"Overpass HTTP {res['status']}")
                    if res["status"] not in (429, 500, 502, 503, 504):
                        break
                if attempt == 1 and (deadline is None or time.monotonic() + 0.05 < deadline):
                    remaining = pause if deadline is None else min(pause, deadline - time.monotonic())
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    continue
            if data is not None or (deadline is not None and time.monotonic() >= deadline - 0.5):
                break
    if data is None:
        raise RuntimeError(f"OpenStreetMap Overpass unavailable: {last_err}")
    leads = [lead for lead in (_to_lead(el) for el in (data or {}).get("elements", [])) if lead]
    log.info(f"Overpass served by {served} ({len(leads)} businesses)")
    _cache_put(ctx, box, query, leads)
    return leads
# ---------- find_leads ----------

MAX_LIMIT = 15
MAX_QUERIES = 2
MAX_VERIFY = 8
VERIFY_CONCURRENCY = 8
VERIFY_TIMEOUT_S = 8
VERIFY_RESERVE_S = 8
STOP_WORDS = {"the", "and", "cafe", "café", "restaurant", "pvt", "ltd", "private", "limited"}


def finder_budget() -> float:
    return env_seconds("FINDER_BUDGET_S", 40)


def _norm_name(name: str) -> str:
    return " ".join(w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def _domain_key(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    return host.removeprefix("www.") or None


def dedupe_leads(leads: list[dict], notes: list[str]) -> list[dict]:
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    for lead in leads:
        keys = [k for k in (f"d:{_domain_key(lead.get('website')) or ''}", f"n:{_norm_name(lead.get('name') or '')}") if k[2:]]
        first = next((seen[k] for k in keys if k in seen), None)
        if first is None:
            first = lead
            kept.append(lead)
        else:
            if not first.get("website") and lead.get("website"):
                first["website"], first["website_source"] = lead["website"], lead.get("website_source")
            if not first.get("phone") and lead.get("phone"):
                first["phone"] = lead["phone"]
            first["osm_emails"] = list(dict.fromkeys((first.get("osm_emails") or []) + (lead.get("osm_emails") or [])))
        for key in keys:
            seen.setdefault(key, first)
    if merged := len(leads) - len(kept):
        notes.append(f"Merged {merged} duplicate lead(s) (same website domain or name).")
    return kept


def _rank(lead: dict) -> int:
    return (4 if lead.get("osm_emails") else 0) + (2 if lead.get("website") else 0) + (1 if lead.get("phone") else 0)


def _why(err: BaseException, limit: float | None = None) -> str:
    limit = env_seconds("LEADREACH_WEB_SEARCH_TIMEOUT", 20) if limit is None else limit
    return f"timed out after {limit:g}s" if isinstance(err, asyncio.TimeoutError) else redact(str(err))


def _left(deadline: float, reserve: float = 0.0) -> float:
    return max(0.01, deadline - reserve - time.monotonic())


async def _discover_all(icp: dict, queries: list[str], limit: float) -> list:
    if not queries:
        return []
    return await asyncio.gather(*(asyncio.wait_for(discover_businesses(icp, query), limit) for query in queries),
                                return_exceptions=True)


async def _osm(icp: dict, city: str, limit: float, notes: list[str], ctx) -> tuple[dict, list]:
    place = {"name": city, "bbox": None}
    lock = asyncio.Lock()
    deadline = time.monotonic() + limit
    try:
        async def lookup():
            found = await geocode(city, ctx, lock)
            tags = tags_for(icp.get("business_types", []), icp.get("osm_tags"))
            return found, tags, await find_businesses(tags, found["bbox"], ctx, lock, deadline=deadline)

        place, tags, osm = await asyncio.wait_for(lookup(), limit)
        notes.append(f"OpenStreetMap: {len(osm)} businesses for {', '.join(tags) or 'no tags'}; "
                     f"{sum(1 for x in osm if x.get('website'))} list a website.")
        return place, osm
    except Exception as err:  # noqa: BLE001
        notes.append(f"OpenStreetMap failed ({_why(err, limit)}); using web search only.")
        return place, []


async def _verify_websites(leads: list[dict], notes: list[str], limit: float) -> int:
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    todo = [lead for lead in leads if lead.get("website") and lead.get("website_source") == "web_candidate"]
    for lead in todo[MAX_VERIFY:]:
        lead["website"], lead["website_source"] = None, None
    todo = todo[:MAX_VERIFY]
    if not todo:
        return 0

    async with PoliteFetcher() as fetcher:
        async def check(lead: dict):
            async with sem:
                try:
                    page = await asyncio.wait_for(fetcher.fetch(lead["website"]), VERIFY_TIMEOUT_S)
                except Exception:  # noqa: BLE001
                    page = None
            if page and page.status < 400 and name_matches(lead["name"], page):
                lead["website"], lead["website_source"], lead["verified"] = page.url, "web_verified", True
            else:
                lead["website"], lead["website_source"] = None, None

        tasks = [asyncio.create_task(check(lead)) for lead in todo]
        _, pending = await asyncio.wait(tasks, timeout=max(0.01, limit))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    unchecked = [lead for lead, task in zip(todo, tasks, strict=True) if task in pending]
    for lead in unchecked:
        lead["verified"] = False
    verified = sum(1 for lead in todo if lead.get("website_source") == "web_verified")
    notes.append(f"Web search suggested {len(todo)} websites; {verified} verified by crawling"
                 + (f", {len(unchecked)} unverified (time budget)." if unchecked else "."))
    return len(todo) - len(unchecked)


def lookup_cap() -> float:
    """Website lookup must not consume the whole finder budget."""
    return min(8.0, env_seconds("LEADREACH_LOOKUP_CAP", 8))


async def _lookup_websites(missing: list[dict], city: str, limit: float) -> tuple[dict, int]:
    """Parallel batches. A slow batch is cancelled; websites from finished batches are kept.
    -> (lead_id -> url, number of batches that did not finish)."""
    cap = min(limit, lookup_cap())
    if cap < 0.2 or not missing:
        return {}, 0
    batches = [missing[i:i + 3] for i in range(0, min(len(missing), 9), 3)]

    async def one(batch: list[dict]) -> dict:
        return await asyncio.wait_for(find_websites(batch, city), cap)

    tasks = [asyncio.create_task(one(batch)) for batch in batches]
    done, pending = await asyncio.wait(tasks, timeout=cap)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    found: dict = {}
    for task in done:
        if task.cancelled():
            continue
        try:
            result = task.result()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(result, dict):
            found.update(result)
    return found, len(pending)


async def find_leads(args: dict, ctx=None) -> dict:
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
    deadline = start + finder_budget()
    queries = (icp.get("search_queries") or [])[:MAX_QUERIES]
    discover_limit = _left(deadline, VERIFY_RESERVE_S)
    web_limit = min(env_seconds("LEADREACH_WEB_SEARCH_TIMEOUT", 20), discover_limit)
    if queries:
        (place, osm), web = await asyncio.gather(
            _osm(icp, city, discover_limit, notes, ctx),
            _discover_all(icp, queries, web_limit),
        )
    else:
        place, osm = await _osm(icp, city, discover_limit, notes, ctx)
        web = []
    log.info(f"find_leads osm={len(osm)} web_queries={len(queries)} in {time.monotonic() - start:.1f}s")
    banned_osm = 0
    for lead in osm:
        url = normalise_url(lead.get("website"))
        if url and is_banned(url):
            url, banned_osm = None, banned_osm + 1
        lead["website"], lead["website_source"] = url, "osm" if url else None
        by_name.setdefault(_norm_name(lead["name"]), lead)
    if banned_osm:
        notes.append(f"Ignored {banned_osm} OpenStreetMap website tag(s) pointing at directories or social media.")
    web_found = 0
    for query, found in zip(queries, web, strict=True):
        if isinstance(found, BaseException):
            notes.append(f"Web search failed for '{query}': {_why(found, web_limit)}")
            continue
        for biz in found:
            key = _norm_name(biz["name"])
            url = normalise_url(biz.get("website"))
            url = None if url and is_banned(url) else url
            if key in by_name:
                existing = by_name[key]
                if url and not existing.get("website"):
                    existing["website"], existing["website_source"] = url, "web_candidate"
                continue
            web_found += 1
            by_name[key] = {
                "id": f"web:{_slug(biz['name'])}", "name": biz["name"], "category": None,
                "address": biz.get("area"), "lat": None, "lon": None,
                "website": url, "website_source": "web_candidate" if url else None,
                "osm_emails": [], "phone": None, "source": "web", "source_ref": biz.get("evidence_url"),
            }
    if web_found:
        notes.append(f"Web search: {web_found} extra businesses not in OpenStreetMap.")
    leads = [lead for lead in by_name.values() if lead["id"] not in exclude]
    leads.sort(key=_rank, reverse=True)
    shortlist = leads[: limit * 2]
    missing = [lead for lead in shortlist if not lead.get("website")][:9]
    lookup_left = _left(deadline, VERIFY_RESERVE_S)
    if missing and lookup_left > 1:
        found, unfinished = await _lookup_websites(missing, city, lookup_left)
        applied = 0
        for lead in missing:
            url = normalise_url(found.get(lead["id"]))
            if url and not is_banned(url):
                lead["website"], lead["website_source"] = url, "web_candidate"
                applied += 1
        if unfinished:
            notes.append(f"Website lookup kept {applied} result(s); {unfinished} batch(es) ran out of time.")
    elif missing:
        notes.append("Website lookup skipped (time budget).")
    await _verify_websites(shortlist, notes, _left(deadline))
    shortlist = [lead for lead in shortlist if lead["source"] == "osm" or lead.get("website")]
    shortlist.sort(key=_rank, reverse=True)
    shortlist = dedupe_leads(shortlist, notes)
    picked = [lead for lead in shortlist if lead.get("website") or lead.get("osm_emails")][:limit]
    call_list = [lead for lead in shortlist if lead not in picked and lead.get("phone")][:limit]
    if len(picked) < limit:
        notes.append(f"Only {len(picked)} leads have a website or email; {len(call_list)} more have just a phone.")
    log.info(f"find_leads done: {len(picked)} leads, {len(call_list)} phone-only "
             f"({time.monotonic() - start:.1f}s)")
    if not picked and not call_list:
        raise RuntimeError("no leads found: " + " ".join(notes)[:300])
    return {"leads": picked, "call_list": call_list, "source_notes": notes, "city": place.get("name") or city}


# ---------- find_contacts ----------

CRAWL_CONCURRENCY = 5
MAX_PAGES = 3
MX_TIMEOUT_S = 5
CONFIDENCES = ["high", "medium", "low"]
ROLE_MAP = {"founder": "owner", "booking": "orders", "hello": "info", "support": "contact"}
HIGH_ROLES = {"owner", "manager", "sales", "orders"}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
ROLE_RANK = {"owner": 0, "manager": 0, "sales": 1, "orders": 1, "info": 2, "contact": 2}


def contact_budget() -> float:
    return env_seconds("CONTACT_BUDGET_S", 30)


def _clean_email(email: str) -> str | None:
    email = email.strip().removeprefix("mailto:").split("?")[0].strip(".,;:").lower()
    if not EMAIL_RE.fullmatch(email) or any(hint in email for hint in FAKE_EMAIL_HINTS):
        return None
    return email


def _same_site(domain: str, own: set) -> bool:
    return any(domain == item or domain.endswith("." + item) or item.endswith("." + domain) for item in own)


def classify(email: str, on_own_site: bool, own_domains: set) -> tuple[str, str]:
    role = ROLE_MAP.get(guess_role(email), guess_role(email))
    domain = email.split("@")[1]
    if not on_own_site:
        return role, "low"
    if _same_site(domain, own_domains):
        return role, "high" if role in HIGH_ROLES else "medium"
    if domain in FREEMAIL:
        return role, "medium"
    return role, "low"


async def _crawl_contact(fetcher, url: str, max_pages: int):
    try:
        if not await fetcher.allowed(url):
            return [], "robots_disallowed"
        pages = await fetcher.crawl_site(url, max_pages=max_pages)
    except Exception:  # noqa: BLE001
        return [], "unreachable"
    if pages and is_banned(pages[0].url):
        return [], "banned_host"
    pages = [page for page in pages if page.status < 400 and not is_banned(page.url)]
    return pages, None if pages else "unreachable"


async def _contacts_for(lead: dict, fetcher, sem: asyncio.Semaphore, mx_cache: dict, max_pages: int):
    website = normalise_url(lead.get("website"))
    found: list[tuple[str, str, bool]] = []
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
            pages, reason = await _crawl_contact(fetcher, website, max_pages)
        if pages:
            own.add(domain_of(pages[0].url))
        for page in pages:
            on_own = _same_site(domain_of(page.url), own)
            found += [(email, page.url, on_own) for email in extract_emails(page)]
            phones.update(extract_phones(page))
    phones.update(re.sub(r"[\s-]", "", phone) for phone in (lead.get("phone") or "").split(";") if phone.strip())
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
        if email and email not in best:
            role = guess_role(email)
            best[email] = {"lead_id": lead["id"], "email": email, "role": ROLE_MAP.get(role, role),
                           "person_name": None, "source": "osm", "source_url": lead.get("source_ref"),
                           "confidence": "low", "mx_ok": True}
    if not best:
        return [], reason or "no_email_found"
    for domain in {email.split("@")[1] for email in best}:
        if domain not in mx_cache:
            mx_cache[domain] = asyncio.ensure_future(asyncio.to_thread(mx_ok, domain))
    contacts = []
    for email, contact in best.items():
        try:
            ok = await asyncio.wait_for(asyncio.shield(mx_cache[email.split("@")[1]]), MX_TIMEOUT_S)
        except asyncio.TimeoutError:
            ok = None
            contact = dict(contact, mx_ok=None, confidence=CONFIDENCES[
                min(CONFIDENCE_RANK[contact["confidence"]] + 1, len(CONFIDENCES) - 1)])
        except Exception:  # noqa: BLE001
            ok = False
        if ok is not False:
            contacts.append(dict(contact, phones=sorted(phones)))
    if not contacts:
        return [], "mx_failed"
    contacts.sort(key=lambda c: (CONFIDENCE_RANK[c["confidence"]], ROLE_RANK.get(c["role"], 3), c["email"]))
    return contacts, None


async def find_contacts(args: dict, ctx=None) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    max_pages = max(1, min(int(args.get("max_pages_per_site") or MAX_PAGES), MAX_PAGES))
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)
    mx_cache: dict = {}
    start = time.monotonic()
    async with PoliteFetcher() as fetcher:
        async def one(lead: dict):
            try:
                return await _contacts_for(lead, fetcher, sem, mx_cache, max_pages)
            except Exception:  # noqa: BLE001
                return [], "unreachable"

        tasks = [asyncio.create_task(one(lead)) for lead in leads]
        _, pending = await asyncio.wait(tasks, timeout=contact_budget())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results = [([], "timed_out") if task in pending else task.result() for task in tasks]
    contacts, skipped = [], []
    for lead, (found, reason) in zip(leads, results, strict=True):
        contacts += found
        if reason and not found:
            skipped.append({"lead_id": lead["id"], "reason": reason})
    log.info(f"find_contacts done in {time.monotonic() - start:.1f}s: "
             f"{len(contacts)} contacts, {len(skipped)} skipped")
    return {"contacts": contacts, "skipped": skipped}


# ---------- qualify_leads ----------

KEYWORDS = {
    "online_ordering": ("order online", "online order", "delivery", "swiggy", "zomato"),
    "menu": ("menu",),
    "events": ("event", "workshop", "live music"),
    "catering": ("catering", "bulk order", "corporate"),
}
ANALYST_CONCURRENCY = 5
ANALYST_MAX_PAGES = 3
MAX_HOOKS = 3
PAGE_TEXT_CHARS = 4000
MIN_EVIDENCE_CHARS = 10
ANALYST_STOPWORDS = {"the", "and", "with", "for", "that", "has", "have", "are", "its", "their", "from", "into", "offers",
                     "serves", "sells", "uses", "shop", "store", "business"}


def analyst_budget() -> float:
    return env_seconds("ANALYST_BUDGET_S", 45)


def site_signals(page: Page) -> dict:
    html, text = page.html, page.text.lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    meta_desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I)
    years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text)]
    return {
        "final_url": page.url, "status": page.status, "https": page.url.startswith("https://"),
        "load_ms": page.load_ms, "size_kb": round(len(html.encode()) / 1024, 1),
        "title": title.group(1).strip() if title else None,
        "meta_description": meta_desc.group(1).strip() if meta_desc else None,
        "mobile_viewport": bool(re.search(r'<meta[^>]+name=["\']viewport', html, re.I)),
        "latest_year_seen": max(years) if years else None,
        "keywords": {key: any(word in text for word in words) for key, words in KEYWORDS.items()},
        "app_links": sorted({u for u in page.links if "play.google.com" in u or "apps.apple.com" in u})[:4],
    }


def merged_signals(pages: list) -> dict:
    signals = site_signals(pages[0])
    for page in pages[1:]:
        extra = site_signals(page)
        signals["keywords"] = {key: val or extra["keywords"][key] for key, val in signals["keywords"].items()}
        years = [y for y in (signals["latest_year_seen"], extra["latest_year_seen"]) if y]
        signals["latest_year_seen"] = max(years) if years else None
        signals["app_links"] = sorted(set(signals["app_links"]) | set(extra["app_links"]))[:4]
    signals["pages_crawled"] = len(pages)
    return signals


def page_text_for_llm(pages: list) -> str:
    per_page = PAGE_TEXT_CHARS // max(1, len(pages))
    return "\n\n".join(f"[{page.url}]\n{page.text[:per_page]}" for page in pages)[:PAGE_TEXT_CHARS]


def _norm_text(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return " ".join(text.split())


def _corpus(page: Page, is_home: bool, signals: dict) -> str:
    parts = [page.text]
    if is_home:
        parts += [signals.get("title") or "", signals.get("meta_description") or ""]
    return _norm_text(" ".join(parts))


def find_evidence(evidence: str, pages: list, signals: dict, preferred_url: str = "") -> str | None:
    fragments = [_norm_text(frag).strip(" \"'.,;:") for frag in re.split(r"\.{3}|…", evidence or "")]
    fragments = [frag for frag in fragments if frag]
    if not fragments or sum(len(frag) for frag in fragments) < MIN_EVIDENCE_CHARS:
        return None
    ordered = sorted(enumerate(pages), key=lambda ip: ip[1].url != preferred_url)
    for index, page in ordered:
        corpus = _corpus(page, index == 0, signals)
        if all(frag in corpus for frag in fragments):
            return page.url
    return None


def _words(text, limit: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:limit]) + ("…" if len(words) > limit else "")


def supported_hooks(hooks, pages: list, signals: dict) -> list[dict]:
    kept, seen = [], set()
    for hook in hooks if isinstance(hooks, list) else []:
        if not isinstance(hook, dict) or not str(hook.get("observation") or "").strip():
            continue
        evidence = str(hook.get("evidence") or "").strip()
        url = find_evidence(evidence, pages, signals, str(hook.get("evidence_url") or ""))
        if url is None or _norm_text(evidence) in seen:
            continue
        seen.add(_norm_text(evidence))
        kept.append({"observation": _words(hook["observation"], 25), "evidence": evidence, "evidence_url": url,
                     "angle": _words(hook.get("angle"), 20)})
        if len(kept) == MAX_HOOKS:
            break
    return kept


def timed_out_review(lead: dict) -> dict:
    return {"lead_id": lead["id"], "reachable": False, "unreachable_reason": "timed_out", "signals": {},
            "fit_score": 0, "hooks": [], "summary": "Review timed out; fit unknown.", "fallback": True}


def _matches(phrase: str, text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if len(w) > 2 and w not in ANALYST_STOPWORDS]
    return bool(words) and sum(word in text for word in words) >= max(1, (len(words) + 1) // 2)


def signals_review(icp: dict, lead: dict, signals: dict, pages: list) -> tuple[int, str]:
    category_fit = bool(lead.get("category")) and lead["category"] in (icp.get("osm_tags") or [])
    if not pages:
        score = 30 if category_fit else 15
        return score, _words("No website to review; score from the business category only." if category_fit
                             else "No website to review; fit unknown.", 40)
    text = _norm_text(" ".join(page.text for page in pages))
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


async def _crawl_review(fetcher, url: str):
    try:
        if not await fetcher.allowed(url):
            return [], "robots_disallowed"
        pages = await fetcher.crawl_site(url, max_pages=ANALYST_MAX_PAGES)
    except Exception:  # noqa: BLE001
        return [], "unreachable"
    if pages and is_banned(pages[0].url):
        return [], "banned_host"
    pages = [page for page in pages if page.status < 400 and not is_banned(page.url)]
    return pages, None if pages else "unreachable"


async def _llm_review(icp: dict, lead: dict, signals: dict, pages: list) -> dict | None:
    try:
        data = await asyncio.wait_for(
            qualify(icp, lead, signals, page_text_for_llm(pages)), env_seconds("LEADREACH_QUALIFY_LLM_TIMEOUT", 20))
        score = max(0, min(100, int(round(float(data["fit_score"])))))
        hooks = supported_hooks(data["hooks"], pages, signals)
    except Exception:  # noqa: BLE001
        return None
    return {"fit_score": score, "hooks": hooks, "summary": _words(data.get("summary"), 40)}


async def review_lead(icp: dict, lead: dict, fetcher, sem: asyncio.Semaphore) -> dict:
    website = normalise_url(lead.get("website"))
    pages, reason = [], None
    async with sem:
        if not website:
            reason = "no_website"
        elif is_banned(website):
            reason = "banned_host"
        else:
            pages, reason = await _crawl_review(fetcher, website)
        signals = merged_signals(pages) if pages else {}
        review = await _llm_review(icp, lead, signals, pages) if pages else None
    fallback = review is None
    score, summary = signals_review(icp, lead, signals, pages)
    if fallback:
        review = {"fit_score": score, "hooks": [], "summary": summary}
    elif not review["summary"]:
        review["summary"] = summary
    return {"lead_id": lead["id"], "reachable": bool(pages), "unreachable_reason": reason, "signals": signals,
            "fit_score": review["fit_score"], "hooks": review["hooks"], "summary": review["summary"],
            "fallback": fallback}


async def qualify_leads(args: dict, ctx=None) -> dict:
    leads, icp = args.get("leads"), args.get("icp")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    if not isinstance(icp, dict):
        raise ValueError("missing 'icp'")
    sem = asyncio.Semaphore(ANALYST_CONCURRENCY)
    start = time.monotonic()
    async with PoliteFetcher() as fetcher:
        async def one(lead: dict) -> dict:
            try:
                return await review_lead(icp, lead, fetcher, sem)
            except Exception:  # noqa: BLE001
                score, summary = signals_review(icp, lead, {}, [])
                return {"lead_id": lead["id"], "reachable": False, "unreachable_reason": "unreachable", "signals": {},
                        "fit_score": score, "hooks": [], "summary": summary, "fallback": True}

        tasks = [asyncio.create_task(one(lead)) for lead in leads]
        _, pending = await asyncio.wait(tasks, timeout=analyst_budget())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    reviews = [timed_out_review(lead) if task in pending else task.result()
               for lead, task in zip(leads, tasks, strict=True)]
    log.info(f"qualify_leads done in {time.monotonic() - start:.1f}s: {len(reviews)} reviews")
    return {"reviews": sorted(reviews, key=lambda r: -r["fit_score"])}


# ---------- draft_emails ----------

DRAFT_CONCURRENCY = 15
DEFAULT_MAX_WORDS = 150
MAX_SUBJECT_WORDS, MAX_SUBJECT_CHARS = 8, 60
MIN_HOOK_MATCH_CHARS = 10
PROFILE_GAP = "config/brand_profile.yaml"
CLAIM_TERMS = (
    "single origin", "fresh roasted", "freshly roasted", "small batch", "organic", "artisan", "artisanal",
    "handcrafted", "hand crafted", "handmade", "award winning", "certified", "fair trade", "ethically sourced",
    "sustainably sourced", "direct trade", "premium", "specialty grade", "locally roasted", "farm to cup", "wholesale",
    "cordless", "waterproof", "long battery", "battery life", "professional grade", "salon grade", "precision",
    "ergonomic", "lightweight", "light weight", "durable", "best in class", "fastest", "quietest", "warranty",
    "guarantee", "guaranteed",
)
PRICE_RE = re.compile(
    r"₹\s*\d|\b(?:rs|inr|usd)\.?\s*\d|\$\s*\d|\d[\d,.]*\s*(?:/-|rupees\b|rs\b|inr\b)"
    r"|\b(?:price[sd]?|pricing|discount(?:s|ed)?|costs?|costing|affordabl[ey]|cheap(?:er|est|ly)?)\b"
    r"|%\s*off\b|\bper\s*cent\s+off\b", re.I)
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


def draft_llm_timeout() -> float:
    return env_seconds("LEADREACH_DRAFT_LLM_TIMEOUT", 20)


def _norm_copy(text) -> str:
    text = str(text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip(" \"'.,;:")


def _known(value) -> str:
    value = str(value or "").strip()
    return "" if not value or "❓" in value else value


def _hook_used(value) -> str | None:
    value = str(value or "").strip()
    return None if value.lower() in ("", "null", "none") else value


def hook_matches(hook_used: str, hooks: list) -> bool:
    used = _norm_copy(hook_used)
    for hook in hooks or []:
        for field_name in (hook.get("observation"), hook.get("evidence"), hook.get("angle")):
            item = _norm_copy(field_name)
            if item and (used == item or (len(used) >= MIN_HOOK_MATCH_CHARS and used in item)
                         or (len(item) >= MIN_HOOK_MATCH_CHARS and item in used)):
                return True
    return False


def _wordbag(text) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9%]+", str(text or "").lower())) + " "


def unsupported_claims(text: str, said: str, profile: dict, review: dict) -> list[str]:
    allowed = _wordbag(" ".join([
        said, json.dumps(known_facts(profile)[0], ensure_ascii=False),
        json.dumps(review.get("hooks") or [], ensure_ascii=False), str(review.get("summary") or ""),
    ]))
    used = _wordbag(text)
    return [term for term in CLAIM_TERMS if f" {term} " in used and f" {term} " not in allowed]


def validate(draft: dict, profile: dict, review: dict, said: str = "") -> list[str]:
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
        problems.append(f"the brand profile has no unsubscribe_line (fill in {PROFILE_GAP})")
    elif unsubscribe not in body:
        problems.append(f'body must contain this exact line: "{unsubscribe}"')
    for key, label in (("sender_name", "sender name"), ("name", "brand name")):
        value = _known(brand.get(key))
        if not value:
            problems.append(f"the brand profile's {label} is not filled in (brand.{key} in {PROFILE_GAP})")
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


def _draft_why(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return f"timed out after {draft_llm_timeout():g}s"
    return redact(f"{type(err).__name__}: {err}")[:200]


async def _llm_draft(profile, lead, review, style_rules, product, brief, problems=None) -> dict:
    raw = await asyncio.wait_for(
        draft_email(profile, lead, review, style_rules, problems=problems, product=product, brief=brief),
        draft_llm_timeout())
    if not isinstance(raw, dict):
        raise ValueError("LLM draft is not a JSON object")
    return raw


async def draft_one(profile, lead, contact, review, style_rules, sem, product="", brief=""):
    if not contact or not contact.get("email"):
        return None, {"lead_id": lead["id"], "reason": "no_contact"}
    said = "; ".join(x for x in (product, brief) if x)
    async with sem:
        try:
            raw = await _llm_draft(profile, lead, review, style_rules, product, brief)
        except Exception as err:  # noqa: BLE001
            log.warning(f"no draft for {lead['id']}: {_draft_why(err)}")
            return None, {"lead_id": lead["id"], "reason": "llm_failed", "detail": _draft_why(err)}
        problems = validate(raw, profile, review, said)
        if any(PROFILE_GAP not in problem for problem in problems):
            try:
                retry = await _llm_draft(profile, lead, review, style_rules, product, brief, problems=problems)
                raw, problems = retry, validate(retry, profile, review, said)
            except Exception as err:  # noqa: BLE001
                log.warning(f"regenerating the draft for {lead['id']} failed, keeping the first one: "
                            f"{_draft_why(err)}")
    return _as_draft(lead["id"], contact["email"], raw, problems), None


async def draft_emails(args: dict, ctx=None) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    profile = args.get("profile") if isinstance(args.get("profile"), dict) else {}
    style_rules = [str(rule) for rule in args.get("style_rules") or []]
    product = str((args.get("icp") or {}).get("product") or "").strip()
    brief = str(args.get("brief") or "").strip()
    contacts, reviews = {}, {}
    for contact in args.get("contacts") or []:
        contacts.setdefault(contact.get("lead_id"), contact)
    for review in args.get("reviews") or []:
        reviews.setdefault(review.get("lead_id"), review)
    sem = asyncio.Semaphore(DRAFT_CONCURRENCY)
    results = await asyncio.gather(*(
        draft_one(profile, lead, contacts.get(lead["id"]), reviews.get(lead["id"], {}), style_rules, sem, product, brief)
        for lead in leads
    ))
    drafts = [draft for draft, _ in results if draft]
    drafts.sort(key=lambda draft: -(reviews.get(draft["lead_id"], {}).get("fit_score") or 0))
    return {"drafts": drafts, "skipped": [skipped for _, skipped in results if skipped]}


# ---------- send_emails: DRY_RUN is hard-coded. This never calls an email API. ----------

async def send_emails(args: dict, ctx=None) -> dict:
    drafts = args.get("drafts")
    if not isinstance(drafts, list) or not drafts:
        raise ValueError("missing 'drafts' list")
    redirect = os.getenv("TEST_REDIRECT_TO", "").strip()  # secret only; never a literal address
    try:
        daily_cap = max(1, int(float(args.get("daily_cap") or 5)))
    except (TypeError, ValueError):
        daily_cap = 5
    would, skipped = [], []
    for draft in drafts:
        lead_id = draft.get("lead_id")
        if len(would) >= daily_cap:
            skipped.append({"lead_id": lead_id, "reason": "cap"})
            continue
        would.append({
            "lead_id": lead_id,
            "to": redirect or draft.get("to"),
            "subject": draft.get("subject"),
            "dry_run": True,
        })
    if ctx is not None:
        box = ctx.storage.get(OUTBOX_KEY)
        box = list(box) if isinstance(box, list) else []
        box.extend(would)
        ctx.storage.set(OUTBOX_KEY, box[-200:])
    log.info(f"dry-run: recorded {len(would)} email(s), sent 0")
    return {"queued": [], "sent": [], "skipped": skipped, "would_send": would, "dry_run": True}
# ---------- payments (seller). Offered only when LEADPACK_PRICE is set. ----------

DEADLINE_S = 600
RECIPIENT_RE = re.compile(r"fetch1[a-z0-9]{33,74}")
PAY_FAST = {"buy lead pack": "buy_lead_pack", "unlock full list": "buy_lead_pack"}
pay_proto = Protocol(spec=payment_protocol_spec, role="seller")


def leadpack_amount() -> str:
    return os.getenv("LEADPACK_PRICE", "").strip()


def _pending_key(sender: str) -> str:
    return f"payment:{sender}"


def wallet_address(ctx: Context) -> str:
    override = os.getenv("LEADPACK_RECIPIENT", "").strip()
    if override and RECIPIENT_RE.fullmatch(override):
        return override
    if override:
        ctx.logger.warning("LEADPACK_RECIPIENT ignored: not a fetch1… wallet address")
    candidates = [getattr(ctx, "wallet", None), getattr(getattr(ctx, "agent", None), "wallet", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        addr = candidate() if callable(candidate) else candidate
        if hasattr(addr, "address"):
            addr = addr.address() if callable(addr.address) else addr.address
        if isinstance(addr, str) and RECIPIENT_RE.fullmatch(addr):
            return addr
    return str(getattr(getattr(ctx, "agent", None), "address", "") or "")


def pack_rows(camp: dict) -> list[str]:
    leads = {lead["id"]: lead for lead in camp.get("leads", [])}
    fit = {row.get("lead_id"): row.get("fit_score") for row in camp.get("reviews", [])}
    rows = []
    for contact in camp.get("contacts", []):
        lead = leads.get(contact.get("lead_id"), {})
        score = f" · fit {fit[contact.get('lead_id')]}" if fit.get(contact.get("lead_id")) is not None else ""
        rows.append(f"- **{lead.get('name', contact.get('lead_id'))}** · {contact.get('email')} · "
                    f"{contact.get('role', '?')} · source {contact.get('source_url') or contact.get('source', '?')} · "
                    f"{lead.get('website') or 'no website'}{score}")
    return rows


def lead_pack_request(ctx: Context, camp: dict) -> RequestPayment:
    amount = leadpack_amount()
    currency = os.getenv("LEADPACK_CURRENCY", "FET").strip() or "FET"
    method = os.getenv("LEADPACK_PAYMENT_METHOD", "fet_direct").strip() or "fet_direct"
    city = (camp.get("target") or {}).get("city") or "your city"
    return RequestPayment(
        accepted_funds=[Funds(amount=amount, currency=currency, payment_method=method)],
        recipient=wallet_address(ctx), deadline_seconds=DEADLINE_S,
        reference=f"leadpack-{camp.get('campaign_id') or 'none'}-{uuid4().hex[:8]}",
        description=f"Harvest Agent lead pack: {len(camp.get('contacts', []))} published contacts in {city}",
        metadata={"product": "lead_pack", "verification": "DEMO, dry run"},
    )


def lead_pack_card(req: RequestPayment) -> ChatMessage:
    """Chat card with the payment details. Sent before the RequestPayment envelope."""
    funds = req.accepted_funds[0]
    rows = [
        {"label": "Amount", "value": f"{funds.amount} {funds.currency}"},
        {"label": "Recipient", "value": req.recipient or "-"},
        {"label": "Reference", "value": req.reference or "-"},
        {"label": "Verification", "value": "Demo only, no real funds move"},
    ]
    ctas = [{
        "label": "Simulate payment (demo)",
        "selection": {"action": "simulate_payment", "step": "payment"},
        "primary": True,
    }]
    narration = f"**Harvest Agent lead pack**\n{_numbered(rows)}\n\nDemo only, no real funds move."
    return detail_card("Harvest Agent lead pack", rows, ctas, narration)


async def _safe_send(ctx: Context, to: str, msg) -> None:
    try:
        await ctx.send(to, msg)
    except Exception as err:  # noqa: BLE001
        ctx.logger.warning(f"payment send failed: {redact(err)}")


def _pay_audit(ctx: Context, sender: str, action: str, detail: str = "") -> None:
    try:
        camp = load(ctx, sender)
        audit(camp, "payment", action, "system", detail)
        save(ctx, sender, camp)
    except Exception as err:  # noqa: BLE001
        ctx.logger.warning(f"payment audit failed: {redact(err)}")


async def offer(ctx: Context, sender: str, camp: dict) -> None:
    try:
        if not leadpack_amount():
            await ctx.send(sender, chat_text(
                "A Harvest Agent lead pack isn't offered on this demo until LEADPACK_PRICE is set in Agentverse secrets. "
                "Your campaign is unchanged."))
            return
        if not camp.get("contacts"):
            await ctx.send(sender, chat_text(
                "The lead pack is offered once leads and contacts are ready — finish the contacts step first."))
            return
        req = lead_pack_request(ctx, camp)
        funds = req.accepted_funds[0]
        ctx.storage.set(_pending_key(sender), {
            "reference": req.reference, "amount": funds.amount,
            "currency": funds.currency, "recipient": req.recipient,
            "payment_method": funds.payment_method,
        })
        audit(camp, "payment", "requested", "system",
              f"{req.reference} {funds.amount} {funds.currency}")
        # Chat card first. A bounced RequestPayment envelope must not swallow this reply.
        await ctx.send(sender, lead_pack_card(req))
    except Exception as err:  # noqa: BLE001
        ctx.logger.warning(f"payment offer failed: {redact(err)}")
        await _safe_send(ctx, sender, chat_text(
            f"I couldn't create the payment request ({redact(err)}). The free campaign is unaffected."))
        return
    await _safe_send(ctx, sender, req)


def check_commit(msg: CommitPayment, pending: dict | None) -> str:
    if not pending:
        return "no open payment request"
    if not (msg.transaction_id or "").strip():
        return "missing transaction id"
    if msg.reference and msg.reference != pending.get("reference"):
        return "unknown payment reference"
    if msg.funds.currency.upper() != str(pending.get("currency", "")).upper():
        return f"wrong currency {msg.funds.currency}"
    try:
        if Decimal(msg.funds.amount) < Decimal(str(pending.get("amount"))):
            return f"amount {msg.funds.amount} is below the price {pending.get('amount')}"
    except (InvalidOperation, TypeError, ValueError):
        return f"invalid amount {msg.funds.amount!r}"
    if msg.recipient != pending.get("recipient"):
        return "wrong recipient"
    return ""


@pay_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    try:
        pending = ctx.storage.get(_pending_key(sender))
        reason = check_commit(msg, pending if isinstance(pending, dict) else None)
        if reason:
            ctx.logger.info(f"payment {msg.transaction_id} cancelled (DEMO, dry run): {reason}")
            _pay_audit(ctx, sender, "cancelled", f"{msg.transaction_id}: {reason}")
            await _safe_send(ctx, sender, CancelPayment(transaction_id=msg.transaction_id, reason=reason))
            await _safe_send(ctx, sender, chat_text(
                f"Payment not accepted: {reason}. Nothing was charged by me; your campaign is unaffected."))
            return
        ctx.storage.remove(_pending_key(sender))
        ctx.logger.info(f"payment {msg.transaction_id} verified (DEMO, dry run - not checked on-chain)")
        _pay_audit(ctx, sender, "completed (DEMO, dry run)",
                   f"{msg.transaction_id} {msg.funds.amount} {msg.funds.currency}")
        await _safe_send(ctx, sender, CompletePayment(transaction_id=msg.transaction_id))
        rows = pack_rows(load(ctx, sender))
        await _safe_send(ctx, sender, chat_text(
            f"✅ Payment `{msg.transaction_id}` accepted (DEMO, dry run verification). Your **Harvest Agent lead pack** "
            f"({len(rows)} contacts, each with the page it was published on):\n" + ("\n".join(rows) or "none")))
    except Exception:  # noqa: BLE001
        ctx.logger.exception("payment commit failed")
        await _safe_send(ctx, sender, CancelPayment(transaction_id=getattr(msg, "transaction_id", None),
                                                    reason="internal error"))


@pay_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    try:
        ctx.storage.remove(_pending_key(sender))
        ctx.logger.info(f"payment rejected: {msg.reason}")
        _pay_audit(ctx, sender, "rejected", msg.reason or "")
        await _safe_send(ctx, sender, chat_text("No problem, no lead pack. Your free campaign carries on as normal."))
    except Exception:  # noqa: BLE001
        ctx.logger.exception("payment reject failed")


async def simulate_payment(ctx: Context, sender: str) -> None:
    """Demo CommitPayment for clients that drop the RequestPayment envelope. No chain and no funds."""
    pending = ctx.storage.get(_pending_key(sender))
    if not isinstance(pending, dict) or not pending.get("reference"):
        await ctx.send(sender, chat_text("There's no open lead pack request to simulate."))
        return
    commit = CommitPayment(
        funds=Funds(
            amount=str(pending["amount"]),
            currency=str(pending["currency"]),
            payment_method=str(pending.get("payment_method") or "fet_direct"),
        ),
        recipient=str(pending["recipient"]),
        transaction_id=f"demo-{pending['reference']}",
        reference=str(pending["reference"]),
        description="Harvest Agent lead pack",
        metadata={"verification": "DEMO, dry run"},
    )
    await on_commit(ctx, sender, commit)


# ---------- orchestrator ----------

# Remote waits match agents/orchestrator.py so the deployed specialists can finish.
# Inline budgets are shorter (finder 40s, contacts 30s, analyst 45s) for a hosted turn.
SPECIALISTS = {
    "find_leads": ("LEAD_FINDER_AGENT_ADDRESS", 75),
    "find_contacts": ("CONTACT_FINDER_AGENT_ADDRESS", 60),
    "qualify_leads": ("LEAD_ANALYST_AGENT_ADDRESS", 70),
    "draft_emails": ("COPYWRITER_AGENT_ADDRESS", 60),
    "send_emails": ("SENDER_AGENT_ADDRESS", 30),
}
INLINE = {
    "find_leads": find_leads,
    "find_contacts": find_contacts,
    "qualify_leads": qualify_leads,
    "draft_emails": draft_emails,
    "send_emails": send_emails,
}
SPECIALIST_NAMES = {"find_leads": "Lead Finder", "find_contacts": "Contact Finder", "qualify_leads": "Lead Analyst",
                    "draft_emails": "Copywriter", "send_emails": "Sender"}
GATE_TOOL = {"leads": "find_leads", "contacts": "find_contacts", "reviews": "qualify_leads",
             "drafts": "draft_emails", "send": "send_emails"}
PROGRESS = {
    "leads": "Searching OpenStreetMap and the web for matching businesses, then verifying their websites...",
    "contacts": "Visiting each website politely to find published emails and phone numbers...",
    "reviews": "Reviewing each website for evidence that it fits your offer...",
    "drafts": "Drafting a short personal email for each lead...",
    "send": "Queueing approved emails...",
}
FAST_INTENTS = {
    "approve": "approve", "yes": "approve", "ok": "approve", "looks good": "approve",
    "redo": "reject", "search again": "reject", "retry": "retry", "skip": "skip", "pause": "pause", "stop": "pause",
    "resume": "resume", "continue": "resume", "status": "status", "help": "help", "settings": "settings",
    "log": "show_log", "show log": "show_log", "new campaign": "start_campaign", "start": "start_campaign",
    "call list": "call_list",
}
FAST_INTENTS.update(PAY_FAST)
STYLE_RULE_DECLINE = {"no", "just this one", "just this draft", "only this one", "only this draft"}
SKIP_REASONS = {"no_contact": "no email found", "llm_failed": "the writer failed", "timed_out": "lookup timed out"}
MENTION_RE = re.compile(r"^\s*@agent1[a-z0-9]+\s*", re.I)
PACK_WORDS = ("lead pack", "buy pack")
HELP = (
    "I'm **LeadReach**: I find local businesses that fit your brand, their published emails, evidence-based "
    "reasons to contact them, and draft personal emails - stopping for your approval at every step.\n\n"
    "- Start: *\"I sell organic coffee beans - find cafés and bakeries in Jaipur\"*\n"
    "- At a checkpoint: **approve**, **remove 2, 5**, **redo** + what to change, **show lead 3**\n"
    "- Anytime: **status**, **settings**, **pause** / **resume**, **log**\n"
    "- Trust a step? *\"stop asking me about contacts\"*"
)
chat_proto = Protocol(spec=chat_protocol_spec)


def inline_budget(tool: str) -> float:
    if tool == "find_leads":
        return finder_budget()
    if tool == "find_contacts":
        return contact_budget()
    if tool == "qualify_leads":
        return analyst_budget()
    if tool == "draft_emails":
        return draft_llm_timeout() * 2 + 5
    return 5.0


def tool_timeout(tool: str) -> float:
    return env_seconds(f"LEADREACH_TIMEOUT_{tool.upper()}", SPECIALISTS[tool][1])


async def _remote(ctx: Context, tool: str, args: dict, address: str):
    """-> (result, error, timed_out). timed_out means the caller should run the inline tool."""
    timeout = tool_timeout(tool)
    resend = env_seconds("RESEND_WAIT_S", 30)
    rid = uuid4().hex
    req = ToolRequest(tool=tool, args_json=json.dumps({**args, REQUEST_ID_KEY: rid}))
    start = time.monotonic()
    for attempt, wait in (("send", timeout), ("resend", resend)):
        try:
            reply, status = await ctx.send_and_receive(address, req, response_type=ToolResponse, timeout=wait)
        except Exception as err:  # noqa: BLE001
            reply, status = None, f"send error {redact(err)[:200]}"
        took = time.monotonic() - start
        if isinstance(reply, ToolResponse):
            ctx.logger.info(f"{tool} {rid[:8]} response after {took:.1f}s ({attempt}, ok={reply.ok})")
            if not reply.ok:
                return None, reply.error or "unknown error", False
            try:
                return json.loads(reply.result or "{}"), "", False
            except ValueError:
                return None, f"the {tool} agent returned invalid JSON", False
        ctx.logger.warning(f"{tool} {rid[:8]} no response after {took:.1f}s ({attempt}, status {status})")
    return None, "timeout", True


async def _inline_tool(ctx: Context, tool: str, args: dict) -> tuple[dict | None, str]:
    name = SPECIALIST_NAMES[tool]
    limit = inline_budget(tool) + 5
    try:
        raw = await asyncio.wait_for(INLINE[tool](json.loads(json.dumps(args)), ctx), limit)
        return json.loads(json.dumps(raw)), ""
    except Exception as err:  # noqa: BLE001
        why = f"timed out after {limit:g}s" if isinstance(err, asyncio.TimeoutError) else redact(
            f"{type(err).__name__}: {err}")[:200]
        ctx.logger.error(f"fallback: inline {tool} failed: {why}")
        return None, f"{name} timed out (inline run: {why})" if isinstance(err, asyncio.TimeoutError) else why


async def call_tool(ctx: Context, tool: str, args: dict) -> tuple[dict | None, str]:
    # Sending is hard-coded dry-run. The hosted sender replies that send_emails is not
    # implemented, so this step never calls it.
    if tool == "send_emails":
        ctx.logger.info("send_emails uses the inline dry run; the remote sender is not called")
        return await _inline_tool(ctx, tool, args)
    env_name = SPECIALISTS[tool][0]
    address = os.getenv(env_name, "").strip()
    if address:
        result, error, timed_out = await _remote(ctx, tool, args, address)
        if not timed_out:
            return result, error
        ctx.logger.warning(f"fallback: inline {tool} (remote timed out)")
    else:
        ctx.logger.info(f"fallback: inline {tool} (no {env_name})")
    return await _inline_tool(ctx, tool, args)


def dry_run_summary(camp: dict) -> str:
    """User-facing record of a dry run. No error wording."""
    names = {lead.get("id"): lead.get("name") or lead.get("id") for lead in camp.get("leads") or []}
    redirect = os.getenv("TEST_REDIRECT_TO", "").strip()
    lines = ["**Dry run, nothing was sent**", ""]
    for draft in camp.get("drafts") or []:
        if not draft.get("approved"):
            continue
        business = names.get(draft.get("lead_id")) or draft.get("lead_id") or "Unknown business"
        subject = draft.get("subject") or "(no subject)"
        recipient = f"TEST_REDIRECT_TO {redirect}" if redirect else (draft.get("to") or "(no email)")
        lines.append(f"- {business} — {recipient} — {subject}")
    if len(lines) == 2:
        lines.append("- No approved drafts to record.")
    lines += ["", "No email went out. Real sending is off for the demo."]
    return "\n".join(lines)


def _host(url: str | None) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.") if url else ""


def lead_rows(leads: list[dict]) -> list[dict]:
    rows = []
    for index, lead in enumerate(leads, 1):
        bits = [_host(lead.get("website")) or "no website"]
        if lead.get("website") and lead.get("verified") is False:
            bits[0] += " (unverified)"
        if lead.get("osm_emails"):
            bits.append("email on OSM")
        if lead.get("phone"):
            bits.append("phone")
        bits.append(str(lead.get("source", "?")).upper())
        rows.append({"label": f"{index}. {lead['name']}", "value": " · ".join(bits)})
    return rows


def checkpoint(gate: str, camp: dict, note: str = "") -> ChatMessage:
    if gate == "icp":
        icp = camp["icp"]
        rows = [
            {"label": "Product", "value": icp.get("product") or "-"},
            {"label": "Target businesses", "value": ", ".join(icp.get("business_types", [])) or "-"},
            {"label": "City", "value": icp.get("city") or "-"},
            {"label": "Web searches", "value": "; ".join(icp.get("search_queries", [])) or "-"},
            {"label": "Good-fit signs", "value": "; ".join(icp.get("fit_criteria", [])) or "-"},
        ]
        camp["last_list"], camp["last_list_names"] = [], []
        return review_card("Who should I look for?", rows, "icp", "Approve & find leads", "Change it",
                           intro="Here's the customer profile I'll search for.")
    if gate == "leads":
        leads = camp["leads"]
        camp["last_list"] = [lead["id"] for lead in leads]
        camp["last_list_names"] = [lead["name"] for lead in leads]
        extra = (f"{len(camp['call_list'])} more businesses have only a phone (say **call list**)."
                 if camp.get("call_list") else "")
        return review_card(f"{len(leads)} leads in {camp['icp']['city']}", lead_rows(leads), "leads",
                           "Approve & find contacts", "Search again",
                           intro="\n".join(camp.get("source_notes", [])), extra_hint=extra)
    if gate == "contacts":
        names = {lead["id"]: lead["name"] for lead in camp["leads"]}
        rows = [{"label": f"{i}. {names.get(c['lead_id'], c['lead_id'])}",
                 "value": f"{c['email']} · {c.get('role', '?')} · "
                          f"{'⚠ low' if c.get('confidence') == 'low' else c.get('confidence', '?')}"}
                for i, c in enumerate(camp["contacts"], 1)]
        camp["last_list"] = [c["lead_id"] for c in camp["contacts"]]
        return review_card(f"{len(rows)} contacts found", rows, "contacts", "Approve & review websites")
    if gate == "reviews":
        names = {lead["id"]: lead["name"] for lead in camp["leads"]}
        rows = []
        for index, review in enumerate(camp["reviews"], 1):
            hook = (review.get("hooks") or [{}])[0].get("observation", "no hook")
            rows.append({"label": f"{index}. {names.get(review['lead_id'], review['lead_id'])}",
                         "value": f"fit {review.get('fit_score', '?')} · {hook}"})
        camp["last_list"] = [review["lead_id"] for review in camp["reviews"]]
        return review_card("Lead reviews", rows, "reviews", "Approve & draft emails",
                           extra_hint="Say **show lead 2** for the evidence.")
    if gate == "drafts":
        index = camp["draft_cursor"]
        if not note and index == 0 and camp.get("draft_skipped"):
            names = {lead["id"]: lead["name"] for lead in camp["leads"]}
            note = "No draft for: " + "; ".join(
                f"{names.get(item['lead_id'], item['lead_id'])} ({SKIP_REASONS.get(item.get('reason'), item.get('reason'))})"
                for item in camp["draft_skipped"]) + "."
        return draft_card(camp["drafts"][index], index + 1, len(camp["drafts"]), note)
    settings = camp["settings"]
    rows = [{"label": "Approved drafts", "value": str(sum(1 for d in camp["drafts"] if d.get("approved")))},
            {"label": "Dry run", "value": "on"},
            {"label": "Redirect", "value": os.getenv("TEST_REDIRECT_TO") or "none"},
            {"label": "Daily cap", "value": str(settings["daily_cap"])}]
    return review_card("Ready to send", rows, "send", "Send", "Not yet")


def step_args(gate: str, camp: dict) -> dict:
    if gate == "leads":
        return {"icp": camp["icp"], "limit": camp["target"]["limit"], "exclude_ids": camp["removed_ids"]}
    if gate == "contacts":
        return {"leads": camp["leads"], "max_pages_per_site": 3}
    if gate == "reviews":
        return {"icp": camp["icp"], "leads": camp["leads"], "contacts": camp["contacts"]}
    if gate == "drafts":
        return {"profile": load_profile(), "brief": camp["brief"], "style_rules": camp["style_rules"], "icp": camp["icp"],
                "leads": camp["leads"], "contacts": camp["contacts"], "reviews": camp["reviews"]}
    return {"campaign_id": camp["campaign_id"], "drafts": [d for d in camp["drafts"] if d.get("approved")],
            "dry_run": True, "redirect_to": os.getenv("TEST_REDIRECT_TO", ""),
            "daily_cap": camp["settings"]["daily_cap"]}


def store_result(gate: str, camp: dict, result: dict) -> int:
    if gate == "leads":
        camp["leads"], camp["call_list"] = result.get("leads", []), result.get("call_list", [])
        camp["source_notes"] = result.get("source_notes", [])
        return len(camp["leads"])
    if gate == "contacts":
        camp["contacts"] = result.get("contacts", [])
        return len(camp["contacts"])
    if gate == "reviews":
        camp["reviews"] = result.get("reviews", [])
        return len(camp["reviews"])
    if gate == "drafts":
        camp["drafts"], camp["draft_cursor"] = result.get("drafts", []), 0
        camp["draft_skipped"], camp["pending_style_rule"] = result.get("skipped", []), None
        return len(camp["drafts"])
    camp["send_result"] = result
    return len(result.get("sent", [])) + len(result.get("queued", [])) + len(result.get("would_send", []))


def next_gate(gate: str) -> str | None:
    index = GATE_ORDER.index(gate)
    return GATE_ORDER[index + 1] if index + 1 < len(GATE_ORDER) else None


def log_draft_skips(ctx: Context, result: dict) -> None:
    for item in result.get("skipped") or []:
        detail = f": {redact(item['detail'])}" if item.get("detail") else ""
        ctx.logger.warning(f"no draft for {item.get('lead_id')} ({item.get('reason')}){detail}")


async def build_icp_step(ctx: Context, sender: str, camp: dict) -> None:
    camp["state"] = BUILDING_ICP
    try:
        camp["icp"] = await build_icp(camp["brief"], camp["target"]["city"], load_profile())
    except Exception as err:  # noqa: BLE001
        camp["state"], camp["failed_gate"] = STEP_FAILED, "icp"
        audit(camp, "icp", "failed", "system", redact(err))
        await ctx.send(sender, chat_text(f"⚠ I couldn't understand the brief right now ({redact(err)}). Reply **retry**."))
        return
    audit(camp, "icp", "built", "system", json.dumps(camp["icp"])[:300])


async def run_from(ctx: Context, sender: str, camp: dict, gate: str) -> None:
    while gate:
        if gate == "icp":
            await build_icp_step(ctx, sender, camp)
            if camp["state"] != BUILDING_ICP:
                return
        else:
            camp["state"] = GATE_STATES[gate][0]
            save(ctx, sender, camp)
            if gate != "send":
                await ctx.send(sender, chat_text(PROGRESS[gate]))
            result, error = await call_tool(ctx, GATE_TOOL[gate], step_args(gate, camp))
            if gate == "send" and result is None:
                # A remote "not implemented" reply must not reach the user. Record the dry run here.
                ctx.logger.info("send step returned no result; recording the inline dry run instead")
                result, error = await _inline_tool(ctx, "send_emails", step_args(gate, camp))
            if result is None:
                camp["state"], camp["failed_gate"] = STEP_FAILED, gate
                audit(camp, gate, "failed", "system", error)
                name = SPECIALIST_NAMES[GATE_TOOL[gate]]
                await ctx.send(sender, chat_text(
                    f"⚠ {name} timed out — reply **retry** to try again, or **skip** to move on."
                    if error.startswith(f"{name} timed out") else
                    f"⚠ The **{gate}** step failed: {error}\n\nReply **retry** to try again, or **skip** to move on."))
                return
            if gate == "drafts":
                log_draft_skips(ctx, result)
            count = store_result(gate, camp, result)
            audit(camp, gate, f"produced {count}", "system")
            if gate == "send":
                camp["state"] = DONE
                audit(camp, "send", "dry run recorded", "system")
                await ctx.send(sender, chat_text(dry_run_summary(camp), end_session=True))
                return
            if gate in ("leads", "drafts") and count == 0:
                camp["state"] = GATE_STATES[gate][1]
                await ctx.send(sender, chat_text(
                    f"I found nothing for the **{gate}** step. Reply **redo** with what to change "
                    "(another city, more business types), or **add lead** with a name and website."))
                return
        if camp["settings"]["ask_before"].get(gate, True):
            camp["state"] = GATE_STATES[gate][1]
            await ctx.send(sender, checkpoint(gate, camp))
            return
        audit(camp, gate, "auto-approved", "ai_auto")
        await ctx.send(sender, chat_text(f"FYI: **{gate}** auto-approved (you turned that checkpoint off)."))
        gate = next_gate(gate)
    camp["state"] = DONE
    await ctx.send(sender, chat_text("Campaign complete. Say **status** for a summary or **new campaign**.",
                                     end_session=True))


async def start_campaign(ctx: Context, sender: str, camp: dict, args: dict) -> dict:
    brief = (args.get("brief") or "").strip()
    city = (args.get("city") or "").strip()
    if not brief or not city:
        camp["state"] = SETUP
        default_city = load_profile().get("targeting", {}).get("default_city")
        await ctx.send(sender, setup_form(None if str(default_city).startswith("❓") else default_city))
        return camp
    if over_cap(ctx, sender):
        await ctx.send(sender, chat_text(LIMIT_MSG))
        return camp
    bump_cap(ctx, sender)
    camp = new_campaign(camp)
    camp["brief"], camp["target"]["city"] = brief, city
    if args.get("limit"):
        camp["target"]["limit"] = max(1, min(int(float(args["limit"])), 15))
    if args.get("language"):
        camp["target"]["language"] = args["language"]
    audit(camp, "setup", "campaign started", "user", f"{brief} | {city}")
    await run_from(ctx, sender, camp, "icp")
    return camp


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "checked"}


async def redraft(ctx: Context, sender: str, camp: dict, lead_ids: list[str], extra_rules: list[str]) -> str:
    ids = set(lead_ids)
    args = dict(step_args("drafts", camp), leads=[lead for lead in camp["leads"] if lead["id"] in ids],
                style_rules=camp["style_rules"] + extra_rules)
    if not args["leads"]:
        return "those leads are no longer in the campaign"
    await ctx.send(sender, chat_text("Rewriting..."))
    result, error = await call_tool(ctx, "draft_emails", args)
    if result is None:
        return error
    log_draft_skips(ctx, result)
    new = {d["lead_id"]: d for d in result.get("drafts", []) if d.get("lead_id") in ids}
    camp["drafts"] = [new.get(d["lead_id"], d) for d in camp["drafts"]]
    return "" if new else "the writer returned no draft"


async def answer_style_rule(ctx: Context, sender: str, camp: dict, args: dict, accepted: bool) -> bool:
    rule = camp.get("pending_style_rule")
    if (not rule or pending_gate(camp) != "drafts" or args.get("all")
            or args.get("step") not in (None, "", "style_rule")):
        camp["pending_style_rule"] = None
        return False
    camp["pending_style_rule"] = None
    if accepted:
        if rule not in camp["style_rules"]:
            camp["style_rules"].append(rule)
        audit(camp, "drafts", "style rule added", "user", rule)
        todo = [d["lead_id"] for d in camp["drafts"][camp["draft_cursor"]:] if not d.get("approved")]
        error = await redraft(ctx, sender, camp, todo, []) if todo else ""
        note = f"Saved style rule *\"{rule}\"*" + (f", but I couldn't rewrite the drafts ({error})." if error
                                                   else f" and rewrote {len(todo)} draft(s).")
    else:
        current = camp["drafts"][camp["draft_cursor"]]["lead_id"]
        audit(camp, "drafts", f"edited {current}", "user", rule)
        error = await redraft(ctx, sender, camp, [current], [rule])
        note = f"⚠ I couldn't rewrite it ({error})." if error else f"Applied *\"{rule}\"* to this draft only."
    await ctx.send(sender, checkpoint("drafts", camp, note))
    return True


async def approve_cursor_advance(ctx: Context, sender: str, camp: dict) -> None:
    if camp["draft_cursor"] + 1 < len(camp["drafts"]):
        camp["draft_cursor"] += 1
        await ctx.send(sender, checkpoint("drafts", camp))
    else:
        await run_from(ctx, sender, camp, "send")


async def approve(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = pending_gate(camp)
    if gate is None:
        await ctx.send(sender, chat_text("There's nothing waiting for approval. Say **status** or **help**."))
        return
    if await answer_style_rule(ctx, sender, camp, args, accepted=True):
        return
    if gate == "drafts" and camp["drafts"]:
        todo = camp["drafts"][camp["draft_cursor"]:] if args.get("all") else [camp["drafts"][camp["draft_cursor"]]]
        for draft in todo:
            draft["approved"] = True
        audit(camp, "drafts", f"approved {len(todo)}")
        if args.get("all"):
            camp["draft_cursor"] = len(camp["drafts"]) - 1
        if camp["draft_cursor"] + 1 < len(camp["drafts"]):
            camp["draft_cursor"] += 1
            await ctx.send(sender, checkpoint("drafts", camp))
            return
    else:
        audit(camp, gate, "approved")
    nxt = next_gate(gate)
    if nxt:
        await run_from(ctx, sender, camp, nxt)


async def reject(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = pending_gate(camp)
    if gate is None:
        await ctx.send(sender, chat_text("Nothing to redo right now. Say **status** or **help**."))
        return
    if await answer_style_rule(ctx, sender, camp, args, accepted=False):
        return
    if gate == "drafts" and camp["drafts"]:
        camp["drafts"][camp["draft_cursor"]]["approved"] = False
        audit(camp, "drafts", f"skipped {camp['drafts'][camp['draft_cursor']].get('lead_id')}")
        await approve_cursor_advance(ctx, sender, camp)
        return
    redo = camp["redo_count"].get(gate, 0)
    if redo >= MAX_REDOS:
        await ctx.send(sender, chat_text(
            f"I've redone **{gate}** {redo} times. Try changing the inputs: a different city or business type, "
            "or start a **new campaign**."))
        return
    camp["redo_count"][gate] = redo + 1
    feedback = (args.get("feedback") or "").strip()
    audit(camp, gate, "rejected", "user", feedback)
    if gate == "icp" and feedback:
        camp["brief"] += f"\nUser feedback on the profile: {feedback}"
    if gate == "leads":
        if not feedback:
            camp["removed_ids"] = list(set(camp["removed_ids"]) | {lead["id"] for lead in camp["leads"]})
        elif camp.get("icp"):
            camp["brief"] += f"\nUser feedback on the leads: {feedback}"
            await run_from(ctx, sender, camp, "icp")
            return
    await run_from(ctx, sender, camp, gate)


async def edit_draft(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    instruction = str(args.get("instruction") or "").strip()
    if pending_gate(camp) != "drafts" or not camp.get("drafts"):
        await ctx.send(sender, chat_text("I can edit emails once the drafts are ready (at the drafts checkpoint)."))
        return
    if not instruction:
        await ctx.send(sender, chat_text("How should I change it? e.g. *\"shorter and warmer\"*."))
        return
    general = args.get("general")
    if general is True or (not isinstance(general, bool) and _truthy(general)):
        camp["pending_style_rule"] = instruction
        audit(camp, "drafts", "style rule proposed", "system", instruction)
        await ctx.send(sender, style_rule_card(instruction))
        return
    lead_ids = [d["lead_id"] for d in camp["drafts"]]
    if args.get("lead_id") in lead_ids:
        camp["draft_cursor"] = lead_ids.index(args["lead_id"])
    current = lead_ids[camp["draft_cursor"]]
    audit(camp, "drafts", f"edited {current}", "user", instruction)
    error = await redraft(ctx, sender, camp, [current], [instruction])
    note = (f"⚠ I couldn't rewrite it ({error}); here's the previous version." if error
            else f"Rewrote this draft: *\"{instruction}\"*.")
    await ctx.send(sender, checkpoint("drafts", camp, note))


async def remove_items(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = pending_gate(camp)
    positions = [int(i) for i in args.get("ids", []) if str(i).isdigit()]
    ids = {camp["last_list"][p - 1] for p in positions if 0 < p <= len(camp.get("last_list") or [])}
    if gate not in ("leads", "contacts", "reviews") or not ids:
        await ctx.send(sender, chat_text("Tell me which numbers to remove from the last list, e.g. **remove 2, 5**."))
        return
    camp["removed_ids"] = list(set(camp["removed_ids"]) | ids)
    for key in ("leads", "contacts", "reviews"):
        camp[key] = [item for item in camp[key] if item.get("id", item.get("lead_id")) not in ids]
    audit(camp, gate, f"removed {len(ids)}", "user", ", ".join(sorted(ids)))
    await ctx.send(sender, checkpoint(gate, camp))


async def show_lead(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    ids = args.get("ids") or []
    lead_ids = camp.get("last_list") or [lead["id"] for lead in camp.get("leads") or []]
    if not ids or not str(ids[0]).isdigit() or not 0 < int(ids[0]) <= len(lead_ids):
        await ctx.send(sender, chat_text("Which one? e.g. **show lead 2**."))
        return
    lead_id = lead_ids[int(ids[0]) - 1]
    lead = next((item for item in camp.get("leads") or [] if item["id"] == lead_id), {"name": lead_id})
    review = next((item for item in camp.get("reviews") or [] if item.get("lead_id") == lead_id), {})
    rows = [{"label": "Address", "value": lead.get("address") or "-"},
            {"label": "Website", "value": lead.get("website") or "-"},
            {"label": "Phone", "value": lead.get("phone") or "-"},
            {"label": "Source", "value": lead.get("source_ref") or lead.get("source", "-")}]
    if review:
        rows.append({"label": "Fit score", "value": str(review.get("fit_score", "-"))})
    rows += [{"label": hook.get("observation", "hook"),
              "value": f"\"{hook.get('evidence')}\" ({hook.get('evidence_url')})"}
             for hook in review.get("hooks", [])]
    narration = f"**{lead['name']}**\n" + "\n".join(f"- {row['label']}: {row['value']}" for row in rows)
    await ctx.send(sender, detail_card(lead["name"], rows, [], narration, terminal=True))


async def update_settings(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    settings, changes = camp["settings"], []
    asks = dict(args.get("ask_before") or {})
    asks.update({key[4:]: value for key, value in args.items() if key.startswith("ask_")})
    for gate, value in asks.items():
        if gate in settings["ask_before"]:
            settings["ask_before"][gate] = value if isinstance(value, bool) else _truthy(value)
            changes.append(f"{gate} will {'ask you first' if settings['ask_before'][gate] else 'auto-approve'}")
    if args.get("daily_cap") not in (None, ""):
        settings["daily_cap"] = max(1, min(int(float(args["daily_cap"])), 50))
        changes.append(f"daily cap {settings['daily_cap']}")
    if "dry_run" in args:
        settings["dry_run"] = True
        changes.append("dry run stays on (this hosted demo never sends email)")
    if not changes:
        await ctx.send(sender, settings_form(settings))
        return
    audit(camp, "settings", "; ".join(changes))
    await ctx.send(sender, chat_text("Done: " + "; ".join(changes) + "."))


async def clarify(ctx: Context, sender: str, camp: dict) -> None:
    gate = pending_gate(camp)
    options = {
        None: "start a campaign (e.g. *\"find cafés in Jaipur for my coffee brand\"*), **status** or **help**",
        "drafts": "**approve**, **skip**, **approve all**, or tell me how to edit the draft",
    }.get(gate, "**approve**, **remove 2, 5**, **redo** with feedback, or **show lead 3**")
    await ctx.send(sender, chat_text(f"Sorry, I didn't catch that. Here you can: {options}."))


def fast_command(text: str, camp: dict) -> dict | None:
    """Intent with no LLM call. None means parse_intent would have to run."""
    text = MENTION_RE.sub("", text, count=1).strip()
    if cmd := parse_card_action(text):
        return cmd
    low = text.lower().strip(" .!")
    if camp.get("pending_style_rule") and low in STYLE_RULE_DECLINE:
        return {"intent": "reject", "args": {"step": "style_rule"}, "confidence": 1.0}
    if low == "approve all":
        return {"intent": "approve", "args": {"all": True}, "confidence": 1.0}
    if low in FAST_INTENTS:
        return {"intent": FAST_INTENTS[low], "args": {}, "confidence": 1.0}
    matched = re.fullmatch(r"(remove|drop|delete)\s+([\d,\sand]+)", low)
    if matched:
        return {"intent": "remove_items", "args": {"ids": [int(x) for x in re.findall(r"\d+", matched.group(2))]},
                "confidence": 1.0}
    matched = re.fullmatch(r"show (?:lead|review)\s+(\d+)", low)
    if matched:
        return {"intent": "show_lead", "args": {"ids": [int(matched.group(1))]}, "confidence": 1.0}
    if any(word in low for word in PACK_WORDS):
        return {"intent": "buy_lead_pack", "args": {}, "confidence": 1.0}
    return None


def parse_card_action(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("action"):
        args = {key: value for key, value in data.items() if key != "action"}
        return {"intent": data["action"], "args": args, "confidence": 1.0}
    return None


async def dispatch(ctx: Context, sender: str, camp: dict, cmd: dict) -> dict:
    intent, args = cmd["intent"], cmd.get("args") or {}
    if camp["state"] == PAUSED and intent not in ("resume", "status", "help", "show_log", "update_settings"):
        await ctx.send(sender, chat_text("Everything is paused. Say **resume** to continue."))
        return camp
    if intent == "help":
        await ctx.send(sender, chat_text(HELP))
    elif intent == "status":
        await ctx.send(sender, status_card(camp))
    elif intent == "show_log":
        lines = [f"- {e['ts'][5:16]} · {e['step']} · {e['action']} ({e['actor']})" for e in camp["log"][-10:]]
        await ctx.send(sender, chat_text("**Audit log (last 10)**\n" + ("\n".join(lines) or "empty")))
    elif intent == "settings" or (intent == "update_settings" and not args):
        await ctx.send(sender, settings_form(camp["settings"]))
    elif intent == "update_settings":
        await update_settings(ctx, sender, camp, args)
    elif intent == "pause":
        if camp["state"] != PAUSED:
            camp["paused_from"], camp["state"] = camp["state"], PAUSED
            audit(camp, "campaign", "paused")
        await ctx.send(sender, chat_text("⏸ Paused. Nothing will run or send until you say **resume**."))
    elif intent == "resume":
        if camp["state"] == PAUSED:
            camp["state"], camp["paused_from"] = camp["paused_from"] or IDLE, None
            audit(camp, "campaign", "resumed")
            gate = pending_gate(camp)
            await ctx.send(sender, checkpoint(gate, camp) if gate else chat_text("▶ Resumed."))
        else:
            await ctx.send(sender, chat_text("Nothing is paused."))
    elif intent == "start_campaign":
        camp = await start_campaign(ctx, sender, camp, args)
    elif camp["state"] == STEP_FAILED and intent in ("retry", "approve"):
        await run_from(ctx, sender, camp, camp.get("failed_gate") or "icp")
    elif camp["state"] == STEP_FAILED and intent == "skip":
        gate = camp.get("failed_gate") or "icp"
        audit(camp, gate, "skipped after failure")
        nxt = next_gate(gate)
        if nxt:
            await run_from(ctx, sender, camp, nxt)
        else:
            await ctx.send(sender, chat_text("Nothing left to skip to."))
    elif intent == "approve":
        await approve(ctx, sender, camp, args)
    elif intent == "reject":
        await reject(ctx, sender, camp, args)
    elif intent == "remove_items":
        await remove_items(ctx, sender, camp, args)
    elif intent == "show_lead":
        await show_lead(ctx, sender, camp, args)
    elif intent == "call_list":
        rows = [f"- {item['name']} · {item.get('phone')} · {item.get('address') or ''}" for item in camp.get("call_list") or []]
        await ctx.send(sender, chat_text("**Phone-only businesses** (no published email)\n" + ("\n".join(rows) or "none")))
    elif intent == "edit_draft":
        await edit_draft(ctx, sender, camp, args)
    elif intent == "buy_lead_pack":
        await offer(ctx, sender, camp)
    elif intent == "simulate_payment":
        await simulate_payment(ctx, sender)
    elif intent in ("add_lead", "import_list", "classify_reply"):
        await ctx.send(sender, chat_text(f"*{intent.replace('_', ' ')}* isn't part of this demo yet. {HELP}"))
    elif camp["state"] in (IDLE, SETUP, DONE) and cmd.get("confidence", 0) < 0.6:
        await ctx.send(sender, chat_text(HELP))
    else:
        await clarify(ctx, sender, camp)
    return camp


def cap_blocks(camp: dict, fast: dict | None) -> bool:
    """True when this message must not call the LLM and must not start a campaign."""
    intent = (fast or {}).get("intent")
    if campaign_active(camp) and intent != "start_campaign":
        return False
    if intent == "start_campaign":
        return True
    return fast is None


@chat_proto.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ack(msg))
    text = text_of(msg)
    if not text:
        return
    ctx.logger.info(f"user {sender[:16]}...: {text[:200]}")
    camp = load(ctx, sender)
    try:
        fast = fast_command(text, camp)
        if over_cap(ctx, sender) and cap_blocks(camp, fast):
            await ctx.send(sender, chat_text(LIMIT_MSG))
            return
        if fast is None:
            cmd = await parse_intent(text, camp["state"], pending_gate(camp), camp.get("last_list_names") or [])
        else:
            cmd = fast
        ctx.logger.info(f"intent: {cmd.get('intent')}")
        camp = await dispatch(ctx, sender, camp, cmd)
    except Exception:  # noqa: BLE001
        ctx.logger.exception("handler failed")
        await ctx.send(sender, chat_text(
            "Something went wrong on my side. Your campaign is saved - say **status** to see where we are, or **retry**."))
    finally:
        save(ctx, sender, camp)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    return None


agent.include(chat_proto, publish_manifest=True)
agent.include(pay_proto, publish_manifest=True)


@agent.on_event("startup")
async def show_address(ctx: Context):
    addr = getattr(getattr(ctx, "agent", None), "address", None) or getattr(agent, "address", "")
    ctx.logger.info(f"Harvest Agent address: {addr}")
    missing = [env for env, _ in SPECIALISTS.values() if not os.getenv(env, "").strip()]
    if missing:
        ctx.logger.info(f"Specialist addresses unset ({', '.join(missing)}); those steps run in-process.")
