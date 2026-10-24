"""leadreach-sender - Agentverse HOSTED port of LeadReach / Harvest Agent agents/sender.py.

Single self-contained file. Uses the pre-provided global `agent` (do not construct Agent or call run()).
Secrets (Agentverse -> agent -> Secrets): none required (DRY_RUN forced true in code; TEST_REDIRECT_TO optional, unused)
Protocol: leadreach-tools 0.1.0, ToolRequest -> ToolResponse (identical to the local agents, same digest).
"""

import asyncio
import json
import os
import re
import time

from uagents import Context, Model, Protocol


# ======================= hosted runtime glue (replaces common.py) =======================
# Agentverse hosted: the global `agent` is pre-provided. No Agent(...), no agent.run(), no .env, no local files.

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



# ======================= sender.py =======================
# SAFETY: this hosted sender NEVER sends email. DRY_RUN is forced to True whatever the secret says, there is no
# email-provider client in this file, and send_emails answers ok=False exactly like the local agent (the
# orchestrator then offers "skip" to finish the campaign).
DRY_RUN = True  # forced; os.environ "DRY_RUN" is deliberately ignored
TEST_REDIRECT_TO = os.environ.get("TEST_REDIRECT_TO", "")  # read for parity; unused because nothing is sent


async def send_emails(args: dict) -> dict:
    if not isinstance(args.get("drafts"), list) or not args["drafts"]:
        raise ValueError("missing 'drafts' list")
    raise NotImplementedError("send_emails is not implemented yet (milestone M6, stretch)")


register_specialist(
    name="leadreach-sender",
    description='Sends approved outreach emails within daily caps and send windows (dry-run by default).',
    tools={"send_emails": send_emails},
)
