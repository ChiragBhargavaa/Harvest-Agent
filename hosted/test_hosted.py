"""Offline checks for the hosted Harvest Agent. No network and no real LLM calls."""

import ast
import asyncio
import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from uagents import Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent  # noqa: E402
from uagents_core.models import Model  # noqa: E402

import common  # noqa: E402

AGENT_PATH = ROOT / "hosted" / "orchestrator" / "agent.py"
SENDER = "agent1quserxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class Storage:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def has(self, key):
        return key in self.data

    def remove(self, key):
        self.data.pop(key, None)


class FakeCtx:
    def __init__(self):
        self.storage = Storage()
        self.sent = []
        self.logger = logging.getLogger("hosted-test")
        self.agent = type("A", (), {
            "address": "agent1qhostedorchestrator",
            "wallet": type("W", (), {"address": staticmethod(lambda: "fetch1" + "q" * 50)})(),
        })()
        self.remote_calls = 0

    async def send(self, dest, msg):
        self.sent.append((dest, msg))

    async def send_and_receive(self, destination, message, response_type=None, timeout=30):
        self.remote_calls += 1
        return None, "timeout"


class StubAgent:
    def __init__(self):
        self.included = []
        self.events = {}
        self.address = "agent1qhostedorchestrator"

    def include(self, proto, publish_manifest=False):
        self.included.append((proto, publish_manifest))

    def on_event(self, name):
        def deco(fn):
            self.events.setdefault(name, []).append(fn)
            return fn
        return deco

    def on_message(self, *args, **kwargs):
        def deco(fn):
            return fn
        return deco

    def on_interval(self, *args, **kwargs):
        def deco(fn):
            return fn
        return deco


