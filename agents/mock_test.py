"""Offline end-to-end test: all agents + a scripted fake ASI:One user in one Bureau, with the
ASI:One LLM, OpenStreetMap and website fetching mocked. No keys or network needed.

    .venv/bin/python agents/mock_test.py      # exit code 0 = pass
"""

import asyncio
import json
import os
import threading

# must be set before the agents are imported
os.environ["MAILBOX"] = "false"
os.environ["OWNER_MODE"] = "false"
os.environ["ASI1_API_KEY"] = "test-key"
for _seed in ("ORCH", "FINDER", "CONTACT", "ANALYST", "WRITER", "SENDER"):
    os.environ[f"{_seed}_SEED"] = f"leadreach mock test seed {_seed.lower()} 0123456789"
os.environ.update({f"{p}_PORT": str(8101 + i) for i, p in enumerate(("ORCH", "FINDER", "CONTACT",
                                                                       "ANALYST", "WRITER", "SENDER"))})

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, MetadataContent  # noqa: E402

import contact_finder  # noqa: E402
import copywriter  # noqa: E402
import geo  # noqa: E402
import lead_analyst  # noqa: E402
import lead_finder  # noqa: E402
import llm  # noqa: E402
import orchestrator  # noqa: E402
import sender  # noqa: E402
import state as st  # noqa: E402
from common import chat_text, text_of  # noqa: E402
from crawl import Page, domain_of  # noqa: E402

os.environ.update({
    "LEAD_FINDER_AGENT_ADDRESS": lead_finder.agent.address,
    "CONTACT_FINDER_AGENT_ADDRESS": contact_finder.agent.address,
    "LEAD_ANALYST_AGENT_ADDRESS": lead_analyst.agent.address,
    "COPYWRITER_AGENT_ADDRESS": copywriter.agent.address,
    "SENDER_AGENT_ADDRESS": sender.agent.address,
})

# ---------------- mocks ----------------

ICP = {"product": "organic coffee beans", "business_types": ["cafe", "bakery"], "osm_tags": ["amenity=cafe"],
       "search_queries": ["specialty cafes in Jaipur"], "fit_criteria": ["brews espresso"], "city": "Jaipur",
       "radius_km": 10}
OSM_LEADS = [
    {"id": "osm:node/1", "name": "Tapri Central", "category": "amenity=cafe", "address": "C-Scheme", "lat": 1, "lon": 1,
     "website": "https://tapri.example.in", "osm_emails": [], "phone": None, "source": "osm",
     "source_ref": "https://www.openstreetmap.org/node/1"},
    {"id": "osm:node/2", "name": "Curious Life Coffee", "category": "amenity=cafe", "address": None, "lat": 1, "lon": 1,
     "website": None, "osm_emails": ["hello@curious.example.in"], "phone": None, "source": "osm",
     "source_ref": "https://www.openstreetmap.org/node/2"},
    {"id": "osm:node/3", "name": "Chai Point Corner", "category": "amenity=cafe", "address": None, "lat": 1, "lon": 1,
     "website": None, "osm_emails": [], "phone": "+91 98290 00000", "source": "osm",
     "source_ref": "https://www.openstreetmap.org/node/3"},
]
WEB_FOUND = [{"name": "Brew Lab Roasters", "website": "https://brewlab.test", "area": "Malviya Nagar",
              "evidence_url": "https://search.example/brewlab"},
             {"name": "Ghost Cafe", "website": "https://ghost.example.in", "area": None,
              "evidence_url": "https://search.example/ghost"}]


async def fake_parse_intent(text, state, gate, numbered):
    t = text.lower()
    if "sell" in t:
        return {"intent": "start_campaign", "args": {"brief": text, "city": "Jaipur"}, "confidence": 0.95}
    if "stop asking" in t:
        return {"intent": "update_settings", "args": {"ask_before": {"contacts": False}}, "confidence": 0.9}
    if t.startswith(("make", "never")):  # "make this one ..." = this draft; "make them ..." / "never ..." = general
        return {"intent": "edit_draft", "args": {"instruction": text, "general": "this one" not in t},
                "confidence": 0.9}
    return {"intent": "unknown", "args": {}, "confidence": 0.2}


async def fake_build_icp(brief, city, profile):
    return dict(ICP, city=city)


async def fake_discover(icp, query):
    return WEB_FOUND


async def fake_find_websites(businesses, city):
    return {}


async def fake_geocode(city, country="India"):
    return {"name": f"{city}, Rajasthan, India", "lat": 26.9, "lon": 75.8, "bbox": [26.7, 27.0, 75.6, 75.9]}


async def fake_find_businesses(tags, bbox):
    return json.loads(json.dumps(OSM_LEADS))


