"""Shared message models and helpers used by every agent."""

import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

for _stream in (sys.stdout, sys.stderr):  # log lines show up immediately, even when piped (run_all, nohup)
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # not a TextIOWrapper (e.g. replaced by a test harness)
        pass


# ---------- agent <-> agent messages (orchestrator -> specialists) ----------

class ToolRequest(Model):
    tool: str              # "find_leads" | "find_contacts" | "qualify_leads" | "draft_emails" | "send_emails"
    args_json: str = "{}"  # JSON-encoded arguments (keeps the schema stable)


class ToolResponse(Model):
    ok: bool
    result: str = ""       # JSON matching the tool's result schema
    error: str = ""


# ---------- chat-protocol helpers ----------

def now() -> datetime:
    return datetime.now(timezone.utc)


def chat_text(text: str, end_session: bool = False) -> ChatMessage:
    """Build a ChatMessage reply for ASI:One. Only end the session when the campaign is finished."""
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=content)


def ack(msg: ChatMessage) -> ChatAcknowledgement:
    return ChatAcknowledgement(timestamp=now(), acknowledged_msg_id=msg.msg_id)


def text_of(msg: ChatMessage) -> str:
    return "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()


# ---------- env helpers ----------

def env_bool(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_seconds(name: str, default: float) -> float:
    """A positive number of seconds from the environment; missing, invalid or <= 0 -> default."""
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def redact(text: str) -> str:
    """Strip API keys / bearer tokens from error text before it is logged or stored."""
    text = str(text)
    for name in ("ASI1_API_KEY", "RESEND_API_KEY", "FOURSQUARE_API_KEY", "GOOGLE_PLACES_API_KEY"):
        secret = os.getenv(name, "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return re.sub(r"(?i)\b(sk-|bearer\s+)[\w.-]{6,}", r"\1***", text)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set - copy .env.example to .env and fill it in (or run setup.sh)")
    return value


def agent_address(seed_env: str) -> str:
    """Address an agent will have, derived from its seed (same as Agent(seed=...).address)."""
    from uagents.crypto import Identity

    return Identity.from_seed(require_env(seed_env), 0).address


async def with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.0, **kwargs):
    """Retry an async call with exponential backoff (judges reward recovery from failed tool calls)."""
    last_err = None
    for i in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001 - we re-raise the last one
            last_err = err
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** i))
    raise last_err


# ---------- specialist factory ----------

ToolFn = Callable[[dict], Awaitable[dict]]


def make_specialist(
    *,
    name: str,
    seed_env: str,
    port_env: str,
    default_port: int,
    profile: str,
    description: str,
    tools: dict[str, ToolFn],
) -> Agent:
    """Build a specialist agent that answers ToolRequest with ToolResponse.

    Each tool takes the parsed args dict and returns a JSON-serialisable result dict.
    Handlers never raise: unknown tools and exceptions come back as ToolResponse(ok=False).
    """
    agent = Agent(
        name=name,
        seed=require_env(seed_env),
        port=int(os.getenv(port_env, str(default_port))),
        mailbox=env_bool("MAILBOX", True),
        publish_agent_details=True,
        description=description,
        readme_path=str(ROOT / "agents" / "profiles" / profile),
    )
    proto = Protocol(name="leadreach-tools", version="0.1.0")

    @proto.on_message(ToolRequest, replies=ToolResponse)
    async def handle_request(ctx: Context, sender: str, req: ToolRequest):
        ctx.logger.info(f"{req.tool} request from {sender[:16]}...")
        fn = tools.get(req.tool)
        if fn is None:
            await ctx.send(sender, ToolResponse(ok=False, error=f"unknown tool '{req.tool}'"))
            return
        start = time.monotonic()
        try:
            args = json.loads(req.args_json or "{}")
            result = await fn(args)
            await ctx.send(sender, ToolResponse(ok=True, result=json.dumps(result)))
            ctx.logger.info(f"{req.tool} done in {time.monotonic() - start:.1f}s")
        except NotImplementedError as err:
            ctx.logger.warning(str(err))
            await ctx.send(sender, ToolResponse(ok=False, error=str(err)))
        except Exception as err:  # noqa: BLE001 - report every failure back to the orchestrator
            error = redact(f"{type(err).__name__}: {err}")[:400]
            ctx.logger.error(f"{req.tool} failed in {time.monotonic() - start:.1f}s: {error}")
            await ctx.send(sender, ToolResponse(ok=False, error=error))

    agent.include(proto, publish_manifest=True)

    @agent.on_event("startup")
    async def show_address(ctx: Context):
        ctx.logger.info(f"{name} address: {agent.address}")

    return agent
