# LeadReach

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

> Tell LeadReach what your brand sells and which businesses you want in a city. Inside one ASI:One chat it finds matching local
> businesses, their published emails, evidence-based reasons to reach out, and drafts personal emails. It stops for your
> approval at every step.

**Fetch-A-Thon 3.0 — ASI:One Agent Challenge: From Intent to Action**

| Link | |
|---|---|
| Source code | https://github.com/ChiragBhargavaa/Harvest-Agent |
| ASI:One shared chat (full workflow) | `<paste>` |
| Demo video (3–5 min) | `<paste>` |
| Submission | Fetch-A-Thon 3.0 dashboard → Project Submission Agent |

## Agents

Addresses are fixed by the seeds in `.env` (keep that file; `bash setup.sh` prints them). Profile URLs are added after the mailbox is connected.

| Agent | Name | Role | Status | Address | Agentverse profile |
|---|---|---|---|---|---|
| Orchestrator | `leadreach` | Talks to the brand in ASI:One (Chat Protocol + Interactive Cards), builds the ideal-customer profile, runs the approval gates, edits drafts, audit log | ✅ implemented | `agent1q0nlrk0hsqgn9l85z8thxmr5tltjqxeqsmjw4zzme389dcw7zkudjq3lk23` | `<url>` |
| Lead Finder | `leadreach-finder` | Finds local businesses: OpenStreetMap + ASI:One web search, verifies websites by crawling | ✅ implemented | `agent1q06nvrv65ewr9g62te9xrhqgc7hu8kc3823rrvtttu33nsk4krek2w4cz0d` | `<url>` |
| Contact Finder | `leadreach-contacts` | Published emails/phones by polite crawling + MX check, confidence + source URL per contact | ✅ implemented | `agent1qdr009c63xpzznwt07k62vxg5fzpr3kf89s65ckz0rjda2ls3s4evf2v4c8` | `<url>` |
| Lead Analyst | `leadreach-analyst` | Fit score + evidence-backed hooks from each website (signals-only fallback) | ✅ implemented | `agent1qd8k32878hmz7jc4cw0cr6mv9q6eqa24xrac6cqxeg0c0kdjf4guj05wpl9` | `<url>` |
| Copywriter | `leadreach-writer` | Short personal drafts (ASI:One) + deterministic validator | ✅ implemented | `agent1q2ttlc7yh8dwy4vj8knr6jhmguy9pcp0a8zawtwnf3up26zgdq6qsaech9t` | `<url>` |
| Sender (stretch) | `leadreach-sender` | Placeholder: runs and answers, but `send_emails` replies "not implemented yet"; nothing is ever sent | ⏸ not part of this demo (DRY_RUN only) | `agent1q007hz3k52l3pjuwxxfrn28a6xv768alq5jygldx3x7ae6had0d47gtw337` | `<url>` |

The demo workflow ends at approved drafts. At the send checkpoint the orchestrator reports that sending isn't available and
offers **skip** to finish the campaign. Sending (M6) and the M5 extras (`add lead`, `import list`, reply classification) are
out of scope for this submission.

## Problem → Users → Outcome

- **Problem:** small brands that sell to local businesses (cafés, salons, gyms, retailers) spend hours per lead finding the right
  businesses, a real contact email, and a reason to write. Lead databases barely cover Indian SMBs, and autopilot AI outreach
  tools send wrong emails without asking.
- **Who it's for:** founders and small sales teams of B2B brands selling locally, e.g. a coffee roaster selling to cafés.
- **Outcome of one ASI:One conversation:** an approved list of local leads with sources, verified published contacts, evidence-based
  reviews, and approved personal email drafts (LeadReach does not send them in this demo).

## Architecture

```
Brand ──ASI:One chat──▶ leadreach (orchestrator: Chat Protocol, Interactive Cards, gates, audit log)
                          │  ASI:One LLM: intent → ideal-customer profile → [card: approve]
                          ├─ToolRequest─▶ leadreach-finder   OSM Nominatim + Overpass, ASI:One web search,
                          │                                  website verification            → [card: approve leads]
                          ├─ToolRequest─▶ leadreach-contacts polite crawl, emails/phones, MX  → [card: approve contacts]
                          ├─ToolRequest─▶ leadreach-analyst  site signals + ASI:One hooks      → [card: approve reviews]
                          ├─ToolRequest─▶ leadreach-writer   ASI:One drafts + validator        → [card per draft]
                          └─ToolRequest─▶ leadreach-sender   placeholder (not part of this demo; DRY_RUN only)
```

Fetch.ai tech: uAgents, Agentverse (mailbox + profiles), Agent Chat Protocol, ASI:One LLM (including web search), ASI
Interactive Cards, and the Agent Payment Protocol.

**Payment Protocol (optional demo):** `leadreach` is a seller (`agents/payments.py`). Say **buy lead pack** to get a
`RequestPayment` (default 0.1 FET) for the verified contacts and their source URLs. A matching `CommitPayment` gets `CompletePayment`
plus the pack; anything else gets `CancelPayment`. Verification is a labelled demo check, not on-chain. The free flow and the approval
gates never depend on it.

**Guardrails:** emails come only from pages we crawled (never from the LLM). Websites suggested by web search count only after
crawling confirms the business. We respect robots.txt and OSM usage policies, never scrape Google Maps, Justdial, Instagram and
the like, and keep dry-run on by default.

## Run it

Requires Python 3.10–3.13 and [uv](https://docs.astral.sh/uv/) (`brew install uv`). `setup.sh` falls back to venv + pip without it.

```bash
bash setup.sh                                  # deps, .env with fresh seeds + derived addresses, local git repo
.venv/bin/python agents/mock_test.py           # offline end-to-end test (no keys needed), exit 0 = pass (110 checks)
.venv/bin/ruff check agents scripts            # lint
.venv/bin/python scripts/probe_sources.py Jaipur cafe   # checks OSM + ASI:One web search with your key
.venv/bin/python agents/local_test.py "I sell organic coffee beans - find cafés in Jaipur"   # live loop in the terminal
```

**Live on Agentverse / ASI:One:** start all 6 agents with one supervisor (restarts any agent that exits; Ctrl+C stops all):
```bash
.venv/bin/python scripts/run_all.py
```
Or run a single agent on its own, e.g. `.venv/bin/python agents/orchestrator.py` (likewise `lead_finder`, `contact_finder`,
`lead_analyst`, `copywriter`, `sender`). Open each agent's **Agent Inspector** link from the logs → **Connect** → **Mailbox**.
Then, on Agentverse, edit the orchestrator's profile and use **Chat with Agent** to open it in ASI:One.

To keep the agents online, run `python scripts/run_all.py` on any always-on machine with the same `.env` (same seeds keep the
addresses). Keep `DRY_RUN=true`.

### Extra resources needed
- ASI:One API key: https://asi1.ai (Developer → API Keys)
- Agentverse account: https://agentverse.ai
- Not needed for the demo: a Resend API key (https://resend.com) is only for the planned sender (M6, out of scope)
- Optional extra lead sources: Foursquare Places (https://foursquare.com/pricing/) or Google Places API (New)

Data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL).