def _page(url: str, text: str, links: list[str] | None = None) -> Page:
    return Page(url, 200, f"<html>{text}</html>", 100, text=text, links=links or [])


# domain -> pages the fake crawler "finds" (reserved .test TLD; crawl drops example.* emails as placeholders)
SITES = {
    "brewlab.test": [
        _page("https://brewlab.test/", "Brew Lab Roasters. Write to owner@brewlab.test or hello [at] brewlab [dot] test",
              ["mailto:Owner@brewlab.test", "https://brewlab.test/contact"]),
        _page("https://brewlab.test/contact", "Orders: orders@brewlab.test, call +91 98290 11111. "
                                              "Website by studio@webcraft.test"),
    ],
    "beanhouse.test": [_page("https://beanhouse.test/", "Bean House. Email beanhouse.jaipur@gmail.com")],
    "quiet.test": [_page("https://quiet.test/", "We roast quietly. Visit us.")],
    "deadmx.test": [_page("https://deadmx.test/", "info@deadmx.test")],
    "linkhub.test": [_page("https://www.instagram.com/somecafe", "Follow us")],  # redirects to a banned host
    "roastery.test": [
        Page("https://roastery.test/", 200,
             '<html><head><title>Bean and Bloom Cafe</title><meta name="viewport" content="width=device-width">'
             '<meta name="description" content="All-day cafe in C-Scheme, Jaipur"></head></html>', 90,
             text="Bean & Bloom Cafe. We brew espresso and pour-over all day. See our menu. © 2026",
             links=["https://roastery.test/about"]),
        _page("https://roastery.test/about", "Opened our second outlet in Malviya Nagar in 2026. "
                                             "Weekend latte art workshops."),
    ],
}
ROBOTS_BLOCKED = {"private.test"}
crawl_stats = {"active": 0, "peak": 0}
mx_calls: list[str] = []


class FakeFetcher:
    """Brew Lab's site exists and names the business; Ghost Cafe's URL is a dead LLM suggestion."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def fetch(self, url):
        if "brewlab" in url:
            return Page(url, 200, "<html>Brew Lab Roasters</html>", 120, text="Welcome to Brew Lab Roasters, Jaipur")
        raise ConnectionError("unreachable")

    async def allowed(self, url):
        return domain_of(url) not in ROBOTS_BLOCKED

    async def crawl_site(self, url, max_pages=5):
        crawl_stats["active"] += 1
        crawl_stats["peak"] = max(crawl_stats["peak"], crawl_stats["active"])
        try:
            await asyncio.sleep(0.05)
            domain = domain_of(url)
            if domain.startswith("slow"):
                return [_page(url, "no contact details")]
            if domain not in SITES:
                raise ConnectionError("unreachable")
            return SITES[domain][:max_pages]
        finally:
            crawl_stats["active"] -= 1


def fake_mx_ok(domain):
    mx_calls.append(domain)
    return domain != "deadmx.test"


qualify_calls: list[dict] = []


async def fake_qualify(icp, lead, signals, page_text):
    """One hook backed by the crawled page, one invented (must be dropped)."""
    qualify_calls.append({"lead": lead["id"], "signals": signals, "page_text": page_text})
    return {"fit_score": 81, "summary": "Roastery-cafe that takes direct orders.",
            "hooks": [{"observation": "Takes orders by email", "evidence": "Orders: orders@brewlab.test",
                       "evidence_url": "https://brewlab.test/contact", "angle": "Easy to pitch a wholesale trial"},
                      {"observation": "Opened a second outlet", "evidence": "now open in Vaishali Nagar",
                       "evidence_url": "https://brewlab.test/", "angle": "More outlets, more beans"}]}


UNSUB = "If you'd prefer not to hear from me again, just reply 'unsubscribe'."
PROFILE = {"brand": {"name": "Bean Co", "sender_name": "Asha"}, "offer": {"product": "organic coffee beans"},
           "pricing": {"mention_price": False}, "cta": {"text": "Free sample?"},
           "email": {"max_words": 60, "unsubscribe_line": UNSUB}}
draft_calls: list[dict] = []


async def fake_draft_email(profile, lead, qualification, style_rules, problems=None):
    """A valid draft for PROFILE; the body echoes the style rules so tests can see they arrived."""
    draft_calls.append({"lead": lead["id"], "style_rules": list(style_rules), "problems": problems})
    hooks = qualification.get("hooks") or []
    style = f" ({'; '.join(style_rules)})" if style_rules else ""
    body = (f"Hi {lead['name']} team, I noticed you take orders by email{style}. We roast organic beans locally. "
            f"Free sample?\n\nAsha, Bean Co\n{UNSUB}")
    return {"subject": "Fresh beans for your cafe", "body_text": body,
            "hook_used": hooks[0]["observation"] if hooks else None}


original_load_profile = orchestrator.load_profile
orchestrator.load_profile = lambda: PROFILE
llm.draft_email = fake_draft_email
llm.parse_intent = fake_parse_intent
llm.qualify = fake_qualify
llm.build_icp = fake_build_icp
llm.discover_businesses = fake_discover
llm.find_websites = fake_find_websites
geo.geocode = fake_geocode
geo.find_businesses = fake_find_businesses
lead_finder.PoliteFetcher = FakeFetcher
contact_finder.PoliteFetcher = FakeFetcher
contact_finder.mx_ok = fake_mx_ok
lead_analyst.PoliteFetcher = FakeFetcher

# ---------------- scripted user ----------------

user = Agent(name="fake-asi1-user", seed="leadreach mock test user seed 0123456789", port=8110)
inbox: asyncio.Queue = asyncio.Queue()
acks = 0
failures: list[str] = []


def check(cond: bool, label: str):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        failures.append(label)


def card_meta(msg: ChatMessage) -> dict | None:
    for c in msg.content:
        if isinstance(c, MetadataContent):
            return c.metadata
    return None


def camp() -> dict:
    return orchestrator.agent.storage.get(st.storage_key(user.address))


async def say(ctx: Context, text: str, until, timeout: float = 20) -> ChatMessage | None:
    """Send a chat message and return the first reply that satisfies `until`."""
    await ctx.send(orchestrator.agent.address, chat_text(text))
    try:
        while True:
            msg = await asyncio.wait_for(inbox.get(), timeout)
            if until(msg):
                return msg
    except asyncio.TimeoutError:
        return None


def is_card(kind: str):
    return lambda m: (card_meta(m) or {}).get("card_kind") == kind


def has_text(*words: str):
    return lambda m: all(w.lower() in text_of(m).lower() for w in words)


async def unit_tests():
    # web search down -> OSM leads still come back
    async def broken(*_a, **_k):
        raise RuntimeError("ASI:One unavailable")

    llm.discover_businesses, original = broken, llm.discover_businesses
    out = await lead_finder.find_leads({"icp": ICP, "limit": 10})
    llm.discover_businesses = original
    check(len(out["leads"]) == 2 and any("Web search failed" in n for n in out["source_notes"]),
          "web-search failure still returns OSM leads")
    check([x["name"] for x in out["call_list"]] == ["Chai Point Corner"], "phone-only business goes to call list")

    await contact_unit_tests()
    await analyst_unit_tests()
    await copywriter_unit_tests()
    await llm_request_tests()


class FakeCompletions:
    """Records the kwargs complete() sends to chat.completions.create (no network)."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = type("Msg", (), {"content": '{"ok": true}'})()
        return type("Resp", (), {"choices": [type("Choice", (), {"message": msg})()]})()