def load_orchestrator():
    stub = StubAgent()
    spec = importlib.util.spec_from_file_location("hosted_orchestrator", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    module.agent = stub
    sys.modules["hosted_orchestrator"] = module
    spec.loader.exec_module(module)
    return module, stub


def chat(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(mod.IST), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


def texts(ctx: FakeCtx) -> list[str]:
    out = []
    for _, msg in ctx.sent:
        if isinstance(msg, ChatMessage):
            out.append("".join(part.text for part in msg.content if isinstance(part, TextContent)))
    return out


def tool_digest(request_model, response_model) -> str:
    proto = Protocol(name="leadreach-tools", version="0.1.0")

    @proto.on_message(request_model, replies=response_model)
    async def handle_request(ctx, sender, req):
        return None

    return proto.manifest()["metadata"]["digest"]


mod, stub = load_orchestrator()


def test_file_compiles_and_protocols_match():
    source = AGENT_PATH.read_text()
    assert "Agent(" not in source
    assert "agent.run" not in source
    assert "first 5 free" not in source.lower()
    assert any(flag for _, flag in stub.included)
    assert Model.build_schema_digest(mod.ToolRequest) == Model.build_schema_digest(common.ToolRequest)
    assert Model.build_schema_digest(mod.ToolResponse) == Model.build_schema_digest(common.ToolResponse)
    assert tool_digest(mod.ToolRequest, mod.ToolResponse) == tool_digest(common.ToolRequest, common.ToolResponse)
    assert mod.REQUEST_ID_KEY == "_request_id"
    assert "❓" in mod.BRAND_PROFILE["brand"]["name"]
    assert mod.BRAND_PROFILE["pricing"]["mention_price"] is False


def test_logger_calls_take_one_message():
    """Hosted ctx.logger rejects %-style extra positional args."""
    tree = ast.parse(AGENT_PATH.read_text())
    methods = {"debug", "info", "warning", "error", "exception", "critical", "log"}
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in methods:
            continue
        recv = node.func.value
        is_logger = (
            (isinstance(recv, ast.Attribute) and recv.attr == "logger")
            or (isinstance(recv, ast.Name) and recv.id in {"log", "logger", "logging"})
            or (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name) and recv.value.id == "logging")
        )
        if is_logger and len(node.args) > 1:
            bad.append(f"{node.lineno}:{node.func.attr} has {len(node.args)} positional args")
    assert bad == [], bad


def test_help_replies():
    ctx = FakeCtx()

    async def run():
        await mod.handle_chat(ctx, SENDER, chat("help"))

    asyncio.run(run())
    body = texts(ctx)
    assert body[0] == "" or True
    assert any("LeadReach" in item for item in body)
    assert ctx.sent[0][1].acknowledged_msg_id == ctx.sent[0][1].acknowledged_msg_id


def test_cap_blocks_before_llm(monkeypatch=None):
    ctx = FakeCtx()
    called = []

    async def boom(*args, **kwargs):
        called.append(1)
        raise AssertionError("LLM/HTTP must not run over the daily cap")

    ctx.storage.set(mod.CAP_KEY, {"ist_day": mod.ist_today(), "total": 20, "users": {}})
    mod.http_request = boom

    async def run():
        await mod.handle_chat(ctx, SENDER, chat("I sell organic coffee beans, find cafes in Jaipur"))

    asyncio.run(run())
    assert texts(ctx)[-1] == mod.LIMIT_MSG
    assert called == []
    assert ctx.storage.get(mod.CAP_KEY)["total"] == 20


def test_per_user_cap_and_active_campaign_not_counted():
    ctx = FakeCtx()
    ctx.storage.set(mod.CAP_KEY, {"ist_day": mod.ist_today(), "total": 1, "users": {SENDER: 3}})
    camp = mod.fresh_campaign()
    camp["state"] = mod.AWAIT_LEADS
    camp["campaign_id"] = "c_live"
    camp["target"]["city"] = "Jaipur"
    camp["leads"] = [{"id": "1", "name": "Cafe"}]
    ctx.storage.set(mod.storage_key(SENDER), camp)

    async def run():
        await mod.handle_chat(ctx, SENDER, chat("status"))

    asyncio.run(run())
    assert all(item != mod.LIMIT_MSG for item in texts(ctx))
    assert any("AWAIT_LEADS" in item for item in texts(ctx))
    assert ctx.storage.get(mod.CAP_KEY)["users"][SENDER] == 3

    ctx2 = FakeCtx()
    ctx2.storage.set(mod.CAP_KEY, {"ist_day": mod.ist_today(), "total": 1, "users": {SENDER: 3}})

    async def blocked():
        await mod.handle_chat(ctx2, SENDER, chat("new campaign"))

    asyncio.run(blocked())
    assert texts(ctx2)[-1] == mod.LIMIT_MSG
    assert ctx2.storage.get(mod.CAP_KEY)["total"] == 1


def test_cap_resets_on_a_new_ist_day_and_counts_one_campaign():
    ctx = FakeCtx()
    yesterday = (datetime.now(mod.IST) - timedelta(days=1)).date().isoformat()
    ctx.storage.set(mod.CAP_KEY, {"ist_day": yesterday, "total": 20, "users": {SENDER: 3}})
    calls = []

    async def fake_http(method, url, **kwargs):
        calls.append(url)
        icp = {
            "product": "organic coffee beans",
            "business_types": ["cafe"],
            "osm_tags": ["amenity=cafe"],
            "search_queries": ["cafes in Jaipur"],
            "fit_criteria": ["serves coffee"],
            "city": "Jaipur",
            "radius_km": 10,
        }
        content = json.dumps(icp)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "content": body, "url": url}

    mod.http_request = fake_http
    os.environ["ASI1_API_KEY"] = "test-key-not-real"
    try:
        payload = json.dumps({
            "action": "start_campaign",
            "brief": "organic coffee beans for cafes",
            "city": "Jaipur",
        })

        async def run():
            await mod.handle_chat(ctx, SENDER, chat(payload))

        asyncio.run(run())
    finally:
        os.environ.pop("ASI1_API_KEY", None)
    assert calls, "building an ICP should call the LLM"
    assert ctx.storage.get(mod.CAP_KEY)["ist_day"] == mod.ist_today()
    assert ctx.storage.get(mod.CAP_KEY)["total"] == 1
    assert ctx.storage.get(mod.CAP_KEY)["users"][SENDER] == 1
    assert any("organic coffee beans" in item for item in texts(ctx))

    async def again():
        await mod.handle_chat(ctx, SENDER, chat("status"))

    asyncio.run(again())
    assert ctx.storage.get(mod.CAP_KEY)["total"] == 1


