"""ASI:One Interactive Cards (card_protocol_version 1) for every LeadReach checkpoint.

A card is a ChatMessage with [TextContent(narration), MetadataContent(metadata)]. If the card fails
validation ASI:One shows only the narration, so every narration lists the items and says how to
reply in plain chat. Every selection carries "action" and "step" so card clicks parse as JSON.
"""

import json
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from common import now

MAX_ROWS = 15


def card(narration: str, kind: str, payload: dict, terminal: bool = False) -> ChatMessage:
    meta = {
        "card_protocol_version": "1",
        "requires_card_interaction": "false" if terminal else "true",
        "card_kind": kind,  # carousel | detail | form | review | custom
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
    rows = rows[:MAX_ROWS]
    payload = {
        "title": title,
        "summary_rows": rows,
        "approve_cta": {"label": approve_label, "selection": {"action": "approve", "step": step}, "primary": True},
        "reject_cta": {"label": reject_label, "selection": {"action": "reject", "step": step}},
    }
    narration = (f"{intro}\n\n" if intro else "") + f"**{title}**\n{_numbered(rows)}\n\n" + (
        f"Reply **approve**, **remove 2, 5**, or **redo** with what to change.{(' ' + extra_hint) if extra_hint else ''}"
    )
    return card(narration, "review", payload)


def detail_card(title: str, rows: list[dict], ctas: list[dict], narration: str, terminal: bool = False) -> ChatMessage:
    return card(narration, "detail", {"title": title, "summary_rows": rows[:MAX_ROWS], "ctas": ctas}, terminal)


def draft_card(draft: dict, index: int, total: int) -> ChatMessage:
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
    narration = (f"**Draft {index}/{total}** to {draft.get('to')}\n\n**Subject:** {draft.get('subject')}\n\n"
                 f"{draft.get('body_text')}\n\nReply **approve**, **skip**, **approve all**, or tell me how to edit it "
                 "(e.g. *\"shorter and warmer\"*).")
    return card(narration, "review", payload)


def status_card(camp: dict) -> ChatMessage:
    s = camp["settings"]
    rows = [
        {"label": "State", "value": camp["state"]},
        {"label": "City", "value": camp["target"].get("city") or "-"},
        {"label": "Leads / contacts / drafts",
         "value": f"{len(camp['leads'])} / {len(camp['contacts'])} / {len(camp['drafts'])}"},
        {"label": "Approvals on", "value": ", ".join(g for g, v in s["ask_before"].items() if v) or "none"},
        {"label": "Daily cap · dry run", "value": f"{s['daily_cap']} · {'on' if s['dry_run'] else 'off'}"},
    ]
    for e in camp["log"][-5:]:
        rows.append({"label": f"{e['ts'][11:16]} {e['step']}", "value": f"{e['action']} ({e['actor']})"})
    narration = "**Campaign status**\n" + _numbered(rows)
    return detail_card("Campaign status", rows, [], narration, terminal=True)