async def llm_request_tests():
    fake = FakeCompletions()
    fake_client = type("Client", (), {"chat": type("Chat", (), {"completions": fake})()})()
    env_keys = ("ASI1_WEB_SEARCH", "ASI1_SEARCH_CONTEXT")  # .env is loaded by common; test the defaults
    original_client, original_env = llm._client, {k: os.environ.pop(k, None) for k in env_keys}
    llm._client = fake_client
    try:
        await llm.complete([{"role": "user", "content": "hi"}])
        await llm.complete([{"role": "user", "content": "find cafes"}], web_search=True)
        os.environ["ASI1_WEB_SEARCH"] = "false"
        await llm.complete([{"role": "user", "content": "find cafes"}], web_search=True)
    finally:
        llm._client = original_client
        for k, v in original_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    plain, search, disabled = [{**c, **(c.get("extra_body") or {})} for c in fake.calls]  # body as sent
    check(not any("web_search" in c for c in (plain, search, disabled)), "llm: never sends a `web_search` key")
    check(search.get("web_search_options") == {"search_context_size": "medium"},
          "llm: web_search=True sends web_search_options {search_context_size: medium}")
    check("web_search_options" not in plain and "web_search_options" not in disabled,
          "llm: no search field without web_search or with ASI1_WEB_SEARCH=false")


def _lead(lead_id: str, website: str | None = None, osm_emails: list[str] | None = None) -> dict:
    return {"id": lead_id, "name": lead_id, "website": website, "osm_emails": osm_emails or [], "phone": None,
            "source": "osm", "source_ref": f"https://www.openstreetmap.org/node/{lead_id}"}


