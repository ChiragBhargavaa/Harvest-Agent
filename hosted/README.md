# Hosted Harvest Agent (Agentverse)

Paste **one file** into a free Agentverse hosted agent so the demo does not need the owner's laptop.
The five specialists are deployed separately. This orchestrator calls them when their address secrets
are set, and otherwise runs the same steps in-process (find leads, contacts, reviews, drafts, dry-run send).

`README.md` at the repo root is the submission README. After this agent is deployed, its **new hosted
address and profile URL** must be written into that table. A hosted agent gets a new address; the old
mailbox address will not match.

## What the hosted runtime allows

Checked 24 Oct 2026 against:

- [Allowed imports (v2)](https://docs.agentverse.ai/v-2/documentation/advanced-usages/allowed-imports.md)
- [Hosted agents](https://docs.agentverse.ai/documentation/create-agents/hosted-agents.md)
- [Hosted code editor](https://docs.agentverse.ai/documentation/create-agents/hosted-code-editor.md) (secrets, storage, multi-file)
- [Subscriptions and quotas](https://docs.agentverse.ai/documentation/advanced-usages/agentverse-subscriptions-and-quotas.md)
- [Agent logs / mailbox quotas](https://docs.agentverse.ai/documentation/advanced-usages/agent-logs-errors.mdx)
- [uAgent creation](https://innovationlab.fetch.ai/resources/docs/agent-creation/uagent-creation)
- [Chat protocol](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents)
- [Interactive cards](https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards)
- [Payment protocol](https://innovationlab.fetch.ai/resources/docs/agent-transaction/agent-payment-protocol)

| Constraint | What the docs say | What this port does |
|---|---|---|
| `agent` | Pre-created. `Agent(...)` is only a wrapper whose name, seed and address are ignored. `agent.run()` is optional. | This file does not construct an agent and does not call `run`. Handlers attach to the injected `agent`. |
| Bureau / mailbox | One agent per project. Bureau is forbidden. Mailbox is for local agents. | No Bureau, no mailbox flag. Hosted delivery is built in. |
| Secrets | Agent Secrets are environment variables (like a `.env`). | Read only with `os.getenv`. Nothing secret is in the file. The owner types them in the UI. |
| Storage | Globals reset between invocations. Persist with Agent Storage. | Campaigns, the OSM cache, the daily cap and the dry-run outbox are JSON in `ctx.storage`. |
| Packages | Allowlist includes `uagents`, `uagents-core`, `requests`, `httpx`, `aiohttp`, `openai`, `bs4` / `beautifulsoup4`, `lxml`, **`dnspython`**, `pydantic`, `PyYAML`, `tenacity`. | HTTP uses the stdlib (`urllib`) so every call has an explicit timeout. HTML uses `bs4`. MX uses `dnspython` (it is on the allowlist, so the check stays). Chat cards and the seller payment protocol come from `uagents` / `uagents-core`. |
| Multi-file | The editor can hold several files. | One file, so it can be pasted as-is. |
| Time / CPU | No per-invocation wall-clock timeout is published. Free (Basic) plan: **60,000s** computation (aggregate), **5 MB** storage, **8** hosted agents. Quota examples also list processed memory. Log in at least once every 30 days or the agent is deactivated. | Each chat turn is one pipeline step (approval gates default on). Inline budgets: finder **40s** (`FINDER_BUDGET_S`), contacts **30s** (`CONTACT_BUDGET_S`), analyst **45s** (`ANALYST_BUDGET_S`). Remote waits match `agents/orchestrator.py` (75/60/70/60/30s, one resend of `RESEND_WAIT_S` default 30s) so the already-deployed specialists can finish; on timeout the inline path runs. Overpass uses a 12s HTTP timeout and does not sleep 30s on 429. |

Message models are the same `ToolRequest` / `ToolResponse` as `agents/common.py`, and
`Protocol(name="leadreach-tools", version="0.1.0")` with `ToolRequest -> ToolResponse` has the same
manifest digest as the local specialists (`hosted/test_hosted.py` checks this).

## What is not the same as the laptop agents

- No `import` of this repo and no `importlib` fallback. The specialist functions are inlined.
- The hosted sender **never sends email**. `DRY_RUN` cannot turn that off. It records `would_send` in `ctx.storage` (`dry_run_outbox`). `TEST_REDIRECT_TO`, if used, comes only from a secret.
- `LEADPACK_PRICE` unset means the pack is not offered. There is no default price in code.
- Inline finder / web-search timeouts are shorter than the mailbox agent's 65s / 90s defaults so one turn can return a partial result. Override with the same env names as main.

## Deploy Harvest Agent

1. In [Agentverse](https://agentverse.ai), launch a **hosted** agent (Build tab, not an external/mailbox agent).
2. Set the agent name to **`Harvest Agent`**.
3. Replace the editor contents with the whole of [`hosted/orchestrator/agent.py`](orchestrator/agent.py). Do not add an agent constructor or a run loop.
4. Reuse the profile README and avatar from [`agents/profiles/orchestrator.md`](../agents/profiles/orchestrator.md) (badges, keywords, use cases).
5. Add secrets in **Agent Secrets** (they become environment variables). Then start the agent.

Create or confirm the five specialists first if you want them called remotely, then paste their addresses here.
Leave an address blank to run that step inside Harvest Agent.

| Secret | Required | Purpose |
|---|---|---|
| `ASI1_API_KEY` | yes, for any LLM step | ASI:One key. Same name as `agents/llm.py`. |
| `LEAD_FINDER_AGENT_ADDRESS` | no | `leadreach-finder`. Unset or timeout → inline `find_leads`. |
| `CONTACT_FINDER_AGENT_ADDRESS` | no | `leadreach-contacts`. |
| `LEAD_ANALYST_AGENT_ADDRESS` | no | `leadreach-analyst`. |
| `COPYWRITER_AGENT_ADDRESS` | no | `leadreach-writer`. |
| `SENDER_AGENT_ADDRESS` | no | `leadreach-sender`. Inline send is dry-run only. |
| `DAILY_CAP_PER_USER` | no (default 3) | New campaigns per sender per IST day. |
| `DAILY_CAP_TOTAL` | no (default 20) | New campaigns globally per IST day. |
| `LEADPACK_PRICE` | no | If unset, no lead pack is offered. |
| `LEADPACK_RECIPIENT` | no | `fetch1…` wallet. Otherwise the agent wallet. |
| `TEST_REDIRECT_TO` | no | Recorded as the dry-run destination. Never hard-coded. |
| `REPLY_TO` | no | Added to the crawler User-Agent when set. |
| `ASI1_MODEL`, `ASI1_WEB_SEARCH`, `ASI1_SEARCH_CONTEXT` | no | Same meaning as the local agent. |
| `DAILY_CAP` | no (default 5) | How many dry-run rows to record per send step, not the campaign cap. |
| `FINDER_BUDGET_S`, `CONTACT_BUDGET_S`, `ANALYST_BUDGET_S`, `RESEND_WAIT_S`, `LEADREACH_TIMEOUT_*`, `LEADREACH_LLM_TIMEOUT`, `LEADREACH_WEB_SEARCH_TIMEOUT`, `LEADREACH_DRAFT_LLM_TIMEOUT` | no | Override the budgets above. |

`DRY_RUN` is ignored. The hosted sender cannot send real mail.

6. Copy the new agent address and profile URL into the root `README.md` agent table.

Over the daily cap, before any LLM call, the reply is exactly:

`Daily demo limit reached — try tomorrow, or self-host with your own ASI1 key: https://github.com/ChiragBhargavaa/Harvest-Agent`

Messages inside a campaign already in progress are not counted. The counters reset on the next IST day.
