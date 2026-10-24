"""Campaign state for the orchestrator, kept in ctx.storage (JSON-serialisable values only)."""

import copy
import os
from datetime import datetime, timedelta, timezone

from uagents import Context

from common import env_bool

IST = timezone(timedelta(hours=5, minutes=30))
LOG_CAP = 500
MAX_REDOS = 2

# States
IDLE, SETUP, BUILDING_ICP, AWAIT_ICP = "IDLE", "SETUP", "BUILDING_ICP", "AWAIT_ICP"
FINDING, AWAIT_LEADS = "FINDING", "AWAIT_LEADS"
FINDING_CONTACTS, AWAIT_CONTACTS = "FINDING_CONTACTS", "AWAIT_CONTACTS"
QUALIFYING, AWAIT_REVIEWS = "QUALIFYING", "AWAIT_REVIEWS"
DRAFTING, AWAIT_DRAFTS = "DRAFTING", "AWAIT_DRAFTS"
AWAIT_SEND, SENDING, DONE, PAUSED = "AWAIT_SEND", "SENDING", "DONE", "PAUSED"
STEP_FAILED = "STEP_FAILED"  # a step errored; camp["failed_gate"] says which -> "retry" or "skip"

# gate key -> (running state, waiting state)
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
    "settings": {
        "ask_before": {g: True for g in GATE_ORDER},
        "daily_cap": int(os.getenv("DAILY_CAP", "5")),
        "dry_run": env_bool("DRY_RUN", True),
    },
    "style_rules": [],
    "pending_style_rule": None,  # a general edit_draft instruction waiting for the user's yes / "just this one"
    "draft_skipped": [],  # draft_emails "skipped" entries (no contact / LLM failed)
    "log": [],
}


def storage_key(sender: str) -> str:
    return "user:owner" if env_bool("OWNER_MODE", False) else f"user:{sender}"


def load(ctx: Context, sender: str) -> dict:
    camp = ctx.storage.get(storage_key(sender))
    fresh = copy.deepcopy(DEFAULT_CAMPAIGN)
    if not isinstance(camp, dict):
        return fresh
    fresh.update(camp)
    return fresh


def save(ctx: Context, sender: str, camp: dict) -> None:
    camp["log"] = camp["log"][-LOG_CAP:]
    ctx.storage.set(storage_key(sender), camp)


def new_campaign(camp: dict) -> dict:
    """Start over but keep the user's settings and learned style rules."""
    fresh = copy.deepcopy(DEFAULT_CAMPAIGN)
    fresh["settings"], fresh["style_rules"] = camp["settings"], camp["style_rules"]
    fresh["log"] = camp["log"]
    fresh["campaign_id"] = "c_" + datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    return fresh


def log(camp: dict, step: str, action: str, actor: str = "user", detail: str = "") -> None:
    """actor = user | ai_auto | system"""
    camp["log"].append({"ts": datetime.now(IST).isoformat(timespec="seconds"), "step": step,
                        "action": action, "actor": actor, "detail": detail[:300]})


def pending_gate(camp: dict) -> str | None:
    return WAITING_TO_GATE.get(camp["state"])