async def contact_unit_tests():
    leads = [
        _lead("brewlab", "https://brewlab.test", ["owner@brewlab.test"]),
        _lead("beanhouse", "https://beanhouse.test"),
        _lead("curious", None, ["Hello@curiousbeans.test"]),
        _lead("nosite"),
        _lead("quiet", "https://quiet.test"),
        _lead("private", "https://private.test"),
        _lead("down", "https://down.test"),
        _lead("deadmx", "https://deadmx.test"),
        _lead("fb", "https://www.facebook.com/somecafe"),
        _lead("linkhub", "https://linkhub.test"),
    ]
    mx_calls.clear()
    out = await contact_finder.find_contacts({"leads": leads, "max_pages_per_site": 5})
    by_email = {c["email"]: c for c in out["contacts"]}
    conf = {e: c["confidence"] for e, c in by_email.items()}

    check(conf.get("owner@brewlab.test") == "high" and conf.get("orders@brewlab.test") == "high",
          "contacts: owner/orders email on own domain -> high")
    check(conf.get("hello@brewlab.test") == "medium" and by_email["hello@brewlab.test"]["role"] == "info",
          "contacts: generic email on own domain (de-obfuscated) -> medium")
    check(conf.get("beanhouse.jaipur@gmail.com") == "medium", "contacts: free-mail on own site -> medium")
    check(conf.get("studio@webcraft.test") == "low", "contacts: another domain's email on the site -> low")
    curious = by_email.get("hello@curiousbeans.test", {})
    check(curious.get("source") == "osm" and curious.get("confidence") == "low"
          and curious.get("source_url") == "https://www.openstreetmap.org/node/curious",
          "contacts: OSM-tag email kept with source osm, low confidence, OSM source_url")
    owner = by_email.get("owner@brewlab.test", {})
    check([c["email"] for c in out["contacts"]].count("owner@brewlab.test") == 1 and owner.get("source") == "website"
          and owner.get("source_url") == "https://brewlab.test/",
          "contacts: deduped (mailto + text + OSM tag) and the website source wins")
    check([c["email"] for c in out["contacts"] if c["lead_id"] == "brewlab"][0] == "owner@brewlab.test",
          "contacts: primary contact (owner) listed first")
    check(all(c["source_url"] and c["mx_ok"] is True and c["person_name"] is None for c in out["contacts"]),
          "contacts: every contact has source_url + mx_ok")
    check("+919829011111" in owner.get("phones", []), "contacts: phones extracted from the site")
    check(mx_calls.count("brewlab.test") == 1, "contacts: one MX lookup per domain")
    reasons = {s["lead_id"]: s["reason"] for s in out["skipped"]}
    check(reasons == {"nosite": "no_website", "quiet": "no_email_found", "private": "robots_disallowed",
                      "down": "unreachable", "deadmx": "mx_failed", "fb": "banned_host", "linkhub": "banned_host"},
          f"contacts: skipped reasons ({reasons})")
    check(not any(c["lead_id"] in reasons for c in out["contacts"]), "contacts: skipped leads have no contacts")

    crawl_stats.update(active=0, peak=0)
    many = [_lead(f"slow{i}", f"https://slow{i}.test") for i in range(12)]
    out = await contact_finder.find_contacts({"leads": many})
    check(crawl_stats["peak"] == contact_finder.CRAWL_CONCURRENCY == 5 and len(out["skipped"]) == 12,
          f"contacts: crawls run concurrently, at most 5 at a time (peak {crawl_stats['peak']})")

    try:
        await contact_finder.find_contacts({})
        check(False, "contacts: missing leads raises ValueError")
    except ValueError:
        check(True, "contacts: missing leads raises ValueError")


async def _qualify_with(fake, leads: list[dict]) -> dict:
    llm.qualify, original = fake, llm.qualify
    try:
        out = await lead_analyst.qualify_leads({"icp": ICP, "leads": leads, "contacts": []})
    finally:
        llm.qualify = original
    return {r["lead_id"]: r for r in out["reviews"]}