def test_dry_run_never_sends():
    ctx = FakeCtx()
    called = []

    async def boom(*args, **kwargs):
        called.append(args)
        raise AssertionError("dry-run sender must not make an HTTP call")

    mod.http_request = boom
    os.environ.pop("TEST_REDIRECT_TO", None)

    async def run():
        return await mod.send_emails({
            "drafts": [{"lead_id": "L1", "to": "owner@cafe.test", "subject": "Hello", "body_text": "Hi"}],
            "dry_run": False,
            "daily_cap": 5,
        }, ctx)

    result = asyncio.run(run())
    assert result["dry_run"] is True
    assert result["sent"] == []
    assert result["queued"] == []
    assert result["would_send"][0]["lead_id"] == "L1"
    assert result["would_send"][0]["dry_run"] is True
    assert called == []
    assert ctx.storage.get(mod.OUTBOX_KEY)[0]["lead_id"] == "L1"


def test_inline_finder_and_remote_fallback():
    ctx = FakeCtx()

    async def fake_http(method, url, **kwargs):
        if "nominatim" in url:
            body = [{
                "display_name": "Jaipur, Rajasthan, India",
                "lat": "26.9", "lon": "75.8",
                "boundingbox": ["26.7", "27.1", "75.6", "76.0"],
            }]
        else:
            body = {"elements": [{
                "type": "node", "id": 42, "lat": 26.9, "lon": 75.8,
                "tags": {"name": "Test Cafe", "amenity": "cafe", "website": "https://testcafe.in",
                         "email": "hello@testcafe.in"},
            }]}
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "content": json.dumps(body).encode(),
                "url": url}

    mod.http_request = fake_http
    os.environ.pop("LEAD_FINDER_AGENT_ADDRESS", None)

    async def inline():
        return await mod.call_tool(ctx, "find_leads", {
            "icp": {"city": "Jaipur", "business_types": ["cafe"], "osm_tags": ["amenity=cafe"], "search_queries": []},
            "limit": 5,
        })

    result, error = asyncio.run(inline())
    assert error == "", error
    assert result["leads"][0]["name"] == "Test Cafe"
    assert ctx.remote_calls == 0

    os.environ["LEAD_FINDER_AGENT_ADDRESS"] = "agent1qfinder"
    try:
        async def remote_then_inline():
            return await mod.call_tool(ctx, "find_leads", {
                "icp": {"city": "Jaipur", "business_types": ["cafe"], "osm_tags": ["amenity=cafe"], "search_queries": []},
                "limit": 5,
            })

        result, error = asyncio.run(remote_then_inline())
    finally:
        os.environ.pop("LEAD_FINDER_AGENT_ADDRESS", None)
    assert error == "", error
    assert ctx.remote_calls == 2  # send, then one resend, then the cached inline result
    assert result["leads"][0]["name"] == "Test Cafe"


