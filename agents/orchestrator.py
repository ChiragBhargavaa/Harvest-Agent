"""Orchestrator agent "leadreach": the one brands talk to inside ASI:One.

Flow: ASI:One chat -> Chat Protocol -> intent (card JSON or ASI:One LLM) -> campaign state machine
      -> specialist agents via send_and_receive -> checkpoint card (approval gate) -> next step.

Pipeline + gates: icp -> leads -> contacts -> reviews -> drafts -> send. Every gate is ON by default;
when a gate is off the step auto-approves, is logged as actor "ai_auto", and an FYI is posted.

Run:  python agents/orchestrator.py
Then open the Agent Inspector link from the logs -> Connect -> Mailbox.
"""

import asyncio
import importlib
import json
import os
import re
import time
from urllib.parse import urlparse
from uuid import uuid4

import yaml
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, chat_protocol_spec

import cards
import llm
import payments
import state as st
from common import (
    REQUEST_ID_KEY,
    ROOT,
    ToolRequest,
    ToolResponse,
    ack,
    chat_text,
    env_bool,
    env_seconds,
    redact,
    require_env,
    text_of,
)

# tool -> (env var with the specialist address, primary wait seconds; override with LEADREACH_TIMEOUT_<TOOL>)
# Per step: wait the primary time; no reply -> resend ONCE with the same _request_id (the specialist dedupes it) and wait
# RESEND_WAIT_S; still nothing -> run the specialist's tool in-process ("fallback: local <tool>"). Worst case per step
# (primary + resend + local run) <= STEP_WORST_S: the specialists cap their own work (FINDER_BUDGET_S 50 s,
# CONTACT_BUDGET_S 50 s, ANALYST_BUDGET_S 60 s, copywriter 2 x 40 s) and the local run gets what is left.
SPECIALISTS = {
    "find_leads": ("LEAD_FINDER_AGENT_ADDRESS", 60),
    "find_contacts": ("CONTACT_FINDER_AGENT_ADDRESS", 60),
    "qualify_leads": ("LEAD_ANALYST_AGENT_ADDRESS", 70),
    "draft_emails": ("COPYWRITER_AGENT_ADDRESS", 60),
    "send_emails": ("SENDER_AGENT_ADDRESS", 30),
}
LOCAL_TOOLS = {"find_leads": "lead_finder", "find_contacts": "contact_finder", "qualify_leads": "lead_analyst",
               "draft_emails": "copywriter", "send_emails": "sender"}  # tool -> module with `async def <tool>(args)`
RESEND_WAIT_S = env_seconds("RESEND_WAIT_S", 30)
STEP_WORST_S = 180
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
FAST_INTENTS = {  # exact phrases handled without an LLM call
    "approve": "approve", "yes": "approve", "ok": "approve", "looks good": "approve",
    "redo": "reject", "search again": "reject", "retry": "retry", "skip": "skip", "pause": "pause", "stop": "pause",
    "resume": "resume", "continue": "resume", "status": "status", "help": "help", "settings": "settings",
    "log": "show_log", "show log": "show_log", "new campaign": "start_campaign", "start": "start_campaign",
    "call list": "call_list",
}
FAST_INTENTS.update(payments.FAST_INTENTS)  # "buy lead pack" / "unlock full list" (optional Payment Protocol demo)
STYLE_RULE_DECLINE = {"no", "just this one", "just this draft", "only this one", "only this draft"}
SKIP_REASONS = {"no_contact": "no email found", "llm_failed": "the writer failed", "timed_out": "lookup timed out"}
MENTION_RE = re.compile(r"^\s*@agent1[a-z0-9]+\s*", re.I)  # ASI:One prefixes "@agent1q... " to the user's text
PACK_WORDS = ("lead pack", "buy pack")
HELP = (
    "I'm **LeadReach**: I find local businesses that fit your brand, their published emails, evidence-based "
    "reasons to contact them, and draft personal emails - stopping for your approval at every step.\n\n"
    "- Start: *\"I sell organic coffee beans - find cafés and bakeries in Jaipur\"*\n"
    "- At a checkpoint: **approve**, **remove 2, 5**, **redo** + what to change, **show lead 3**\n"
    "- Anytime: **status**, **settings**, **pause** / **resume**, **log**\n"
    "- Trust a step? *\"stop asking me about contacts\"*"
)