async def analyst_unit_tests():
    seen: list[dict] = []

    async def mixed_hooks(icp, lead, signals, page_text):
        seen.append({"signals": signals, "page_text": page_text})
        return {"fit_score": "88", "summary": "Busy specialty cafe, expanding.", "hooks": [
            {"observation": "Brews espresso all day", "evidence": "We brew espresso … all day",
             "evidence_url": "https://roastery.test/menu", "angle": "Beans for a high-volume espresso bar"},
            {"observation": "Opened a second outlet", "evidence": "Opened our second outlet in Malviya Nagar",
             "evidence_url": "https://roastery.test/about", "angle": "Supply both outlets"},
            {"observation": "Voted best cafe in Jaipur", "evidence": "Voted best cafe in Jaipur 2025",
             "evidence_url": "https://roastery.test/", "angle": "Award winners deserve award beans"},
            {"observation": "Has a menu", "evidence": "menu", "evidence_url": "https://roastery.test/", "angle": "x"},
            {"observation": "All-day cafe in C-Scheme", "evidence": "All-day cafe in C-Scheme, Jaipur",
             "evidence_url": "https://roastery.test/", "angle": "Central location"},
        ]}

    leads = [_lead("roastery", "https://roastery.test"), _lead("nosite"), _lead("down", "https://down.test"),
             _lead("private", "https://private.test"), _lead("fb", "https://www.facebook.com/somecafe")]
    by_id = await _qualify_with(mixed_hooks, leads)
    r = by_id["roastery"]
    hooks = {h["evidence"]: h["evidence_url"] for h in r["hooks"]}
    check(r["reachable"] and r["fallback"] is False and r["fit_score"] == 88
          and r["summary"] == "Busy specialty cafe, expanding.", "analyst: LLM review used (score, summary)")
    check(hooks == {"We brew espresso … all day": "https://roastery.test/",
                    "Opened our second outlet in Malviya Nagar": "https://roastery.test/about",
                    "All-day cafe in C-Scheme, Jaipur": "https://roastery.test/"},
          f"analyst: only observed hooks kept, evidence_url = page it was seen on ({hooks})")
    check(not any("Voted" in h["evidence"] or h["evidence"] == "menu" for h in r["hooks"]),
          "analyst: invented and trivial hooks dropped")
    sig = r["signals"]
    check(sig.get("pages_crawled") == 2 and sig["keywords"]["events"] and sig["keywords"]["menu"]
          and sig["mobile_viewport"] and sig["latest_year_seen"] == 2026 and sig["title"] == "Bean and Bloom Cafe",
          "analyst: site_signals merged across crawled pages")
    check(len(seen) == 1 and "[https://roastery.test/about]" in seen[0]["page_text"] and seen[0]["signals"] == sig,
          "analyst: llm.qualify gets signals + page text with URLs, only for reachable sites")
    reasons = {k: (v["reachable"], v["unreachable_reason"], v["hooks"], v["fallback"]) for k, v in by_id.items()
               if k != "roastery"}
    check(reasons == {"nosite": (False, "no_website", [], True), "down": (False, "unreachable", [], True),
                      "private": (False, "robots_disallowed", [], True), "fb": (False, "banned_host", [], True)},
          f"analyst: no-site / failed crawls get signals-only reviews with a reason ({reasons})")
    check(all(set(v) >= {"lead_id", "reachable", "signals", "fit_score", "hooks", "summary"} and len(v["summary"].split()) <= 40
              for v in by_id.values()), "analyst: every review has the §3.8 shape")

    async def broken(*_a, **_k):
        raise RuntimeError("ASI:One unavailable")

    r = (await _qualify_with(broken, [_lead("roastery", "https://roastery.test")]))["roastery"]
    check(r["fallback"] is True and r["hooks"] == [] and r["reachable"] and 0 <= r["fit_score"] <= 100
          and r["summary"].startswith("Signals-only") and "brews espresso" in r["summary"],
          f"analyst: LLM error -> deterministic signals-only review ({r['fit_score']}: {r['summary']})")
    again = (await _qualify_with(broken, [_lead("roastery", "https://roastery.test")]))["roastery"]
    check(again["fit_score"] == r["fit_score"], "analyst: fallback score is deterministic")

    async def no_score(*_a, **_k):
        return {"hooks": [], "summary": "looks great"}

    r = (await _qualify_with(no_score, [_lead("roastery", "https://roastery.test")]))["roastery"]
    check(r["fallback"] is True and r["summary"].startswith("Signals-only"), "analyst: LLM reply missing fit_score -> fallback")

    async def bad_json(*_a, **_k):
        raise ValueError("LLM did not return JSON")

    r = (await _qualify_with(bad_json, [_lead("roastery", "https://roastery.test")]))["roastery"]
    check(r["fallback"] is True, "analyst: bad JSON -> fallback")

    crawl_stats.update(active=0, peak=0)
    out = await lead_analyst.qualify_leads({"icp": ICP, "leads": [_lead(f"slow{i}", f"https://slow{i}.test")
                                                                  for i in range(12)]})
    check(crawl_stats["peak"] == lead_analyst.CRAWL_CONCURRENCY == 5 and len(out["reviews"]) == 12,
          f"analyst: leads reviewed concurrently, at most 5 at a time (peak {crawl_stats['peak']})")

    for bad in ({"icp": ICP}, {"leads": [_lead("x")]}):
        try:
            await lead_analyst.qualify_leads(bad)
            check(False, f"analyst: bad args raise ValueError ({list(bad)})")
        except ValueError:
            check(True, f"analyst: bad args raise ValueError ({list(bad)})")