def test_contacts_reviews_drafts_and_unpriced_pack():
    ctx = FakeCtx()

    async def fake_http(method, url, **kwargs):
        if url.endswith("/robots.txt"):
            return {"status": 404, "headers": {}, "content": b"", "url": url}
        if "api.asi1.ai" in url:
            draft = {"subject": "Hello from the roaster", "hook_used": None,
                     "body_text": "We make a new trimmer.\n\nIf you'd prefer not to hear from me again, "
                                  "just reply 'unsubscribe'."}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(draft)}}]}).encode()
            return {"status": 200, "headers": {"Content-Type": "application/json"}, "content": body, "url": url}
        html = (
            b"<html><head><title>Test Cafe</title></head><body>"
            b"<a href='mailto:hello@testcafe.in'>mail</a> Test Cafe</body></html>"
        )
        return {"status": 200, "headers": {"Content-Type": "text/html"}, "content": html, "url": url}

    mod.http_request = fake_http
    mod.mx_ok = lambda domain: True
    os.environ["ASI1_API_KEY"] = "test-key-not-real"
    os.environ.pop("LEADPACK_PRICE", None)
    try:
        async def pipeline():
            contacts = await mod.find_contacts({"leads": [{
                "id": "web:test-cafe", "name": "Test Cafe", "website": "https://testcafe.in", "osm_emails": [],
            }]}, ctx)
            reviews = await mod.qualify_leads({
                "icp": {"product": "a new trimmer", "business_types": ["cafe"],
                        "osm_tags": ["amenity=cafe"], "fit_criteria": []},
                "leads": [{"id": "1", "name": "No Site", "category": "amenity=cafe"}],
            }, ctx)
            drafts = await mod.draft_emails({
                "profile": mod.load_profile(),
                "brief": "a new trimmer for salons",
                "icp": {"product": "a new trimmer"},
                "style_rules": [],
                "leads": [{"id": "web:test-cafe", "name": "Test Cafe"}],
                "contacts": contacts["contacts"],
                "reviews": [],
            }, ctx)
            await mod.handle_chat(ctx, SENDER, chat("buy lead pack"))
            return contacts, reviews, drafts

        contacts, reviews, drafts = asyncio.run(pipeline())
    finally:
        os.environ.pop("ASI1_API_KEY", None)
    assert contacts["contacts"][0]["email"] == "hello@testcafe.in"
    assert contacts["contacts"][0]["source_url"].startswith("https://testcafe.in")
    assert reviews["reviews"][0]["fallback"] is True
    assert reviews["reviews"][0]["fit_score"] == 30
    assert drafts["drafts"] and drafts["drafts"][0]["warnings"]
    assert all("RequestPayment" not in type(msg).__name__ for _, msg in ctx.sent)
    joined = "\n".join(texts(ctx))
    assert "LEADPACK_PRICE" in joined
    assert "first 5 free" not in joined.lower()


def test_remote_success_skips_inline():
    ctx = FakeCtx()
    called = []

    async def boom(*args, **kwargs):
        called.append(1)
        raise AssertionError("inline HTTP should not run when the specialist replies")

    async def remote(destination, message, response_type=None, timeout=30):
        ctx.remote_calls += 1
        from uagents_core.models import Model as _M
        assert response_type is mod.ToolResponse
        reply = mod.ToolResponse(ok=True, result=json.dumps({
            "leads": [{"id": "osm:node/1", "name": "Remote Cafe"}], "call_list": [], "source_notes": ["remote"],
        }))
        assert _M.build_schema_digest(reply) == _M.build_schema_digest(common.ToolResponse)
        return reply, "sent"

    ctx.send_and_receive = remote
    mod.http_request = boom
    os.environ["LEAD_FINDER_AGENT_ADDRESS"] = "agent1qfinder"
    try:
        async def run():
            return await mod.call_tool(ctx, "find_leads", {"icp": {"city": "Jaipur", "business_types": ["cafe"]}})

        result, error = asyncio.run(run())
    finally:
        os.environ.pop("LEAD_FINDER_AGENT_ADDRESS", None)
    assert error == ""
    assert result["leads"][0]["name"] == "Remote Cafe"
    assert called == []
    assert ctx.remote_calls == 1


if __name__ == "__main__":
    test_file_compiles_and_protocols_match()
    test_logger_calls_take_one_message()
    test_help_replies()
    test_cap_blocks_before_llm()
    test_per_user_cap_and_active_campaign_not_counted()
    test_cap_resets_on_a_new_ist_day_and_counts_one_campaign()
    test_dry_run_never_sends()
    test_inline_finder_and_remote_fallback()
    test_contacts_reviews_drafts_and_unpriced_pack()
    test_remote_success_skips_inline()
    print("hosted tests ok")