agent = Agent(
    name=os.getenv("ORCH_NAME", "leadreach"),
    seed=require_env("ORCH_SEED"),
    port=int(os.getenv("ORCH_PORT", "8001")),
    mailbox=env_bool("MAILBOX", True),
    publish_agent_details=True,
    handle_messages_concurrently=True,
    description="Approval-first lead finder for brands: finds local businesses in a city, their published "
                "emails and evidence-based hooks, then drafts personal outreach emails.",
    readme_path=str(ROOT / "agents" / "profiles" / "orchestrator.md"),
)
chat_proto = Protocol(spec=chat_protocol_spec)


def load_profile() -> dict:
    try:
        return yaml.safe_load((ROOT / "config" / "brand_profile.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


# ---------------- intent ----------------

def parse_card_action(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("action"):
        args = {k: v for k, v in data.items() if k != "action"}
        return {"intent": data["action"], "args": args, "confidence": 1.0}
    return None


async def understand(text: str, camp: dict) -> dict:
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
    if m := re.fullmatch(r"(remove|drop|delete)\s+([\d,\sand]+)", low):
        return {"intent": "remove_items", "args": {"ids": [int(x) for x in re.findall(r"\d+", m.group(2))]},
                "confidence": 1.0}
    if m := re.fullmatch(r"show (?:lead|review)\s+(\d+)", low):
        return {"intent": "show_lead", "args": {"ids": [int(m.group(1))]}, "confidence": 1.0}
    if any(w in low for w in PACK_WORDS):  # e.g. "can I buy the lead pack?" - any state, no LLM call
        return {"intent": "buy_lead_pack", "args": {}, "confidence": 1.0}
    try:
        return await llm.parse_intent(text, camp["state"], st.pending_gate(camp), camp.get("last_list_names", []))
    except Exception:  # noqa: BLE001 - degrade to a clarifying question
        return {"intent": "unknown", "args": {}, "confidence": 0.0}


# ---------------- specialists ----------------

def tool_timeout(tool: str) -> float:
    """Seconds to wait for a specialist: LEADREACH_TIMEOUT_<TOOL> if it is a positive number, else the default."""
    return env_seconds(f"LEADREACH_TIMEOUT_{tool.upper()}", SPECIALISTS[tool][1])


async def call_tool(ctx: Context, tool: str, args: dict) -> tuple[dict | None, str]:
    """-> (result, error). Never raises. Send, resend once (same _request_id), then run the tool locally."""
    env_name, timeout = SPECIALISTS[tool][0], tool_timeout(tool)
    name = SPECIALIST_NAMES[tool]
    address = os.getenv(env_name, "")
    if not address:
        return None, f"the {tool} agent address is not configured ({env_name})"
    rid = uuid4().hex
    req = ToolRequest(tool=tool, args_json=json.dumps({**args, REQUEST_ID_KEY: rid}))
    start = time.monotonic()
    for attempt, wait in (("send", timeout), ("resend", RESEND_WAIT_S)):
        try:
            reply, status = await ctx.send_and_receive(address, req, response_type=ToolResponse, timeout=wait)
        except Exception as err:  # noqa: BLE001 - treated like no answer
            reply, status = None, f"send error {redact(err)[:200]}"
        took = time.monotonic() - start
        if isinstance(reply, ToolResponse):
            ctx.logger.info(f"{tool} {rid[:8]} response after {took:.1f}s ({attempt}, ok={reply.ok})")
            if not reply.ok:
                return None, reply.error or "unknown error"
            try:
                return json.loads(reply.result or "{}"), ""
            except ValueError:
                return None, f"the {tool} agent returned invalid JSON"
        ctx.logger.warning(f"{tool} {rid[:8]} no response after {took:.1f}s ({attempt}, status {status})")

    ctx.logger.warning(f"fallback: local {tool} ({rid[:8]})")
    limit = max(1.0, STEP_WORST_S - timeout - RESEND_WAIT_S)
    try:
        fn = getattr(importlib.import_module(LOCAL_TOOLS[tool]), tool)
        result = await asyncio.wait_for(fn(json.loads(json.dumps(args))), limit)  # a copy, like the wire
        result = json.loads(json.dumps(result))
    except Exception as err:  # noqa: BLE001 - incl. the local time limit
        why = (f"timed out after {limit:g}s" if isinstance(err, asyncio.TimeoutError)
               else redact(f"{type(err).__name__}: {err}")[:200])
        ctx.logger.error(f"fallback: local {tool} ({rid[:8]}) failed after {time.monotonic() - start:.1f}s: {why}")
        return None, f"{name} timed out (no answer within {timeout:g}s + resend {RESEND_WAIT_S:g}s; local run: {why})"
    ctx.logger.info(f"fallback: local {tool} ({rid[:8]}) done after {time.monotonic() - start:.1f}s")
    return result, ""


# ---------------- rendering ----------------

def _host(url: str | None) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.") if url else ""


def lead_rows(leads: list[dict]) -> list[dict]:
    rows = []
    for i, lead in enumerate(leads, 1):
        bits = [_host(lead.get("website")) or "no website"]
        if lead.get("website") and lead.get("verified") is False:
            bits[0] += " (unverified)"
        if lead.get("osm_emails"):
            bits.append("email on OSM")
        if lead.get("phone"):
            bits.append("phone")
        bits.append(lead.get("source", "?").upper())
        rows.append({"label": f"{i}. {lead['name']}", "value": " · ".join(bits)})
    return rows


def checkpoint(gate: str, camp: dict, note: str = "") -> ChatMessage:
    """The approval card for a gate. Also records what the numbered list refers to. `note` is shown on draft cards."""
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
        return cards.review_card("Who should I look for?", rows, "icp", "Approve & find leads", "Change it",
                                 intro="Here's the customer profile I'll search for.")
    if gate == "leads":
        leads = camp["leads"]
        camp["last_list"] = [lead["id"] for lead in leads]
        camp["last_list_names"] = [lead["name"] for lead in leads]
        extra = f"{len(camp['call_list'])} more businesses have only a phone (say **call list**)." \
            if camp["call_list"] else ""
        return cards.review_card(f"{len(leads)} leads in {camp['icp']['city']}", lead_rows(leads), "leads",
                                 "Approve & find contacts", "Search again",
                                 intro="\n".join(camp.get("source_notes", [])), extra_hint=extra)
    if gate == "contacts":
        names = {lead["id"]: lead["name"] for lead in camp["leads"]}
        rows = [{"label": f"{i}. {names.get(c['lead_id'], c['lead_id'])}",
                 "value": f"{c['email']} · {c.get('role', '?')} · "
                          f"{'⚠ low' if c.get('confidence') == 'low' else c.get('confidence', '?')}"}
                for i, c in enumerate(camp["contacts"], 1)]
        camp["last_list"] = [c["lead_id"] for c in camp["contacts"]]
        return cards.review_card(f"{len(rows)} contacts found", rows, "contacts", "Approve & review websites")
    if gate == "reviews":
        names = {lead["id"]: lead["name"] for lead in camp["leads"]}
        rows = []
        for i, r in enumerate(camp["reviews"], 1):
            hook = (r.get("hooks") or [{}])[0].get("observation", "no hook")
            rows.append({"label": f"{i}. {names.get(r['lead_id'], r['lead_id'])}",
                         "value": f"fit {r.get('fit_score', '?')} · {hook}"})
        camp["last_list"] = [r["lead_id"] for r in camp["reviews"]]
        return cards.review_card("Lead reviews", rows, "reviews", "Approve & draft emails",
                                 extra_hint="Say **show lead 2** for the evidence.")
    if gate == "drafts":
        i = camp["draft_cursor"]
        if not note and i == 0 and camp["draft_skipped"]:
            names = {lead["id"]: lead["name"] for lead in camp["leads"]}
            note = "No draft for: " + "; ".join(
                f"{names.get(s['lead_id'], s['lead_id'])} ({SKIP_REASONS.get(s.get('reason'), s.get('reason'))})"
                for s in camp["draft_skipped"]) + "."
        return cards.draft_card(camp["drafts"][i], i + 1, len(camp["drafts"]), note)
    s = camp["settings"]
    rows = [{"label": "Approved drafts", "value": str(sum(1 for d in camp["drafts"] if d.get("approved")))},
            {"label": "Dry run", "value": "on" if s["dry_run"] else "OFF - real emails"},
            {"label": "Redirect", "value": os.getenv("TEST_REDIRECT_TO") or "none"},
            {"label": "Daily cap", "value": str(s["daily_cap"])}]
    return cards.review_card("Ready to send", rows, "send", "Send", "Not yet")


# ---------------- pipeline ----------------

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
            "dry_run": camp["settings"]["dry_run"], "redirect_to": os.getenv("TEST_REDIRECT_TO", ""),
            "daily_cap": camp["settings"]["daily_cap"]}


def store_result(gate: str, camp: dict, result: dict) -> int:
    """Save a step's output (re-running replaces, never duplicates). -> item count."""
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
    return len(result.get("sent", [])) + len(result.get("queued", []))


async def run_from(ctx: Context, sender: str, camp: dict, gate: str) -> None:
    """Run `gate` and continue through every gate the user switched off, stopping at the next checkpoint."""
    while gate:
        if gate == "icp":
            await build_icp(ctx, sender, camp)
            if camp["state"] != st.BUILDING_ICP:
                return
        else:
            camp["state"] = st.GATE_STATES[gate][0]
            st.save(ctx, sender, camp)
            await ctx.send(sender, chat_text(PROGRESS[gate]))
            result, error = await call_tool(ctx, GATE_TOOL[gate], step_args(gate, camp))
            if result is None:
                camp["state"], camp["failed_gate"] = st.STEP_FAILED, gate
                st.log(camp, gate, "failed", "system", error)
                name = SPECIALIST_NAMES[GATE_TOOL[gate]]
                await ctx.send(sender, chat_text(
                    f"⚠ {name} timed out — reply **retry** to try again, or **skip** to move on."
                    if error.startswith(f"{name} timed out") else
                    f"⚠ The **{gate}** step failed: {error}\n\nReply **retry** to try again, or **skip** to move on."))
                return
            if gate == "drafts":
                log_draft_skips(ctx, result)
            count = store_result(gate, camp, result)
            st.log(camp, gate, f"produced {count}", "system")
            if gate in ("leads", "drafts") and count == 0:
                camp["state"] = st.GATE_STATES[gate][1]
                await ctx.send(sender, chat_text(f"I found nothing for the **{gate}** step. "
                                                 "Reply **redo** with what to change (another city, more business "
                                                 "types), or **add lead** with a name and website."))
                return
        if camp["settings"]["ask_before"].get(gate, True):
            camp["state"] = st.GATE_STATES[gate][1]
            await ctx.send(sender, checkpoint(gate, camp))
            return
        st.log(camp, gate, "auto-approved", "ai_auto")
        await ctx.send(sender, chat_text(f"FYI: **{gate}** auto-approved (you turned that checkpoint off)."))
        gate = next_gate(gate)
    camp["state"] = st.DONE
    await ctx.send(sender, chat_text("Campaign complete. Say **status** for a summary or **new campaign**.",
                                     end_session=True))


def log_draft_skips(ctx: Context, result: dict) -> None:
    """Why each lead got no draft (the card only says "the writer failed"), so live failures are diagnosable."""
    for s in result.get("skipped") or []:
        detail = f": {redact(s['detail'])}" if s.get("detail") else ""
        ctx.logger.warning(f"no draft for {s.get('lead_id')} ({s.get('reason')}){detail}")


def next_gate(gate: str) -> str | None:
    i = st.GATE_ORDER.index(gate)
    return st.GATE_ORDER[i + 1] if i + 1 < len(st.GATE_ORDER) else None


async def build_icp(ctx: Context, sender: str, camp: dict) -> None:
    camp["state"] = st.BUILDING_ICP
    try:
        camp["icp"] = await llm.build_icp(camp["brief"], camp["target"]["city"], load_profile())
    except Exception as err:  # noqa: BLE001
        camp["state"], camp["failed_gate"] = st.STEP_FAILED, "icp"
        st.log(camp, "icp", "failed", "system", str(err))
        await ctx.send(sender, chat_text(f"⚠ I couldn't understand the brief right now ({err}). Reply **retry**."))
        return
    st.log(camp, "icp", "built", "system", json.dumps(camp["icp"])[:300])


# ---------------- handlers ----------------

async def start_campaign(ctx: Context, sender: str, camp: dict, args: dict) -> dict:
    brief = (args.get("brief") or "").strip()
    city = (args.get("city") or "").strip()
    if not brief or not city:
        camp["state"] = st.SETUP
        default_city = load_profile().get("targeting", {}).get("default_city")
        await ctx.send(sender, cards.setup_form(None if str(default_city).startswith("❓") else default_city))
        return camp
    camp = st.new_campaign(camp)
    camp["brief"], camp["target"]["city"] = brief, city
    if args.get("limit"):
        camp["target"]["limit"] = max(1, min(int(float(args["limit"])), 15))
    if args.get("language"):
        camp["target"]["language"] = args["language"]
    st.log(camp, "setup", "campaign started", "user", f"{brief} | {city}")
    await run_from(ctx, sender, camp, "icp")
    return camp


async def approve(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = st.pending_gate(camp)
    if gate is None:
        await ctx.send(sender, chat_text("There's nothing waiting for approval. Say **status** or **help**."))
        return
    if await answer_style_rule(ctx, sender, camp, args, accepted=True):
        return
    if gate == "drafts" and camp["drafts"]:
        todo = camp["drafts"][camp["draft_cursor"]:] if args.get("all") else [camp["drafts"][camp["draft_cursor"]]]
        for draft in todo:
            draft["approved"] = True
        st.log(camp, "drafts", f"approved {len(todo)}")
        if args.get("all"):
            camp["draft_cursor"] = len(camp["drafts"]) - 1
        if camp["draft_cursor"] + 1 < len(camp["drafts"]):
            camp["draft_cursor"] += 1
            await ctx.send(sender, checkpoint("drafts", camp))
            return
    else:
        st.log(camp, gate, "approved")
    nxt = next_gate(gate)
    if nxt:
        await run_from(ctx, sender, camp, nxt)


async def reject(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = st.pending_gate(camp)
    if gate is None:
        await ctx.send(sender, chat_text("Nothing to redo right now. Say **status** or **help**."))
        return
    if await answer_style_rule(ctx, sender, camp, args, accepted=False):
        return
    if gate == "drafts" and camp["drafts"]:  # "Skip" on a draft card
        camp["drafts"][camp["draft_cursor"]]["approved"] = False
        st.log(camp, "drafts", f"skipped {camp['drafts'][camp['draft_cursor']].get('lead_id')}")
        await approve_cursor_advance(ctx, sender, camp)
        return
    redo = camp["redo_count"].get(gate, 0)
    if redo >= st.MAX_REDOS:
        await ctx.send(sender, chat_text(f"I've redone **{gate}** {redo} times. Try changing the inputs: a different "
                                         "city or business type, or start a **new campaign**."))
        return
    camp["redo_count"][gate] = redo + 1
    feedback = (args.get("feedback") or "").strip()
    st.log(camp, gate, "rejected", "user", feedback)
    if gate == "icp" and feedback:
        camp["brief"] += f"\nUser feedback on the profile: {feedback}"
    if gate == "leads":
        camp["removed_ids"] = list(set(camp["removed_ids"]) | {lead["id"] for lead in camp["leads"]}) \
            if not feedback else camp["removed_ids"]
        if feedback and camp["icp"]:
            camp["brief"] += f"\nUser feedback on the leads: {feedback}"
            await run_from(ctx, sender, camp, "icp")
            return
    await run_from(ctx, sender, camp, gate)


async def approve_cursor_advance(ctx: Context, sender: str, camp: dict) -> None:
    if camp["draft_cursor"] + 1 < len(camp["drafts"]):
        camp["draft_cursor"] += 1
        await ctx.send(sender, checkpoint("drafts", camp))
    else:
        await run_from(ctx, sender, camp, "send")


async def redraft(ctx: Context, sender: str, camp: dict, lead_ids: list[str], extra_rules: list[str]) -> str:
    """Rewrite these leads' drafts in place (campaign style_rules + extra_rules). -> error ("" = ok)."""
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
    camp["drafts"] = [new.get(d["lead_id"], d) for d in camp["drafts"]]  # an LLM failure keeps the old draft
    return "" if new else "the writer returned no draft"


async def edit_draft(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    """A specific edit rewrites one draft; a general one is proposed as a style rule and waits for confirmation."""
    instruction = str(args.get("instruction") or "").strip()
    if st.pending_gate(camp) != "drafts" or not camp["drafts"]:
        await ctx.send(sender, chat_text("I can edit emails once the drafts are ready (at the drafts checkpoint)."))
        return
    if not instruction:
        await ctx.send(sender, chat_text("How should I change it? e.g. *\"shorter and warmer\"*."))
        return
    general = args.get("general")
    if general is True or (not isinstance(general, bool) and _truthy(general)):
        camp["pending_style_rule"] = instruction
        st.log(camp, "drafts", "style rule proposed", "system", instruction)
        await ctx.send(sender, cards.style_rule_card(instruction))
        return
    lead_ids = [d["lead_id"] for d in camp["drafts"]]
    if args.get("lead_id") in lead_ids:  # a draft card's lead_id; otherwise the draft on screen
        camp["draft_cursor"] = lead_ids.index(args["lead_id"])
    current = lead_ids[camp["draft_cursor"]]
    st.log(camp, "drafts", f"edited {current}", "user", instruction)
    error = await redraft(ctx, sender, camp, [current], [instruction])
    note = f"⚠ I couldn't rewrite it ({error}); here's the previous version." if error \
        else f"Rewrote this draft: *\"{instruction}\"*."
    await ctx.send(sender, checkpoint("drafts", camp, note))


async def answer_style_rule(ctx: Context, sender: str, camp: dict, args: dict, accepted: bool) -> bool:
    """Handle yes / "just this one" for a proposed style rule. -> False if no proposal was waiting."""
    rule = camp.get("pending_style_rule")
    if (not rule or st.pending_gate(camp) != "drafts" or args.get("all")
            or args.get("step") not in (None, "", "style_rule")):
        camp["pending_style_rule"] = None  # e.g. a draft card's own approve/skip: the proposal lapses
        return False
    camp["pending_style_rule"] = None
    if accepted:
        if rule not in camp["style_rules"]:
            camp["style_rules"].append(rule)
        st.log(camp, "drafts", "style rule added", "user", rule)
        todo = [d["lead_id"] for d in camp["drafts"][camp["draft_cursor"]:] if not d.get("approved")]
        error = await redraft(ctx, sender, camp, todo, []) if todo else ""
        note = f"Saved style rule *\"{rule}\"*" + (f", but I couldn't rewrite the drafts ({error})." if error
                                                   else f" and rewrote {len(todo)} draft(s).")
    else:
        current = camp["drafts"][camp["draft_cursor"]]["lead_id"]
        st.log(camp, "drafts", f"edited {current}", "user", rule)
        error = await redraft(ctx, sender, camp, [current], [rule])
        note = f"⚠ I couldn't rewrite it ({error})." if error else f"Applied *\"{rule}\"* to this draft only."
    await ctx.send(sender, checkpoint("drafts", camp, note))
    return True


async def remove_items(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    gate = st.pending_gate(camp)
    positions = [int(i) for i in args.get("ids", []) if str(i).isdigit()]
    ids = {camp["last_list"][p - 1] for p in positions if 0 < p <= len(camp["last_list"])}
    if gate not in ("leads", "contacts", "reviews") or not ids:
        await ctx.send(sender, chat_text("Tell me which numbers to remove from the last list, e.g. **remove 2, 5**."))
        return
    camp["removed_ids"] = list(set(camp["removed_ids"]) | ids)
    for key in ("leads", "contacts", "reviews"):
        camp[key] = [x for x in camp[key] if x.get("id", x.get("lead_id")) not in ids]
    st.log(camp, gate, f"removed {len(ids)}", "user", ", ".join(sorted(ids)))
    await ctx.send(sender, checkpoint(gate, camp))


async def show_lead(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    ids = args.get("ids") or []
    lead_ids = camp["last_list"] or [lead["id"] for lead in camp["leads"]]
    if not ids or not str(ids[0]).isdigit() or not 0 < int(ids[0]) <= len(lead_ids):
        await ctx.send(sender, chat_text("Which one? e.g. **show lead 2**."))
        return
    lead_id = lead_ids[int(ids[0]) - 1]
    lead = next((x for x in camp["leads"] if x["id"] == lead_id), {"name": lead_id})
    review = next((r for r in camp["reviews"] if r.get("lead_id") == lead_id), {})
    rows = [{"label": "Address", "value": lead.get("address") or "-"},
            {"label": "Website", "value": lead.get("website") or "-"},
            {"label": "Phone", "value": lead.get("phone") or "-"},
            {"label": "Source", "value": lead.get("source_ref") or lead.get("source", "-")}]
    rows += [{"label": "Fit score", "value": str(review.get("fit_score", "-"))}] if review else []
    rows += [{"label": h.get("observation", "hook"), "value": f"\"{h.get('evidence')}\" ({h.get('evidence_url')})"}
             for h in review.get("hooks", [])]
    narration = f"**{lead['name']}**\n" + "\n".join(f"- {r['label']}: {r['value']}" for r in rows)
    await ctx.send(sender, cards.detail_card(lead["name"], rows, [], narration, terminal=True))


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on", "checked"}


async def update_settings(ctx: Context, sender: str, camp: dict, args: dict) -> None:
    s, changes = camp["settings"], []
    asks = dict(args.get("ask_before") or {})
    asks.update({k[4:]: v for k, v in args.items() if k.startswith("ask_")})
    for gate, value in asks.items():
        if gate in s["ask_before"]:
            s["ask_before"][gate] = _truthy(value) if not isinstance(value, bool) else value
            changes.append(f"{gate} will {'ask you first' if s['ask_before'][gate] else 'auto-approve'}")
    if args.get("daily_cap") not in (None, ""):
        s["daily_cap"] = max(1, min(int(float(args["daily_cap"])), 50))
        changes.append(f"daily cap {s['daily_cap']}")
    if "dry_run" in args:  # only ever changed by an explicit user settings change
        s["dry_run"] = args["dry_run"] if isinstance(args["dry_run"], bool) else _truthy(args["dry_run"])
        changes.append(f"dry run {'on' if s['dry_run'] else 'OFF'}")
    if not changes:
        await ctx.send(sender, cards.settings_form(s))
        return
    st.log(camp, "settings", "; ".join(changes))
    await ctx.send(sender, chat_text("Done: " + "; ".join(changes) + "."))


async def clarify(ctx: Context, sender: str, camp: dict) -> None:
    gate = st.pending_gate(camp)
    options = {
        None: "start a campaign (e.g. *\"find cafés in Jaipur for my coffee brand\"*), **status** or **help**",
        "drafts": "**approve**, **skip**, **approve all**, or tell me how to edit the draft",
    }.get(gate, "**approve**, **remove 2, 5**, **redo** with feedback, or **show lead 3**")
    await ctx.send(sender, chat_text(f"Sorry, I didn't catch that. Here you can: {options}."))


async def dispatch(ctx: Context, sender: str, camp: dict, cmd: dict) -> dict:
    intent, args = cmd["intent"], cmd.get("args") or {}
    if camp["state"] == st.PAUSED and intent not in ("resume", "status", "help", "show_log", "update_settings"):
        await ctx.send(sender, chat_text("Everything is paused. Say **resume** to continue."))
        return camp
    if intent == "help":
        await ctx.send(sender, chat_text(HELP))
    elif intent == "status":
        await ctx.send(sender, cards.status_card(camp))
    elif intent == "show_log":
        lines = [f"- {e['ts'][5:16]} · {e['step']} · {e['action']} ({e['actor']})" for e in camp["log"][-10:]]
        await ctx.send(sender, chat_text("**Audit log (last 10)**\n" + ("\n".join(lines) or "empty")))
    elif intent == "settings" or (intent == "update_settings" and not args):
        await ctx.send(sender, cards.settings_form(camp["settings"]))
    elif intent == "update_settings":
        await update_settings(ctx, sender, camp, args)
    elif intent == "pause":
        if camp["state"] != st.PAUSED:
            camp["paused_from"], camp["state"] = camp["state"], st.PAUSED
            st.log(camp, "campaign", "paused")
        await ctx.send(sender, chat_text("⏸ Paused. Nothing will run or send until you say **resume**."))
    elif intent == "resume":
        if camp["state"] == st.PAUSED:
            camp["state"], camp["paused_from"] = camp["paused_from"] or st.IDLE, None
            st.log(camp, "campaign", "resumed")
            gate = st.pending_gate(camp)
            await ctx.send(sender, checkpoint(gate, camp) if gate else chat_text("▶ Resumed."))
        else:
            await ctx.send(sender, chat_text("Nothing is paused."))
    elif intent == "start_campaign":
        camp = await start_campaign(ctx, sender, camp, args)
    elif camp["state"] == st.STEP_FAILED and intent in ("retry", "approve"):
        await run_from(ctx, sender, camp, camp.get("failed_gate") or "icp")
    elif camp["state"] == st.STEP_FAILED and intent == "skip":
        gate = camp.get("failed_gate") or "icp"
        st.log(camp, gate, "skipped after failure")
        nxt = next_gate(gate)
        await (run_from(ctx, sender, camp, nxt) if nxt else ctx.send(sender, chat_text("Nothing left to skip to.")))
    elif intent == "approve":
        await approve(ctx, sender, camp, args)
    elif intent == "reject":
        await reject(ctx, sender, camp, args)
    elif intent == "remove_items":
        await remove_items(ctx, sender, camp, args)
    elif intent == "show_lead":
        await show_lead(ctx, sender, camp, args)
    elif intent == "call_list":
        rows = [f"- {x['name']} · {x.get('phone')} · {x.get('address') or ''}" for x in camp["call_list"]]
        body = "\n".join(rows) or "none"
        await ctx.send(sender, chat_text("**Phone-only businesses** (no published email)\n" + body))
    elif intent == "edit_draft":
        await edit_draft(ctx, sender, camp, args)
    elif intent == "buy_lead_pack":
        await payments.offer(ctx, sender, camp)
    elif intent in ("add_lead", "import_list", "classify_reply"):
        await ctx.send(sender, chat_text(f"*{intent.replace('_', ' ')}* isn't part of this demo yet. {HELP}"))
    elif camp["state"] in (st.IDLE, st.SETUP, st.DONE) and cmd.get("confidence", 0) < 0.6:
        await ctx.send(sender, chat_text(HELP))
    else:
        await clarify(ctx, sender, camp)
    return camp


@chat_proto.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ack(msg))
    text = text_of(msg)
    if not text:  # e.g. a StartSessionContent-only message
        return
    ctx.logger.info(f"user {sender[:16]}...: {text[:200]}")
    camp = st.load(ctx, sender)
    try:
        cmd = await understand(text, camp)
        ctx.logger.info(f"intent: {cmd}")
        camp = await dispatch(ctx, sender, camp, cmd)
    except Exception:  # noqa: BLE001 - never leave the user without a reply
        ctx.logger.exception("handler failed")
        await ctx.send(sender, chat_text("Something went wrong on my side. Your campaign is saved - "
                                         "say **status** to see where we are, or **retry**."))
    st.save(ctx, sender, camp)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


# publish_manifest=True advertises the Chat Protocol so ASI:One can discover and talk to this agent
agent.include(chat_proto, publish_manifest=True)
agent.include(payments.pay_proto, publish_manifest=True)  # Payment Protocol, seller role (agents/payments.py)


@agent.on_event("startup")
async def show_address(ctx: Context):
    ctx.logger.info(f"Orchestrator address: {agent.address}")
    payments.DEFAULT_RECIPIENT = str(agent.wallet.address())
    missing = [env for env, _ in SPECIALISTS.values() if not os.getenv(env)]
    if missing:
        ctx.logger.warning(f"Specialist addresses not set yet: {missing} (run setup.sh)")


if __name__ == "__main__":
    agent.run()
