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
from crawl import Page  # noqa: E402

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
WEB_FOUND = [{"name": "Brew Lab Roasters", "website": "https://brewlab.example.in", "area": "Malviya Nagar",
              "evidence_url": "https://search.example/brewlab"},
             {"name": "Ghost Cafe", "website": "https://ghost.example.in", "area": None,
              "evidence_url": "https://search.example/ghost"}]


async def fake_parse_intent(text, state, gate, numbered):
    t = text.lower()
    if "sell" in t:
        return {"intent": "start_campaign", "args": {"brief": text, "city": "Jaipur"}, "confidence": 0.95}
    if "stop asking" in t:
        return {"intent": "update_settings", "args": {"ask_before": {"contacts": False}}, "confidence": 0.9}
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


llm.parse_intent = fake_parse_intent
llm.build_icp = fake_build_icp
llm.discover_businesses = fake_discover
llm.find_websites = fake_find_websites
geo.geocode = fake_geocode
geo.find_businesses = fake_find_businesses
lead_finder.PoliteFetcher = FakeFetcher

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


@user.on_event("startup")
async def run_script(ctx: Context):
    await asyncio.sleep(2)
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

        msg = await say(ctx, "approve", has_text("failed", "retry"))
        check(msg is not None, "unimplemented contacts step fails gracefully with retry/skip")
        check(camp()["state"] == st.STEP_FAILED and camp()["failed_gate"] == "contacts", "state STEP_FAILED")

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
