# LeadReach

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

> Tell LeadReach what your brand sells and which businesses you want in a city. Inside one ASI:One chat it finds matching local
> businesses, their published emails, evidence-based reasons to reach out, and drafts personal emails. It stops for your
> approval at every step.

**Fetch-A-Thon 3.0 — ASI:One Agent Challenge: From Intent to Action**

| Link | |
|---|---|
| ASI:One shared chat (full workflow) | `<paste>` |
| Demo video (3–5 min) | `<paste>` |
| Submission | Fetch-A-Thon 3.0 dashboard → Project Submission Agent |

## Agents

Addresses are derived from the seeds in `.env` (`bash setup.sh` prints them). Fill in after the first deploy.

| Agent | Name | Role | Address | Agentverse profile |
|---|---|---|---|---|
| Orchestrator | `leadreach` | Talks to the brand in ASI:One (Chat Protocol + Interactive Cards), runs the approval gates | `agent1q...` | `<url>` |
| Lead Finder | `leadreach-finder` | Finds local businesses: OpenStreetMap + ASI:One web search, verifies websites | `agent1q...` | `<url>` |
| Contact Finder | `leadreach-contacts` | Published emails/phones by polite crawling + MX check | `agent1q...` | `<url>` |
| Lead Analyst | `leadreach-analyst` | Fit score + evidence-backed hooks from each website | `agent1q...` | `<url>` |
| Copywriter | `leadreach-writer` | Short personal drafts (ASI:One) + deterministic validator | `agent1q...` | `<url>` |
| Sender (stretch) | `leadreach-sender` | Dry-run by default, daily cap, send window, unsubscribe | `agent1q...` | `<url>` |

## Problem → Users → Outcome

- **Problem:** small brands that sell to local businesses (cafés, salons, gyms, retailers) spend hours per lead finding the right
  businesses, a real contact email, and a reason to write. Lead databases barely cover Indian SMBs, and autopilot AI outreach
  tools send wrong emails without asking.
- **Who it's for:** founders and small sales teams of B2B brands selling locally, e.g. a coffee roaster selling to cafés.
- **Outcome of one ASI:One conversation:** an approved list of local leads with sources, verified published contacts, evidence-based
  reviews, and personal email drafts ready to send at a safe daily pace.

## Architecture

```
Brand ──ASI:One chat──▶ leadreach (orchestrator: Chat Protocol, Interactive Cards, gates, audit log)
                          │  ASI:One LLM: intent → ideal-customer profile → [card: approve]
                          ├─ToolRequest─▶ leadreach-finder   OSM Nominatim + Overpass, ASI:One web search,
                          │                                  website verification            → [card: approve leads]
                          ├─ToolRequest─▶ leadreach-contacts polite crawl, emails/phones, MX  → [card: approve contacts]
                          ├─ToolRequest─▶ leadreach-analyst  site signals + ASI:One hooks      → [card: approve reviews]
                          ├─ToolRequest─▶ leadreach-writer   ASI:One drafts + validator        → [card per draft]
                          └─ToolRequest─▶ leadreach-sender   Resend, dry-run, cap, window      → [card: send]
```

Fetch.ai tech: uAgents, Agentverse (mailbox + profiles), Agent Chat Protocol, ASI:One LLM (including web search), ASI
Interactive Cards; Payment Protocol planned.

**Guardrails:** emails come only from pages we crawled (never from the LLM). Websites suggested by web search count only after
crawling confirms the business. We respect robots.txt and OSM usage policies, never scrape Google Maps, Justdial, Instagram and
the like, and keep dry-run on by default.

## Run it

Requires Python 3.10–3.13 and [uv](https://docs.astral.sh/uv/) (`brew install uv`). `setup.sh` falls back to venv + pip without it.

```bash
bash setup.sh                                  # deps, .env with fresh seeds + derived addresses, local git repo
.venv/bin/python agents/mock_test.py           # offline end-to-end test (no keys needed), exit 0 = pass
.venv/bin/python scripts/probe_sources.py Jaipur cafe   # checks OSM + ASI:One web search with your key
.venv/bin/python agents/local_test.py "I sell organic coffee beans - find cafés in Jaipur"   # live loop in the terminal
```

**Live on Agentverse / ASI:One:** start each agent in its own terminal (or with a process manager):
```bash
.venv/bin/python agents/lead_finder.py   # likewise contact_finder, lead_analyst, copywriter, sender
.venv/bin/python agents/orchestrator.py
```
Open each agent's **Agent Inspector** link from the logs → **Connect** → **Mailbox**. Then, on Agentverse, edit the orchestrator's
profile and use **Chat with Agent** to open it in ASI:One.

### Extra resources needed
- ASI:One API key: https://asi1.ai (Developer → API Keys)
- Agentverse account: https://agentverse.ai
- Optional, for sending: Resend API key and a verified domain: https://resend.com
- Optional extra lead sources: Foursquare Places (https://foursquare.com/pricing/) or Google Places API (New)

Data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL).
