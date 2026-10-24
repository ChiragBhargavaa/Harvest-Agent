# HACKATHON_README — Build reference for **LeadReach** (Fetch-A-Thon 3.0)

> **Audience:** AI coding assistants (Claude Code, Cursor) and Chirag.
> **Read this whole file before writing code.** It contains the hackathon rules, judging criteria,
> the product spec (contracts, state machine, cards, prompts, guardrails, milestones) and a verified
> Fetch.ai technical reference.
>
> Researched: 24 Oct 2026. Library facts were checked against an installed `uagents==0.26.1` /
> `uagents-core==0.4.12` (Python 3.13).
>
> **Legend:** ✅ verified (source or test noted) · ⚠️ plausible but verify before relying on it · ❓ unknown — ask Chirag, never invent
>
> `README.md` is the **submission README** (badges + agent table). Do not replace it with this file.

---

## 0. TL;DR

**What we're building:** **LeadReach** is an approval-first lead finder and outreach copilot for **brands**. It runs entirely inside an
ASI:One chat. A brand says what it sells and who it wants to reach in a city (e.g. *"I sell organic coffee beans, find cafés and
bakeries in Jaipur"*), and LeadReach:
1. turns that into an ideal-customer profile (business types, search queries, fit criteria);
2. finds matching local businesses (OpenStreetMap + ASI:One web search);
3. finds their **published** emails and phones by politely crawling their own websites;
4. qualifies each lead with **evidence** from its website (fit score + hooks);
5. drafts short, personal outreach emails;
6. sends a few per day (stretch, dry-run by default).

By default it **stops for the user's approval after every step**. The user can switch approval off per step, edit anything at
any checkpoint, and pause everything at any time.

**Why it fits the judging:**
- **Fetch.ai tech:** 6 cooperating uAgents on Agentverse, Chat Protocol, the ASI:One LLM (including its web search) for every
  reasoning step, Interactive Cards at every checkpoint, Payment Protocol as a stretch goal.
- **Impact:** every small brand selling to local businesses (cafés, salons, gyms, retailers…) needs leads, and has no sales team.
- **Innovation:** the opposite of autopilot AI-SDR tools; evidence-or-nothing contact data.

---

## 1. Rules for the coding agent (MUST)

1. **Follow the contracts in §3.** Message models, tool names, args/result JSON and state names are fixed. Change them only if
   Chirag agrees, and then update this file in the same change.
2. **Never invent brand facts.** Product, claims, customers and prices come only from the user's brief or
   `config/brand_profile.yaml`. Anything marked ❓ → ask Chirag.
3. **Never fabricate data.**
   - Emails come only from a crawled page or an OSM tag, and the source URL is kept. **The LLM is never a source of emails.**
   - Websites suggested by the LLM's web search are **candidates**: they count only after we crawl them and the business name appears.
   - Hooks come only from evidence actually observed on the site.
   - Never guess email patterns (e.g. `owner@<domain>` when it isn't published).
4. **Sending is off by default.** `DRY_RUN=true` unless Chirag explicitly changes it. Tests and demos must never email real
   businesses: use `TEST_REDIRECT_TO`.
5. **Secrets:** never commit `.env`, seeds or API keys, and never print keys in logs.
6. **Versions:** Python 3.10–3.13 and `uagents==0.26.1` / `uagents-core==0.4.12` (pinned in `pyproject.toml`, locked in `uv.lock`).
   uAgents `Model` classes are **pydantic v1**: use `.dict()`, `.json()`, `.parse_obj()`, never `.model_dump()`. ✅
7. **Handlers never raise.** Every specialist always replies with `ToolResponse(ok=False, error=...)` on failure
   (`common.make_specialist` does this). The orchestrator handles a `None` reply (timeout) gracefully.
8. **State:** keep it in `ctx.storage`, with JSON-serialisable values only.
9. **Crawling rules (§3.13):** respect robots.txt, rate limits, OSM usage policies and the banned-source list.
10. **Each agent is runnable on its own:** `python agents/<file>.py`.
11. **Test before claiming done:** run `.venv/bin/python agents/mock_test.py` (§3.18) before saying a change works.
12. **Keep `README.md` current:** update its agent table (name, address, Agentverse profile URL) and `agents/profiles/*.md`
    whenever agents change.
13. **Small, descriptive commits.** The commit history should show the work was done during the event.

---

## 2. Hackathon facts

### 2.1 Event ✅ (event page + hackpack)
- **Name:** Fetch-A-Thon 3.0, an AI Agent Hackathon by the **Fetch.ai Innovation Lab** (India). LNMIIT is a collaborating college.
- **Date:** Saturday **24 Oct 2026**, one day. **Results:** Wednesday **28 Oct 2026, 12:15 IST**, announced on social media and by email.
- **Schedule (IST):**
  - 09:00 check-in
  - 09:30 opening
  - 09:45 intro to Fetch.ai, ASI:One and agents
  - 10:15 challenge brief and rules
  - **10:30 building starts**
  - 13:00 lunch
  - 14:00 building continues
  - **16:00 mentor support and project review**
  - **16:30 final building and submission**
  - 17:50 closing
  - 18:00 end
- **Judges:** Sana Wajid, Rishank Jhavar, Gautam Kumar, Dev Chauhan.
- **Mentors:** Shruti Sharma, Abhijeet Chauhan, Shyamji Pandey, Geetanshi Goel.
- **Implication:** agents must **stay online at least until 28 Oct**, because judges may test them after the event. Deploy them (§4.8). Don't rely on a laptop.

### 2.2 The challenge ✅ (hackpack)
**ASI:One Agent Challenge: From Intent to Action.** Build a single agent or a multi-agent system that:
- solves a clearly defined real-world problem;
- does multi-step planning, reasoning or orchestration;
- uses tools, APIs, external services, data sources, MCP or other agents to produce an **executable outcome**;
- is registered on Agentverse and **discoverable through ASI:One**;
- implements the **Agent Chat Protocol (ACP)**;
- can be **fully demonstrated inside an ASI:One conversation, with no custom frontend**;
- *(recommended)* uses **ASI1 Interactive Cards** for guided interactions: forms, carousels, review screens, detail views.

The hackpack says the result should be "more than a chatbot or a thin wrapper around an API".

Allowed frameworks: Google ADK, LangGraph, CrewAI, OpenAI Agents SDK, Claude Agent SDK, uAgents, plain Python. **We use uAgents.**

### 2.3 Mandatory requirements (prize eligibility) ✅, and how LeadReach meets them
| # | Requirement | How we satisfy it |
|---|---|---|
| 1 | At least one agent registered on Agentverse | All agents run with `mailbox=True` + Inspector "Connect → Mailbox", each with a profile |
| 2 | Implements the Agent Chat Protocol | Orchestrator includes the chat protocol with `publish_manifest=True` |
| 3 | Discoverable and directly usable through ASI:One | Profile README with keywords, handle, avatar (§4.7 ranking factors); test early |
| 4 | Meaningful tool execution or multi-agent orchestration | 5 specialists doing OSM lookups, ASI:One web search, polite crawling, DNS MX checks, LLM drafting, email API |
| 5 | Primary workflow completed entirely inside an ASI:One conversation | Every step and approval happens through chat and cards |
| 6 | Public GitHub repo with run/test instructions | `README.md` + `setup.sh` + mock test |

### 2.4 Bonus considerations ✅ (hackpack), mapped to our plan
| Bonus | Our plan |
|---|---|
| Effective multi-agent collaboration | Orchestrator plus 5 specialists; lead finder → contact finder + lead analyst → copywriter |
| Payment Protocol with a credible monetisation model | Stretch goal (§3.20): brands buy verified lead packs, or agent-to-agent pay-per-verified-contact |
| Effective use of ASI Interactive Cards | form (setup, settings), review (each checkpoint), detail (drafts, report) |
| Reliability, error handling, recovery from failed tool calls | Retries, partial success, timeouts reported to the user, re-run step with feedback |
| Creative use of real-time data and external services | Live OSM data, live ASI:One web search, live website crawl, live DNS, live email API |
| Could realistically keep running after the hackathon | Any brand can use it from ASI:One; scheduled daily sending with caps |

### 2.5 Judging criteria ✅ (hackpack)
| Criterion | Weight | What judges look for | How LeadReach earns it |
|---|---|---|---|
| Functionality & Technical Implementation | **25%** | Works as intended; agents communicate and reason in real time | Full 5-step pipeline in one chat; real inter-agent calls; graceful failures |
| Use of Fetch.ai Technology | **20%** | Agents registered and discoverable on Agentverse; ACP; **ASI:One LLM central to reasoning**; Payment Protocol for monetisation or **agent-to-agent payments**; effective ecosystem use | All agents on Agentverse; ACP; ASI:One LLM for intent, ICP building, web discovery, qualification and copywriting; Payment Protocol stretch |
| Innovation & Creativity | **20%** | Original, unconventional problem-solving | Per-step approval switches, evidence-grounded hooks, learns from the user's edits |
| Real-World Impact & Usefulness | **20%** | Meaningful problem, end-user value | Small brands selling to local businesses can't afford sales teams or lead databases that cover Indian SMBs |
| User Experience & Presentation | **15%** | Clear, structured demo; smooth, intuitive | Cards at every checkpoint; narration that works even if a card fails to render |

### 2.6 Deliverables and submission ✅ (hackpack, re-checked 24 Oct 2026)
**Submit through the Fetch-A-Thon 3.0 dashboard** (https://asi1.ai/festival/fetchathon3/dashboard) → **Project Submission Agent**,
following its instructions. Mandatory for online and offline participants. Promo code for one free month of ASI:One Pro and
Agentverse Premium: `FETCHATHONINDFETCHATHONIND`. An **AI Mentor** on the dashboard helps with debugging.

1. Public **ASI:One Shared Chat URL** showing the complete workflow.
2. **Agentverse Agent Profile URL(s)** for each agent.
3. Public **GitHub repository**.
4. **Demo video**, 3–5 minutes.
5. Brief description: **problem, target users, outcome**.

**`README.md` must contain:**
- each agent's **name and address**;
- any extra resources needed to run the project, with links;
- these two badges, verbatim:
```
![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)
```

### 2.7 Prizes ✅
| Award | Prize |
|---|---|
| National Winner | ₹35,000 + internship interview |
| 1st Runner-up | ₹25,000 + internship interview |
| 2nd Runner-up | ₹15,000 + internship interview |
| Best Multi-Agent Workflow (×2) | ₹5,000 each |
| Best Agentverse Deployment (×2) | ₹5,000 each |
| Best Personal AI | ₹5,000 |

**LeadReach targets:** the main prizes, Best Multi-Agent Workflow, and Best Agentverse Deployment.

### 2.8 Unknown rules ❓ (announced at the 10:15 brief; record the answers here)
- Team size:
- Are pre-existing code or templates allowed?
- Exact submission deadline (submission is via the dashboard agent, §2.6):

---

## 3. Product spec: LeadReach

### 3.1 Summary and pitch
- **User:** a brand or small business that sells to other local businesses (e.g. a coffee roaster selling to cafés, a packaging
  supplier selling to restaurants, a SaaS tool for salons). ❓ Chirag's own demo brand (§3.21).
- **Problem:** finding the right local businesses, their real contact emails and a relevant reason to write takes hours per lead.
  Lead databases cover Indian SMBs badly, and autopilot AI outreach tools send wrong or embarrassing emails without approval.
- **Outcome:** in one ASI:One chat, the brand gets an approved lead list, verified contacts with sources, evidence-based reviews,
  and personal drafts, then sends a few per day, staying in control at every step.
- **Pitch line:** *"Most AI outreach tools run on autopilot. LeadReach proposes every step and waits: approve, edit or reject, and
  switch approvals off only where you trust it."*

### 3.2 Principles (design rules)
1. **Approval-first:** every step is a checkpoint by default.
2. **Evidence or nothing:** every lead has a source reference, every contact has a source URL, and every hook has an observed snippet
   plus its URL.
3. **The user can intervene anywhere:** remove items, redo a step with feedback, edit a draft, add a lead, pause or resume.
4. **Transparent:** every action is written to an audit log with `actor = user | ai_auto | system`.
5. **Polite by construction:** low volume, unsubscribe honoured, robots.txt and OSM usage policies respected.

### 3.3 Pipeline
| # | Step | Tool (agent) | Gate key | What the user sees at the checkpoint |
|---|---|---|---|---|
| 0 | Understand the brand | `llm.build_icp` (Orchestrator) | `icp` | Review card: product, target business types, city, web searches, fit signs |
| 1 | Find leads | `find_leads` (Lead Finder) | `leads` | Review card: business → website · email/phone · source (OSM/WEB) |
| 2 | Find contacts | `find_contacts` (Contact Finder) | `contacts` | Review card: business → email · role · confidence |
| 3 | Qualify | `qualify_leads` (Lead Analyst) | `reviews` | Review card: business → fit score + top hook; "show lead N" opens a detail card |
| 4 | Draft emails | `draft_emails` (Copywriter) | `drafts` | One review card per draft (approve / skip; edit through chat) |
| 5 | Send | `send_emails` (Sender, stretch) | `send` | Review card: count, dry-run status, redirect, cap |

### 3.4 Approval gates
```json
{"ask_before": {"icp": true, "leads": true, "contacts": true, "reviews": true, "drafts": true, "send": true},
 "daily_cap": 5, "dry_run": true}
```
- **Default:** all gates `true`. When a gate is `false`, the step's output is auto-approved, logged with `actor: "ai_auto"`, and an
  FYI message (no card, no waiting) is posted.
- **Changing settings:** the settings **form card**, or natural language ("stop asking me about contacts", "cap at 8 a day"). The
  agent confirms in one line and logs it.
- **Invariants that hold even with every gate off:**
  1. `daily_cap` and the sending window are always enforced.
  2. The suppression list (unsubscribed addresses) is always honoured.
  3. **A contact with `confidence: "low"` always needs the user's explicit approval** before anything is sent to it.
  4. "pause" stops everything immediately, including queued sends.
  5. `dry_run` is turned off only by an explicit settings change from the user, never by the AI.

### 3.5 Agents
| Agent | File | Agentverse name | Seed env | Address env | Port | Role |
|---|---|---|---|---|---|---|
| Orchestrator | `agents/orchestrator.py` | `leadreach` | `ORCH_SEED` | — | 8001 | Chat protocol, cards, state machine, gates, audit log, ASI:One intent + ICP |
| Lead Finder | `agents/lead_finder.py` | `leadreach-finder` | `FINDER_SEED` | `LEAD_FINDER_AGENT_ADDRESS` | 8002 | `find_leads`: OSM + ASI:One web search + website verification |
| Contact Finder | `agents/contact_finder.py` | `leadreach-contacts` | `CONTACT_SEED` | `CONTACT_FINDER_AGENT_ADDRESS` | 8003 | `find_contacts`: polite crawl + extraction + MX check |
| Lead Analyst | `agents/lead_analyst.py` | `leadreach-analyst` | `ANALYST_SEED` | `LEAD_ANALYST_AGENT_ADDRESS` | 8004 | `qualify_leads`: deterministic signals + LLM hooks |
| Copywriter | `agents/copywriter.py` | `leadreach-writer` | `WRITER_SEED` | `COPYWRITER_AGENT_ADDRESS` | 8005 | `draft_emails`: LLM drafts + validator |
| Sender (stretch) | `agents/sender.py` | `leadreach-sender` | `SENDER_SEED` | `SENDER_AGENT_ADDRESS` | 8006 | `send_emails`: Resend, cap, window, suppression |

Specialists are built with `common.make_specialist` and speak `ToolRequest`/`ToolResponse`. Only the orchestrator speaks the Chat Protocol.

### 3.6 Message contract ✅ (`agents/common.py`)
```python
class ToolRequest(Model):
    tool: str              # "find_leads" | "find_contacts" | "qualify_leads" | "draft_emails" | "send_emails"
    args_json: str = "{}"  # JSON matching the tool's args schema (§3.7)

class ToolResponse(Model):
    ok: bool
    result: str = ""       # JSON matching the tool's result schema (§3.7)
    error: str = ""
```
`reply, status = await ctx.send_and_receive(addr, ToolRequest(...), response_type=ToolResponse, timeout=T)` — ✅ verified in
`agents/mock_test.py`; `reply` is `None` on timeout.

**Per-tool timeouts (s):** `find_leads` 300 · `find_contacts` 300 · `qualify_leads` 300 · `draft_emails` 240 · `send_emails` 60
(sized for live web search + polite crawling; override with `LEADREACH_TIMEOUT_<TOOL>`, §3.16). The progress text is sent
before the call. Send **≤15 leads per request**.

### 3.7 Tool args and result schemas
**`find_leads`** ✅ implemented
```jsonc
// args
{"icp": ICP, "limit": 15, "exclude_ids": []}
// result
{"leads": [Lead], "call_list": [Lead], "source_notes": ["OpenStreetMap: 86 businesses ...; 8 list a website."], "city": "Jaipur, ..."}
```
The ≤3 `search_queries` run `llm.discover_businesses` **concurrently** and are merged in query order. Every web-search call
(`discover_businesses`, `find_websites`) is bounded by `LEADREACH_WEB_SEARCH_TIMEOUT` (default 90 s); a failed or timed-out
query becomes a `source_notes` entry ("Web search failed for '<query>': ..."), never a step failure.
An OSM `website`/`contact:website` tag on a banned host (§3.13, e.g. a zomato.com page) is treated as no website: it is
never shown or crawled, and a `source_notes` entry counts them.
**`find_contacts`** ✅ implemented (M2)
```jsonc
{"leads": [Lead], "max_pages_per_site": 5}
→ {"contacts": [Contact], "skipped": [{"lead_id": "...", "reason": "no_website|banned_host|robots_disallowed|unreachable|no_email_found|mx_failed"}]}
```
A lead appears in `skipped` only if it ends up with no contact. Contacts are deduped per lead (best confidence wins) and
sorted per lead: confidence, then primary-contact role. `phones` = all phones seen on the site plus the OSM phone tag.
Leads are crawled concurrently (≤5 at a time, one shared `PoliteFetcher`); MX lookups run in a thread, once per domain.
**`qualify_leads`** ✅ implemented (M3)
```jsonc
{"icp": ICP, "leads": [Lead], "contacts": [Contact]}  →  {"reviews": [Review]}
```
Every lead gets exactly one review, sorted best fit first. Leads are reviewed concurrently (≤5 at a time, one shared
`PoliteFetcher`): `crawl_site` → `site_signals` (homepage, with keyword flags / years / app links merged across the crawled pages,
plus `pages_crawled`) → `llm.qualify` (60 s timeout). A hook is kept only if its `evidence` (every `...`-separated fragment,
≥10 chars) appears in a crawled page's text, or the homepage title / meta description; its `evidence_url` is set to that page.
LLM failure (error, timeout, bad JSON, missing/invalid `fit_score` or `hooks`) → deterministic signals-only review,
`fallback: true`, no hooks. No website or failed crawl → `reachable: false` + `unreachable_reason`
(`no_website|banned_host|robots_disallowed|unreachable`), signals-only, no LLM call. `contacts` is accepted but not used yet.
**`draft_emails`** ✅ implemented (M4)
```jsonc
{"profile": BrandProfile, "brief": "<the user's own words>", "style_rules": [], "icp": ICP, "leads": [Lead],
 "contacts": [Contact], "reviews": [Review]}
→ {"drafts": [Draft], "skipped": [{"lead_id": "...", "reason": "no_contact|llm_failed", "detail": "... (llm_failed only)"}]}
```
One draft per lead that has a contact, addressed to its first (= primary, §3.7 `find_contacts` sort) contact; sorted best
fit first. Contact confidence doesn't matter here: low-confidence contacts get drafts too (their explicit approval is a
send-time invariant, §3.4). Leads are drafted concurrently (≤15 at a time = one wave), `llm.draft_email(..., product=
icp.product, brief=brief)` with a `LEADREACH_DRAFT_LLM_TIMEOUT` (default 100 s) timeout per attempt (2 attempts fit the
240 s `draft_emails` timeout), then the validator (§3.12). A lead with no contact, or whose first LLM call fails (error,
timeout, bad JSON), goes to `skipped`: the copywriter never writes a draft without the LLM. `detail` says why (e.g.
"timed out after 100s"; secrets redacted) and both the copywriter and the orchestrator log it as a warning. (Live
24 Oct 2026: a 25 s timeout cancelled 2 of 3 calls in flight; httpx logs a request only once it is answered, so no
request appeared in the log.) The orchestrator stores `skipped` as `draft_skipped` and names those leads on the first
draft card.
**`send_emails`** (stretch)
```jsonc
{"campaign_id": "c_...", "drafts": [Draft], "dry_run": true, "redirect_to": "test@example.com", "daily_cap": 5}
→ {"queued": ["lead_id"], "sent": [{"lead_id": "...", "provider_id": "..."}], "skipped": [{"lead_id": "...", "reason": "suppressed|cap|low_confidence_unapproved"}]}
```

### 3.8 Data models
```jsonc
// ICP (built by llm.build_icp, approved at the "icp" gate)
{"product": "organic coffee beans", "business_types": ["cafe", "bakery"], "osm_tags": ["amenity=cafe", "shop=bakery"],
 "search_queries": ["specialty cafes in C-Scheme Jaipur"], "fit_criteria": ["serves espresso drinks"], "city": "Jaipur", "radius_km": 10}

// Lead
{"id": "osm:node/123 | web:<slug> | manual:<slug>", "name": "...", "category": "amenity=cafe | null", "address": null,
 "lat": null, "lon": null, "website": "https://... | null", "website_source": "osm | web_verified | null",
 "osm_emails": [], "phone": null, "source": "osm | web | manual", "source_ref": "https://www.openstreetmap.org/node/123 | <search result URL>"}

// Contact
{"lead_id": "...", "email": "hello@cafe.in", "role": "owner|manager|sales|orders|info|contact|unknown", "person_name": null,
 "source": "website|osm", "source_url": "https://.../contact", "confidence": "high|medium|low", "mx_ok": true, "phones": []}

// Review
{"lead_id": "...", "reachable": true, "unreachable_reason": "no_website|banned_host|robots_disallowed|unreachable | null",
 "signals": {...site_signals..., "pages_crawled": 2} | {}, "fit_score": 0-100, "hooks": [Hook], "summary": "≤40 words",
 "fallback": false}  // fallback = scored from signals only (LLM failed, or no reachable site)

// Hook (max 3)
{"observation": "≤25 words", "evidence": "exact snippet", "evidence_url": "https://...", "angle": "≤20 words"}

// Draft
{"lead_id": "...", "to": "...", "subject": "...", "body_text": "...", "hook_used": "... | null", "word_count": 118,
 "warnings": [], "approved": false}

// AuditEntry
{"ts": "2026-10-24T11:05:00+05:30", "step": "drafts", "action": "approved 4", "actor": "user|ai_auto|system", "detail": "..."}
```
**Contact confidence:** `high` = owner/manager/sales-type address on the business's own domain, published on its own site;
`medium` = generic address (info@, hello@) on its own domain, or a free-mail address published on its own site;
`low` = found only on a third-party page (this includes OSM `email` tags, `source: "osm"`, `source_url` = the OSM object), or an
address on another, non-free-mail domain that appears on the business's site (e.g. its web designer's).
Emails failing `crawl.mx_ok` are dropped. Roles from `crawl.guess_role` map founder→owner, booking→orders, hello→info, support→contact.
**Primary contact:** owner/manager > sales/orders > info/hello.

### 3.9 Orchestrator state (one campaign per user) — `agents/state.py`
**Storage key:** `f"user:{sender}"`, or `user:owner` with `OWNER_MODE=true`. ⚠️ Confirm `sender` is stable across one ASI:One chat.

**States:** `IDLE`, `SETUP`, `BUILDING_ICP`→`AWAIT_ICP`, `FINDING`→`AWAIT_LEADS`, `FINDING_CONTACTS`→`AWAIT_CONTACTS`,
`QUALIFYING`→`AWAIT_REVIEWS`, `DRAFTING`→`AWAIT_DRAFTS`, `SENDING`/`AWAIT_SEND`, `DONE`, `PAUSED`, `STEP_FAILED`.

```
IDLE --start_campaign(brief + city; else setup form)--> BUILDING_ICP --> AWAIT_ICP (or auto → FINDING)
AWAIT_<gate> --approve--> next step;  --remove_items(ids)--> re-render;  --reject(feedback)--> redo (max 2 per step)
  reject on leads with feedback → feedback is appended to the brief and the ICP is rebuilt
AWAIT_DRAFTS: one draft at a time (draft_cursor); approve / skip / "approve all"; edit_draft (§3.10.4)
any step error or timeout --> STEP_FAILED (failed_gate) --retry--> same step;  --skip--> next step
ANY --pause--> PAUSED (paused_from);  PAUSED --resume--> paused_from (re-shows the pending card)
ANY --status--> status detail card;  --log--> last 10 audit entries
```

### 3.10 Intent handling (`orchestrator.understand`)
1. **Card action:** text that parses as JSON with an `"action"` key. Every card selection carries `"action"` and `"step"`.
2. **Fast path (no LLM):** exact phrases (`approve`, `approve all`, `redo`, `retry`, `skip`, `pause`, `resume`, `status`, `help`,
   `settings`, `log`, `call list`, `new campaign`), `remove 2, 5`, `show lead 3`.
3. **Otherwise `llm.parse_intent(text, state, gate, last_list_names)`** →
   `{"intent": "start_campaign|approve|reject|remove_items|edit_draft|add_lead|import_list|update_settings|pause|resume|status|show_log|show_lead|classify_reply|help|unknown", "args": {}, "confidence": 0..1}`.
   Low confidence → one short clarifying message listing what can be done at this checkpoint.
4. **Learning from edits (M4)** ✅ (`orchestrator.edit_draft`, only at `AWAIT_DRAFTS`), args `{"instruction", "general", "lead_id"?}`:
   - **specific** (`general: false`): rewrites that draft (the card's `lead_id`, else the one on screen) with
     `style_rules + [instruction]`; nothing is saved.
   - **general** (`general: true`, e.g. "make them shorter"): stored as `pending_style_rule` and a `style_rule_card` asks
     to confirm (selections `{"action": "approve|reject", "step": "style_rule"}`). **approve / yes** → appended to the
     campaign's `style_rules` (logged, actor `user`) and every unapproved draft from the cursor on is rewritten;
     **reject / "just this one" / no** → applied to the current draft only. A draft card's own approve/skip
     (`step: "drafts"`) or "approve all" drops the proposal. Rewrites call `draft_emails` with just those leads; a lead
     that fails keeps its old draft.

### 3.11 Cards (`agents/cards.py`, payload schemas ✅ §4.5)
- **The narration must stand alone:** it lists the items and says how to reply in chat ("Reply approve, remove 2, 5, or redo…").
- One review card per checkpoint (≤15 rows; with more, the title says "(showing 15 of N)" and rows keep their
  numbers, so **remove N** still means row N). One card per ChatMessage. ⚠️ Whether consecutive messages (progress text + card)
  both render in ASI:One is unverified — check early.
- `EndSessionContent` only when the campaign is `DONE`.
- Builders: `setup_form`, `settings_form`, `review_card`, `draft_card`, `detail_card`, `status_card` (terminal).
- Settings-form checkbox values are undocumented, so they are parsed leniently (`true`, `"on"`, `"checked"`, …).

### 3.12 LLM prompts (ASI:One LLM via `agents/llm.py`)
All prompts demand **JSON only** (`complete_json`: parse, retry once with the error, then raise). Prompts in code:
`parse_intent`, `build_icp`, `discover_businesses` (web search), `find_websites` (web search), `qualify`, `draft_email`,
`classify_reply`. Web search is enabled per call (`complete(..., web_search=True)`) by sending
`web_search_options={"search_context_size": ASI1_SEARCH_CONTEXT}` (§4.4); calls without search send no search field at all.
It can be turned off with `ASI1_WEB_SEARCH=false`.

**Deterministic draft validator (M4, `copywriter.validate`)** ✅: body word count ≤ `email.max_words` (default 150); subject
non-empty, ≤ 8 words and ≤ 60 chars; the exact `email.unsubscribe_line` in the body; `brand.sender_name` and `brand.name`
in the body; no `{` `}` or `❓` placeholders; no price (₹/Rs/INR/$ amounts, `/-`) unless `pricing.mention_price` is `true`;
`hook_used` is null or matches a review hook's observation / evidence / angle. Profile values that are missing or still
❓ count as failures ("not filled in"), so an unfilled brand profile shows up as draft warnings, never as invented facts.
**Product claims:** a fixed list of quality words (`copywriter.CLAIM_TERMS`: single-origin, fresh-roasted, small-batch,
organic, artisan, certified, premium, wholesale, ...; hyphen/case-insensitive) may appear in the draft only if the
brief, the ICP product, the filled-in profile facts or the lead's review hooks/summary contain it.
**Prompt:** `llm.draft_email` sends only the filled-in profile facts (`llm.known_facts`: ❓/empty fields removed); the
removed fields are listed as UNKNOWN by name, and the product may be described only with the user's wording (ICP product +
brief) and those facts.
On failure regenerate once via `llm.draft_email(..., problems=[...])` (the problems are passed back to the LLM), then
return the draft with the remaining problems as `warnings` (a failing regenerate keeps the first draft and its warnings).

### 3.13 Data sources and crawling rules
**Lead sources (no Google key needed)**
1. **OpenStreetMap Nominatim** (city → bbox) ✅: `GET https://nominatim.openstreetmap.org/search?q=<city>, India&format=json&limit=1`.
   ≤1 request/s, a real User-Agent; **a placeholder contact such as example.com in the UA gets HTTP 403** ✅ (24 Oct 2026). Cached 24 h.
2. **OpenStreetMap Overpass** ✅: `POST https://overpass-api.de/api/interpreter` (fallback `https://overpass.private.coffee/api/interpreter`),
   query `nwr["<k>"="<v>"]["name"](bbox)` per tag, `out center tags`. No parallel requests; on 429/504 wait 30 s. Cached 24 h.
   Tags read: `name`, `name:en`, `website`, `contact:website`, `email`, `contact:email`, `phone`, `contact:phone`, `addr:*`.
   **Measured 24 Oct 2026:** Jaipur `amenity=cafe` in the city bbox → 86 businesses, 8 with a website, 2 with an email, 11 with a phone.
   OSM coverage of websites is thin, which is why source 3 exists.
3. **ASI:One LLM web search** ⚠️ (quality unmeasured until `scripts/probe_sources.py` runs with a key): discovers extra businesses
   and official websites. Output is **candidates only**; `lead_finder._verify_websites` crawls each candidate and keeps it only if the
   page loads and `crawl.name_matches` finds the business name. Web-only businesses without a verified site are dropped.
4. **User-provided:** `add_lead` / `import_list` (M5).
5. **Optional later:** `FOURSQUARE_API_KEY` (Foursquare Places; free tier 500 calls/month per the official pricing page ⚠️) or
   `GOOGLE_PLACES_API_KEY` (Places API (New) Text Search, `X-Goog-FieldMask` with `places.websiteUri,places.nationalPhoneNumber` =
   Enterprise SKU; only `place_id` may be stored long-term; show "Google Maps" attribution).

**Not used:** Fetch.ai's "Google Maps Places Agent" (`agent1qvcqsyxsq7fpy9z2r0quvng5xnhhwn3vy7tmn5v0zwr4nlm7hcqrckcny9e`) —
Agentverse flags it `unresponsive` (recent success 0.0), it allows 6 requests/hour, and its POI model has no website or phone. ✅ (24 Oct 2026)

**Banned sources (never fetched automatically):** Google Maps/Search scraping, Justdial, IndiaMART, Sulekha, Zomato, Swiggy,
LinkedIn, Facebook, Instagram, X (`crawl.BANNED_HOSTS`). Users may paste data they collected by hand.

**Polite crawler rules (MUST, `agents/crawl.py`)**
- robots.txt fetched once per site (`urllib.robotparser`); disallowed → skip and record `robots_disallowed`.
- User-Agent `LeadReachBot/0.1 (+mailto:<REPLY_TO>)` (no contact part if `REPLY_TO` is unset).
- ≤1 request/second per domain; 15 s timeout; follow redirects; HTML only; ≤2 MB.
- ≤5 pages per site: homepage + same-domain links matching `contact|about|team|reach|enquir|inquir|connect|location|store`.
- Never submit forms, log in, or bypass captchas or paywalls.

**Email extraction:** `mailto:` links + regex; de-obfuscate `[at]`, `(dot)`; lowercase, strip punctuation, dedupe; drop asset
names and placeholder domains. **Phones:** `tel:` links + Indian number regex. **MX:** `crawl.mx_ok(domain)` (MX, else A record).

**Website signals (`lead_analyst.site_signals`):** status, final URL, https, load ms, size, title, meta description, viewport,
latest year seen, keyword flags (online ordering, menu, events, catering), app-store links.

### 3.14 Sending (stretch), email rules and guardrails
**Resend API:** `POST https://api.resend.com/emails`, `Authorization: Bearer <RESEND_API_KEY>`, body
`{from, to, subject, text, reply_to, headers}`, optional `Idempotency-Key: "{campaign_id}:{lead_id}"`. ⚠️ Confirm free-tier limits.
- `DRY_RUN=true` logs "would send"; `TEST_REDIRECT_TO` reroutes *all* mail (use for the demo).
- Window Mon–Sat 09:30–18:00 IST, ≥10 min apart, ≤ `daily_cap`/day — use `@agent.on_schedule("*/10 * * * *", tz="Asia/Kolkata")` (§4.2).
- One email per business per campaign; at most one follow-up after ≥5 days (stretch).
- Real sender identity, `reply_to`, `List-Unsubscribe` header and the unsubscribe line in every email; unsubscribes are permanent.
- No tracking pixels or link tracking.
- **Compliance (not legal advice):** contact only addresses the business publishes; store only what's needed; delete on request;
  India's DPDP Act 2023 / Rules 2025 apply to personal data, so keep the footprint minimal.

### 3.15 Reliability rules
- Every external call has a timeout; transient errors go through `with_retry`.
- Specialist timeouts must cover the live worst case: a live ASI:One web-search call takes ~30-60 s, so the lead finder runs
  its queries concurrently and caps each at `LEADREACH_WEB_SEARCH_TIMEOUT`, and the orchestrator waits up to
  `LEADREACH_TIMEOUT_<TOOL>` (§3.6). A reply that arrives after the orchestrator's timeout is dropped by uAgents
  ("unrecognized schema digest"), so keep each specialist's own budget below its orchestrator timeout.
- Partial success is fine (e.g. OSM down → web-search leads; web search down → OSM leads). Report what's missing and why
  (`source_notes`).
- `send_and_receive` → `None` or `ok=False` → tell the user plainly and offer **retry** / **skip** (`STEP_FAILED`). Never silently move on.
- Re-running a step replaces that step's output, never duplicates it.
- The orchestrator's chat handler catches everything and always replies.

### 3.16 Configuration
- **`config/brand_profile.yaml`:** brand, offer, ideal_customers, social_proof, pricing, cta, email, targeting. Every unknown is ❓.
- **`.env`** (template `.env.example`; `setup.sh` generates seeds and derives addresses):

| Var | Meaning |
|---|---|
| `ASI1_API_KEY`, `ASI1_MODEL`, `ASI1_WEB_SEARCH` | ASI:One LLM (`asi1`), web search on/off |
| `ASI1_SEARCH_CONTEXT` | Web-search `search_context_size`: `low` \| `medium` (default) \| `high` |
| `ORCH_SEED`, `FINDER_SEED`, `CONTACT_SEED`, `ANALYST_SEED`, `WRITER_SEED`, `SENDER_SEED` | Seeds fix the agent addresses. Keep stable and secret. |
| `LEAD_FINDER_AGENT_ADDRESS`, `CONTACT_FINDER_AGENT_ADDRESS`, `LEAD_ANALYST_AGENT_ADDRESS`, `COPYWRITER_AGENT_ADDRESS`, `SENDER_AGENT_ADDRESS` | Derived: `Identity.from_seed(seed, 0).address` ✅ |
| `FOURSQUARE_API_KEY`, `GOOGLE_PLACES_API_KEY` | Optional extra lead sources (unused unless set) |
| `MAILBOX`, `OWNER_MODE`, `*_PORT` | Runtime (tests force `MAILBOX=false`) |
| `LEADREACH_TIMEOUT_FIND_LEADS`, `_FIND_CONTACTS`, `_QUALIFY_LEADS`, `_DRAFT_EMAILS`, `_SEND_EMAILS` | Optional: seconds the orchestrator waits for each specialist (defaults 300/300/300/240/60; invalid or ≤0 → default) |
| `LEADREACH_WEB_SEARCH_TIMEOUT` | Optional: seconds per lead-finder web-search call (default 90) |
| `LEADREACH_DRAFT_LLM_TIMEOUT` | Optional: seconds per copywriter `llm.draft_email` attempt (default 100; invalid or ≤0 → default) |
| `RESEND_API_KEY`, `SENDER_FROM`, `REPLY_TO`, `DRY_RUN`, `TEST_REDIRECT_TO`, `DAILY_CAP` | Sending + safety defaults |

### 3.17 File layout
```
agents/
  common.py          ToolRequest/ToolResponse, chat helpers, env helpers, with_retry, make_specialist
  llm.py             ASI:One client, complete/complete_json (+web_search), all prompts
  state.py           campaign defaults, load/save, audit log, state + gate constants
  cards.py           Interactive Card builders
  crawl.py           polite fetcher, email/phone extraction, MX check, banned hosts
  geo.py             Nominatim + Overpass
  orchestrator.py    chat protocol, intent, state machine, gates          ✅ M0
  lead_finder.py     find_leads                                           ✅ M1 logic
  contact_finder.py  find_contacts                                        ✅ M2 logic
  lead_analyst.py    qualify_leads                                        ✅ M3 logic
  copywriter.py      draft_emails + validator                             ✅ M4 logic
  sender.py          send_emails (shell → M6, stretch)
  mock_test.py       offline end-to-end test (mocked LLM + OSM + fetch)
  local_test.py      live loop: all agents + interactive fake user (real LLM/network)
  profiles/*.md      Agentverse profile READMEs (badges + keywords)
config/brand_profile.yaml
scripts/probe_sources.py   go/no-go check for OSM + ASI:One web search
pyproject.toml, uv.lock, requirements.txt (exported for deploys), setup.sh
```
Shared modules are fine for mailbox agents run from this repo. If an agent moves to an **Agentverse hosted** agent, inline its
helpers into one file (§4.7).

### 3.18 Milestones and acceptance tests
**Mock test** ✅ (`agents/mock_test.py`, 96 checks): `MAILBOX=false` + dummy seeds before imports; `llm.*`, `geo.*`, the
fetcher and `mx_ok` are monkeypatched; all agents + a scripted user run in a `Bureau(port=8010)`; exit code 0 = pass.

| Milestone | Done when |
|---|---|
| **M0** ✅ | Repo set up; all 6 agents boot; mock test passes; orchestrator reachable in ASI:One via mailbox (user step) |
| **M1** Leads | `probe_sources.py` GO; real Jaipur run returns ≥10 leads with verified website or email; ICP + leads cards render in ASI:One; "remove 2" works |
| **M2** Contacts | ≥50% of leads with websites get an email; every contact has `source_url` + `mx_ok`; low confidence flagged |
| **M3** Reviews | Every reachable site gets signals; ≥1 evidence-backed hook for most; "show lead N" detail card |
| **M4** Drafts | All drafts pass the validator; an edit becomes a style rule and changes the next draft |
| **M5** Gates | Settings form + NL toggle gates; auto-approve logs `ai_auto`; pause/resume mid-flow; add_lead/import_list |
| **M6** Sender (stretch) | DRY_RUN + TEST_REDIRECT_TO deliver to the test inbox; cap/window/suppression enforced |
| **M7** Ship | All agents deployed (not a laptop), profiles filled, shared chat URL captured, README table complete, video, submitted |

### 3.19 Demo script (3–4 min)
1. **(20 s)** Problem: "Small brands selling to local businesses spend days finding leads; AI tools spam."
2. "I sell organic coffee beans — find cafés and bakeries in Jaipur" → **ICP card** → approve.
3. **Leads card:** sources shown (OSM / WEB, verified websites) → "remove 4 and 9" → approve.
4. **Contacts card:** one row `⚠ low`; show its source URL. Approve.
5. **Reviews:** "show lead 2" → evidence (e.g. "menu lists no single-origin coffee", "opened a 2nd outlet in 2026").
6. **Draft 1:** "make it warmer, don't mention price" → draft 2 already follows the rule.
7. "Stop asking me about reviews" → second mini-batch runs faster but stops at drafts; audit log shows `ai_auto`.
8. **Send:** dry-run with redirect → email arrives in the test inbox → "pause" freezes the queue.
9. **(20 s)** Architecture: 6 agents on Agentverse, ASI:One LLM + web search, cards, Payment Protocol if built.

### 3.20 Out of scope and stretch
- **Out of scope:** auto-replying, inbox OAuth, open/click tracking, scraping banned sources, guessing emails.
- **Stretch, in order:** (1) Sender; (2) reply classification + funnel report; (3) **Payment Protocol** (§4.6) — (A) other brands buy
  a "verified lead pack" from `leadreach` (Stripe test mode, the orchestrator as seller), or (B) agent-to-agent pay-per-verified-contact
  (FET testnet), which maps to "agent-to-agent payments".

### 3.21 Open questions for Chirag ❓
1. Which real brand/product do we demo with? (fills `config/brand_profile.yaml`)
2. Demo city and target business types.
3. May emails name existing customers? Mention price?
4. Sending domain and `SENDER_FROM` (verify in Resend), plus the test inbox for the demo.
5. Is anyone else on the team? Who owns which agent?

---

## 4. Fetch.ai technical reference

### 4.1 Versions and environment
- **Packages:** `uagents==0.26.1` (8 Oct 2026), `uagents-core==0.4.12` (13 Oct 2026). ✅ Installed with `uv sync` and tested (mock test). 0.26 adds `on_schedule`, event telemetry (`report_events=True` by default) and an experimental health protocol.
- **Tooling:** `uv` (`uv sync`, `uv run`), lockfile `uv.lock`; `requirements.txt` is exported with `uv export --no-hashes --no-dev --no-emit-project` for Render.
- **Protocols:** Chat Protocol `AgentChatProtocol 0.3.0` ✅; Payment Protocol `AgentPaymentProtocol 0.1.0` ✅.
- **Python:** 3.10–3.13. 3.13 tested OK ✅. Another Fetch-A-Thon team's README says 3.14 isn't supported yet ⚠️.
- **Models:** `uagents_core.models.Model` subclasses **`pydantic.v1.BaseModel`**. ✅

### 4.2 uAgents essentials ✅ (introspected)
- **`Agent(...)` parameters:**
  - identity and networking: `name`, `port`, `seed`, `endpoint`, `mailbox`, `network` (default `"mainnet"`);
  - profile: `readme_path`, `description`, `handle`, `avatar_url`, `publish_agent_details=True`;
  - behaviour: `handle_messages_concurrently` (**set `True` on the orchestrator** so it can await specialists).
- **Handlers:**
  - `@agent.on_event("startup")`
  - `@agent.on_interval(period=float)`
  - `@agent.on_schedule(cron: str, tz: str | None = None)` (0.26+, cron-style, e.g. `"*/10 9-18 * * 1-6", tz="Asia/Kolkata"`) ✅ introspected
  - `@proto.on_message(Model, replies=Model | {..})`
  - `agent.include(proto, publish_manifest=True)`
- **Context:**
  - `await ctx.send(addr, msg)`;
  - `await ctx.send_and_receive(destination, message, response_type, sync=False, timeout=30)` returns `(reply | None, MsgStatus)`;
  - `ctx.logger`;
  - `ctx.storage.get/set/has/remove/clear` (JSON-serialisable values).
- **`Protocol(name=None, version=None, spec=None, role=None)`.**
- **`Bureau`:** runs several agents in one process (local testing). **Not allowed in Agentverse hosted agents.**
- **Addresses** derive from seeds: `Identity.from_seed(seed, 0).address == Agent(seed=seed).address`. ✅

### 4.3 Chat Protocol ✅
- **Imports** (from `uagents_core.contrib.protocols.chat`):
  - messages: `ChatMessage(timestamp, msg_id, content)`, `ChatAcknowledgement(timestamp, acknowledged_msg_id, metadata)`;
  - content types: `TextContent(type="text", text)`, `MetadataContent(type="metadata", metadata: dict[str, str])`, `ResourceContent(type, resource_id, resource)`;
  - session and stream markers: `StartSessionContent`, `EndSessionContent`, `StartStreamContent`, `EndStreamContent`;
  - the spec: `chat_protocol_spec`.
- **Rules:**
  - acknowledge every incoming `ChatMessage` first;
  - ignore content-free messages (e.g. `StartSessionContent` only);
  - also register an `on_message(ChatAcknowledgement)` handler;
  - `agent.include(chat_proto, publish_manifest=True)` so ASI:One can discover the agent.
- **Already implemented:** `chat_text()`, `ack()`, `text_of()` in `agents/common.py`. ✅

### 4.4 ASI:One LLM
- **Client:** OpenAI-compatible: `OpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI1_API_KEY)`. ✅ Per the Innovation Lab guide; implemented in `agents/llm.py`.
- **Model names:** `asi1` (default, balanced), `asi1-ultra` (deepest reasoning, agentic runs), `asi1-mini` (fastest) per the current quickstart ✅. Older docs mention `asi1-fast`/`asi1-extended`.
- **Web search:** on `/v1/chat/completions` pass a `web_search_options` object; its presence is the opt-in, and without it there is
  no search. Allowed values: `{}` or `{"search_context_size": "low|medium|high"}`; any other key → HTTP 400 (ASI:One web-search doc).
  In Python: `client.chat.completions.create(..., web_search_options={"search_context_size": "medium"})` (native kwarg in the
  installed `openai` SDK). ❌ The old boolean `extra_body={"web_search": True}` from the OpenAI-compatibility doc is now rejected
  with HTTP 400 "Unrecognized request argument supplied: web_search", even when `False` (verified live 24 Oct 2026).
  Whether replies include source URLs is undocumented ⚠️ → `scripts/probe_sources.py`.
- **API key:** asi1.ai → Developer → API Keys. An ASI:One Pro/Premium subscription does not by itself put a key in `.env`.
- **Agentic models** (`asi1-agentic`, `asi1-fast-agentic`, `asi1-extended-agentic`) search and call Agentverse agents themselves and need an `x-session-id` header. Not needed for LeadReach.
- **Using it for decisions** (intent, hooks, drafts, classification) is what the "ASI:One LLM central to reasoning" criterion rewards.

### 4.5 Interactive Cards ✅ (Innovation Lab doc)
A card is a normal `ChatMessage` with `[TextContent(narration), MetadataContent(metadata)]`. All metadata values are **strings**.
```python
import json
from uuid import uuid4
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent
from common import now

def card(narration: str, kind: str, payload: dict, terminal: bool = False) -> ChatMessage:
    meta = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": kind,                       # carousel | detail | form | review | custom
        "card_payload": json.dumps(payload),     # payload at top level, no envelope; <= 64 KB
        # "preferred_drawer_width_px": "540",   # optional, clamped 320-800
    }
    if terminal:
        meta["is_terminal"] = "true"            # informational card, no input expected
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=[
        TextContent(type="text", text=narration),
        MetadataContent(type="metadata", metadata=meta),
    ])
```
**Payload shapes:**
- **carousel:** `{title, subtitle, items:[{id, image?, title, subtitle?, badges?:[{label, variant: info|success|warning}], secondary_text?, primary_cta:{label, selection:{...}}}]}`
- **detail:** `{title, hero_image?, summary_rows:[{label, value}], sub_options?:{name, kind:"radio", label, choices:[{value, label, secondary_text?}]}, ctas:[{label, selection:{...}, primary?}]}`
- **form:** `{title, fields:[{name, kind: text|number|email|select|checkbox, label, required?, options?:[{value, label}] (required for select)}], submit_cta:{label, selection:{...}}}`
- **review:** `{title, summary_rows:[{label, value}], approve_cta:{label, selection, primary?}, reject_cta:{label, selection}}`
- **custom:** `{"root": ...}` built from section, group, divider, text, heading, image, badge, button, input, list, choice_grid. Max 8 levels.

**What comes back when the user acts:**
- **carousel:** the item's `selection`.
- **detail:** the chosen `sub_options` value merged with the clicked CTA's `selection`.
- **form:** the field values (keyed by `name`) merged with `submit_cta.selection`.
- **review:** the approve or reject `selection`.

**How it arrives:**
- via direct @mention: a **JSON string**;
- via the planner: **prose** mentioning every identifier.

So try `json.loads` first, then fall back to the LLM intent parser. If validation fails, ASI:One shows **only the narration**, so make the narration self-sufficient.

### 4.6 Payment Protocol ✅ (introspected + Innovation Lab doc)
- **Imports** (from `uagents_core.contrib.protocols.payment`):
  - `Funds(amount: str, currency: str, payment_method: str)`. `payment_method` is a free string, not an enum; documented values are `stripe`, `skyfire` and `fet_direct` (default).
  - `RequestPayment(accepted_funds: list[Funds], recipient: str, deadline_seconds: int, reference?, description?, metadata?: dict[str, str | dict[str, str]])`
  - `CommitPayment(funds: Funds, recipient: str, transaction_id: str, reference?, description?, metadata?)`
  - `RejectPayment(reason?)`, `CompletePayment(transaction_id?)`, `CancelPayment(transaction_id?, reason?)`
  - `payment_protocol_spec`
- **Roles:**
  - **seller** handles `CommitPayment` and `RejectPayment`;
  - **buyer** handles `RequestPayment`, `CompletePayment` and `CancelPayment`.
  - Create the protocol with the matching role, or you get locked-spec errors.
- **Flow:**
  1. The seller sends `RequestPayment`.
  2. The buyer replies `CommitPayment` (with a `transaction_id`) or `RejectPayment`.
  3. The seller **verifies the payment**, then sends `CompletePayment` or `CancelPayment`.
- **Stripe:** `payment_method="stripe"`. The `transaction_id` is usually the Stripe Checkout Session ID, and `metadata={"stripe": {...}}` carries hints for embedded checkout.
- **Working reference:** the Parley project (§6).
- **Open questions:** ⚠️ what `recipient` should hold (agent vs wallet address), and how ASI:One renders non-Stripe methods. Both are undocumented; test before demoing.
```python
from uagents import Protocol
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, Funds, RejectPayment, RequestPayment, payment_protocol_spec)

pay_proto = Protocol(spec=payment_protocol_spec, role="seller")

@pay_proto.on_message(CommitPayment)
async def on_commit(ctx, sender, msg: CommitPayment):
    if await verify_payment(msg.transaction_id):          # implement: Stripe session status / chain tx check
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    else:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="payment not verified"))

@pay_proto.on_message(RejectPayment)
async def on_reject(ctx, sender, msg: RejectPayment):
    ctx.logger.info(f"payment rejected: {msg.reason}")

agent.include(pay_proto, publish_manifest=True)
# request: await ctx.send(buyer, RequestPayment(accepted_funds=[Funds(amount="1.00", currency="usd", payment_method="stripe")],
#                                               recipient=<see ⚠️>, deadline_seconds=600, reference=order_id, description="..."))
```

### 4.7 Agentverse
- **Mailbox agents** (what we use):
  - run the agent with `mailbox=True`;
  - open the **Agent Inspector** link from the logs → **Connect** → **Mailbox**;
  - edit the profile on Agentverse;
  - use "Chat with Agent" to open it in ASI:One.
  - Needs outbound internet only; no public endpoint. ✅
- **Ranking factors** (they decide Marketplace and ASI:One placement) ✅. Nine factors, no published weights:
  1. domain association
  2. **mainnet** registration
  3. verification
  4. **README quality**
  5. interaction metrics (recent, total, positive feedback)
  6. **active status** (inactive agents aren't ranked)
  7. **Chat Protocol support**
  8. avatar or logo
  9. **handle**
- **Ranking tips:**
  - short name (<20 chars);
  - README with a clear title, use cases, how to interact, limitations and keywords;
  - set a handle;
  - keep the agent active and update it regularly.
- **The dashboard** also has Testing (manual + generated tests, quality score), Discovery (queries, impressions) and Growth tabs.
- **Hosted agents** (optional; we run mailbox agents instead):
  - Single agent per project; **`Bureau` not allowed**.
  - `Agent(...)` is a wrapper whose config is ignored (seed, name and address are fixed by the platform).
  - Global variables reset between calls, so use storage.
  - Only **allowed imports**, which include: `uagents`, `uagents-core`, `requests`, `httpx`, `aiohttp`, `openai`, `beautifulsoup4`/`bs4`, `lxml`, **`dnspython`**, `pydantic`, `tenacity`, `PyYAML`, `python-dateutil`, `validators`, `RapidFuzz`, `langchain*`, `langgraph`, `anthropic`, `mcp`, `pymongo`, `asyncpg`, `SQLAlchemy`, `numpy`. So our specialists *could* be hosted.
- **Hosting API** ⚠️ (older v1 docs; confirm current):
  - auth: `Authorization: Bearer <AGENTVERSE_API_KEY>`
  - agents: `GET/POST /v1/hosting/agents`, `GET/DELETE /v1/hosting/agents/{addr}`
  - code: `GET/PUT /v1/hosting/agents/{addr}/code` (body `{"code": "<string>"}`)
  - lifecycle: `POST /v1/hosting/agents/{addr}/start|stop`
  - logs: `GET /v1/hosting/agents/{addr}/logs/latest`
  - secrets: `POST /v1/hosting/secrets {address, name, secret}`
  - a newer v2 also exists: `POST /v2/hosting`
- **Search API** ✅ (public):
  - request: `POST https://agentverse.ai/v1/search/agents` with `{search_text, semantic_search, limit<=50, sort: relevancy|created-at|last-modified|interactions, offset, filters}`;
  - response: `agents[]` with `name, address, handle, protocols, description, rating, total_interactions, recent_success_rate, status, ...`
  - **Use it to check whether competing agents exist.**
- **MCP servers:** Agentverse full `https://mcp.agentverse.ai/sse`, lite `https://mcp-lite.agentverse.ai/mcp`.

### 4.8 Deployment (agents must stay online until results on 28 Oct)
- **Render:** the Innovation Lab guide uses a **Background Worker** (no HTTP server needed for mailbox agents):
  - build: `pip install -r requirements.txt`
  - start: `python <agent>.py`
  - env vars set in the dashboard; then open the Inspector link from the logs to connect the mailbox.
  - ⚠️ Check Render pricing: background workers may not be on the free plan.
- **Alternatives:** Railway, or a small VPS running all agents with a process manager (one process per agent; ports 8001–8006).
- **Seeds must match** what was registered, otherwise the addresses change and the README, profiles and env all break.
- **After deploying,** re-run the full ASI:One flow and capture the **shared chat URL** from the deployed agents.

### 4.9 fetch-skills (coding-assistant knowledge packs) ✅
- **Install:** `npx fetch-skills` (interactive). Skills include `chat-protocol`, `payment-protocol`, `stripe-payment-protocol` and `fet-payment-protocol`, plus package-setup skills.
- **Targets:** Claude Code → `.claude/skills/<skill>/SKILL.md`; Cursor → `.cursor/skills/...`.
- **Avoid** the "AGENTS.md / generic" target: it overwrites `AGENTS.md`.

---

## 5. Repo state (24 Oct 2026)
- **M0 done:** all files in §3.17 exist; the 6 agents boot; `agents/mock_test.py` passes (20 checks) ✅.
- **Implemented:** orchestrator (intent, gates, cards, pause/resume, settings, retry/skip, audit log), `find_leads`
  (OSM + web search + verification), `find_contacts` (polite crawl + extraction + MX, mock-tested only), `qualify_leads`
  (crawl + signals + LLM hooks filtered against page text, signals-only fallback; mock-tested only), `draft_emails`
  (LLM drafts + deterministic validator, regenerate once, warnings; mock-tested only), orchestrator `edit_draft`
  (per-draft edits, general edits → style rules after confirmation), crawler, geo, all LLM prompts.
- **Shells (return `ok=False, "not implemented yet"`):** `send_emails` (M6). `add_lead` / `import_list` / `classify_reply`
  are answered "coming soon" (M5).
- **Not yet done by Chirag:** `ASI1_API_KEY` in `.env`, run `setup.sh`, `scripts/probe_sources.py`, mailbox connection, Agentverse profiles.
- The earlier generic starter (`specialist.py`/`page_check`) was folded into `common.make_specialist` and `lead_analyst.site_signals`.

---

## 6. Patterns from past Fetch.ai winners (what judges reward)
- **Parley** (LA Hacks 2026, freight): a buyer-facing mailbox uAgent plus 5 carrier agents with **private pricing policies**.
  - The **whole flow runs in ASI:One cards:** form → offers carousel → review → payment → receipt.
  - Approval cards appear when a replacement exceeds the buyer's limit.
  - It acts as the Payment Protocol **seller** with Stripe sandbox, and **verifies payment server-side** before confirming.
- **Agent Santa** (Cal Hacks 12.0, **Best Use of ASI:One**): a gifting agent plus "personality agents" representing each person; Chat Protocol; simulated payments.
- **Fetch-A-Thon 2.0 winners:** Open Sandbox (a decentralised agent hub for reuse), Soldier Health Monitoring System, Rentora (a local marketplace).
- **Takeaways:**
  - agents that represent different parties with their own goals;
  - every step inside ASI:One;
  - cards at each decision;
  - reliability visible in the demo.

---

## 7. Competitive context and pitch
- **Explee** (AI GTM / outbound agent): finds B2B prospects, writes cold emails, sends from a pool of pre-warmed inboxes (~$0.03/email).
  Reviewer complaints: sending started ~2 hours after the user *didn't* approve the leads; incorrect offers; no own sender domain;
  Trustpilot 2.4/5 (6 reviews).
- **AI SDRs:** 11x (Alice), Artisan (Ava), AiSDR; Apollo moving into agentic prospecting. Autopilot-oriented and B2B-database-driven,
  with poor coverage of Indian local businesses.
- **Lead scrapers** (Google Maps scrapers, Justdial exports): break platform ToS and give no reason to write.
- **LeadReach's wedge:** approval-first with per-step switches; local-business focus from open data (OSM) + ASI:One web search;
  evidence-or-nothing contacts and hooks; the brand's own domain and voice; open and agent-native on Agentverse.
- **Pitch line:** *"Explee sends without asking. LeadReach asks at every step, until you tell it not to."*

---

## 8. Submission checklist
- [ ] All agents deployed (not a laptop), mailbox connected, `active` on Agentverse
- [ ] Each agent's profile: name ≤20 chars, handle, avatar, README with badges, keywords, use cases, limitations
- [ ] Full workflow completed in ASI:One → **Shared Chat URL** copied
- [ ] `README.md`: badges, every agent's name + address + profile URL, run instructions, extra resources with links, architecture
- [ ] Public GitHub repo; commits show work done during the event
- [ ] Demo video 3–5 min (problem → live ASI:One run → architecture and Fetch.ai tech → impact)
- [ ] Submitted via the dashboard Project Submission Agent: shared chat URL, profile URLs, repo, video, description (problem, target users, outcome)
- [ ] `DRY_RUN` / redirect settings confirmed, so no real business is emailed by accident during judging

---

## 9. References
**Hackathon**
- Event page: https://innovationlab.fetch.ai/events/fetch-a-thon-3
- Hackpack (rules, criteria, deliverables): https://innovationlab.fetch.ai/events/hackathons/fetch-a-thon-3/hackpack

**Fetch.ai / Innovation Lab docs**
- uAgent creation (incl. mailbox agents): https://innovationlab.fetch.ai/resources/docs/agent-creation/uagent-creation
- uAgent ↔ uAgent communication: https://innovationlab.fetch.ai/resources/docs/agent-communication/uagent-uagent-communication
- ASI:One-compatible uAgents (Chat Protocol): https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents
- ASI:One quickstart: https://innovationlab.fetch.ai/resources/docs/asione/asi-one-quickstart
- ASI:One overview: https://innovationlab.fetch.ai/resources/docs/asione/asi-one-overview
- ASI1 agentic models: https://innovationlab.fetch.ai/resources/docs/asione/models/asi1-agentic
- Interactive Cards: https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards
- Agent Payment Protocol: https://innovationlab.fetch.ai/resources/docs/agent-transaction/agent-payment-protocol
- fetch-skills: https://innovationlab.fetch.ai/resources/docs/fetch-skills/overview
- MCP integration: https://innovationlab.fetch.ai/resources/docs/mcp-integration/what-is-mcp
- Deploy on Agentverse via Render: https://innovationlab.fetch.ai/resources/docs/agentverse/deploy-agent-on-agentverse-via-render
- Image generation agent example: https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/image-generation-agent
- Innovation Lab examples repo: https://github.com/fetchai/innovation-lab-examples
- Past hackathon projects: https://innovationlab.fetch.ai/projects

**Agentverse docs**
- Hosted agents: https://docs.agentverse.ai/documentation/launch-agents/hosted-agents
- Allowed imports: https://docs.agentverse.ai/documentation/advanced-usages/allowed-imports
- Ranking score: https://docs.agentverse.ai/documentation/agent-discovery/agents-ranking-score
- Dashboard and testing: https://docs.agentverse.ai/documentation/agent-optimization/dashboard-and-build-tab
- Search API: https://docs.agentverse.ai/v-1/api-reference/search/agents
- Hosting API (v1): https://fetch.ai/docs/apis/agentverse/hosting
- Secrets API: https://fetch.ai/docs/guides/apis/secret-management-apis
- Create hosted agent (v2): https://docs.agentverse.ai/v-1/api-reference/hosting-v-2/create-hosted-agent-v-2

**External services**
- Overpass QL: https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL
- Nominatim usage policy: https://operations.osmfoundation.org/policies/nominatim/
- ASI:One web search (`web_search_options`): https://docs.asi1.ai/documentation/build-with-asi-one/web-search.md
- ASI:One OpenAI compatibility (outdated `web_search` boolean): https://innovationlab.fetch.ai/resources/docs/asione/build/openai-compatibility
- Google Places Text Search (New), optional: https://developers.google.com/maps/documentation/places/web-service/text-search
- Foursquare pricing, optional: https://foursquare.com/pricing/
- Fetch.ai Google Maps Places Agent source (not used): https://github.com/fetchai/uAgent-Examples/tree/main/6-deployed-agents/geo/google-maps-places-agent
- Overpass usage policy and instances: https://wiki.openstreetmap.org/wiki/Overpass_API
- Resend send-email API: https://resend.com/docs/api-reference/emails/send-email
- Resend free tier (secondary source): https://automationatlas.io/answers/resend-free-tier-explained-2026/

**Prior art / context**
- Parley (LA Hacks 2026): https://github.com/Parth-Dua/freight-recovery-exchange
- Agent Santa (Cal Hacks 12.0): https://devpost.com/software/agent-santa
- Fetch-A-Thon 2.0 gallery: https://fetch-a-thon-2-0.devpost.com/project-gallery
- Explee review (Salesforge): https://www.salesforge.ai/blog/explee-review
- AI SDR landscape (11x): https://www.11x.ai/guides/ai-sdr-agents-b2b-sales-teams
- DPDP consent managers (context): https://www.mondaq.com/building-the-consent-layer-consent-managers-under-the-dpdp-rules-2025/1832140
