"""Copywriter agent: `draft_emails` - one short personal email per lead (ASI:One + deterministic validator).

Pipeline per lead (<=DRAFT_CONCURRENCY = 15 leads at a time): pick the lead's primary contact (contacts arrive sorted per lead) ->
llm.draft_email(profile, lead, review, style_rules) -> validate(). Validator (HACKATHON_README §3.12): word count
<= max_words; subject <= 8 words and <= 60 chars; the exact unsubscribe line; sender name + brand present; no
{placeholders} or ❓; no price/cost/discount words unless pricing.mention_price; no existing customers, "trusted by"
or testimonials unless social_proof.can_name_customers with names or a count ("your customers" is fine);
hook_used matches one of the lead's review hooks. Also: no product claim (e.g. "single-origin", "cordless") that
the brief, the ICP product, the filled-in profile facts and the lead's review don't contain. On failure regenerate
once with the problems passed back; if it still fails, keep the draft with `warnings`. Problems that only an
unfilled brand profile causes (sender name, brand name, unsubscribe line) never trigger a regenerate: the LLM can't
fix them, so they stay as `warnings`. Leads without a contact, or
whose LLM call fails, go to `skipped` (no draft is ever written without the LLM). Low-confidence contacts are
drafted too: their approval is enforced at send time (§3.4).

Run:  python agents/copywriter.py
"""

import asyncio
import json
import logging
import re

import llm
from common import env_seconds, make_specialist, redact

NAME = "leadreach-writer"
log = logging.getLogger(NAME)  # the agent's own logger (uAgents names it after the agent)
DRAFT_CONCURRENCY = 15  # = the ≤15 leads per request (§3.6): one wave
# A live ASI:One draft call can take 30+ s, and an in-flight call cancelled by this timeout is never logged by
# httpx. One wave x 2 attempts x 100 s stays inside the orchestrator's 240 s draft_emails timeout.
LLM_TIMEOUT_S = env_seconds("LEADREACH_DRAFT_LLM_TIMEOUT", 100)
DEFAULT_MAX_WORDS = 150  # same default as llm.draft_email
MAX_SUBJECT_WORDS, MAX_SUBJECT_CHARS = 8, 60
MIN_HOOK_MATCH_CHARS = 10
PROFILE_GAP = "config/brand_profile.yaml"  # in every problem caused by an unfilled profile field
# Product qualities the LLM likes to add; each must appear in the brief / ICP product / known profile facts / review.
CLAIM_TERMS = (
    "single origin", "fresh roasted", "freshly roasted", "small batch", "organic", "artisan", "artisanal",
    "handcrafted", "hand crafted", "handmade", "award winning", "certified", "fair trade", "ethically sourced",
    "sustainably sourced", "direct trade", "premium", "specialty grade", "locally roasted", "farm to cup", "wholesale",
    # generic product-quality claims (e.g. for a trimmer)
    "cordless", "waterproof", "long battery", "battery life", "professional grade", "salon grade", "precision",
    "ergonomic", "lightweight", "light weight", "durable", "best in class", "fastest", "quietest", "warranty",
    "guarantee", "guaranteed",
)
PRICE_RE = re.compile(r"₹\s*\d|\b(?:rs|inr|usd)\.?\s*\d|\$\s*\d|\d[\d,.]*\s*(?:/-|rupees\b|rs\b|inr\b)"
                      r"|\b(?:price[sd]?|pricing|discount(?:s|ed)?|costs?|costing|affordabl[ey]|cheap(?:er|est|ly)?)\b"
                      r"|%\s*off\b|\bper\s*cent\s+off\b", re.I)
# Claims about the brand's existing customers. "your customers" (the lead's own) never matches.
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


def _norm(text) -> str:
    text = str(text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).strip(" \"'.,;:")


def _known(value) -> str:
    """A brand-profile value, or "" if it is missing or still a ❓ placeholder."""
    value = str(value or "").strip()
    return "" if not value or "❓" in value else value


def _hook_used(value) -> str | None:
    value = str(value or "").strip()
    return None if value.lower() in ("", "null", "none") else value


def hook_matches(hook_used: str, hooks: list[dict]) -> bool:
    """hook_used must be (or quote) a review hook's observation, evidence or angle."""
    used = _norm(hook_used)
    for h in hooks or []:
        for field in (h.get("observation"), h.get("evidence"), h.get("angle")):
            f = _norm(field)
            if f and (used == f or (len(used) >= MIN_HOOK_MATCH_CHARS and used in f)
                      or (len(f) >= MIN_HOOK_MATCH_CHARS and f in used)):
                return True
    return False


def _words(text) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9%]+", str(text or "").lower())) + " "


def unsupported_claims(text: str, said: str, profile: dict, review: dict) -> list[str]:
    """CLAIM_TERMS in `text` that no allowed source contains (❓/empty profile fields don't count)."""
    allowed = _words(" ".join([said, json.dumps(llm.known_facts(profile)[0], ensure_ascii=False),
                               json.dumps(review.get("hooks") or [], ensure_ascii=False), str(review.get("summary") or "")]))
    used = _words(text)
    return [t for t in CLAIM_TERMS if f" {t} " in used and f" {t} " not in allowed]


