"""Payment Protocol (AgentPaymentProtocol 0.1.0) for the orchestrator, seller role: a paid "verified lead pack".

Flow: the user says "buy lead pack" -> offer() sends RequestPayment -> the buyer replies CommitPayment
      (-> verify -> CompletePayment + the pack, or CancelPayment) or RejectPayment.

Optional monetisation demo: it never changes the campaign state, so the free flow and the approval gates
are untouched. On-chain verification is not implemented, so every verification is a clearly labelled DEMO
(mock) check of the commit against our open request (amount, currency, recipient, reference, transaction id).
The pack only contains data the campaign already verified (crawled/OSM emails with their source URLs).

Env: LEADPACK_PRICE (default "0.1"), LEADPACK_CURRENCY ("FET"), LEADPACK_PAYMENT_METHOD ("fet_direct"),
     LEADPACK_RECIPIENT (wallet address; default the agent address).
"""

import os
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

import state as st
from common import chat_text, env_bool, redact

DEADLINE_S = 600
FAST_INTENTS = {"buy lead pack": "buy_lead_pack", "unlock full list": "buy_lead_pack"}

pay_proto = Protocol(spec=payment_protocol_spec, role="seller")


def price() -> tuple[str, str, str]:
    """-> (amount, currency, payment_method) from the environment."""
    return (os.getenv("LEADPACK_PRICE", "0.1").strip() or "0.1", os.getenv("LEADPACK_CURRENCY", "FET").strip() or "FET",
            os.getenv("LEADPACK_PAYMENT_METHOD", "fet_direct").strip() or "fet_direct")


def recipient(ctx: Context) -> str:
    return os.getenv("LEADPACK_RECIPIENT", "").strip() or ctx.agent.address


def verification_mode() -> str:
    """Only a demo check exists (no on-chain verifier is configured); say so wherever a payment is verified."""
    return "DEMO, dry run" if env_bool("DRY_RUN", True) else "DEMO, no on-chain verification configured"


def _pending_key(sender: str) -> str:
    return f"payment:{sender}"


def _audit(ctx: Context, sender: str, action: str, detail: str = "") -> None:
    """Add a "payment" entry to the buyer's campaign audit log. Sync (no await between load and save)."""
    try:
        camp = st.load(ctx, sender)
        st.log(camp, "payment", action, "system", detail)
        st.save(ctx, sender, camp)
    except Exception as err:  # noqa: BLE001 - auditing must never break a payment
        ctx.logger.warning(f"payment audit failed: {redact(err)}")


def pack_rows(camp: dict) -> list[str]:
    """One line per verified contact: business, email, where it was published, fit score."""
    leads = {lead["id"]: lead for lead in camp.get("leads", [])}
    fit = {r.get("lead_id"): r.get("fit_score") for r in camp.get("reviews", [])}
    rows = []
    for c in camp.get("contacts", []):
        lead = leads.get(c.get("lead_id"), {})
        score = f" · fit {fit[c.get('lead_id')]}" if fit.get(c.get("lead_id")) is not None else ""
        rows.append(f"- **{lead.get('name', c.get('lead_id'))}** · {c.get('email')} · {c.get('role', '?')} · "
                    f"source {c.get('source_url') or c.get('source', '?')} · {lead.get('website') or 'no website'}{score}")
    return rows


def lead_pack_request(ctx: Context, camp: dict) -> RequestPayment:
    amount, currency, method = price()
    city = (camp.get("target") or {}).get("city") or "your city"
    return RequestPayment(
        accepted_funds=[Funds(amount=amount, currency=currency, payment_method=method)],
        recipient=recipient(ctx), deadline_seconds=DEADLINE_S,
        reference=f"leadpack-{camp.get('campaign_id') or 'none'}-{uuid4().hex[:8]}",
        description=f"LeadReach verified lead pack: {len(camp.get('contacts', []))} published contacts in {city}",
        metadata={"product": "verified_lead_pack", "verification": verification_mode()},
    )


