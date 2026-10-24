"""Copywriter agent: `draft_emails` - one short personal email per lead (ASI:One + deterministic validator).

M4 work: llm.draft_email per lead, then validate: word count <= max_words; subject <= 8 words and
<= 60 chars; exact unsubscribe line; sender name + brand present; no {placeholders}; no price unless
allowed; hook_used matches a hook from the qualification. Regenerate once on failure, else fill `warnings`.

Run:  python agents/copywriter.py
"""

from common import make_specialist


async def draft_emails(args: dict) -> dict:
    if not isinstance(args.get("leads"), list) or not args["leads"]:
        raise ValueError("missing 'leads' list")
    raise NotImplementedError("draft_emails is not implemented yet (milestone M4)")


agent = make_specialist(
    name="leadreach-writer",
    seed_env="WRITER_SEED",
    port_env="WRITER_PORT",
    default_port=8005,
    profile="copywriter.md",
    description="Writes short, evidence-based, personal outreach emails from a brand to local businesses.",
    tools={"draft_emails": draft_emails},
)

if __name__ == "__main__":
    agent.run()