REVIEW = {"lead_id": "tapri", "fit_score": 70, "hooks": [
    {"observation": "Brews espresso all day", "evidence": "We brew espresso all day",
     "evidence_url": "https://tapri.test/", "angle": "Beans for a busy espresso bar"}]}
GOOD = {"subject": "Beans for your espresso bar", "hook_used": "Brews espresso all day",
        "body_text": f"Hi Tapri team, I saw you brew espresso all day. Want a free sample?\n\nAsha, Bean Co\n{UNSUB}"}


async def _draft_with(fake, args: dict) -> dict:
    llm.draft_email, original = fake, llm.draft_email
    try:
        return await copywriter.draft_emails(args)
    finally:
        llm.draft_email = original


def _scripted(*replies):
    """A fake llm.draft_email returning `replies` in order (an Exception instance is raised)."""
    calls: list[dict] = []

    async def fake(profile, lead, qualification, style_rules, problems=None):
        calls.append({"lead": lead["id"], "problems": problems})
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    return fake, calls


async def copywriter_unit_tests():
    v = copywriter.validate
    check(v(GOOD, PROFILE, REVIEW) == [], f"copywriter: valid draft passes the validator ({v(GOOD, PROFILE, REVIEW)})")
    bad = {
        "max_words": dict(GOOD, body_text=GOOD["body_text"] + " word" * 60),
        "subject words": dict(GOOD, subject="A really very long subject line with nine words"),
        "subject chars": dict(GOOD, subject="Supercalifragilisticexpialidocious espresso-beans-for-everyone today"),
        "unsubscribe": dict(GOOD, body_text=GOOD["body_text"].replace(UNSUB, "Reply stop to opt out.")),
        "sender name": dict(GOOD, body_text=GOOD["body_text"].replace("Asha, ", "")),
        "brand": dict(GOOD, body_text=GOOD["body_text"].replace(", Bean Co", "")),
        "placeholder {}": dict(GOOD, body_text="Hi {first_name}, " + GOOD["body_text"]),
        "placeholder ❓": dict(GOOD, body_text=GOOD["body_text"] + " ❓ phone"),
        "price": dict(GOOD, body_text=GOOD["body_text"].replace("free sample", "kilo at ₹450")),
        "hook_used": dict(GOOD, hook_used="Voted best cafe in Jaipur"),
    }
    caught = {k: len(v(d, PROFILE, REVIEW)) == 1 for k, d in bad.items()}
    check(all(caught.values()), f"copywriter: validator catches each §3.12 rule ({caught})")
    check(v(bad["price"], dict(PROFILE, pricing={"mention_price": True}), REVIEW) == [],
          "copywriter: price allowed when pricing.mention_price is true")
    check(v(dict(GOOD, hook_used="null"), PROFILE, REVIEW) == [] and v(GOOD, PROFILE, {"hooks": []}) != [],
          "copywriter: hook_used may be null, but must match a review hook")
    unfilled = v(GOOD, original_load_profile(), REVIEW)  # the real config/brand_profile.yaml is still ❓
    check(any("not filled in" in p for p in unfilled), "copywriter: ❓ brand profile values are flagged, never used")

    contacts = [{"lead_id": "tapri", "email": "owner@tapri.test", "confidence": "high"},
                {"lead_id": "tapri", "email": "info@tapri.test", "confidence": "medium"}]
    args = {"profile": PROFILE, "style_rules": ["be brief"], "icp": ICP, "leads": [_lead("tapri", "https://tapri.test")],
            "contacts": contacts, "reviews": [REVIEW]}
    fake, calls = _scripted(GOOD)
    out = await _draft_with(fake, args)
    d = out["drafts"][0]
    check(len(calls) == 1 and d["to"] == "owner@tapri.test" and d["warnings"] == [] and d["approved"] is False
          and d["word_count"] == len(GOOD["body_text"].split()) and d["hook_used"] == "Brews espresso all day",
          "copywriter: valid first draft -> one LLM call, primary contact, §3.8 Draft shape")

    fake, calls = _scripted(bad["unsubscribe"], GOOD)
    d = (await _draft_with(fake, args))["drafts"][0]
    check(len(calls) == 2 and calls[0]["problems"] is None and any("unsubscribe" in p or UNSUB in p
                                                                   for p in calls[1]["problems"] or [])
          and d["warnings"] == [] and d["body_text"] == GOOD["body_text"],
          "copywriter: failed validation -> regenerated once with the problems passed back")

    fake, calls = _scripted(bad["price"], bad["price"], GOOD)
    d = (await _draft_with(fake, args))["drafts"][0]
    check(len(calls) == 2 and d["warnings"] == ["do not mention prices"] and "₹450" in d["body_text"],
          f"copywriter: still failing after one regenerate -> draft kept with warnings ({d['warnings']})")

    fake, calls = _scripted(bad["brand"], RuntimeError("ASI:One unavailable"))
    d = (await _draft_with(fake, args))["drafts"][0]
    check(len(calls) == 2 and d["warnings"] and "Bean Co" not in d["body_text"],
          "copywriter: regenerate error -> first draft kept with its warnings")

    async def per_lead(profile, lead, qualification, style_rules, problems=None):
        if lead["id"] == "broken":
            raise ValueError("LLM did not return JSON")
        return dict(GOOD)

    seen: list[str] = []

    async def recording(profile, lead, qualification, style_rules, problems=None):
        seen.append(lead["id"])
        return await per_lead(profile, lead, qualification, style_rules, problems)

    out = await _draft_with(recording, dict(args, leads=[_lead("tapri"), _lead("broken"), _lead("nomail")],
                                            contacts=contacts + [{"lead_id": "broken", "email": "hi@broken.test"}]))
    skipped = {s["lead_id"]: s["reason"] for s in out["skipped"]}
    check([x["lead_id"] for x in out["drafts"]] == ["tapri"] and skipped == {"broken": "llm_failed", "nomail": "no_contact"}
          and "nomail" not in seen, f"copywriter: LLM failure / no contact -> skipped, others still drafted ({skipped})")

    try:
        await copywriter.draft_emails({"profile": PROFILE})
        check(False, "copywriter: missing leads raises ValueError")
    except ValueError:
        check(True, "copywriter: missing leads raises ValueError")