async def offer(ctx: Context, sender: str, camp: dict) -> None:
    """Chat intent "buy lead pack": send a RequestPayment. Logs into `camp` (the caller saves it). Never raises."""
    try:
        if not camp.get("contacts"):
            await ctx.send(sender, chat_text("A lead pack needs verified contacts first: run a campaign to the contacts "
                                             "step (free). Paying is optional and never needed for the free flow."))
            return
        req = lead_pack_request(ctx, camp)
        ctx.storage.set(_pending_key(sender), {"reference": req.reference, "amount": req.accepted_funds[0].amount,
                                               "currency": req.accepted_funds[0].currency,
                                               "recipient": req.recipient})
        st.log(camp, "payment", "requested", "system", f"{req.reference} {req.accepted_funds[0].amount} "
                                                       f"{req.accepted_funds[0].currency}")
        funds = req.accepted_funds[0]
        await ctx.send(sender, chat_text(
            f"💳 **Verified lead pack**: {req.description}, with source URLs, for **{funds.amount} {funds.currency}**. "
            f"I've sent a payment request (ref `{req.reference}`, valid {DEADLINE_S // 60} min). "
            f"Verification is a **{verification_mode()}** check. This is optional: your campaign and approvals "
            "carry on as normal."))
        await ctx.send(sender, req)
    except Exception as err:  # noqa: BLE001
        ctx.logger.warning(f"payment offer failed: {redact(err)}")
        await _safe_send(ctx, sender, chat_text(f"I couldn't create the payment request ({redact(err)}). "
                                                "The free campaign is unaffected."))


def check_commit(msg: CommitPayment, pending: dict | None) -> str:
    """Demo verification of a commit against our open request. -> "" if valid, else the reason."""
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


async def _safe_send(ctx: Context, to: str, msg) -> None:
    try:
        await ctx.send(to, msg)
    except Exception as err:  # noqa: BLE001
        ctx.logger.warning(f"payment send failed: {redact(err)}")


@pay_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    try:
        pending = ctx.storage.get(_pending_key(sender))
        reason = check_commit(msg, pending if isinstance(pending, dict) else None)
        mode = verification_mode()
        if reason:
            ctx.logger.info(f"payment {msg.transaction_id} cancelled ({mode}): {reason}")
            _audit(ctx, sender, "cancelled", f"{msg.transaction_id}: {reason}")
            await _safe_send(ctx, sender, CancelPayment(transaction_id=msg.transaction_id, reason=reason))
            await _safe_send(ctx, sender, chat_text(f"Payment not accepted: {reason}. Nothing was charged by me; "
                                                    "your campaign is unaffected."))
            return
        ctx.storage.remove(_pending_key(sender))
        ctx.logger.info(f"payment {msg.transaction_id} verified ({mode} - not checked on-chain)")
        _audit(ctx, sender, f"completed ({mode})", f"{msg.transaction_id} {msg.funds.amount} {msg.funds.currency}")
        await _safe_send(ctx, sender, CompletePayment(transaction_id=msg.transaction_id))
        rows = pack_rows(st.load(ctx, sender))
        await _safe_send(ctx, sender, chat_text(
            f"✅ Payment `{msg.transaction_id}` accepted ({mode} verification). Your **verified lead pack** "
            f"({len(rows)} contacts, each with the page it was published on):\n" + ("\n".join(rows) or "none")))
    except Exception:  # noqa: BLE001 - handlers never raise
        ctx.logger.exception("payment commit failed")
        await _safe_send(ctx, sender, CancelPayment(transaction_id=getattr(msg, "transaction_id", None),
                                                    reason="internal error"))


@pay_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    try:
        ctx.storage.remove(_pending_key(sender))
        ctx.logger.info(f"payment rejected: {msg.reason}")
        _audit(ctx, sender, "rejected", msg.reason or "")
        await _safe_send(ctx, sender, chat_text("No problem, no lead pack. Your free campaign carries on as normal."))
    except Exception:  # noqa: BLE001 - handlers never raise
        ctx.logger.exception("payment reject failed")
