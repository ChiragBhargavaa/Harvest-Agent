"""Sender agent (stretch): `send_emails` - Resend API with dry-run, test redirect, daily cap, send window.

Safety: DRY_RUN=true by default; TEST_REDIRECT_TO reroutes every email; suppression list always honoured;
low-confidence contacts need explicit approval. Window Mon-Sat 09:30-16:30 IST can use @agent.on_schedule.

Run:  python agents/sender.py
"""

from common import make_specialist


async def send_emails(args: dict) -> dict:
    if not isinstance(args.get("drafts"), list) or not args["drafts"]:
        raise ValueError("missing 'drafts' list")
    raise NotImplementedError("send_emails is not implemented yet (milestone M6, stretch)")


agent = make_specialist(
    name="leadreach-sender",
    seed_env="SENDER_SEED",
    port_env="SENDER_PORT",
    default_port=8006,
    profile="sender.md",
    description="Sends approved outreach emails within daily caps and send windows (dry-run by default).",
    tools={"send_emails": send_emails},
)

if __name__ == "__main__":
    agent.run()
