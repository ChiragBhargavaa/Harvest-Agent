"""Live local loop: all agents + a fake ASI:One user in one process (no mailbox), using the REAL
ASI:One LLM, OpenStreetMap and websites. Needs ASI1_API_KEY + seeds in .env (run setup.sh).

    .venv/bin/python agents/local_test.py "I sell organic coffee beans - find cafés in Jaipur"

Prints every reply; type follow-ups (approve, remove 2, status, ...) at the prompt. Ctrl+C to quit.
"""

import asyncio
import os
import sys

os.environ["MAILBOX"] = "false"  # must be set before the agents are imported

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage  # noqa: E402

import contact_finder  # noqa: E402
import copywriter  # noqa: E402
import lead_analyst  # noqa: E402
import lead_finder  # noqa: E402
import orchestrator  # noqa: E402
import sender  # noqa: E402
from common import chat_text, text_of  # noqa: E402

for env, mod in (("LEAD_FINDER_AGENT_ADDRESS", lead_finder), ("CONTACT_FINDER_AGENT_ADDRESS", contact_finder),
                 ("LEAD_ANALYST_AGENT_ADDRESS", lead_analyst), ("COPYWRITER_AGENT_ADDRESS", copywriter),
                 ("SENDER_AGENT_ADDRESS", sender)):
    os.environ[env] = mod.agent.address

FIRST = " ".join(sys.argv[1:]) or "I sell organic coffee beans - find cafés and bakeries in Jaipur"
user = Agent(name="fake-asi1-user", seed="leadreach local test user seed 0123456789", port=8011)


@user.on_event("startup")
async def ask(ctx: Context):
    print(f"\n>>> {FIRST}", flush=True)
    await ctx.send(orchestrator.agent.address, chat_text(FIRST))

    async def repl():
        while True:
            line = (await asyncio.to_thread(input, "> ")).strip()
            if line:
                await ctx.send(orchestrator.agent.address, chat_text(line))

    asyncio.create_task(repl())


@user.on_message(ChatAcknowledgement)
async def got_ack(ctx: Context, sender_addr: str, msg: ChatAcknowledgement):
    pass


@user.on_message(ChatMessage)
async def got_reply(ctx: Context, sender_addr: str, msg: ChatMessage):
    print("\n========== LEADREACH ==========\n" + text_of(msg) + "\n===============================\n", flush=True)


bureau = Bureau(port=8010)
for a in (orchestrator.agent, lead_finder.agent, contact_finder.agent, lead_analyst.agent, copywriter.agent,
          sender.agent, user):
    bureau.add(a)

if __name__ == "__main__":
    bureau.run()