@user.on_event("startup")
async def run_script(ctx: Context):
    await asyncio.sleep(2)
    orchestrator.agent.storage.remove(st.storage_key(user.address))  # start clean: storage persists between runs
    try:
        await unit_tests()

        msg = await say(ctx, "hi", has_text("LeadReach"))
        check(msg is not None, "help reply to 'hi'")
        check(acks >= 1, "chat acknowledgement received")

        msg = await say(ctx, "I sell organic coffee beans, find cafés in Jaipur", is_card("review"))
        check(msg is not None, "ICP review card shown")
        if msg:
            payload = json.loads(card_meta(msg)["card_payload"])
            check(payload["approve_cta"]["selection"] == {"action": "approve", "step": "icp"}, "card selection JSON")
        check(camp()["state"] == st.AWAIT_ICP, "state AWAIT_ICP")

        msg = await say(ctx, "approve", is_card("review"))
        check(msg is not None and "leads in" in text_of(msg), "leads card shown")
        leads = camp()["leads"]
        names = [x["name"] for x in leads]
        check(camp()["state"] == st.AWAIT_LEADS, "state AWAIT_LEADS")
        check("Brew Lab Roasters" in names and "Ghost Cafe" not in names, "web lead kept only if site verified")
        check(all(x.get("source_ref") for x in leads), "every lead has a source_ref")

        msg = await say(ctx, "remove 1", is_card("review"))
        check(msg is not None and len(camp()["leads"]) == len(leads) - 1, "'remove 1' drops a lead")

        msg = await say(ctx, "approve", has_text("contacts found"))
        check(msg is not None and card_meta(msg) and "⚠ low" in text_of(msg), "contacts card shown, low confidence flagged")
        check(camp()["state"] == st.AWAIT_CONTACTS and {c["lead_id"] for c in camp()["contacts"]} == {"web:brew-lab-roasters"},
              "state AWAIT_CONTACTS with Brew Lab's contacts")

        msg = await say(ctx, "approve", has_text("Lead reviews"))
        check(msg is not None and card_meta(msg) and camp()["state"] == st.AWAIT_REVIEWS, "reviews card shown, AWAIT_REVIEWS")
        reviews = camp()["reviews"]
        brew = next((r for r in reviews if r["lead_id"] == "web:brew-lab-roasters"), {})
        check(len(reviews) == len(camp()["leads"]) and brew.get("fallback") is False and brew.get("fit_score") == 81
              and [h["evidence"] for h in brew.get("hooks", [])] == ["Orders: orders@brewlab.test"],
              "every lead reviewed; Brew Lab keeps only its observed hook")
        check(msg is not None and "Takes orders by email" in text_of(msg), "reviews card shows the top hook")
        n = camp()["last_list"].index("web:brew-lab-roasters") + 1
        msg = await say(ctx, f"show lead {n}", is_card("detail"))
        check(msg is not None and "Orders: orders@brewlab.test" in text_of(msg)
              and "https://brewlab.test/contact" in text_of(msg), "'show lead N' detail card shows evidence + URL")

        msg = await say(ctx, "approve", has_text("Draft 1/"))
        drafts = camp()["drafts"]
        check(msg is not None and camp()["state"] == st.AWAIT_DRAFTS and len(drafts) == 1
              and drafts[0]["to"] == "owner@brewlab.test" and drafts[0]["warnings"] == []
              and drafts[0]["hook_used"] == "Takes orders by email", "draft card shown: primary contact, valid draft")
        no_contact = [x["name"] for x in camp()["leads"] if x["id"] != "web:brew-lab-roasters"]
        check(msg is not None and bool(no_contact)
              and all(f"{name} (no email found)" in text_of(msg).split("\n")[0] for name in no_contact),
              f"draft card says which leads got no draft and why ({text_of(msg).split(chr(10))[0] if msg else None})")

        msg = await say(ctx, "make this one shorter", has_text("Rewrote this draft"))
        check(msg is not None and camp()["style_rules"] == [] and draft_calls[-1]["style_rules"] == ["make this one shorter"]
              and "make this one shorter" in camp()["drafts"][0]["body_text"],
              "edit_draft on one draft rewrites that draft only, no style rule saved")

        msg = await say(ctx, "never mention discounts", has_text("style rule"))
        check(msg is not None and (card_meta(msg) or {}).get("card_kind") == "review"
              and camp()["pending_style_rule"] == "never mention discounts" and camp()["style_rules"] == [],
              "general edit -> style-rule confirmation card, rule not saved yet")
        msg = await say(ctx, json.dumps({"action": "reject", "step": "style_rule"}), has_text("this draft only"))
        check(msg is not None and camp()["style_rules"] == [] and camp()["pending_style_rule"] is None
              and draft_calls[-1]["style_rules"] == ["never mention discounts"],
              "'just this draft' -> applied to the current draft, not saved as a rule")

        calls_before = len(draft_calls)
        msg = await say(ctx, "make them warmer", has_text("style rule"))
        check(msg is not None and camp()["style_rules"] == [] and len(draft_calls) == calls_before,
              "general edit waits for confirmation (no rewrite yet)")
        msg = await say(ctx, "yes", has_text("Saved style rule"))
        d = camp()["drafts"][0]
        check(msg is not None and camp()["style_rules"] == ["make them warmer"]
              and draft_calls[-1]["style_rules"] == ["make them warmer"] and "make them warmer" in d["body_text"]
              and d["approved"] is False and camp()["state"] == st.AWAIT_DRAFTS,
              "confirmed style rule saved to the campaign and unapproved drafts rewritten")
        check(any(e["action"] == "style rule added" and e["actor"] == "user" for e in camp()["log"]),
              "audit log records the style rule")

        msg = await say(ctx, "approve", has_text("failed", "retry"))
        check(msg is not None and camp()["drafts"][0]["approved"] is True, "approving the last draft moves to send")
        check(camp()["state"] == st.STEP_FAILED and camp()["failed_gate"] == "send",
              "unimplemented send step fails gracefully: STEP_FAILED")

        msg = await say(ctx, "stop asking me about contacts", has_text("auto-approve"))
        check(msg is not None and camp()["settings"]["ask_before"]["contacts"] is False, "NL settings change")

        msg = await say(ctx, "pause", has_text("paused"))
        check(msg is not None and camp()["state"] == st.PAUSED, "pause")
        msg = await say(ctx, "retry", has_text("resume"))
        check(msg is not None and camp()["state"] == st.PAUSED, "paused campaign ignores 'retry'")
        msg = await say(ctx, "resume", lambda m: True)
        check(msg is not None and camp()["state"] == st.STEP_FAILED, "resume restores state")

        msg = await say(ctx, json.dumps({"action": "status", "step": "any"}), is_card("detail"))
        check(msg is not None and card_meta(msg).get("is_terminal") == "true", "card-action JSON -> status card")
        check(any(e["step"] == "settings" for e in camp()["log"]), "audit log records settings change")
    except Exception as err:  # noqa: BLE001
        failures.append(f"exception: {err!r}")
        import traceback

        traceback.print_exc()
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}", flush=True)
    os._exit(1 if failures else 0)


@user.on_message(ChatAcknowledgement)
async def got_ack(ctx: Context, sender_addr: str, msg: ChatAcknowledgement):
    global acks
    acks += 1


@user.on_message(ChatMessage)
async def got_reply(ctx: Context, sender_addr: str, msg: ChatMessage):
    await inbox.put(msg)


bureau = Bureau(port=8010)
for a in (orchestrator.agent, lead_finder.agent, contact_finder.agent, lead_analyst.agent, copywriter.agent,
          sender.agent, user):
    bureau.add(a)

if __name__ == "__main__":
    threading.Timer(150, lambda: (print("TIMEOUT", flush=True), os._exit(2))).start()
    bureau.run()