def validate(draft: dict, profile: dict, review: dict, said: str = "") -> list[str]:
    """Deterministic checks from §3.12. -> problems (empty = pass). Problems are phrased for the LLM to fix.
    `said` = the user's own product wording (ICP product + brief)."""
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
        problems.append("the brand profile has no unsubscribe_line (fill in config/brand_profile.yaml)")
    elif unsubscribe not in body:
        problems.append(f'body must contain this exact line: "{unsubscribe}"')
    for key, label in (("sender_name", "sender name"), ("name", "brand name")):
        value = _known(brand.get(key))
        if not value:
            problems.append(f"the brand profile's {label} is not filled in (brand.{key} in config/brand_profile.yaml)")
        elif value.lower() not in body.lower():
            problems.append(f'sign the email with the {label} "{value}"')
    if any(ch in subject + body for ch in "{}❓"):
        problems.append("remove placeholders ({...} or ❓); use only known facts")
    if not llm.price_allowed(profile) and PRICE_RE.search(subject + " " + body):
        problems.append("do not mention prices")
    if not llm.social_proof_allowed(profile) and SOCIAL_PROOF_RE.search(subject + " " + body):
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


def _why(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):  # str() is empty
        return f"timed out after {LLM_TIMEOUT_S:g}s"
    return redact(f"{type(err).__name__}: {err}")[:200]


async def _llm_draft(profile: dict, lead: dict, review: dict, style_rules: list[str], product: str, brief: str,
                     problems: list[str] | None = None) -> dict:
    raw = await asyncio.wait_for(llm.draft_email(profile, lead, review, style_rules, problems=problems,
                                                 product=product, brief=brief), LLM_TIMEOUT_S)
    if not isinstance(raw, dict):
        raise ValueError("LLM draft is not a JSON object")
    return raw


async def draft_one(profile: dict, lead: dict, contact: dict | None, review: dict, style_rules: list[str],
                    sem: asyncio.Semaphore, product: str = "", brief: str = "") -> tuple[dict | None, dict | None]:
    """-> (draft, None) or (None, skipped entry). Never raises. Contact confidence doesn't matter here (§3.4)."""
    if not contact or not contact.get("email"):
        return None, {"lead_id": lead["id"], "reason": "no_contact"}
    said = "; ".join(x for x in (product, brief) if x)
    async with sem:
        try:
            raw = await _llm_draft(profile, lead, review, style_rules, product, brief)
        except Exception as err:  # noqa: BLE001 - error, timeout, bad JSON -> report this lead, keep going
            log.warning(f"no draft for {lead['id']}: {_why(err)}")
            return None, {"lead_id": lead["id"], "reason": "llm_failed", "detail": _why(err)}
        problems = validate(raw, profile, review, said)
        if any(PROFILE_GAP not in p for p in problems):  # regenerate once with the problems passed back
            try:
                retry = await _llm_draft(profile, lead, review, style_rules, product, brief, problems=problems)
                raw, problems = retry, validate(retry, profile, review, said)
            except Exception as err:  # noqa: BLE001 - keep the first draft and its warnings
                log.warning(f"regenerating the draft for {lead['id']} failed, keeping the first one: {_why(err)}")
    return _as_draft(lead["id"], contact["email"], raw, problems), None


async def draft_emails(args: dict) -> dict:
    leads = args.get("leads")
    if not isinstance(leads, list) or not leads:
        raise ValueError("missing 'leads' list")
    if not all(isinstance(lead, dict) and lead.get("id") for lead in leads):
        raise ValueError("every lead needs an 'id'")
    profile = args.get("profile") if isinstance(args.get("profile"), dict) else {}
    style_rules = [str(r) for r in args.get("style_rules") or []]
    product = str((args.get("icp") or {}).get("product") or "").strip()
    brief = str(args.get("brief") or "").strip()
    contacts, reviews = {}, {}
    for c in args.get("contacts") or []:  # sorted per lead by find_contacts: the first one is the primary contact
        contacts.setdefault(c.get("lead_id"), c)
    for r in args.get("reviews") or []:
        reviews.setdefault(r.get("lead_id"), r)
    sem = asyncio.Semaphore(DRAFT_CONCURRENCY)
    results = await asyncio.gather(*(draft_one(profile, lead, contacts.get(lead["id"]), reviews.get(lead["id"], {}),
                                               style_rules, sem, product, brief) for lead in leads))
    drafts = [d for d, _ in results if d]
    drafts.sort(key=lambda d: -(reviews.get(d["lead_id"], {}).get("fit_score") or 0))  # best fit first (stable)
    return {"drafts": drafts, "skipped": [s for _, s in results if s]}


agent = make_specialist(
    name=NAME,
    seed_env="WRITER_SEED",
    port_env="WRITER_PORT",
    default_port=8005,
    profile="copywriter.md",
    description="Writes short, evidence-based, personal outreach emails from a brand to local businesses.",
    tools={"draft_emails": draft_emails},
)

if __name__ == "__main__":
    agent.run()
